"""Decky SoT: Sea of Thieves helpers for the Steam Deck.

First feature: when the storm reaches the islands chosen by the player, shown
in game through the shared Decky Overlay plugin (powange/decky-overlay).

The storm follows the same path on every server. Its forecast (about the next
two hours) comes from the Merfolk's Lullaby GraphQL API, like the Stream Deck
plugin powange/streamdeck-sot: each forecast point says when the centre of the
storm is over an island. Nothing is read from the game itself.
"""
import asyncio
import glob
import json
import os
import re
import ssl
import time
import urllib.request

import decky

# ---- Merfolk's Lullaby ------------------------------------------------------ #
API_URL = "https://api.merfolkslullaby.com/api"
USER_AGENT = "decky-sot (https://github.com/powange/decky-sot)"
HTTP_TIMEOUT = 15
FORECAST_TTL = 24 * 60          # same polling interval as their map (one in-game day)
FORECAST_MIN_AHEAD = 20 * 60    # refresh early when the forecast ends sooner than this
MIN_FETCH_INTERVAL = 2 * 60     # never call the API more often than this
ISLANDS_TTL = 7 * 24 * 3600     # island list cache
HORIZON = 2 * 3600              # beyond this, an island shows "Pas de tempête"
FORECAST_QUERY = "query getBetaWeather { getBetaWeather { eid date } }"
ISLANDS_QUERY = ('query getElements { getElements(type: "island") '
                 '{ id attributes { type value localization { locale value } } } }')
EXCLUDED_REALMS = {"sea-of-the-damned"}  # the storm never goes there

# ---- Decky Overlay --------------------------------------------------------- #
OVERLAY_SOCKET = "/tmp/decky-overlay/overlay.sock"
OVERLAY_PLUGIN_NAME = "Decky Overlay"
OVERLAY_CLIENT = "decky-sot"
OVERLAY_BLOCK = "storm"
OVERLAY_RETRY = 3               # seconds between connection attempts
OVERLAY_MAX_ITEMS = 12          # per block, protocol limit
PREVIEW_SECONDS = 10

# ---- Game detection -------------------------------------------------------- #
GAME_EXE = "sotgame.exe"        # Sea of Thieves under Proton (Steam app 1172620)
GAME_CHECK_INTERVAL = 5

MAX_ISLANDS = OVERLAY_MAX_ITEMS
NAME_MAX = 28
ID_RE = re.compile(r"^[0-9A-Fa-f-]{8,64}$")

DEFAULT_SETTINGS = {
    "overlay_enabled": True,
    "position": "top-left",     # top-left | top-center | top-right | bottom-left | bottom-right
    "only_in_game": True,       # overlay (and API calls) only while Sea of Thieves runs
    "hide_without_storm": False,  # hide islands the storm will not reach within 2 h
    "sort_by_time": True,       # soonest storm first, instead of the chosen order
    "show_under_minutes": 0,    # only show a countdown in its last N minutes (0 = always)
    "islands": [],              # chosen island ids, in the chosen order
    "overlay_install_prompted": False,
}


# ---- Input validation ------------------------------------------------------ #
def _bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("not a boolean")


def _choice(*allowed):
    def check(v):
        if v in allowed:
            return v
        raise ValueError(f"not one of {allowed}")
    return check


def _number(lo, hi, cast=int):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("not a number")
        v = cast(v)
        if not lo <= v <= hi:
            raise ValueError(f"out of range [{lo}, {hi}]")
        return v
    return check


def _island_ids(v):
    if not isinstance(v, list):
        raise ValueError("not a list")
    out = []
    for i in v:
        if isinstance(i, str) and ID_RE.match(i) and i.upper() not in out:
            out.append(i.upper())
    return out[:MAX_ISLANDS]


SETTINGS_SCHEMA = {
    "overlay_enabled": _bool,
    "position": _choice("top-left", "top-center", "top-right", "bottom-left", "bottom-right"),
    "only_in_game": _bool,
    "hide_without_storm": _bool,
    "sort_by_time": _bool,
    "show_under_minutes": _number(0, 120),
    "islands": _island_ids,
    "overlay_install_prompted": _bool,
}


def _valid_settings(raw):
    """Known keys with valid values only; anything else is dropped and logged."""
    out = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        check = SETTINGS_SCHEMA.get(key)
        if check is None:
            continue
        try:
            out[key] = check(value)
        except (ValueError, TypeError) as e:
            decky.logger.warning(f"Ignoring setting {key}={value!r}: {e}")
    return out


def _now() -> float:
    return time.time()


# ---- API parsing (pure functions, tested) ---------------------------------- #
def _parse_date(value):
    """ISO 8601 date of the API ("2026-11-22T14:38:13.646Z") -> Unix time."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse_forecast(data):
    """getBetaWeather -> [{"at": unix time, "eid": island id or None}], by time."""
    out = []
    for p in (data or {}).get("getBetaWeather") or []:
        if not isinstance(p, dict):
            continue
        at = _parse_date(p.get("date"))
        if at is None:
            continue
        eid = p.get("eid")
        out.append({"at": at, "eid": eid.upper() if isinstance(eid, str) and eid else None})
    out.sort(key=lambda p: p["at"])
    return out


def _english_name(attributes):
    for a in attributes or []:
        if not isinstance(a, dict) or a.get("type") != "name":
            continue
        loc = a.get("localization")
        locs = loc if isinstance(loc, list) else [loc] if isinstance(loc, dict) else []
        best = next((l for l in locs if l.get("locale") == "en"), locs[0] if locs else None)
        if best and best.get("value"):
            return " ".join(str(best["value"]).split())
    return None


def parse_islands(data):
    """getElements(type: "island") -> [{"id", "name"}], sorted by name."""
    out = {}
    for e in (data or {}).get("getElements") or []:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            continue
        attrs = e.get("attributes") or []
        realm = next((a.get("value") for a in attrs if isinstance(a, dict) and a.get("type") == "realm"), None)
        if realm in EXCLUDED_REALMS:
            continue
        name = _english_name(attrs)
        if name:
            out[e["id"].upper()] = name
    return sorted(({"id": i, "name": n} for i, n in out.items()), key=lambda i: i["name"].casefold())


def storm_status(forecast, island_id, now, horizon=HORIZON):
    """Where the storm stands for one island.

    {"state": "now"}            the centre of the storm is over the island
    {"state": "soon", "at": t}  it reaches the island at t (within the horizon)
    {"state": "none"}           not within the horizon
    {"state": "unknown"}        no usable forecast
    """
    if not forecast or forecast[-1]["at"] < now:
        return {"state": "unknown"}
    current = None
    for p in forecast:
        if p["at"] <= now:
            current = p
    if current is not None and current["eid"] == island_id:
        return {"state": "now"}
    for p in forecast:
        if now < p["at"] <= now + horizon and p["eid"] == island_id:
            return {"state": "soon", "at": p["at"]}
    return {"state": "none"}


def _ssl_context():
    ctx = ssl.create_default_context()
    # Decky's bundled Python may not find the system certificates.
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        if os.path.exists(path):
            try:
                ctx.load_verify_locations(cafile=path)
                break
            except (OSError, ssl.SSLError):
                continue
    return ctx


def _graphql(query):
    """Blocking GraphQL call (run in a thread)."""
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_ssl_context()) as res:
        payload = json.loads(res.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("data"):
        errors = payload.get("errors") if isinstance(payload, dict) else None
        raise RuntimeError((errors or [{}])[0].get("message") or "réponse vide")
    return payload["data"]


def _game_running():
    """True while Sea of Thieves runs (SoTGame.exe under Proton)."""
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(path, "rb") as f:
                cmd = f.read(4096).lower()
        except OSError:
            continue
        if GAME_EXE.encode() in cmd:
            return True
    return False


def _overlay_installed():
    """True if a plugin named "Decky Overlay" is in Decky's plugins folder."""
    plugins = os.path.join(getattr(decky, "DECKY_HOME", "/home/deck/homebrew"), "plugins")
    for manifest in glob.glob(os.path.join(plugins, "*", "plugin.json")):
        try:
            with open(manifest) as f:
                if json.load(f).get("name") == OVERLAY_PLUGIN_NAME:
                    return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


class OverlayClient:
    """Connection to the Decky Overlay plugin's socket (same as Decky Timer)."""

    def __init__(self, on_connect):
        self.on_connect = on_connect
        self.reader = None
        self.writer = None
        self.read_task = None
        self.last_attempt = 0.0
        self.error = ""

    @property
    def connected(self):
        return self.writer is not None

    async def ensure(self):
        """Connects if needed (called from the watch loop, rate limited)."""
        if self.writer is not None or _now() - self.last_attempt < OVERLAY_RETRY:
            return
        self.last_attempt = _now()
        if not os.path.exists(OVERLAY_SOCKET):
            return
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_unix_connection(OVERLAY_SOCKET), 2)
        except Exception as e:
            self.reader = self.writer = None
            self.error = f"Connexion impossible : {e}"
            return
        self.error = ""
        self.send({"op": "hello", "client": OVERLAY_CLIENT, "name": "Decky SoT", "protocol": 1})
        self.read_task = asyncio.get_event_loop().create_task(self._read())
        self.on_connect()

    async def _read(self):
        try:
            while self.reader is not None:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("op") == "error":
                    self.error = f"Decky Overlay : {msg.get('error')}"
                    decky.logger.warning(self.error)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self._drop()

    def send(self, msg):
        if self.writer is None:
            return
        try:
            self.writer.write((json.dumps(msg) + "\n").encode())
        except Exception:
            self._drop()

    def _drop(self):
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = self.writer = None

    def close(self):
        if self.read_task:
            self.read_task.cancel()
        self._drop()


class Plugin:
    def __init__(self):
        self.state_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "state.json")
        self.islands_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "islands.json")
        self.settings = dict(DEFAULT_SETTINGS)
        self.islands = []            # [{"id", "name"}] from the API (cached)
        self.islands_at = 0.0
        self.islands_error = ""
        self.forecast = []
        self.forecast_at = 0.0       # last successful fetch
        self.forecast_error = ""
        self.last_fetch = 0.0        # last attempt (success or not)
        self.fetching = False
        self.game_running = False
        self.game_checked = 0.0
        self.panel_open_until = 0.0  # the panel wants fresh data even outside the game
        self.preview_until = 0.0
        self.overlay = OverlayClient(on_connect=self._force_sync)
        self.overlay_installed = False
        self.overlay_checked = 0.0
        self.last_sent = None
        self.watch_task = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def _main(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        self._load()
        self.watch_task = asyncio.get_event_loop().create_task(self._watch())
        decky.logger.info("Decky SoT loaded")

    async def _unload(self):
        if self.watch_task:
            self.watch_task.cancel()
        self.overlay.close()
        decky.logger.info("Decky SoT unloaded")

    async def _uninstall(self):
        self.overlay.close()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self):
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self.settings = {**DEFAULT_SETTINGS, **_valid_settings(data.get("settings", {}))}
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Could not read state: {e}")
        try:
            with open(self.islands_path) as f:
                data = json.load(f)
            self.islands = [i for i in data.get("islands", [])
                            if isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("name"), str)]
            self.islands_at = float(data.get("fetched_at", 0))
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Could not read the island cache: {e}")

    def _write_json(self, path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _save(self):
        self._write_json(self.state_path, {"settings": self.settings})

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #

    def _island_name(self, island_id):
        name = next((i["name"] for i in self.islands if i["id"] == island_id), None)
        return (name or "Île inconnue")[:NAME_MAX]

    def _wants_data(self, now):
        """API calls only while they are useful: in game, or with the panel open."""
        if not self.settings["islands"] and now >= self.panel_open_until:
            return False
        if now < self.panel_open_until:
            return True
        return self.settings["overlay_enabled"] and (self.game_running or not self.settings["only_in_game"])

    def _forecast_due(self, now):
        if now - self.last_fetch < MIN_FETCH_INTERVAL:
            return False
        if not self.forecast or now - self.forecast_at >= FORECAST_TTL:
            return True
        return self.forecast[-1]["at"] < now + FORECAST_MIN_AHEAD

    async def _refresh(self, force=False):
        """Fetches the island list and the forecast when they are due."""
        if self.fetching:
            return
        now = _now()
        self.fetching = True
        try:
            if not self.islands or now - self.islands_at > ISLANDS_TTL:
                try:
                    islands = parse_islands(await asyncio.to_thread(_graphql, ISLANDS_QUERY))
                    if islands:
                        self.islands, self.islands_at, self.islands_error = islands, now, ""
                        self._write_json(self.islands_path, {"fetched_at": now, "islands": islands})
                except Exception as e:
                    self.islands_error = f"Liste des îles indisponible : {e}"
                    decky.logger.warning(self.islands_error)
            if self._forecast_due(now) or (force and now - self.last_fetch >= 10):
                self.last_fetch = now
                try:
                    forecast = parse_forecast(await asyncio.to_thread(_graphql, FORECAST_QUERY))
                    if not forecast:
                        raise RuntimeError("prévision vide")
                    self.forecast, self.forecast_at, self.forecast_error = forecast, now, ""
                except Exception as e:
                    self.forecast_error = f"Prévision indisponible : {e}"
                    decky.logger.warning(self.forecast_error)
        finally:
            self.fetching = False
        self._sync_overlay()

    def _statuses(self, now):
        out = []
        for index, island_id in enumerate(self.settings["islands"]):
            out.append({"id": island_id, "name": self._island_name(island_id), "index": index,
                        **storm_status(self.forecast, island_id, now)})
        if self.settings["sort_by_time"]:
            rank = {"now": 0, "soon": 1, "none": 2, "unknown": 2}
            out.sort(key=lambda s: (rank[s["state"]], s.get("at", 0), s["index"]))
        return out

    # ------------------------------------------------------------------ #
    # Decky Overlay
    # ------------------------------------------------------------------ #

    def _overlay_status(self):
        if self.overlay.connected:
            return "connected"
        if _now() - self.overlay_checked > 10:
            self.overlay_installed = _overlay_installed()
            self.overlay_checked = _now()
        return "not_running" if self.overlay_installed else "not_installed"

    def _overlay_message(self, now):
        items = []
        preview = now < self.preview_until
        visible = self.settings["overlay_enabled"] and (self.game_running or not self.settings["only_in_game"])
        if preview or visible:
            limit = int(self.settings["show_under_minutes"]) * 60
            for s in self._statuses(now):
                if s["state"] == "now":
                    items.append({"label": s["name"], "text": "Tempête !", "style": "alert"})
                elif s["state"] == "soon":
                    item = {"label": s["name"], "countdown": {"end_at": s["at"]}}
                    if limit and not preview:
                        item["show_under"] = limit
                    items.append(item)
                elif s["state"] == "none":
                    if not self.settings["hide_without_storm"] or preview:
                        items.append({"label": s["name"], "text": "Pas de tempête", "style": "muted"})
                else:
                    items.append({"label": s["name"], "text": "Prévision indispo.", "style": "warn"})
            if preview and not items:
                items.append({"label": "Aperçu Decky SoT", "text": "Choisis une île", "style": "muted"})
        if not items:
            return {"op": "clear", "id": OVERLAY_BLOCK}
        return {"op": "set", "id": OVERLAY_BLOCK, "zone": self.settings["position"], "order": 0,
                "items": items[:OVERLAY_MAX_ITEMS]}

    def _sync_overlay(self):
        """Sends the block when it changed (no-op when not connected)."""
        msg = self._overlay_message(_now())
        if msg != self.last_sent and self.overlay.connected:
            self.overlay.send(msg)
            self.last_sent = msg

    def _force_sync(self):
        self.last_sent = None
        self._sync_overlay()

    async def _watch(self):
        while True:
            try:
                await asyncio.sleep(2)
                now = _now()
                if now - self.game_checked >= GAME_CHECK_INTERVAL:
                    self.game_checked = now
                    running = await asyncio.to_thread(_game_running)
                    if running != self.game_running:
                        self.game_running = running
                        decky.logger.info(f"Sea of Thieves {'started' if running else 'stopped'}")
                if self._wants_data(now) and self._forecast_due(now):
                    await self._refresh()
                await self.overlay.ensure()
                self._sync_overlay()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"watch loop: {e}")

    # ------------------------------------------------------------------ #
    # Frontend API
    # ------------------------------------------------------------------ #

    def _state(self):
        now = _now()
        return {
            "now": now,
            "settings": self.settings,
            "statuses": self._statuses(now),
            "forecast_at": self.forecast_at,
            "forecast_until": self.forecast[-1]["at"] if self.forecast else 0,
            "forecast_error": self.forecast_error,
            "islands_count": len(self.islands),
            "islands_error": self.islands_error,
            "game_running": self.game_running,
            "overlay_status": self._overlay_status(),
            "overlay_error": self.overlay.error,
        }

    def _changed(self):
        self._save()
        self._sync_overlay()
        return self._state()

    async def get_state(self):
        # The panel is open: keep the data fresh for a few minutes, even outside the game.
        self.panel_open_until = _now() + 5 * 60
        if not self.islands or not self.forecast:
            await self._refresh()
        return self._state()

    async def get_startup_state(self):
        """State without any network call (plugin load, menu closed)."""
        return self._state()

    async def get_version(self) -> str:
        version = os.environ.get("DECKY_PLUGIN_VERSION")
        if version:
            return version
        try:
            with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json")) as f:
                return json.load(f).get("version", "?")
        except Exception:
            return "?"

    async def get_islands(self):
        if not self.islands:
            await self._refresh()
        return self.islands

    async def refresh_forecast(self):
        await self._refresh(force=True)
        return self._state()

    async def add_island(self, island_id: str):
        ids = _island_ids([island_id])
        if ids and ids[0] not in self.settings["islands"] and len(self.settings["islands"]) < MAX_ISLANDS:
            self.settings["islands"] = self.settings["islands"] + ids
        return self._changed()

    async def remove_island(self, island_id: str):
        self.settings["islands"] = [i for i in self.settings["islands"] if i != str(island_id).upper()]
        return self._changed()

    async def move_island(self, island_id: str, delta: int):
        ids = list(self.settings["islands"])
        island_id = str(island_id).upper()
        if island_id in ids:
            idx = ids.index(island_id)
            new = max(0, min(len(ids) - 1, idx + (1 if int(delta) > 0 else -1)))
            ids.insert(new, ids.pop(idx))
            self.settings["islands"] = ids
        return self._changed()

    async def save_settings(self, settings: dict):
        patch = _valid_settings(settings)
        patch.pop("islands", None)  # changed through add/remove/move only
        self.settings.update(patch)
        return self._changed()

    async def preview_overlay(self):
        self.preview_until = _now() + PREVIEW_SECONDS
        self._sync_overlay()
        return self._state()

    async def overlay_install_prompted(self):
        """Remembers that the Decky Overlay install was offered (once only)."""
        self.settings["overlay_install_prompted"] = True
        self._save()
        self.overlay_checked = 0.0
        return self._state()
