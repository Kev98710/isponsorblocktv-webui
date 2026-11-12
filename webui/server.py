"""
iSponsorBlockTV WebUI
=====================

A small web interface for iSponsorBlockTV (https://github.com/dmunozv04/iSponsorBlockTV).

The WebUI runs *inside the same container* as iSponsorBlockTV and acts as a tiny
process supervisor: it starts the original iSponsorBlockTV process as a child,
captures its log output and restarts it after configuration changes.
No access to the Docker socket or the host is required.

Only the Python standard library and aiohttp (already shipped with iSponsorBlockTV)
are used.
"""

import asyncio
import base64
import collections
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
from types import SimpleNamespace

from aiohttp import ClientSession, web

# --------------------------------------------------------------------------------------
# Settings (environment variables)
# --------------------------------------------------------------------------------------

APP_DIR = os.environ.get("ISBTV_APP_DIR", "/app")
_data = os.environ.get("iSPBTV_data_dir", "data")
DATA_DIR = os.path.abspath(_data if os.path.isabs(_data) else os.path.join(APP_DIR, _data))
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SECRET_FILE = os.path.join(DATA_DIR, ".webui_secret")

HOST = os.environ.get("WEBUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEBUI_PORT", "8080"))
PASSWORD = os.environ.get("WEBUI_PASSWORD", "")
SESSION_HOURS = int(os.environ.get("WEBUI_SESSION_HOURS", "12"))
LOG_LINES = int(os.environ.get("WEBUI_LOG_LINES", "500"))
# Command used to start iSponsorBlockTV. The default matches the official image.
ISBTV_CMD = json.loads(os.environ.get("ISBTV_CMD", '["python3", "-u", "main.pyc"]'))
ISBTV_EXTRA_ARGS = os.environ.get("ISBTV_ARGS", "").split()

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)  # iSponsorBlockTV package (compiled .pyc in the official image)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VERSION = "1.0.0"

CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Categories as defined by iSponsorBlockTV (constants.skip_categories)
DEFAULT_CATEGORIES = [
    "sponsor", "selfpromo", "intro", "outro", "music_offtopic",
    "interaction", "exclusive_access", "preview", "filler", "hook",
]

# Defaults as used by iSponsorBlockTV's Config class
DEFAULT_CONFIG = {
    "devices": [],
    "apikey": "",
    "skip_categories": ["sponsor"],
    "channel_whitelist": [],
    "skip_count_tracking": True,
    "mute_ads": False,
    "skip_ads": False,
    "minimum_skip_length": 1,
    "auto_play": True,
    "join_name": "iSponsorBlockTV",
    "use_proxy": False,
}

log = logging.getLogger("webui")


def load_upstream_categories():
    """Use the category list from iSponsorBlockTV itself if it is importable."""
    try:
        from iSponsorBlockTV import constants  # type: ignore

        return [c[1] for c in constants.skip_categories]
    except Exception:  # noqa: BLE001 - fall back to the known list
        return DEFAULT_CATEGORIES


CATEGORIES = load_upstream_categories()


# --------------------------------------------------------------------------------------
# Configuration file handling
# --------------------------------------------------------------------------------------

class ConfigError(ValueError):
    """Raised for invalid user input. The message is a translation key."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


def read_config():
    """Return the raw config dict (unknown keys are preserved)."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ConfigError("error.config_unreadable")


def _open_private(path, mode):
    """Open a file for writing with permissions 0600 (config.json contains the API key)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    return os.fdopen(fd, "wb")


def write_config(data):
    """Write the config atomically and keep one backup. The existing file mode is kept."""
    os.makedirs(DATA_DIR, exist_ok=True)
    mode = 0o600
    if os.path.exists(CONFIG_FILE):
        try:
            mode = os.stat(CONFIG_FILE).st_mode & 0o777
            with open(CONFIG_FILE, "rb") as src, _open_private(CONFIG_FILE + ".bak", mode) as dst:
                dst.write(src.read())
        except OSError:
            pass
    tmp = CONFIG_FILE + ".tmp"
    with _open_private(tmp, mode) as f:
        f.write(json.dumps(data, indent=4, ensure_ascii=False).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, CONFIG_FILE)


def effective_config(raw):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(raw)
    # Remove placeholder entries from the template (empty screen ids / channel ids)
    cfg["devices"] = [d for d in cfg.get("devices") or [] if isinstance(d, dict) and d.get("screen_id")]
    cfg["channel_whitelist"] = [
        c for c in cfg.get("channel_whitelist") or [] if isinstance(c, dict) and c.get("id")
    ]
    return cfg


def config_problems(cfg):
    """Problems that would make iSponsorBlockTV refuse to start."""
    problems = []
    if "atvs" in cfg:
        problems.append("problem.legacy_atvs")
    if not cfg["devices"]:
        problems.append("problem.no_devices")
    if cfg["channel_whitelist"] and not cfg.get("apikey"):
        problems.append("problem.whitelist_needs_apikey")
    return problems


def public_config(cfg):
    """Config for the browser. Only known settings are returned, the API key never."""
    out = {k: copy.deepcopy(cfg[k]) for k in DEFAULT_CONFIG if k not in ("apikey", "devices")}
    out["has_apikey"] = bool(cfg.get("apikey"))
    out["devices"] = [
        {"screen_id": d["screen_id"], "name": d.get("name") or "", "offset": d.get("offset", 0),
         "legacy_id": len(d["screen_id"]) == 26}
        for d in cfg["devices"]
    ]
    return out


def _bool(value, key):
    if isinstance(value, bool):
        return value
    raise ConfigError("error.invalid_value", field=key)


def apply_settings(raw, body):
    """Apply the editable settings from the browser to the raw config dict."""
    cfg = effective_config(raw)

    if "skip_categories" in body:
        cats = body["skip_categories"]
        if not isinstance(cats, list) or any(c not in CATEGORIES for c in cats):
            raise ConfigError("error.invalid_value", field="skip_categories")
        raw["skip_categories"] = [c for c in CATEGORIES if c in cats]

    for key in ("skip_count_tracking", "mute_ads", "skip_ads", "auto_play", "use_proxy"):
        if key in body:
            raw[key] = _bool(body[key], key)

    if "minimum_skip_length" in body:
        try:
            value = float(body["minimum_skip_length"])
        except (TypeError, ValueError):
            raise ConfigError("error.invalid_value", field="minimum_skip_length")
        if value < 0 or value > 3600:
            raise ConfigError("error.invalid_value", field="minimum_skip_length")
        raw["minimum_skip_length"] = int(value) if value.is_integer() else value

    if "join_name" in body:
        name = str(body["join_name"]).strip()[:64]
        raw["join_name"] = name or "iSponsorBlockTV"

    if "apikey" in body:
        # None = keep, "" = delete, string = replace
        if body["apikey"] is not None:
            key = str(body["apikey"]).strip()
            if len(key) > 200 or any(ch.isspace() for ch in key):
                raise ConfigError("error.invalid_value", field="apikey")
            raw["apikey"] = key

    if "channel_whitelist" in body:
        wl = body["channel_whitelist"]
        if not isinstance(wl, list):
            raise ConfigError("error.invalid_value", field="channel_whitelist")
        if len(wl) > 1000:
            raise ConfigError("error.invalid_value", field="channel_whitelist")
        clean, seen = [], set()
        for c in wl:
            cid = str(c.get("id", "")).strip() if isinstance(c, dict) else ""
            if not CHANNEL_ID_RE.match(cid):
                raise ConfigError("error.invalid_value", field="channel_whitelist")
            if cid in seen:
                continue
            seen.add(cid)
            clean.append({"id": cid, "name": str(c.get("name", "")).strip()[:100]})
        raw["channel_whitelist"] = clean

    raw["devices"] = cfg["devices"]  # drop template placeholders
    return raw


# --------------------------------------------------------------------------------------
# Process supervisor
# --------------------------------------------------------------------------------------

class Supervisor:
    """Starts, stops and restarts the iSponsorBlockTV process and keeps its log."""

    def __init__(self):
        self.proc = None
        self.logs = collections.deque(maxlen=LOG_LINES)
        self.seq = 0
        self.started_at = None
        self.last_exit = None
        self.wanted = True  # user wants it running
        self.state = "stopped"
        self.problems = []
        self._lock = asyncio.Lock()
        self._watch_task = None
        self._backoff = 5

    def add_log(self, line, source="app"):
        self.seq += 1
        self.logs.append({"n": self.seq, "t": time.time(), "s": source, "m": line.rstrip()})

    async def start(self):
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self):
        if self.proc and self.proc.returncode is None:
            return
        try:
            cfg = effective_config(read_config())
        except ConfigError:
            self.problems = ["problem.config_unreadable"]
            self.state = "not_configured"
            return
        self.problems = config_problems(cfg)
        if self.problems:
            self.state = "not_configured"
            self.add_log("Not starting iSponsorBlockTV: " + ", ".join(self.problems), "webui")
            return
        cmd = ISBTV_CMD + ISBTV_EXTRA_ARGS
        self.add_log("Starting iSponsorBlockTV", "webui")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=APP_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as e:
            self.proc = None
            self.state = "error"
            self.add_log(f"Could not start iSponsorBlockTV: {e}", "webui")
            return
        self.started_at = time.time()
        self.state = "running"
        self._watch_task = asyncio.create_task(self._watch(self.proc))

    async def _watch(self, proc):
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace")
            # Strip terminal escape sequences produced by rich
            self.add_log(_strip_ansi(text))
            print(text, end="", flush=True)  # keep "docker logs" useful
        code = await proc.wait()
        if proc is not self.proc:
            return
        ran_for = time.time() - (self.started_at or time.time())
        if ran_for > 120:
            self._backoff = 5  # it ran fine for a while: start over with a short delay
        self.last_exit = {"code": code, "t": time.time()}
        self.add_log(f"iSponsorBlockTV exited with code {code}", "webui")
        self.proc = None
        self.started_at = None
        if self.wanted:
            # Unexpected exit: restart with growing delay
            self.state = "restarting"
            delay = self._backoff
            self._backoff = min(self._backoff * 2, 300)
            await asyncio.sleep(delay)
            if self.wanted and self.proc is None:
                await self.start()
        else:
            self.state = "stopped"

    async def stop(self, wanted=False):
        async with self._lock:
            self.wanted = wanted
            proc = self.proc
            if not proc or proc.returncode is not None:
                self.state = "stopped" if not wanted else self.state
                return
            self.add_log("Stopping iSponsorBlockTV", "webui")
            self.proc = None
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            self.started_at = None
            self.state = "stopped"

    async def restart(self):
        await self.stop(wanted=True)
        self._backoff = 5
        self.wanted = True
        await self.start()

    def status(self):
        running = self.proc is not None and self.proc.returncode is None
        return {
            "state": "running" if running else self.state,
            "pid": self.proc.pid if running else None,
            "uptime": (time.time() - self.started_at) if running and self.started_at else None,
            "last_exit": self.last_exit,
            "problems": self.problems,
            "wanted": self.wanted,
        }


def _strip_ansi(text):
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            i += 1
            if i < len(text) and text[i] == "[":
                i += 1
                while i < len(text) and not ("@" <= text[i] <= "~"):
                    i += 1
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------------------
# Authentication (optional, enabled with WEBUI_PASSWORD)
# --------------------------------------------------------------------------------------

def _server_secret():
    try:
        with open(SECRET_FILE, "rb") as f:
            secret = f.read()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    secret = secrets.token_bytes(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _open_private(SECRET_FILE, 0o600) as f:
            f.write(secret)
    except OSError:
        pass
    return secret


SECRET = None
COOKIE = "isbtv_webui"


def _sign(payload: bytes) -> str:
    mac = hmac.new(SECRET + PASSWORD.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + mac).decode()


REVOKED = {}  # session id -> expiry, filled on logout
LOGIN_LOCK = None  # serialises login attempts (brute-force protection)


def make_token():
    expires = int(time.time() + SESSION_HOURS * 3600)
    return _sign(f"{expires}:{secrets.token_hex(8)}".encode())


def _token_payload(token):
    """Return (expires, session_id) for a valid token, otherwise None."""
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        payload, mac = raw.rsplit(b".", 1)
        expected = hmac.new(SECRET + PASSWORD.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None
        expires, sid = payload.decode().split(":", 1)
        expires = int(expires)
        if expires <= time.time() or sid in REVOKED:
            return None
        return expires, sid
    except Exception:  # noqa: BLE001
        return None


def check_token(token):
    return _token_payload(token) is not None


def revoke_token(token):
    found = _token_payload(token)
    now = time.time()
    for sid in [s for s, exp in REVOKED.items() if exp <= now]:
        del REVOKED[sid]
    if found:
        REVOKED[found[1]] = found[0]


PUBLIC_PATHS = {"/api/health", "/api/meta", "/api/login", "/api/session"}


@web.middleware
async def security_middleware(request, handler):
    path = request.path
    if path.startswith("/api/"):
        # Simple CSRF protection: state-changing requests must come from our own JS
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "webui":
            return json_error(403, "error.forbidden")
        if PASSWORD and path not in PUBLIC_PATHS and not check_token(request.cookies.get(COOKIE, "")):
            return json_error(401, "error.unauthorized")
    response = await handler(request)
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'",
    )
    return response


# --------------------------------------------------------------------------------------
# HTTP handlers
# --------------------------------------------------------------------------------------

def json_error(status, key, **params):
    return web.json_response({"error": key, "params": params}, status=status)


async def body_json(request):
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise ConfigError("error.invalid_request")
    if not isinstance(data, dict):
        raise ConfigError("error.invalid_request")
    return data


def handles_config_errors(func):
    async def wrapper(request):
        try:
            return await func(request)
        except ConfigError as e:
            return json_error(400, e.key, **e.params)

    return wrapper


def exclusive(func):
    """Serialise all requests that read-modify-write config.json or control the process."""
    async def wrapper(request):
        async with request.app["lock"]:
            return await func(request)

    return wrapper


async def h_health(_request):
    return web.json_response({"ok": True})


async def h_meta(_request):
    return web.json_response({
        "version": VERSION,
        "categories": CATEGORIES,
        "auth": bool(PASSWORD),
    })


async def h_session(request):
    ok = not PASSWORD or check_token(request.cookies.get(COOKIE, ""))
    return web.json_response({"authenticated": ok, "auth": bool(PASSWORD)})


async def h_login(request):
    if not PASSWORD:
        return web.json_response({"authenticated": True})
    try:
        data = await body_json(request)
    except ConfigError as e:
        return json_error(400, e.key)
    candidate = hashlib.sha256(str(data.get("password", "")).encode()).digest()
    async with LOGIN_LOCK:  # one attempt at a time, failed attempts are delayed
        if not hmac.compare_digest(candidate, hashlib.sha256(PASSWORD.encode()).digest()):
            await asyncio.sleep(1.5)
            log.warning("Failed login from %s", request.remote)
            return json_error(401, "error.wrong_password")
    resp = web.json_response({"authenticated": True})
    resp.set_cookie(
        COOKIE, make_token(), max_age=SESSION_HOURS * 3600, httponly=True, samesite="Strict",
        secure=request.secure or request.headers.get("X-Forwarded-Proto") == "https",
    )
    return resp


async def h_logout(request):
    revoke_token(request.cookies.get(COOKIE, ""))
    resp = web.json_response({"authenticated": False})
    resp.del_cookie(COOKIE)
    return resp


async def h_status(request):
    return web.json_response(request.app["sup"].status())


async def h_logs(request):
    sup = request.app["sup"]
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    lines = [l for l in sup.logs if l["n"] > since]
    return web.json_response({"lines": lines, "last": sup.seq})


@handles_config_errors
async def h_get_config(_request):
    return web.json_response(public_config(effective_config(read_config())))


@handles_config_errors
@exclusive
async def h_put_config(request):
    body = await body_json(request)
    raw = read_config()
    raw = apply_settings(raw, body)
    cfg = effective_config(raw)
    if cfg["channel_whitelist"] and not cfg.get("apikey"):
        raise ConfigError("problem.whitelist_needs_apikey")
    write_config(raw)
    await request.app["sup"].restart()
    return web.json_response(public_config(effective_config(raw)))


def _upstream_api_helper(cfg, session):
    """Create iSponsorBlockTV's own ApiHelper so pairing works exactly like the CLI."""
    from iSponsorBlockTV import api_helpers  # type: ignore

    ns = SimpleNamespace(
        apikey=cfg.get("apikey", ""),
        skip_categories=cfg.get("skip_categories", ["sponsor"]),
        channel_whitelist=cfg.get("channel_whitelist", []),
        skip_count_tracking=cfg.get("skip_count_tracking", True),
        devices=cfg.get("devices", []),
        minimum_skip_length=cfg.get("minimum_skip_length", 1),
        sponsorblock_api_url=cfg.get("sponsorblock_api_url", "https://sponsor.ajay.app/api/"),
    )
    return api_helpers.ApiHelper(ns, session)


@handles_config_errors
@exclusive
async def h_pair(request):
    body = await body_json(request)
    code = "".join(ch for ch in str(body.get("code", "")) if ch.isdigit())
    if len(code) < 8:
        raise ConfigError("error.invalid_code")
    raw = read_config()
    cfg = effective_config(raw)
    try:
        async with ClientSession(trust_env=cfg.get("use_proxy", False)) as session:
            helper = _upstream_api_helper(cfg, session)
            paired = await helper.pair_with_code(code)
    except ImportError:
        return json_error(500, "error.upstream_missing")
    except Exception as e:  # noqa: BLE001
        log.warning("Pairing failed: %s", e)
        return json_error(502, "error.pairing_failed")
    if not paired:
        return json_error(400, "error.pairing_failed")
    name = str(body.get("name") or paired.get("name") or "YouTube TV").strip()[:64]
    devices = [d for d in cfg["devices"] if d["screen_id"] != paired["screen_id"]]
    devices.append({"screen_id": paired["screen_id"], "name": name, "offset": 0})
    raw["devices"] = devices
    write_config(raw)
    request.app["sup"].add_log(f"Paired device: {name}", "webui")
    await request.app["sup"].restart()
    return web.json_response(public_config(effective_config(raw)))


@handles_config_errors
@exclusive
async def h_update_device(request):
    screen_id = request.match_info["screen_id"]
    body = await body_json(request)
    raw = read_config()
    cfg = effective_config(raw)
    found = False
    for d in cfg["devices"]:
        if d["screen_id"] == screen_id:
            found = True
            if "name" in body:
                d["name"] = str(body["name"]).strip()[:64] or "YouTube TV"
            if "offset" in body:
                try:
                    offset = int(float(body["offset"]))
                except (TypeError, ValueError):
                    raise ConfigError("error.invalid_value", field="offset")
                if abs(offset) > 60000:
                    raise ConfigError("error.invalid_value", field="offset")
                d["offset"] = offset
    if not found:
        return json_error(404, "error.device_not_found")
    raw["devices"] = cfg["devices"]
    write_config(raw)
    await request.app["sup"].restart()
    return web.json_response(public_config(effective_config(raw)))


@handles_config_errors
@exclusive
async def h_delete_device(request):
    screen_id = request.match_info["screen_id"]
    raw = read_config()
    cfg = effective_config(raw)
    devices = [d for d in cfg["devices"] if d["screen_id"] != screen_id]
    if len(devices) == len(cfg["devices"]):
        return json_error(404, "error.device_not_found")
    raw["devices"] = devices
    write_config(raw)
    if devices:
        await request.app["sup"].restart()
    else:
        await request.app["sup"].stop(wanted=True)
        await request.app["sup"].start()  # sets state "not_configured"
    return web.json_response(public_config(effective_config(raw)))


@handles_config_errors
async def h_search_channels(request):
    query = request.query.get("q", "").strip()[:100]
    if not query:
        return web.json_response({"channels": []})
    cfg = effective_config(read_config())
    if not cfg.get("apikey"):
        raise ConfigError("problem.whitelist_needs_apikey")
    try:
        async with ClientSession(trust_env=cfg.get("use_proxy", False)) as session:
            helper = _upstream_api_helper(cfg, session)
            found = await helper.search_channels(query)
    except ImportError:
        return json_error(500, "error.upstream_missing")
    except Exception as e:  # noqa: BLE001
        log.warning("Channel search failed: %s", e)
        return json_error(502, "error.search_failed")
    return web.json_response({
        "channels": [{"id": c[0], "name": c[1], "subscribers": str(c[2])} for c in found]
    })


@exclusive
async def h_restart(request):
    await request.app["sup"].restart()
    return web.json_response(request.app["sup"].status())


@exclusive
async def h_stop(request):
    await request.app["sup"].stop(wanted=False)
    return web.json_response(request.app["sup"].status())


@exclusive
async def h_start(request):
    sup = request.app["sup"]
    sup.wanted = True
    sup._backoff = 5
    await sup.start()
    return web.json_response(sup.status())


async def h_index(_request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


# --------------------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------------------

async def on_startup(app):
    await app["sup"].start()


async def on_cleanup(app):
    await app["sup"].stop(wanted=False)


def build_app():
    global SECRET, LOGIN_LOCK
    SECRET = _server_secret()
    LOGIN_LOCK = asyncio.Lock()
    app = web.Application(middlewares=[security_middleware], client_max_size=64 * 1024)
    app["sup"] = Supervisor()
    app["lock"] = asyncio.Lock()
    app.router.add_get("/", h_index)
    app.router.add_get("/api/health", h_health)
    app.router.add_get("/api/meta", h_meta)
    app.router.add_get("/api/session", h_session)
    app.router.add_post("/api/login", h_login)
    app.router.add_post("/api/logout", h_logout)
    app.router.add_get("/api/status", h_status)
    app.router.add_get("/api/logs", h_logs)
    app.router.add_get("/api/config", h_get_config)
    app.router.add_put("/api/config", h_put_config)
    app.router.add_post("/api/devices/pair", h_pair)
    app.router.add_patch("/api/devices/{screen_id}", h_update_device)
    app.router.add_delete("/api/devices/{screen_id}", h_delete_device)
    app.router.add_get("/api/channels/search", h_search_channels)
    app.router.add_post("/api/process/restart", h_restart)
    app.router.add_post("/api/process/stop", h_stop)
    app.router.add_post("/api/process/start", h_start)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s webui %(levelname)s: %(message)s")
    if not PASSWORD:
        log.warning("WEBUI_PASSWORD is not set - the web interface has no login. "
                    "Only expose it behind a protected reverse proxy.")
    log.info("iSponsorBlockTV WebUI %s on http://%s:%s (data: %s)", VERSION, HOST, PORT, DATA_DIR)
    web.run_app(build_app(), host=HOST, port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
