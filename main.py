"""Decky SoT: Sea of Thieves helpers for the Steam Deck.

First feature: when the first rain of the storm reaches the islands chosen by
the player, shown in game through the shared Decky Overlay plugin
(powange/decky-overlay).

The storm follows the same path on every server. Its forecast (about the next
two hours) comes from the Merfolk's Lullaby GraphQL API, like the Stream Deck
plugin powange/streamdeck-sot: each forecast point says when and where the
centre of the storm is over an island. Between two points the centre moves the
way their own map animates it (straight line, eased by speed), and the rain is
a disc of their storm shape's size around it, so the first rain on an island is
when that disc touches the island's outline. Two settings calibrate it against
the game: the rain size and a time offset. Nothing is read from the game.
"""
import asyncio
import glob
import json
import math
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
FORECAST_QUERY = "query getBetaWeather { getBetaWeather { eid date coordinates } }"
ISLANDS_QUERY = ('query getElements { getElements(type: "island") '
                 '{ id attributes { type value localization { locale value } } } }')
EXCLUDED_REALMS = {"sea-of-the-damned"}  # the storm never goes there
ISLANDS_CACHE_VERSION = 2       # 2: island outlines added

# ---- Storm model (from the Merfolk's Lullaby map) ------------------------- #
# Map coordinates are longitude/latitude near (-64.4, 18.6). Longitudes are
# scaled by cos(latitude) so distances are the same in every direction.
LNG_SCALE = math.cos(math.radians(18.63))
# Largest radius of their "StormShape" polygon, in scaled degrees (~650 m).
STORM_RADIUS = 0.0059
SAMPLE_STEP = 10                # seconds between two computed storm positions
METERS_PER_DEGREE = 110574.0

# ---- Decky Overlay --------------------------------------------------------- #
OVERLAY_SOCKET = "/tmp/decky-overlay/overlay.sock"
OVERLAY_PLUGIN_NAME = "Decky Overlay"
OVERLAY_CLIENT = "decky-sot"
OVERLAY_BLOCK = "storm"
OVERLAY_RETRY = 3               # seconds between connection attempts
OVERLAY_MAX_ITEMS = 12          # per block, protocol limit
OVERLAY_VALUE_MAX = 16          # longer texts are cut with "…" by the overlay
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
    "rain_scale": 100,          # rain radius, % of the storm shape drawn by Merfolk's Lullaby
    "offset_seconds": 0,        # added to every forecast time (game later than the site: > 0)
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
    "rain_scale": _number(50, 400),
    "offset_seconds": _number(-3600, 3600),
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


def _coordinates(value):
    """'[lng, lat]' (JSON text) -> scaled (x, y), or None."""
    try:
        lng, lat = json.loads(value) if isinstance(value, str) else value
        return (float(lng) * LNG_SCALE, float(lat))
    except (TypeError, ValueError):
        return None


def parse_forecast(data):
    """getBetaWeather -> [{"at", "eid", "pos"}], by time (pos: scaled x, y or None)."""
    out = []
    for p in (data or {}).get("getBetaWeather") or []:
        if not isinstance(p, dict):
            continue
        at = _parse_date(p.get("date"))
        if at is None:
            continue
        eid = p.get("eid")
        out.append({"at": at, "eid": eid.upper() if isinstance(eid, str) and eid else None,
                    "pos": _coordinates(p.get("coordinates"))})
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


def convex_hull(points):
    """Andrew's monotone chain; returns the hull counter-clockwise."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _outline(attributes):
    """Island "area" GeoJSON -> convex hull in scaled coordinates, rounded."""
    area = next((a.get("value") for a in attributes or [] if isinstance(a, dict) and a.get("type") == "area"), None)
    try:
        geo = json.loads(area) if isinstance(area, str) else None
        points = []
        for feature in (geo or {}).get("features") or []:
            geometry = feature.get("geometry") or {}
            polygons = geometry.get("coordinates") or []
            if geometry.get("type") == "Polygon":
                polygons = [polygons]
            for polygon in polygons:
                for ring in polygon:
                    points.extend((float(x) * LNG_SCALE, float(y)) for x, y in ring)
    except (TypeError, ValueError, AttributeError):
        return []
    return [[round(x, 7), round(y, 7)] for x, y in convex_hull(points)]


def parse_islands(data):
    """getElements(type: "island") -> [{"id", "name", "hull"}], sorted by name."""
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
            out[e["id"].upper()] = {"id": e["id"].upper(), "name": name, "hull": _outline(attrs)}
    return sorted(out.values(), key=lambda i: i["name"].casefold())


def _ease(fraction, speed):
    """Progress along a segment, as the Merfolk's Lullaby map animates it."""
    if speed <= 2.5:
        return fraction ** 4
    if speed <= 3.5:
        return 1 - math.cos(fraction * math.pi / 2)
    if speed <= 4.25:
        return fraction * fraction
    return -fraction * (fraction - 2)


def storm_position(forecast, t):
    """Centre of the storm at time t (scaled x, y), or None outside the forecast."""
    for a, b in zip(forecast, forecast[1:]):
        if a["at"] <= t <= b["at"] and a["pos"] and b["pos"]:
            duration = b["at"] - a["at"]
            if duration <= 0:
                return b["pos"]
            dx, dy = b["pos"][0] - a["pos"][0], b["pos"][1] - a["pos"][1]
            speed = math.hypot(dx, dy) * METERS_PER_DEGREE / duration
            e = _ease((t - a["at"]) / duration, speed)
            return (a["pos"][0] + dx * e, a["pos"][1] + dy * e)
    return None


def _segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    u = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(a[0] + u * dx - p[0], a[1] + u * dy - p[1])


def distance_to_outline(p, hull):
    """Distance from point p to a convex outline (0 inside)."""
    if not hull:
        return math.inf
    if len(hull) >= 3:
        inside = True
        for i in range(len(hull)):
            a, b = hull[i], hull[(i + 1) % len(hull)]
            if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) < 0:
                inside = False
                break
        if inside:
            return 0.0
    if len(hull) == 1:
        return math.hypot(p[0] - hull[0][0], p[1] - hull[0][1])
    return min(_segment_distance(p, hull[i], hull[(i + 1) % len(hull)]) for i in range(len(hull)))


def rain_windows(forecast, hull, radius, step=SAMPLE_STEP):
    """Rain periods over an island: [[start, end]] (unix times, no offset).

    start is None when it already rains at the start of the forecast, end is
    None when it still rains at its end.
    """
    usable = [p for p in forecast if p["pos"]]
    if len(usable) < 2 or not hull:
        return []
    windows = []
    t, last = usable[0]["at"], usable[-1]["at"]
    raining = False
    while True:
        pos = storm_position(usable, t)
        wet = pos is not None and distance_to_outline(pos, hull) <= radius
        if wet and not raining:
            windows.append([None if t == usable[0]["at"] else t, None])
        elif raining and not wet:
            windows[-1][1] = t
        raining = wet
        if t >= last:
            break
        t = min(last, t + step)
    return windows


def storm_status(windows, forecast_until, now, offset=0, horizon=HORIZON):
    """Where the rain stands for one island.

    {"state": "now", "until": t|None}  it rains on the island (until t)
    {"state": "soon", "at": t}         first rain at t (within the horizon)
    {"state": "none"}                  not within the horizon
    {"state": "unknown"}               no usable forecast
    """
    if not forecast_until or forecast_until + offset < now:
        return {"state": "unknown"}
    for start, end in windows:
        start = None if start is None else start + offset
        end = None if end is None else end + offset
        if (start is None or start <= now) and (end is None or now < end):
            return {"state": "now", "until": end}
        if start is not None and now < start <= now + horizon:
            return {"state": "soon", "at": start}
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
        self.islands = []            # [{"id", "name", "hull"}] from the API (cached)
        self.rain_cache = {}         # island id -> rain windows for the current forecast
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
            if data.get("version") != ISLANDS_CACHE_VERSION:
                raise FileNotFoundError  # older cache without outlines: fetch again
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
                        self.rain_cache = {}
                        self._write_json(self.islands_path, {"version": ISLANDS_CACHE_VERSION,
                                                             "fetched_at": now, "islands": islands})
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
                    self.rain_cache = {}
                except Exception as e:
                    self.forecast_error = f"Prévision indisponible : {e}"
                    decky.logger.warning(self.forecast_error)
        finally:
            self.fetching = False
        self._sync_overlay()

    def _rain_windows(self, island_id):
        """Rain periods over one island (cached until the forecast or size changes)."""
        if island_id not in self.rain_cache:
            island = next((i for i in self.islands if i["id"] == island_id), None)
            hull = [tuple(p) for p in (island or {}).get("hull") or []]
            if not hull:
                # No outline: the points where the storm centre is over the island.
                hull = [p["pos"] for p in self.forecast if p["eid"] == island_id and p["pos"]][:1]
            radius = STORM_RADIUS * self.settings["rain_scale"] / 100
            self.rain_cache[island_id] = rain_windows(self.forecast, hull, radius)
        return self.rain_cache[island_id]

    def _statuses(self, now):
        out = []
        until = self.forecast[-1]["at"] if self.forecast else 0
        for index, island_id in enumerate(self.settings["islands"]):
            status = storm_status(self._rain_windows(island_id), until, now, self.settings["offset_seconds"])
            out.append({"id": island_id, "name": self._island_name(island_id), "index": index, **status})
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
                    # Decky Overlay shows at most VALUE_MAX (16) characters.
                    text = f"Pluie, fin {time.strftime('%H:%M', time.localtime(s['until']))}" \
                        if s.get("until") else "Pluie en cours"
                    items.append({"label": s["name"], "text": text, "style": "alert"})
                elif s["state"] == "soon":
                    item = {"label": s["name"], "countdown": {"end_at": s["at"]}}
                    if limit and not preview:
                        item["show_under"] = limit
                    items.append(item)
                elif s["state"] == "none":
                    if not self.settings["hide_without_storm"] or preview:
                        items.append({"label": s["name"], "text": "Pas de tempête", "style": "muted"})
                else:
                    items.append({"label": s["name"], "text": "Sans prévision", "style": "warn"})
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
            "forecast_until": (self.forecast[-1]["at"] + self.settings["offset_seconds"]) if self.forecast else 0,
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
        return [{"id": i["id"], "name": i["name"]} for i in self.islands]

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
        if "rain_scale" in patch and patch["rain_scale"] != self.settings["rain_scale"]:
            self.rain_cache = {}
        self.settings.update(patch)
        return self._changed()

    async def calibrate(self, island_id: str):
        """"The rain starts on this island now": sets the offset from the forecast.

        Uses the predicted first rain on that island closest to now (without
        offset), within an hour. Returns the state, with "calibrated" set to the
        new offset in seconds, or None when there is nothing to match.
        """
        island_id = str(island_id).upper()
        now = _now()
        starts = [w[0] for w in self._rain_windows(island_id) if w[0] is not None]
        best = min(starts, key=lambda t: abs(now - t), default=None)
        if best is None or abs(now - best) > 3600:
            return {**self._state(), "calibrated": None}
        self.settings["offset_seconds"] = int(round(now - best))
        state = self._changed()
        return {**state, "calibrated": self.settings["offset_seconds"]}

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
