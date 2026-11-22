"""Forecast parsing, storm status per island and the overlay block."""
import asyncio
import json
import os

import main

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


FORECAST = main.parse_forecast(load("forecast.json"))
T0 = FORECAST[0]["at"]           # 14:38:13 UTC, storm over 7802F2DD
A = "4D88AC8D-1CE6-45DC-9175-F91E31F0A473"   # reached at 14:48, again at 16:46
B = "EECA09D5-5671-4813-83EC-B36ED2F9CB7F"   # reached at 15:46, again at 16:27
NEVER = "11111111-2222-3333-4444-555555555555"


def test_parse_forecast_sorted_and_upper():
    assert len(FORECAST) == 15
    assert all(a["at"] <= b["at"] for a, b in zip(FORECAST, FORECAST[1:]))
    assert FORECAST[0]["eid"] == "7802F2DD-78EA-47F1-BAF7-5F9CB8C0551B"


def test_parse_forecast_ignores_bad_points():
    data = {"getBetaWeather": [{"eid": "abc", "date": "pas une date"}, None, {"eid": None, "date": "2026-11-22T15:00:00Z"}]}
    assert main.parse_forecast(data) == [{"at": main._parse_date("2026-11-22T15:00:00Z"), "eid": None}]
    assert main.parse_forecast(None) == []


def test_status_soon_then_now_then_next_pass():
    first = next(p["at"] for p in FORECAST if p["eid"] == A)
    assert main.storm_status(FORECAST, A, T0) == {"state": "soon", "at": first}
    # Over the island until the storm reaches the next point.
    assert main.storm_status(FORECAST, A, first + 1) == {"state": "now"}
    after = next(p["at"] for p in FORECAST if p["at"] > first) + 1
    second = [p["at"] for p in FORECAST if p["eid"] == A][1]
    assert main.storm_status(FORECAST, A, after) == {"state": "soon", "at": second}


def test_status_none_and_unknown():
    assert main.storm_status(FORECAST, NEVER, T0) == {"state": "none"}
    assert main.storm_status([], A, T0) == {"state": "unknown"}
    # Forecast entirely in the past: unusable.
    assert main.storm_status(FORECAST, A, FORECAST[-1]["at"] + 60) == {"state": "unknown"}


def test_status_beyond_horizon_is_none():
    second_b = [p["at"] for p in FORECAST if p["eid"] == B][0]
    assert main.storm_status(FORECAST, B, T0, horizon=second_b - T0 - 1) == {"state": "none"}


def test_parse_islands_skips_sea_of_the_damned():
    islands = main.parse_islands(load("islands.json"))
    assert len(islands) == 6
    assert all(i["id"] == i["id"].upper() for i in islands)
    assert "Sea of the Damned" not in [i["name"] for i in islands]
    assert [i["name"] for i in islands] == sorted((i["name"] for i in islands), key=str.casefold)


def test_settings_validation():
    s = main._valid_settings({
        "position": "milieu", "show_under_minutes": 999, "only_in_game": "oui",
        "islands": [A.lower(), A, "../../etc", 42, B], "inconnu": 1,
    })
    assert s == {"islands": [A, B]}
    assert len(main._island_ids([f"{i:08X}-0000" for i in range(30)])) == main.MAX_ISLANDS


def _with_forecast(plugin, now):
    plugin.forecast = FORECAST
    plugin.islands = [{"id": A, "name": "Alpha"}, {"id": B, "name": "Bravo"}, {"id": NEVER, "name": "Nulle part"}]
    plugin.settings["islands"] = [NEVER, B, A]
    plugin.game_running = True
    return plugin._overlay_message(now)


def test_overlay_block_sorted_by_time(plugin):
    msg = _with_forecast(plugin, T0)
    assert msg["op"] == "set" and msg["id"] == "storm" and msg["zone"] == "top-left"
    assert [i["label"] for i in msg["items"]] == ["Alpha", "Bravo", "Nulle part"]
    assert "countdown" in msg["items"][0] and msg["items"][2] == {
        "label": "Nulle part", "text": "Pas de tempête", "style": "muted"}


def test_overlay_block_now_alert_and_hidden_islands(plugin):
    first = next(p["at"] for p in FORECAST if p["eid"] == A)
    plugin.settings.update(hide_without_storm=True, sort_by_time=False, show_under_minutes=30)
    msg = _with_forecast(plugin, first + 1)
    assert [i["label"] for i in msg["items"]] == ["Bravo", "Alpha"]
    assert msg["items"][0]["show_under"] == 1800
    assert msg["items"][1] == {"label": "Alpha", "text": "Tempête !", "style": "alert"}


def test_overlay_only_in_game(plugin):
    _with_forecast(plugin, T0)
    plugin.game_running = False
    assert plugin._overlay_message(T0) == {"op": "clear", "id": "storm"}
    plugin.settings["only_in_game"] = False
    assert plugin._overlay_message(T0)["op"] == "set"


def test_add_remove_move_islands(plugin):
    run = asyncio.run
    run(plugin.add_island(A.lower()))
    run(plugin.add_island(B))
    run(plugin.add_island(A))           # no duplicate
    assert plugin.settings["islands"] == [A, B]
    run(plugin.move_island(B, -1))
    assert plugin.settings["islands"] == [B, A]
    run(plugin.remove_island(B))
    assert plugin.settings["islands"] == [A]
    run(plugin.save_settings({"islands": [], "position": "bottom-right"}))
    assert plugin.settings["islands"] == [A] and plugin.settings["position"] == "bottom-right"
    with open(plugin.state_path) as f:
        assert json.load(f)["settings"]["position"] == "bottom-right"


def test_no_api_call_without_need(plugin):
    plugin.game_running = True
    assert not plugin._wants_data(T0)            # no island chosen
    plugin.settings["islands"] = [A]
    assert plugin._wants_data(T0)
    plugin.game_running = False
    assert not plugin._wants_data(T0)            # game closed
    plugin.panel_open_until = T0 + 60
    assert plugin._wants_data(T0)                # panel open


def test_forecast_due(plugin):
    plugin.forecast, plugin.forecast_at, plugin.last_fetch = FORECAST, T0, T0
    assert not plugin._forecast_due(T0 + 60)
    assert plugin._forecast_due(T0 + main.FORECAST_TTL)
    assert plugin._forecast_due(FORECAST[-1]["at"] - 60)   # forecast almost over
