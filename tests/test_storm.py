"""Forecast parsing, rain model per island and the overlay block."""
import asyncio
import json
import os

import main

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


FORECAST = main.parse_forecast(load("forecast.json"))
ISLANDS = main.parse_islands(load("islands.json"))
HULLS = {i["id"]: [tuple(p) for p in i["hull"]] for i in ISLANDS}
T0 = FORECAST[0]["at"]
UNTIL = FORECAST[-1]["at"]
R = main.STORM_RADIUS
A = "F59220AE-F4EC-4652-A808-9F091D13F582"   # centre over it at 01:31:11
B = "EECA09D5-5671-4813-83EC-B36ED2F9CB7F"   # centre over it at 01:54:59
NEVER = "11111111-2222-3333-4444-555555555555"


def centre_time(eid):
    return next(p["at"] for p in FORECAST if p["eid"] == eid)


def test_parse_forecast():
    assert len(FORECAST) == 15
    assert all(a["at"] <= b["at"] for a, b in zip(FORECAST, FORECAST[1:]))
    assert all(p["pos"] for p in FORECAST)
    bad = {"getBetaWeather": [{"eid": "abc", "date": "pas une date"}, None,
                              {"eid": None, "date": "2026-11-22T15:00:00Z", "coordinates": "oups"}]}
    assert main.parse_forecast(bad) == [{"at": main._parse_date("2026-11-22T15:00:00Z"), "eid": None, "pos": None}]


def test_parse_islands_outlines():
    assert len(ISLANDS) == 6                      # Sea of the Damned skipped
    assert all(len(i["hull"]) >= 3 for i in ISLANDS)
    assert [i["name"] for i in ISLANDS] == sorted((i["name"] for i in ISLANDS), key=str.casefold)


def test_storm_position_follows_the_points():
    for p in FORECAST:
        assert main.storm_position(FORECAST, p["at"]) == p["pos"] or \
            main.storm_position(FORECAST, p["at"]) == next(q["pos"] for q in FORECAST if q["at"] == p["at"])
    assert main.storm_position(FORECAST, T0 - 1) is None


def test_ease_ends():
    for speed in (1, 3, 4, 5):
        assert main._ease(0, speed) == 0 and abs(main._ease(1, speed) - 1) < 1e-9


def test_first_rain_comes_before_the_centre():
    for eid in (A, B):
        windows = main.rain_windows(FORECAST, HULLS[eid], R)
        start = next(w[0] for w in windows if w[0] is not None)
        assert start <= centre_time(eid)
        assert centre_time(eid) - start < 10 * 60
        # Bigger rain: earlier.
        bigger = main.rain_windows(FORECAST, HULLS[eid], R * 1.5)
        assert next(w[0] for w in bigger if w[0] is not None) <= start


def test_status_soon_now_and_offset():
    windows = main.rain_windows(FORECAST, HULLS[B], R)
    start, end = next(w for w in windows if w[0] is not None)
    assert main.storm_status(windows, UNTIL, start - 60) == {"state": "soon", "at": start}
    now = main.storm_status(windows, UNTIL, start + 1)
    assert now["state"] == "now" and now["until"] == end
    # Game 10 min late compared to the site.
    assert main.storm_status(windows, UNTIL, start + 1, offset=600) == {"state": "soon", "at": start + 600}


def test_status_none_and_unknown():
    assert main.storm_status([], UNTIL, T0) == {"state": "none"}
    assert main.storm_status([], 0, T0) == {"state": "unknown"}
    assert main.storm_status([], UNTIL, UNTIL + 60) == {"state": "unknown"}
    windows = main.rain_windows(FORECAST, HULLS[B], R)
    start = next(w[0] for w in windows if w[0] is not None)
    assert main.storm_status(windows, UNTIL, T0, horizon=start - T0 - 1) == {"state": "none"}


def test_settings_validation():
    s = main._valid_settings({
        "position": "milieu", "show_under_minutes": 999, "only_in_game": "oui", "rain_scale": 10,
        "offset_seconds": -600, "islands": [A.lower(), A, "../../etc", 42, B], "inconnu": 1,
    })
    assert s == {"islands": [A, B], "offset_seconds": -600}


def _setup(plugin):
    plugin.forecast = FORECAST
    plugin.islands = [dict(i, name=n) for i, n in zip(
        (next(x for x in ISLANDS if x["id"] == A), next(x for x in ISLANDS if x["id"] == B)), ("Alpha", "Bravo"))]
    plugin.islands.append({"id": NEVER, "name": "Nulle part", "hull": []})
    plugin.settings["islands"] = [NEVER, B, A]
    plugin.game_running = True


def test_overlay_block_sorted_by_time(plugin):
    _setup(plugin)
    msg = plugin._overlay_message(T0 + 60)
    assert msg["op"] == "set" and msg["id"] == "storm"
    assert [i["label"] for i in msg["items"]] == ["Alpha", "Bravo", "Nulle part"]
    assert "countdown" in msg["items"][0]
    assert msg["items"][2] == {"label": "Nulle part", "text": "Pas de tempête", "style": "muted"}


def test_overlay_rain_now(plugin):
    _setup(plugin)
    plugin.settings.update(hide_without_storm=True, sort_by_time=False)
    msg = plugin._overlay_message(centre_time(A))
    alpha = next(i for i in msg["items"] if i["label"] == "Alpha")
    assert alpha["style"] == "alert" and alpha["text"].startswith("Pluie")
    assert "Nulle part" not in [i["label"] for i in msg["items"]]


def test_overlay_only_in_game(plugin):
    _setup(plugin)
    plugin.game_running = False
    assert plugin._overlay_message(T0) == {"op": "clear", "id": "storm"}


def test_calibrate_sets_offset(plugin, monkeypatch):
    _setup(plugin)
    start = next(w[0] for w in plugin._rain_windows(B) if w[0] is not None)
    monkeypatch.setattr(main, "_now", lambda: start + 540)   # rain seen 9 min later in game
    state = asyncio.run(plugin.calibrate(B))
    assert state["calibrated"] == 540 and plugin.settings["offset_seconds"] == 540
    monkeypatch.setattr(main, "_now", lambda: start + 5 * 3600)
    assert asyncio.run(plugin.calibrate(B))["calibrated"] is None


def test_rain_scale_change_clears_cache(plugin):
    _setup(plugin)
    before = plugin._rain_windows(B)
    asyncio.run(plugin.save_settings({"rain_scale": 150}))
    after = plugin._rain_windows(B)
    assert after[0][0] < before[0][0]


def test_add_remove_move_islands(plugin):
    run = asyncio.run
    run(plugin.add_island(A.lower()))
    run(plugin.add_island(B))
    run(plugin.add_island(A))
    assert plugin.settings["islands"] == [A, B]
    run(plugin.move_island(B, -1))
    assert plugin.settings["islands"] == [B, A]
    run(plugin.remove_island(B))
    assert plugin.settings["islands"] == [A]


def test_no_api_call_without_need(plugin):
    plugin.game_running = True
    assert not plugin._wants_data(T0)
    plugin.settings["islands"] = [A]
    assert plugin._wants_data(T0)
    plugin.game_running = False
    assert not plugin._wants_data(T0)
    plugin.panel_open_until = T0 + 60
    assert plugin._wants_data(T0)


def test_overlay_texts_fit(plugin, monkeypatch):
    """Decky Overlay cuts values longer than 16 characters with an ellipsis."""
    _setup(plugin)
    texts = set()
    for t in range(int(T0), int(UNTIL) + 600, 60):
        for item in plugin._overlay_message(t).get("items", []):
            if "text" in item:
                texts.add(item["text"])
    plugin.forecast = []
    texts.update(i["text"] for i in plugin._overlay_message(T0)["items"])
    assert any(t.startswith("Pluie, fin") for t in texts) and "Sans prévision" in texts
    assert all(len(t) <= main.OVERLAY_VALUE_MAX for t in texts), texts
