"""
MediaReducer web server (Flask). Serves the dashboard and config UI, launches
engine.py as a subprocess for every run, drives an APScheduler tick that
performs the automatic daily cleanup, gates deletion on plan-currency (a
completed Simulate under the current config), and builds a sanitized debug
report. Run state (_run_active/_run_process) is in-memory and resets on restart.

READ THIS FIRST: RUN_MODE names do not match the GUI labels
----------------------------------------------------------
Scheduler Mode has three settings. The stored values predate the labels, and
one of them is actively misleading — RUN_MODE "paused" is NOT the GUI's
"Paused":

    RUN_MODE      GUI label           What the scheduler does
    ──────────    ─────────────────   ───────────────────────────────────────
    "off"         Paused              Nothing but a quiet storage refresh.
                                      No scans, no deletions, no alerts.
    "paused"      Monitor Only        Full cadence — 15-minute upkeep, daily
                                      Simulate, every alert — but never
                                      deletes on its own.
    "headroom"    Automatic Cleanup   The above, plus it deletes at the daily
                                      run and on a Redline breach.

So `RUN_MODE == "paused"` means the scheduler is FULLY ACTIVE and merely
non-deleting. Read modes through the helpers rather than comparing strings:
_is_cleanup_mode() for "may this delete", _ui_run_mode() for "which of the
three is this". Comments below say Automatic Cleanup / Monitor Only / Paused —
the GUI's words — so they can be matched against what a user sees.

A saved Automatic Cleanup is forced back to Monitor Only on every startup;
a Paused scheduler stays paused.
"""

import concurrent.futures
import configparser
import contextlib
import copy
import gzip
import ipaddress
import hashlib
import io
import json
import math
import os
import sys
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone as _utc_tz
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from apscheduler.schedulers.background import BackgroundScheduler

from scoring_constants import SCORING, FINGERPRINT as SCORING_FINGERPRINT, season_retention_score
from flask import (Flask, g, has_request_context, jsonify, render_template, request,
                   send_from_directory)

import db  # shared SQLite persistence (metadata cache, library snapshot, queue, meta)
import notify  # best-effort outbound alerting (apprise wrapper); never raises
import shared  # pass mechanics both executors must agree on (deficit, delay clock, log line, rungs)

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR          = Path(__file__).parent


def _load_dotenv() -> None:
    """For bare-metal runs, load a .env file (next to app.py, or MEDIAREDUCER_ENV_FILE)
    so MEDIAREDUCER_* vars live in one place. A real env var always wins, so Docker is
    unaffected. Must run before any root/config constant is read; the engine subprocess
    inherits the result via os.environ.copy()."""
    env_path = Path(os.environ.get("MEDIAREDUCER_ENV_FILE") or (APP_DIR / ".env"))
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


_load_dotenv()

SCRIPT_PATH      = APP_DIR / "engine.py"
DEFAULT_CFG_PATH = APP_DIR / "default_config.json"
CONFIG_PATH      = Path(os.environ.get("MEDIAREDUCER_CONFIG", "/config/config.json"))


# ── Appearance: per browser, not per install ─────────────────────────────────
# The one class of setting that says nothing about the library — only how THIS
# browser should draw the page. Two people on one install can disagree about
# them without either being wrong, and a phone and a desktop usually do, so
# they live in cookies rather than config.json. Nothing here reaches the
# engine and no save is involved. Reset MediaReducer expires them, which is the
# one server-side reach into them and only for the browser that pressed it.
APPEARANCE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _is_firefox_mobile(ua: str) -> bool:
    """Gecko on a phone or tablet.

    It gets a different default because backdrop-filter costs it far more than
    it costs Blink: a page carrying dozens of blurred surfaces scrolls visibly
    badly there. Firefox for iOS is deliberately NOT matched — Apple requires
    WebKit under it, so "FxiOS" is Firefox in name only and does not share the
    cost. Matching on the marketing name would turn the blur off for a browser
    that renders it fine."""
    ua = ua or ""
    return "Firefox/" in ua and ("Android" in ua or "Mobile;" in ua)


def _appearance_flags(req) -> dict:
    """How to stamp <html> for this request's browser.

    `reduce_effects` and `glass_pref_off` are what the two Advanced toggles say
    — a cookie each, absent until someone touches them. `no_glass` is what the
    stylesheet actually reads: Reduce visual effects turns the blur off along
    with everything else, so the OR happens here rather than in every rule.

    With no cookie set, the glass defaults OFF on Firefox mobile. That is the
    one place the default is worth changing rather than leaving someone to find
    the setting after deciding the app is slow."""
    def _flag(name):
        v = (req.cookies.get(name) or "").strip().lower()
        return True if v == "off" else False if v == "on" else None

    reduce_effects = _flag("mr_effects") or False
    glass_pref_off = _flag("mr_glass")
    if glass_pref_off is None:
        glass_pref_off = _is_firefox_mobile(req.headers.get("User-Agent", ""))
    return {"reduce_effects": bool(reduce_effects),
            "glass_pref_off": bool(glass_pref_off),
            "no_glass": bool(glass_pref_off or reduce_effects)}


def _clear_appearance_cookies(resp, status: int | None = None):
    """Expire the appearance cookies on a Reset MediaReducer response.

    Reset means back to a first-time install, and someone who set the page to
    plain and then reset it does not expect it to still be plain. These live in
    the browser rather than config.json, so the only way to clear one is to
    tell the browser to drop it — which reaches THIS browser, the one that
    pressed the button, and no other. A phone that set its own keeps them until
    it resets too; there is nowhere server-side to reach it from, and that is
    the trade that made them per-browser in the first place.

    Same path the setter used, or the browser treats it as a different cookie
    and leaves the original in place."""
    if status is not None:
        resp.status_code = status
    for name in ("mr_effects", "mr_glass"):
        resp.delete_cookie(name, path="/", samesite="Lax")
    return resp


def _root_from_env(var: str, default: str) -> str:
    """Deployment root, relocatable for bare-metal installs. The engine inherits
    this process's environment and reads the SAME vars, so the two always agree.
    Deploy-time infrastructure — never a UI or config setting."""
    return os.environ.get(var, default).rstrip("/") or default


# The library mount (deletion boundary) and the three appdata mounts default to
# their Docker paths; set the env vars to run outside Docker.
FILESYSTEM_CHECK_PATH = _root_from_env("MEDIAREDUCER_LIBRARY", "/library")
TAUTULLI_APPDATA_DIR  = _root_from_env("MEDIAREDUCER_TAUTULLI_APPDATA", "/tautulli")
RADARR_APPDATA_DIR    = _root_from_env("MEDIAREDUCER_RADARR_APPDATA", "/radarr")
SONARR_APPDATA_DIR    = _root_from_env("MEDIAREDUCER_SONARR_APPDATA", "/sonarr")

# Single background clock. Each tick runs an automatic cleanup when Automatic Cleanup is
# enabled, else a quiet Summary/debug_info refresh so dashboard disk/library
# numbers never go stale. Paused while any run/Summary is in flight and restarted
# from zero when it finishes. Coarse because a cleanup tick can trigger a full
# deletion pass.
SCHEDULE_INTERVAL_MINUTES = 15
CONNECTION_CONFIG_FIELDS = (
    "TAUTULLI_URL", "TAUTULLI_API_KEY", "PLEX_URL", "PLEX_TOKEN",
    "RADARR_URL", "RADARR_API_KEY", "SONARR_URL", "SONARR_API_KEY",
    "JELLYFIN_URL", "JELLYFIN_API_KEY",
)
CONNECTION_ONBOARDING_SEEN_KEY = "_CONNECTIONS_ONBOARDING_SEEN"
CONNECTION_EVER_CONFIGURED_KEY = "_CONNECTIONS_EVER_CONFIGURED"
WELCOME_GUIDE_SEEN_KEY = "_WELCOME_GUIDE_SEEN"
# Records that the user DELIBERATELY chose Paused, so a later completed
# Simulate leaves them there. Deliberately stored in the positive: absent
# means "the scheduler is only resting", which is the right reading for a
# fresh install, a config reset, and a hand-edited file alike. Storing the
# resting state instead made every config that lacked the flag unwakeable —
# a reset writes the shipped defaults, so setup + Simulate never left Paused.
RUN_MODE_USER_PAUSED_KEY = "_RUN_MODE_USER_PAUSED"
RADARR_SECTION_CACHE_KEYS = (
    "_RADARR_DETECTED_SECTION_ID",
    "_RADARR_DETECTED_SECTION_NAME",
    "_RADARR_DETECTED_SECTION_METHOD",
    "_RADARR_DETECTED_SECTION_METHOD_LABEL",
)
RADARR_SECTION_METHOD_LABELS = {
    "path-prefix": "path prefix",
    "root-prefix": "root folder path",
    "folder-name": "library folder name",
}

app = Flask(__name__)


@app.template_filter("commafy")
def _tpl_commafy(value, digits=1):
    """Thousands-separated number, rounded to `digits` decimals with trailing zeros
    dropped — mirrors JS toLocaleString(maximumFractionDigits: digits)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    s = f"{num:,.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


@app.template_filter("measured_gb")
def _tpl_measured_gb(value):
    """A MEASURED size in GB, always to one decimal.

    The app draws one line between numbers it measured — free space, disk
    totals, library and movie sizes — and numbers the user typed, which are
    whole GB. Measured figures keep their decimal even when it is .0, so the
    same reading never appears as "400" in one place and "400.0" in another;
    commafy is the wrong tool for them because it drops the trailing zero.
    Settings use `|commafy(0)`.

    A missing reading is not zero: anything that is not a real number becomes
    the em dash the cards already use for an unknown value, rather than being
    passed through to render as "None" or fabricated into "0.0".
    """
    if value is None or value == "":
        return "—"
    try:
        gb = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if gb != gb else f"{gb:,.1f}"      # NaN never equals itself


@app.template_filter("group_int")
def _tpl_group_int(value):
    """Thousands-separate the integer part of an already-formatted numeric
    string, keeping its fractional digits exactly ('3.20' -> '3.20',
    '1234.5' -> '1,234.5'). Non-numeric input is returned unchanged."""
    s = str(value).strip()
    neg = s.startswith("-")
    int_part, dot, frac = (s[1:] if neg else s).partition(".")
    if not int_part.isdigit():
        return value
    return ("-" if neg else "") + f"{int(int_part):,}" + (("." + frac) if dot else "")

# ── Drive-by request protection ──────────────────────────────────────────────
# No login (LAN tool), so two browser attack paths stay open: cross-origin POSTs
# from any site the user visits (a simple request needs no preflight, and /api/run
# with an empty body starts a cleanup), and DNS rebinding (an attacker domain
# resolving to this LAN IP, making its page same-origin). Two cheap checks close both:
#   1. Every mutating request must carry the X-MediaReducer header. base.html's
#      fetch wrapper adds it; a cross-origin page cannot without a CORS preflight
#      this server never approves.
#   2. The Host header must look local: IP literal, localhost, a dot-less LAN
#      hostname, or *.local (mDNS). Reverse-proxy names go in
#      MEDIAREDUCER_TRUSTED_HOSTS (comma-separated).
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_header_allowed(host_header: str | None) -> bool:
    raw = str(host_header or "").strip().lower().rstrip(".")
    if not raw:
        return False
    try:
        ipaddress.ip_address(raw)
        return True  # bare IP literal, including unbracketed IPv6 like ::1
    except ValueError:
        pass
    try:
        host = (urlparse("//" + raw).hostname or "").rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    if "." not in host and ":" not in host:
        return True  # single-label LAN hostname (e.g. "tower")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    trusted = os.environ.get("MEDIAREDUCER_TRUSTED_HOSTS", "")
    return host in {h.strip().lower().rstrip(".") for h in trusted.split(",") if h.strip()}


@app.before_request
def _reject_cross_origin_requests():
    if not _host_header_allowed(request.host):
        return jsonify({
            "ok": False,
            "error": "Rejected: unrecognized Host header. Access MediaReducer by IP or local "
                     "hostname, or add this name to MEDIAREDUCER_TRUSTED_HOSTS.",
        }), 403
    if request.method in _MUTATING_METHODS and request.headers.get("X-MediaReducer") != "1":
        return jsonify({
            "ok": False,
            "error": "Rejected: missing X-MediaReducer header. If you are scripting against "
                     "the API, send \"X-MediaReducer: 1\" with every write request.",
        }), 403
    # Remember this request's LAN host so background/startup probes (no request
    # context) resolve service URL defaults to the SAME address the Config page
    # shows, instead of an appdata-detected host that can wrongly report down.
    _request_lan_host()
    return None


# Text this app serves compresses ~4x (the Config page: 396 KB → 90 KB), and it
# is served by Flask directly; there is no proxy in the container to do it.
# Small bodies are left alone: below ~1 KB the header overhead eats the saving.
_COMPRESSIBLE_TYPES = ("text/", "application/json", "application/javascript",
                       "image/svg+xml")
_COMPRESS_MIN_BYTES = 1024


@app.after_request
def _security_headers(response):
    """The three headers any app should send, whether or not it faces the net.

    X-Frame-Options is the one that earns its place here. The mutating-request
    header (see above) stops another origin SCRIPTING a write, but it does
    nothing about framing: a page that embeds MediaReducer invisibly and steers
    a real click is clicking as you, and the header this app requires is set by
    its own JavaScript, so it rides along. With one-click Cleanup and no login,
    refusing to be framed is the cheap half of that problem solved.

    nosniff stops a browser second-guessing a declared content type — the
    archived-log route hands back files whose names came from disk. no-referrer
    keeps a LAN hostname out of the Referer header on any outbound link."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.after_request
def _cache_vendor_assets(response):
    """Let the browser keep the vendored Bootstrap/Inter files forever.

    Everything under static/vendor is third-party and version-pinned in its
    filename, so the bytes behind a URL never change — which is exactly what
    "immutable" promises. Without it Flask revalidates each one on every
    navigation, turning a cached asset back into a round trip."""
    try:
        if request.path.startswith("/static/vendor/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    except Exception:
        pass
    return response


# Vendored CSS/JS compressed once and kept, keyed by file identity. Flask serves
# static files in passthrough mode, which the hook below would otherwise skip —
# leaving Bootstrap's 233 KB uncompressed on the one load that matters most.
# Only the few text files land here; woff2 is already compressed and its media
# type isn't in the list above, so it never reaches this.
_static_gzip_cache: dict = {}
_static_gzip_lock = threading.Lock()


def _gzipped_static(url_path: str):
    """The gzipped bytes of a vendored static file, or None if it can't be read.
    Read from disk rather than off the response, which is a passthrough wrapper."""
    try:
        fp = Path(app.static_folder) / url_path[len("/static/"):]
        st = fp.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except (OSError, TypeError):
        return None
    with _static_gzip_lock:
        hit = _static_gzip_cache.get(url_path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        blob = gzip.compress(fp.read_bytes(), 6)
    except OSError:
        return None
    with _static_gzip_lock:
        _static_gzip_cache[url_path] = (stamp, blob)
    return blob


@app.after_request
def _compress_response(response):
    """gzip text responses the client said it can decode.

    Level 6 rather than 9: on the Config page it is ~3 ms instead of ~40 ms for
    2 KB more, and the page is served on every navigation. Skipped for
    already-encoded responses, and for streamed ones — a direct_passthrough body
    (send_from_directory for an archived log) must reach the client untouched.
    The vendored static files are the exception: they are passthrough too, but
    they are few, never change, and are compressed once into the memo above."""
    try:
        if (response.status_code < 200 or response.status_code >= 300
                or "Content-Encoding" in response.headers):
            return response
        ctype = (response.headers.get("Content-Type") or "").lower()
        if not ctype.startswith(_COMPRESSIBLE_TYPES):
            return response
        if response.direct_passthrough:
            if not request.path.startswith("/static/vendor/"):
                return response
            response.headers.add("Vary", "Accept-Encoding")
            if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
                return response
            blob = _gzipped_static(request.path)
            if blob is None:
                return response
            response.direct_passthrough = False
            response.set_data(blob)
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(blob))
            return response
        # Vary regardless of whether THIS response was compressed: a shared cache
        # must not hand a gzipped body to a client that didn't ask for one.
        response.headers.add("Vary", "Accept-Encoding")
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return response
        body = response.get_data()
        if len(body) < _COMPRESS_MIN_BYTES:
            return response
        response.set_data(gzip.compress(body, 6))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(response.get_data()))
    except Exception:
        pass   # compression is an optimization; never fail a response over it
    return response


# Serializes config.json read-modify-write cycles and the atomic save. RLock so
# helpers can hold it across load_config() + save_config() without deadlocking on
# save_config's own acquisition.
_config_io_lock = threading.RLock()

# Connection health is probed once on startup so the Config page can show
# connection problems without waiting for a Check for Errors click. Keyed by the
# connection-relevant config values so edited/saved settings don't reuse stale results.
_connection_health_cache_lock = threading.Lock()
_connection_health_cache: dict = {"signature": None, "health": None, "checked_at": None}

# Common time zones shown first in the config dropdown; the full IANA list is
# appended after these.
_COMMON_TIME_ZONES = [
    "UTC",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Australia/Sydney",
]

def _time_zone_options() -> list[str]:
    try:
        all_zones = sorted(available_timezones())
    except Exception:
        all_zones = []
    seen = set()
    options = []
    for zone in _COMMON_TIME_ZONES + all_zones:
        if zone and zone not in seen:
            seen.add(zone)
            options.append(zone)
    return options

# Application version; shown in the welcome guide and the debug report so bug
# reports name the build. Bump on release. SemVer pre-release: the number is the
# release being worked TOWARD, not one that shipped, and alpha < beta < rc < the
# plain version when anything sorts them.
APP_VERSION = "1.0.0-alpha.16"

# Episodes above which a "season" is really a whole show filed under one
# number. Named here because two places need the same fallback: the settings
# resolver, when a config predates the key, and the page that renders the
# field. Set well clear of a real season — the longest ordinary ones run to
# about 26 — so raising it is a choice, never a rescue from false positives.
TV_MAX_SEASON_EPISODES_DEFAULT = 50

# Host clock, captured before any TIME_ZONE override is applied so switching the
# setting back to auto can restore it.
_HOST_TZ = os.environ.get("TZ")
_HOST_TZ_NAME = (_HOST_TZ
                 or getattr(datetime.now().astimezone().tzinfo, "key", None)
                 or datetime.now().astimezone().tzname() or "UTC")

def _server_time_zone_name() -> str:
    """Best-effort IANA-ish name for the zone the process clock follows —
    the configured TIME_ZONE once applied, else the container clock."""
    env_tz = str(os.environ.get("TZ") or "").strip()
    if env_tz:
        return env_tz
    local = datetime.now().astimezone()
    return getattr(local.tzinfo, "key", None) or local.tzname() or "UTC"

def _host_time_zone_name() -> str:
    """Zone the container clock follows when TIME_ZONE is auto."""
    return _HOST_TZ_NAME


# ── Config helpers ────────────────────────────────────────────────────────────

def _coerce_bool(value, *, default: bool = False) -> bool:
    """`default` is what a MISSING value means. Almost every flag here is off
    when absent, but one defaults on — a config with no PAUSE_CLEANUP_ON_STARTUP
    must not read as someone having turned the startup safety demote off."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _normalize_library_path(value: str | None) -> str | None:
    """Return a normalized path under the library root."""
    root = FILESYSTEM_CHECK_PATH
    root_name = root.rsplit("/", 1)[-1] or "library"
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw in ("/", root, root_name):
        return root
    if raw.startswith(root + "/"):
        suffix = raw[len(root) + 1:]
    elif raw.startswith(root_name + "/"):
        suffix = raw[len(root_name) + 1:]
    elif raw.startswith("/"):
        # Unknown absolute path outside the library root: its root can't be
        # inferred, so keep only the library folder name.
        suffix = raw.rstrip("/").split("/")[-1]
    else:
        suffix = raw.lstrip("/")
    suffix = suffix.strip("/")
    return f"{root}/{suffix}" if suffix else None


def _normalize_library_paths(cfg: dict) -> dict:
    """Reduce MONITOR_DIRS to a clean, deduplicated list of library subfolders."""
    monitor_dirs = []
    for raw in cfg.get("MONITOR_DIRS", []) or []:
        normalized = _normalize_library_path(str(raw).strip())
        if normalized and normalized not in monitor_dirs:
            monitor_dirs.append(normalized)
    cfg["MONITOR_DIRS"] = monitor_dirs
    return cfg


def _monitoring_summary_signature(cfg: dict | None) -> dict:
    """Values that change the dashboard library-size scan."""
    normalized = dict(cfg or {})
    _normalize_library_paths(normalized)
    monitor_dirs = [
        str(path).strip().replace("\\", "/").rstrip("/")
        for path in (normalized.get("MONITOR_DIRS") or [])
        if str(path).strip()
    ]
    extensions = [
        str(ext).strip().lower()
        for ext in (normalized.get("MOVIE_EXTENSIONS") or [])
        if str(ext).strip()
    ]
    return {
        "monitor_dirs": monitor_dirs,
        "movie_extensions": sorted(set(extensions)),
    }


def _should_refresh_summary_after_config_save(saved_cfg: dict, new_cfg: dict) -> bool:
    """Queue storage stats only when saved config changes what gets measured."""
    if _monitoring_summary_signature(saved_cfg) != _monitoring_summary_signature(new_cfg):
        return True
    stats = library_stats()
    has_monitor_dirs = bool(_monitoring_summary_signature(new_cfg)["monitor_dirs"])
    return has_monitor_dirs and stats.get("library_gb") is None


def _normalize_retention_scoring(cfg: dict) -> None:
    """Clamp the retention-score fields to their valid ranges."""
    try:
        cfg["GRACE_PERIOD_DAYS"] = max(0, int(float(cfg.get("GRACE_PERIOD_DAYS", 30))))
    except (TypeError, ValueError):
        cfg["GRACE_PERIOD_DAYS"] = 30
    try:
        cfg["SCORE_BALANCE"] = max(0, min(100, round(float(cfg.get("SCORE_BALANCE", 50)))))
    except (TypeError, ValueError):
        cfg["SCORE_BALANCE"] = 50
    cfg["MAX_IMDB_RATING"] = _clamp_max_imdb_rating(cfg.get("MAX_IMDB_RATING"))
    cfg["NEAR_TIE_PTS"] = _clamp_near_tie_pts(cfg.get("NEAR_TIE_PTS", 2))
    cfg["MAX_STALENESS_MONTHS"] = _clamp_staleness_months(cfg.get("MAX_STALENESS_MONTHS", 36))


def _is_blank(value) -> bool:
    """None or a whitespace-only string — the GUI's 'disabled' spelling."""
    return value is None or (isinstance(value, str) and not value.strip())


def _clamp_max_imdb_rating(value):
    """Nullable rating cutoff: None/blank = disabled, else clamp to 0.1–10.
    A value of 0 or below also reads as disabled — a cutoff of 0 matches
    nothing, so it means the same as "no cutoff" rather than being an error."""
    if _is_blank(value):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return round(min(10.0, f), 1)


def _clamp_near_tie_pts(value):
    """Nullable near-tie window in retention-score points: None/blank =
    file size optimization off, else clamp to 0.5–25."""
    if _is_blank(value):
        return None
    try:
        return round(max(0.5, min(25.0, float(value))), 1)
    except (TypeError, ValueError):
        return None


def _clamp_staleness_months(value):
    """Max staleness window in months (the recency curve fades to 0 over it):
    a whole number clamped to 1–120, default 36."""
    try:
        return int(max(1, min(120, round(float(value)))))
    except (TypeError, ValueError):
        return 36


# Hand-edit guard: config.json is only ever written by the GUI (which validates
# everything), so a value that breaks a GUI rule means someone edited the file by
# hand. load_config() refreshes this list on every read, while it is non-empty the
# app locks out runs and every config-mutating endpoint until the invalid values
# are reset (/api/config/reset-invalid) or MediaReducer is reset.
_CONFIG_FILE_ISSUES: list = []

# Reset target for invalid keys, and the effective value of keys missing from the
# file (the cross-field check compares what a run would actually use).
with open(DEFAULT_CFG_PATH) as _f:
    _CONFIG_DEFAULTS: dict = json.load(_f)


def _config_num(value):
    """Finite float for validation, else None. Bools are not numbers here, and JSON
    can smuggle in inf/nan (1e999) which no GUI rule accepts."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# Numeric GUI rules: key, skip-mode for the disabled spelling (None = value
# required when the key is present, "null" = literal null allowed, "blank" =
# null or blank string allowed), acceptance test, message.
_CONFIG_NUM_RULES = (
    ("HEADROOM_GB", None, lambda n: n >= 0, "must be a number of GB, zero or greater"),
    ("REDLINE_GB", "null", lambda n: n > 0, "must be a number of GB above zero, or null"),
    ("MAX_HEADROOM_PCT", None, lambda n: 0 < n <= 100, "must be a percentage above 0 and at most 100"),
    ("MAX_LIBRARY_GB", "null", lambda n: n > 0, "must be a number of GB above zero, or null"),
    ("GRACE_PERIOD_DAYS", None, lambda n: n >= 0 and float(n).is_integer(), "must be a whole number of days, zero or greater"),
    # Floor of 1 (a marked movie is never deleted the same day), so 0 has no valid
    # meaning — the GUI never writes it. A hand-edited 0 locks out like any other
    # out-of-range edit rather than being silently reinterpreted.
    ("DELETE_DELAY_DAYS", None, lambda n: 1 <= n <= 365 and float(n).is_integer(), "must be a whole number of days from 1 to 365"),
    ("LOG_RETENTION_DAYS", None, lambda n: n >= 0, "must be a number of days, zero or greater"),
    ("IMDB_RATINGS_MAX_AGE_DAYS", None, lambda n: n >= 1, "must be a number of days, one or greater"),
    ("SCORE_BALANCE", None, lambda n: 0 <= n <= 100, "must be a number from 0 to 100"),
    # 0 is accepted as a disabled spelling (a cutoff of 0 matches nothing, so the
    # clamp reads it as None); the GUI writes null, not 0.
    ("MAX_IMDB_RATING", "blank", lambda n: 0 <= n <= 10, "must be a number from 0 to 10, or null"),
    ("NEAR_TIE_PTS", "blank", lambda n: 0.5 <= n <= 25, "must be a number from 0.5 to 25 points, or null"),
    ("TV_SERIES_WATCH_BUMP", None, lambda n: 0 <= n <= 25, "must be a number of points from 0 to 25"),
    ("TV_WATCH_WEIGHT", None, lambda n: 100 <= n <= 200, "must be a percentage from 100 to 200"),
    # 0 is the off switch: no season is ever too big. The GUI writes it, so
    # unlike DELETE_DELAY_DAYS's 0 this is a real value, not a lockout.
    ("TV_MAX_SEASON_EPISODES", None, lambda n: 0 <= n <= 999 and float(n).is_integer(),
     "must be a whole number of episodes from 0 to 999 (0 turns it off)"),
    ("MAX_STALENESS_MONTHS", None, lambda n: 1 <= n <= 120, "must be a number of months from 1 to 120"),
)


def _config_file_issues(saved: dict) -> list[dict]:
    """Validate the raw config.json content against the same rules the GUI
    enforces on save. Only keys present in the file are checked — missing keys
    fall back to defaults. Returns [{key, message}, ...]."""
    issues: list[dict] = []

    def bad(key, message):
        issues.append({"key": key, "message": message})

    for key, skip, ok, message in _CONFIG_NUM_RULES:
        if key not in saved:
            continue
        v = saved[key]
        if (skip == "null" and v is None) or (skip == "blank" and _is_blank(v)):
            continue
        n = _config_num(v)
        if n is None or not ok(n):
            bad(key, message)
    # Cross-field check on the EFFECTIVE values (defaults fill missing keys, so
    # deleting HEADROOM_GB can't smuggle in an oversized redline). Skipped while
    # either field is itself flagged: its range message already covers it, and
    # comparing garbage would double-flag.
    flagged = {i["key"] for i in issues}
    if not ({"REDLINE_GB", "HEADROOM_GB", "MAX_LIBRARY_GB"} & flagged):
        redline = _config_num(saved.get("REDLINE_GB", _CONFIG_DEFAULTS.get("REDLINE_GB")))
        headroom = _config_num(saved.get("HEADROOM_GB", _CONFIG_DEFAULTS.get("HEADROOM_GB")))
        # The one cross-field rule: under an ARMED headroom (>= 1) the Redline
        # floor sits strictly below the target it backs up. With headroom off
        # there is no target, so Redline is free, and EVERY threshold may be
        # off at once (the app keeps Monitor Only and Automatic Cleanup
        # unavailable until one is armed, so nothing here refuses the state).
        # Keyed to the headroom VALUE, never the REDLINE_ONLY_MODE flag: the
        # flag is derived from the value at load, so a hand-edited
        # contradiction resolves instead of erroring.
        if (redline is not None and headroom is not None and headroom > 0
                and redline >= headroom):
            bad("REDLINE_GB", "must be lower than HEADROOM_GB (or untick Headroom)")
    for key in ("SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES", "USE_PLEX",
                "USE_JELLYFIN", "KEEP_INTERRUPTED_LOGS", "DEBUG_MODE",
                "REDLINE_ONLY_MODE", "PAUSE_CLEANUP_ON_STARTUP",
                "MOVIE_CLEANUP_ENABLED", "TV_CLEANUP_ENABLED",
                "SONARR_CLEANUP_ENABLED"):
        if key in saved and not isinstance(saved[key], bool):
            bad(key, "must be true or false")
    # "off" = the scheduler is fully paused (no ticks do anything); "paused" =
    # Monitor Only (refreshes and the daily Simulate, never deletes);
    # "headroom" = Automatic Cleanup.
    if "RUN_MODE" in saved and saved["RUN_MODE"] not in ("off", "paused", "headroom"):
        bad("RUN_MODE", 'must be "off", "paused" or "headroom"')
    # Debug mode and Automatic Cleanup are mutually exclusive: Debug mode is a no-delete
    # diagnostic state, so it can only be turned on while Scheduler Mode is
    # Paused or Monitor Only, and it can't be on while the mode is Automatic Cleanup.
    if saved.get("DEBUG_MODE") and saved.get("RUN_MODE", "paused") == "headroom":
        bad("DEBUG_MODE", "can only be on while Scheduler Mode is Paused or Monitor Only — "
                          "Debug mode does not run cleanup deletions")
    if "IMDB_RATINGS_URL" in saved:
        try:
            _validate_imdb_url(saved["IMDB_RATINGS_URL"])
        except ValueError:
            bad("IMDB_RATINGS_URL", "must be an http(s) URL")
    if "MONITOR_DIRS" in saved:
        v = saved["MONITOR_DIRS"]
        if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v):
            bad("MONITOR_DIRS", "must be a list of path strings")
    if "MOVIE_EXTENSIONS" in saved:
        v = saved["MOVIE_EXTENSIONS"]
        if not isinstance(v, list) or not v or any(
                not isinstance(x, str) or not x.strip().startswith(".") for x in v):
            bad("MOVIE_EXTENSIONS", 'must be a list of extensions like ".mkv"')
    for key in ("TAUTULLI_URL", "PLEX_URL", "RADARR_URL", "SONARR_URL", "JELLYFIN_URL"):
        if key in saved:
            v = saved[key]
            if not isinstance(v, str):
                bad(key, "must be a URL string (may be blank)")
            elif v.strip() and not urlparse(_normalize_service_url(v)).netloc:
                bad(key, "must be a valid URL, or blank")
    for key in ("TAUTULLI_API_KEY", "PLEX_TOKEN", "RADARR_API_KEY", "SONARR_API_KEY", "JELLYFIN_API_KEY"):
        if key in saved and not isinstance(saved[key], str):
            bad(key, "must be a string (may be blank)")
    for key in ("PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS"):
        if key in saved:
            v = saved[key]
            if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                bad(key, "must be a list of collection names")
    if "RADARR_OVERSEERR_SECTION_ID" in saved:
        v = saved["RADARR_OVERSEERR_SECTION_ID"]
        if v is not None and not isinstance(v, (str, int)):
            bad("RADARR_OVERSEERR_SECTION_ID", 'must be a section ID, "auto", or null')
    if "OUTPUT_DIR" in saved and (not isinstance(saved["OUTPUT_DIR"], str) or not saved["OUTPUT_DIR"].strip()):
        bad("OUTPUT_DIR", "must be a directory path")
    if "TIME_ZONE" in saved:
        try:
            _validate_time_zone(saved["TIME_ZONE"])
        except ValueError:
            bad("TIME_ZONE", 'must be an IANA time zone or "auto"')
    if "DISPLAY_TIME_FORMAT" in saved:
        try:
            _validate_display_time_format(saved["DISPLAY_TIME_FORMAT"])
        except ValueError:
            bad("DISPLAY_TIME_FORMAT", 'must be "12h" or "24h"')
    if "DAILY_RUN_TIME" in saved:
        try:
            _validate_daily_run_time(saved["DAILY_RUN_TIME"])
        except ValueError:
            bad("DAILY_RUN_TIME", "must be a 24-hour HH:MM time")
    return issues



def _any_space_trigger_armed(cfg: dict) -> bool:
    """Is ANY deletion trigger armed — a Headroom target (>= 1 GB), a Redline
    floor, or a Library Size Cap? With none of them, there is nothing to
    monitor and nothing a cleanup could enforce, so Monitor Only and Automatic
    Cleanup are both unavailable and the scheduler rests at Paused."""
    if (_config_num(cfg.get("HEADROOM_GB")) or 0) > 0:
        return True
    return cfg.get("REDLINE_GB") is not None or cfg.get("MAX_LIBRARY_GB") is not None


def _redline_only_mode_cfg(cfg: dict | None = None) -> bool:
    """True when Redline is the ONLY deletion trigger: the Headroom checkbox is
    unticked (headroom value 0), a Redline floor is set, AND no Library Size Cap is
    armed. Simulate maintains a standing preview of the deletion order and is always
    required before Automatic Cleanup; the deletion delay does not apply. Unticking Headroom with a
    Library Size Cap is a DIFFERENT state — the cap runs on the daily schedule with the
    delay, so it is not redline-only (this returns False). Derived from the VALUES,
    like the REDLINE_ONLY_MODE flag itself, so a raw dict gets the same answer a
    loaded config would."""
    c = cfg if cfg is not None else load_config()
    return ((_config_num(c.get("HEADROOM_GB")) or 0) <= 0
            and c.get("REDLINE_GB") is not None
            and c.get("MAX_LIBRARY_GB") is None)


def _read_saved_config_file() -> dict | None:
    """Raw config.json content: {} when the file is missing (fresh install),
    None when it is unparseable or not a JSON object (corrupt → lockout)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError(f"config.json holds {type(saved).__name__}, not an object")
        return saved
    except Exception as e:
        print(f"WARNING: could not read {CONFIG_PATH} ({e}) — using defaults until it is fixed or re-saved.",
              file=sys.stderr)
        return None


_config_memo_lock = threading.Lock()
_config_memo = {"key": None, "cfg": None, "issues": []}


# Keys the app used to derive and write into config.json, pruned on load so an
# existing file heals itself. /jellyfin was dropped once ports stopped being read
# from appdata: it could only ever have supplied Jellyfin's HTTP port, and
# Jellyfin issues API keys from its own dashboard, storing none on disk.
_RETIRED_CONFIG_KEYS = ("JELLYFIN_APPDATA",)


def _build_config() -> tuple[dict, list]:
    """Parse + validate + normalize config.json into the runtime cfg. Returns
    (cfg, issues); the caller memoizes and sets the _CONFIG_FILE_ISSUES global."""
    with open(DEFAULT_CFG_PATH) as f:
        cfg = json.load(f)
    # A corrupt/non-object config.json must not take every route down (including
    # the Config page needed to fix it): fall back to defaults, but record the
    # problem so runs and config edits lock until it's reset or fixed. save_config
    # is atomic, so this is only reachable via hand edits or disk trouble. Values
    # that break a GUI validation rule lock the same way.
    saved = _read_saved_config_file()
    if saved is None:
        issues = [{"key": "config.json", "message": "is not valid JSON"}]
    else:
        issues = _config_file_issues(saved)
        cfg.update(saved)
    cfg["SKIP_UNPLAYED_MOVIES"] = _coerce_bool(cfg.get("SKIP_UNPLAYED_MOVIES"))
    cfg["PROTECT_JELLYFIN_FAVORITES"] = _coerce_bool(cfg.get("PROTECT_JELLYFIN_FAVORITES"))
    cfg["USE_PLEX"] = _coerce_bool(cfg.get("USE_PLEX"))
    cfg["USE_JELLYFIN"] = _coerce_bool(cfg.get("USE_JELLYFIN"))
    # Absent means OFF for both per-type switches. Deleting files is opt-in:
    # a fresh install scans, scores and shows you everything, and deletes
    # nothing until you say which types it may touch. Absent reading as ON
    # would mean a config that never named a type still deleted one.
    cfg["MOVIE_CLEANUP_ENABLED"] = _coerce_bool(cfg.get("MOVIE_CLEANUP_ENABLED", False), default=False)
    cfg["TV_CLEANUP_ENABLED"] = _coerce_bool(cfg.get("TV_CLEANUP_ENABLED", False), default=False)
    # Absent means OFF, matching Optional Radarr cleanup: reaching into
    # another app's state is opt-in, never something a fresh install does on
    # its own. Off, a deleted season's files still go; Sonarr re-downloads it
    # if it stays monitored, which is the user's call to make.
    cfg["SONARR_CLEANUP_ENABLED"] = _coerce_bool(cfg.get("SONARR_CLEANUP_ENABLED", False), default=False)
    # Absent means ON: the safety demote is the shipped behaviour, and a config
    # missing the key must not read as someone having turned it off.
    cfg["PAUSE_CLEANUP_ON_STARTUP"] = _coerce_bool(
        cfg.get("PAUSE_CLEANUP_ON_STARTUP", True), default=True)
    _normalize_retention_scoring(cfg)
    _normalize_library_paths(cfg)
    # REDLINE_ONLY_MODE is DERIVED: the headroom trigger is armed iff
    # HEADROOM_GB >= 1, and the flag just mirrors "the Headroom checkbox is
    # unticked". Deriving on every load means a hand-edited contradiction
    # (armed value + flag set, or 0 + flag clear) resolves to what the value
    # says, and the GUI, validators and engine can never disagree about it.
    cfg["REDLINE_ONLY_MODE"] = (_config_num(cfg.get("HEADROOM_GB")) or 0) <= 0
    cfg["CHECK_PATH"] = FILESYSTEM_CHECK_PATH
    cfg["TAUTULLI_APPDATA"] = TAUTULLI_APPDATA_DIR
    cfg["RADARR_APPDATA"] = RADARR_APPDATA_DIR
    cfg["SONARR_APPDATA"] = SONARR_APPDATA_DIR
    # Derived keys that no longer exist. They were written into config.json by
    # every save while they DID exist, so dropping them here is what stops a
    # fossil showing up in `mr config show` as though it were a real setting.
    for gone in _RETIRED_CONFIG_KEYS:
        cfg.pop(gone, None)
    return cfg, issues


def _copy_config(cfg: dict) -> dict:
    """A private copy of a config dict, for load_config's per-call handout.

    The config is plain JSON data — scalars plus a few short lists and dicts —
    so copy.deepcopy's generic machinery (memo table, reductors, recursion
    bookkeeping) dominates the cost, and load_config sits under db_path(), i.e.
    under every store read. This copies the same depth for that shape at a
    fraction of the cost; anything unexpected still falls back to deepcopy so
    callers can never mutate the memo."""
    out = {}
    for k, v in cfg.items():
        t = type(v)
        if t is list:
            out[k] = [i if type(i) in (str, int, float, bool, type(None))
                      else _copy_config(i) if type(i) is dict else copy.deepcopy(i)
                      for i in v]
        elif t is dict:
            out[k] = _copy_config(v)
        elif t in (str, int, float, bool, type(None)):
            out[k] = v
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config() -> dict:
    """The runtime config, memoized by config.json's (mtime, size): a single
    /api/status poll asks for it hundreds of times (every timestamp render calls
    it for the display format), and the file only changes on save. Each call
    returns a fresh copy so callers can mutate it freely; the parse + GUI-rule
    validation runs once per file version, not once per consult."""
    global _CONFIG_FILE_ISSUES
    try:
        st = CONFIG_PATH.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None   # missing file (fresh install); a stable, memoizable state
    with _config_memo_lock:
        if _config_memo["key"] == key and _config_memo["cfg"] is not None:
            _CONFIG_FILE_ISSUES = _config_memo["issues"]
            return _copy_config(_config_memo["cfg"])
    cfg, issues = _build_config()
    with _config_memo_lock:
        _config_memo.update({"key": key, "cfg": cfg, "issues": issues})
    _CONFIG_FILE_ISSUES = issues
    return _copy_config(cfg)


def _invalid_config_response():
    """409 for config-mutating endpoints while config.json holds invalid hand edits:
    nothing may change (including API credentials) until the values are reset or
    MediaReducer is reset."""
    load_config()  # refresh _CONFIG_FILE_ISSUES from disk
    if _CONFIG_FILE_ISSUES:
        return jsonify({
            "ok": False,
            "invalid_config": _CONFIG_FILE_ISSUES,
            "error": "config.json contains invalid values — reset them on the "
                     "Configuration page (or reset MediaReducer) first.",
        }), 409
    return None


def save_config(cfg: dict, *, overwrite_invalid: bool = False) -> bool:
    """Atomically persist config.json (write tmp, rename over the target). Returns
    True when written.

    os.replace() is atomic on the same filesystem, so a container killed mid-write
    can't leave a half-written file that breaks every page load — readers always see
    the old or the new complete file. Writers are serialized by _config_io_lock and
    each uses a unique tmp name: concurrent saves do happen (e.g. the welcome popup's
    mark-seen POST racing the Config page's onboarding flag), and a shared tmp name
    would let one writer rename the file out from under another.

    Refused while the on-disk file holds invalid hand edits (saving would clear the
    lockout and silently replace the user's values with coerced ones); only the
    reset-invalid endpoint passes overwrite_invalid=True, and the full reset deletes
    the file instead.
    """
    with _config_io_lock:
        if not overwrite_invalid:
            saved = _read_saved_config_file()
            if saved is None or _config_file_issues(saved):
                print("WARNING: config.json holds invalid hand-edited values — save skipped "
                      "until they are reset.", file=sys.stderr)
                return False
        _atomic_write_json(CONFIG_PATH, cfg, indent=2)
    return True


def _atomic_write_json(path: Path, data, *, indent: int | None = None) -> None:
    """The one JSON write pattern for shared state files: a unique tmp then
    os.replace, so readers never see a torn file. The tmp name carries pid AND
    thread id — Flask threads and the engine subprocess can race on the same
    target, and two writers sharing a tmp path would interleave into garbage.
    Serialization (locks) is the caller's job; this only makes each write whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        # fsync before the rename, so the write is atomic against POWER LOSS
        # and not just process death: without it the rename can be persisted
        # while the data blocks are not, and an unclean host shutdown leaves a
        # zero-length config.json — the whole configuration (tokens,
        # thresholds, monitor dirs) gone behind an invalid-JSON lockout.
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=indent))
            f.flush()
            os.fsync(f.fileno())
        # Owner-only, before the rename rather than after: config.json holds a
        # Plex token, three API keys and any notification webhook URLs in clear
        # text, and the default umask leaves it world-readable — which on a NAS
        # means every other container sharing appdata can read them. The mode
        # rides the tmp file, so os.replace carries it over and an existing
        # install is tightened by its next save. chmod can fail on a filesystem
        # with no POSIX modes (a Windows bind mount); the write still stands.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _radarr_section_method_label(method: str | None) -> str:
    method = str(method or "").strip()
    return RADARR_SECTION_METHOD_LABELS.get(method, method.replace("-", " ") if method else "")


def _clear_radarr_section_detection_cache(cfg: dict) -> None:
    for key in RADARR_SECTION_CACHE_KEYS:
        cfg.pop(key, None)


def _store_radarr_section_detection_cache(cfg: dict, detection: dict) -> bool:
    section_id = str(detection.get("section_id") or "").strip()
    if not detection.get("ok") or not section_id:
        return False

    section_name = str(detection.get("section_name") or "").strip()
    method = str(detection.get("method") or "").strip()
    method_label = str(detection.get("method_label") or _radarr_section_method_label(method)).strip()

    cfg["_RADARR_DETECTED_SECTION_ID"] = section_id
    if section_name:
        cfg["_RADARR_DETECTED_SECTION_NAME"] = section_name
    else:
        cfg.pop("_RADARR_DETECTED_SECTION_NAME", None)
    if method:
        cfg["_RADARR_DETECTED_SECTION_METHOD"] = method
    else:
        cfg.pop("_RADARR_DETECTED_SECTION_METHOD", None)
    if method_label:
        cfg["_RADARR_DETECTED_SECTION_METHOD_LABEL"] = method_label
    else:
        cfg.pop("_RADARR_DETECTED_SECTION_METHOD_LABEL", None)
    return True


def _radarr_section_detection_cache_incomplete(cfg: dict) -> bool:
    if not str(cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip():
        return False
    return not (
        str(cfg.get("_RADARR_DETECTED_SECTION_NAME") or "").strip()
        and str(cfg.get("_RADARR_DETECTED_SECTION_METHOD") or "").strip()
    )


def _has_saved_connection_credentials(cfg: dict | None = None) -> bool:
    """Return True once any saved Connection URL/API field has a value."""
    cfg = cfg or load_config()
    return any(str(cfg.get(name) or "").strip() for name in CONNECTION_CONFIG_FIELDS)


def _connection_onboarding_needed(cfg: dict | None = None) -> bool:
    """Show the first-run Config cue only for a fresh, never-configured instance."""
    cfg = cfg or load_config()
    if cfg.get(CONNECTION_ONBOARDING_SEEN_KEY) or cfg.get(CONNECTION_EVER_CONFIGURED_KEY):
        return False
    if _has_saved_connection_credentials(cfg):
        return False
    # config.json exists from first boot, so the persisted marker is the reliable
    # signal. Once Config has been opened or any connection value ever saved, this
    # stays False even if those values are later deleted.
    return True


def _mark_connection_onboarding_seen(cfg: dict | None = None) -> dict:
    """Persist that the user has visited Config so the first-run cue stops."""
    with _config_io_lock:
        cfg = dict(cfg or load_config())
        changed = False
        if not cfg.get(CONNECTION_ONBOARDING_SEEN_KEY):
            cfg[CONNECTION_ONBOARDING_SEEN_KEY] = True
            changed = True
        if _has_saved_connection_credentials(cfg) and not cfg.get(CONNECTION_EVER_CONFIGURED_KEY):
            cfg[CONNECTION_EVER_CONFIGURED_KEY] = True
            changed = True
        if changed:
            save_config(cfg)
        return cfg


def _welcome_guide_needed(cfg: dict | None = None) -> bool:
    """Show the first-run welcome/quick-start popup until dismissed once. The flag
    persists in config.json (surviving rebuilds) but returns after a full reset,
    which deletes config.json to restore the first-time state."""
    cfg = cfg or load_config()
    return not bool(cfg.get(WELCOME_GUIDE_SEEN_KEY))


def _mark_welcome_guide_seen() -> bool:
    with _config_io_lock:
        cfg = load_config()
        if not cfg.get(WELCOME_GUIDE_SEEN_KEY):
            return save_config(cfg | {WELCOME_GUIDE_SEEN_KEY: True})
    return True


def _preserve_connection_onboarding_flags(new_cfg: dict, old_cfg: dict | None = None) -> dict:
    """Keep first-run onboarding markers across normal Config saves."""
    old_cfg = old_cfg or load_config()
    if old_cfg.get(WELCOME_GUIDE_SEEN_KEY):
        new_cfg[WELCOME_GUIDE_SEEN_KEY] = True
    if old_cfg.get(CONNECTION_ONBOARDING_SEEN_KEY):
        new_cfg[CONNECTION_ONBOARDING_SEEN_KEY] = True
    if (
        old_cfg.get(CONNECTION_EVER_CONFIGURED_KEY)
        or _has_saved_connection_credentials(old_cfg)
        or _has_saved_connection_credentials(new_cfg)
    ):
        new_cfg[CONNECTION_EVER_CONFIGURED_KEY] = True
    return new_cfg


def force_paused_run_mode_on_startup():
    """Safety reset: a restart never resumes a saved Automatic Cleanup — it is
    demoted to Monitor Only, while Paused and Monitor Only survive as saved.
    An incomplete setup (missing a monitored path, a selected media server, or
    an up-to-date library database) rests at fully-Paused with the auto-wake
    marker, so the other modes stay ghosted until a completed Simulate
    re-enables Monitor Only. Server SELECTION rather than live connectivity is
    the test here: startup can't probe yet, and a transient API outage must not
    demote a configured Monitor Only. Manual Dashboard runs are still allowed
    once checks pass. Connection URLs/keys are never auto-filled on startup —
    Auto Detect on the Config page is the only appdata-to-field fill."""
    try:
        cfg = load_config()
        if not (_has_monitored_dirs(cfg)
                and (cfg.get("USE_PLEX") or cfg.get("USE_JELLYFIN"))):
            # Nothing is set up: rest at Paused. A deliberate pause recorded
            # earlier is left alone; the user can still have meant it.
            if cfg.get("RUN_MODE") == "off":
                return
            cfg["RUN_MODE"] = "off"
            # No autopause reason: this is the onboarding rest state, not a
            # flip of something the user armed.
            if save_config(cfg):
                print("Startup: nothing is set up yet — Scheduler Mode rests at Paused.",
                      flush=True)
            return
        # A fully-paused scheduler ("off") is already the safest state; a
        # restart keeps it off rather than promoting it to Monitor Only.
        if cfg.get("RUN_MODE") not in ("off", "paused"):
            # The demote is the default and the safe answer, because a restart
            # is usually an upgrade or a crash and the plan waiting on disk may
            # no longer describe the library. Turning it off says "resume where
            # I left off" — but only when there is nothing to be wrong about:
            # a snapshot built for THESE monitored paths, inside the full-scan
            # window. A stale one still demotes, whatever the setting says,
            # since resuming deletions against a plan nothing has re-checked is
            # the thing the demote exists to prevent.
            if not cfg.get("PAUSE_CLEANUP_ON_STARTUP", True) and _library_db_fresh(cfg):
                print("Startup: Automatic Cleanup resumed — 'Set to Monitor Only at "
                      "startup' is off and the library database is current.", flush=True)
            else:
                cfg["RUN_MODE"] = "paused"
                # Recorded so the dashboard/config can EXPLAIN the flip; a silent reset
                # read as "my Automatic Cleanup setting didn't stick". Cleared by the next config save
                # (the form never posts internal underscore keys).
                cfg["_RUN_MODE_AUTOPAUSE_REASON"] = (
                    "Automatic Cleanup is paused automatically after every restart."
                    if cfg.get("PAUSE_CLEANUP_ON_STARTUP", True) else
                    "Automatic Cleanup did not resume after the restart: the library database is "
                    "out of date. Run Simulate, then turn it back on.")
                if save_config(cfg):
                    print("Startup safety: RUN_MODE reset to paused.", flush=True)
        # Monitor Only also needs an up-to-date library database. A missing or
        # stale snapshot (never scanned, wiped by a code/schema-change store
        # rebuild, or a container stopped past the full-scan window) rests the
        # scheduler at fully-Paused: the next completed Simulate re-enables
        # Monitor Only on its own. The system is doing this, not the user, so
        # any recorded deliberate pause is cleared; this state must wake.
        cfg = load_config()
        if cfg.get("RUN_MODE") == "paused" and not _library_db_fresh(cfg):
            cfg["RUN_MODE"] = "off"
            cfg.pop(RUN_MODE_USER_PAUSED_KEY, None)
            if save_config(cfg):
                print("Startup: the library database is missing or out of date — "
                      "Scheduler Mode rests at Paused; run Simulate and Monitor "
                      "Only re-enables automatically.", flush=True)
    except Exception as e:
        print(f"WARNING: could not reset RUN_MODE to paused on startup: {e}", flush=True)


# NOTE: a Library Size Cap stays ARMED across restarts. It only prunes during an
# automatic cleanup, and startup forces RUN_MODE to paused
# (force_paused_run_mode_on_startup), so nothing deletes until the user
# deliberately re-enables Automatic Cleanup, and the engine's 85% safety floor
# still blocks any extreme prune.


def _apply_setup_mode_lifecycle(cfg: dict, saved_cfg: dict, save_health: dict,
                                *, forced_pause: bool = False) -> str:
    """Onboarding lifecycle for Scheduler Mode, applied on every config save
    (mutates cfg in place). Monitor Only requires the full working set: a
    connected media server, a monitored path, and an up-to-date library
    database.
      • Any piece missing demotes Monitor Only to fully-Paused. Only "paused"
        is demoted: an explicit Automatic Cleanup request must reach the
        arming gates (simulate-required, thresholds, connection force-pauses)
        first, which is why this runs after that chain, and a save where a
        forced pause just fired keeps its reason visible instead.
      • Everything ready while the scheduler is RESTING at Paused: wake into
        Monitor Only. The usual wake path is _sync_scheduler_mode_with_db (a
        Simulate finishes outside any save); this covers the last piece
        arriving via a config change.
    Returns "forced_off", "auto_enabled", or ""."""
    connected = bool((save_health or {}).get("critical_ok"))
    # The CONFIGURED working set: the pieces only a deliberate save can remove
    # — a selected server, a monitored path, an armed space trigger. A save
    # that drops one of these rests the scheduler at Paused even when a forced
    # pause fired in the same save: the forced-pause skip below exists for
    # probe blips (staying at Monitor Only keeps the reason visible without
    # flapping), but a structural removal is a choice, and Paused is the
    # honest landing. Database freshness is deliberately NOT in this set; a
    # stale scan is drift, not a decision, and the tick's own demotion covers
    # it.
    structural_cfg_ok = (bool(cfg.get("USE_PLEX") or cfg.get("USE_JELLYFIN"))
                         and _has_monitored_dirs(cfg)
                         and _any_space_trigger_armed(cfg))
    ready = (connected and structural_cfg_ok and _library_db_fresh(cfg))
    # A save is the ONLY way a user selects Paused, so it is the only place the
    # deliberate flag is set: moving off Monitor Only/Automatic Cleanup means
    # they chose it. Selecting either of those clears it again. Anything else
    # keeps whatever was recorded; the form never posts underscore keys, so a
    # flag that isn't carried forward here is lost.
    if cfg.get("RUN_MODE") == "off":
        if saved_cfg.get("RUN_MODE") in ("paused", "headroom") and not forced_pause:
            cfg[RUN_MODE_USER_PAUSED_KEY] = True
        elif saved_cfg.get(RUN_MODE_USER_PAUSED_KEY):
            cfg[RUN_MODE_USER_PAUSED_KEY] = True
    else:
        cfg.pop(RUN_MODE_USER_PAUSED_KEY, None)

    if not ready:
        if cfg.get("RUN_MODE") == "paused" and (not forced_pause
                                                or not structural_cfg_ok):
            # The system is parking it, not the user; this must stay wakeable.
            cfg["RUN_MODE"] = "off"
            cfg.pop(RUN_MODE_USER_PAUSED_KEY, None)
            return "forced_off"
        return ""
    if cfg.get("RUN_MODE") == "off" and not cfg.get(RUN_MODE_USER_PAUSED_KEY):
        cfg["RUN_MODE"] = "paused"
        return "auto_enabled"
    return ""


def _sync_scheduler_mode_with_db() -> None:
    """Keep Scheduler Mode honest about the library database, in both
    directions. Called whenever the store may have changed underneath the mode:
    after a run or Summary finishes, and at the top of each tick.

      • Wake: a RESTING fully-Paused scheduler returns to Monitor Only once a
        server is selected, paths exist, and the snapshot is fresh. Server
        SELECTION rather than a live probe — the scan that just wrote the
        snapshot proves the connection worked seconds ago. Only a pause the
        user actually chose (RUN_MODE_USER_PAUSED_KEY) blocks this.
      • Demote: Monitor Only or Automatic Cleanup drops to the resting
        fully-Paused state when the snapshot is missing or stale. The engine
        wipes every code-derived section (snapshot and queue included) the
        first time it runs after its own code changes — which happens in the
        startup Summary, AFTER force_paused_run_mode_on_startup has already
        judged the mode. Without this, an updated container came up in Monitor
        Only with an empty queue and no plan behind it.

    Best-effort, and never while a run or Summary is mid-write."""
    try:
        if _run_active or _summary_active:
            return
        cfg = load_config()
        mode = cfg.get("RUN_MODE")
        db_fresh = _library_db_fresh(cfg)
        if mode == "off":
            # Waking needs the whole working set, not just the database; a
            # server may have been deselected, or the last space trigger
            # disarmed, while the scheduler rested.
            if (cfg.get(RUN_MODE_USER_PAUSED_KEY)
                    or not (db_fresh and _has_monitored_dirs(cfg)
                            and _any_space_trigger_armed(cfg)
                            and (cfg.get("USE_PLEX") or cfg.get("USE_JELLYFIN")))):
                return
            cfg["RUN_MODE"] = "paused"
            if save_config(cfg):
                _restart_schedule_clock()
                print("Library scan complete — Scheduler Mode set to Monitor Only.", flush=True)
            return
        # Demoting keys on the DATABASE alone: setup completeness (servers,
        # paths) only changes through a config save, which applies the full
        # lifecycle itself.
        if db_fresh or mode not in ("paused", "headroom"):
            return
        cfg["RUN_MODE"] = "off"
        cfg.pop(RUN_MODE_USER_PAUSED_KEY, None)   # the system parked it, so it must wake
        if save_config(cfg):
            _restart_schedule_clock()
            print("The library database is missing or out of date — Scheduler Mode "
                  "rests at Paused; run Simulate and Monitor Only re-enables "
                  "automatically.", flush=True)
    except Exception:
        pass


def output_dir() -> Path:
    return Path(load_config().get("OUTPUT_DIR", "/config"))


def db_path() -> Path:
    """The SQLite store shared with the engine (metadata cache, library
    snapshot, marked/eligible queue, kv meta)."""
    return output_dir() / "mediareducer.db"


def burn_daily_window_on_startup(reason: str = "startup") -> None:
    """Safety reset: stamp today as the last daily-cleanup date.

    The once-per-day window lives in the store, so a restart or cache wipe loses it,
    and a lost window would hand the scheduler a free IMMEDIATE daily run when Automatic Cleanup is
    re-armed — that catch-up run is the hazard. A run time still AHEAD of the clock
    today is not a catch-up: it is the normally scheduled slot, so it stays armed
    (mirroring the maintenance-scan latch). Redline emergencies ignore the window
    and still fire either way."""
    try:
        run_epoch = _todays_daily_run_epoch(load_config())
        if run_epoch is not None and time.time() < run_epoch:
            return   # today's slot is still ahead; a scheduled run, not a catch-up
    except Exception:
        pass   # can't judge the slot → burn (fail safe)
    today = time.strftime("%Y-%m-%d")
    try:
        with _store_write() as conn:
            if db.get_meta(conn, "last_cleanup_date") == today:
                return
            db.set_meta(conn, "last_cleanup_date", today)
        print(f"Startup safety: daily-run window marked used for today ({reason}).", flush=True)
    except Exception as e:
        print(f"WARNING: could not stamp the daily-run window ({e})", flush=True)


def reopen_daily_window(reason: str = "daily run time moved later") -> None:
    """Undo today's daily-window burn so a run time moved to a slot still ahead
    of the clock can fire again today. No-op unless the window is currently burned
    for today. Safe with the deletion delay: mark ages are calendar-day granular
    (see engine _mark_age_days), so a second run on the same day only re-marks
    candidates — no mark ages into eligibility between two runs on one day, so
    nothing extra is deleted."""
    today = time.strftime("%Y-%m-%d")
    try:
        with _store_write() as conn:
            if db.get_meta(conn, "last_cleanup_date") != today:
                return
            db.del_meta(conn, "last_cleanup_date")
        print(f"Daily-run window reopened for today ({reason}).", flush=True)
    except Exception as e:
        print(f"WARNING: could not reopen the daily-run window ({e})", flush=True)

def log_path()     -> Path: return output_dir() / "lastrun.log"
def deleted_path() -> Path: return output_dir() / "deleted.log"
def progress_path()-> Path: return output_dir() / "progress.json"

# The engine died without writing a terminal state — killed, OOM, the container
# stopped. The dashboard and the failure alert both fall back to this, so one
# event is not described two ways.
RUN_ENDED_UNEXPECTEDLY = ("The run ended unexpectedly and did not report why. "
                          "Check the detailed log.")
def run_report_path() -> Path: return output_dir() / "last_run_report.json"
def logs_dir()     -> Path: return output_dir() / "logs"


def _clear_run_report(out_dir: Path) -> None:
    """Remove any stale engine run-report so a fresh one means "this run made it".
    The engine writes last_run_report.json at the end of a real cleanup; clearing
    it at launch stops a previous run's report from being re-notified. Best-effort."""
    try:
        (out_dir / "last_run_report.json").unlink(missing_ok=True)
        for p in out_dir.glob("last_run_report.json.*.tmp"):
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _read_run_report():
    """Read the engine's last_run_report.json, or None if absent/unreadable."""
    try:
        with open(run_report_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _archive_lastrun_log(out_dir: Path) -> None:
    """Move out_dir/lastrun.log into out_dir/logs/ under a timestamped name (the same
    naming the engine uses for archived cleanup runs), so the Dashboard run panel starts
    empty while the run's log is preserved — never deleted. A zero-byte/empty log is just
    removed. Best-effort: a failure here must never fail the caller. out_dir is passed in
    (not re-resolved via log_path()) so a caller mid-reset — with config.json already gone
    — still targets the pre-reset OUTPUT_DIR, and covers a logs/ folder on another mount."""
    last_log = out_dir / "lastrun.log"
    try:
        if last_log.exists() and last_log.stat().st_size > 0:
            archive_dir = out_dir / "logs"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            target = archive_dir / f"{stamp}.log"
            n = 1
            while target.exists():
                target = archive_dir / f"{stamp}_{n}.log"
                n += 1
            shutil.move(str(last_log), str(target))
        else:
            last_log.unlink(missing_ok=True)
    except OSError as e:
        print(f"WARNING: could not archive lastrun.log: {e}", flush=True)


_cache_file_memo_lock = threading.Lock()
_cache_file_memo = {"key": None, "data": {}}
# Held while COMPOSING the store, so a version change can't send every waiting
# request off to build the same dict at once (see _cache_file_data).
_cache_compose_lock = threading.Lock()


def _invalidate_request_store_cache() -> None:
    """Drop this request's store snapshot after an app-side write, so a handler
    that writes and then reads sees its own change (see _cache_file_data)."""
    try:
        if has_request_context():
            g.pop("_pr_store_data", None)
    except Exception:
        pass


def _drop_store_caches() -> None:
    """Forget everything read from the store — for the wipe paths, which replace
    the file rather than write through it. The cross-request memo is keyed on a
    fingerprint that includes the file's identity, so it would notice on its
    own; clearing it explicitly keeps a deleted store's contents from being
    handed to the very next reader in the meantime."""
    with _cache_file_memo_lock:
        _cache_file_memo["key"] = None
        _cache_file_memo["data"] = {}
    _invalidate_request_store_cache()


@contextlib.contextmanager
def _store_write():
    """db.transaction on the store, plus the per-request read-cache drop on the
    way out — so every app-side write path invalidates without each caller
    having to remember (they return from inside the block)."""
    try:
        with db.transaction(db_path()) as conn:
            yield conn
    finally:
        _invalidate_request_store_cache()


def _cache_file_data() -> dict:
    """The whole store composed as one dict, memoized by a data fingerprint.

    Two layers, because the two costs are different:
      • Per request: one /api/status poll consults the store six times (library
        stats, cleanup gating, the daily window, the queue…). Validating the
        fingerprint OPENS the store — so re-validating per consult cost more
        than composing does. The first consult in a request is reused by the
        rest, which also gives the response one coherent view instead of six
        independently-sampled ones. App-side writes clear it
        (_invalidate_request_store_cache); background threads have no request
        context and skip this layer entirely.
      • Across requests/threads: the fingerprint memo below. It tracks the
        file identity plus the store's write generation, so a commit from the
        engine or the app is always picked up."""
    cached = g.get("_pr_store_data") if has_request_context() else None
    if cached is not None:
        return cached
    p = db_path()
    # Sampled BEFORE the lock: the fingerprint opens the store, and holding the
    # memo lock across that I/O would queue every other reader behind it.
    key = (str(p), db.data_fingerprint(p))
    with _cache_file_memo_lock:
        if _cache_file_memo["key"] == key:
            data = _cache_file_memo["data"]
            if has_request_context():
                g._pr_store_data = data
            return data
    # Memo miss. Compose under a lock so that a store change, which every run,
    # Summary and tick produces; doesn't send every open page off to build the
    # same dict simultaneously: composing the whole store is by far the most
    # expensive read here, and N pollers were each paying it. The first thread
    # in composes; the rest wait and then find the memo already filled. The
    # fingerprint is re-sampled inside the lock so the data is always stored
    # under the version it was actually read at.
    try:
        with _cache_compose_lock:
            key = (str(p), db.data_fingerprint(p))   # re-sampled: we may have waited
            with _cache_file_memo_lock:
                if _cache_file_memo["key"] == key:
                    data = _cache_file_memo["data"]      # filled while we waited
                else:
                    data = None
            if data is None:
                data = db.read_cache_dict(p)
                if not isinstance(data, dict):
                    data = {}
                with _cache_file_memo_lock:
                    _cache_file_memo["key"] = key
                    _cache_file_memo["data"] = data
    except Exception:
        return {}
    if has_request_context():
        g._pr_store_data = data
    return data


def library_stats() -> dict:
    """Last-known dashboard storage stats (the store's dashboard_stats), written by
    Summary/run refreshes so the dashboard's frequent status poll reads cached values
    instead of touching the filesystem."""
    stats = _cache_file_data().get("dashboard_stats")
    return stats if isinstance(stats, dict) else {}

def cached_disk_stats(stats: dict | None = None) -> dict | None:
    """Last-known filesystem capacity, cache-only by design: /api/status polls often
    and must not call disk_usage() every few seconds. Fresh values are written to
    the store by the scheduler tick, config-triggered and manual Summary refreshes,
    and real runs."""
    stats = stats if isinstance(stats, dict) else library_stats()
    disk = stats.get("disk") if isinstance(stats, dict) else None
    if not isinstance(disk, dict):
        return None
    out = {}
    for key in ("used_gb", "total_gb", "free_gb", "pct_used"):
        try:
            out[key] = round(float(disk[key]), 1)
        except (KeyError, TypeError, ValueError):
            return None
    return out
def imdb_ratings_path() -> Path: return output_dir() / "title.ratings.tsv"


def _headroom_window_used_today() -> bool:
    """True when today's once-per-day daily cleanup (headroom or cap trigger)
    already ran. Mirrors the engine's check (local-time date vs the store's
    last_cleanup_date); only a Redline breach ignores the window."""
    try:
        return _cache_file_data().get("last_cleanup_date") == time.strftime("%Y-%m-%d")
    except Exception:
        return False


def _check_directory_read_write(path: Path, label: str) -> dict:
    """Return whether MediaReducer can create, list, write, read, and delete here."""
    # Unique per pid AND thread: two concurrent health checks sharing one probe name
    # could unlink each other's file between write and read; a spurious "cannot read
    # and write its config/log folders" that disables the run buttons.
    probe = path / f".mediareducer-rw-check.{os.getpid()}-{threading.get_ident()}.tmp"
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return {"ok": False, "label": label, "path": str(path), "error": "path is not a folder"}
        list(path.iterdir())
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            return {"ok": False, "label": label, "path": str(path), "error": "readback failed"}
        probe.unlink()
        return {"ok": True, "label": label, "path": str(path), "error": ""}
    except OSError as e:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "label": label, "path": str(path), "error": str(e)}


def _filesystem_rw_state() -> dict:
    """Health check for MediaReducer-owned folders under the configured output dir."""
    checks = [
        _check_directory_read_write(output_dir(), "Config folder"),
        _check_directory_read_write(logs_dir(), "Archived logs folder"),
    ]
    errors = [
        f"{item['label']} ({item['path']}): {item['error']}"
        for item in checks
        if not item.get("ok")
    ]
    return {
        "ok": not errors,
        "checks": checks,
        "errors": errors,
    }

# ── Display time helpers ─────────────────────────────────────────────────────

_LOG_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?P<sep>\s+-\s+|\s+\|\s*)")
# ts | title | path, then optional size_bytes and rationale fields (score, plays,
# last_played); the writer appends them best-effort, so any suffix may be absent.
# Each field is anchored by its key so the non-greedy path can't swallow the tail.
_DELETED_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\|\s+(?P<title>.*?)\s+\|\s+(?P<path>.*?)"
    r"(?:\s+\|\s+size_bytes=(?P<size_bytes>\d+))?"
    r"(?:\s+\|\s+score=(?P<score>[\d.]+))?"
    r"(?:\s+\|\s+plays=(?P<plays>\d+))?"
    r"(?:\s+\|\s+last_played=(?P<last_played>[^|]+?))?"
    r"(?:\s+\|\s+media=(?P<media>\w+))?"
    r"(?:\s+\|\s+season=(?P<season>\d+))?\s*$"
)

def _validate_time_zone(value: str | None) -> str:
    """Normalize the operating timezone. 'auto' means the container clock."""
    raw = str(value or "auto").strip()
    if not raw or raw.lower() == "auto":
        return "auto"
    try:
        ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        raise ValueError("Enter a valid IANA time zone such as America/Phoenix, or use auto.")
    return raw

def _apply_configured_time_zone(cfg: dict | None = None) -> bool:
    """Point the process clock at the configured zone. Everything keyed off local
    time — the once-per-day run window, deletion-delay aging, log timestamps —
    follows it. 'auto' restores the container clock. The engine subprocess inherits
    TZ from our environment and also applies the setting from config.json itself.
    Returns True when the effective zone changed."""
    try:
        name = _validate_time_zone((cfg if cfg is not None else load_config()).get("TIME_ZONE", "auto"))
    except ValueError:
        name = "auto"
    before = os.environ.get("TZ")
    if name == "auto":
        if _HOST_TZ is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _HOST_TZ
    else:
        os.environ["TZ"] = name
    time.tzset()
    return os.environ.get("TZ") != before

def _validate_daily_run_time(value: str | None) -> str:
    """Time of day (24h HH:MM, operating zone) the daily cleanup may fire.
    Blank means midnight."""
    raw = str(value or "").strip()
    if not raw:
        return "00:00"
    if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw):
        return raw
    raise ValueError("Daily run time must be a 24-hour HH:MM time, e.g. 03:30.")

def _validate_display_time_format(value: str | None) -> str:
    raw = str(value or "12h").strip().lower()
    if raw in ("12", "12h", "12-hour", "12 hour"):
        return "12h"
    if raw in ("24", "24h", "24-hour", "24 hour"):
        return "24h"
    raise ValueError("Time format must be 12h or 24h.")

def _request_time_format() -> str:
    # Callable outside a request (a background/test caller rendering timestamps):
    # fall back to the configured format when there is no query string to read.
    if has_request_context():
        requested = request.args.get("time_format") or request.args.get("fmt")
        if requested:
            try:
                return _validate_display_time_format(requested)
            except ValueError:
                pass
    return _validate_display_time_format(load_config().get("DISPLAY_TIME_FORMAT", "12h"))

def _format_dt_for_display(dt: datetime, fmt: str | None = None) -> str:
    fmt = _validate_display_time_format(fmt or _request_time_format())
    if fmt == "24h":
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    # Linux supports %-I. Fall back to stripping a leading zero from %I.
    try:
        return dt.strftime("%Y-%m-%d %-I:%M:%S %p")
    except ValueError:
        s = dt.strftime("%Y-%m-%d %I:%M:%S %p")
        return s.replace(" 0", " ", 1)

def _format_epoch_for_display(epoch_seconds: float | int | None) -> str | None:
    if epoch_seconds is None:
        return None
    # Local time IS the operating zone; TIME_ZONE is applied to the process.
    return _format_dt_for_display(datetime.fromtimestamp(float(epoch_seconds)))

def _format_log_timestamp_for_display(ts: str, fmt: str | None = None) -> str:
    """Re-render a script log timestamp in the configured 12/24-hour format.
    Log timestamps are already written in the operating time zone. Pass fmt to
    reuse a format resolved once when rendering many timestamps in a loop."""
    try:
        return _format_dt_for_display(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), fmt)
    except Exception:
        return ts

def _format_log_text_for_display(text: str) -> str:
    """Re-render leading log timestamps in the configured 12/24-hour format."""
    fmt = _request_time_format()

    def convert_line(line: str) -> str:
        m = _LOG_TS_RE.match(line)
        if not m:
            return line
        try:
            naive = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
            return _format_dt_for_display(naive, fmt) + m.group("sep") + line[m.end():]
        except Exception:
            return line

    return "".join(convert_line(line) for line in text.splitlines(keepends=True))

def _format_reclaimed_size(size_bytes: int | float | None) -> str:
    """Compact display for lifetime space reclaimed from deleted.log size_bytes."""
    try:
        n = max(0, int(size_bytes or 0))
    except (TypeError, ValueError):
        n = 0
    gb = n / 1_000_000_000
    if gb >= 1000:
        return f"{gb / 1000:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = n / 1_000_000
    if mb >= 1:
        return f"{mb:.1f} MB"
    return "0.0 GB"

def _api_connection_error(cfg: dict | None = None) -> bool:
    """True when the LAST connection probe found API problems.

    Reads only cached health (never probes), so it is cheap for the header on every
    page. The signature check matters: Check for Errors can probe UNSAVED form values,
    and a failure there must not paint the tab red on every page after the user
    discards the edit."""
    with _connection_health_cache_lock:
        cached = _connection_health_cache.get("health")
        cached_sig = _connection_health_cache.get("signature")
    if not isinstance(cached, dict):
        return False
    if cached_sig is not None and cached_sig != _connection_health_signature(cfg or load_config()):
        return False
    return bool(cached.get("errors")) or not cached.get("critical_ok", True)


@app.context_processor
def inject_display_time_settings():
    cfg = load_config()
    return {
        "app_version": APP_VERSION,
        "display_time_format": cfg.get("DISPLAY_TIME_FORMAT", "12h"),
        "server_time_zone": _server_time_zone_name(),
        "host_time_zone": _host_time_zone_name(),
        "server_epoch": time.time(),
        "connection_onboarding_needed": _connection_onboarding_needed(cfg),
        "api_connection_error": _api_connection_error(cfg),
        "debug_mode": bool(cfg.get("DEBUG_MODE")),
        # Stamped on <html> by base.html so the very first paint is already
        # right — a page that animated once and then settled would defeat the
        # point of asking for no animation, and glass that painted once and
        # then vanished would be a flash of the thing being turned off.
        **_appearance_flags(request),
        "welcome_needed": _welcome_guide_needed(cfg),
        # First-launch only: adopt the browser's time zone (client posts it to
        # /api/timezone/init). True only on a brand-new install still on "auto"
        # that hasn't detected yet and has never configured connections, so an
        # existing install or a deliberate "auto" is never overwritten.
        "time_zone_needs_init": (not cfg.get("_TIME_ZONE_AUTODETECTED")
                                 and not cfg.get(CONNECTION_EVER_CONFIGURED_KEY)
                                 and str(cfg.get("TIME_ZONE", "auto")).strip().lower() == "auto"),
    }


@app.route("/api/timezone/init", methods=["POST"])
def api_timezone_init():
    """First-launch only: adopt the browser-detected IANA time zone as the
    TIME_ZONE setting and persist it, so a fresh install lands on the user's
    zone instead of the container's (usually UTC).

    One-shot and heavily guarded: no-ops once _TIME_ZONE_AUTODETECTED is set, if
    connections were ever configured, or if TIME_ZONE is no longer "auto" — so a
    deliberate choice or an existing install is never overwritten. Keeps "auto"
    if the posted zone is missing or invalid. Only touches TIME_ZONE + the flag;
    not the full config-save path."""
    cfg = load_config()
    already = (cfg.get("_TIME_ZONE_AUTODETECTED")
               or cfg.get(CONNECTION_EVER_CONFIGURED_KEY)
               or str(cfg.get("TIME_ZONE", "auto")).strip().lower() != "auto")
    if already:
        return jsonify({"ok": True, "already": True, "time_zone": cfg.get("TIME_ZONE", "auto")})
    if _run_active:
        # Never rewrite config mid-run; the next page load retries.
        return jsonify({"ok": False, "deferred": True})
    tz = str((request.get_json(silent=True) or {}).get("tz") or "").strip()
    try:
        applied = _validate_time_zone(tz) if tz else "auto"
    except ValueError:
        applied = "auto"   # unknown browser zone; stay on the container clock
    cfg["TIME_ZONE"] = applied
    cfg["_TIME_ZONE_AUTODETECTED"] = True
    save_config(cfg)
    if _apply_configured_time_zone(cfg):
        # Startup judged today's slot under the CONTAINER clock; re-judge under
        # the detected zone, in both directions: burn if the corrected clock is
        # past the run time, and re-arm if the startup burn was a misjudgment
        # (slot actually still ahead). This runs once per install, before any
        # real run could have used the window, so the reopen can't hand back a
        # window a genuine cleanup consumed.
        global _last_maintenance_scan_epoch
        burn_daily_window_on_startup("timezone auto-detect")
        burn_todays_maintenance_slot_on_startup()
        try:
            run_epoch = _todays_daily_run_epoch(cfg)
            if run_epoch is not None and time.time() < run_epoch:
                reopen_daily_window(reason="timezone auto-detect — run time still ahead")
                _last_maintenance_scan_epoch = None
        except Exception:
            pass
    return jsonify({"ok": True, "time_zone": applied})

# ── Run manager ───────────────────────────────────────────────────────────────

# All in-memory: a restart resets every flag (no run survives a container stop).
_run_lock      = threading.Lock()
_run_active    = False
_run_cleanup      = False         # True while the active run is a deleting run, not a simulation
_run_debug_cleanup = False        # True while the active run is a Debug Cleanup (live path, no deletions)
# True while the active run was started from a button rather than the scheduler.
# Reported on /api/status as a fact about the run; nothing in the UI branches on
# it — the status pills answer whether a run is DELETING, which is the same
# either way.
_run_manual    = False
_run_start     = None          # datetime
_run_process   = None          # subprocess.Popen
_run_stop_requested = threading.Event()
_shutting_down = False         # a container/app shutdown signal is being handled
_summary_active = False        # background Summary (debug_info) stats refresh in progress
_summary_queued = False        # coalesced follow-up Summary requested while one is active
# Today's daily-run epoch we last spun the maintenance Simulate for, so a
# scan that fails past the connection probe (leaving the snapshot stale) isn't respun
# every ~15-min tick; it retries next window / on restart / on a manual Simulate.
_last_maintenance_scan_epoch = None

def _pause_scheduler_for_run():
    """Freeze the single background clock while a run or Summary is active."""
    try:
        scheduler.pause_job("engine")
    except Exception:
        # Scheduler may not be initialized yet during import/startup.
        pass

def _restart_schedule_clock():
    """Restart the single background clock from zero: a run or Summary just finished,
    so the info is fresh and the next tick should be a full interval away.
    Rescheduling also un-pauses the job if it was paused for the run. Best-effort —
    never let a scheduler hiccup escape into a worker's finally block."""
    try:
        scheduler.reschedule_job(
            "engine", trigger="interval", minutes=SCHEDULE_INTERVAL_MINUTES,
        )
    except Exception:
        pass
    _arm_daily_slot_job()


def _next_daily_slot_epoch(cfg: dict | None = None):
    """Epoch of the NEXT daily-run slot: today's if still ahead, else
    tomorrow's. An unparseable DAILY_RUN_TIME falls back to 00:00 via
    _daily_run_time; None only when mktime itself fails."""
    epoch = _todays_daily_run_epoch(cfg)
    if epoch is None:
        return None
    if epoch > time.time():
        return epoch
    try:
        hh, mm = (int(x) for x in _daily_run_time(cfg).split(":"))
        now = time.localtime()
        # mktime normalizes tm_mday+1 across month/year ends, and isdst=-1
        # re-derives DST for the new date.
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday + 1, hh, mm, 0,
                            0, 0, -1))
    except (ValueError, AttributeError, OverflowError):
        return None


def _arm_daily_slot_job(cfg: dict | None = None):
    """Point a one-shot scheduler job at the NEXT daily-run slot, so the daily
    Simulate/cleanup fires AT the configured time instead of on the first
    15-minute tick after it (which averaged ~7 minutes late — a countdown
    restarting at 1:50 didn't run a 2:00 slot until 2:05). The job just invokes
    the ordinary tick: every existing guard (run-active, latch, window,
    connections) applies, and the 15-minute tick remains the catch-all if this
    precise shot is lost (mid-run pause, scheduler hiccup). Re-armed after
    every run/save/tick; replace_existing keeps it single."""
    try:
        epoch = _next_daily_slot_epoch(cfg)
        if epoch is None:
            return
        scheduler.add_job(
            _scheduled_tick, "date",
            # +1s: 'now >= slot' holds inside the tick. UTC-AWARE on purpose:
            # a naive datetime would be localized in the scheduler's
            # construction-time zone, so a runtime TIME_ZONE change (tzset)
            # would mis-aim every re-arm until restart; an aware instant is
            # unambiguous (and immune to DST-fold disagreements too).
            run_date=datetime.fromtimestamp(epoch + 1, tz=_utc_tz.utc),
            id="daily-slot", replace_existing=True,
            # APScheduler's default misfire grace is ONE second; a busy
            # executor or a slow wakeup would silently discard the shot, in
            # exactly the conditions most likely to delay it. Any same-hour
            # late fire is still this slot's work.
            misfire_grace_time=3600,
        )
    except Exception:
        pass

def _write_progress_start_stub(mode_override: str | None, *, manual: bool = False):
    """Reset progress.json to a 'starting' state the instant a run is launched, so the
    dashboard panel flips immediately — even before the subprocess has imported Python.
    The engine then overwrites this with real phase updates. Best-effort only.

    Returns the started_at it stamped, which IS the run's identity: the engine
    is handed the same value so both writers agree. The dashboard treats a
    changed started_at as a whole new run and restarts its per-stage bar, so
    two stamps for one run made the first stage fill, snap to empty, and fill
    again."""
    try:
        now = time.time()
        _atomic_write_json(progress_path(), {
            "schema": 1, "status": "starting", "phase": "checking",
            "mode": mode_override or load_config().get("RUN_MODE"),
            # The engine re-states this from MEDIAREDUCER_MANUAL, which run_script
            # sets from the same argument; the stub only makes the very first
            # frame right, before the subprocess has emitted anything.
            "manual": bool(manual),
            "scanned": 0, "total": 0, "eligible": 0, "protected": 0, "skipped": 0,
            "deleted": 0, "bytes_freed": 0, "target_bytes": 0,
            "trigger": "", "current_title": "", "message": "Starting…",
            "started_at": now, "updated_at": now,
        })
        return now
    except Exception:
        return None


def _mark_progress_terminal(status: str, message: str, *, force: bool = False):
    """Best-effort terminal progress marker for stops/crashes. The engine owns normal
    progress updates; this only fills the gap when it is terminated or exits before
    writing a terminal state, preserving the last phase so the dashboard can mark the
    exact stopped/failed stage."""
    try:
        p = progress_path()
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if not force and data.get("status") in ("done", "stopped", "error"):
            return
        now = time.time()
        data.update({
            "schema": data.get("schema", 1),
            "status": status,
            "phase": data.get("phase") or "checking",
            "message": message,
            "updated_at": now,
            "ended_at": now,
            })
        data.setdefault("started_at", now)
        _atomic_write_json(p, data)
    except Exception:
        pass


def _preview_rebuild_needed(cleanup_run: bool) -> bool:
    """Should a background Simulate rebuild the standing queue after a run?

    Two triggers: the engine's queue_rebuild progress flag (a Redline fast path
    consumed marks), or — the invariant that catches every other path — any LIVE
    run in redline-only mode that actually deleted something (manual Cleanups
    and full-scan Redline fallbacks trim the queue too, and the mode has no
    daily runs to replenish it). Sim runs never qualify: the Simulate IS the
    rebuild, so they'd loop forever."""
    try:
        prog = json.loads(progress_path().read_text(encoding="utf-8"))
        if prog.get("queue_rebuild"):
            return True
        deleted_any = (prog.get("deleted") or 0) > 0
    except Exception:
        deleted_any = False
    if not cleanup_run:
        return False
    try:
        return _redline_only_mode_cfg() and deleted_any
    except Exception:
        return False


def _maybe_rebuild_preview_after_run(cleanup_run: bool = False) -> None:
    """After a successful run, rebuild the standing preview when needed (see
    _preview_rebuild_needed): kick a background Simulate so it grows back to
    full strength. The post-run Summary owns the lock briefly, so retry for a
    while rather than failing; any new engine run rewrites progress.json, which
    clears the fast path's flag."""
    if not _preview_rebuild_needed(cleanup_run):
        return

    def _kick():
        # The post-run Summary can legitimately take minutes on a large library
        # (its own subprocess budget is 600 s); wait past that, not under it.
        for _ in range(140):           # up to ~11½ minutes
            time.sleep(5)
            if _run_active or _summary_active:
                continue
            # In headroom mode a within-limits Simulate would spin up just to
            # no-op ("nothing to simulate"); the daily run replans anyway.
            # Redline-only mode always rebuilds (the preview IS the plan).
            cfg = load_config()
            if not _redline_only_mode_cfg(cfg):
                try:
                    if not _deletion_limits_exceeded(cfg, disk_stats(),
                                                     library_stats().get("library_gb")):
                        print("Queue rebuild: skipped — limits are satisfied and the "
                              "daily run replans on the next breach.", flush=True)
                        return
                except Exception:
                    pass   # can't judge → let the Simulate decide
            ok, msg = run_script(mode_override="debug_sim")
            print(("Queue rebuild: background Simulate started to restore the marked "
                   "preview after the Redline fast path."
                   if ok else f"Queue rebuild: could not start the Simulate ({msg})."),
                  flush=True)
            return
        print("Queue rebuild: gave up waiting for the post-run refresh to finish.", flush=True)

    threading.Thread(target=_kick, daemon=True, name="queue-rebuild").start()


def _marked_queue_state(cfg: dict) -> tuple[str, dict]:
    """A stable signature of the CURRENT scheduled-marks state, plus the marked
    entries themselves ({path: {title, delete_on}}). The signature covers each
    mark's path and its effective delete date. Marks keep the delay they were
    made under, so a delay-setting change alone never re-signatures them — the
    config page's delay-reset button does (re-stamping every mark), and that
    earns one fresh alert with the new dates."""
    marked = {}
    for e in pending_deletion_entries(cfg, with_lines=True):
        if e.get("marked_at") is None:
            continue
        marked[e["path"]] = {"title": e.get("title") or Path(e["path"]).name,
                             "delete_on": e.get("delete_on") or ""}
    fingerprint = "\n".join(f"{p}|{marked[p]['delete_on']}" for p in sorted(marked))
    sig = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return sig, marked


def _store_notify_meta(key: str, value) -> None:
    try:
        with _store_write() as conn:
            db.set_meta(conn, key, value)
    except Exception:
        pass


def _read_notify_meta(key: str):
    """Read a notification baseline. A plain read, deliberately: taking a write
    transaction for it would fail whenever another writer holds the store, and
    the callers read "no baseline" as "first observation" — which re-baselines
    and swallows the alert that read was meant to raise."""
    try:
        with db.connect(db_path()) as conn:
            return db.get_meta(conn, key)
    except Exception:
        return None


# The per-destination notification rate limit lives in notify.py but its state
# has to outlive the process; in memory alone, a container bounce clears every
# cooldown and the next alert goes out inside a window that was still open. The
# keys are hashed destination ids, so no webhook token lands in the store.
notify.set_rate_state_store(
    lambda: _read_notify_meta("notify_rate_state"),
    lambda state: _store_notify_meta("notify_rate_state", state),
)


def _update_marked_baseline(cfg: dict | None = None) -> None:
    """Re-baseline the marked-queue state WITHOUT alerting — called after every
    completed run, whose own notification already covers its marks. Stores the
    marked map alongside the signature so the next 15-minute diff can name
    exactly which movies are new (a sig-only baseline would make it misreport
    the whole marked set as new)."""
    try:
        cfg = cfg or load_config()
        sig, marked = _marked_queue_state(cfg)
        _store_notify_meta("notify_marked_state", {"sig": sig, "marked": marked})
    except Exception:
        pass


def _notifications_muted_by_mode(cfg: dict) -> bool:
    """True when the scheduler mode means NOTHING goes out — no run summary, no
    marked-changes alert, no low-space warning.

    Fully Paused is silent by definition. Monitor Only is silent too unless
    "Also in Monitor Only" is ticked, and that opt-in covers every alert rather
    than the run summary alone: a mode that never deletes on its own, with its
    notifications turned off, is expected to be quiet — being told what a scan
    marked is exactly the alert someone switching it off is switching off.

    The muted modes leave every notification BASELINE untouched as well, so
    turning the opt-in back on (or arming Automatic Cleanup) reports what
    changed meanwhile in one alert instead of silently swallowing it."""
    mode = cfg.get("RUN_MODE")
    if _is_cleanup_mode(mode):
        return False
    return mode == "off" or not notify.flag(cfg, "NOTIFY_IN_MONITOR_ONLY")


def _check_marked_change_notification() -> None:
    """After a 15-minute maintenance pass: if the scheduled-marks set changed
    since the last baseline (new marks, or the delay-reset button re-dating
    them), send ONE alert and re-baseline — never a repeat for the same state.
    Silent in every mode that mutes notifications (see
    _notifications_muted_by_mode). Marks whose run sent its own notification
    never land here (that dispatch re-baselines first)."""
    try:
        cfg = load_config()
        if _notifications_muted_by_mode(cfg):
            return
        sig, marked = _marked_queue_state(cfg)
        prev = _read_notify_meta("notify_marked_state")
        prev_sig = (prev or {}).get("sig") if isinstance(prev, dict) else None
        prev_marked = (prev or {}).get("marked") if isinstance(prev, dict) else None
        if prev_sig == sig:
            return
        # The baseline (which also stores the marked map so the NEXT diff can
        # name exactly which movies are new) advances immediately when nothing
        # needs announcing — but when an alert IS due, it advances only after
        # the send succeeded: storing first meant one failed delivery lost
        # those marks forever, since the next tick compared against a baseline
        # that already contained them.
        baseline = {"sig": sig, "marked": marked}
        if prev_sig is None:
            _store_notify_meta("notify_marked_state", baseline)
            return   # first observation; baseline only, nothing to compare
        if not (notify.flag(cfg, "NOTIFY_ENABLED") and notify.flag(cfg, "NOTIFY_ON_MARKED_CHANGES")):
            _store_notify_meta("notify_marked_state", baseline)
            return
        prev_paths = set(prev_marked or {})
        new_items = [dict(v) for p, v in marked.items() if p not in prev_paths]
        redated = False
        if not new_items and isinstance(prev_marked, dict):
            # No additions — but a re-dated existing mark (delay change) also
            # deserves one alert, presented as the current marked set.
            new_items = [dict(v) for p, v in marked.items()
                         if p in prev_marked and prev_marked[p].get("delete_on") != v["delete_on"]]
            redated = bool(new_items)
        if not new_items:
            _store_notify_meta("notify_marked_state", baseline)
            return   # marks only dropped off; nothing new to announce
        sent, _detail = notify.dispatch_marked_changes(
            cfg, new_items=new_items, marked_total=len(marked), redated=redated)
        if sent:
            _store_notify_meta("notify_marked_state", baseline)
    except Exception:
        pass


def _check_low_space_notification(cfg: dict, disk: dict | None) -> None:
    """Every scheduler tick: warn ONCE when free space enters the configured
    margin above the Redline floor. Silent in every mode that mutes
    notifications (see _notifications_muted_by_mode). The warning re-arms after
    space recovers back above the margin, or the floor/margin values change —
    never a repeat every 15 minutes; the re-arm bookkeeping runs even while
    muted, so a stale flag can't swallow the first warning after unmuting."""
    try:
        state = _read_notify_meta("notify_low_space")
        state = state if isinstance(state, dict) else {}
        redline = cfg.get("REDLINE_GB")
        if redline is None:
            # No floor to watch. Clear a stale flag so the same floor re-armed
            # later still warns on its next approach.
            if state.get("warned"):
                _store_notify_meta("notify_low_space", {"warned": False})
            return
        redline = float(redline)
        try:
            margin = max(1.0, float(cfg.get("NOTIFY_LOW_SPACE_GB")
                                      or notify.NUMBER_DEFAULTS["NOTIFY_LOW_SPACE_GB"]))
        except (TypeError, ValueError):
            margin = 25.0
        free_gb = float((disk or {}).get("free_gb"))
        armed_for = (state.get("redline"), state.get("margin"))
        in_zone = redline < free_gb <= redline + margin
        if not in_zone:
            # Above the zone (recovered) or already below the floor (the
            # emergency cleanup's own notification takes over): re-arm. This
            # runs even with the alert toggled off, so a stale "warned" flag
            # can't suppress the first warning after re-enabling it.
            if state.get("warned"):
                _store_notify_meta("notify_low_space", {"warned": False})
            return
        if (not (notify.flag(cfg, "NOTIFY_ENABLED") and notify.flag(cfg, "NOTIFY_ON_LOW_SPACE"))
                or _notifications_muted_by_mode(cfg)):
            return
        if state.get("warned") and armed_for == (redline, margin):
            return   # already warned for this exact floor/margin; no spam
        items = None
        if notify.flag(cfg, "NOTIFY_SHOW_MOVIES"):
            entries = pending_deletion_entries(cfg, with_lines=True)
            # Marked entries are first in line; without any (redline-only mode
            # above the floor has a zero deficit, so nothing reads as marked)
            # the queue front IS the deletion order an emergency would follow.
            front = [e for e in entries if e.get("marked")] or entries
            items = [{"title": e.get("title"), "size": e.get("size_bytes")}
                     for e in front][:15]
        sent, _detail = notify.dispatch_low_space(cfg, free_gb=free_gb, redline_gb=redline,
                                                  margin_gb=margin, items=items)
        # Only a DELIVERED warning is a warning: storing "warned" on a failed
        # send silenced the alert until free space recovered past the margin
        # and dropped again — the next tick retries instead.
        if sent:
            _store_notify_meta("notify_low_space",
                               {"warned": True, "redline": redline, "margin": margin})
    except Exception:
        pass


def _dispatch_run_notifications(effective_mode: str | None, returncode: int, stopped: bool,
                                *, scheduled: bool = False,
                                tv_pass_report: dict | None = None) -> None:
    """After a run's subprocess exits, deliver any configured outbound alert.

    Runs on its own daemon thread so notification network I/O can never delay the
    run worker's cleanup/summary steps, and is wrapped so it can never raise into
    the caller. What notifies:
      • every cleanup (manual or automatic);
      • Simulate ONLY when the scheduler launched it (the daily maintenance
        Simulate) — a manual Simulate is watched live on the dashboard;
      • Debug Cleanup is silent like a manual Simulate (a button-press dry run
        being watched live; the between-run alerts cover its marks anyway);
      • a user-initiated Stop is silent.
    A COMPLETED run re-baselines the marked-queue state afterwards ONLY when its
    marks were actually announced (its own notification went out), nothing is
    listening (notifications/marked-change alerts off), or the run is silent BY
    DESIGN (a manual Simulate watched live on the dashboard). A run whose
    summary was suppressed — Monitor Only without the opt-in, or the summary
    toggle off — leaves the baseline alone so the next 15-minute check
    announces the new marks through the marked-changes alert instead of
    silently swallowing them. A FAILED run also leaves the baseline alone —
    whatever the queue holds was never announced, so the next tick may still
    report it."""
    try:
        cfg = load_config()
        is_debug = effective_mode == "debug_cleanup"
        is_sim = effective_mode == "debug_sim"
        wants_notify = _is_cleanup_mode(effective_mode) or (is_sim and scheduled)
        # A muted mode swallows the run summary AND leaves the baseline alone,
        # so the marks this run made are reported once if the opt-in is later
        # turned on. Handing them to the marked-changes alert instead would mean
        # switching run notifications off in Monitor Only produces a "34 movies
        # marked" message minutes later: the opposite of what turning them off
        # asks for.
        muted = _notifications_muted_by_mode(cfg)
        # Is anything still listening for these marks if this run's own summary
        # doesn't go out? With the marked-changes alert off, nobody is, so the
        # baseline moves rather than being held open for an alert that will
        # never fire. Read here, beside the other gating flags, and closed over
        # by _work below.
        marked_alert_armed = notify.flag(cfg, "NOTIFY_ON_MARKED_CHANGES")
        if not notify.flag(cfg, "NOTIFY_ENABLED") or stopped or not wants_notify or muted:
            # Debug Cleanup is silent by design, but it PERSISTS the same
            # queue upkeep a real tick would — its mark changes are real, and
            # the promise above is that the between-run marked-changes alert
            # covers them. Re-baselining here would swallow them unannounced,
            # so while that alert is armed the baseline is left for it.
            if (returncode == 0 and not stopped and not muted
                    and not (is_debug and marked_alert_armed)):
                threading.Thread(target=_update_marked_baseline, daemon=True,
                                 name="notify-baseline").start()
            return

        # Read the report BEFORE handing off to the thread; a background queue
        # rebuild launched right after this run would clear the file.
        report = _read_run_report() if returncode == 0 else None

        def _work():
            summary_sent = False
            try:
                # The engine's own terminal write, not the exit code: a run that
                # stops on a bad connection or an invalid threshold RETURNS from
                # run() rather than exiting nonzero, so gating the alert on the
                # exit code alone stayed silent on the two failures a user is
                # most likely to actually hit — while the dashboard showed the
                # red x for them all along.
                _prog = {}
                try:
                    _prog = json.loads(progress_path().read_text(encoding="utf-8"))
                    if not isinstance(_prog, dict) or _prog.get("status") != "error":
                        _prog = {}
                except Exception:
                    _prog = {}
                if returncode == 0 and not _prog:
                    if report:
                        # Enrich with the app's own queue view: total marked,
                        # the next-deletion forecast (date / "at the next run"),
                        # and the marks that PREDATE this run with their due
                        # dates — the daily summary's "Already marked" section
                        # (the run's own new marks are listed separately).
                        try:
                            fresh_cfg = load_config()
                            fc = pending_delete_forecast(fresh_cfg)
                            _sig, marked = _marked_queue_state(fresh_cfg)
                            report.setdefault("marked_total", len(marked))
                            report.setdefault("next_event_on", fc.get("event_on"))
                            report.setdefault("next_event_ripe", bool(fc.get("ripe")))
                            new_items = report.get("marked_items") or []
                            new_paths = {str(it.get("path") or "") for it in new_items}
                            # Only a TRUNCATED marked_items list (the engine
                            # caps it at _MARKED_ITEMS_CAP) leaves new-mark
                            # paths unknown here, which would misfile them as
                            # "already marked"; skip the section only then.
                            # marked_count can't answer this: a Simulate
                            # reports the total it would delete now, not how
                            # many of those marks are new, so comparing the two
                            # hid the list on every day the marks carried over.
                            if len(new_items) < _MARKED_ITEMS_CAP:
                                report.setdefault("existing_marked_items", [
                                    {"title": v.get("title"), "delete_on": v.get("delete_on")}
                                    for p, v in marked.items() if p not in new_paths])
                            # The seasons this run handled ride the same
                            # message, folded into the run's own numbers —
                            # one cleanup, one report. A season-side
                            # fail-closed abort means movies covered the
                            # whole deficit. Passed in per run, never read
                            # from a global the NEXT run may already have
                            # overwritten.
                            tvp = tv_pass_report
                            if isinstance(tvp, dict):
                                report.setdefault("seasons", {
                                    "marked_new": tvp.get("marked_new") or 0,
                                    "held_by_delay": tvp.get("held_by_delay") or 0,
                                    "deleted_seasons": len(tvp.get("deleted_seasons") or []),
                                    "skipped": len(tvp.get("skipped") or []),
                                    "freed_bytes": tvp.get("freed_bytes") or 0,
                                    "aborted": tvp.get("aborted"),
                                    "marked_total": len(_tv_cleanup_state().get("marked") or {}),
                                })
                        except Exception:
                            pass
                        summary_sent, _detail = notify.dispatch_run_report(cfg, report)
                elif not is_debug:
                    # The failure in the words the dashboard is showing for the
                    # same event; a torn or stale file just falls back.
                    notify.dispatch_error(
                        cfg, _prog.get("message") or RUN_ENDED_UNEXPECTEDLY,
                        stage=_prog.get("stage") or "",
                        issues=_prog.get("issues") or [],
                        names=_prog.get("names") or [])
            except Exception:
                pass  # alerting is best-effort; never surface into the app
            finally:
                # Marks the summary didn't announce (toggle off, no report, or
                # a failed delivery) stay un-baselined for the 15-minute
                # marked-changes alert to pick up; unless that alert is off.
                if returncode == 0 and (summary_sent or not marked_alert_armed):
                    _update_marked_baseline()

        threading.Thread(target=_work, daemon=True, name="notify-dispatch").start()
    except Exception:
        pass


def run_script(mode_override: str | None = None, manual: bool = False,
               scheduled: bool = False) -> tuple[bool, str]:
    """Launch engine.py as a subprocess. manual=True marks a Dashboard-button run:
    a manual Cleanup prunes every breached target immediately — the deletion delay
    and once-per-day window pace automatic runs only. scheduled=True marks a
    launch from the scheduler tick (drives which runs notify — the daily
    maintenance Simulate alerts, other automatic Simulates don't).
    Returns (started, message)."""
    global _run_active, _run_cleanup, _run_debug_cleanup, _run_manual, _run_start

    with _run_lock:
        if _run_active:
            return False, "A run is already in progress."
        if _summary_active:
            return False, "A background status refresh is finishing — try again in a moment."

        # A store damaged since startup (a full disk, a power loss mid-write)
        # would kill the engine on its first read; every run failing the same
        # way with nothing the user can do from the UI. Rebuild it here instead,
        # so this run starts from the same clean slate a fresh install would.
        # Inside the lock and past the busy guards: unlinking the file out from
        # under a live engine subprocess would break the run it is trying to save.
        _rebuild_store_if_damaged(reason="pre-run check")

        _run_stop_requested.clear()
        _effective_mode = mode_override or load_config().get("RUN_MODE")
        _run_active  = True
        _run_cleanup    = _is_cleanup_mode(_effective_mode)
        _run_debug_cleanup = (_effective_mode == "debug_cleanup")
        _run_manual  = bool(manual)
        _run_start   = datetime.now()
        _pause_scheduler_for_run()
        _run_started_at = _write_progress_start_stub(mode_override, manual=manual)
        _clear_run_report(output_dir())

    def _worker():
        global _run_active, _run_process
        try:
            env = os.environ.copy()
            env["MEDIAREDUCER_CONFIG"] = str(CONFIG_PATH)
            if mode_override:
                env["MEDIAREDUCER_MODE_OVERRIDE"] = mode_override
            if manual:
                env["MEDIAREDUCER_MANUAL"] = "1"
            # One run, one started_at. Without this the engine mints its own,
            # the dashboard reads the change as a new run, and the Checking
            # bar restarts from empty mid-stage.
            if _run_started_at:
                env["MEDIAREDUCER_RUN_STARTED_AT"] = repr(float(_run_started_at))

            if _run_stop_requested.is_set():
                return

            # The run's season side fires WITH every run — a Simulate or
            # Debug Cleanup marks, a real Cleanup also deletes due marks.
            # BEFORE the engine launches: the tv_share stamp it subtracts is
            # then fresh, and the two deleters never run concurrently. Its
            # failures never block the movie run (and its own fetch is
            # fail-closed anyway). When it does NOT fire (disarmed, Redline
            # emergency, crash), the share is stamped 0 so the engine never
            # subtracts bytes no executor is going to free.
            _tv_report = None
            try:
                _tv_pass_cfg = load_config()
                _tv_exec = _tv_pass_plan_for_run(_tv_pass_cfg, _effective_mode)
                if _tv_exec is not None:
                    # `manual` rides along: a Dashboard Cleanup prunes every
                    # breached target NOW, which is this function's own contract
                    # and what the movie side has always done. Without it the
                    # season side sat out its delay while the movies went, and
                    # since the seasons' share is subtracted from the movie
                    # target first, the run freed less than it was asked for.
                    _tv_report = _run_tv_cleanup_pass(_tv_pass_cfg,
                                                      execute=_tv_exec,
                                                      immediate=manual)
                else:
                    _stamp_tv_share(0)
            except Exception as e:
                print(f"TV cleanup failed: {e}", flush=True)
                _stamp_tv_share(0)

            if _run_stop_requested.is_set():
                return

            proc = subprocess.Popen(
                ["python3", "-u", str(SCRIPT_PATH)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with _run_lock:
                _run_process = proc
                if _run_stop_requested.is_set() and proc.poll() is None:
                    proc.terminate()
            returncode = proc.wait()
            stopped = _run_stop_requested.is_set()
            if stopped:
                _mark_progress_terminal("stopped", "Run stopped.", force=True)
            elif returncode == 0:
                # A completed Redline fast path asks for its preview to be rebuilt;
                # any cleanup in redline-only mode that thinned the preview does too.
                _maybe_rebuild_preview_after_run(
                    cleanup_run=_is_cleanup_mode(_effective_mode))
            else:
                _mark_progress_terminal("error", RUN_ENDED_UNEXPECTEDLY)
                # Runs fail closed on any API error, so re-probe now: the cached
                # health drives the red Configuration tab, the jump-to-Connections
                # link, and the per-field error highlights.
                threading.Thread(
                    target=lambda: _refresh_connection_health_cache(load_config(), probe=True),
                    daemon=True, name="engine-postfail-health",
                ).start()
            # Deliver any configured outbound alert for this run (own thread,
            # best-effort — see _dispatch_run_notifications).
            _dispatch_run_notifications(_effective_mode, returncode, stopped,
                                        scheduled=scheduled,
                                        tv_pass_report=_tv_report)
        except Exception as e:
            # A failed launch (Popen OSError etc.) must not vanish in the daemon
            # thread — mark the run terminal so the dashboard shows the failure
            # instead of a phantom active run.
            _mark_progress_terminal("error", f"Could not launch the run: {e}", force=True)
        finally:
            with _run_lock:
                _run_active  = False
                _run_process = None
                _run_stop_requested.clear()
            # Only a deleting run leaves storage stats stale: it reads the
            # library size before deleting and never recomputes it, and even a stop
            # can have removed files first. So kick a quiet Summary to refresh the
            # size (it also restarts the clock when it lands). A simulation deletes
            # nothing and writes fresh stats during its own pass, so it just needs
            # the clock restarted.
            # This run may have built the snapshot a resting scheduler was
            # waiting on — or dropped one the current mode relied on. Judged
            # here, after the run state is cleared: the sync deliberately does
            # nothing while a run holds the store mid-write.
            _sync_scheduler_mode_with_db()
            # A completed scan is also the moment to refresh the TV inventory:
            # "run Simulate" is how the library gets rebuilt in this app, so TV
            # follows the same gesture. It also repairs the code-change case —
            # ensure_code_current wipes EVERY snapshot row including TV, and the
            # Simulate that rebuilds the movies would otherwise leave the TV
            # view empty until some unrelated config save. Own thread: the
            # media-server inventory plus the per-user watch sweeps are seconds
            # of HTTP, and nothing here may hold up the run-end path.
            _tv_cfg = load_config()
            if _tv_inventory_configured(_tv_cfg):
                threading.Thread(
                    target=lambda: _refresh_tv_inventory(_tv_cfg),
                    daemon=True, name="tv-inventory-refresh",
                ).start()
            if _is_cleanup_mode(_effective_mode):
                run_summary()
            else:
                _restart_schedule_clock()
                # Drain any reconcile queued while this run held the lock (the
                # cleanup branch drains through its Summary worker instead).
                _maybe_launch_queued_reconcile()

    threading.Thread(target=_worker, daemon=True, name="engine-run").start()
    return True, "Run started."

def stop_script():
    with _run_lock:
        if not _run_active:
            return False
        _run_stop_requested.set()
        proc = _run_process
    _mark_progress_terminal("stopped", "Run stopped — deletions already made are permanent.", force=True)
    if proc and proc.poll() is None:
        proc.terminate()
    return True


# How long to wait for a killed engine to actually die before reporting that it
# didn't. Only an unkillable process reaches the end of this, so it is short.
FORCE_STOP_WAIT_S = 5


def force_stop_script():
    """Kill the engine outright, for a run the graceful Stop could not end.
    Returns (ok, message).

    Stop sends SIGTERM, which the engine turns into a clean unwind — it finishes
    the unlink→deleted.log record it is in the middle of, archives its partial
    log, then exits. SIGKILL cannot be caught, so none of that happens: the log
    is not archived, and a deletion caught mid-record can be missing from the
    history. That is the trade, and it is why this is a separate button rather
    than what Stop escalates to on its own.

    SIGKILL is also not a bigger hammer for every case. A process blocked in an
    uninterruptible syscall — the classic hung NFS/SMB mount — takes neither
    signal until the kernel lets it go, so this waits for the process to
    actually die and reports what happened instead of assuming it worked."""
    with _run_lock:
        active, proc = _run_active, _run_process
        if not active:
            return False, "No active run."
        _run_stop_requested.set()
    if proc is None:
        # Flagged active but the process was never assigned: the launcher is in
        # the window between claiming the run and Popen returning. The stop
        # request set above makes it bail there, so this is already handled.
        return True, "The run was stopping before it started; nothing to kill."
    if proc.poll() is not None:
        return True, "The run had already ended; the dashboard will catch up."
    try:
        proc.kill()
    except Exception as e:
        return False, f"Could not signal the run: {e}"
    # poll(), not wait(): the run worker owns wait() and reaping the child, and
    # racing it here would block this request behind that thread.
    deadline = time.time() + FORCE_STOP_WAIT_S
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        return False, (
            "The engine did not respond to being killed. That almost always means it is "
            "stuck in a system call the kernel won't interrupt — usually a network share "
            "that stopped answering. No signal can end it until that storage responds "
            "again or the host restarts. Check the mount for your media library first.")
    _mark_progress_terminal(
        "stopped", "Run force-stopped. Its log was not archived; deletions already made "
                   "are permanent.", force=True)
    return True, "Run terminated. Settings unlock as soon as the dashboard refreshes."


def _graceful_shutdown(signum, frame):
    """Container/app stop (SIGTERM/SIGINT to PID 1): forward the stop to an active
    deletion run so the engine finishes its current file's unlink→deleted.log record
    and archives its partial log (the same clean path as the web Stop button) before
    the app exits.

    Without this, PID 1 exits on the signal and the kernel SIGKILLs the still-running
    engine child mid delete-and-record. We wait a few seconds (well inside Docker's
    default 10s stop grace) for the engine to exit; if it doesn't, we fall through and
    let the container's own SIGKILL take it."""
    global _shutting_down
    if _shutting_down:
        # A second signal (or an impatient orchestrator); exit now.
        raise SystemExit(0)
    _shutting_down = True
    proc = _run_process
    if _run_active and proc is not None and proc.poll() is None:
        print("Shutdown: stopping the active run cleanly before exit…", flush=True)
        try:
            stop_script()                       # SIGTERM → engine's graceful path
        except Exception as e:
            print(f"Shutdown: stop_script failed ({e}); terminating engine directly.", flush=True)
            try:
                proc.terminate()
            except Exception:
                pass
        # poll() (not wait()) so we don't race the worker thread's own wait(): the
        # worker reaps the child and poll() then sees the cached returncode.
        deadline = time.time() + 8
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        print("Shutdown: engine exited; app shutting down.", flush=True)
    raise SystemExit(0)


def _run_summary_subprocess(config_path: Path, timeout: int = 600) -> tuple[bool, str]:
    # 600s: the summary walks every monitored dir, and a large library on
    # spinning/network storage can legitimately take minutes.
    """Run engine.py in quiet debug_info mode against config_path."""
    env = os.environ.copy()
    env["MEDIAREDUCER_CONFIG"] = str(config_path)
    env["MEDIAREDUCER_MODE_OVERRIDE"] = "debug_info"
    proc = subprocess.run(
        ["python3", "-u", str(SCRIPT_PATH)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return False, "Summary refresh failed."
    return True, "Summary refreshed."


def run_summary() -> tuple[bool, str]:
    """Run a quiet Summary (debug_info) in the background to refresh the store
    dashboard stats.

    Same unified-clock rules as run_script (pauses the clock while working, restarts
    from zero when done), but never writes a progress stub or streams the log —
    debug_info leaves lastrun.log and the progress panel intact. Shares the run lock
    so it can never overlap a real run (and vice versa), which lets the dashboard
    ghost the run buttons while it is in flight."""
    global _summary_active, _summary_queued
    with _run_lock:
        if _run_active or _summary_active:
            if _summary_active and not _run_active:
                _summary_queued = True
                return True, "Summary refresh queued."
            return False, "A run or storage refresh is in progress — stats update when it finishes."
        _summary_active = True
    _pause_scheduler_for_run()  # freeze the clock while stats refresh

    def _worker():
        global _summary_active, _summary_queued
        while True:
            try:
                _run_summary_subprocess(CONFIG_PATH)
            except Exception:
                pass
            with _run_lock:
                if _summary_queued and not _run_active:
                    _summary_queued = False
                    continue
                # A queue abandoned because a real run started is dropped, not left
                # latched: the run refreshes the stats the queued summary wanted, and a
                # stale flag would fire one spurious summary after a LATER refresh.
                _summary_queued = False
                _summary_active = False
                break
        _restart_schedule_clock()  # restart the clock from zero
        # The engine drops every code-derived store section the first time it
        # runs after a code change; including this quiet startup Summary, so
        # re-judge the mode against what the store now holds.
        _sync_scheduler_mode_with_db()
        # A config-save reconcile requested while this Summary held the lock
        # would otherwise sit queued until some unrelated sync Summary ran.
        _maybe_launch_queued_reconcile()

    threading.Thread(target=_worker, daemon=True, name="engine-summary").start()
    return True, "Summary started."


def run_summary_sync(timeout: int = 600, *, defer_reconcile: bool = False) -> tuple[bool, str, dict]:
    """Run Summary/debug_info now and return the freshly cached dashboard stats.

    defer_reconcile leaves any queued config-save reconcile queued instead of
    launching it here. A caller that means to start a real run the moment this
    returns must pass it: the launch takes _summary_active synchronously, so the
    run that follows is refused deterministically — and the caller owns firing
    _maybe_launch_queued_reconcile() once it knows no run is starting."""
    global _summary_active, _summary_queued
    with _run_lock:
        if _run_active:
            return False, _RUN_ACTIVE_MESSAGE, {}
        if _summary_active:
            return False, "A background storage refresh is already running. Try again in a moment.", {}
        _summary_active = True
    _pause_scheduler_for_run()

    try:
        ok, msg = _run_summary_subprocess(CONFIG_PATH, timeout=timeout)
        # The subprocess just rewrote the store, so anything this request read
        # from it earlier (the pre-run stats an endpoint shows alongside the
        # refresh) is now the old numbers. Drop the snapshot before reading.
        _invalidate_request_store_cache()
        return ok, msg, library_stats()
    except subprocess.TimeoutExpired:
        return False, "Timed out while refreshing storage stats.", {}
    except Exception as e:
        return False, str(e), {}
    finally:
        with _run_lock:
            _summary_active = False
            # Consume a summary queued while this sync one held the flag —
            # run_summary() answered its caller "queued", so it must actually happen.
            queued = _summary_queued and not _run_active
            _summary_queued = False
        _restart_schedule_clock()
        if queued:
            run_summary()
        if not defer_reconcile:
            _maybe_launch_queued_reconcile()


# ── Config-save reconcile launcher ────────────────────────────────────────────
# A saved filtering / scoring / threshold / collections / favorites change rebuilds
# the marked & eligible queue in place from the stored snapshot (the engine's quiet
# `reconcile` mode) instead of leaving it stale until the next Simulate. It's a
# Summary-class background job: it shares _summary_active so it can never overlap a
# real run or a Summary, and it queues behind one that's already in flight.
_reconcile_queued = None   # (refetch: bool, trigger: str) waiting for the active job
_reconcile_held = None      # a refetch reconcile held because a needed server was down
# Ticks a held reconcile has been passed over for a Redline emergency, and the
# cap on that (~1 hour at the 15-minute cadence); see _scheduled_tick_body.
_reconcile_deferred_ticks = 0
_RECONCILE_DEFER_TICKS = 4

def _run_reconcile_subprocess(config_path: Path, *, refetch: bool,
                              trigger: str, timeout: int = 600) -> bool:
    """Run engine.py in the quiet `reconcile` mode against config_path."""
    env = os.environ.copy()
    env["MEDIAREDUCER_CONFIG"] = str(config_path)
    env["MEDIAREDUCER_MODE_OVERRIDE"] = "reconcile"
    env["MEDIAREDUCER_RECONCILE_REFETCH"] = "1" if refetch else "0"
    env["MEDIAREDUCER_RECONCILE_TRIGGER"] = trigger
    try:
        proc = subprocess.run(
            ["python3", "-u", str(SCRIPT_PATH)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return proc.returncode == 0
    finally:
        _invalidate_request_store_cache()   # it rewrote the queue out of process


def _maybe_launch_queued_reconcile() -> None:
    """Fire a reconcile that was queued while a Summary/run held _summary_active.

    Hand it to run_reconcile unconditionally: if something is still holding the
    lock it re-queues itself there, for whoever finishes next. Dropping it on a
    live run instead lost the request outright — and a run launched moments
    earlier is exactly when this gets called."""
    global _reconcile_queued
    with _run_lock:
        nxt = _reconcile_queued
        _reconcile_queued = None
    if nxt:
        run_reconcile(refetch=nxt[0], trigger=nxt[1])


def _reconcile_worker(refetch: bool, trigger: str) -> None:
    global _summary_active, _summary_queued
    try:
        _run_reconcile_subprocess(CONFIG_PATH, refetch=refetch, trigger=trigger)
    except Exception:
        pass
    with _run_lock:
        _summary_active = False
        # Consume a Summary queued while this reconcile held the flag — the same
        # handshake the other two owners of this flag make. run_summary() already
        # answered its caller "Summary refresh queued", so it has to happen; and a
        # flag left latched fires one spurious refresh after an unrelated later one.
        queued = _summary_queued and not _run_active
        _summary_queued = False
    _restart_schedule_clock()
    if queued:
        run_summary()
    _maybe_launch_queued_reconcile()   # a reconcile requested while this one ran


def run_reconcile(*, refetch: bool, trigger: str) -> None:
    """Launch a background queue reconcile. If a run/Summary is in flight, queue it
    to fire on completion (the refetch flag is sticky — if either the queued or the
    new request needs a server lookup, the coalesced one does)."""
    global _summary_active, _reconcile_queued
    with _run_lock:
        if _run_active or _summary_active:
            prev = _reconcile_queued
            _reconcile_queued = ((prev[0] if prev else False) or refetch, trigger)
            return
        _summary_active = True
    _pause_scheduler_for_run()
    threading.Thread(target=_reconcile_worker, args=(refetch, trigger),
                     daemon=True, name="engine-reconcile").start()


# Plan-affecting settings that reconcile in place on save. PURE keys recompute from
# the snapshot with no server call; REFETCH keys change a protection SOURCE, so the
# reconcile re-fetches the live protected/favorite sets first.
_RECONCILE_PURE_KEYS = (
    "SCORE_BALANCE", "GRACE_PERIOD_DAYS", "MAX_IMDB_RATING", "NEAR_TIE_PTS",
    "MAX_STALENESS_MONTHS", "SKIP_UNPLAYED_MOVIES", "MOVIE_CLEANUP_ENABLED",
    "HEADROOM_GB", "REDLINE_GB", "REDLINE_ONLY_MODE", "MAX_LIBRARY_GB",
)
_RECONCILE_REFETCH_KEYS = (
    "PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS", "PROTECT_JELLYFIN_FAVORITES",
)


def _plan_value_changed(a, b) -> bool:
    """Compare two config values the way the plan cares about — order-insensitive
    for the collection lists, direct for scalars."""
    def _norm(v):
        if isinstance(v, (list, tuple)):
            return sorted(str(x) for x in v)
        return v
    return _norm(a) != _norm(b)


def _reconcile_after_save(old_cfg: dict, new_cfg: dict, *, source: str) -> str:
    """After a plan-affecting save, rebuild the marked & eligible queue in place from
    the stored snapshot (no rescan) instead of leaving it stale until the next
    Simulate. Returns "started", "held_connection", or "none".

    Pure filter/scoring/threshold changes reconcile with no server call. A
    protected-collection or Jellyfin-favorites change re-fetches the live sets; if
    the server it needs is unreachable, the reconcile is HELD and the Connections
    check flags that server (fix it, or disable the server — a disabled server has
    no collections/favorites to honor), with Cleanup/arming blocked meanwhile."""
    global _reconcile_held
    pure = any(_plan_value_changed(old_cfg.get(k), new_cfg.get(k)) for k in _RECONCILE_PURE_KEYS)
    refetch = any(_plan_value_changed(old_cfg.get(k), new_cfg.get(k)) for k in _RECONCILE_REFETCH_KEYS)
    if not (pure or refetch) or not _has_monitored_dirs(new_cfg):
        return "none"
    if refetch and not _refresh_connection_health_cache(new_cfg, probe=True).get("critical_ok", False):
        # Hold it: the Connections check (just refreshed) flags the down server, and
        # _retry_held_reconcile fires this once the connection recovers or the server
        # is disabled. Coalesce with any earlier held request (either wanting a
        # refetch keeps it).
        prev = _reconcile_held
        _reconcile_held = ((prev[0] if prev else False) or refetch, source)
        return "held_connection"
    _reconcile_held = None            # a change that reconciled supersedes a held one
    run_reconcile(refetch=refetch, trigger=source)
    return "started"


def _retry_held_reconcile(cfg: dict | None = None) -> bool:
    """Fire a reconcile that was held because a needed server was down, once the
    connection is healthy again (the user fixed it) or the server was disabled (a
    disabled server has no collections/favorites, so a pure recompute is enough).
    Called from the scheduler tick, so recovery is automatic within ~15 minutes; a
    re-save or a passing Connections check applies it sooner. Returns True if it
    launched a reconcile."""
    global _reconcile_held
    if not _reconcile_held:
        return False
    cfg = cfg or load_config()
    if not _has_monitored_dirs(cfg):
        _reconcile_held = None
        return False
    refetch, trigger = _reconcile_held
    # If both servers are off there's nothing to re-fetch; a pure recompute applies
    # the change; otherwise require the connection to be healthy before re-fetching.
    needs_server = refetch and (cfg.get("USE_PLEX") or cfg.get("USE_JELLYFIN"))
    if needs_server and not _refresh_connection_health_cache(cfg, probe=True).get("critical_ok", False):
        return False
    _reconcile_held = None
    run_reconcile(refetch=needs_server, trigger=trigger)
    return True


# ── IMDb dataset helpers ──────────────────────────────────────────────────────
# Download ceilings for the IMDb dataset (real archive ~10 MB, unpacks to ~25 MB):
# pure guardrails so a wrong URL or crafted archive can't balloon into memory.
_IMDB_GZ_MAX_BYTES = 64 * 1024 * 1024
_IMDB_TSV_MAX_BYTES = 512 * 1024 * 1024


def _download_imdb_gz(url: str) -> bytes:
    """Fetch and decompress the ratings archive with hard size caps on both
    the download and its decompressed output."""
    with urllib.request.urlopen(url, timeout=120) as resp:
        gz_data = resp.read(_IMDB_GZ_MAX_BYTES + 1)
    if len(gz_data) > _IMDB_GZ_MAX_BYTES:
        raise ValueError("IMDb ratings download exceeded the size limit.")
    tsv_data = gzip.GzipFile(fileobj=io.BytesIO(gz_data)).read(_IMDB_TSV_MAX_BYTES + 1)
    if len(tsv_data) > _IMDB_TSV_MAX_BYTES:
        raise ValueError("IMDb ratings archive decompressed beyond the size limit.")
    return tsv_data


def _read_library_snapshot():
    """The full-library snapshot the last completed scan stored under
    the store's library_snapshot — the Filtering & Scoring page's
    table. Returns (payload, None) or (None, "missing"). Reads through the
    memoized cache parse — the arming gate consults this on the status-poll
    path, and composing the whole store (all movie metadata + the snapshot) is
    far too big to redo every 3 seconds."""
    snap = _cache_file_data().get("library_snapshot")
    if not isinstance(snap, dict):
        return None, "missing"
    return snap, None


def _simulate_evidence(cfg: dict) -> bool:
    """Proof that a Simulate (or any completed scan) has already seen THIS
    library: the snapshot every completed scan writes, stamped with the paths
    it scanned. Used by the arming gate when space limits are satisfied — the
    eligible queue can legitimately be empty then, so the plan stamp alone
    can't prove a Simulate happened. A snapshot built from different monitored
    paths is not evidence for the current ones."""
    snap, err = _read_library_snapshot()
    if err is not None or not snap.get("built_at"):
        return False
    dirs = snap.get("monitor_dirs")
    return (isinstance(dirs, list)
            and sorted(str(d) for d in dirs) == _normalized_monitor_dirs(cfg))


# ── "A full scan is required every 48 hours" ──────────────────────────────────
# The store is PRESERVED across a restart, so an unchanged, recent plan stays
# usable with no re-scan. But a full library scan (a Simulate, or the automatic
# daily Cleanup / paused daily maintenance Simulate, which rebuild the whole
# snapshot) must have run within the last two days; otherwise the plan is treated
# as too old to trust and Cleanup + arming ghost until a fresh Simulate. A daily
# full scan normally keeps this satisfied with a full day of slack (so moving the
# daily run time later never trips it); if scans stop for two days (e.g. the APIs
# are unreachable) the plan ages out and the user runs Simulate manually.
# Evaluated live off the snapshot's built_at; no persistent flag: a completed
# scan refreshes built_at and clears it automatically.
_FULL_SCAN_MAX_AGE_SECONDS = 48 * 3600

# Mirrors engine.REPORT_ITEMS_CAP: the longest per-movie list a run report
# carries. A marked_items list SHORTER than this is complete, so everything
# else in the marked queue genuinely predates the run.
_MARKED_ITEMS_CAP = 200

_SCAN_OVERDUE_MESSAGE = (
    "It's been over two days since MediaReducer last scanned your whole library, so "
    "the saved deletion plan may be out of date — run Simulate to refresh it "
    "before running a Cleanup or enabling automatic mode. (A daily scan normally "
    "does this for you.)")


def _full_scan_overdue() -> bool:
    """True when a full library scan exists but completed over
    _FULL_SCAN_MAX_AGE_SECONDS ago. False when there's no snapshot at all — the
    first-time 'run a Simulate' case is handled by the plan/evidence logic with
    its own message. Cheap: reads the snapshot's built_at through the memoized
    store parse."""
    snap, err = _read_library_snapshot()
    if err is not None:
        return False
    built_at = snap.get("built_at")
    if not isinstance(built_at, (int, float)):
        return False
    return (time.time() - float(built_at)) > _FULL_SCAN_MAX_AGE_SECONDS


def _library_db_fresh(cfg: dict | None = None) -> bool:
    """The 'up-to-date database' Monitor Only requires: a library snapshot of
    the CURRENT monitored paths (simulate evidence), built within the full-scan
    window. Missing (never scanned, or wiped by a code/schema-change store
    rebuild), built for different paths, or overdue all read as NOT fresh."""
    cfg = cfg or load_config()
    return _simulate_evidence(cfg) and not _full_scan_overdue()


def _last_scan_failed() -> bool:
    """True when the most recent run ended in failure.

    Monitor Only unlocks on a COMPLETED scan, so a run that aborts leaves the
    scheduler resting at Paused — correctly, but indistinguishably from never
    having run one. Without this the UI keeps saying "Run Simulate first" to
    someone who just did, and the mode looks broken rather than blocked. A
    first-run abort is easy to hit: the IMDb dataset has to download before the
    first scan can score anything."""
    try:
        return str((json.loads(progress_path().read_text(encoding="utf-8"))
                    or {}).get("status") or "") == "error"
    except Exception:
        return False


def _scan_lock_reason(cfg: dict) -> str:
    """Why Monitor Only is still locked, in the user's terms — '' when it isn't.
    Shared by the config page's mode card and the dashboard's paused line so the
    two can never explain the same state differently."""
    if not ((cfg.get("USE_PLEX") or cfg.get("USE_JELLYFIN")) and _has_monitored_dirs(cfg)):
        return "Connect a media server and add a monitored path first."
    if not _any_space_trigger_armed(cfg):
        return ("Arm a space threshold first — a Headroom target, Redline floor, "
                "or Library Size Cap gives Monitor Only something to preview.")
    if _library_db_fresh(cfg):
        return ""
    if _last_scan_failed():
        return ("The last run didn't finish — open the log to see why, fix it, "
                "then run Simulate again.")
    if _simulate_evidence(cfg) and _full_scan_overdue():
        return ("The last library scan is over two days old — run Simulate "
                "again to refresh it.")
    return "Run Simulate first — this enables automatically once the scan finishes."


def _todays_daily_run_epoch(cfg: dict | None = None):
    """Epoch of today's configured daily run time (DAILY_RUN_TIME) in the app's
    local/configured timezone. An unparseable DAILY_RUN_TIME falls back to
    00:00 via _daily_run_time; None only when mktime itself fails."""
    try:
        hh, mm = (int(x) for x in _daily_run_time(cfg).split(":"))
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0,
                            now.tm_wday, now.tm_yday, -1))
    except (ValueError, AttributeError, OverflowError):
        return None


def _daily_maintenance_scan_due(cfg: dict) -> bool:
    """True when the once-a-day maintenance Simulate is owed: the configured
    daily run time has passed and no full library scan has completed since then
    (the snapshot's built_at predates today's run time). Applies in Monitor Only
    on every daily slot, and in Automatic Cleanup whenever the daily slot passes
    with nothing to delete — either way it keeps the store current (refreshing
    the plan and the 'full scan within 48h' clock) and produces the daily-summary
    notification. A fresh, never-scanned install keeps its manual-Simulate
    onboarding instead of auto-scanning — but an install that HAS scanned before
    and lost its snapshot to a code/schema-change store rebuild is
    owed a rebuild, not onboarding (the snapshot_ever_built meta marker survives
    those rebuilds and tells the two apart)."""
    run_epoch = _todays_daily_run_epoch(cfg)
    if run_epoch is None or time.time() < run_epoch:
        return False   # before today's scheduled time
    snap, err = _read_library_snapshot()
    if err is not None:
        # No snapshot in the store: never-scanned onboarding stays manual;
        # a rebuild-wiped store re-scans on the daily slot.
        return _snapshot_ever_built()
    built_at = snap.get("built_at")
    if not isinstance(built_at, (int, float)):
        return True
    return float(built_at) < run_epoch   # no full scan since today's run time


def _snapshot_ever_built() -> bool:
    """True when this install has completed at least one library scan — read
    from the snapshot_ever_built meta marker, which survives the code-change and
    schema-change store rebuilds (only a full store reset clears it)."""
    try:
        p = db_path()
        if not p.exists():
            return False   # never-scanned install; don't create the store on a read
        with db.connect(p) as conn:
            return bool(db.get_meta(conn, "snapshot_ever_built"))
    except Exception:
        return False


# ── Disk / status helpers ─────────────────────────────────────────────────────

def disk_stats() -> dict | None:
    try:
        u = shutil.disk_usage(FILESYSTEM_CHECK_PATH)
        used_gb  = round(u.used  / 1e9, 1)
        total_gb = round(u.total / 1e9, 1)
        free_gb  = round(u.free  / 1e9, 1)
        pct_used = round(used_gb / total_gb * 100, 1) if total_gb else 0
        return {"used_gb": used_gb, "total_gb": total_gb,
                "free_gb": free_gb, "pct_used": pct_used}
    except Exception:
        return None


def _coerce_float(value) -> tuple[float | None, bool]:
    """Return (number, ok)."""
    if value is None:
        return None, False
    try:
        return float(value), True
    except (TypeError, ValueError):
        return None, False



def _is_cleanup_mode(mode: str | None) -> bool:
    return mode == "headroom"


def _ui_run_mode(mode: str | None) -> str:
    """Snap any stored value to the GUI's three modes: off (fully paused) /
    paused (Monitor Only) / headroom (Automatic Cleanup)."""
    if _is_cleanup_mode(mode):
        return "headroom"
    return "off" if mode == "off" else "paused"


def _threshold_gb_or_none(value):
    """A positive GB threshold as a float, or None when unset/invalid/zero."""
    if value is None or value == "":
        return None
    num, ok = _coerce_float(value)
    return num if ok and num is not None and num > 0 else None


def _space_threshold_state(cfg: dict | None = None, disk: dict | None = None,
                           library_gb=None, *, candidate_cfg: bool = False) -> dict:
    """Validate Space Thresholds for Simulate and cleanup runs.

    Simulate is deliberately allowed when HEADROOM_GB is above the safety percentage,
    or when the Library Size Cap would delete more than that percentage of the library,
    so users can preview the deletion order. Automatic Cleanup is blocked by either condition. Other
    malformed threshold values block even Simulate — they make the run ambiguous."""
    cfg = cfg or load_config()
    disk = disk if disk is not None else disk_stats()
    if library_gb is None:
        library_gb = library_stats().get("library_gb")

    hard_errors: list[str] = []
    safety_errors: list[str] = []

    headroom_gb, headroom_ok = _coerce_float(cfg.get("HEADROOM_GB"))
    if not headroom_ok or headroom_gb is None or headroom_gb < 0:
        hard_errors.append("Headroom must be zero or greater.")
        headroom_ok = False

    max_pct, max_pct_ok = _coerce_float(cfg.get("MAX_HEADROOM_PCT", 15))
    if not max_pct_ok or max_pct is None or max_pct <= 0:
        hard_errors.append("Headroom safety percentage must be greater than zero.")
        max_pct_ok = False

    redline_gb = None
    redline_ok = True
    if cfg.get("REDLINE_GB") is not None:
        redline_gb, redline_ok = _coerce_float(cfg.get("REDLINE_GB"))
        if not redline_ok or redline_gb is None or redline_gb <= 0:
            hard_errors.append("Redline must be greater than zero, or disabled.")
            redline_ok = False
        elif headroom_ok and headroom_gb > 0 and redline_gb >= headroom_gb:
            # Only under an ARMED Headroom (>= 1) must Redline sit strictly
            # below it — with the trigger off there is no ceiling. Keyed to the
            # VALUE, not the REDLINE_ONLY_MODE flag (the flag is derived from
            # the value at load). Matches the save validator, the file
            # validator, the engine, and the client-side check.
            hard_errors.append("Redline must be lower than Headroom — or "
                               "untick Headroom.")
            redline_ok = False

    cap_gb = None
    cap_configured = False
    cap_value = cfg.get("MAX_LIBRARY_GB")
    if cap_value is not None:
        cap_gb, cap_ok = _coerce_float(cap_value)
        if not cap_ok or cap_gb is None or cap_gb <= 0:
            hard_errors.append("Library Size Cap must be greater than zero, or disabled.")
        else:
            cap_configured = True

    total_gb = None
    try:
        total_gb = float((disk or {}).get("total_gb") or 0)
    except (TypeError, ValueError):
        total_gb = None

    limit_gb = None
    safety_ok = True
    safety_message = ""
    if headroom_ok and max_pct_ok and total_gb and total_gb > 0:
        limit_gb = round(total_gb * max_pct / 100, 1)
        # The safety cap bounds the free-space floor the system maintains: the
        # Headroom target normally, the Redline floor in redline-only mode.
        _maintained_gb = headroom_gb if headroom_gb > 0 else (
            redline_gb if (redline_ok and redline_gb) else None)
        safety_ok = _maintained_gb is None or _maintained_gb <= limit_gb
        if not safety_ok:
            safety_message = ("Redline floor is over the safety percentage."
                              if headroom_gb == 0 else
                              "Headroom target is over the safety percentage.")
            safety_errors.append(safety_message)

    # The same safety percentage caps how much a Library Size Cap may delete in a cleanup
    # run: the cap can't sit below (library - max_pct% of library), so a cleanup pass
    # removes at most max_pct% of the library. Simulate is exempt (preview only).
    library_gb_val = None
    try:
        library_gb_val = float(library_gb) if library_gb is not None else None
    except (TypeError, ValueError):
        library_gb_val = None
    cap_floor_gb = None
    cap_safety_ok = True
    cap_safety_message = ""
    if cap_configured and max_pct_ok and library_gb_val and library_gb_val > 0:
        cap_floor_gb = round(library_gb_val * (100 - max_pct) / 100, 1)
        cap_safety_ok = cap_gb >= cap_floor_gb
        if not cap_safety_ok:
            cap_safety_message = "Library Size Cap would delete more than the safety percentage of the library."
            safety_errors.append(cap_safety_message)

    # A Cleanup needs at least one active space target. Headroom 0 + no Redline + no
    # cap means nothing to enforce, so block Automatic Cleanup (Simulate can preview an empty plan).
    if (headroom_ok and headroom_gb == 0
            and cfg.get("REDLINE_GB") is None
            and cfg.get("MAX_LIBRARY_GB") is None):
        safety_errors.append("Set a Headroom target, Redline, or Library Size Cap to enable Automatic Cleanup.")

    # ...and something it is allowed to delete. With both per-type switches off
    # nothing is ever eligible, so an armed Automatic Cleanup would wake on
    # schedule, scan, and find its own hands tied — a mode that reads as
    # deleting while deleting nothing.
    #
    # This is not a space rule, and it lives here anyway: ok_for_cleanup is the
    # single gate every armed-cleanup path already consults — the mode radio,
    # the save that arms it, the manual Cleanup endpoint and the scheduler's
    # own next-run check. A parallel gate would have to be added to each, and
    # the one that got missed would be the one that mattered.
    if not (cfg.get("MOVIE_CLEANUP_ENABLED") or cfg.get("TV_CLEANUP_ENABLED")):
        safety_errors.append("Allow movie or TV show cleanup in Filtering & Scoring "
                             "to enable Automatic Cleanup.")

    # Dedupe (order-preserving): a message can be reached by multiple paths.
    def _dedupe(items: list[str]) -> list[str]:
        seen = set()
        out = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    simulate_errors = _dedupe(hard_errors)
    headroom_errors = _dedupe(hard_errors + safety_errors)

    # User-facing deletion (arming Automatic Cleanup, the manual Cleanup button) always
    # requires that a Simulate has seen the library under the CURRENT config:
    #   • limits breached (or redline-only, which deletes the moment its floor is
    #     hit): a deletion plan stamped with exactly these thresholds; moving
    #     Headroom/Redline/Cap ghosts Automatic Cleanup until a Simulate refreshes it;
    #   • limits satisfied: proof a Simulate has run at least once; the plan
    #     stamp when there is one, else the library snapshot every completed
    #     scan writes (within limits the eligible queue can legitimately be
    #     empty, so the snapshot is the evidence).
    # Deliberately NOT part of ok_for_cleanup; the scheduler recomputes its own
    # plan every run and must not auto-pause an armed Automatic Cleanup over this.
    simulate_required = False
    simulate_first_time = False
    # A full library scan must have run within the last two days (see
    # _full_scan_overdue); an older plan is ghosted no matter what its stamp
    # says. The snapshot is kept for display; only Cleanup + arming lock until a
    # fresh scan. Skipped when there's no snapshot yet (the plan/evidence logic
    # below owns the first-time "run a Simulate" case).
    scan_overdue = _full_scan_overdue()
    if not headroom_errors:
        if scan_overdue:
            simulate_required = True
        else:
            try:
                _plan_current = _pending_plan_current(cfg, use_saved_file=not candidate_cfg)
                if _redline_only_mode_cfg(cfg):
                    simulate_required = not _plan_current
                elif _deletion_limits_exceeded(cfg, disk, library_gb_val):
                    simulate_required = not _plan_current
                else:
                    simulate_required = not (_plan_current or _simulate_evidence(cfg))
                    simulate_first_time = simulate_required
            except Exception:
                simulate_required = False

    if not simulate_required:
        simulate_required_message = ""
    elif scan_overdue:
        simulate_required_message = _SCAN_OVERDUE_MESSAGE
    elif _redline_only_mode_cfg(cfg):
        simulate_required_message = ("Run Simulate to build the Redline deletion-order "
                                     "preview before enabling Automatic Cleanup.")
    elif simulate_first_time:
        simulate_required_message = ("Run Simulate once so MediaReducer has scanned "
                                     "your library before enabling automatic mode.")
    elif _pending_raw():
        # A plan exists but its stamp no longer matches; the settings moved.
        simulate_required_message = ("Settings changed since the last Simulate — "
                                     "run it again to rebuild the deletion plan.")
    else:
        simulate_required_message = SIMULATE_REQUIRED_MESSAGE

    return {
        "ok_for_simulate": not simulate_errors,
        "ok_for_cleanup": not headroom_errors,
        "has_library_cap": cap_configured,
        # Automatic Cleanup blocked specifically because a target exceeds the safety percentage
        # (headroom over the cap, or a cap below the floor); the dashboard's breach
        # note words itself around this.
        "safety_blocked": not (safety_ok and cap_safety_ok),
        "simulate_required": simulate_required,
        "simulate_required_message": simulate_required_message,
        "simulate_tooltip": " ".join(simulate_errors),
        "cleanup_tooltip": " ".join(headroom_errors),
    }


def _find_appdata_file(base: str | Path, *names: str) -> Path | None:
    """Locate a config marker under a mounted appdata directory."""
    base = Path(base)
    for d in (base, base / "config"):
        for name in names:
            p = d / name
            try:
                if p.exists():
                    return p
            except OSError:
                pass
    try:
        if base.exists():
            for root, dirs, files in os.walk(base):
                rel = Path(root).relative_to(base)
                if len(rel.parts) >= 4:
                    dirs[:] = []
                    continue
                for name in names:
                    if name in files:
                        return Path(root) / name
    except OSError:
        pass
    return None


def _configured_connection_values(cfg: dict | None = None) -> dict:
    """Return only the saved connection values from config.json/form data."""
    cfg = cfg or load_config()
    return {
        "tautulli_url": str(cfg.get("TAUTULLI_URL") or "").strip(),
        "tautulli_key": str(cfg.get("TAUTULLI_API_KEY") or "").strip(),
        "plex_url": str(cfg.get("PLEX_URL") or "").strip(),
        "plex_token": str(cfg.get("PLEX_TOKEN") or "").strip(),
        "radarr_url": str(cfg.get("RADARR_URL") or "").strip(),
        "radarr_key": str(cfg.get("RADARR_API_KEY") or "").strip(),
        "sonarr_url": str(cfg.get("SONARR_URL") or "").strip(),
        "sonarr_key": str(cfg.get("SONARR_API_KEY") or "").strip(),
        "jellyfin_url": str(cfg.get("JELLYFIN_URL") or "").strip(),
        "jellyfin_key": str(cfg.get("JELLYFIN_API_KEY") or "").strip(),
    }


def _effective_connection_values(cfg: dict | None = None) -> dict:
    """Connection values the way a connection should use them.

    Saved URLs win. A blank URL falls back to the detected default only when the
    service's credential is present — the key is the on/off switch, so a service with
    no credential keeps its blank URL and stays off (no probe, no error). The form
    never shows these resolved values; it renders the saved (possibly blank) fields via
    _connection_field_values."""
    cfg = cfg or load_config()
    conn = _configured_connection_values(cfg)
    defaults = None
    stored = cfg.get("_SERVICE_URL_DEFAULTS") or {}
    for url_field, key_field in _URL_KEY_FIELD_PAIRS.items():
        url_key = CONNECTION_FORM_FIELD_MAP[url_field]
        cred_key = CONNECTION_FORM_FIELD_MAP[key_field]
        if conn[url_key] or not conn[cred_key]:
            continue
        if defaults is None:
            defaults = _connection_url_defaults(cfg)
        conn[url_key] = defaults.get(url_field) or str(stored.get(url_field) or "").strip()
    return conn


def _autodetected_connection_values(cfg: dict | None = None) -> dict:
    """Best-effort one-shot appdata detection for the Config Auto Detect button."""
    import xml.etree.ElementTree as ET
    detected = {
        "tautulli_url": "",
        "tautulli_key": "",
        "plex_url": "",
        "plex_token": "",
        "radarr_url": "",
        "radarr_key": "",
        "sonarr_url": "",
        "sonarr_key": "",
        "jellyfin_url": "",
        "jellyfin_key": "",
    }

    # PORTS ARE NEVER READ FROM APPDATA. Every URL default uses the service's
    # documented port (_SERVICE_DEFAULT_PORTS), so what the form suggests is
    # predictable and matches each project's own docs. A detected port was a
    # second source of truth that only ever mattered for a non-standard setup —
    # exactly the setup that is going to type its own URL anyway.
    #
    # Only the PLEX HOST is taken from appdata, and only because Plex is the one
    # service that genuinely lives on another box: everything else runs
    # alongside MediaReducer, so the address the browser reached it on is right.
    tautulli_ini = _find_appdata_file(TAUTULLI_APPDATA_DIR, "config.ini")
    if tautulli_ini:
        try:
            parser = configparser.RawConfigParser(strict=False)
            parser.read(tautulli_ini, encoding="utf-8")
            # Keys first and unconditionally: they are the point of this whole
            # mechanism, and reading them inside the host check meant a config
            # with an api_key but no pms_ip yielded nothing at all.
            detected["tautulli_key"] = parser.get("General", "api_key", fallback="") or ""
            detected["plex_token"] = parser.get("PMS", "pms_token", fallback="") or ""
            host = parser.get("PMS", "pms_ip", fallback=None)
            if host:
                detected["host"] = host
                for field, key in (("TAUTULLI_URL", "tautulli_url"),
                                   ("PLEX_URL", "plex_url")):
                    detected[key] = f"http://{host}:{_SERVICE_DEFAULT_PORTS[field]}"
        except Exception:
            pass

    radarr_xml = _find_appdata_file(RADARR_APPDATA_DIR, "config.xml")
    if radarr_xml:
        try:
            key = ET.parse(radarr_xml).getroot().findtext("ApiKey")
            if key:
                detected["radarr_key"] = key
                if detected.get("host"):
                    detected["radarr_url"] = (
                        f"http://{detected['host']}:{_SERVICE_DEFAULT_PORTS['RADARR_URL']}")
        except Exception:
            pass

    # Same config.xml layout as Radarr — both are Servarr apps.
    sonarr_xml = _find_appdata_file(SONARR_APPDATA_DIR, "config.xml")
    if sonarr_xml:
        try:
            key = ET.parse(sonarr_xml).getroot().findtext("ApiKey")
            if key:
                detected["sonarr_key"] = key
                if detected.get("host"):
                    detected["sonarr_url"] = (
                        f"http://{detected['host']}:{_SERVICE_DEFAULT_PORTS['SONARR_URL']}")
        except Exception:
            pass

    return detected



CONNECTION_FORM_FIELD_MAP = {
    "TAUTULLI_URL": "tautulli_url",
    "TAUTULLI_API_KEY": "tautulli_key",
    "PLEX_URL": "plex_url",
    "PLEX_TOKEN": "plex_token",
    "RADARR_URL": "radarr_url",
    "RADARR_API_KEY": "radarr_key",
    "SONARR_URL": "sonarr_url",
    "SONARR_API_KEY": "sonarr_key",
    "JELLYFIN_URL": "jellyfin_url",
    "JELLYFIN_API_KEY": "jellyfin_key",
}

# Each service URL is paired with the credential that signals the user wants it:
# a blank URL is filled from the detected default only when its key is present.
_URL_KEY_FIELD_PAIRS = {
    "TAUTULLI_URL": "TAUTULLI_API_KEY",
    "PLEX_URL": "PLEX_TOKEN",
    "RADARR_URL": "RADARR_API_KEY",
    "SONARR_URL": "SONARR_API_KEY",
    "JELLYFIN_URL": "JELLYFIN_API_KEY",
}

# Standard LAN ports, used to show a generic placeholder when appdata detection
# can't supply a real host. None of these services serve TLS by default.
_GENERIC_URL_PLACEHOLDERS = {
    "TAUTULLI_URL": "http://SERVER-IP:8181",
    "PLEX_URL": "http://SERVER-IP:32400",
    "RADARR_URL": "http://SERVER-IP:7878",
    "SONARR_URL": "http://SERVER-IP:8989",
    "JELLYFIN_URL": "http://SERVER-IP:8096",
}


def _normalize_service_url(value) -> str:
    """Trim a service URL and assume http:// when no scheme is given — none of
    the supported services serve TLS by default, so a bare host:port is the
    common case."""
    raw = str(value or "").strip()
    if raw and "://" not in raw:
        raw = "http://" + raw
    return raw


_SERVICE_DEFAULT_PORTS = {
    "TAUTULLI_URL": 8181,
    "PLEX_URL": 32400,
    "RADARR_URL": 7878,
    "SONARR_URL": 8989,
    "JELLYFIN_URL": 8096,
}


_LAST_REQUEST_LAN_HOST = ""


def _request_lan_host() -> str:
    """The host the browser used to reach MediaReducer — the natural 'server address'
    for same-box service defaults. Blank when loopback: inside a container 127.0.0.1 is
    the container itself, never a sibling.

    The connection-health probe runs in a background thread (startup, Check for Errors,
    after a save) with NO request context, so the last usable host a real request used
    is remembered and reused there. Otherwise the threaded probe resolves URL defaults
    to a different address than the Config page shows and can report a healthy service
    as down (e.g. locking Radarr optional cleanup)."""
    global _LAST_REQUEST_LAN_HOST
    try:
        host = urlparse("//" + (request.host or "")).hostname or ""
    except Exception:
        return _LAST_REQUEST_LAN_HOST   # no request context; reuse the last real one
    if not host or host == "localhost" or host.startswith("127.") or host == "::1":
        return _LAST_REQUEST_LAN_HOST
    _LAST_REQUEST_LAN_HOST = host
    return host


def _usable_detected_host(url: str) -> str:
    """Hostname from a detected URL, unless loopback/unspecified — appdata configs
    often say 127.0.0.1 for same-box services, unreachable from this container."""
    try:
        h = urlparse(url).hostname or ""
    except Exception:
        return ""
    if not h or h == "localhost" or h.startswith("127.") or h in ("0.0.0.0", "::1", "::"):
        return ""
    return h


def _connection_url_defaults(cfg: dict | None = None) -> dict:
    """Best-known default URL per service.

    Host comes from the address the browser used to reach MediaReducer
    (everything but Plex runs alongside it); Plex prefers the host Tautulli's
    config points at, since Plex can live on another box. The PORT is always the
    service's documented one — appdata is never consulted for it, so the
    suggested URL is predictable and matches each project's own docs. Blank when
    no host is known."""
    det = _autodetected_connection_values(cfg)
    req_host = _request_lan_host()
    out = {}
    for field, det_key in (("TAUTULLI_URL", "tautulli_url"), ("PLEX_URL", "plex_url"),
                           ("RADARR_URL", "radarr_url"), ("SONARR_URL", "sonarr_url"),
                           ("JELLYFIN_URL", "jellyfin_url")):
        det_host = _usable_detected_host(det.get(det_key) or "")
        # Always the documented port; never one read out of appdata.
        port = _SERVICE_DEFAULT_PORTS[field]
        host = (det_host or req_host) if field == "PLEX_URL" else (req_host or det_host)
        out[field] = f"http://{host}:{port}" if host else ""
    return out


def _normalize_saved_service_urls(cfg: dict) -> None:
    """Scheme-normalize the URL fields for saving. Blank stays blank — the default
    address is applied at connection time by _effective_connection_values, never
    written into the user's config."""
    for url_field in _URL_KEY_FIELD_PAIRS:
        cfg[url_field] = _normalize_service_url(cfg.get(url_field))


def _connection_field_values(cfg: dict | None = None) -> dict:
    """The saved connection form values only — never the resolved defaults,
    so a blank URL field stays blank in the form (its placeholder shows the
    address that will be used)."""
    conn = _configured_connection_values(cfg)
    return {field: str(conn.get(key) or "") for field, key in CONNECTION_FORM_FIELD_MAP.items()}


def _new_tv_row(*, title, year, path, seasons, added_at, status, source_id=None,
                jf_source_id=None, imdb_id=None) -> dict:
    """One TV snapshot row from whatever media server supplied the facts. The
    path is in the SERVER's filesystem namespace; _resolve_tv_scope replaces
    it with the /library-resolved path (or None) before the row is stored."""
    seasons = sorted((s for s in seasons if s.get("eps")), key=lambda x: x["n"])
    size = sum(int(s.get("size_bytes") or 0) for s in seasons)
    return {
        "media_type": "tv",
        "path": str(path or "") or None,
        "tv_episodes": sum(int(s.get("eps") or 0) for s in seasons),
        "tv_episodes_watched": 0,
        "tv_seasons": seasons,
        "title": str(title or ""),
        "year": year,
        "size_gb": round(size / 1e9, 2),
        "size_bytes": size,
        "added_at": added_at or 0,
        "tv_status": status,
        "source_id": source_id,
        "jf_source_id": jf_source_id,
        # The media server knows the series' IMDb id; rating/votes are filled
        # from the on-disk ratings dataset by _annotate_tv_imdb so seasons
        # score on the same IMDb axis movies do.
        "imdb_id": imdb_id,
        "plays": 0, "users": 0, "rating": None, "votes": 0, "last_played": 0,
    }


def _new_season_bucket(n: int) -> dict:
    # added_at = the NEWEST episode file's added date: a season that just
    # received content reads fresh (kept), and only a season whose files have
    # all sat untouched ages toward stale.
    return {"n": n, "eps": 0, "size_bytes": 0, "added_at": 0,
            "eps_watched": 0, "last_played": 0, "plays": 0, "users": 0}


def _jellyfin_series_inventory(url: str, key: str) -> list | None:
    """Jellyfin's TV libraries as snapshot rows (media_type 'tv'), or None
    when the queries fail. Read-only inventory: title, year, per-season
    episode counts and sizes on disk, added date, continuing/ended status and
    the IMDb id — the facts the Filtering page shows and season scoring
    builds on. Two admin queries: every series, then every episode with its
    media sources (sizes) and season number."""
    try:
        series = (_jellyfin_get(url, key, "/Items", {
            "IncludeItemTypes": "Series", "Recursive": "true",
            "Fields": "Path,ProviderIds,DateCreated,Status,ProductionYear",
        }, timeout=30) or {}).get("Items")
        episodes = (_jellyfin_get(url, key, "/Items", {
            "IncludeItemTypes": "Episode", "Recursive": "true",
            "Fields": "MediaSources,ParentIndexNumber,SeriesId,DateCreated",
        }, timeout=60) or {}).get("Items")
        if not isinstance(series, list) or not isinstance(episodes, list):
            return None
    except Exception:
        return None
    # The parse shares the fetch's None-on-failure contract: an item shaped
    # unlike Jellyfin's own wire format must read as "no answer", not crash
    # the refresh thread.
    try:
        return _jellyfin_series_rows_from(series, episodes)
    except Exception:
        return None


def _jellyfin_series_rows_from(series: list, episodes: list) -> list | None:
    per: dict[str, dict[int, dict]] = {}
    for it in episodes:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("SeriesId") or "")
        try:
            n = int(it.get("ParentIndexNumber"))
        except (TypeError, ValueError):
            continue
        size = 0
        for ms in it.get("MediaSources") or []:
            if isinstance(ms, dict):
                try:
                    size += int(ms.get("Size") or 0)
                except (TypeError, ValueError):
                    pass
        b = per.setdefault(sid, {}).setdefault(n, _new_season_bucket(n))
        b["eps"] += 1
        b["size_bytes"] += size
        raw_added = str(it.get("DateCreated") or "")
        if raw_added:
            try:
                b["added_at"] = max(b["added_at"], int(
                    datetime.fromisoformat(raw_added.replace("Z", "+00:00")).timestamp()))
            except ValueError:
                pass
    rows = []
    for s in series:
        if not isinstance(s, dict) or not s.get("Id") or not s.get("Name"):
            continue
        seasons = list((per.get(str(s["Id"])) or {}).values())
        if not any(x["size_bytes"] > 0 for x in seasons):
            continue   # nothing of this series on disk
        added = 0
        raw_added = str(s.get("DateCreated") or "")
        if raw_added:
            try:
                added = int(datetime.fromisoformat(raw_added.replace("Z", "+00:00")).timestamp())
            except ValueError:
                added = 0
        providers = s.get("ProviderIds") or {}
        imdb = next((str(v) for k, v in providers.items()
                     if str(k).lower() == "imdb" and v), None)
        rows.append(_new_tv_row(
            title=s["Name"], year=s.get("ProductionYear"), path=s.get("Path"),
            seasons=seasons, added_at=added,
            status=(str(s.get("Status") or "").lower() or None),
            jf_source_id=f"jellyfin:{s['Id']}", imdb_id=imdb))
    # Series listed but EVERY one filtered for zero on-disk bytes is a sizes
    # blip (a mid-scan server answering without MediaSources), not an emptied
    # library — a real answer with no rows would wipe the stored inventory.
    if series and not rows:
        return None
    return rows


def _plex_series_inventory(conn: dict) -> list | None:
    """Plex's TV libraries as snapshot rows, or None when the queries fail.
    Per show section: every show (title, year, added date, folder location,
    IMDb guid), then every EPISODE with its media parts, so seasons carry
    real on-disk sizes. Plex does not state continuing/ended, so tv_status
    stays None — the season plan shields the latest season of any series not
    known to be ended."""
    url, token = conn.get("plex_url"), conn.get("plex_token")
    if not (url and token):
        return None
    try:
        secs = _plex_get(url, token, "/library/sections", timeout=10) or {}
        rows_by_rk: dict[str, dict] = {}
        for sec in (secs.get("MediaContainer") or {}).get("Directory") or []:
            if not isinstance(sec, dict) or sec.get("type") != "show":
                continue
            shows = _plex_get(url, token,
                              f"/library/sections/{sec.get('key')}/all?type=2&includeGuids=1",
                              timeout=30) or {}
            for sh in (shows.get("MediaContainer") or {}).get("Metadata") or []:
                if not isinstance(sh, dict) or not sh.get("ratingKey") or not sh.get("title"):
                    continue
                imdb = next((str(g.get("id"))[7:] for g in _as_list(sh.get("Guid"))
                             if isinstance(g, dict)
                             and str(g.get("id") or "").startswith("imdb://")), None)
                loc = next((str(l.get("path")) for l in _as_list(sh.get("Location"))
                            if isinstance(l, dict) and l.get("path")), None)
                try:
                    added = int(sh.get("addedAt") or 0)
                except (TypeError, ValueError):
                    added = 0
                rows_by_rk[str(sh["ratingKey"])] = {
                    "title": sh["title"], "year": sh.get("year"),
                    "path": loc, "added_at": added, "imdb": imdb,
                    "rk": str(sh["ratingKey"]), "seasons": {},
                }
            episodes = _plex_get(url, token,
                                 f"/library/sections/{sec.get('key')}/all?type=4",
                                 timeout=60) or {}
            for ep in (episodes.get("MediaContainer") or {}).get("Metadata") or []:
                if not isinstance(ep, dict):
                    continue
                show = rows_by_rk.get(str(ep.get("grandparentRatingKey") or ""))
                if show is None:
                    continue
                try:
                    n = int(ep.get("parentIndex"))
                except (TypeError, ValueError):
                    continue
                size = 0
                first_file = None
                for m in _as_list(ep.get("Media")):
                    for p in _as_list((m or {}).get("Part")):
                        if isinstance(p, dict):
                            try:
                                size += int(p.get("size") or 0)
                            except (TypeError, ValueError):
                                pass
                            first_file = first_file or str(p.get("file") or "") or None
                b = show["seasons"].setdefault(n, _new_season_bucket(n))
                b["eps"] += 1
                b["size_bytes"] += size
                try:
                    b["added_at"] = max(b["added_at"], int(ep.get("addedAt") or 0))
                except (TypeError, ValueError):
                    pass
                # A show with no Location still needs a folder: derive the
                # series dir from an episode file (file → season dir → series).
                if not show["path"] and first_file:
                    parent = PurePosixPath(first_file).parent
                    show["path"] = str(parent.parent if parent.parent.name else parent)
    except Exception:
        return None
    rows = []
    for show in rows_by_rk.values():
        seasons = list(show["seasons"].values())
        if not any(x["size_bytes"] > 0 for x in seasons):
            continue
        rows.append(_new_tv_row(
            title=show["title"], year=show["year"], path=show["path"],
            seasons=seasons, added_at=show["added_at"], status=None,
            source_id=f"plex:{show['rk']}", imdb_id=show["imdb"]))
    # Same sizes-blip rule as the Jellyfin side: shows listed but none with a
    # byte on disk reads as "no answer", never as an emptied library.
    if rows_by_rk and not rows:
        return None
    return rows


def _merge_tv_sources(jf_rows: list | None, px_rows: list | None) -> list:
    """One inventory from up to two servers watching the same files, joined by
    normalized title AND year. The year matters: same-title distinct shows are
    real (Doctor Who 1963/2005), and a title-only join would graft one show's
    Jellyfin identity onto the other's folder — the deletion pass would then
    list show A's files under show B's path. Sizes measure the same files, so
    a disagreement keeps the larger measurement; identity from both sides is
    kept (source_id = Plex, jf_source_id = Jellyfin) so the deletion pass can
    ask either server for the season's file list."""
    def _key(r):
        return (_norm_series_title(r["title"]), r.get("year") or None)

    by_title: dict[tuple, dict] = {}
    for r in (px_rows or []):
        k = _key(r)
        # Two same-title same-year shows on ONE server cannot be told apart
        # by this join — keep both as separate rows rather than dropping one.
        while k in by_title:
            k = (k[0], k[1], id(r))
        by_title[k] = r
    for r in (jf_rows or []):
        k = _key(r)
        base = by_title.get(k)
        if base is None:
            by_title[k] = r
            continue
        base["jf_source_id"] = r.get("jf_source_id")
        base["tv_status"] = base.get("tv_status") or r.get("tv_status")
        base["imdb_id"] = base.get("imdb_id") or r.get("imdb_id")
        base["added_at"] = base.get("added_at") or r.get("added_at")
        base["path"] = base.get("path") or r.get("path")
        theirs = {s["n"]: s for s in r.get("tv_seasons") or []}
        for s in base.get("tv_seasons") or []:
            other = theirs.pop(s["n"], None)
            if other:
                s["size_bytes"] = max(s["size_bytes"], other["size_bytes"])
                s["eps"] = max(s["eps"], other["eps"])
                s["added_at"] = max(s.get("added_at") or 0, other.get("added_at") or 0)
        base["tv_seasons"] = sorted((base.get("tv_seasons") or []) + list(theirs.values()),
                                    key=lambda x: x["n"])
        base["size_bytes"] = sum(s["size_bytes"] for s in base["tv_seasons"])
        base["size_gb"] = round(base["size_bytes"] / 1e9, 2)
        base["tv_episodes"] = sum(s["eps"] for s in base["tv_seasons"])
    rows = list(by_title.values())
    rows.sort(key=lambda r: (-(r.get("size_bytes") or 0), r.get("title") or ""))
    return rows


def _tv_inventory_configured(cfg: dict) -> bool:
    """A media server that can supply the TV inventory is selected and has
    connection values. Sonarr plays no part: it is optional and cleanup-only
    (the unmonitor step)."""
    conn = _effective_connection_values(cfg)
    return bool(
        (bool(cfg.get("USE_JELLYFIN")) and conn.get("jellyfin_url") and conn.get("jellyfin_key"))
        or (bool(cfg.get("USE_PLEX")) and conn.get("plex_url") and conn.get("plex_token")))


def _tv_inventory_rows(cfg: dict, strict: bool = False) -> list | None:
    """The TV inventory from the selected media servers — Jellyfin and/or
    Plex, title-merged. strict=False: a server that fails contributes
    nothing; None only when NO server contributed. strict=True (the deletion
    path): every configured server must answer or the call raises — deletion
    decisions are never made from a partial inventory."""
    conn = _effective_connection_values(cfg)
    jf_rows = px_rows = None
    if bool(cfg.get("USE_JELLYFIN")) and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        jf_rows = _jellyfin_series_inventory(conn["jellyfin_url"], conn["jellyfin_key"])
        if jf_rows is None and strict:
            raise RuntimeError("Jellyfin's TV inventory did not answer")
    if bool(cfg.get("USE_PLEX")) and conn.get("plex_url") and conn.get("plex_token"):
        px_rows = _plex_series_inventory(conn)
        if px_rows is None and strict:
            raise RuntimeError("Plex's TV inventory did not answer")
    if jf_rows is None and px_rows is None:
        return None
    return _merge_tv_sources(jf_rows, px_rows)


def _annotate_tv_imdb(rows: list) -> None:
    """Fill each TV row's rating/votes from the on-disk IMDb ratings dataset,
    joined by the imdb_id the media server supplies. One streaming pass over
    the TSV for the whole inventory; a missing dataset or unknown id leaves
    rating None, which the scorer treats exactly as it does an unrated
    movie."""
    wanted = {}
    for r in rows:
        tid = str(r.get("imdb_id") or "").strip()
        if tid:
            wanted.setdefault(tid, []).append(r)
    if not wanted:
        return
    try:
        with open(imdb_ratings_path(), encoding="utf-8") as f:
            for line in f:
                tid, _, rest = line.partition("\t")
                targets = wanted.get(tid)
                if not targets:
                    continue
                parts = rest.rstrip("\n").split("\t")
                try:
                    rating, votes = float(parts[0]), int(parts[1])
                except (IndexError, TypeError, ValueError):
                    continue
                for r in targets:
                    r["rating"], r["votes"] = rating, votes
    except OSError:
        return   # no dataset on disk — series stay unrated, never an error


def _norm_series_title(title) -> str:
    """A join key for one series named by three systems. Sonarr, Tautulli and
    Jellyfin all carry the show's display title but disagree on punctuation and
    case, so the key is lowercased alphanumerics with runs of everything else
    collapsed to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", str(title or "").lower()).strip()


def _new_series_agg() -> dict:
    # seasons: season number -> {"eps": set of episode ids, "last": epoch,
    #                            "plays": total play count, "users": set}
    return {"plays": 0, "users": set(), "last_played": 0, "eps": set(), "seasons": {}}


def _season_bucket(agg: dict, season_number) -> dict | None:
    try:
        n = int(season_number)
    except (TypeError, ValueError):
        return None
    return agg["seasons"].setdefault(n, {"eps": set(), "last": 0,
                                         "plays": 0, "users": set()})


def _tautulli_series_watch(conn: dict) -> dict:
    """Per-series watch facts from Tautulli's episode history, keyed by
    normalized title: plays, distinct users, distinct episodes, last watched.
    Swept page by page the way the movie viewer count is, with the same
    recordsFiltered termination and page backstop."""
    out: dict[str, dict] = {}
    start = 0
    for _page in range(1000):
        data = _tautulli_api_request(conn, "get_history", timeout=15,
                                     media_type="episode", length=1000, start=start)
        rows = (data or {}).get("data") or []
        if not rows:
            break
        for r in rows:
            if not isinstance(r, dict) or not r.get("grandparent_title"):
                continue
            agg = out.setdefault(_norm_series_title(r["grandparent_title"]), _new_series_agg())
            agg["plays"] += 1
            user = r.get("user_id") if r.get("user_id") is not None else r.get("user")
            if user is not None and str(user).strip():
                agg["users"].add(str(user))
            ep = r.get("rating_key")
            if ep is not None:
                agg["eps"].add(str(ep))
            try:
                when = int(r.get("date") or 0)
            except (TypeError, ValueError):
                when = 0
            agg["last_played"] = max(agg["last_played"], when)
            bucket = _season_bucket(agg, r.get("parent_media_index"))
            if bucket is not None:
                if ep is not None:
                    bucket["eps"].add(str(ep))
                bucket["last"] = max(bucket["last"], when)
                bucket["plays"] += 1
                if user is not None and str(user).strip():
                    bucket["users"].add(str(user))
        start += len(rows)
        total = (data or {}).get("recordsFiltered")
        if isinstance(total, int) and start >= total:
            break
    return out


def _jellyfin_series_watch(url: str, key: str, strict: bool = False) -> dict:
    """Per-series watch facts from Jellyfin: every user's played episodes,
    aggregated by series title. One Items query per user, like the movie
    per-user sweep. strict=True (the deletion path) raises on a failed
    favorites query instead of shrugging — one user's unreadable favorites
    must never read as that user having none."""
    out: dict[str, dict] = {}
    favorites: set[str] = set()
    # An empty-body 200 (misbehaving proxy) decodes to None — that is NOT
    # "no users": reading it as such would drop every watch and favorites
    # shield without any exception for the strict path to catch.
    users = _jellyfin_get(url, key, "/Users", timeout=15)
    if not isinstance(users, list):
        raise RuntimeError("Jellyfin's user list did not answer")
    for u in users:
        uid = str((u or {}).get("Id") or "")
        if not uid:
            continue
        # A series ANY user favorited is protected, same as movie favorites.
        try:
            favs = _jellyfin_get(url, key, f"/Users/{uid}/Items", {
                "IncludeItemTypes": "Series", "Recursive": "true",
                "Filters": "IsFavorite",
            }, timeout=20)
            if not isinstance(favs, dict):
                raise RuntimeError("empty favorites answer")
            for it in favs.get("Items") or []:
                if isinstance(it, dict) and it.get("Name"):
                    favorites.add(_norm_series_title(it["Name"]))
        except Exception:
            if strict:
                raise RuntimeError("a Jellyfin favorites query did not answer")
        data = _jellyfin_get(url, key, f"/Users/{uid}/Items", {
            "IncludeItemTypes": "Episode", "Recursive": "true",
            "Filters": "IsPlayed", "Fields": "SeriesName,ParentIndexNumber",
        }, timeout=30)
        if not isinstance(data, dict):
            raise RuntimeError("a Jellyfin played-episodes query did not answer")
        for it in data.get("Items") or []:
            if not isinstance(it, dict) or not it.get("SeriesName"):
                continue
            agg = out.setdefault(_norm_series_title(it["SeriesName"]), _new_series_agg())
            ud = it.get("UserData") or {}
            try:
                it_plays = max(1, int(ud.get("PlayCount") or 0))
            except (TypeError, ValueError):
                it_plays = 1
            agg["plays"] += it_plays
            agg["users"].add(uid)
            if it.get("Id") is not None:
                agg["eps"].add(str(it["Id"]))
            lp = 0
            raw_lp = str(ud.get("LastPlayedDate") or "")
            if raw_lp:
                try:
                    lp = int(datetime.fromisoformat(raw_lp.replace("Z", "+00:00")).timestamp())
                    agg["last_played"] = max(agg["last_played"], lp)
                except ValueError:
                    lp = 0
            bucket = _season_bucket(agg, it.get("ParentIndexNumber"))
            if bucket is not None:
                if it.get("Id") is not None:
                    bucket["eps"].add(str(it["Id"]))
                bucket["last"] = max(bucket["last"], lp)
                bucket["plays"] += it_plays
                bucket["users"].add(uid)
    return out, favorites


def _plex_protected_series_titles(conn: dict, names) -> set:
    """Normalized titles of every series inside the named Plex collections,
    from the SHOW sections. The movie side protects by file path; series
    protection joins by title like everything else TV."""
    url, token = conn.get("plex_url"), conn.get("plex_token")
    if not (url and token and names):
        return set()
    wanted = {str(n) for n in names}
    out: set = set()
    data = _plex_get(url, token, "/library/sections", timeout=10)
    if not isinstance(data, dict):
        # An empty-body 200 is not "no collections" — protection must never
        # silently read as absent. The non-strict caller catches this.
        raise RuntimeError("Plex's section list did not answer")
    for sec in (data.get("MediaContainer") or {}).get("Directory") or []:
        if not isinstance(sec, dict) or sec.get("type") != "show":
            continue
        cols = _plex_get(url, token,
                         f"/library/sections/{sec.get('key')}/collections", timeout=10)
        if not isinstance(cols, dict):
            # The same rule as the section list above, for the SAME reason: an
            # empty-body 200 here would silently read as "this section has no
            # collections" and strip a protected show's shield for the pass.
            raise RuntimeError("Plex's collection list did not answer")
        for col in (cols.get("MediaContainer") or {}).get("Metadata") or []:
            if not isinstance(col, dict) or str(col.get("title") or "") not in wanted:
                continue
            kids = _plex_get(url, token,
                             f"/library/collections/{col.get('ratingKey')}/children",
                             timeout=15)
            if not isinstance(kids, dict):
                raise RuntimeError("Plex's collection members did not answer")
            for kid in (kids.get("MediaContainer") or {}).get("Metadata") or []:
                if isinstance(kid, dict) and kid.get("title"):
                    out.add(_norm_series_title(kid["title"]))
    return out


def _jellyfin_protected_series_titles(url: str, key: str, names) -> set:
    """Normalized titles of every SERIES inside the named Jellyfin box sets."""
    if not names:
        return set()
    wanted = {str(n) for n in names}
    out: set = set()
    data = _jellyfin_get(url, key, "/Items", {
        "IncludeItemTypes": "BoxSet", "Recursive": "true"}, timeout=15)
    if not isinstance(data, dict):
        # Same rule as the Plex side: an empty body must raise, not read as
        # "nothing is protected".
        raise RuntimeError("Jellyfin's box-set list did not answer")
    for box in data.get("Items") or []:
        if not isinstance(box, dict) or str(box.get("Name") or "") not in wanted \
                or not box.get("Id"):
            continue
        kids = _jellyfin_get(url, key, "/Items", {"ParentId": box["Id"]}, timeout=15)
        if not isinstance(kids, dict):
            # An empty body must raise here too — the box-set list answered,
            # but its MEMBERS are the protection; reading them as absent
            # unprotects the whole set.
            raise RuntimeError("Jellyfin's box-set members did not answer")
        for kid in kids.get("Items") or []:
            if isinstance(kid, dict) and kid.get("Type") == "Series" and kid.get("Name"):
                out.add(_norm_series_title(kid["Name"]))
    return out


def _annotate_tv_watch(rows: list, cfg: dict, strict: bool = False) -> None:
    """Fill each TV row's plays/users/last_played and episodes-watched from
    whichever servers are selected and answering. Counts take the MAX across
    servers, never the sum: both servers watch the same audience, and someone
    who uses both would otherwise be counted twice — the same rule the movie
    viewer count follows.

    strict=False (the inventory refresh): a server that fails to answer
    contributes nothing, and rows it would have annotated keep zeros rather
    than blocking the refresh. strict=True (the deletion path): EVERY
    configured source must answer or the call raises — watch history shields
    mid-watch seasons and favorites/collections shield whole shows, so a gap
    in any of them must abort a deletion pass, never soften it."""
    conn = _effective_connection_values(cfg)
    taut: dict = {}
    jf: dict = {}
    if bool(cfg.get("USE_PLEX")) and conn.get("tautulli_url") and conn.get("tautulli_key"):
        try:
            taut = _tautulli_series_watch(conn)
        except Exception:
            if strict:
                raise RuntimeError("Tautulli watch history did not answer")
            taut = {}
    jf_favs: set = set()
    if bool(cfg.get("USE_JELLYFIN")) and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        try:
            jf, jf_favs = _jellyfin_series_watch(conn["jellyfin_url"], conn["jellyfin_key"],
                                                 strict=strict)
        except Exception:
            if strict:
                raise RuntimeError("Jellyfin watch history / favorites did not answer")
            jf, jf_favs = {}, set()
    # Protected collections apply to shows exactly as to movies: the configured
    # collection names, with each side's series members joined by title. In the
    # non-strict refresh a fetch failure protects nothing HERE (this flag is
    # display and scoring input) — the deletion path runs strict, where it
    # aborts the pass instead.
    protected: set = set()
    if bool(cfg.get("USE_PLEX")):
        try:
            protected |= _plex_protected_series_titles(conn, cfg.get("PROTECTED_COLLECTIONS"))
        except Exception:
            if strict:
                raise RuntimeError("Plex protected collections did not answer")
    if bool(cfg.get("USE_JELLYFIN")) and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        try:
            protected |= _jellyfin_protected_series_titles(
                conn["jellyfin_url"], conn["jellyfin_key"],
                cfg.get("JELLYFIN_PROTECTED_COLLECTIONS"))
        except Exception:
            if strict:
                raise RuntimeError("Jellyfin protected collections did not answer")
    if not taut and not jf and not jf_favs and not protected:
        return
    empty = _new_series_agg()
    for row in rows:
        k = _norm_series_title(row.get("title"))
        a, b = taut.get(k, empty), jf.get(k, empty)
        row["plays"] = max(a["plays"], b["plays"])
        row["users"] = max(len(a["users"]), len(b["users"]))
        row["last_played"] = max(a["last_played"], b["last_played"])
        row["tv_episodes_watched"] = max(len(a["eps"]), len(b["eps"]))
        if _norm_series_title(row.get("title")) in jf_favs:
            row["favorite"] = True
        if _norm_series_title(row.get("title")) in protected:
            row["protected"] = True

        for season in row.get("tv_seasons") or []:
            sa = a["seasons"].get(season["n"]) if a["seasons"] else None
            sb = b["seasons"].get(season["n"]) if b["seasons"] else None
            season["eps_watched"] = max(len(sa["eps"]) if sa else 0,
                                        len(sb["eps"]) if sb else 0)
            season["last_played"] = max(sa["last"] if sa else 0,
                                        sb["last"] if sb else 0)
            # Season plays and watchers feed the movie-equivalent usage curve
            # and the per-season multi-user term; max across servers, never
            # the sum, same as every other watch count.
            season["plays"] = max(sa["plays"] if sa else 0,
                                  sb["plays"] if sb else 0)
            season["users"] = max(len(sa["users"]) if sa else 0,
                                  len(sb["users"]) if sb else 0)


def _resolve_tv_scope(rows: list, cfg: dict) -> None:
    """Decide which series cleanup MAY touch: monitored paths are the deletion
    allow-list for TV exactly as for movies. The media server supplies the
    inventory — including shows kept somewhere deliberately unmanaged — so
    each series folder is looked for under the monitored directories BY NAME
    (the server's own path is in its container namespace, so the folder name
    is the part both filesystems share).

    Exactly one match: the row carries that resolved /library path and is in
    scope. None, or more than one: out of scope, and the row stays with the
    reason on it — an inventory that silently omits what it won't manage looks
    like a scan bug, not a decision. More than one is its own answer because a
    name is not an identity: two libraries of the same shows give the same
    folder name under both, and picking either would delete out of the copy the
    server was not describing."""
    dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or []) if str(d or "").strip()]
    for row in rows:
        # The same trailing-run rule the movie side's resolve_under_library
        # uses, at directory grain: the LONGEST run of the server path's
        # segments that exists under a monitored dir wins, so a nested layout
        # (/shows/Anime/Dead Show under a monitored "shows") resolves too,
        # and the bare folder name is the natural last rung. Dotted segments
        # never resolve — a path ending in ".." would name a monitored dir's
        # PARENT and hand the deletion pass a folder that isn't a series.
        parts = [seg for seg in str(row.get("path") or "").replace("\\", "/").split("/") if seg]
        resolved, conflict = None, []
        if parts and not any(seg in (".", "..") for seg in parts):
            for start in range(len(parts)):
                # Every monitored dir that HAS this run, not the first one that
                # does. Two libraries of the same shows — a 1080p tree and a 4K
                # tree, an archive beside a live one — give the same folder name
                # under both, and taking whichever came first in MONITOR_DIRS
                # would delete seasons out of the copy the server was not
                # talking about, scored on the other copy's facts. A name is
                # not an identity, so an ambiguous one resolves to nothing.
                #
                # Keyed by the REAL path: the same folder reachable through two
                # overlapping monitored entries is one place, not a conflict.
                hits = {}
                for d in dirs:
                    candidate = Path(d).joinpath(*parts[start:])
                    try:
                        if candidate.is_dir():
                            hits.setdefault(candidate.resolve(), str(candidate))
                    except OSError:
                        continue
                if len(hits) == 1:
                    resolved = next(iter(hits.values()))
                    break
                if hits:
                    # A SHORTER run matches in at least as many places, never
                    # fewer, so there is no less-ambiguous rung left to try.
                    conflict = sorted(hits.values())
                    break
        row["path"] = resolved
        row["tv_in_scope"] = 1 if resolved else 0
        # Kept on the row so the debug report can say "ambiguous" rather than
        # leaving it indistinguishable from a show nobody monitors.
        row["tv_scope_conflict"] = conflict
        if conflict:
            print(f"TV scope: {row.get('title') or row.get('path')} matches "
                  f"{len(conflict)} monitored folders ({', '.join(conflict)}) — "
                  f"out of scope until the name is unambiguous", flush=True)

    # The same refusal in the OTHER direction: one name matching two folders is
    # ambiguous, and two ROWS claiming one folder is too. A same-named show in
    # a monitored and an unmonitored library resolves BY NAME into the
    # monitored copy's folder, and one show splits into two rows when the
    # servers disagree on its year — either way the plan would treat one
    # on-disk folder as two shows: seasons counted twice in the TV share, and
    # one row's episode paths joined onto a folder the other row's facts
    # describe. A folder claimed twice is not an identity, so every claimant
    # goes out of scope.
    claims: dict = {}
    for row in rows:
        if not row.get("path"):
            continue
        try:
            key = str(Path(row["path"]).resolve())
        except OSError:
            key = str(row["path"])
        claims.setdefault(key, []).append(row)
    for key, group in claims.items():
        if len(group) < 2:
            continue
        titles = ", ".join(str(r.get("title") or "?") for r in group)
        for row in group:
            row["path"] = None
            row["tv_in_scope"] = 0
            row["tv_scope_conflict"] = [key]
        print(f"TV scope: {len(group)} inventory rows ({titles}) all resolve to "
              f"{key} — out of scope until one show owns the folder", flush=True)


def _season_score_settings(cfg: dict) -> dict:
    """The scoring/filter settings season scoring reads, resolved once — the
    same knobs the movie curve uses, plus the TV series-watch bump."""
    balance = _config_num(cfg.get("SCORE_BALANCE"))
    balance = 50.0 if balance is None else max(0.0, min(100.0, balance))
    quality_weight = balance / 100.0
    stale = _config_num(cfg.get("MAX_STALENESS_MONTHS"))
    bump = _config_num(cfg.get("TV_SERIES_WATCH_BUMP"))
    weight = _config_num(cfg.get("TV_WATCH_WEIGHT"))
    cutoff = _config_num(cfg.get("MAX_IMDB_RATING"))
    eps_cap = _config_num(cfg.get("TV_MAX_SEASON_EPISODES"))
    if eps_cap is None:
        eps_cap = float(TV_MAX_SEASON_EPISODES_DEFAULT)
    return {
        "history_weight": 1.0 - quality_weight,
        "quality_weight": quality_weight,
        "max_staleness_months": stale if stale and stale > 0 else 36.0,
        "series_watch_bump": bump if bump is not None else 10.0,
        # Percent → multiplier: what one full season-watch (plays = episodes)
        # is worth against one movie watch. 100 = one watch, 200 = two.
        "watch_weight": max(1.0, min(2.0, weight / 100.0)) if weight else 1.0,
        "grace_days": max(0.0, _config_num(cfg.get("GRACE_PERIOD_DAYS")) or 0.0),
        "max_imdb_rating": cutoff if cutoff and cutoff > 0 else None,
        "skip_unplayed": bool(cfg.get("SKIP_UNPLAYED_MOVIES")),
        "protect_favorites": bool(cfg.get("PROTECT_JELLYFIN_FAVORITES")),
        # Which of a show's seasons may delete: only the oldest on disk
        # (default), any but the most recently ADDED (newest ≠ latest), or
        # all. An unknown value reads as the strictest — oldest.
        "season_eligibility": (str(cfg.get("TV_SEASON_ELIGIBILITY") or "oldest").lower()
                               if str(cfg.get("TV_SEASON_ELIGIBILITY") or "oldest").lower()
                               in ("oldest", "except_newest", "all") else "oldest"),
        # Above this many episodes a season is not a season — it is a show
        # filed under one number, and deleting it deletes the show. See the
        # ladder in _tv_season_plan for why it is a shield rather than a
        # scoring input.
        #
        # A MISSING key takes the shipped default rather than "no cap": this
        # shield only ever holds deletions back, so a config written before it
        # existed should get it. 0 is the off switch, and 0 is a value someone
        # chose — absent is not.
        "max_season_episodes": (int(eps_cap) if eps_cap and eps_cap > 0 else None),
    }


def _tv_season_plan(tv_rows: list, cfg: dict, now: float | None = None) -> dict:
    """The order a TV cleanup would remove seasons in. The season is the unit;
    a "whole show" is every season of it scoring low. Returned as
    {"order": [...], "excluded": {...}, "target": ...} with retention scores
    ASCENDING — the same 0-100 scale movies score on (season_retention_score
    shares the movie curve family), so this order intersplices directly with
    the movie deletion order.

    Whole series step aside first — off monitored paths, protected, favorited —
    and a CONTINUING show's latest season on disk is never in the order (that
    is the season the household may be keeping up with). The shared
    eligibility filters then apply at season grain, mirroring the movie rules:
    the grace period (on the series' added date), the IMDb rating cutoff and
    the no-IMDb-data rule (on the series rating, when IMDb is in use), and the
    skip-unplayed switch (a season with no watch history).

    Sizing lives in _merged_pool_takes: seasons and the engine's movie queue
    sort into ONE order there, and the covering prefix of the pool's deficit
    decides which seasons (take=True) and how many movie-bytes go.
    """
    now = now or time.time()
    ss = _season_score_settings(cfg)
    imdb_in_use = ss["quality_weight"] > 0 or ss["max_imdb_rating"] is not None

    order = []
    excluded = {"off_path": 0, "protected": 0, "favorite": 0,
                "latest_of_continuing": 0, "oversized_season": 0, "season_rule": 0,
                "recently_added": 0, "high_rated": 0, "no_imdb_data": 0,
                "unplayed": 0}
    for r in tv_rows:
        if not isinstance(r, dict) or r.get("media_type") != "tv":
            continue
        seasons = [s for s in (r.get("tv_seasons") or []) if isinstance(s, dict)]
        if not seasons:
            continue
        if not r.get("tv_in_scope"):
            excluded["off_path"] += len(seasons)
            continue
        if r.get("protected"):
            excluded["protected"] += len(seasons)
            continue
        # Favorites shield the whole show under the SAME eligibility toggle
        # movies use (Don't delete Jellyfin favorites).
        if ss["protect_favorites"] and r.get("favorite"):
            excluded["favorite"] += len(seasons)
            continue
        # The shared series-level filters — the rung decisions live in
        # shared.py, imported by the movie ladder too. Series ratings come
        # from the dataset (which always carries votes), so absence of the
        # rating alone is the data gap: require_votes=False. (The grace
        # period applies at SEASON grain below, on each season's own added
        # date — a brand-new season of an old show is graced too.)
        added_at = r.get("added_at") or 0
        rating = r.get("rating")
        imdb = shared.imdb_rung(rating, r.get("votes"), in_use=imdb_in_use,
                                require_votes=False, cutoff=ss["max_imdb_rating"])
        if imdb:
            excluded[imdb] += len(seasons)
            continue
        # The latest season on disk is shielded unless the show is KNOWN to be
        # ended: a continuing show's current season is what the household may
        # be keeping up with, and a show whose server states no status (Plex
        # has none) gets the same benefit of the doubt.
        continuing = (r.get("tv_status") != "ended")
        latest_n = max(s.get("n") or 0 for s in seasons)
        # The season-eligibility rule's reference points: the oldest season
        # on disk (lowest number) and the NEWEST (most recently added, by
        # each season's own date — which may not be the latest season).
        # Season 0 is Specials: letting it count as "the oldest" would make
        # oldest-only mode spend forever on a tiny extras folder while every
        # real season is excluded. The oldest REAL season is the eligible one;
        # S0 only counts when it is all the show has.
        real_ns = [s.get("n") or 0 for s in seasons if (s.get("n") or 0) >= 1]
        oldest_n = min(real_ns) if real_ns else min(s.get("n") or 0 for s in seasons)
        newest_n = max(seasons, key=lambda x: ((x.get("added_at") or 0),
                                               x.get("n") or 0)).get("n") or 0
        series_facts = {"eps": r.get("tv_episodes"),
                        "eps_watched": r.get("tv_episodes_watched"),
                        "users": r.get("users"), "added_at": added_at,
                        "rating": rating, "votes": r.get("votes")}
        for s in seasons:
            if continuing and (s.get("n") or 0) == latest_n:
                excluded["latest_of_continuing"] += 1
                continue
            # A show whose seasons are not split — everything filed under one
            # number — presents its whole run as a single season, and deleting
            # that deletes the show. Anime that never re-numbers, long-running
            # dailies, a library that flattened its folders. The cap catches
            # them by the one thing that distinguishes them: an episode count
            # no real season has. Ranked ABOVE the season-eligibility rule so
            # a flattened show, which is by definition its own oldest season,
            # reports the size and not "waiting its turn".
            #
            # A season with no episode count is not judged: unknown is not
            # oversized, and a shield that fires on missing data would hold
            # back everything the moment a server stopped reporting.
            if ss["max_season_episodes"] and (s.get("eps") or 0) > ss["max_season_episodes"]:
                excluded["oversized_season"] += 1
                continue
            if ss["season_eligibility"] == "oldest" and (s.get("n") or 0) != oldest_n:
                excluded["season_rule"] += 1
                continue
            if ss["season_eligibility"] == "except_newest" and (s.get("n") or 0) == newest_n:
                excluded["season_rule"] += 1
                continue
            s_added = s.get("added_at") or added_at
            if shared.grace_rung(s_added, ss["grace_days"], now):
                excluded["recently_added"] += 1
                continue
            eps, watched = (s.get("eps") or 0), (s.get("eps_watched") or 0)
            if shared.unplayed_rung(watched > 0 or (s.get("last_played") or 0),
                                    ss["skip_unplayed"]):
                excluded["unplayed"] += 1
                continue
            score, _breakdown = season_retention_score(
                s, series_facts,
                history_weight=ss["history_weight"],
                quality_weight=ss["quality_weight"],
                max_staleness_months=ss["max_staleness_months"],
                series_watch_bump=ss["series_watch_bump"],
                watch_weight=ss["watch_weight"], now=now)
            order.append({
                "title": r.get("title"), "season": s.get("n"),
                "year": r.get("year"),
                # Identity + location for the cleanup pass: the media server's
                # id is the mark key (stable across title edits), the resolved
                # series folder is where the season's files live.
                "sid": r.get("jf_source_id") or r.get("source_id"),
                "path": r.get("path"),
                "size_bytes": s.get("size_bytes") or 0,
                "eps": eps, "eps_watched": watched,
                "last_played": s.get("last_played") or 0,
                "score": round(score, 3),
                "take": False,
            })
    # Worst-scored first; a tie goes to the LARGER season, since the order
    # exists to show where the space would come from.
    order.sort(key=lambda e: (e["score"], -e["size_bytes"]))
    return {"order": order, "excluded": excluded}


def _refresh_tv_inventory(cfg: dict) -> int | None:
    """Fetch the media servers' TV inventory and replace the snapshot's TV
    rows. Returns the row count, or None when nothing was fetched (not
    configured / no answer) — in which case the stored rows are LEFT ALONE,
    so a server blip doesn't empty the TV view it filled yesterday."""
    rows = _tv_inventory_rows(cfg)
    if rows is None:
        return None
    if rows:
        _resolve_tv_scope(rows, cfg)
        _annotate_tv_watch(rows, cfg)
        _annotate_tv_imdb(rows)
    with db.transaction(db_path()) as conn:
        db.replace_tv_series(conn, rows)
    return len(rows)


# ── TV cleanup: the season deletion pass ──────────────────────────────────────
# The season is the deletion unit; the triggers are the POOL's — the Headroom
# target and the Library Size Cap, shared with the movie side. The pass fires
# WITH every engine run (before the engine launches): it recomputes the
# season plan from FRESH server data, merges it with the movie queue into one
# worst-first order, and (a) marks the seasons inside the deficit-covering
# prefix — each mark waits out the deletion delay like a marked movie — and
# (b) on a real Cleanup, deletes marked seasons whose delay has elapsed and
# that the fresh plan STILL takes. Monitor Only runs the same pass without the
# deleting half, so the marks (and their delete-on dates) are visible in the
# Cache debug before anything is armed.
#
# Everything is fail-closed: if any CONFIGURED source (Tautulli, Jellyfin,
# Plex) does not answer, the pass aborts without touching a file — a missing
# favorites list must read as "everything might be a favorite", never as
# "nothing is". Per season, when Sonarr is configured it must accept the
# season unmonitor BEFORE the first file is removed (a deleted-but-monitored
# season would just be re-downloaded), and every file is re-checked against
# the monitored roots. Sonarr itself is OPTIONAL and cleanup-only: the
# inventory comes from the media servers.

_TV_STATE_KEY = "tv_cleanup"


def _tv_mark_key(entry: dict) -> str:
    return f"{entry.get('sid')}|S{entry.get('season')}"


def _tv_in_scope(cfg: dict) -> bool:
    """Seasons take part in the run at all: a media server can supply the
    inventory and a DAILY pool trigger is armed — the Headroom target or the
    Library Size Cap, the same thresholds that size the movie marks (one pool,
    one cap; Redline is a movie-only emergency). Sonarr is NOT required — it
    is optional and cleanup-only.

    Deliberately NOT gated on the TV switch. Off means seasons are ineligible,
    not invisible: the run still plans them and reports how many the switch
    held back, exactly as the movie side reports movie_cleanup_off. Whether a
    run may DELETE is decided by RUN_MODE at run time (Automatic Cleanup
    deletes, Monitor Only marks)."""
    return (_tv_inventory_configured(cfg)
            and (_threshold_gb_or_none(cfg.get("MAX_LIBRARY_GB")) is not None
                 or (_config_num(cfg.get("HEADROOM_GB")) or 0) > 0))


def _tv_cleanup_armed(cfg: dict) -> bool:
    """Seasons may actually be marked and deleted: in scope AND the switch is
    on. _tv_in_scope is the wider question of whether they are planned at all."""
    return bool(cfg.get("TV_CLEANUP_ENABLED", False)) and _tv_in_scope(cfg)


def _tv_cleanup_state() -> dict:
    """The pass's persistent state from the store: {"marked": {key: mark},
    "last_pass": report, "last_pass_date": "YYYY-MM-DD"}. Missing/unreadable
    reads as empty — worst case marks restart their delay clocks, which only
    ever delays a deletion."""
    try:
        with db.connect(db_path()) as conn:
            st = db.get_meta(conn, _TV_STATE_KEY, None)
    except Exception:
        st = None
    if not isinstance(st, dict):
        st = {}
    if not isinstance(st.get("marked"), dict):
        st["marked"] = {}
    return st


def _save_tv_cleanup_state(st: dict) -> None:
    with db.transaction(db_path()) as conn:
        db.set_meta(conn, _TV_STATE_KEY, st)


def _tv_fresh_rows_strict(cfg: dict) -> list:
    """TV rows with scope, watch and protection facts, every configured source
    REQUIRED to answer. Raises on any gap — deletion decisions are never made
    from partial protection data."""
    rows = _tv_inventory_rows(cfg, strict=True)
    if rows is None:
        raise RuntimeError("no media server answered with a TV inventory")
    _resolve_tv_scope(rows, cfg)
    _annotate_tv_watch(rows, cfg, strict=True)
    _annotate_tv_imdb(rows)   # ratings only raise scores; a missing dataset is not a gap
    return rows


def _pool_deletion_target_bytes(cfg: dict) -> int:
    """The pool's DAILY deficit in bytes — the larger of the headroom overage
    and the Library Size Cap overage, the same arithmetic the engine sizes its
    movie marks with (_daily_deficit_bytes there). The cap measures the whole
    pool: the library size is the measured on-disk total of every monitored
    directory, TV included. Redline is an emergency and never sizes marks.

    The arithmetic itself is shared.pool_deficit_gb — imported by the engine
    too — so the two executors can never disagree on the deficit; the only
    translation here is the Headroom target into the used-space limit it
    means (total minus headroom)."""
    disk = disk_stats() or {}
    try:
        used, total = float(disk.get("used_gb")), float(disk.get("total_gb"))
    except (TypeError, ValueError):
        return 0
    headroom = _config_num(cfg.get("HEADROOM_GB")) or 0
    used_limit = (total - headroom) if headroom > 0 else None
    return int(shared.pool_deficit_gb(
        used, used_limit,
        (library_stats() or {}).get("library_gb"),
        _threshold_gb_or_none(cfg.get("MAX_LIBRARY_GB"))) * 1_000_000_000)


def _least_wasteful_index(pool, remaining, near_tie):
    """Index of the next item to take from the worst-first pool.

    The head, unless the near-tied band at the head holds MORE than what is
    left to free — then only some of that band needs to go. Those scores are
    equivalent by definition, so the least wasteful cover wins: the SMALLEST
    item that still finishes the job, movie or season alike. When nothing in
    the band covers it alone, the largest goes first (fastest progress), which
    is the movie loop's rule too.

    This is the only place the two media types are compared by size, and it is
    deliberately the last question asked: score decides which items reach the
    boundary at all, and this decides only which of several equally-scored
    ones is the cheapest way to finish. Without it the type with the bigger
    unit — always TV — overshot every small deficit by a whole season while a
    near-tied movie would have covered it with a fraction of the waste.
    """
    if not near_tie or remaining <= 0:
        return 0
    head = pool[0][0]
    band = []
    for i, t in enumerate(pool):
        if t[0] - head > near_tie:
            break
        band.append(i)
    # Short-circuit, not a guard: when the whole band is needed every member is
    # taken whatever order they go in, so this cannot change the outcome — it
    # skips the work and mirrors the condition the movie loop uses to decide
    # whether a tie group forms at all.
    if sum(pool[i][1] for i in band) <= remaining:
        return 0
    covers = [i for i in band if pool[i][1] >= remaining]
    if covers:
        return min(covers, key=lambda i: (pool[i][1], pool[i][0]))
    return max(band, key=lambda i: (pool[i][1], -pool[i][0]))


def _merged_pool_takes(season_order: list, cfg: dict) -> tuple[dict, dict]:
    """Split the pool's daily deficit between MOVIES and SEASONS with one
    merged deletion order: every eligible movie (the engine's queue, scores
    and sizes as the last plan stored them) and every eligible season
    (season_order, the same 0-100 scale) sorted together worst-first, ties to
    the larger item. The seasons inside the covering prefix get take=True —
    those are the season side's to delete; the movie bytes inside it are the
    movie share. The engine learns the split via the tv_share stamp and
    subtracts the SEASON share from its own target, so the two executors
    cover the one deficit exactly once."""
    share = {"target_bytes": _pool_deletion_target_bytes(cfg),
             "tv_share_bytes": 0, "movie_share_bytes": 0}
    if share["target_bytes"] <= 0:
        return {}, share
    movies = []
    try:
        doc = db.read_pending_doc(db_path())
        for e in (doc.get("entries") or {}).values():
            if isinstance(e, dict):
                movies.append((float(e.get("score") or 0.0), _entry_size_bytes(e)))
    except Exception:
        movies = []   # no movie plan yet: seasons cover what they can
    # One queue, both types, worst-first; an exact score tie goes to the LARGER
    # item. A season the pass could never act on (no server id, no resolved
    # folder) is left out entirely rather than skipped mid-walk: it must never
    # claim share bytes, or the engine would subtract them from the movie
    # target and NOBODY would ever free them.
    pool = [(e["score"], e["size_bytes"] or 0, "season", e) for e in season_order
            if e.get("sid") and e.get("path")]
    pool += [(sc, sz, "movie", None) for sc, sz in movies]
    pool.sort(key=lambda t: (t[0], -t[1]))
    near_tie = _config_num(cfg.get("NEAR_TIE_PTS"))
    covered = 0
    while pool and covered < share["target_bytes"]:
        _score, size, kind, item = pool.pop(
            _least_wasteful_index(pool, share["target_bytes"] - covered, near_tie))
        if kind == "season":
            item["take"] = True
            share["tv_share_bytes"] += size
        else:
            share["movie_share_bytes"] += size
        covered += size
    takes = {_tv_mark_key(e): e for e in season_order if e["take"]}
    return takes, share


def _stamp_tv_share(share_bytes) -> None:
    """Persist the season share of the pool deficit for the engine, which
    subtracts it from its own movie target (engine reads it with a freshness
    window, so a stale stamp fails toward the movie side covering it all)."""
    try:
        with db.transaction(db_path()) as conn:
            db.set_meta(conn, "tv_share", {"bytes": int(max(0, share_bytes)),
                                           "at": time.time()})
    except Exception:
        pass


def _tv_marked_count() -> int:
    """Marked seasons with a running delay clock — counted right alongside
    the marked movies in the dashboard's one number. One cleanup, one
    count; nothing here is a separate TV anything."""
    return sum(1 for m in (_tv_cleanup_state().get("marked") or {}).values()
               if isinstance(m, dict) and m.get("marked_at"))


def _tv_eligible_count(cfg: dict) -> int:
    """How many seasons the standing eligible order holds — the number the
    dashboard's Marked & Eligible button adds to the movie count.

    Counted from the SAME place the window lists them: the library snapshot,
    through the same plan. Reading a number the run stored instead looked
    cheaper and was wrong — the two sources part company whenever the run has
    planned seasons the snapshot does not carry yet, which is exactly what a
    rebuild after a wiped store does (the season side plans first, the
    inventory lands when the run finishes). The button then advertised
    seasons the window could not show, and opening it corrected the count
    only until the next poll put the stored number back.

    The snapshot read is memoized, and the plan is arithmetic over rows
    already in memory, so this stays cheap enough for the status poll."""
    if not _tv_cleanup_armed(cfg):
        return 0
    return len(_tv_eligible_entries(cfg, set(_tv_cleanup_state().get("marked") or {})))


def _tv_pass_plan_for_run(cfg: dict, effective_mode: str | None):
    """Whether the run handles seasons in this mode, and whether it may
    DELETE them. None = no season side (TV cleanup disarmed, or a Redline
    emergency is breached RIGHT NOW — the engine must start deleting movies
    immediately, not wait minutes behind a server sweep, and Redline
    never deletes TV anyway); False = mark only (Simulate, Debug Cleanup,
    Monitor Only's maintenance run); True = a real Cleanup, which also
    deletes due marks. Seasons ride every non-emergency engine run — same
    gesture as the movies, no schedule of their own."""
    if not _tv_in_scope(cfg):
        return None
    if _redline_breached_now(cfg):
        return None
    return _is_cleanup_mode(effective_mode)


def _run_tv_cleanup_pass(cfg: dict, *, execute: bool, immediate: bool = False) -> dict:
    """The run's season side — fired with every engine run: refresh facts fail-closed,
    reconcile the marks with the fresh take prefix, and (execute=True only)
    delete the marked seasons whose delay has elapsed, worst-kept first.
    immediate=True skips the deletion delay, for the one run that is allowed to:
    a manual Cleanup. The delay paces the AUTOMATIC schedule — it buys time to
    change your mind about what a daily run will take — and a button press is
    the user saying the target matters now.

    Returns the report it also persists as the state's last_pass."""
    today = time.strftime("%Y-%m-%d")
    report = {"at": time.time(), "date": today,
              "mode": "cleanup" if execute else "monitor",
              "pool_target_gb": 0, "tv_share_gb": 0, "movie_share_gb": 0,
              "marked_new": 0, "unmarked": 0, "held_by_delay": 0,
              "deleted_seasons": [], "skipped": [], "aborted": None,
              "blocked_by_safety": False, "eligible_seasons": 0,
              "seasons_seen": 0, "cleanup_off": 0, "excluded": {},
              "deleted_files": 0, "freed_bytes": 0,
              "vanished_seasons": 0, "vanished_bytes": 0}
    st = _tv_cleanup_state()

    def _finish():
        st["last_pass"] = report
        st["last_pass_date"] = today
        _save_tv_cleanup_state(st)
        return report

    try:
        rows = _tv_fresh_rows_strict(cfg)
    except Exception as e:
        report["aborted"] = str(e)
        _stamp_tv_share(0)   # seasons will free nothing; the movie side covers it all
        print(f"TV cleanup: aborted fail-closed — {e}", flush=True)
        return _finish()

    # The same safety refusal the movie side makes: thresholds so far below
    # the pool that reaching them would delete more than the safety
    # percentage allows. Mark nothing rather than pace a config mistake.
    # This is a RUN-WIDE fact, not a TV one — the Space Thresholds module
    # reports it once for the whole cleanup, so nothing here surfaces to
    # the user as a separate abort.
    state = _space_threshold_state(cfg, disk_stats(),
                                   (library_stats() or {}).get("library_gb"))
    if state.get("safety_blocked"):
        report["blocked_by_safety"] = True
        _stamp_tv_share(0)
        print("TV cleanup: space thresholds outside the safety limits — "
              "marking nothing this run", flush=True)
        return _finish()

    # The plan is built whether or not TV cleanup is on. Off means seasons are
    # INELIGIBLE, not invisible — the same shape as the movie side, which keeps
    # scanning and scoring and reports movie_cleanup_off as the reason nothing
    # is eligible. Building it first is what gives the run a number to report.
    plan = _tv_season_plan(rows, cfg)
    report["excluded"] = dict(plan.get("excluded") or {})
    report["seasons_seen"] = sum(len(r.get("tv_seasons") or [])
                                 for r in rows if isinstance(r, dict))
    if not bool(cfg.get("TV_CLEANUP_ENABLED", False)):
        # Held back by the switch. Marks go too: an ineligible season must not
        # keep a delay clock running, exactly as a raised cap unmarks.
        report["cleanup_off"] = len(plan["order"])
        report["unmarked"] = len(st["marked"])
        st["marked"] = {}
        _stamp_tv_share(0)
        print(f"TV cleanup is off — {report['cleanup_off']} season(s) planned "
              f"but not eligible", flush=True)
        return _finish()
    takes, pool = _merged_pool_takes(plan["order"], cfg)
    report["eligible_seasons"] = len(plan["order"])
    report["pool_target_gb"] = round(pool["target_bytes"] / 1e9, 1)
    report["tv_share_gb"] = round(pool["tv_share_bytes"] / 1e9, 1)
    report["movie_share_gb"] = round(pool["movie_share_bytes"] / 1e9, 1)
    _stamp_tv_share(pool["tv_share_bytes"])
    marked = st["marked"]

    # A mark the fresh plan no longer takes is dropped, whatever the reason —
    # the cap was raised, the show got protected/favorited, someone started
    # watching, or enough space came back. Marks only ever mirror the plan.
    for k in [k for k in marked if k not in takes]:
        del marked[k]
        report["unmarked"] += 1

    try:
        delay = max(1, int(float(cfg.get("DELETE_DELAY_DAYS") or 1)))
    except (TypeError, ValueError):
        delay = 1
    now = time.time()
    for k, e in takes.items():
        if k not in marked:
            marked[k] = {"marked_at": now, "delay_days": delay,
                         "title": e["title"], "season": e["season"],
                         "year": e.get("year"), "path": e["path"],
                         "size_bytes": e["size_bytes"], "score": e["score"]}
            report["marked_new"] += 1

    if execute:
        # Plan order (worst-kept first), never dict order.
        for e in plan["order"]:
            # Stop means stop: the engine's Stop button fires while THIS loop
            # may be mid-deletion, and the subprocess kill can't reach it.
            if _run_stop_requested.is_set():
                print("TV cleanup: stop requested — remaining seasons left "
                      "for the next pass", flush=True)
                break
            if not e["take"]:
                continue
            k = _tv_mark_key(e)
            m = marked.get(k)
            if not m:
                continue
            # Same clock as a marked movie (shared.delete_on_str): marked
            # today, deletable at the earliest by tomorrow's pass. An
            # unusable stamp ("") holds the season — fail closed, never due.
            if not immediate:
                due_on = shared.delete_on_str(m.get("marked_at") or now,
                                              m.get("delay_days") or delay)
                if not due_on or today < due_on:
                    report["held_by_delay"] += 1
                    continue
            if _delete_tv_season(cfg, e, report):
                del marked[k]
        # The share was stamped from PRE-deletion disk numbers; the engine
        # measures AFTER these deletions and would subtract the freed bytes a
        # second time. Re-stamp with only the share still PENDING (marked,
        # not yet deleted), so the two executors cover the deficit once.
        # A VANISHED season leaves the pending share too: it was priced in at
        # its server-reported size, frees nothing, and its mark is cleared —
        # leaving its claim standing would have the engine subtract bytes no
        # season will ever free.
        if report["freed_bytes"] or report["vanished_bytes"]:
            _stamp_tv_share(max(0, pool["tv_share_bytes"] - report["freed_bytes"]
                                - report["vanished_bytes"]))

    # Same note the engine prints for movies, for the season side's own share:
    # a season is the biggest deletion unit there is, so this is where a small
    # deficit most often costs far more than it asked for.
    _over = shared.overshoot_note(pool["tv_share_bytes"], pool["target_bytes"])
    if _over:
        report["overshoot_note"] = _over
        print(f"TV cleanup: NOTE — the season side {_over}", flush=True)

    _finish()
    if report["deleted_files"] or report["vanished_seasons"]:
        # Sizes changed — or a season the inventory still lists turned out to
        # be gone from disk; either way the stored inventory is stale and
        # would re-plan the same phantom next pass.
        _refresh_tv_inventory(cfg)
    print(f"TV cleanup ({report['mode']}): pool deficit {report['pool_target_gb']} GB "
          f"→ seasons {report['tv_share_gb']} GB, movies {report['movie_share_gb']} GB | "
          f"{report['marked_new']} newly marked, "
          f"{report['unmarked']} unmarked, {report['held_by_delay']} waiting out the delay, "
          f"{len(report['deleted_seasons'])} season(s) deleted "
          f"({report['freed_bytes'] / 1e9:.1f} GB)", flush=True)
    return report


def _tv_log_deleted(entry: dict, path, size_bytes: int) -> None:
    """One deleted.log line per removed episode file — shared.deleted_log_line,
    the same builder the engine appends with, so lifetime totals and the
    dashboard count TV alongside movies from one format."""
    line = shared.deleted_log_line(entry.get("title"), path, size_bytes,
                                   score=entry.get("score"),
                                   media="tv", season=entry.get("season") or 0)
    try:
        deleted_path().parent.mkdir(parents=True, exist_ok=True)
        with open(deleted_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass   # the file is already gone; the record is best-effort


def _tv_season_relpaths(cfg: dict, sid: str, season_n) -> tuple[list, str | None]:
    """The season's episode files as (path RELATIVE to the series folder,
    byte size the server reports or 0), freshly listed by the media server
    that indexed the row (never from the stored inventory). The size lets the
    delete note when the server's metadata is behind the disk; the LOCAL stat
    is the authority for what actually gets freed. Returns (files, error):
    any gap — the server not answering, no series path, a file outside the
    series folder — comes back as an error string and the caller skips the
    season, fail closed."""
    conn = _effective_connection_values(cfg)
    base, files = "", []

    def _size(v):
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    try:
        if sid.startswith("jellyfin:"):
            url, key = conn.get("jellyfin_url"), conn.get("jellyfin_key")
            if not (url and key):
                return [], "the Jellyfin connection that indexed this series is gone"
            jid = sid.split(":", 1)[1]
            info = (_jellyfin_get(url, key, "/Items",
                                  {"Ids": jid, "Fields": "Path"}, timeout=15) or {}).get("Items") or []
            base = str((info[0] if info else {}).get("Path") or "")
            eps = (_jellyfin_get(url, key, f"/Shows/{jid}/Episodes",
                                 {"Fields": "Path,MediaSources"}, timeout=30) or {}).get("Items") or []
            for e in eps:
                if not isinstance(e, dict) or not e.get("Path"):
                    continue
                try:
                    if int(e.get("ParentIndexNumber")) != int(season_n):
                        continue
                except (TypeError, ValueError):
                    continue
                want = next((_size(ms.get("Size")) for ms in e.get("MediaSources") or []
                             if isinstance(ms, dict) and _size(ms.get("Size"))), 0)
                files.append((str(e["Path"]), want))
        elif sid.startswith("plex:"):
            url, token = conn.get("plex_url"), conn.get("plex_token")
            if not (url and token):
                return [], "the Plex connection that indexed this series is gone"
            rk = sid.split(":", 1)[1]
            meta = _plex_get(url, token, f"/library/metadata/{rk}", timeout=15) or {}
            show = next(iter((meta.get("MediaContainer") or {}).get("Metadata") or []), None) or {}
            base = next((str(l.get("path")) for l in _as_list(show.get("Location"))
                         if isinstance(l, dict) and l.get("path")), "")
            leaves = _plex_get(url, token, f"/library/metadata/{rk}/allLeaves", timeout=30) or {}
            for ep in (leaves.get("MediaContainer") or {}).get("Metadata") or []:
                if not isinstance(ep, dict):
                    continue
                try:
                    if int(ep.get("parentIndex")) != int(season_n):
                        continue
                except (TypeError, ValueError):
                    continue
                for m in _as_list(ep.get("Media")):
                    for p in _as_list((m or {}).get("Part")):
                        if isinstance(p, dict) and p.get("file"):
                            files.append((str(p["file"]), _size(p.get("size"))))
            if not base and files:
                parent = PurePosixPath(files[0][0]).parent
                base = str(parent.parent if parent.parent.name else parent)
        else:
            return [], "no usable media-server id"
    except Exception as e:
        return [], f"could not list the season's files ({e})"
    if not base:
        return [], "the server states no path for the series"
    if not files:
        return [], "the server lists no files for this season"
    rels = []
    for f, want in files:
        try:
            rels.append((PurePosixPath(f).relative_to(PurePosixPath(base)), want))
        except ValueError:
            return [], f"unsafe file path from the media server: {f!r}"
    return rels, None


def _delete_tv_season(cfg: dict, entry: dict, report: dict) -> bool:
    """Delete one season: unmonitor it in Sonarr first WHEN Sonarr is
    configured (it is optional and cleanup-only), then remove its episode
    files — freshly listed by the media server that indexed the series — from
    under the resolved series folder, then tidy any now-empty season
    directory and ask Sonarr to rescan. Returns True when the mark is
    finished; False leaves it marked for the next pass. Any doubt — an id
    that doesn't parse, a path that escapes the series folder, a server call
    that fails — skips the season rather than improvising.

    A skip raised part-way through the files still reports what it removed
    (marked `partial`): those episodes are gone whatever the rest of the
    season does, and the run's accounting has to agree with deleted.log."""
    def skip(why: str) -> bool:
        report["skipped"].append({"title": entry.get("title"),
                                  "season": entry.get("season"), "why": why})
        print(f"TV cleanup: SKIP {entry.get('title')} S{entry.get('season')} — {why}",
              flush=True)
        return False

    sid = str(entry.get("sid") or "")
    if ":" not in sid:
        return skip("no usable media-server id")

    series_dir = Path(str(entry.get("path") or ""))
    roots = [str(d) for d in (cfg.get("MONITOR_DIRS") or []) if str(d or "").strip()]
    try:
        if not series_dir.is_dir():
            return skip("series folder is gone")
        series_real = series_dir.resolve()
        root_reals = [Path(d).resolve() for d in roots]
        # STRICTLY inside a monitored dir: a "series folder" that IS a
        # monitored root (a hostile server path, or a flat library whose
        # derived folder is the library itself) would widen every per-file
        # guard below to the whole root.
        if series_real in root_reals:
            return skip("series folder resolves to a monitored directory itself")
        if not any(series_real.is_relative_to(r) for r in root_reals):
            return skip("series folder is not under a monitored directory")
    except OSError as e:
        return skip(f"could not verify the series folder ({e})")

    # 1) Unmonitor the season FIRST, when Sonarr is part of the setup.
    #    Deleting a monitored season just queues its re-download; if Sonarr is
    #    configured but won't take the change, nothing is deleted. No Sonarr
    #    configured = nothing manages downloads, nothing to unmonitor.
    #    SONARR_CLEANUP_ENABLED off skips the step by explicit choice: the
    #    files still delete, and Sonarr will re-download the season if it is
    #    monitored — the user opted to manage monitoring themselves.
    conn_values = _effective_connection_values(cfg)
    s_url = str(conn_values.get("sonarr_url") or "").strip().rstrip("/")
    s_key = str(conn_values.get("sonarr_key") or "").strip()
    s_headers = {"X-Api-Key": s_key}
    sonarr_sid = None
    if not (s_url and s_key):
        pass
    elif not bool(cfg.get("SONARR_CLEANUP_ENABLED", False)):
        print(f"TV cleanup: Sonarr cleanup is off — leaving {entry.get('title')} "
              f"S{entry.get('season')} monitored in Sonarr", flush=True)
    else:
        try:
            series_list = _json_request(f"{s_url}/api/v3/series",
                                        headers=s_headers, timeout=20)
            if not isinstance(series_list, list):
                raise RuntimeError("series list did not answer")
            candidates = [s for s in series_list if isinstance(s, dict)
                          and s.get("id") is not None
                          and _norm_series_title(s.get("title"))
                          == _norm_series_title(entry.get("title"))]
            # Same-title shows are real (Doctor Who 1963/2005): the year
            # disambiguates. A candidate whose known year DIFFERS from ours
            # is a different show; ambiguity that survives the year check is
            # skipped fail-closed rather than unmonitoring a coin-flip.
            entry_year = entry.get("year")
            if entry_year and len(candidates) > 1:
                same_year = [s for s in candidates if s.get("year") == entry_year]
                if same_year:
                    candidates = same_year
            if len(candidates) > 1:
                raise RuntimeError(
                    f"{len(candidates)} Sonarr series share this title — "
                    "cannot tell which to unmonitor")
            match = candidates[0] if candidates else None
            if match is not None and entry_year and match.get("year") \
                    and match["year"] != entry_year:
                # One title match, wrong year: OUR show is not in Sonarr.
                match = None
            if match is None:
                # Not managed by Sonarr — no re-download risk, nothing to do.
                print(f"TV cleanup: {entry.get('title')} is not in Sonarr — "
                      f"nothing to unmonitor", flush=True)
            else:
                sonarr_sid = match["id"]
                found = False
                for s in match.get("seasons") or []:
                    if isinstance(s, dict) and s.get("seasonNumber") == entry.get("season"):
                        s["monitored"] = False
                        found = True
                if not found:
                    raise RuntimeError(f"Sonarr lists no season {entry.get('season')}")
                _json_request(f"{s_url}/api/v3/series/{sonarr_sid}", headers=s_headers,
                              timeout=20, method="PUT", payload=match)
        except Exception as e:
            return skip(f"could not unmonitor the season in Sonarr ({e})")

    # 2) The season's files, as the media server that indexed the series
    #    knows them — relative paths joined to OUR resolved series folder
    #    (the server's absolute paths are in its own container namespace).
    rels, err = _tv_season_relpaths(cfg, sid, entry.get("season"))
    if err:
        return skip(err)

    deleted, freed, dirs = 0, 0, set()

    def abort(why: str) -> bool:
        """Give up on this season, having possibly already removed some of it.

        Files unlinked before the doubt are GONE, and deleted.log already
        carries their lines. Bailing without folding them into the report
        makes the run disagree with its own history: it says 0 GB freed, the
        inventory refresh that keeps stored sizes honest never fires (it is
        gated on deleted_files), and the TV share is not re-stamped, so the
        engine subtracts a share nothing freed. The mark still stays — the
        return is False — so the next pass finishes the season."""
        if deleted:
            report["deleted_seasons"].append({
                "title": entry.get("title"), "season": entry.get("season"),
                "files": deleted, "bytes": freed, "partial": True})
            report["deleted_files"] += deleted
            report["freed_bytes"] += freed
        return skip(why)

    for rp, want in rels:
        if not rp.parts or rp.is_absolute() or any(p in ("..", ".") for p in rp.parts):
            return abort(f"unsafe file path from the media server: {str(rp)!r}")
        target = series_dir.joinpath(*rp.parts)
        try:
            if target.is_symlink():
                return abort(f"refusing a symlink: {target}")
            if not target.is_file():
                continue   # already gone — nothing to remove
            if not target.resolve().is_relative_to(series_real):
                return abort(f"file escapes the series folder: {target}")
            size = target.stat().st_size
            # The LOCAL stat is the size authority (the file's identity is its
            # path under the series folder): a file the server describes at
            # different bytes was replaced since the server's last scan — note
            # it and delete at the size actually on disk, which is what the
            # freed accounting and the history line carry.
            if want and size != want:
                print(f"TV cleanup: {target.name} is {size} bytes on disk, "
                      f"{want} in the server's metadata — deleting at the "
                      f"on-disk size (server metadata is behind)", flush=True)
            target.unlink()
        except OSError as e:
            return abort(f"could not delete {target} ({e})")
        deleted += 1
        freed += size
        dirs.add(target.parent)
        _tv_log_deleted(entry, target, size)

    # 3) A season folder emptied by the deletions is removed; rmdir refuses a
    #    non-empty directory, so this can never take anything but the empty
    #    shell. The series folder itself always stays — the media server and
    #    Sonarr both key on it.
    for d in sorted(dirs, key=lambda p: len(str(p)), reverse=True):
        if d != series_dir:
            try:
                d.rmdir()
            except OSError:
                pass

    # 4) Best-effort: have Sonarr rescan so its size/episode facts follow
    #    (only when the unmonitor step found the series there).
    if sonarr_sid is not None:
        try:
            _json_request(f"{s_url}/api/v3/command", headers=s_headers, timeout=15,
                          method="POST",
                          payload={"name": "RescanSeries", "seriesId": sonarr_sid})
        except Exception:
            pass

    if not deleted:
        # Every file was already gone: the season vanished outside this pass
        # (removed by hand, or the server's layout no longer matches disk).
        # NOT a deletion — reporting it as one ("DELETED … 0 file(s)") once
        # let this repeat forever: freed_bytes stayed 0, so the share was
        # never re-stamped and the inventory never refreshed, and the season
        # re-entered every plan claiming its stale server size while the
        # movie side under-covered the deficit by exactly that much, daily.
        # The mark still clears (the files ARE gone); the vanished counters
        # are what makes the caller re-stamp and refresh.
        report["vanished_seasons"] += 1
        report["vanished_bytes"] += max(0, int(entry.get("size_bytes") or 0))
        print(f"TV cleanup: {entry.get('title')} S{entry.get('season')} — every "
              "file already gone on disk (removed outside MediaReducer); "
              "clearing the mark and refreshing the inventory", flush=True)
        return True
    report["deleted_seasons"].append({"title": entry.get("title"),
                                      "season": entry.get("season"),
                                      "files": deleted, "bytes": freed})
    report["deleted_files"] += deleted
    report["freed_bytes"] += freed
    print(f"TV cleanup: DELETED {entry.get('title')} S{entry.get('season')} — "
          f"{deleted} file(s), {freed / 1e9:.1f} GB", flush=True)
    return True


def _autodetected_connection_field_values() -> dict:
    """One-shot appdata detections mapped to Config form field names —
    credentials only, since URLs are default-driven and the Auto Detect
    button never fills them."""
    conn = _autodetected_connection_values()
    return {field: str(conn.get(key) or "")
            for field, key in CONNECTION_FORM_FIELD_MAP.items()
            if not field.endswith("_URL")}


def _json_request(url: str, headers: dict | None = None, timeout: int = 15,
                  method: str | None = None, payload=None):
    """One JSON round-trip. GET by default; pass method/payload for the write
    calls (Sonarr season unmonitor, rescan command). An empty response body
    reads as None."""
    hdrs = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def _appdata_marker(path: str, *markers: str) -> dict:
    """Return mount/marker status for a Docker appdata volume."""
    base = Path(path)
    mounted = False
    try:
        mounted = base.exists()
    except OSError:
        mounted = False
    found = _find_appdata_file(base, *markers) if mounted else None
    return {
        "mounted": bool(mounted),
        "ok": bool(found),
        "path": str(found) if found else None,
    }


def _appdata_mount_state() -> dict:
    """Fast appdata marker check shared by dashboard/config health checks."""
    library = Path(FILESYSTEM_CHECK_PATH)
    try:
        library_ok = library.exists() and any(library.iterdir())
    except OSError:
        library_ok = False
    return {
        "tautulli":  _appdata_marker(TAUTULLI_APPDATA_DIR, "config.ini", "tautulli.db"),
        "radarr":    _appdata_marker(RADARR_APPDATA_DIR, "config.xml"),
        "sonarr":    _appdata_marker(SONARR_APPDATA_DIR, "config.xml"),
        "library":   {"mounted": library.exists(), "ok": library_ok, "path": FILESYSTEM_CHECK_PATH if library_ok else None},
    }


def _probe_json(url: str, headers: dict | None = None, timeout: int = 5) -> tuple[bool, str]:
    """Small JSON HTTP probe used by the Config health check."""
    try:
        _json_request(url, headers=headers, timeout=timeout)
        return True, "reachable"
    except Exception as e:
        return False, str(e)


def _settled(call):
    """A call's value, or the exception it raised — never raises itself."""
    try:
        return call()
    except Exception as e:
        return e


# A media-path sample is only meaningful once its server has answered, so it is
# started with the health probe but collected only if that probe succeeded.
_PROBE_SAMPLE_DEPENDS_ON = {"tautulli_paths": "tautulli", "jellyfin_paths": "jellyfin"}


def _prefetch_connection_probes(conn: dict, *, use_plex: bool, use_jellyfin: bool,
                                media_paths: bool = True) -> dict:
    """Every network call the connection check needs, fetched concurrently.

    Gated exactly as the checks below are, so nothing is requested that the
    sequential version wouldn't have. The two media-path samples are the one
    exception: they normally wait on their server's health probe passing, but
    they are read-only GETs against the same host, so they are STARTED
    alongside it and only COLLECTED if that probe succeeded (see below) — which
    keeps the healthy case to one round of waiting without letting a dead host
    hold the check open for the sampler's longer timeout."""
    jobs = {}
    if use_plex:
        if conn.get("tautulli_url") and conn.get("tautulli_key"):
            url = (f"{conn['tautulli_url'].rstrip('/')}/api/v2"
                   f"?apikey={conn['tautulli_key']}&cmd=get_libraries")
            jobs["tautulli"] = lambda: _probe_json(url, timeout=6)
            if media_paths:
                jobs["tautulli_paths"] = lambda: _sample_tautulli_media_paths(conn)
        if conn.get("plex_url") and conn.get("plex_token"):
            purl = (f"{conn['plex_url'].rstrip('/')}/library/sections"
                    f"?X-Plex-Token={conn['plex_token']}")
            jobs["plex"] = lambda: _probe_json(
                purl, headers={"Accept": "application/json"}, timeout=6)
    if use_jellyfin and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        jf_key = conn["jellyfin_key"]
        jurl = f"{conn['jellyfin_url'].rstrip('/')}/System/Info"
        jobs["jellyfin"] = lambda: _probe_json(
            jurl,
            headers={"Authorization": f'MediaBrowser Token="{jf_key}"', "X-Emby-Token": jf_key},
            timeout=6)
        if media_paths:
            jobs["jellyfin_paths"] = lambda: _sample_jellyfin_media_paths(conn)
    if conn.get("radarr_url") and conn.get("radarr_key"):
        rurl = f"{conn['radarr_url'].rstrip('/')}/api/v3/system/status"
        rkey = conn["radarr_key"]
        jobs["radarr"] = lambda: _probe_json(rurl, headers={"X-Api-Key": rkey}, timeout=6)
    if conn.get("sonarr_url") and conn.get("sonarr_key"):
        surl = f"{conn['sonarr_url'].rstrip('/')}/api/v3/system/status"
        skey = conn["sonarr_key"]
        jobs["sonarr"] = lambda: _probe_json(surl, headers={"X-Api-Key": skey}, timeout=6)
    if not jobs:
        return {}

    # Two phases, because the samples are CONDITIONAL. Everything is started at
    # once, but only the health probes are waited on unconditionally; a sample is
    # collected only once its own server has answered. That matters because the
    # samplers allow a longer timeout than the health probes do (a big library
    # legitimately takes longer than /System/Info), so blocking on a sample whose
    # host has just proved unreachable made an outage SLOWER than checking in
    # sequence — 10s against a hung server where the sequential version took 6s,
    # for a result that was then thrown away. Abandoned requests are left to
    # expire on their own socket timeout; nothing reads them.
    out = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs),
                                                 thread_name_prefix="probe")
    try:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            if name not in _PROBE_SAMPLE_DEPENDS_ON:
                out[name] = _settled(fut.result)
        for name, health_name in _PROBE_SAMPLE_DEPENDS_ON.items():
            fut = futures.get(name)
            answered = out.get(health_name)
            if fut is not None and isinstance(answered, tuple) and answered[0]:
                out[name] = _settled(fut.result)
    finally:
        pool.shutdown(wait=False)
    return out


def _probe_result(pre: dict, name: str) -> tuple[bool, str]:
    """A prefetched _probe_json result, with a failed job reading as unreachable."""
    got = pre.get(name)
    if isinstance(got, tuple):
        return got
    return False, str(got) if got is not None else "not probed"


def _sample_result(pre: dict, name: str) -> list:
    """A prefetched media-path sample; a failed job re-raises for the caller's
    own try/except, which is what decides the warning wording."""
    got = pre.get(name)
    if isinstance(got, BaseException):
        raise got
    return got or []


_MEDIA_FP_INDEX = {"at": 0.0, "dir_name": {}, "name_size": {}}
_MEDIA_FP_LOCK = threading.Lock()

# The media-path check's LAST verdict, replayed into probes that don't re-run
# it. The check itself (server samples + a full library walk) runs only when
# the user clicks Check for Errors or a save changes the monitored paths —
# never on an ordinary save, even while its error is active; the stored
# verdict (its error/warning lines and blocker state) simply carries forward
# until one of those two triggers re-judges the current disk. PER SERVER:
# a slot is replaced only when that server was actually judged (a sampler
# failure or a down server never wipes an active verdict) and replayed only
# while its server stays enabled (deselecting Jellyfin retires its verdict).
_MEDIA_PATH_VERDICT = {"sig": None, "servers": {}}


def _monitor_dirs_signature(cfg: dict | None = None) -> str:
    """Normalization-invariant signature of the monitored paths: the SAME
    normalization a save applies, deduped and sorted — a posted form whose
    dirs normalize to the saved value must never read as a change (that would
    re-probe, and re-judge the media paths, on saves that changed nothing)."""
    cfg = cfg or load_config()
    dirs = {_normalize_library_path(d) for d in (cfg.get("MONITOR_DIRS") or [])
            if str(d or "").strip()}
    return json.dumps(sorted(d for d in dirs if d))


def _media_fingerprint_index() -> dict:
    """Fingerprint index of the library mount for the compatibility check and
    the path-debug endpoints, mirroring the engine's per-run index:
    (parent-folder name, filename) -> [paths] and (filename, byte size) ->
    [paths]. Cached briefly (30 s) so one Check for Errors walks the library
    once, while a re-check after a mount fix never reads a stale walk."""
    # One walk at a time, and check-then-build under the lock: without it a
    # slow concurrent walk could publish a PRE-fix view over a fresh reset.
    with _MEDIA_FP_LOCK:
        now = time.time()
        if now - _MEDIA_FP_INDEX["at"] > 30:
            by_dir_name, by_name_size = {}, {}
            _visited = set()
            try:
                # followlinks: a library assembled from symlinked subtrees is
                # a normal layout; the visited-realpath guard stops cycles.
                for dirpath, dirnames, files in os.walk(FILESYSTEM_CHECK_PATH,
                                                        followlinks=True):
                    try:
                        _rp = os.path.realpath(dirpath)
                    except OSError:
                        dirnames[:] = []
                        continue
                    if _rp in _visited:
                        dirnames[:] = []
                        continue
                    _visited.add(_rp)
                    parent = Path(dirpath).name.lower()
                    for fn in files:
                        p = Path(dirpath) / fn
                        by_dir_name.setdefault((parent, fn.lower()), []).append(p)
                        try:
                            by_name_size.setdefault((fn.lower(), p.stat().st_size), []).append(p)
                        except OSError:
                            continue
            except Exception:
                by_dir_name, by_name_size = {}, {}
            _MEDIA_FP_INDEX.update({"at": now, "dir_name": by_dir_name,
                                    "name_size": by_name_size})
        return _MEDIA_FP_INDEX


def _resolve_media_path_explained(path_str: str | None, expected_size=0):
    """(resolved Path|None, how/why) — the fingerprint ladder mirroring the
    engine's resolve_under_library, with the reason spelled out so the debug
    endpoints can SHOW why a path did or didn't match: mount layouts never
    need to agree on prefixes, the server's byte count only disambiguates
    same-named copies (it never rejects a folder+filename match; the local
    file is the size authority), a unique (filename, size) hit rescues a path
    whose folder finds nothing. Folder paths are NOT resolved — the check is
    about movie FILE entries (name + size), never about path layouts. A path
    already under the library shortcut-checks existence."""
    if not path_str:
        return None, "empty path"
    raw = str(path_str).strip().replace("\\", "/")
    if not raw:
        return None, "empty path"
    library_root = Path(FILESYSTEM_CHECK_PATH)
    library_s = str(library_root)

    if raw == library_s or raw.startswith(library_s + "/"):
        rel = raw[len(library_s):].lstrip("/")
        candidate = library_root / rel if rel else library_root
        if candidate.exists():
            return candidate, f"already under {library_s}"
        # Nothing at the stated nesting: fall through to the fingerprint
        # ladder like any other prefix — sharing our mount doesn't exempt a
        # server's stale layout from folder+name matching.

    try:
        expected = int(expected_size) if expected_size else 0
    except (TypeError, ValueError):
        expected = 0
    parts = [part for part in raw.split("/") if part]
    if len(parts) < 2:
        return None, "a bare name with no folder is never matched (it could be a different film)"
    idx = _media_fingerprint_index()
    pair = (parts[-2].lower(), parts[-1].lower())
    hits = idx["dir_name"].get(pair) or []
    if len(hits) == 1:
        # The size never rejects a folder+name match, but it can point at a
        # BETTER one: with a flat server layout the "folder" is the server's
        # mount-root name, and a size-exact file elsewhere outranks a
        # same-named file under a merely look-alike folder.
        if expected > 0:
            try:
                if hits[0].stat().st_size != expected:
                    by_size = idx["name_size"].get((parts[-1].lower(), expected)) or []
                    if len(by_size) == 1 and by_size[0] != hits[0]:
                        return by_size[0], ("matched by file name + exact size "
                                            "(a same-named file under a look-alike "
                                            "folder was passed over)")
            except OSError:
                pass
        return hits[0], "matched by folder + file name"
    if len(hits) > 1:
        if expected <= 0:
            return None, (f"{len(hits)} same-named files and the server reported "
                          "no size to pick between them")
        sized = []
        for p in hits:
            try:
                if p.stat().st_size == expected:
                    sized.append(p)
            except OSError:
                continue
        if len(sized) == 1:
            return sized[0], (f"matched by folder + file name, size-disambiguated "
                              f"among {len(hits)} same-named copies")
        if not sized:
            return None, (f"{len(hits)} same-named files, none at the reported "
                          f"{expected} bytes")
        return None, f"{len(sized)} same-named files at the same size — never guessed between"
    if expected > 0:
        by_size = idx["name_size"].get((parts[-1].lower(), expected)) or []
        if len(by_size) == 1:
            return by_size[0], "matched by file name + size (no folder-name match)"
    return None, (f"no file named {parts[-1]!r} (in a folder named "
                  f"{parts[-2]!r}) found under {library_s}")


def _resolve_reported_media_path(path_str: str | None, expected_size=0) -> Path | None:
    """The fingerprint resolution alone — see _resolve_media_path_explained."""
    return _resolve_media_path_explained(path_str, expected_size)[0]


def _extract_media_paths_from_item(item: dict | None) -> list[tuple[str, int]]:
    """(path, server-reported byte size|0) pairs from a server row shape.
    The size rides along as the resolution fingerprint and the stale-metadata
    tripwire; a media/part entry's own size describes ITS file more precisely
    than the row total, so the nearest enclosing size wins."""
    paths: list[tuple[str, int]] = []
    if not isinstance(item, dict):
        return paths

    def _size_of(d: dict, fallback: int = 0) -> int:
        for k in ("file_size", "Size", "size"):
            try:
                v = int(d.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return fallback

    item_size = _size_of(item)
    for key in ("file", "file_path", "media_file", "location", "path", "Path"):
        value = item.get(key)
        if value:
            paths.append((str(value), item_size))

    containers = []
    for key in ("media_info", "Media", "MediaSources"):
        value = item.get(key)
        if isinstance(value, list):
            containers.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            containers.append(value)

    for media in containers:
        media_size = _size_of(media, item_size)
        for key in ("file", "file_path", "media_file", "location", "path", "Path"):
            value = media.get(key)
            if value:
                paths.append((str(value), media_size))
        for parts_key in ("parts", "Part"):
            for part in _as_list(media.get(parts_key)):
                if not isinstance(part, dict):
                    continue
                part_size = _size_of(part, media_size)
                for key in ("file", "file_path", "media_file", "location", "path", "Path"):
                    value = part.get(key)
                    if value:
                        paths.append((str(value), part_size))

    out = []
    seen = {}
    for path, size in paths:
        path = str(path).strip()
        if not path:
            continue
        if path in seen:
            # The same path often appears twice — the item level (which for
            # Jellyfin carries NO size) and again inside MediaSources (which
            # does). Keep the first position but take the first REAL size, or
            # the sizeless item entry would shadow it and starve the size
            # tripwire and the same-name disambiguation of their fingerprint.
            i = seen[path]
            if size and not out[i][1]:
                out[i] = (path, size)
            continue
        seen[path] = len(out)
        out.append((path, size))
    return out


def _tautulli_api_request(conn: dict, cmd: str, timeout: int = 8, **params):
    values = dict(params)
    values.update({"apikey": conn["tautulli_key"], "cmd": cmd})
    url = f"{conn['tautulli_url'].rstrip('/')}/api/v2?{urlencode(values)}"
    payload = _json_request(url, timeout=timeout)
    response = (payload or {}).get("response") or {}
    if response.get("result") != "success":
        raise RuntimeError(f"Tautulli API error: {payload}")
    return response.get("data")


def _sample_tautulli_media_paths(conn: dict, limit: int = 12) -> list[tuple[str, int]]:
    paths: list[str] = []
    libraries = _tautulli_api_request(conn, "get_libraries", timeout=8) or []
    section_ids = [
        lib.get("section_id") for lib in libraries
        if isinstance(lib, dict) and lib.get("section_type") == "movie" and lib.get("is_active", 1)
    ]
    for section_id in section_ids:
        data = _tautulli_api_request(
            conn,
            "get_library_media_info",
            timeout=10,
            section_id=section_id,
            section_type="movie",
            start=0,
            length=25,
            order_column="title",
            order_dir="asc",
        )
        rows = data.get("data", data if isinstance(data, list) else []) if data is not None else []
        for row in rows:
            paths.extend(_extract_media_paths_from_item(row))
            if len(paths) >= limit:
                return paths[:limit]
        for row in rows[: min(len(rows), 8)]:
            rating_key = row.get("rating_key") if isinstance(row, dict) else None
            if not rating_key:
                continue
            meta = _tautulli_api_request(conn, "get_metadata", timeout=10, rating_key=rating_key) or {}
            paths.extend(_extract_media_paths_from_item(meta))
            if len(paths) >= limit:
                return paths[:limit]
    return paths[:limit]


def _sample_jellyfin_media_paths(conn: dict, limit: int = 12) -> list[tuple[str, int]]:
    data = _jellyfin_get(
        conn["jellyfin_url"],
        conn["jellyfin_key"],
        "Items",
        {
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Fields": "Path,MediaSources",
            "Limit": max(limit, 25),
        },
        timeout=10,
    ) or {}
    paths: list[str] = []
    for item in _jellyfin_items_from_payload(data):
        paths.extend(_extract_media_paths_from_item(item))
        if len(paths) >= limit:
            break
    return paths[:limit]


def _media_file_extensions(cfg: dict | None = None) -> set:
    """The configured movie-file extension set, lowercased — what decides
    whether a server-reported path is a media FILE entry at all."""
    cfg = cfg or load_config()
    exts = set()
    for e in (cfg.get("MOVIE_EXTENSIONS") or []):
        e = str(e).strip().lower()
        if e:
            exts.add(e if e.startswith(".") else "." + e)
    return exts


def _is_media_file_path(raw, exts: set) -> bool:
    return PurePosixPath(str(raw).replace("\\", "/")).suffix.lower() in exts


def _media_path_compatibility_state(server_name: str, paths: list) -> dict:
    """Resolve each sampled movie FILE entry (name + size) by fingerprint and
    verify the bytes on disk against what the server reports. Folder-shaped
    paths the servers also hand out (section locations, folder items) are not
    part of the check at all — the check is about the media a run would act
    on, never about path layouts. A few mismatches are normal (quality
    upgrades the server hasn't rescanned; the local file is the size
    authority) — but MOST samples mismatching is the wrong-library tripwire
    (shared.wrong_library_problem, the same rule the engine pre-check fails a
    run on), and it fails this check."""
    exts = _media_file_extensions()
    pairs = [s if isinstance(s, tuple) else (s, 0) for s in paths]
    folder_entries = sum(1 for r, _w in pairs if not _is_media_file_path(r, exts))
    pairs = [(r, w) for r, w in pairs if _is_media_file_path(r, exts)]
    resolved, unresolved, mismatches = [], [], []
    size_checked = 0
    for raw, want in pairs:
        match = _resolve_reported_media_path(raw, expected_size=want)
        if not match:
            unresolved.append(raw)
            continue
        resolved.append({"reported": raw, "resolved": str(match)})
        if want and match.is_file():
            try:
                disk = match.stat().st_size
            except OSError:
                continue
            size_checked += 1
            if disk != want:
                mismatches.append({"reported": raw, "resolved": str(match),
                                   "reported_bytes": want, "disk_bytes": disk})
    mismatch_problem = shared.wrong_library_problem(size_checked, len(mismatches))
    # A FEW unmatched entries are stale server rows (files renamed, upgraded,
    # or removed since its last scan) — they scan as missing and are never
    # deleted, so they warn; MOST samples unmatched is the wrong library (or
    # none), and that fails the check.
    unmatched_problem = shared.wrong_library_problem(len(pairs), len(unresolved))
    return {
        "server": server_name,
        "checked": len(pairs),
        "folder_entries": folder_entries,
        "matched": len(resolved),
        "unmatched": len(unresolved),
        "unmatched_problem": unmatched_problem,
        "size_checked": size_checked,
        "size_mismatched": len(mismatches),
        "mismatch_problem": mismatch_problem,
        "ok": (not unmatched_problem and not mismatch_problem) if pairs else True,
        "resolved_examples": resolved[:3],
        "unmatched_examples": unresolved[:5],
        "mismatch_examples": mismatches[:5],
    }


def _plex_collection_names(url: str, token: str, timeout: int = 8) -> list[str]:
    """All Plex collection titles across movie library sections (sorted).

    Plex returns section and collection lists under either ``Directory`` or
    ``Metadata`` depending on version/endpoint, so we accept either. If no
    section reports type ``movie`` we fall back to scanning every section.
    """
    base = url.rstrip("/")

    def _get(path):
        sep = "&" if "?" in path else "?"
        return _json_request(f"{base}{path}{sep}X-Plex-Token={token}",
                             headers={"Accept": "application/json"}, timeout=timeout)

    def _items(container):
        mc = (container or {}).get("MediaContainer") or {}
        items = mc.get("Directory")
        if not items:
            items = mc.get("Metadata") or []
        return [items] if isinstance(items, dict) else (items or [])

    sections = _items(_get("/library/sections"))
    section_keys = [s.get("key") for s in sections if s.get("type") == "movie" and s.get("key")]
    if not section_keys:  # fall back to all sections if type detection is off
        section_keys = [s.get("key") for s in sections if s.get("key")]

    names = set()
    for key in section_keys:
        for coll in _items(_get(f"/library/sections/{key}/collections")):
            title = str(coll.get("title") or "").strip()
            if title:
                names.add(title)
    return sorted(names, key=str.lower)


def _debug_collection_names(raw) -> list[str]:
    if isinstance(raw, str):
        raw = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
    return [str(n).strip() for n in (raw or []) if str(n).strip()]


def _plex_get(url: str, token: str, path: str, timeout: int = 15):
    sep = "&" if "?" in path else "?"
    return _json_request(
        f"{url.rstrip('/')}{path}{sep}X-Plex-Token={token}",
        headers={"Accept": "application/json"},
        timeout=timeout,
    )


def _plex_items(container):
    mc = (container or {}).get("MediaContainer") or {}
    items = mc.get("Directory")
    if not items:
        items = mc.get("Metadata") or []
    return [items] if isinstance(items, dict) else (items or [])


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _plex_debug_movie(item: dict) -> dict:
    files = []
    for media in _as_list(item.get("Media")):
        for part in _as_list(media.get("Part")):
            if isinstance(part, dict):
                files.append({
                    "id": part.get("id"),
                    "key": part.get("key"),
                    "file": part.get("file"),
                    "size": part.get("size"),
            })
    guids = []
    for guid in _as_list(item.get("Guid")):
        if isinstance(guid, dict) and guid.get("id"):
            guids.append(guid.get("id"))
    return {
        "title": item.get("title"),
        "type": item.get("type"),
        "year": item.get("year"),
        "rating_key": item.get("ratingKey"),
        "key": item.get("key"),
        "guid": item.get("guid"),
        "guids": guids,
        "files": files,
    }


@app.route("/api/collections/plex/debug", methods=["POST"])
def api_plex_collection_debug():
    """Show what Plex returns for the selected protected collections."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("plex_url") and conn.get("plex_token")):
        return jsonify({"ok": False, "error": "Add the Plex URL and token above and Save, then debug."}), 400

    data = request.get_json(silent=True) or {}
    names = _debug_collection_names(data.get("names") or [])
    if not names:
        return jsonify({"ok": False, "error": "Tick at least one Plex collection first."}), 400
    wanted = {n.lower() for n in names}

    url, token = conn["plex_url"], conn["plex_token"]
    try:
        section_payload = _plex_get(url, token, "/library/sections", timeout=15)
        sections = _plex_items(section_payload)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Could not list Plex sections: {e}"}), 400

    movie_sections = [s for s in sections if s.get("type") == "movie" and s.get("key")]
    section_source = "movie sections"
    if not movie_sections:
        movie_sections = [s for s in sections if s.get("key")]
        section_source = "all sections fallback"

    section_attempts = []
    matched = []
    seen_collection_keys = set()
    for section in movie_sections:
        section_key = section.get("key")
        endpoint = f"/library/sections/{section_key}/collections"
        try:
            payload = _plex_get(url, token, endpoint, timeout=15)
            collections = _plex_items(payload)
            error = ""
        except Exception as e:
            collections = []
            error = str(e)
        section_attempts.append({
            "section_key": section_key,
            "section_title": section.get("title"),
            "section_type": section.get("type"),
            "endpoint": endpoint,
            "count": len(collections),
            "error": error,
            "names": [str(c.get("title") or "") for c in collections[:100]],
            "truncated": len(collections) > 100,
        })
        for coll in collections:
            title = str(coll.get("title") or "").strip()
            coll_key = coll.get("ratingKey") or coll.get("key")
            dedupe_key = str(coll_key or "") + "|" + title.lower()
            if title.lower() in wanted and coll_key and dedupe_key not in seen_collection_keys:
                seen_collection_keys.add(dedupe_key)
                matched.append({
                    "section_key": section_key,
                    "section_title": section.get("title"),
                    "title": title,
                    "rating_key": coll.get("ratingKey"),
                    "key": coll.get("key"),
                    "collection_key": coll_key,
                    "type": coll.get("type"),
                })

    collections = []
    for coll in matched:
        coll_key = coll.get("collection_key")
        child_attempts = []
        for endpoint in (f"/library/collections/{coll_key}/children", f"/library/metadata/{coll_key}/children"):
            try:
                payload = _plex_get(url, token, endpoint, timeout=20)
                children = _plex_items(payload)
                error = ""
            except Exception as e:
                children = []
                error = str(e)
            child_attempts.append({
                "endpoint": endpoint,
                "count": len(children),
                "error": error,
                "items": [_plex_debug_movie(i) for i in children[:100]],
                "truncated": len(children) > 100,
            })
        coll = dict(coll)
        coll["child_attempts"] = child_attempts
        collections.append(coll)

    return jsonify({
        "ok": True,
        "selected": names,
        "section_source": section_source,
        "sections": [
            {"key": s.get("key"), "title": s.get("title"), "type": s.get("type")}
            for s in sections
        ],
        "section_attempts": section_attempts,
        "matched_count": len(collections),
        "collections": collections,
    })


def _jellyfin_boxset_names(url: str, key: str, timeout: int = 8) -> list[str]:
    """All Jellyfin BoxSet (collection) names (sorted)."""
    data = _json_request(
        f"{url.rstrip('/')}/Items?IncludeItemTypes=BoxSet&Recursive=true",
        headers={"Authorization": f'MediaBrowser Token="{key}"', "X-Emby-Token": key, "Accept": "application/json"},
        timeout=timeout,
    )
    items = (data or {}).get("Items") or []
    names = {str(i.get("Name") or "").strip() for i in items if i.get("Name")}
    return sorted(names, key=str.lower)


def _jellyfin_headers(key: str) -> dict:
    return {
        "Authorization": f'MediaBrowser Token="{key}"',
        "X-Emby-Token": key,
        "Accept": "application/json",
    }


def _jellyfin_get(url: str, key: str, path: str, params: dict | None = None, timeout: int = 15):
    query = ("?" + urlencode(params)) if params else ""
    return _json_request(
        f"{url.rstrip('/')}/{path.lstrip('/')}{query}",
        headers=_jellyfin_headers(key),
        timeout=timeout,
    )


def _jellyfin_debug_item(item: dict) -> dict:
    media_sources = []
    for ms in (item.get("MediaSources") or [])[:4]:
        if isinstance(ms, dict):
            media_sources.append({
                "id": ms.get("Id"),
                "path": ms.get("Path"),
                "size": ms.get("Size"),
            })
    return {
        "name": item.get("Name"),
        "id": item.get("Id"),
        "item_id": item.get("ItemId"),
        "movie_id": item.get("MovieId"),
        "type": item.get("Type"),
        "path": item.get("Path"),
        "parent_id": item.get("ParentId"),
        "collection_type": item.get("CollectionType"),
        "provider_ids": item.get("ProviderIds") or {},
        "media_sources": media_sources,
    }


def _jellyfin_items_from_payload(payload):
    items = payload.get("Items") if isinstance(payload, dict) else payload
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    return []


@app.route("/api/collections/jellyfin/debug", methods=["POST"])
def api_jellyfin_collection_debug():
    """Show what Jellyfin returns for the selected protected collections."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("jellyfin_url") and conn.get("jellyfin_key")):
        return jsonify({"ok": False, "error": "Add the Jellyfin URL and API key above and Save, then debug."}), 400

    data = request.get_json(silent=True) or {}
    names = _debug_collection_names(data.get("names") or [])
    if not names:
        return jsonify({"ok": False, "error": "Tick at least one Jellyfin collection first."}), 400
    wanted = {n.lower() for n in names}

    url, key = conn["jellyfin_url"], conn["jellyfin_key"]
    fields = "Path,MediaSources,ProviderIds,ParentId,DateCreated"

    try:
        users_payload = _jellyfin_get(url, key, "Users", timeout=10) or []
    except Exception as e:
        users_payload = []
        users_error = str(e)
    else:
        users_error = ""
    users = [u for u in users_payload if isinstance(u, dict)]
    user_ids = [u.get("Id") for u in users if u.get("Id")]
    user_id = user_ids[0] if user_ids else None

    boxset_attempts = []
    boxsets_by_id = {}
    for path in ([f"Users/{user_id}/Items"] if user_id else []) + ["Items"]:
        params = {"IncludeItemTypes": "BoxSet", "Recursive": "true"}
        try:
            payload = _jellyfin_get(url, key, path, params, timeout=15) or {}
            items = _jellyfin_items_from_payload(payload)
            error = ""
        except Exception as e:
            items = []
            error = str(e)
        for item in items:
            if item.get("Id"):
                boxsets_by_id.setdefault(item.get("Id"), item)
        boxset_attempts.append({
            "endpoint": path,
            "params": params,
            "count": len(items),
            "error": error,
            "names": [str(i.get("Name") or "") for i in items[:100]],
            "truncated": len(items) > 100,
        })

    matched = [
        item for item in boxsets_by_id.values()
        if str(item.get("Name") or "").strip().lower() in wanted and item.get("Id")
    ]

    collections = []
    for box in sorted(matched, key=lambda b: str(b.get("Name") or "").lower()):
        box_id = box.get("Id")
        child_attempts = []
        # Parity attempts: EXACTLY the queries the engine's _jellyfin_boxset_children
        # runs (same endpoints, params, Fields string, order). Some Jellyfin builds
        # enumerate a BoxSet through only one query form, so a faithful mirror is the
        # only way the debug output reflects what a real run sees.
        engine_fields = "Path,MediaSources,ProviderIds"
        attempts = []  # (path, params, is_engine_parity)
        if user_id:
            attempts.append((f"Collections/{box_id}/Items", {"UserId": user_id, "IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
            attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
            attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Fields": engine_fields}, True))
        attempts.append((f"Collections/{box_id}/Items", {"IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
        attempts.append(("Items", {"ParentId": box_id, "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": engine_fields}, True))
        attempts.append(("Items", {"ParentId": box_id, "Fields": engine_fields}, True))
        # Extra query forms the engine does not run, probed for diagnostics.
        if user_id:
            attempts.append((f"Collections/{box_id}/Items", {"UserId": user_id, "Fields": fields}, False))
            attempts.append((f"Users/{user_id}/Items", {"ParentId": box_id, "Recursive": "true", "Fields": fields}, False))
        attempts.append((f"Collections/{box_id}/Items", {"Fields": fields}, False))
        attempts.append(("Items", {"ParentId": box_id, "Recursive": "true", "Fields": fields}, False))

        for path, params, is_engine in attempts:
            try:
                payload = _jellyfin_get(url, key, path, params, timeout=20) or {}
                items = _jellyfin_items_from_payload(payload)
                error = ""
            except Exception as e:
                items = []
                error = str(e)
            child_attempts.append({
                "endpoint": path,
                "params": params,
                "engine": is_engine,
                "count": len(items),
                "error": error,
                "items": [_jellyfin_debug_item(i) for i in items[:100]],
                "truncated": len(items) > 100,
            })

        collections.append({
            "name": box.get("Name"),
            "id": box_id,
            "type": box.get("Type"),
            "child_attempts": child_attempts,
        })

    return jsonify({
        "ok": True,
        "selected": names,
        "users": [{"id": u.get("Id"), "name": u.get("Name")} for u in users],
        "users_error": users_error,
        "using_user_id": user_id,
        "boxset_attempts": boxset_attempts,
        "matched_count": len(collections),
        "collections": collections,
    })


# ── API debug endpoints (debug mode) ─────────────────────────────────────────
# Each mirrors the engine's real API calls (same commands, params, extraction) so
# the output shows what a run will actually see. All return
# {"ok": True, "text": "..."}; preformatted, secrets never echoed.

_DEBUG_SAMPLE_ROWS = 25
_DEBUG_SAMPLE_META = 3


def _debug_resolved_line(raw, monitor_dirs, want=0):
    """One 'raw -> resolved' line for path debugging, stating HOW the
    fingerprint matched (or exactly why it didn't), plus monitored-dir status
    and whether the disk's bytes agree with the server's metadata. A
    folder-shaped entry says so — those are not part of the accuracy check."""
    if not _is_media_file_path(raw, _media_file_extensions()):
        return (f"      {raw}\n        -> a folder path (no media file "
                "extension) — not a movie file entry, ignored by the "
                "accuracy check")
    resolved, why = _resolve_media_path_explained(raw, expected_size=want)
    if resolved is None:
        return f"      {raw}\n        -> NO MATCH: {why}"
    rs = str(resolved)
    monitored = any(rs == d or rs.startswith(d.rstrip("/") + "/") for d in monitor_dirs)
    line = (f"      {raw}\n        -> {rs}\n        {why} | "
            f"monitored={'yes' if monitored else 'NO'}")
    if want and resolved.is_file():
        try:
            disk = resolved.stat().st_size
            line += (" | size=verified" if disk == int(want)
                     else f" | size=DIFFERS (disk {disk}, server {want} — server metadata behind?)")
        except OSError:
            pass
    return line


@app.route("/api/debug/tautulli/movies", methods=["POST"])
def api_debug_tautulli_movies():
    """Mirror the engine's Tautulli movie-source calls: get_libraries ->
    get_library_media_info -> get_metadata (guids/collections + file paths)."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("tautulli_url") and conn.get("tautulli_key")):
        return jsonify({"ok": False, "error": "Add the Tautulli URL and API key above and Save, then debug."}), 400

    protected_names = set(cfg.get("PROTECTED_COLLECTIONS") or [])
    lines = ["Tautulli movie-source debug (mirrors engine calls)", ""]
    try:
        libraries = _tautulli_api_request(conn, "get_libraries", timeout=10) or []
    except Exception as e:
        return jsonify({"ok": False, "error": f"get_libraries failed: {e}"}), 400

    lines.append("get_libraries:")
    selected = []
    for lib in libraries:
        if not isinstance(lib, dict):
            continue
        is_movie = lib.get("section_type") == "movie" and lib.get("is_active", 1)
        tag = "  <- engine scans this" if is_movie else ""
        lines.append(f"  {lib.get('section_name')} [id={lib.get('section_id')}, type={lib.get('section_type')}, "
                     f"active={lib.get('is_active', 1)}, count={lib.get('count', 'n/a')}]{tag}")
        if is_movie:
            selected.append(lib)
    if not selected:
        lines.append("  WARNING: no active movie sections — the engine would find nothing to scan.")

    path_keys = ("file", "file_path", "media_file", "location", "path")
    sample_rows = []
    for lib in selected:
        section_id = lib.get("section_id")
        lines.append("")
        lines.append(f"get_library_media_info | section={lib.get('section_name')} [{section_id}] "
                     f"(engine params, first {_DEBUG_SAMPLE_ROWS} rows):")
        try:
            data = _tautulli_api_request(
                conn, "get_library_media_info", timeout=20,
                section_id=section_id, section_type="movie",
                start=0, length=_DEBUG_SAMPLE_ROWS,
                order_column="title", order_dir="asc",
            )
        except Exception as e:
            lines.append(f"  ERROR: {e}")
            continue
        rows = data.get("data", data if isinstance(data, list) else []) if data is not None else []
        total = data.get("recordsFiltered") or data.get("recordsTotal") if isinstance(data, dict) else None
        lines.append(f"  rows returned={len(rows)}" + (f" | total in section={total}" if total is not None else ""))
        rows_with_paths = sum(1 for r in rows if isinstance(r, dict) and any(r.get(k) for k in path_keys))
        lines.append(f"  rows carrying a file path: {rows_with_paths}/{len(rows)}"
                     + ("  <- expected: 0 (Tautulli rows have no paths; the engine resolves them via get_metadata)"
                        if rows_with_paths == 0 else ""))
        for r in rows[:3]:
            if isinstance(r, dict):
                lines.append(f"    - {r.get('title')!r} | rating_key={r.get('rating_key')} | "
                             f"play_count={r.get('play_count')} | last_played={r.get('last_played')} | "
                             f"added_at={r.get('added_at')} | "
                             f"file_size={r.get('file_size') or '(none)'}")
        sample_rows.extend(r for r in rows if isinstance(r, dict) and r.get("rating_key"))

    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]
    lines.append("")
    lines.append(f"get_metadata samples (first {_DEBUG_SAMPLE_META} movies — engine fetches this per movie):")
    for row in sample_rows[:_DEBUG_SAMPLE_META]:
        rk, title = row.get("rating_key"), row.get("title")
        lines.append(f"  {title!r} (rating_key={rk}):")
        try:
            meta = _tautulli_api_request(conn, "get_metadata", timeout=10, rating_key=rk, media_info=0) or {}
        except Exception as e:
            lines.append(f"    get_metadata media_info=0 ERROR: {e}")
            meta = {}
        colls = []
        for e in (meta.get("collections") or []):
            colls.append(e if isinstance(e, str) else (e.get("tag") if isinstance(e, dict) else str(e)))
        tmdb_id = imdb_id = None
        for guid in (meta.get("guids") or []):
            if isinstance(guid, str):
                if guid.startswith("tmdb://"):
                    tmdb_id = guid.replace("tmdb://", "").strip()
                elif guid.startswith("imdb://"):
                    imdb_id = guid.replace("imdb://", "").strip()
        protected = bool(protected_names & set(c for c in colls if c))
        lines.append(f"    collections={colls or '(none)'} | protected={'YES' if protected else 'no'} "
                     f"(vs {sorted(protected_names) or '(none configured)'})")
        lines.append(f"    guids -> imdb={imdb_id or '(none)'} | tmdb={tmdb_id or '(none)'}")
        try:
            meta_paths = _tautulli_api_request(conn, "get_metadata", timeout=10, rating_key=rk, media_info=1) or {}
        except Exception as e:
            lines.append(f"    get_metadata media_info=1 ERROR: {e}")
            meta_paths = {}
        raw_paths = _extract_media_paths_from_item(meta_paths)
        if not raw_paths:
            lines.append("    file paths: NONE FOUND — the engine would skip this movie (no_file_path)")
        else:
            lines.append("    file paths:")
            for raw, want in raw_paths[:4]:
                lines.append(_debug_resolved_line(raw, monitor_dirs, want))
    if not sample_rows:
        lines.append("  (no rows available to sample)")

    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/jellyfin/movies", methods=["POST"])
def api_debug_jellyfin_movies():
    """Mirror the engine's Jellyfin movie-source calls: System/Info, Items
    (movie listing), Users, and per-user play-data aggregation."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("jellyfin_url") and conn.get("jellyfin_key")):
        return jsonify({"ok": False, "error": "Add the Jellyfin URL and API key above and Save, then debug."}), 400
    url, key = conn["jellyfin_url"], conn["jellyfin_key"]

    lines = ["Jellyfin movie-source debug (mirrors engine calls)", ""]
    try:
        info = _jellyfin_get(url, key, "System/Info", timeout=10) or {}
        lines.append(f"System/Info: {info.get('ServerName') or '(no name)'} | version={info.get('Version') or 'n/a'}")
    except Exception as e:
        lines.append(f"System/Info ERROR: {e}")

    lines.append("")
    lines.append(f"Items (engine movie listing, first {_DEBUG_SAMPLE_ROWS} shown):")
    try:
        payload = _jellyfin_get(url, key, "Items", {
            "IncludeItemTypes": "Movie", "Recursive": "true",
            "Fields": "Path,MediaSources,DateCreated,ProviderIds",
            "EnableUserData": "false", "Limit": _DEBUG_SAMPLE_ROWS,
        }, timeout=20) or {}
    except Exception as e:
        return jsonify({"ok": False, "error": f"Items query failed: {e}"}), 400
    items = _jellyfin_items_from_payload(payload)
    total = payload.get("TotalRecordCount") if isinstance(payload, dict) else None
    lines.append(f"  returned={len(items)}" + (f" | TotalRecordCount={total}" if total is not None else ""))
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]
    with_path = with_ids = 0
    for item in items:
        prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        path = item.get("Path") or next((ms.get("Path") for ms in (item.get("MediaSources") or [])
                                         if isinstance(ms, dict) and ms.get("Path")), None)
        if path:
            with_path += 1
        if prov.get("imdb") or prov.get("tmdb"):
            with_ids += 1
    lines.append(f"  items with a Path: {with_path}/{len(items)} | with imdb/tmdb ProviderIds: {with_ids}/{len(items)}")
    for item in items[:3]:
        prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
        _sz = next((ms.get("Size") for ms in (item.get("MediaSources") or [])
                    if isinstance(ms, dict) and ms.get("Size")), None)
        lines.append(f"    - {item.get('Name')!r} ({item.get('ProductionYear')}) | id={item.get('Id')} | "
                     f"imdb={prov.get('imdb') or '(none)'} | tmdb={prov.get('tmdb') or '(none)'} | "
                     f"DateCreated={item.get('DateCreated') or '(none)'} | "
                     f"Size={_sz or '(none — the size fingerprint and flat-file matching need it)'}")
        raw = item.get("Path") or ""
        if raw:
            _want = next((int(ms.get("Size") or 0) for ms in (item.get("MediaSources") or [])
                          if isinstance(ms, dict) and ms.get("Size")), 0)
            lines.append(_debug_resolved_line(raw, monitor_dirs, _want))
        else:
            lines.append("      (no Path on item)")

    lines.append("")
    lines.append("Per-user play data (engine aggregates across ALL users):")
    try:
        users = [u for u in (_jellyfin_get(url, key, "Users", timeout=10) or []) if isinstance(u, dict)]
    except Exception as e:
        users = []
        lines.append(f"  Users ERROR: {e}")
    for u in users:
        uid = u.get("Id")
        try:
            up = _jellyfin_get(url, key, f"Users/{uid}/Items", {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "EnableUserData": "true", "Limit": 100,
            }, timeout=20) or {}
            uitems = _jellyfin_items_from_payload(up)
            played = sum(1 for i in uitems if (i.get("UserData") or {}).get("PlayCount"))
            latest = max((str((i.get("UserData") or {}).get("LastPlayedDate") or "") for i in uitems), default="")
            lines.append(f"  {u.get('Name') or '(no name)'} [{uid}]: sample={len(uitems)} | "
                         f"with PlayCount>0: {played} | most recent LastPlayedDate: {latest or '(none)'}")
        except Exception as e:
            lines.append(f"  {u.get('Name') or '(no name)'} [{uid}]: ERROR {e}")
    if not users:
        lines.append("  (no users listed — play counts would all be 0)")

    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/radarr", methods=["POST"])
def api_debug_radarr():
    """Mirror the engine's Radarr calls: system/status (startup check),
    /api/v3/movie (section detection) and the tmdbId lookup used at delete time."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("radarr_url") and conn.get("radarr_key")):
        return jsonify({"ok": False, "error": "Add the Radarr URL and API key above and Save, then debug."}), 400
    radarr_url, radarr_key = conn["radarr_url"].rstrip("/"), conn["radarr_key"]

    lines = ["Radarr API debug (mirrors engine calls)", ""]
    try:
        status = _json_request(f"{radarr_url}/api/v3/system/status",
                               headers={"X-Api-Key": radarr_key}, timeout=10) or {}
        lines.append(f"system/status: {status.get('appName') or 'Radarr'} | version={status.get('version') or 'n/a'}")
    except Exception as e:
        return jsonify({"ok": False, "error": f"system/status failed: {e}"}), 400

    lines.append("")
    lines.append("GET /api/v3/movie (engine uses this for section detection):")
    try:
        movies = _json_request(f"{radarr_url}/api/v3/movie",
                               headers={"X-Api-Key": radarr_key}, timeout=25) or []
    except Exception as e:
        lines.append(f"  ERROR: {e}")
        movies = []
    movies = [m for m in movies if isinstance(m, dict)]
    lines.append(f"  movies in Radarr: {len(movies)}")
    for m in movies[:5]:
        lines.append(f"    - {m.get('title')!r} ({m.get('year')}) | tmdbId={m.get('tmdbId')} | "
                     f"monitored={m.get('monitored')} | hasFile={m.get('hasFile')} | path={m.get('path')}")

    detected_id = cfg.get("_RADARR_DETECTED_SECTION_ID")
    detected_name = cfg.get("_RADARR_DETECTED_SECTION_NAME")
    lines.append("")
    lines.append(f"Detected Radarr/Overseerr section: "
                 f"{(str(detected_name) + ' [' + str(detected_id) + ']') if detected_id else '(none detected)'}")

    sample_tmdb = next((m.get("tmdbId") for m in movies if m.get("tmdbId")), None)
    lines.append("")
    if sample_tmdb:
        lines.append(f"Delete-time lookup form: GET /api/v3/movie?tmdbId={sample_tmdb}")
        try:
            hits = _json_request(f"{radarr_url}/api/v3/movie?tmdbId={sample_tmdb}",
                                 headers={"X-Api-Key": radarr_key}, timeout=10) or []
            hits = [h for h in hits if isinstance(h, dict)]
            lines.append(f"  returned {len(hits)} match(es)"
                         + (f": {hits[0].get('title')!r}" if hits else ""))
        except Exception as e:
            lines.append(f"  ERROR: {e}")
    else:
        lines.append("Delete-time lookup form: skipped (no movie with a tmdbId to sample)")

    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/sonarr", methods=["POST"])
def api_debug_sonarr():
    """Mirror the cleanup's Sonarr calls: system/status (the probe),
    /api/v3/series (the unmonitor lookup's source), and how each sampled
    series folder resolves under the monitored dirs — the same by-name rule
    the season deletion pass uses. Sonarr is optional and cleanup-only: the
    TV inventory comes from the media servers, so this debug is about the
    unmonitor-before-delete step, not about what gets scored."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    if not (conn.get("sonarr_url") and conn.get("sonarr_key")):
        return jsonify({"ok": False, "error": "Add the Sonarr URL and API key above and Save, then debug."}), 400
    s_url, s_key = conn["sonarr_url"].rstrip("/"), conn["sonarr_key"]

    lines = ["Sonarr API debug (mirrors the TV cleanup's calls)", ""]
    try:
        status = _json_request(f"{s_url}/api/v3/system/status",
                               headers={"X-Api-Key": s_key}, timeout=10) or {}
        lines.append(f"system/status: {status.get('appName') or 'Sonarr'} | version={status.get('version') or 'n/a'}")
    except Exception as e:
        return jsonify({"ok": False, "error": f"system/status failed: {e}"}), 400

    lines.append("")
    lines.append("GET /api/v3/series (the pass matches by title + year here to unmonitor a season):")
    try:
        series = _json_request(f"{s_url}/api/v3/series",
                               headers={"X-Api-Key": s_key}, timeout=25) or []
    except Exception as e:
        lines.append(f"  ERROR: {e}")
        series = []
    series = [s for s in series if isinstance(s, dict)]
    lines.append(f"  series in Sonarr: {len(series)}")

    # The same by-name folder rule the season deletion uses: the series
    # folder must be found under a monitored dir, whatever Sonarr's own
    # mount prefix looks like.
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or []) if str(d or "").strip()]
    for s in series[:8]:
        seasons = [x for x in (s.get("seasons") or []) if isinstance(x, dict)]
        monitored_n = sum(1 for x in seasons if x.get("monitored"))
        lines.append(f"    - {s.get('title')!r} ({s.get('year')}) | id={s.get('id')} | "
                     f"monitored={s.get('monitored')} | seasons={len(seasons)} "
                     f"({monitored_n} monitored) | path={s.get('path')}")
        parts = [seg for seg in str(s.get("path") or "").replace("\\", "/").split("/") if seg]
        resolved = None
        if parts and not any(seg in (".", "..") for seg in parts):
            for start in range(len(parts)):
                for d in monitor_dirs:
                    candidate = Path(d).joinpath(*parts[start:])
                    try:
                        if candidate.is_dir():
                            resolved = str(candidate)
                            break
                    except OSError:
                        continue
                if resolved:
                    break
        lines.append(f"        -> {resolved + ' (in scope for cleanup)' if resolved else 'NO FOLDER of this name under the monitored dirs — cleanup would skip it'}")

    dup_titles = {}
    for s in series:
        dup_titles.setdefault(_norm_series_title(s.get("title")), []).append(s.get("year"))
    dups = {t: ys for t, ys in dup_titles.items() if len(ys) > 1}
    lines.append("")
    if dups:
        lines.append("Same-title series (the pass disambiguates these by YEAR before unmonitoring;")
        lines.append("ambiguity that survives the year check is skipped fail-closed):")
        for t, ys in list(dups.items())[:5]:
            lines.append(f"  - {t!r}: years {sorted(y for y in ys if y)}")
    else:
        lines.append("No same-title series — the unmonitor lookup is unambiguous.")

    lines.append("")
    lines.append(f"SONARR_CLEANUP_ENABLED = {bool(cfg.get('SONARR_CLEANUP_ENABLED', False))} "
                 "(off: files still delete; seasons stay monitored in Sonarr)")
    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/media-paths", methods=["POST"])
def api_debug_media_paths():
    """Show how server-reported media paths resolve under /library and whether
    they land inside the monitored directories — the engine's deletion keyspace."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]

    lines = ["Media path mapping debug", ""]
    lines.append("How matching works: files match by FINGERPRINT — the movie's folder")
    lines.append("name + file name, with the server's byte count disambiguating")
    lines.append("same-named copies. Mount prefixes never need to line up. Flat files")
    lines.append("(no movie folder) match by file name + size, and with Jellyfin")
    lines.append("enabled they are deletable only when every source and the disk")
    lines.append("agree on the bytes. Folder paths (section locations) are not media")
    lines.append("entries and are ignored by the accuracy check.")
    lines.append("")
    root = Path(FILESYSTEM_CHECK_PATH)
    try:
        top = sorted(p.name for p in root.iterdir() if p.is_dir())
    except Exception as e:
        top = []
        lines.append(f"{FILESYSTEM_CHECK_PATH} listing ERROR: {e}")
    lines.append(f"{FILESYSTEM_CHECK_PATH} top-level folders ({len(top)}): {', '.join(top[:20]) or '(none)'}"
                 + (" …" if len(top) > 20 else ""))

    exts = _media_file_extensions(cfg)
    lines.append(f"Movie file extensions (what counts as a media FILE entry): "
                 f"{', '.join(sorted(exts)) or '(none configured)'}")
    idx = _media_fingerprint_index()
    lines.append(f"Fingerprint index: {sum(len(v) for v in idx['dir_name'].values()):,} "
                 f"file(s) walked {max(0, time.time() - idx['at']):.0f}s ago "
                 "(rebuilt fresh whenever the accuracy check runs)")

    lines.append("")
    lines.append("Stored media-path verdict (re-judged ONLY by Check for Errors or a")
    lines.append("monitored-paths save; every other probe replays it as-is):")
    if not _MEDIA_PATH_VERDICT["servers"]:
        lines.append("  (never judged yet — run Check for Errors)")
    for _name, _slot in sorted(_MEDIA_PATH_VERDICT["servers"].items()):
        _c = _slot.get("compat") or {}
        _age = time.time() - (_slot.get("at") or 0)
        lines.append(f"  {_name}: judged {_age / 60:.0f} min ago | "
                     f"checked={_c.get('checked')} matched={_c.get('matched')} "
                     f"unmatched={_c.get('unmatched')} | size-checked={_c.get('size_checked')} "
                     f"mismatched={_c.get('size_mismatched')} | "
                     f"folder entries ignored={_c.get('folder_entries')} | "
                     f"{'BLOCKING' if _slot.get('blocker') else 'ok'}")
        for _e in _slot.get("errors") or []:
            lines.append(f"    ERROR: {_e}")
        for _w in _slot.get("warnings") or []:
            lines.append(f"    warning: {_w}")

    lines.append("")
    lines.append("Monitored directories (the engine may only delete inside these):")
    if not monitor_dirs:
        lines.append("  (none configured)")
    for d in monitor_dirs:
        exists = Path(d).exists()
        lines.append(f"  {d} | exists={'yes' if exists else 'NO'}")

    if bool(cfg.get("USE_PLEX")) and conn.get("tautulli_url") and conn.get("tautulli_key"):
        lines.append("")
        lines.append("Tautulli-reported sample paths -> resolved /library paths:")
        try:
            samples = _sample_tautulli_media_paths(conn, limit=8)
            if not samples:
                lines.append("  (no paths sampled — Tautulli rows carry no paths; metadata sampling found none)")
            for raw, want in samples:
                lines.append(_debug_resolved_line(raw, monitor_dirs, want))
        except Exception as e:
            lines.append(f"  ERROR: {e}")

    if bool(cfg.get("USE_JELLYFIN")) and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        lines.append("")
        lines.append("Jellyfin-reported sample paths -> resolved /library paths:")
        try:
            samples = _sample_jellyfin_media_paths(conn, limit=8)
            if not samples:
                lines.append("  (no paths sampled)")
            for raw, want in samples:
                lines.append(_debug_resolved_line(raw, monitor_dirs, want))
        except Exception as e:
            lines.append(f"  ERROR: {e}")

    return jsonify({"ok": True, "text": "\n".join(lines)})


def _fmt_epoch(ts) -> str:
    try:
        ts = int(ts)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts > 0 else "never"
    except (TypeError, ValueError):
        return "unknown"


@app.route("/api/debug/library-snapshot", methods=["POST"])
def api_debug_library_snapshot():
    """Filtering & Scoring debug: what the current library snapshot holds."""
    cfg = load_config()
    lines = ["Library snapshot debug", ""]
    lines.append(f"USE_PLEX={bool(cfg.get('USE_PLEX'))} | USE_JELLYFIN={bool(cfg.get('USE_JELLYFIN'))}")
    monitor_dirs = [str(d) for d in (cfg.get("MONITOR_DIRS") or [])]
    lines.append("monitored paths (the snapshot only holds movies under these): "
                 + (", ".join(monitor_dirs) or "(none)"))
    lines.append(f"IMDb ratings dataset on disk: {imdb_ratings_path().exists()}")

    lines.append("")
    data, pool_err = _read_library_snapshot()
    if pool_err:
        lines.append(f"library snapshot: ({'missing — run a Simulate to build it' if pool_err == 'missing' else 'unreadable'})")
        return jsonify({"ok": True, "text": "\n".join(lines)})
    movies = data.get("movies") or []
    rated = sum(1 for m in movies if m.get("rating") is not None)
    protected = sum(1 for m in movies if m.get("protected"))
    favorite = sum(1 for m in movies if m.get("favorite"))
    unplayed = sum(1 for m in movies if not m.get("plays"))
    lines.append(f"library snapshot: built {_fmt_epoch(data.get('built_at'))} | {len(movies)} movies | "
                 f"rated={rated} | protected={protected} | favorite={favorite} | unplayed={unplayed}")
    lines.append("")
    # The snapshot is the ENTIRE library; cap the per-movie dump so the debug
    # popup stays usable on big collections.
    _dump_cap = 1000
    lines.append("Entries (scoring inputs per movie):")
    for m in movies[:_dump_cap]:
        lines.append(f"  {m.get('title') or '(no title)'} ({m.get('year') or '?'}) | "
                     f"rating={m.get('rating')} votes={m.get('votes')} | plays={m.get('plays')} "
                     f"users={m.get('users')} | last_played={_fmt_epoch(m.get('last_played'))} | "
                     f"added={_fmt_epoch(m.get('added_at'))} | size={m.get('size_gb')} GB"
                     + (" | PROTECTED" if m.get("protected") else "")
                     + (" | FAVORITE" if m.get("favorite") else ""))
    if len(movies) > _dump_cap:
        lines.append(f"  … and {len(movies) - _dump_cap} more (first {_dump_cap} shown)")
    return jsonify({"ok": True, "text": "\n".join(lines)})


@app.route("/api/debug/cache", methods=["POST"])
def api_debug_cache():
    """Advanced/Cache debug: a readable dump of what the store currently holds — the
    library-snapshot summary, the marked & eligible deletion queue + its plan stamp,
    dashboard storage stats, and the top-level bookkeeping keys. Mirrors the other
    /api/debug/* popups (returns {ok, text} for prRunDebug)."""
    p = db_path()
    lines = ["Cache contents debug", ""]
    try:
        st = p.stat()
        lines.append(f"store: {p} | {st.st_size:,} bytes | modified {_fmt_epoch(st.st_mtime)}")
    except OSError:
        lines.append(f"store: {p} | not present (cleared on startup — run a Simulate to rebuild)")
        return jsonify({"ok": True, "text": "\n".join(lines)})
    try:
        cache = db.read_cache_dict(p)
        if not isinstance(cache, dict):
            raise ValueError("store did not compose to an object")
    except Exception as e:
        lines.append(f"  (unreadable — {e})")
        return jsonify({"ok": True, "text": "\n".join(lines)})

    lines.append("top-level keys: " + (", ".join(sorted(cache.keys())) or "(none)"))
    lines.append(f"code_checksum: {cache.get('code_checksum')!r}")
    lines.append(f"last_cleanup_date: {cache.get('last_cleanup_date')!r}")

    # Library snapshot (the Filtering & Scoring table); summary only; the Filtering
    # & Scoring "Debug" button dumps it per-movie.
    lines.append("")
    snap = cache.get("library_snapshot")
    if isinstance(snap, dict):
        sm = snap.get("movies") or []
        movie_rows = [r for r in sm if isinstance(r, dict)
                      and (r.get("media_type") or "movie") == "movie"]
        tv_rows = [r for r in sm if isinstance(r, dict) and r.get("media_type") == "tv"]
        dirs = snap.get("monitor_dirs")
        lines.append(f"library_snapshot: built {_fmt_epoch(snap.get('built_at'))} | "
                     f"{len(movie_rows)} movies + {len(tv_rows)} TV series"
                     + (f" | monitor_dirs={dirs}" if dirs else ""))
        if tv_rows:
            in_scope = sum(1 for r in tv_rows if r.get("tv_in_scope"))
            watched = sum(1 for r in tv_rows if (r.get("tv_episodes_watched") or 0) > 0)
            favs = sum(1 for r in tv_rows if r.get("favorite"))
            size_gb = sum(r.get("size_bytes") or 0 for r in tv_rows) / 1e9
            eps = sum(r.get("tv_episodes") or 0 for r in tv_rows)
            lines.append("")
            lines.append(f"TV inventory (from the media servers): {len(tv_rows)} series | {eps:,} episode files | "
                         f"{size_gb:,.1f} GB | {in_scope} under monitored paths | "
                         f"{watched} with watch history | {favs} Jellyfin-favorited")
            _tv_cfg = load_config()
            tv_cleanup_on = bool(_tv_cfg.get("TV_CLEANUP_ENABLED", False))
            plan = _tv_season_plan(tv_rows, _tv_cfg)
            porder = plan["order"]
            pex = plan["excluded"]
            _pool = {"target_bytes": 0, "tv_share_bytes": 0, "movie_share_bytes": 0}
            if tv_cleanup_on:
                _, _pool = _merged_pool_takes(porder, _tv_cfg)
            total_gb = sum(e["size_bytes"] for e in porder) / 1e9
            tv_state = _tv_cleanup_state()
            tv_marked = tv_state.get("marked") or {}
            lines.append("")
            lines.append(f"TV season plan (the season rows of the cleanup's deletion order): "
                         f"{len(porder)} seasons | {total_gb:,.1f} GB in the order | "
                         f"held back: {pex['protected']} protected, {pex['favorite']} favorited, "
                         f"{pex['latest_of_continuing']} latest-of-continuing, "
                         f"{pex['oversized_season']} over the episode cap, "
                         f"{pex['season_rule']} by the season-eligibility rule, "
                         f"{pex['off_path']} off-path")
            lp = tv_state.get("last_pass") if isinstance(tv_state.get("last_pass"), dict) else None
            if lp:
                lines.append(f"  seasons in the last run: {lp.get('date')} ({lp.get('mode')}) | "
                             f"{lp.get('marked_new')} newly marked, {lp.get('unmarked')} unmarked, "
                             f"{lp.get('held_by_delay')} waiting out the delay | "
                             f"{len(lp.get('deleted_seasons') or [])} season(s) deleted "
                             f"({(lp.get('freed_bytes') or 0) / 1e9:.1f} GB)"
                             + (f" | seasons ABORTED fail-closed: {lp.get('aborted')}" if lp.get("aborted") else "")
                             + (" | held by the space-safety refusal (run-wide)"
                                if lp.get("blocked_by_safety") else ""))
                for sk in (lp.get("skipped") or [])[:10]:
                    lines.append(f"    skipped {sk.get('title')} S{sk.get('season')}: {sk.get('why')}")
            else:
                lines.append("  seasons in the last run: none yet — seasons ride every "
                             "Simulate and Cleanup once a media server and a space "
                             "trigger are set")
            if tv_marked:
                lines.append(f"  marked seasons ({len(tv_marked)} — deleted by an Automatic "
                             "Cleanup once their delete-on date arrives):")
                for m in sorted(tv_marked.values(), key=lambda x: x.get("score") or 0):
                    try:
                        _due = (datetime.fromtimestamp(m.get("marked_at") or 0)
                                + timedelta(days=int(m.get("delay_days") or 1))).strftime("%Y-%m-%d")
                    except (OSError, OverflowError, ValueError):
                        _due = "?"
                    lines.append(f"    {m.get('title')} S{m.get('season')} | "
                                 f"{(m.get('size_bytes') or 0) / 1e9:.1f} GB | score={m.get('score')} | "
                                 f"delete on {_due}")
            if not tv_cleanup_on:
                lines.append("  TV cleanup: OFF (Filtering & Scoring) — the order below is "
                             "informational and nothing would be taken")
            elif _pool["target_bytes"] <= 0:
                lines.append("  pool within its limits (Headroom / Library Size Cap) — "
                             "the cleanup would take no seasons")
            else:
                lines.append(f"  pool deficit {_pool['target_bytes'] / 1e9:,.1f} GB "
                             "(Headroom / Library Size Cap, movies and TV in ONE order) → "
                             f"seasons cover {_pool['tv_share_bytes'] / 1e9:,.1f} GB (TAKE), "
                             f"movies {_pool['movie_share_bytes'] / 1e9:,.1f} GB")
            run = 0
            for e in porder[:40]:
                run += e["size_bytes"]
                lines.append(
                    f"    {'TAKE ' if e['take'] else ''}{e['title']} S{e['season']} | "
                    f"{e['size_bytes'] / 1e9:.1f} GB "
                    f"(cum {run / 1e9:,.1f}) | score={e['score']} | "
                    f"eps {e['eps_watched']}/{e['eps']} watched | "
                    f"last={_fmt_epoch(e['last_played']) if e['last_played'] else 'never'}")
            if len(porder) > 40:
                hidden_takes = sum(1 for e in porder[40:] if e["take"])
                lines.append(f"    … and {len(porder) - 40} more seasons"
                             + (f" ({hidden_takes} of them TAKE)" if hidden_takes else ""))
            lines.append("")
            lines.append("  per series (biggest first):")
            for r in tv_rows:
                seasons = r.get("tv_seasons") or []
                s_bits = ", ".join(
                    f"S{s.get('n')}:{s.get('eps_watched') or 0}/{s.get('eps') or 0}"
                    for s in seasons if isinstance(s, dict))
                _title = str(r.get("title") or "")
                if not _title.endswith(f"({r.get('year')})"):
                    _title = f"{_title} ({r.get('year')})"
                lines.append(
                    f"    {'[in-scope]' if r.get('tv_in_scope') else '[off-path]'} "
                    f"{_title} | {r.get('tv_status') or '?'} | "
                    f"{(r.get('size_bytes') or 0) / 1e9:.1f} GB | "
                    f"eps {r.get('tv_episodes_watched') or 0}/{r.get('tv_episodes') or 0} watched | "
                    f"users={r.get('users') or 0} | plays={r.get('plays') or 0} | "
                    f"last={_fmt_epoch(r.get('last_played')) if r.get('last_played') else 'never'}"
                    + (" | PROTECTED" if r.get("protected") else "")
                    + (" | FAVORITE" if r.get("favorite") else "")
                    + (f" | seasons {s_bits}" if s_bits else ""))
    else:
        lines.append("library_snapshot: (none — run a Simulate to build it)")

    # Deletion queue + plan-currency stamp (the store's queue table + pending meta).
    lines.append("")
    pend = cache.get("pending")
    if isinstance(pend, dict):
        entries = pend.get("entries") if isinstance(pend.get("entries"), dict) else {}
        marked = sum(1 for e in entries.values() if isinstance(e, dict) and e.get("marked_at") is not None)
        lines.append(f"pending (deletion queue): schema={pend.get('schema')} | {len(entries)} entries | "
                     f"{marked} marked (delay clock running) | {len(entries) - marked} eligible")
        stamp = pend.get("plan_config")
        if isinstance(stamp, dict):
            lines.append("  plan_config stamp: " + ", ".join(f"{k}={v}" for k, v in sorted(stamp.items())))
        else:
            lines.append(f"  plan_config stamp: {stamp!r}")
        if pend.get("monitor_dirs") is not None:
            lines.append(f"  monitor_dirs: {pend.get('monitor_dirs')}")
        _cap = 200
        lines.append(f"  entries (stored deletion order, first {_cap} shown):")
        for pth, e in list(entries.items())[:_cap]:
            e = e if isinstance(e, dict) else {}
            marked_at = e.get("marked_at")
            tag = "marked  " if marked_at is not None else "eligible"
            size_gb = round((e.get("size_bytes") or 0) / 1e9, 2)
            lines.append(f"    [{tag}] {e.get('title') or pth} | score={e.get('score')} | "
                         f"size={size_gb} GB | marked_at={_fmt_epoch(marked_at) if marked_at else '—'}")
        if len(entries) > _cap:
            lines.append(f"    … and {len(entries) - _cap} more (first {_cap} shown)")
    else:
        lines.append("pending (deletion queue): (none — run a Simulate to build it)")

    # Dashboard storage stats.
    lines.append("")
    ds = cache.get("dashboard_stats")
    if isinstance(ds, dict):
        lines.append("dashboard_stats: " + ", ".join(f"{k}={v}" for k, v in sorted(ds.items())))
    else:
        lines.append(f"dashboard_stats: {ds!r}")

    # Metadata cache (summary only; it can be thousands of rows).
    meta_cache = cache.get("movies")
    if isinstance(meta_cache, dict):
        lines.append("")
        lines.append(f"metadata_cache: {len(meta_cache)} movie(s) | config_hash={cache.get('config_hash')!r}")

    # Any top-level keys not covered above, so nothing in the store is hidden.
    _known = {"code_checksum", "last_cleanup_date", "library_snapshot", "pending",
              "dashboard_stats", "movies", "config_hash"}
    other = sorted(k for k in cache.keys() if k not in _known)
    if other:
        lines.append("")
        lines.append("other keys: " + ", ".join(other))

    return jsonify({"ok": True, "text": "\n".join(lines)})


# ── Sanitized diagnostic report ───────────────────────────────────────────────
# "Create report" on the Config page: a full snapshot of config, connections, API
# samples, and engine state, with everything personal/identifiable replaced by
# stable hash tokens; safe to attach to a bug report. The same value always maps to
# the same token, so cross-server path/title comparisons still line up within one
# report. The report is downloaded via the browser and never written to disk.

_REPORT_SECRET_KEYS = ("TAUTULLI_API_KEY", "PLEX_TOKEN", "JELLYFIN_API_KEY",
                       "RADARR_API_KEY", "SONARR_API_KEY") + notify.SECRET_KEYS
_REPORT_URL_KEYS = ("TAUTULLI_URL", "PLEX_URL", "JELLYFIN_URL", "RADARR_URL",
                    "SONARR_URL", "IMDB_RATINGS_URL",
                    # The notification servers are the user's own hosts exactly
                    # like the media servers above; their tokens/topics are in
                    # SECRET_KEYS but the hostnames leaked through the raw
                    # {value!r} fallback.
                    "NOTIFY_NTFY_SERVER", "NOTIFY_GOTIFY_SERVER")


class _ReportSanitizer:
    def __init__(self, cfg: dict):
        # Raw values that must never appear anywhere in the report, even inside
        # error messages: secrets, configured URLs, and their hostnames.
        self._scrub: list[tuple[str, str]] = []
        for key in _REPORT_SECRET_KEYS:
            value = cfg.get(key)
            # NOTIFY_CUSTOM_URLS is a LIST of apprise URLs; registering its
            # Python repr would scrub nothing. Each element is its own secret.
            values = value if isinstance(value, list) else [value]
            for v in values:
                v = str(v or "").strip()
                if v:
                    self._scrub.append((v, f"<{key.lower()}>"))
        for key in _REPORT_URL_KEYS:
            value = str(cfg.get(key) or "").strip()
            if not value or key == "IMDB_RATINGS_URL":
                continue
            self._scrub.append((value, f"<{key.lower()}>"))
            try:
                host = urlparse(value).hostname
            except Exception:
                host = None
            if host:
                self._scrub.append((host, "<host>"))
        # The detected service defaults are URLs the user never typed into a
        # *_URL field — the LAN IP the app guessed, the Plex host read out of
        # Tautulli's config — so in the standard "URL left blank" setup they
        # are the ONLY spelling of those hosts, and nothing above registered
        # them. Same scrub as a configured URL.
        for value in (cfg.get("_SERVICE_URL_DEFAULTS") or {}).values():
            value = str(value or "").strip()
            if not value:
                continue
            self._scrub.append((value, "<url>"))
            try:
                host = urlparse(value).hostname
            except Exception:
                host = None
            if host:
                self._scrub.append((host, "<host>"))

    def token(self, value, prefix: str = "t") -> str:
        raw = str(value or "").strip()
        if not raw:
            return "(blank)"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"<{prefix}:{digest}>"

    def url(self, value) -> str:
        raw = str(value or "").strip()
        if not raw:
            return "(blank)"
        try:
            parts = urlparse(raw)
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme or 'http'}://<host>{port}"
        except Exception:
            return "<url>"

    def path(self, value) -> str:
        """Keep a path's structure (root, depth, extension) but tokenize every other
        segment. The same segment always maps to the same token, so mount mismatches
        stay diagnosable without exposing titles."""
        raw = str(value or "").strip().replace("\\", "/")
        if not raw:
            return "(blank)"
        lead = "/" if raw.startswith("/") else ""
        parts = [seg for seg in raw.split("/") if seg]
        out = []
        for i, seg in enumerate(parts):
            if i == 0:
                out.append(seg)
                continue
            stem, ext = os.path.splitext(seg)
            out.append(self.token(stem, "p") + (ext if i == len(parts) - 1 else ""))
        return lead + "/".join(out)

    def redact(self, text: str) -> str:
        """Defensively de-identify a free-form line (a log or engine message) that can
        embed unpredictable private values: quoted/bracketed names (collections,
        titles) and absolute filesystem paths. Registered secrets/hosts are scrubbed
        too. Used where raw text is echoed and the specific values aren't known ahead
        of time."""
        raw = str(text or "")
        # Quoted names first ('Keep Forever', "The Matrix"); tokenize the inner value
        # so bracketed collection/title lists never surface verbatim.
        raw = re.sub(r"'[^']+'|\"[^\"]+\"",
                     lambda m: self.token(m.group(0)[1:-1], "name"), raw)
        # Then absolute paths of depth ≥2 (/library/movies/…): keep structure, tokenize
        # the segments. Segments MUST allow spaces; movie folders/filenames routinely
        # contain them ("The Grey (2012)"), and stopping at the first space would leak
        # the title after it. We still stop at field delimiters (| , : and quotes) so a
        # path ending a "key=… | key=…" line doesn't swallow the next field; a path
        # followed by bare prose over-tokenizes the prose; the safe
        # (never-under-redact) direction for a sanitizer.
        raw = re.sub(r"(?:/[^/'\"|,:]+){2,}/?",
                     lambda m: self.path(m.group(0)), raw)
        return self.scrub(raw)

    def scrub(self, text: str) -> str:
        for raw, placeholder in sorted(self._scrub, key=lambda t: -len(t[0])):
            text = text.replace(raw, placeholder)
        return text


def _build_debug_report() -> str:
    cfg = load_config()
    s = _ReportSanitizer(cfg)
    L: list[str] = []
    add = L.append

    add("MediaReducer diagnostic report (sanitized)")
    add(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    add(f"mediareducer: {APP_VERSION}")
    add(f"python: {sys.version.split()[0]}")
    add("titles, names, hosts, keys, and path segments are replaced by stable")
    add("hash tokens — the same value maps to the same token within this report.")

    # Every section opens the same way, and the report is read by eye; a rule
    # that drifted by a character in one section would look like corruption.
    def section(title: str) -> None:
        add("")
        add("=" * 60)
        add(title)
        add("=" * 60)

    section("CONFIG")
    for key in sorted(cfg.keys()):
        value = cfg.get(key)
        if key in _REPORT_SECRET_KEYS:
            add(f"  {key} = {'<set>' if str(value or '').strip() else '(blank)'}")
        elif key in _REPORT_URL_KEYS and key != "IMDB_RATINGS_URL":
            add(f"  {key} = {s.url(value)}")
        elif key == "MONITOR_DIRS":
            add(f"  {key} = [{', '.join(s.path(d) for d in (value or []))}]")
        elif key in ("PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS"):
            names = value if isinstance(value, (list, set, tuple)) else [value]
            add(f"  {key} = [{', '.join(s.token(n, 'name') for n in names if n)}]")
        elif key in ("_RADARR_DETECTED_SECTION_NAME",):
            add(f"  {key} = {s.token(value, 'name')}")
        elif key == "OUTPUT_DIR":
            add(f"  {key} = {value}")
        elif key == "_SERVICE_URL_DEFAULTS":
            urls = value if isinstance(value, dict) else {}
            add(f"  {key} = {{{', '.join(f'{k}: {s.url(v)}' for k, v in sorted(urls.items()))}}}")
        elif key == "_RUN_MODE_AUTOPAUSE_REASON":
            # Free-form sentence that can embed a real library path with a
            # movie title (it is built from health-check error text).
            add(f"  {key} = {s.redact(value)}")
        else:
            add(f"  {key} = {value!r}")

    section("SCHEDULER & CLOCK")
    add(f"  run mode: {cfg.get('RUN_MODE')!r}")
    if cfg.get("RUN_MODE") == "paused" and cfg.get("_RUN_MODE_AUTOPAUSE_REASON"):
        add(f"  auto-pause reason: {s.redact(cfg.get('_RUN_MODE_AUTOPAUSE_REASON'))}")
    add(f"  TIME_ZONE setting: {cfg.get('TIME_ZONE')!r}")
    add(f"  effective process zone: {_server_time_zone_name()} (host zone: {_host_time_zone_name()})")
    add(f"  process clock now: {time.strftime('%Y-%m-%d %H:%M:%S %z')} (tzname={'/'.join(time.tzname)})")
    add(f"  daily run time: {_daily_run_time(cfg)} · delete delay: {_delete_delay_days(cfg)} day(s)")
    try:
        job = scheduler.get_job("engine")
        if job is None:
            add("  scheduler job 'engine': (not registered)")
        else:
            nxt = getattr(job, "next_run_time", None)
            add(f"  next scheduler tick: {nxt.strftime('%Y-%m-%d %H:%M:%S %z') if nxt else '(paused)'}")
            add(f"  tick interval: every {SCHEDULE_INTERVAL_MINUTES} min")
    except Exception as e:
        add(f"  scheduler: unreadable — {s.redact(str(e))}")

    section("CONNECTION HEALTH (fresh probe)")
    try:
        health = _connection_health_state(cfg, probe=True)
        add(f"  critical_ok={health.get('critical_ok')} | severity={health.get('severity')}")
        add(f"  plex_connected={health.get('plex_connected')} | tautulli_connected={health.get('tautulli_connected')} | "
            f"jellyfin_connected={health.get('jellyfin_connected')} | radarr_connected={health.get('radarr_connected')} | "
            f"sonarr_connected={health.get('sonarr_connected')}")
        for err in health.get("errors") or []:
            add(f"  ERROR: {s.redact(str(err))}")
        for warn in health.get("warnings") or []:
            add(f"  warning: {s.redact(str(warn))}")
        for compat in health.get("media_path_compatibility") or []:
            add(f"  path compatibility [{compat.get('server')}]: matched {compat.get('matched')}/{compat.get('checked')}")
            for ex in compat.get("resolved_examples") or []:
                add(f"    {s.path(ex.get('reported'))} -> {s.path(ex.get('resolved'))}")
            for ex in compat.get("unmatched_examples") or []:
                add(f"    UNMATCHED: {s.path(ex)}")
        appdata = health.get("appdata") or {}
        for name, state in sorted(appdata.items()):
            if isinstance(state, dict):
                add(f"  appdata {name}: mounted={state.get('mounted')} ok={state.get('ok')}")
    except Exception as e:
        add(f"  probe failed: {s.redact(str(e))}")

    section("FILESYSTEM & STORAGE")
    disk = disk_stats()
    if disk:
        add(f"  {FILESYSTEM_CHECK_PATH} disk: used={disk.get('used_gb')} GB / total={disk.get('total_gb')} GB free={disk.get('free_gb')} GB")
    stats = library_stats()
    add(f"  cached library size: {stats.get('library_gb', 'n/a')} GB (refreshed {_fmt_epoch(stats.get('updated_at'))})")
    for d in cfg.get("MONITOR_DIRS") or []:
        add(f"  monitored: {s.path(d)} | exists={Path(d).exists()}")

    section("SPACE VERDICT & FORECAST")
    try:
        lib_gb = stats.get("library_gb")
        free_gb = (disk or {}).get("free_gb")
        headroom_gb = _threshold_gb_or_none(cfg.get("HEADROOM_GB"))
        redline_gb = _threshold_gb_or_none(cfg.get("REDLINE_GB"))
        cap_gb = _threshold_gb_or_none(cfg.get("MAX_LIBRARY_GB"))

        def _breach(free, target):
            if free is None or target is None:
                return "n/a"
            # Match the engine's trigger (free <= target → a run would delete);
            # using < here would print "ok" at the exact boundary while the
            # "a run would delete now" line two rows down says yes.
            return "BREACH" if free <= target else "ok"

        add(f"  free space: {free_gb} GB")
        add(f"  headroom target: {headroom_gb} GB → {_breach(free_gb, headroom_gb)}")
        add(f"  redline target: {redline_gb} GB → {_breach(free_gb, redline_gb)}")
        if cap_gb is not None:
            add(f"  library size cap: {cap_gb} GB | library now {lib_gb} GB → "
                f"{'BREACH' if (lib_gb is not None and lib_gb > cap_gb) else 'ok'}")
        else:
            add("  library size cap: (disabled)")
        try:
            limits_exceeded = _deletion_limits_exceeded(cfg, disk, lib_gb)
            add(f"  a run would delete now: {'yes — limits exceeded' if limits_exceeded else 'no — limits satisfied'}")
        except Exception as e:
            add(f"  a run would delete now: (undetermined — {s.redact(str(e))})")
        live = _cleanup_button_state(cfg, disk).get("space_thresholds", {})
        add(f"  ok_for_simulate={live.get('ok_for_simulate')} ok_for_cleanup={live.get('ok_for_cleanup')} "
            f"safety_blocked={live.get('safety_blocked')} simulate_required={live.get('simulate_required')}")
    except Exception as e:
        add(f"  verdict unavailable: {s.redact(str(e))}")

    conn = _effective_connection_values(cfg)

    if cfg.get("USE_PLEX") and conn.get("tautulli_url") and conn.get("tautulli_key"):
        section("TAUTULLI API")
        try:
            libraries = _tautulli_api_request(conn, "get_libraries", timeout=10) or []
            for lib in libraries:
                if isinstance(lib, dict):
                    add(f"  section {s.token(lib.get('section_name'), 'name')} [id={lib.get('section_id')}, "
                        f"type={lib.get('section_type')}, active={lib.get('is_active', 1)}, count={lib.get('count', 'n/a')}]")
            movie_sections = [l for l in libraries if isinstance(l, dict)
                              and l.get("section_type") == "movie" and l.get("is_active", 1)]
            for lib in movie_sections[:2]:
                data = _tautulli_api_request(conn, "get_library_media_info", timeout=20,
                                             section_id=lib.get("section_id"), section_type="movie",
                                             start=0, length=5, order_column="title", order_dir="asc")
                rows = (data or {}).get("data") or []
                add(f"  media info sample (section id={lib.get('section_id')}, total={((data or {}).get('recordsFiltered'))}):")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    add(f"    {s.token(row.get('title'), 'title')} ({row.get('year') or '?'}) | plays={row.get('play_count')} | "
                        f"last_played={_fmt_epoch(row.get('last_played'))} | added={_fmt_epoch(row.get('added_at'))} | "
                        f"size={row.get('file_size')} | file={s.path(row.get('file'))}")
                    add(f"      row keys: {', '.join(sorted(row.keys()))}")
        except Exception as e:
            add(f"  ERROR: {s.redact(str(e))}")

    if cfg.get("USE_JELLYFIN") and conn.get("jellyfin_url") and conn.get("jellyfin_key"):
        section("JELLYFIN API")
        try:
            info = _jellyfin_get(conn["jellyfin_url"], conn["jellyfin_key"], "System/Info", timeout=8) or {}
            add(f"  server version: {info.get('Version', 'n/a')}")
            data = _jellyfin_get(conn["jellyfin_url"], conn["jellyfin_key"], "Items", {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "Fields": "Path,MediaSources,DateCreated,ProviderIds", "Limit": 5,
            }, timeout=10) or {}
            add(f"  movie count: {data.get('TotalRecordCount', 'n/a')}")
            users = _jellyfin_get(conn["jellyfin_url"], conn["jellyfin_key"], "Users", timeout=8) or []
            add(f"  user count: {len(users)}")
            for item in _jellyfin_items_from_payload(data)[:5]:
                prov = {str(k).lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
                add(f"    {s.token(item.get('Name'), 'title')} ({item.get('ProductionYear') or '?'}) | "
                    f"imdb={'yes' if prov.get('imdb') else 'no'} tmdb={'yes' if prov.get('tmdb') else 'no'} | "
                    f"path={s.path(item.get('Path'))}")
        except Exception as e:
            add(f"  ERROR: {s.redact(str(e))}")

    if conn.get("radarr_url") and conn.get("radarr_key"):
        section("RADARR API")
        try:
            status = _json_request(f"{conn['radarr_url'].rstrip('/')}/api/v3/system/status",
                                   headers={"X-Api-Key": conn["radarr_key"]}, timeout=8) or {}
            add(f"  version: {status.get('version', 'n/a')}")
            movies = _json_request(f"{conn['radarr_url'].rstrip('/')}/api/v3/movie",
                                   headers={"X-Api-Key": conn["radarr_key"]}, timeout=20) or []
            add(f"  movie count: {len(movies)}")
            roots = _json_request(f"{conn['radarr_url'].rstrip('/')}/api/v3/rootfolder",
                                  headers={"X-Api-Key": conn["radarr_key"]}, timeout=8) or []
            for root in roots:
                if isinstance(root, dict):
                    add(f"  root folder: {s.path(root.get('path'))}")
        except Exception as e:
            add(f"  ERROR: {s.redact(str(e))}")
        add(f"  cleanup enabled: {cfg.get('RADARR_OVERSEERR_SECTION_ID') is not None} "
            f"(section={cfg.get('RADARR_OVERSEERR_SECTION_ID')!r}, "
            f"detected id={cfg.get('_RADARR_DETECTED_SECTION_ID')!r} via {cfg.get('_RADARR_DETECTED_SECTION_METHOD')!r})")

    section("PROTECTED COLLECTIONS (resolution — counts only, no names)")

    def _configured_count(config_key):
        selected = cfg.get(config_key) or []
        if isinstance(selected, str):
            selected = [p.strip() for p in selected.split(",") if p.strip()]
        return selected

    def _collection_resolution(label, config_key, use_flag, connected, names_fn):
        selected = _configured_count(config_key)
        if not use_flag:
            add(f"  {label}: server disabled | {len(selected)} configured")
            return
        if not connected:
            add(f"  {label}: not connected | {len(selected)} configured (cannot resolve)")
            return
        try:
            available = set(names_fn())
        except Exception as e:
            add(f"  {label}: {len(selected)} configured | resolve failed — {s.redact(str(e))}")
            return
        missing = [n for n in selected if n not in available]
        add(f"  {label}: {len(selected)} configured | {len(selected) - len(missing)} resolved | "
            f"{len(missing)} missing (renamed/removed) | server lists {len(available)} collections")

    _collection_resolution(
        "Plex", "PROTECTED_COLLECTIONS", bool(cfg.get("USE_PLEX")),
        bool(conn.get("plex_url") and conn.get("plex_token")),
        lambda: _plex_collection_names(conn["plex_url"], conn["plex_token"]))
    _collection_resolution(
        "Jellyfin", "JELLYFIN_PROTECTED_COLLECTIONS", bool(cfg.get("USE_JELLYFIN")),
        bool(conn.get("jellyfin_url") and conn.get("jellyfin_key")),
        lambda: _jellyfin_boxset_names(conn["jellyfin_url"], conn["jellyfin_key"]))

    section("ENGINE STATE FILES")
    try:
        cache = db.read_cache_dict(db_path())
        movies = cache.get("movies") or {}
        add(f"  store: {len(movies)} cached movie entries | last_cleanup_date={cache.get('last_cleanup_date')!r} | "
            f"dashboard_stats={'yes' if isinstance(cache.get('dashboard_stats'), dict) else 'no'}")
    except Exception as e:
        add(f"  store: unreadable — {s.redact(str(e))}")
    pool, pool_err = _read_library_snapshot()
    if pool_err:
        add(f"  library snapshot: ({pool_err})")
    else:
        pm = pool.get("movies") or []
        add(f"  library snapshot: {len(pm)} movies | built {_fmt_epoch(pool.get('built_at'))} | "
            f"rated={sum(1 for m in pm if m.get('rating') is not None)} | "
            f"protected={sum(1 for m in pm if m.get('protected'))} | favorite={sum(1 for m in pm if m.get('favorite'))}")
    try:
        prog = json.loads(progress_path().read_text(encoding="utf-8"))
        title = prog.get("current_title")
        message = str(prog.get("message") or "")
        if title:
            message = message.replace(str(title), s.token(title, "title"))
        # Engine abort messages embed the movie as "title=<name> |"; tokenize it.
        message = re.sub(r"title=([^|]+)",
                         lambda m: "title=" + s.token(m.group(1).strip(), "title"), message)
        # Defensively de-identify any remaining quoted names (e.g. a protected
        # collection list) or paths the specific-field passes above didn't catch.
        message = s.redact(message)
        add(f"  progress.json: status={prog.get('status')} phase={prog.get('phase')} mode={prog.get('mode')} | "
            f"scanned={prog.get('scanned')}/{prog.get('total')} eligible={prog.get('eligible')} "
            f"deleted={prog.get('deleted')} | ended {_fmt_epoch(prog.get('ended_at'))}")
        add(f"    message: {message}")
    except FileNotFoundError:
        add("  progress.json: (missing — no run yet)")
    except Exception as e:
        add(f"  progress.json: unreadable — {s.redact(str(e))}")
    try:
        archived = sorted(p.name for p in logs_dir().glob("*.log"))
        add(f"  archived run logs: {len(archived)}" + (f" (latest {archived[-1]})" if archived else ""))
    except Exception:
        add("  archived run logs: (unreadable)")
    try:
        deleted_lines = deleted_path().read_text(encoding="utf-8").strip().splitlines()
        add(f"  deleted.log: {len(deleted_lines)} entries (content not included)")
    except FileNotFoundError:
        add("  deleted.log: (missing)")
    except Exception:
        add("  deleted.log: (unreadable)")
    imdb_path = imdb_ratings_path()
    if imdb_path.exists():
        try:
            st = imdb_path.stat()
            age_days = max(0.0, (time.time() - st.st_mtime) / 86400.0)
            max_age = cfg.get("IMDB_RATINGS_MAX_AGE_DAYS")
            stale = isinstance(max_age, (int, float)) and age_days > float(max_age)
            with imdb_path.open("r", encoding="utf-8", errors="replace") as fh:
                rows = max(0, sum(1 for _ in fh) - 1)   # minus the header row
            add(f"  IMDb ratings dataset: present | {rows} ratings | "
                f"updated {_fmt_epoch(st.st_mtime)} ({age_days:.1f} days old"
                + (f", STALE > {max_age}d" if stale else "") + ")")
        except Exception as e:
            add(f"  IMDb ratings dataset: present (details unreadable — {s.redact(str(e))})")
    else:
        add("  IMDb ratings dataset: missing")

    section("DELETION PLAN & CURRENCY")
    try:
        data = _pending_file_data()
        forecast = pending_delete_forecast(cfg)
        plan_current = _pending_plan_current(cfg)
        add(f"  pending plan: {'present' if data else 'missing'}")
        add(f"  plan current under saved config: {plan_current}"
            + ("" if plan_current else "  → Automatic Cleanup is LOCKED until a new Simulate"))
        if data and not plan_current:
            # Name only WHICH keys drifted (key names, never their values) so the
            # report stays private while still pinpointing the staleness cause.
            reasons = []
            if (not isinstance(data.get("monitor_dirs"), list)
                    or sorted(str(d) for d in data.get("monitor_dirs") or []) != _normalized_monitor_dirs(cfg)):
                reasons.append("monitor_dirs")
            stamp = data.get("plan_config")
            if not isinstance(stamp, dict) or set(stamp) != set(_PLAN_CONFIG_KEYS):
                reasons.append("plan_config stamp shape")
            else:
                def _n(v):
                    if v is None or isinstance(v, (bool, str)):
                        return v
                    if isinstance(v, list):
                        return sorted(str(x) for x in v)
                    try:
                        return round(float(v), 3)
                    except (TypeError, ValueError):
                        return "invalid"
                reasons += [k for k in _PLAN_CONFIG_KEYS
                            if _n(stamp.get(k)) != _n(_plan_config_value(cfg, k))]
            add(f"  staleness cause (changed keys): {', '.join(reasons) or 'unknown'}")
        add(f"  marked queue: {forecast['count']} total | {forecast['ripe']} deletable now")
        if forecast["event_on"]:
            add(f"  next deletion event: {forecast['event_count']} movie(s), "
                f"{_format_reclaimed_size(forecast['event_bytes'])} on {forecast['event_on']}")
        # Sample rows carry NO title, NO path, NO host; only non-identifying
        # score / size / schedule fields, per the privacy requirement.
        entries = pending_deletion_entries(cfg)
        if entries:
            add("  sample marked entries (up to 5, de-identified):")
            for e in entries[:5]:
                add(f"    score={e.get('score')} | size={e.get('size') or 'n/a'} | "
                    f"delete_on={e.get('delete_on')} | days_remaining={e.get('days_remaining')}")
    except Exception as e:
        add(f"  plan state unavailable: {s.redact(str(e))}")

    section("RECENT ERRORS (last run log)")
    try:
        lp = log_path()
        if not lp.exists():
            add("  (no run log yet)")
        else:
            rx = _LOG_SECTION_RES["errors"]
            flagged = [ln.rstrip("\n") for ln in lp.read_text(encoding="utf-8").splitlines()
                       if ln.strip() and rx.search(ln)]
            if not flagged:
                add("  (none flagged in the last run)")
            else:
                add(f"  {len(flagged)} flagged line(s); showing the last {min(len(flagged), 20)}:")
                for ln in flagged[-20:]:
                    add(f"  {s.redact(ln)}")
    except Exception as e:
        add(f"  errors unreadable — {s.redact(str(e))}")

    add("")
    add("run/refresh flags at report time: "
        f"run_active={_run_active} summary_active={_summary_active}")

    return s.scrub("\n".join(L)) + "\n"


@app.route("/api/debug/report", methods=["POST"])
def api_debug_report():
    """Build a full sanitized diagnostic report and return it for the browser to
    download — nothing is written to the container filesystem."""
    # Mid-run the report would capture a half-written run log and a moving
    # deletion plan, and its live connection probes compete with the engine's
    # own API traffic; a snapshot like that misleads the bug report it's for.
    if _run_active:
        return _run_active_response("error")
    try:
        text = _build_debug_report()
        name = f"debug_report_{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
        return jsonify({"ok": True, "filename": name, "text": text})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/collections", methods=["POST"])
def api_collections():
    """List Plex collections and Jellyfin collections for the protected-collection pickers."""
    cfg = load_config()
    conn = _effective_connection_values(cfg)
    out = {
        "plex":     {"enabled": bool(cfg.get("USE_PLEX")),     "ok": False, "names": [], "error": "", "missing": []},
        "jellyfin": {"enabled": bool(cfg.get("USE_JELLYFIN")), "ok": False, "names": [], "error": "", "missing": []},
    }

    def _flag_missing(server_key: str, config_key: str):
        # Report saved selections the server no longer lists, but NEVER auto-remove
        # them: silently dropping a protection when a collection is renamed once let the
        # next run delete the very movies it was guarding. A stale name stays saved
        # (runs fail closed on it and say why); the user unchecks it deliberately.
        available = set(out[server_key].get("names") or [])
        selected = cfg.get(config_key) or []
        if isinstance(selected, str):
            selected = [p.strip() for p in selected.split(",") if p.strip()]
        out[server_key]["missing"] = [name for name in selected if name not in available]

    if cfg.get("USE_PLEX"):
        if conn.get("plex_url") and conn.get("plex_token"):
            try:
                out["plex"]["names"] = _plex_collection_names(conn["plex_url"], conn["plex_token"])
                out["plex"]["ok"] = True
                _flag_missing("plex", "PROTECTED_COLLECTIONS")
            except Exception as e:
                out["plex"]["error"] = f"Could not read Plex collections: {e}"
        else:
            out["plex"]["error"] = "Add the Plex URL and token above and Save, then scan."
    if cfg.get("USE_JELLYFIN"):
        if conn.get("jellyfin_url") and conn.get("jellyfin_key"):
            try:
                out["jellyfin"]["names"] = _jellyfin_boxset_names(conn["jellyfin_url"], conn["jellyfin_key"])
                out["jellyfin"]["ok"] = True
                _flag_missing("jellyfin", "JELLYFIN_PROTECTED_COLLECTIONS")
            except Exception as e:
                out["jellyfin"]["error"] = f"Could not read Jellyfin collections: {e}"
        else:
            out["jellyfin"]["error"] = "Add the Jellyfin URL and API key above and Save, then scan."
    return jsonify(out)



def _judge_media_paths(*, pre, use_plex, use_jellyfin, tautulli_connected,
                       jellyfin_connected, dirs_sig, errors, warnings,
                       add_error, add_warning) -> tuple[list, bool]:
    """Sample each connected server's reported file paths against the library.

    The question is whether the files a run would act on are actually there:
    a server describing a library that is not the one mounted is the wrong
    -library tripwire, and it has to stop a run rather than delete against a
    backup. A few misses are ordinary and only warn.

    errors and warnings are the caller's own lists, passed in rather than
    returned, because a retained verdict is replayed by slicing what THIS
    call appended — and add_error/add_warning carry field highlighting the
    plain lists do not.

    Returns the per-server compatibility rows and whether any of them blocks.
    """
    media_path_compatibility: list[dict] = []
    media_path_blocker = False
    _dirs_sig = dirs_sig
    # Re-judging: a fresh walk, always — the fingerprint index's short
    # cache exists to share ONE walk between the two servers' checks
    # below, never to judge a walk from before a mount fix.
    _MEDIA_FP_INDEX["at"] = 0.0
    _judged_any = False

    def _store_media_verdict(_name, _e0, _w0):
        """Replace one server's stored verdict slot — called only when its
        check actually produced a compat entry, so a sampler failure or a
        down server keeps the previous verdict instead of clearing it."""
        nonlocal _judged_any
        if media_path_compatibility and media_path_compatibility[-1].get("server") == _name:
            _c = media_path_compatibility[-1]
            _MEDIA_PATH_VERDICT["servers"][_name] = {
                "compat": dict(_c),
                "errors": list(errors[_e0:]),
                "warnings": list(warnings[_w0:]),
                "blocker": bool(_c.get("checked") and not _c.get("ok")),
                "at": time.time(),
            }
            _judged_any = True

    # One judgement, run per connected server. The two used to be written out
    # twice, identical apart from five places each names itself — which is a
    # standing invitation to leave a Jellyfin user being told to press the
    # Tautulli button. The names live in one table instead, and
    # test_media_path_messages pins every slot of every message.
    SERVERS = (
        # on,           connected,           label,             sample key,
        #   reports / debug button / stale-entry noun / the rescan that clears it
        (use_plex,      tautulli_connected,  "Plex/Tautulli",   "tautulli_paths",
         "Plex", "Tautulli", "Plex", "a Plex/Tautulli rescan clears this"),
        (use_jellyfin,  jellyfin_connected,  "Jellyfin",        "jellyfin_paths",
         "Jellyfin", "Jellyfin", "Jellyfin", "a Jellyfin library scan clears this"),
    )
    for on, connected, label, sample_key, reports, button, stale, clears in SERVERS:
        if not (on and connected):
            continue
        _e0, _w0 = len(errors), len(warnings)
        try:
            compat = _media_path_compatibility_state(label, _sample_result(pre, sample_key))
            media_path_compatibility.append(compat)
            if compat["checked"] == 0:
                if compat.get("folder_entries"):
                    add_warning(f"{label} returned only folder entries "
                                f"({compat['folder_entries']}) — no movie file paths to check.")
                else:
                    add_warning(f"{label} connected, but no movie paths were available to check.")
            elif not compat["ok"]:
                media_path_blocker = True
                if compat.get("mismatch_problem"):
                    add_error(
                        f"{compat['size_mismatched']} of {compat['size_checked']} sampled files "
                        f"under {FILESYSTEM_CHECK_PATH} differ in size from what {reports} "
                        f"reports — that looks like the wrong library (a backup or stale copy "
                        f"mounted, or a {reports} library describing different files), not a few "
                        "quality upgrades. Check the /library mount, or rescan "
                        f"{reports}, then recheck.")
                else:
                    _ex = (compat.get("unmatched_examples") or [""])[0]
                    add_error(
                        f"{compat['unmatched']} of {compat['checked']} sampled {reports} paths "
                        f"match nothing under {FILESYSTEM_CHECK_PATH}"
                        + (f" — for example {_ex}" if _ex else "") + ". Files match by "
                        "folder + file name (mounts never need to line up), so the "
                        f"folder and file names {reports} reports must exist under the "
                        f"library. Check the /library mount, then recheck; the "
                        f"{button} debug button under Connections shows each sampled "
                        "path and why it did or didn't match.")
            elif compat["unmatched"]:
                _ex = (compat.get("unmatched_examples") or [""])[0]
                add_warning(
                    f"{compat['unmatched']} of {compat['checked']} sampled {reports} entries "
                    f"match no file under {FILESYSTEM_CHECK_PATH}"
                    + (f" (for example {_ex})" if _ex else "") + " — usually a stale "
                    f"{stale} entry whose file was renamed, upgraded, or removed since "
                    "its last scan. It scans as missing and is never deleted; "
                    f"{clears}.")
        except Exception:
            add_warning(f"Could not check {reports} media paths.")
        _store_media_verdict(label, _e0, _w0)
    # A server whose judge did NOT complete this run (its sampler failed,
    # or it was down) keeps showing its RETAINED verdict — otherwise one
    # hiccup would flap an active error to "no problem" for exactly one
    # response and back on the next probe.
    for _name, _on in (("Plex/Tautulli", use_plex), ("Jellyfin", use_jellyfin)):
        if not _on or any(c.get("server") == _name for c in media_path_compatibility):
            continue
        _slot = _MEDIA_PATH_VERDICT["servers"].get(_name)
        if not _slot:
            continue
        media_path_compatibility.append(dict(_slot["compat"]))
        for _e in _slot["errors"]:
            add_error(_e)
        for _w in _slot["warnings"]:
            add_warning(_w)
        if _slot["blocker"]:
            media_path_blocker = True
    # The signature stamps only when SOMETHING was judged: a re-judge
    # where no media server answered keeps retrying on later probes
    # instead of recording "nothing wrong" it never verified.
    if _judged_any:
        _MEDIA_PATH_VERDICT["sig"] = _dirs_sig
    return media_path_compatibility, media_path_blocker


def _connection_health_state(cfg: dict | None = None, *, probe: bool = False,
                             media_paths: bool | None = None) -> dict:
    """Validate connection health for the services MediaReducer talks to.

    Checks selected-API reachability and whether connected media-server paths resolve
    to real files under the /library mount. Does NOT validate headroom, library caps,
    scoring, or other thresholds. Tautulli is required only when Plex is selected;
    Jellyfin is checked when selected; optional Plex/Radarr helpers are probed once
    their URL and key are present, so dependent UI sections stay locked until each API
    connects. Blank optional URLs raise no warning/error. MediaReducer-owned folders
    are also checked for read/write, since cache, logs, IMDb data, saves, and runs all
    depend on them.

    media_paths controls the media-path accuracy check (server samples + a full
    library walk — the expensive part): True runs it (Check for Errors), None
    runs it only when the monitored paths changed since the last verdict, and
    every probe that doesn't run it REPLAYS the stored verdict unchanged — an
    active media-path error neither re-runs the check nor disappears on an
    unrelated save."""
    cfg = cfg or load_config()
    mounts = _appdata_mount_state()
    filesystem = _filesystem_rw_state()
    conn = _effective_connection_values(cfg)
    errors: list[str] = []
    warnings: list[str] = []
    highlights: list[str] = []
    mount_highlights: list[str] = []
    tautulli_blocker = False

    cleanup_section_value = str(cfg.get("RADARR_OVERSEERR_SECTION_ID") or "").strip().lower()
    cleanup_auto_section = cleanup_section_value == "auto"
    plex_connected = False
    tautulli_connected = False
    radarr_connected = False
    sonarr_connected = False
    use_plex = bool(cfg.get("USE_PLEX"))
    use_jellyfin = bool(cfg.get("USE_JELLYFIN"))
    jellyfin_connected = False
    jellyfin_blocker = False
    media_path_blocker = False
    filesystem_blocker = False
    media_path_compatibility: list[dict] = []
    no_server_selected = not (use_plex or use_jellyfin)
    _dirs_sig = _monitor_dirs_signature(cfg)
    run_media_check = bool(probe and (media_paths is True
                                      or (media_paths is None
                                          and _dirs_sig != _MEDIA_PATH_VERDICT["sig"])))
    # Every probe below reads from here. Started together and awaited once, so a
    # save waits for the slowest server rather than the sum of all of them. The
    # media-path samplers ride along only when this probe re-judges the paths.
    pre = (_prefetch_connection_probes(conn, use_plex=use_plex, use_jellyfin=use_jellyfin,
                                       media_paths=run_media_check)
           if probe else {})

    def dedupe(items):
        out = []
        seen = set()
        for item in items:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def add_error(message: str, fields: list[str] | tuple[str, ...] = (), mounts_to_highlight: list[str] | tuple[str, ...] = ()):  # keep order / de-dupe later
        errors.append(message)
        highlights.extend(fields)
        mount_highlights.extend(mounts_to_highlight)

    def add_warning(message: str, fields: list[str] | tuple[str, ...] = (), mounts_to_highlight: list[str] | tuple[str, ...] = ()):
        warnings.append(message)
        highlights.extend(fields)
        mount_highlights.extend(mounts_to_highlight)

    # Hand-edited config.json with invalid values: everything locks (runs and config
    # edits) until the values are reset or MediaReducer is reset.
    invalid_config = list(_CONFIG_FILE_ISSUES)
    if invalid_config:
        add_error(
            "config.json was edited outside MediaReducer and contains invalid values. "
            "Everything is locked until they are reset to defaults or MediaReducer is reset.",
        )
        for issue in invalid_config:
            add_error(f"{issue['key']} {issue['message']}.", [issue["key"]])

    if not filesystem.get("ok", True):
        filesystem_blocker = True
        # Include the captured OSError; it distinguishes a read-only mount from a
        # permissions problem.
        _fs_detail = (filesystem.get("errors") or [""])[0]
        add_error(
            "MediaReducer cannot read and write its config/log folders"
            + (f" — {_fs_detail}" if _fs_detail else "")
            + ". Check the /config mount and its permissions, then recheck.",
        )

    if no_server_selected:
        # Both servers off is saveable but non-functional; health stays red until one
        # media server API is configured. SERVER_SOFTWARE is a pseudo-field that
        # highlights the checkbox row.
        add_error(
            "No media server is enabled — select Plex or Jellyfin under Server software and configure its API.",
            ["SERVER_SOFTWARE"],
        )

    if use_plex:
        # Required: Tautulli URL/API key + reachable API. The /tautulli appdata mount
        # exists ONLY so Auto Detect can read the key from config.ini; the engine
        # talks to Tautulli over HTTP. So a missing mount warns (Auto Detect
        # unavailable) rather than disabling the URL/key fields; the optional
        # volume must never block typing them in.
        # Only worth saying while the key is still missing: it explains why Auto
        # Detect won't help. Once the key is in, the mount has no job left, and
        # the warning was a permanent nag for anyone who chose not to mount it.
        tautulli_ok = bool(mounts["tautulli"].get("ok"))
        if not tautulli_ok and not conn.get("tautulli_key"):
            add_warning(
                "Tautulli appdata is not mounted — Auto Detect can't read the key. "
                "Enter the URL and API key manually.",
                mounts_to_highlight=["tautulli"],
            )
        # A blank URL fills from the server default once the key is present, so the key
        # is the one thing to ask for. A blank URL WITH a key means no default host
        # could be detected.
        if not conn.get("tautulli_key"):
            tautulli_blocker = True
            add_error(
                "Enter the Tautulli API key — the URL fills in automatically.",
                ["TAUTULLI_API_KEY"],
            )
        elif not conn.get("tautulli_url"):
            tautulli_blocker = True
            add_error(
                "Tautulli URL is missing and no default address could be detected — enter it manually.",
                ["TAUTULLI_URL"],
            )
        if probe and conn.get("tautulli_url") and conn.get("tautulli_key"):
            ok, msg = _probe_result(pre, "tautulli")
            tautulli_connected = ok
            if not ok:
                tautulli_blocker = True
                add_error(
                    "Tautulli did not connect. Check the URL and API key.",
                    ["TAUTULLI_URL", "TAUTULLI_API_KEY"],
                    mounts_to_highlight=["tautulli"],
                )

        # Plex is optional and the token is its on/off switch: no token means Plex-only
        # features stay locked with no warning, whatever the URL holds. With a token, a
        # blank URL resolves to its default and real connection/auth problems surface.
        plex_url = conn.get("plex_url")
        plex_token = conn.get("plex_token")
        plex_has_url = bool(plex_url)
        plex_has_creds = bool(plex_url and plex_token)
        # No token = the user does not use direct Plex access: dependent features stay
        # locked silently, whether or not a URL was typed.
        if plex_token and not plex_has_url:
            add_warning(
                "Plex token is set but no URL default could be detected — enter the Plex URL.",
                ["PLEX_URL"],
            )
        if probe and plex_has_creds:
            ok, msg = _probe_result(pre, "plex")
            plex_connected = ok
            if not ok:
                add_warning(
                    "Plex did not connect. Check the URL and token.",
                    ["PLEX_URL", "PLEX_TOKEN"],
                )
                if cleanup_auto_section:
                    add_warning(
                        "Radarr section auto-detection needs Plex to connect.",
                        ["PLEX_URL", "PLEX_TOKEN"],
                                )

    # Jellyfin (native API), only checked when selected. Its API key must be created by
    # hand, so validate URL + key by probing /System/Info.
    if use_jellyfin:
        jf_url = conn.get("jellyfin_url")
        jf_key = conn.get("jellyfin_key")
        # Same shape as Tautulli: the key is the ask; the URL only surfaces when it
        # stayed blank because no default host could be detected.
        if not jf_key:
            jellyfin_blocker = True
            add_error("Enter the Jellyfin API key — the URL fills in automatically.", ["JELLYFIN_API_KEY"])
        elif not jf_url:
            jellyfin_blocker = True
            add_error("Jellyfin URL is missing and no default address could be detected — enter it manually.", ["JELLYFIN_URL"])
        if probe and jf_url and jf_key:
            ok, msg = _probe_result(pre, "jellyfin")
            jellyfin_connected = ok
            if not ok:
                jellyfin_blocker = True
                add_error("Jellyfin did not connect. Check the URL and API key.", ["JELLYFIN_URL", "JELLYFIN_API_KEY"])

    # Radarr is optional and its API key is the on/off switch: no key means Radarr
    # integration stays locked with no warning, whatever the URL holds. With a key, a
    # blank URL resolves to its default and connection/auth problems surface.
    radarr_url = conn.get("radarr_url")
    radarr_key = conn.get("radarr_key")
    radarr_has_url = bool(radarr_url)
    radarr_has_creds = bool(radarr_url and radarr_key)
    # No key = the user does not use Radarr: cleanup stays locked silently, whether or
    # not a URL was typed.
    if radarr_key and not radarr_has_url:
        add_warning(
            "Radarr API key is set but no URL default could be detected — enter the Radarr URL. Optional cleanup is locked.",
            ["RADARR_URL"],
        )
    if probe and radarr_has_creds:
        ok, msg = _probe_result(pre, "radarr")
        radarr_connected = ok
        if not ok:
            add_warning(
                "Radarr did not connect. Optional cleanup is locked.",
                ["RADARR_URL", "RADARR_API_KEY"],
                )

    # Sonarr is informational: it gates nothing yet, so a bad connection warns
    # about itself and nothing else.
    sonarr_url = conn.get("sonarr_url")
    sonarr_key = conn.get("sonarr_key")
    if sonarr_key and not sonarr_url:
        add_warning(
            "Sonarr API key is set but no URL default could be detected — enter the Sonarr URL.",
            ["SONARR_URL"],
        )
    if probe and sonarr_url and sonarr_key:
        ok, msg = _probe_result(pre, "sonarr")
        sonarr_connected = ok
        if not ok:
            add_warning(
                "Sonarr did not connect.",
                ["SONARR_URL", "SONARR_API_KEY"],
                )

    # A missing or empty library root makes every path sample fail, and "paths do
    # not line up" then sends people hunting for a Plex prefix mismatch that isn't
    # there. Reword that case; do NOT widen it. This replaces the alignment
    # complaint exactly where the alignment check would have run, so a state that
    # blocked before still blocks and one that did not still does not.
    _library = mounts.get("library") or {}
    library_missing = probe and not _library.get("ok", True) and (
        (use_plex and tautulli_connected) or (use_jellyfin and jellyfin_connected))
    if library_missing:
        media_path_blocker = True
        if not _library.get("mounted"):
            add_error(
                f"No media library at {FILESYSTEM_CHECK_PATH}. The container has no "
                "library mounted there, so nothing can be scanned or deleted. Check "
                "the /library mapping, then recheck.")
        else:
            add_error(
                f"{FILESYSTEM_CHECK_PATH} is mounted but empty. That is usually a "
                "share or disk that has not come up yet rather than an empty "
                "library. Check the mount, then recheck.")

    if probe and not library_missing and not run_media_check:
        # This probe does NOT re-judge the media paths: replay each stored
        # per-server verdict unchanged — the same compat facts, error/warning
        # lines, and blocker state — so an active problem stays visible (and
        # keeps blocking) without the check's samples and library walk running
        # on every save. Check for Errors or a monitored-paths save re-judges.
        # Only servers still ENABLED replay; deselecting one retires its slot.
        for _name, _on in (("Plex/Tautulli", use_plex), ("Jellyfin", use_jellyfin)):
            _slot = _MEDIA_PATH_VERDICT["servers"].get(_name)
            if not _on or not _slot:
                continue
            media_path_compatibility.append(dict(_slot["compat"]))
            for _e in _slot["errors"]:
                add_error(_e)
            for _w in _slot["warnings"]:
                add_warning(_w)
            if _slot["blocker"]:
                media_path_blocker = True

    if probe and not library_missing and run_media_check:
        media_path_compatibility, media_path_blocker = _judge_media_paths(
            pre=pre, use_plex=use_plex, use_jellyfin=use_jellyfin,
            tautulli_connected=tautulli_connected, jellyfin_connected=jellyfin_connected,
            dirs_sig=_dirs_sig, errors=errors, warnings=warnings,
            add_error=add_error, add_warning=add_warning)

    # Threshold values, library-cap readiness, and other run-mode blockers live in the
    # Dashboard buttons, Config validation, and the engine's own safety checks. Only
    # API reachability and media path compatibility are decided here.
    errors = dedupe(errors)
    warnings = dedupe(warnings)
    highlights = dedupe(highlights)
    mount_highlights = dedupe(mount_highlights)

    # Radarr connection problems show on the Radarr URL/API fields; the Optional
    # Radarr cleanup box is never painted red here. Health checks only report
    # connection state; the save endpoint force-disables cleanup after SAVED
    # credentials fail a probe, not while typing.
    radarr_cleanup_forced_disabled = False
    # Deletion is gated on every selected server being healthy. The engine reads
    # library and watch history from whichever servers are enabled (Plex via Tautulli,
    # and/or Jellyfin), so a Jellyfin-only setup can run once Jellyfin connects; no
    # Tautulli required.
    critical_ok = (
        (not invalid_config)
        and (not no_server_selected)
        and (not tautulli_blocker if use_plex else True)
        and (not jellyfin_blocker if use_jellyfin else True)
        and not media_path_blocker
        and not filesystem_blocker
    )
    if invalid_config:
        required_tooltip = "Fix the invalid config file on the Configuration page first."
    elif no_server_selected:
        # Shown on the dashboard run buttons too, where the server-software checkboxes
        # aren't visible; point at the fix, not at controls the user can't see.
        required_tooltip = "Fix the API connections on the Configuration page first."
    elif errors:
        required_tooltip = errors[0]
    else:
        required_tooltip = ""

    has_visible_issues = bool(errors) or bool(warnings)

    return {
        "ok": True,
        "critical_ok": critical_ok,
        "invalid_config": invalid_config,
        "severity": "error" if errors else ("warning" if warnings else "ok"),
        "summary": "Ready." if not errors and not warnings else ("Fix the errors below." if errors else "Ready with warnings."),
        "errors": errors,
        "warnings": warnings,
        "highlights": highlights,
        "radarr_cleanup_forced_disabled": radarr_cleanup_forced_disabled,
        "media_path_blocker": media_path_blocker,
        "filesystem_blocker": filesystem_blocker,
        "media_path_compatibility": media_path_compatibility,
        "has_visible_issues": has_visible_issues,
        "mount_highlights": mount_highlights,
        "probed": probe,
        "appdata": mounts,
        "radarr_connected": radarr_connected,
        "sonarr_connected": sonarr_connected,
        "plex_connected": plex_connected,
        "tautulli_connected": tautulli_connected,
        "jellyfin_connected": jellyfin_connected,
        "required_tooltip": required_tooltip,
    }

# The connection-related change detectors share one shape: serialize a fixed set of
# config keys and compare the strings. `strip` compares values as trimmed strings
# (user-typed fields) rather than raw.
_API_CREDENTIAL_KEYS = (
    "USE_PLEX", "USE_JELLYFIN",
    "TAUTULLI_URL", "TAUTULLI_API_KEY", "PLEX_URL", "PLEX_TOKEN",
    "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "RADARR_URL", "RADARR_API_KEY",
    "SONARR_URL", "SONARR_API_KEY",
    "RADARR_OVERSEERR_SECTION_ID",
)


def _config_signature(cfg: dict | None, keys, *, strip: bool = False) -> str:
    cfg = cfg or load_config()
    if strip:
        payload = {key: (str(cfg.get(key)).strip() if cfg.get(key) is not None else None) for key in keys}
    else:
        payload = {key: cfg.get(key) for key in keys}
    return json.dumps(payload, sort_keys=True, default=str)


def _connection_health_signature(cfg: dict | None = None) -> str:
    """Hash only the values that can change connection health. Includes the
    invalid-config issues so a hand edit invalidates the cached health, and
    MONITOR_DIRS so a monitored-paths save re-probes — editing the paths
    usually accompanies a mount/library change, and the media-path check must
    re-run against the CURRENT disk to show a problem cleared (or appeared)
    rather than serving the cached verdict."""
    return (_config_signature(cfg, _API_CREDENTIAL_KEYS + (
        "PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS", "OUTPUT_DIR",
    )) + "|dirs:" + _monitor_dirs_signature(cfg)
        + json.dumps(_CONFIG_FILE_ISSUES, sort_keys=True))


def _api_config_signature(cfg: dict | None = None) -> str:
    """Hash user-edited API/connection settings that should pause Automatic Cleanup when changed."""
    return _config_signature(cfg, _API_CREDENTIAL_KEYS, strip=True)


def _radarr_section_detection_signature(cfg: dict | None = None) -> str:
    """Hash the saved credentials that can change Radarr/Plex section detection."""
    return _config_signature(cfg, ("PLEX_URL", "PLEX_TOKEN", "RADARR_URL", "RADARR_API_KEY"), strip=True)


def _refresh_connection_health_cache(cfg: dict | None = None, *, probe: bool = True,
                                     media_paths: bool | None = None) -> dict:
    """Run the connection check and store it for first-page display."""
    cfg = cfg or load_config()
    sig = _connection_health_signature(cfg)
    try:
        health = _connection_health_state(cfg, probe=probe, media_paths=media_paths)
    except Exception as e:
        health = {
            "ok": False,
            "critical_ok": False,
            "severity": "error",
            "summary": "Could not check connections.",
            "errors": [str(e)],
            "warnings": [],
            "highlights": [],
            "mount_highlights": [],
            "has_visible_issues": True,
            "probed": probe,
            "appdata": _appdata_mount_state(),
            "filesystem_blocker": True,
            "required_tooltip": "Connection check failed.",
            "invalid_config": list(_CONFIG_FILE_ISSUES),
        }
    with _connection_health_cache_lock:
        _connection_health_cache.update({
            "signature": sig,
            "health": health,
            "checked_at": time.time(),
        })
    return health


def _kick_startup_health_check_and_summary(cfg: dict | None = None):
    """On startup, probe connections first, then refresh library stats.

    The dashboard Storage card shows a cached library size from the store that can be
    stale if MediaReducer was stopped while the library changed. This worker runs the
    same health check the UI uses; when connections are good it immediately runs a quiet
    Summary/debug_info refresh so the store catches up rather than waiting for the
    first scheduled tick. That Summary also restarts the clock, so startup is a fresh
    interval reset."""
    cfg = dict(cfg or load_config())

    def _worker():
        # Probe connections either way (onboarding shows their status), but only
        # refresh the library Summary when there's something monitored; no paths
        # means nothing to size, matching the idle scheduler. Paused ("off")
        # still refreshes: its ticks keep the storage numbers current too.
        health = _refresh_connection_health_cache(cfg, probe=True)
        if health.get("critical_ok", False) and _has_monitored_dirs(cfg):
            run_summary()

    threading.Thread(target=_worker, daemon=True, name="engine-startup-summary").start()


def _connection_health_for_ui(cfg: dict | None = None) -> dict:
    """Return the last matching connection probe without launching new probes.

    Connection health is sampled: once at startup, on Check for Errors, and after saved
    API settings change. Cached results are reused only for the same
    connection-relevant config signature, so an old good check can't keep Automatic Cleanup enabled
    after the API settings changed."""
    cfg = cfg or load_config()
    sig = _connection_health_signature(cfg)
    with _connection_health_cache_lock:
        cached = _connection_health_cache.get("health")
        cached_sig = _connection_health_cache.get("signature")
    if cached and cached_sig == sig:
        return cached
    # A startup/manual probe may still be running. Return a fast, non-network view so
    # the page can render without creating a second background check.
    return _connection_health_state(cfg, probe=False)


# How long a good probe stands in for a fresh one on a config save. Matched to
# the scheduler tick, which re-probes on its own, so the cached answer is never
# older than one tick behind what the scheduler is already acting on.
_HEALTH_REPROBE_AFTER_SECONDS = SCHEDULE_INTERVAL_MINUTES * 60


def _health_for_config_save(cfg: dict) -> dict:
    """The connection health a save acts on, probed only when it could have moved.

    A save used to probe every service every time, on the reasoning that
    connections can fail without any field changing. True, but this is the wrong
    place to catch it, and it is not what protects anything: a run re-probes and
    REFUSES TO START on a failure (api_run), the scheduler tick re-probes before
    any automatic cleanup, and the engine fails closed on any API error mid-run.
    Saving a notification toggle or a scoring curve cannot make a media server
    unreachable, so the probe was pure latency on the Save button — and the page
    that renders the result already reuses this same cache without probing.

    Re-probe when the answer could genuinely differ:
      • a connection-relevant value was edited (the signature covers every key
        the health check reads, so this catches all of them);
      • the last probe FAILED — the user is most likely saving because they are
        fixing it, and recovery has to be noticed immediately;
      • the cached answer predates the last tick, or there isn't one.
    Otherwise the cached probe stands. The staleness that leaves is bounded by
    the same tick that would have corrected it anyway."""
    sig = _connection_health_signature(cfg)
    with _connection_health_cache_lock:
        cached = _connection_health_cache.get("health")
        cached_sig = _connection_health_cache.get("signature")
        checked_at = _connection_health_cache.get("checked_at") or 0
    if (cached and cached_sig == sig
            and cached.get("probed") and cached.get("critical_ok")
            and (time.time() - checked_at) < _HEALTH_REPROBE_AFTER_SECONDS):
        return cached
    return _refresh_connection_health_cache(cfg, probe=True)


def _norm_service_path(value: str | None) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    raw = re.sub(r"/+", "/", raw)
    return raw.rstrip("/").lower() if len(raw) > 1 else raw.lower()


def _path_under_or_same(path: str | None, root: str | None) -> bool:
    p = _norm_service_path(path)
    r = _norm_service_path(root)
    return bool(p and r and (p == r or p.startswith(r.rstrip("/") + "/")))


def _detect_radarr_plex_section(cfg: dict | None = None) -> dict:
    """Infer the Plex movie section Radarr manages by comparing paths."""
    cfg = cfg or load_config()
    conn = _effective_connection_values(cfg)
    result = {
        "ok": False,
        "section_id": None,
        "section_name": None,
        "method": None,
        "message": "Unable to detect Radarr's Plex section.",
        "sections": [],
        "counts": {},
    }

    radarr_url = (conn.get("radarr_url") or "").rstrip("/")
    radarr_key = conn.get("radarr_key") or ""
    plex_url = (conn.get("plex_url") or "").rstrip("/")
    plex_token = conn.get("plex_token") or ""
    if not radarr_url or not radarr_key:
        result["message"] = "Radarr URL/API key are not available."
        return result
    if not plex_url or not plex_token:
        result["message"] = "Plex URL/token are not available."
        return result

    try:
        movies = _json_request(f"{radarr_url}/api/v3/movie", headers={"X-Api-Key": radarr_key}, timeout=20) or []
    except Exception as e:
        result["message"] = f"Could not read Radarr movies: {e}"
        return result
    if not isinstance(movies, list) or not movies:
        result["message"] = "Radarr returned no movies to compare."
        return result

    radarr_paths = []
    radarr_roots = []
    for movie in movies:
        if not isinstance(movie, dict):
            continue
        if movie.get("path") or movie.get("folderName"):
            radarr_paths.append(str(movie.get("path") or movie.get("folderName")))
        if movie.get("rootFolderPath"):
            radarr_roots.append(str(movie.get("rootFolderPath")))
    if not radarr_paths and not radarr_roots:
        result["message"] = "Radarr movies did not include usable paths."
        return result

    try:
        plex_data = _plex_get(plex_url, plex_token, "/library/sections")
    except Exception as e:
        result["message"] = f"Could not read Plex sections: {e}"
        return result
    plex_dirs = ((plex_data or {}).get("MediaContainer") or {}).get("Directory") or []
    if isinstance(plex_dirs, dict):
        plex_dirs = [plex_dirs]

    sections = []
    for sec in plex_dirs:
        if not isinstance(sec, dict):
            continue
        if sec.get("type") not in (None, "movie"):
            continue
        sid = str(sec.get("key") or "").strip()
        locations = [str(loc.get("path", "")) for loc in _as_list(sec.get("Location"))
                     if isinstance(loc, dict) and loc.get("path")]
        if sid and locations:
            sections.append({"id": sid, "name": str(sec.get("title") or sid), "locations": locations})
    result["sections"] = sections
    if not sections:
        result["message"] = "Plex returned no movie sections with folder locations."
        return result

    def unique_winner(counts: dict):
        positive = [(sid, count) for sid, count in counts.items() if count > 0]
        if len(positive) != 1:
            return None
        sid, count = positive[0]
        return sid

    counts = {s["id"]: 0 for s in sections}
    for path in radarr_paths:
        for sec in sections:
            if any(_path_under_or_same(path, loc) for loc in sec["locations"]):
                counts[sec["id"]] += 1
    # A section wins if it's the ONLY one any Radarr movie path falls under. We don't
    # require every movie to match: a single stray unresolvable path shouldn't void an
    # otherwise-unambiguous match.
    winner = unique_winner(counts)
    method = "path-prefix"

    if not winner and radarr_roots:
        counts = {s["id"]: 0 for s in sections}
        roots = sorted(set(radarr_roots))
        for root in roots:
            for sec in sections:
                if any(_path_under_or_same(root, loc) or _path_under_or_same(loc, root) for loc in sec["locations"]):
                    counts[sec["id"]] += 1
        winner = unique_winner(counts)
        method = "root-prefix"

    if not winner:
        counts = {s["id"]: 0 for s in sections}
        names = []
        for value in sorted(set(radarr_roots or [])) or radarr_paths:
            name = Path(value.replace("\\", "/")).name.lower()
            if name:
                names.append(name)
        for name in names:
            for sec in sections:
                if name in {Path(loc).name.lower() for loc in sec["locations"] if Path(loc).name}:
                    counts[sec["id"]] += 1
        winner = unique_winner(counts)
        method = "folder-name"

    result["counts"] = counts
    if not winner:
        nonzero = {sid: count for sid, count in counts.items() if count}
        if len(nonzero) > 1:
            result["message"] = f"Radarr paths matched more than one Plex section: {nonzero}. Set the section manually."
        else:
            result["message"] = "Radarr paths did not match any Plex movie section. Set the section manually."
        return result

    sec = next((s for s in sections if s["id"] == winner), None)
    result.update({
        "ok": True,
        "section_id": winner,
        "section_name": sec["name"] if sec else winner,
        "method": method,
        "method_label": _radarr_section_method_label(method),
        "message": f"Detected: {winner}" + (f" ({sec['name']})" if sec else "") + (f" via {_radarr_section_method_label(method)}" if method else ""),
    })
    return result


NO_MONITORED_DIRS_MESSAGE = "No monitored library paths are set — add one on the Configuration page."
SIMULATE_REQUIRED_MESSAGE = "Over space limits — run Simulate to review the deletion plan first."


def _has_monitored_dirs(cfg: dict | None = None) -> bool:
    """Return True when at least one monitored library path is configured."""
    cfg = cfg or load_config()
    return bool(cfg.get("MONITOR_DIRS") or [])

def _cleanup_button_state(cfg: dict | None = None, disk: dict | None = None) -> dict:
    cfg = cfg or load_config()
    threshold_state = _space_threshold_state(cfg, disk)
    health = _connection_health_for_ui(cfg)
    has_monitored_dirs = _has_monitored_dirs(cfg)

    if not health.get("critical_ok", True):
        # A healthy media server is required for every run button, including Summary;
        # this takes priority over monitored-path and threshold warnings.
        msg = health.get("required_tooltip") or "Connect the selected media server first."
        # These tooltips show on the DASHBOARD's run buttons, but the error strings are
        # written for the Config page ("Enter the API key…", "…then recheck"); point at
        # where the fix lives.
        if "Configuration page" not in msg:
            msg = msg.rstrip(".") + " — fix it on the Configuration page."
        return {
            "summary_disabled": True,
            "summary_tooltip": msg,
            "simulate_disabled": True,
            "simulate_tooltip": msg,
            "cleanup_disabled": True,
            "cleanup_tooltip": msg,
            "debug_disabled": True,
            "debug_tooltip": msg,
            "space_satisfied": False,
            "space_thresholds": threshold_state,
            "connection_health": health,
        }

    if not has_monitored_dirs:
        # No monitored folders is the highest-priority non-connection Dashboard block:
        # nothing can be simulated or deleted until the allow-list has at least one path
        # (use / to monitor all of /library).
        return {
            "summary_disabled": False,
            "summary_tooltip": "",
            "simulate_disabled": True,
            "simulate_tooltip": NO_MONITORED_DIRS_MESSAGE,
            "cleanup_disabled": True,
            "cleanup_tooltip": NO_MONITORED_DIRS_MESSAGE,
            "debug_disabled": True,
            "debug_tooltip": NO_MONITORED_DIRS_MESSAGE,
            "space_satisfied": False,
            "space_thresholds": threshold_state,
            "connection_health": health,
        }

    # Everything configured and connected, but if every space limit is satisfied a
    # Cleanup would delete nothing, so ghost the Cleanup button with the reason. Simulate
    # never ghosts on satisfied limits in any mode: its job includes building/refreshing
    # the standing marked & eligible queue, which exists regardless of breach state.
    # Unknown values fail OPEN (buttons stay enabled; the engine is the authority
    # and no-ops safely), like _deletion_limits_exceeded.
    satisfied_msg = ""
    if threshold_state["ok_for_simulate"] or threshold_state["ok_for_cleanup"]:
        try:
            _lib_gb = library_stats().get("library_gb")
        except Exception:
            _lib_gb = None
        if not _deletion_limits_exceeded(cfg, disk, _lib_gb):
            satisfied_msg = "Space limits are satisfied — a run would delete nothing."

    # Over limits without a plan computed under these exact thresholds, the manual Cleanup
    # Run ghosts too; it deletes immediately, so the user must have seen what a run
    # would remove. Simulate stays available: running it is how Automatic Cleanup gets un-ghosted.
    # Redline-only mode never masks the plan requirement behind satisfied limits.
    rl_only = _redline_only_mode_cfg(cfg)
    simulate_required = bool(threshold_state.get("simulate_required")) and (rl_only or not satisfied_msg)

    return {
        "summary_disabled": False,
        "summary_tooltip": "",
        "simulate_disabled": not threshold_state["ok_for_simulate"],
        "simulate_tooltip": threshold_state["simulate_tooltip"],
        # Debug Cleanup replays the standing marked queue, so it needs a CURRENT
        # plan to exist: ghost it (like the Cleanup button) until a Simulate has built/refreshed
        # the queue under the current settings. Uses the Simulate gate for hard
        # config errors, but ignores the live/safety thresholds (it deletes nothing).
        "debug_disabled": (not threshold_state["ok_for_simulate"]
                           or bool(threshold_state.get("simulate_required"))),
        "debug_tooltip": (threshold_state["simulate_tooltip"]
                          or ("Run Simulate first — Debug Cleanup replays the marked "
                              "& eligible queue a Simulate builds."
                              if threshold_state.get("simulate_required") else "")),
        "cleanup_disabled": (not threshold_state["ok_for_cleanup"] or bool(satisfied_msg)
                          or simulate_required),
        "cleanup_tooltip": (threshold_state["cleanup_tooltip"]
                         or (threshold_state.get("simulate_required_message", "") if simulate_required and rl_only else "")
                         or satisfied_msg
                         or (threshold_state.get("simulate_required_message", "") if simulate_required else "")),
        "space_satisfied": bool(satisfied_msg),
        "space_thresholds": threshold_state,
        "connection_health": health,
    }


def _cache_clear_state() -> dict:
    """Return whether the cache store exists and can be cleared from the UI."""
    p = db_path()
    state = {
        "exists": False,
        "can_clear": False,
        "path": str(p),
        "size_kb": None,
        "mtime": None,
        "reason": "A run is active." if _run_active else "No cached store found.",
    }
    if p.exists():
        try:
            st = p.stat()
            state.update({
                "exists": True,
                "can_clear": not _run_active,
                "size_kb": round(st.st_size / 1024, 1),
                "mtime": _format_epoch_for_display(st.st_mtime),
                "mtime_ts": st.st_mtime,
                "reason": "A run is active." if _run_active else "Cache file is available to clear.",
            })
        except Exception as e:
            state.update({
                "exists": True,
                "can_clear": False,
                "reason": f"Could not inspect cache file: {e}",
            })
    return state

def _imdb_download_state() -> dict:
    """Return local IMDb ratings file status and rolling 24-hour throttle state."""
    p = imdb_ratings_path()
    state = {
        "exists": False,
        "download_locked": False,
        "can_download": not _run_active,
        "path": str(p),
        "size_mb": None,
        "mtime": None,
        "mtime_ts": None,
        "age_days": None,
        "next_download": None,
        "next_download_ts": None,
        "seconds_until_download": 0,
        "reason": "No local IMDb ratings file found." if not _run_active else "A run is active.",
    }

    if p.exists():
        try:
            st = p.stat()
            now = time.time()
            age_seconds = max(0.0, now - st.st_mtime)
            age_days = age_seconds / 86400
            next_download_ts = st.st_mtime + 86400
            seconds_until_download = max(0, int(next_download_ts - now + 0.999))
            download_locked = seconds_until_download > 0
            state.update({
                "exists": True,
                "download_locked": download_locked,
                "can_download": (not _run_active) and (not download_locked),
                "size_mb": round(st.st_size / 1_000_000, 1),
                "mtime": _format_epoch_for_display(st.st_mtime),
                "mtime_ts": st.st_mtime,
                "age_days": round(age_days, 2),
                "next_download": _format_epoch_for_display(next_download_ts),
                "next_download_ts": next_download_ts,
                "seconds_until_download": seconds_until_download,
                "reason": "Download is available." if not download_locked else "Already downloaded within the last 24 hours.",
            })
        except OSError as e:
            state.update({"can_download": False, "reason": f"Could not inspect local file: {e}"})

    if _run_active:
        state["can_download"] = False
        state["reason"] = _RUN_ACTIVE_MESSAGE
    return state


_RUN_ACTIVE_MESSAGE = "A run is active. Try again when it finishes."


def _run_active_response(key: str = "message", **payload):
    """The 409 every file-touching endpoint returns while a run owns what it
    would rewrite. The refusal carries the caller's own state block, so it has
    the same shape as that endpoint's success response and the page can render
    it without a special case. `key` is "error" on the endpoints whose UI reads
    that field instead."""
    return jsonify({"ok": False, key: _RUN_ACTIVE_MESSAGE, **payload}), 409


def _filesystem_write_block_response(status: dict | None = None):
    filesystem = _filesystem_rw_state()
    if filesystem.get("ok", True):
        return None
    payload = {
        "ok": False,
        "message": "MediaReducer cannot read and write its config/log folders. Check the /config Docker mount, then recheck.",
        "filesystem": filesystem,
    }
    if status is not None:
        payload["status"] = status
    return jsonify(payload), 409


def _validate_imdb_url(url: str) -> str:
    url = str(url or "").strip()
    parsed = urlparse(url)
    if not url or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a valid HTTP(S) URL for the IMDB ratings dataset.")
    return url

def last_run_epoch() -> float | None:
    p = log_path()
    if p.exists():
        try:
            return p.stat().st_mtime
        except OSError:
            return None
    return None

def last_run_time() -> str | None:
    ts = last_run_epoch()
    return _format_epoch_for_display(ts) if ts is not None else None

_deleted_log_memo: tuple | None = None

def _deleted_log_lines() -> list[str]:
    """Read deleted.log, memoized by file identity. /api/status polls this every ~3s
    per open dashboard, and deleted.log only grows. Re-parse only when (mtime_ns, size)
    changes — engine appends and the Erase button both change it, so the cache can't go
    stale. Callers never mutate the returned list."""
    global _deleted_log_memo
    p = deleted_path()
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size)
    memo = _deleted_log_memo
    if memo and memo[0] == key:
        return memo[1]
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []
    _deleted_log_memo = (key, lines)
    return lines


def _deleted_line_size_bytes(match) -> int:
    try:
        return max(0, int(match.group("size_bytes") or 0))
    except (TypeError, ValueError):
        return 0

def deleted_count() -> int:
    return len(_deleted_log_lines())

_deleted_stats_memo: tuple | None = None


def deleted_stats() -> dict:
    """Totals over deleted.log, memoized on the same list object the line cache returns,
    so the regex re-sum only runs when the file changed (called on every /api/status
    poll)."""
    global _deleted_stats_memo
    lines = _deleted_log_lines()
    memo = _deleted_stats_memo
    if memo and memo[0] is lines:
        return memo[1]
    reclaimed_bytes = 0
    for raw_line in lines:
        m = _DELETED_LOG_RE.match(raw_line)
        if m:
            reclaimed_bytes += _deleted_line_size_bytes(m)
    stats = {
        "count": len(lines),
        "reclaimed_bytes": reclaimed_bytes,
        "reclaimed_label": _format_reclaimed_size(reclaimed_bytes),
    }
    _deleted_stats_memo = (lines, stats)
    return stats

def deleted_entries(limit: int | None = None) -> list[dict]:
    lines = _deleted_log_lines()
    if limit is not None:
        lines = lines[-max(0, int(limit)):]
    entries = []
    for raw_line in lines:
        m = _DELETED_LOG_RE.match(raw_line)
        if m:
            display_time = _format_log_timestamp_for_display(m.group("ts"))
            title = m.group("title").strip()
            path = m.group("path").strip()
            size_bytes = _deleted_line_size_bytes(m)
            size_label = _format_reclaimed_size(size_bytes) if size_bytes else ""
            # The WHY behind the deletion; score, plays, last watch; when the
            # line carries the optional fields.
            why_bits = []
            if m.group("score") is not None:
                why_bits.append(f"score {m.group('score')}")
            if m.group("plays") is not None:
                why_bits.append(f"{m.group('plays')} plays")
            if m.group("last_played") is not None:
                lp = m.group("last_played").strip()
                why_bits.append("never watched" if lp == "never" else f"last watched {lp.split(' ')[0]}")
            why = " · ".join(why_bits)
            line_parts = [display_time, title]
            if size_label:
                line_parts.append(size_label)
            if why:
                line_parts.append(why)
            line_parts.append(path)
            entry = {
                "time": display_time,
                "title": title,
                "path": path,
                "size_bytes": size_bytes,
                "size": size_label,
                "why": why,
                "line": " | ".join(line_parts),
            }
            # TV lines state their type; the history view's Type column then
            # says "TV Show · S2" instead of defaulting to Movie.
            if m.group("media"):
                entry["media"] = m.group("media")
                if m.group("season") is not None:
                    entry["season"] = int(m.group("season"))
            entries.append(entry)
        else:
            converted = _format_log_text_for_display(raw_line + "\n").strip()
            entries.append({"time": "", "title": "", "path": "", "size_bytes": 0, "size": "", "line": converted})
    return entries


def _pending_file_data() -> dict | None:
    """The marked & eligible queue document ({schema, entries, plan_config,
    monitor_dirs}), from the store's queue table + pending meta. Read through the
    memoized _cache_file_data(), so the frequent /api/status consults reuse the
    same once-per-version compose."""
    doc = _cache_file_data().get("pending")
    return doc if isinstance(doc, dict) else None


def _pending_raw() -> dict:
    """The engine's marked-for-deletion queue ({path: entry}), {} on any problem."""
    data = _pending_file_data()
    entries = data.get("entries") if data else None
    return entries if isinstance(entries, dict) else {}


def pending_count() -> int:
    return len(_pending_raw())


def _unschedule_pending_marks() -> int:
    """Null every running delay clock (marked_at) in the queue, keeping the
    queue itself — it is the standing eligible deletion order in every mode,
    and the engine's own satisfied-limits upkeep does exactly the same
    (_revalidate_pending_marks). Returns how many marks were unscheduled.
    Callers must ensure no run is active (the engine owns the queue during a
    run); the config-save path guards that by re-checking
    _run_active/_summary_active under _run_lock right at the write. One targeted
    UPDATE inside the WAL write transaction — every other table is untouched."""
    try:
        with _store_write() as conn:
            cur = conn.execute(
                "UPDATE queue SET marked_at=NULL, delay_days=NULL "
                "WHERE marked_at IS NOT NULL")
            return cur.rowcount
    except Exception:
        return 0


def _restart_pending_mark_clocks() -> int:
    """Re-stamp every running delay clock (marked_at) to NOW under the CURRENT
    delay setting, so each mark gets the full configured delay again from today.
    This is the one deliberate way to move existing marks onto a changed delay —
    a setting change alone leaves each mark on the delay it was made under.
    Marked seasons run the same clock as marked movies, so they move with them;
    a delay reset that skipped them would leave half the queue on the old dates.
    Returns how many clocks were restarted. Callers must ensure no run/reconcile
    is writing the queue or the season marks (the endpoint re-checks under
    _run_lock right at the write)."""
    now, restarted = time.time(), 0
    try:
        delay = _delete_delay_days()
        with _store_write() as conn:
            cur = conn.execute(
                "UPDATE queue SET marked_at=?, delay_days=? WHERE marked_at IS NOT NULL",
                (now, delay))
            restarted += cur.rowcount
    except Exception:
        pass
    try:
        st = _tv_cleanup_state()
        moved = 0
        for m in (st.get("marked") or {}).values():
            if isinstance(m, dict) and m.get("marked_at"):
                m["marked_at"], m["delay_days"] = now, delay
                moved += 1
        if moved:
            _save_tv_cleanup_state(st)
            restarted += moved
    except Exception:
        pass
    return restarted


@app.route("/api/queue/reset-delays", methods=["POST"])
def api_reset_mark_delays():
    """Restart the deletion-delay clock on every currently marked movie and
    season — the config page's button next to the Deletion delay setting. Each
    mark's delete date becomes today + DELETE_DELAY_DAYS; the 15-minute
    marked-change alert then reports the re-dated marks once, like a
    delay-setting change."""
    if _run_active:
        return _run_active_response()
    if _summary_active:
        return jsonify({"ok": False,
                        "message": "A background refresh is running — "
                                   "try again in a moment."}), 409
    # Close the check→write window: a run/reconcile starting between the checks
    # above and the UPDATE would own the queue. Re-check under the lock.
    with _run_lock:
        if _run_active or _summary_active:
            return jsonify({"ok": False,
                            "message": "A run just started. Try again when it "
                                       "finishes."}), 409
        n = _restart_pending_mark_clocks()
    if not n:
        return jsonify({"ok": True, "reset": 0,
                        "message": "Nothing is marked for deletion."})
    delay = int(load_config().get("DELETE_DELAY_DAYS") or 1)
    return jsonify({"ok": True, "reset": n,
                    "message": f"Delay reset — {n} mark(s) now delete "
                               f"{delay} day(s) from today."})


def code_reset_pending() -> bool:
    """The engine wiped the store because the code changed, and no scan has
    rebuilt it since.

    Two conditions, because the note has two ways to stop being true: the user
    dismisses it, or a Simulate rebuilds the snapshot and there is nothing left
    to warn about. Without the second, the note would still be telling someone to
    run a Simulate they had already run."""
    data = _cache_file_data()
    return bool(data.get("code_reset_pending")) and not isinstance(
        data.get("library_snapshot"), dict)


def _dismiss_autopause_note() -> tuple[bool, bool, str]:
    """(ok, dismissed_something, error). Rewrites that one config key and nothing
    else, deliberately: a dismissal must not carry whatever is half-typed in the
    form on screen into config.json the way a full save would. Only the note
    goes; the mode it describes already changed and stays where it is."""
    cfg = load_config()
    if not cfg.get("_RUN_MODE_AUTOPAUSE_REASON"):
        return True, False, ""
    cfg.pop("_RUN_MODE_AUTOPAUSE_REASON", None)
    if not save_config(cfg):
        return False, False, "Could not write to /config — the note stays until that is fixed."
    return True, True, ""


def _dismiss_store_reset_note() -> tuple[bool, bool, str]:
    """(ok, dismissed_something, error). Clears the engine's marker only; the
    empty store it describes is rebuilt by a Simulate, not by reading this."""
    try:
        with _store_write() as conn:
            if not db.get_meta(conn, "code_reset_pending", False):
                return True, False, ""
            db.set_meta(conn, "code_reset_pending", False)
    except Exception as e:
        return False, False, f"Could not update the database: {e}"
    return True, True, ""


# Notes the user can dismiss, by the name the page posts. Each reports something
# ALREADY DONE, which is what makes dismissing it safe: there is no condition
# left for hiding it to conceal.
_DISMISSIBLE_NOTES = {
    "autopause": _dismiss_autopause_note,
    "store-reset": _dismiss_store_reset_note,
}


@app.route("/api/notes/<name>/dismiss", methods=["POST"])
def api_dismiss_note(name):
    """Dismiss one of the notes above, clearing whatever made it true."""
    handler = _DISMISSIBLE_NOTES.get(str(name or "").strip())
    if handler is None:
        return jsonify({"ok": False, "message": f"Unknown note: {name}"}), 404
    ok, dismissed, err = handler()
    if not ok:
        return jsonify({"ok": False, "message": err}), 500
    return jsonify({"ok": True, "dismissed": dismissed})


def _normalized_monitor_dirs(cfg: dict) -> list[str]:
    out = []
    for raw in cfg.get("MONITOR_DIRS") or []:
        n = _normalize_library_path(str(raw))
        if n and n not in out:
            out.append(n)
    return sorted(out)


# Settings that change WHAT a run would delete and can't be reconciled from the
# cache. A Simulate stamps these into the plan; changing any of them ghosts both
# deleting actions until a fresh Simulate rebuilds it. Mirrored in engine.py —
# keep identical.
#
# Excluded on purpose: protected collections (the engine's upkeep re-fetches
# them and every deletion re-verifies protection, so a change is honoured from
# the standing cache) and Radarr cleanup (the queue carries what a delete
# needs). Monitored paths still force a Simulate, compared separately via the
# plan's stamped monitor_dirs.
_PLAN_CONFIG_KEYS = (
    "HEADROOM_GB", "REDLINE_GB", "REDLINE_ONLY_MODE", "MAX_LIBRARY_GB",
    "GRACE_PERIOD_DAYS", "SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES",
    "MAX_IMDB_RATING", "SCORE_BALANCE", "NEAR_TIE_PTS", "MAX_STALENESS_MONTHS",
    "MOVIE_EXTENSIONS", "MOVIE_CLEANUP_ENABLED", "_SCORING_CURVES",
)


def _plan_config_value(cfg: dict, key: str):
    """The value the plan stamp holds for a key. All but one come from
    config.json; _SCORING_CURVES is the scoring_constants fingerprint, because
    the queue's stored scores (which the Redline fast path deletes by) came
    from those curves."""
    if key == "_SCORING_CURVES":
        return SCORING_FINGERPRINT
    if key == "MOVIE_CLEANUP_ENABLED":
        # The engine stamps the EFFECTIVE bool (absent means OFF), so compare
        # like with like — comparing the raw file value read a hand-edited
        # config with the key deleted as stale forever, ghosting Automatic
        # Cleanup with a "changed keys" the engine itself did not see.
        return _coerce_bool(cfg.get(key), default=False)
    return cfg.get(key)


def _pending_plan_current(cfg: dict, *, use_saved_file: bool = True) -> bool:
    """True when the marked-for-deletion queue holds a plan computed under the CURRENT
    deletion-affecting config — thresholds, filters, scoring, and monitored paths, all
    stamped by a completed Simulate (a stopped/partial one never writes). Any mismatch
    means the plan can't be trusted: Automatic Cleanup locks until a fresh Simulate, which also pulls
    fresh play/last-played data in its full scan.

    use_saved_file=False compares against the PASSED cfg instead of the file on
    disk — the config-save arming gate must judge the plan against the candidate
    config being saved, not the old file it is about to replace (else one save
    could change thresholds AND arm automatic mode against a stamp only the old
    thresholds match)."""
    data = _pending_file_data()
    # An EMPTY stamped queue is a real plan: a completed Simulate that found
    # nothing eligible under these settings (all filtered/protected). Treating
    # it as stale would loop the user through Simulate forever.
    if not data or not isinstance(data.get("entries"), dict):
        return False
    if (not isinstance(data.get("monitor_dirs"), list)
            or sorted(str(d) for d in data["monitor_dirs"]) != _normalized_monitor_dirs(cfg)):
        return False
    stamp = data.get("plan_config")
    if not isinstance(stamp, dict) or set(stamp) != set(_PLAN_CONFIG_KEYS):
        return False

    # Compare RAW config.json values, like the engine's _plan_stamp_current: the
    # stamp holds the file's verbatim values, and load_config()'s normalization
    # (rounding SCORE_BALANCE, clamping cutoffs) would make a hand-edited
    # off-grid value mismatch its own stamp forever; every fresh Simulate would
    # instantly read as stale. Falls back to the normalized cfg only when the
    # file is unreadable (invalid config locks runs anyway).
    raw_saved = _read_saved_config_file() if use_saved_file else None
    src = raw_saved if isinstance(raw_saved, dict) else cfg
    current = {key: _plan_config_value(src, key) for key in _PLAN_CONFIG_KEYS}
    try:
        # Mirror the engine's one stamp normalization: a rating cutoff <= 0
        # reads as disabled (None) on both sides.
        _rating = current.get("MAX_IMDB_RATING")
        if _rating is not None and float(_rating) <= 0:
            current["MAX_IMDB_RATING"] = None
    except (TypeError, ValueError):
        pass

    def _norm(value):
        if value is None or isinstance(value, bool) or isinstance(value, str):
            return value
        if isinstance(value, list):
            return sorted(str(x) for x in value)
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return "invalid"

    return all(_norm(stamp.get(key)) == _norm(current.get(key))
               for key in _PLAN_CONFIG_KEYS)


def _redline_deficit_bytes(cfg: dict) -> int:
    """Bytes a Redline breach would need to free right now — 0 above the floor,
    outside redline-only mode, or when the disk can't be read."""
    if not _redline_only_mode_cfg(cfg):
        return 0
    try:
        free_gb = float((disk_stats() or {}).get("free_gb"))
        red_gb = float(cfg.get("REDLINE_GB"))
    except (TypeError, ValueError):
        return 0
    return int((red_gb - free_gb) * 1_000_000_000) if free_gb < red_gb else 0


def _marked_imminent_count(cfg: dict) -> int:
    """The 'marked' half of the Marked & Eligible display. Redline-only: the
    queue-front entries covering the current Redline deficit. Every other
    mode: everything with a delay clock running — marked movies and marked
    seasons in one number, the same merged list the history window shows
    (Redline is movie-only, so seasons play no part in its branch)."""
    raw = _pending_raw()
    if not _redline_only_mode_cfg(cfg):
        return (sum(1 for e in raw.values()
                    if isinstance(e, dict) and e.get("marked_at") is not None)
                + _tv_marked_count())
    if not raw:
        return 0
    deficit = _redline_deficit_bytes(cfg)
    if not deficit:
        return 0
    cum = n = 0
    for e in raw.values():
        if not isinstance(e, dict):
            continue
        if cum >= deficit:
            break
        cum += _entry_size_bytes(e)
        n += 1
    return n


def _entry_size_bytes(e: dict) -> int:
    """A queue entry's size_bytes as a safe non-negative int — the file is
    hand-editable, so a string, Infinity, or garbage value must read as 0,
    not crash every status poll."""
    try:
        return max(0, int(float(e.get("size_bytes") or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def pending_deletion_entries(cfg: dict | None = None, *, with_lines: bool = True) -> list[dict]:
    """The marked & eligible queue, in the file's own (deletion) order for every
    mode. MARKED entries are the ones actually scheduled: in normal modes the
    entries the engine clocked (marked_at set — each aging against the delay
    stamped when it was marked, so a later setting change never re-dates an
    existing mark); in
    redline-only mode the queue prefix covering the current Redline deficit.
    Everything else is merely ELIGIBLE — visible deletion order with no dates.
    Pass an already-loaded cfg when you have one — the default reloads
    config.json.

    with_lines=False skips the per-entry display strings (title, size label,
    "when" text, formatted timestamp, joined line) — the /api/status forecast
    reads only marked_at/days_remaining/delete_on/size_bytes and pays for none
    of that formatting on a queue that can be thousands of entries."""
    raw = _pending_raw()
    if not raw:
        return []
    cfg = cfg or load_config()
    rl_only = _redline_only_mode_cfg(cfg)
    delay = _delete_delay_days(cfg)
    now = time.time()
    today = datetime.now().date()
    _deficit_bytes = _redline_deficit_bytes(cfg)
    _marked_bytes = 0
    # The display format is one config value; resolve it once, not per entry.
    _fmt = _request_time_format() if with_lines else None
    out = []
    for i, (path, e) in enumerate(raw.items(), start=1):
        if not isinstance(e, dict):
            continue
        try:
            marked_at = float(e["marked_at"]) if e.get("marked_at") is not None else None
        except (TypeError, ValueError):
            marked_at = now
        if marked_at is not None:
            # A hand-edited out-of-range epoch would OverflowError the date
            # math below and 500 every status poll; read it as "just marked".
            # The probe includes the +delay headroom (a year-9999 epoch passes
            # fromtimestamp but overflows date + timedelta).
            try:
                datetime.fromtimestamp(marked_at).date() + timedelta(days=400)
            except (OverflowError, OSError, ValueError):
                marked_at = now
        size_bytes = _entry_size_bytes(e)
        marked = False
        remaining = delete_on = None
        if rl_only:
            if _marked_bytes < _deficit_bytes:
                _marked_bytes += size_bytes
                marked = True
        elif marked_at is not None:
            # Calendar-day aging on the shared clock (shared.delete_on_date,
            # the same one both deletion sides use): marked date + the
            # delay the mark was MADE under (stamped delay_days, or the
            # current setting when the stamp is missing or junk) is when it
            # becomes deletable at that day's daily run (or any manual
            # Cleanup from then on).
            marked = True
            delete_on = (shared.delete_on_date(marked_at, e.get("delay_days") or delay)
                         or shared.delete_on_date(marked_at, delay))
            remaining = max(0, (delete_on - today).days)
        if not with_lines:
            out.append({
                "marked_at": marked_at,
                "size_bytes": size_bytes,
                "marked": marked,
                "days_remaining": remaining,
                "delete_on": delete_on.isoformat() if delete_on else None,
            })
            continue
        title = str(e.get("title") or Path(path).name)
        size_label = _format_reclaimed_size(size_bytes) if size_bytes else ""
        marked_disp = "" if marked_at is None else _format_log_timestamp_for_display(
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(marked_at)), _fmt)
        out.append({
            "time": marked_disp,
            "marked_at": marked_at,
            "title": title,
            "path": path,
            "size_bytes": size_bytes,
            "size": size_label,
            "score": e.get("score"),
            "marked": marked,
            "days_remaining": remaining,
            "delete_on": delete_on.isoformat() if delete_on else None,
        })
    # Normal (delay) modes: float the marked entries to the top, SOONEST deletion
    # first — with the incremental re-verify a backfilled mark starts a fresh,
    # longer countdown, so it sinks below the aging original marks and the list
    # reads "what's about to go" downward. Eligible entries keep the deletion
    # order behind them (stable sort). Redline-only has no delay clock, so its
    # deficit-order display is left untouched. Only the displayed list is
    # reordered; the /api/status forecast (with_lines=False) never sorts.
    if with_lines and not rl_only:
        out.sort(key=lambda r: (0 if r.get("marked") else 1,
                                r["days_remaining"] if (r.get("marked")
                                and r.get("days_remaining") is not None) else 0))
    if with_lines:
        # Rank AFTER the sort. The row's other columns carry the rest — Status
        # says marked or eligible, Deletes says the date, "now", or "at Redline"
        # — and the window states the ordering once above the table, so this
        # number is all a row needs to say about where it sits. Numbering during
        # the walk numbered the FILE's order and the sort then floated the
        # marked entries over it, so the column read #4, #2, #1, #3, #5 down a
        # list whose whole point is that it is in order.
        for n, r in enumerate(out, start=1):
            r["when"] = f"#{n}"
            parts = [p for p in (r["time"], r["title"]) if p]
            if r["size"]:
                parts.append(r["size"])
            if r["score"] is not None:
                parts.append(f"score {r['score']}")
            parts.extend((r["when"], r["path"]))
            r["line"] = " | ".join(parts)
    return out


def _tv_marked_entries(cfg: dict | None = None) -> list[dict]:
    """The marked seasons, in the movie marked-entry shape, so the
    deletion-history window shows ONE marked list for the whole pool. Delay
    math mirrors the pass: calendar-day aging against the delay stamped on
    the mark."""
    marked = _tv_cleanup_state().get("marked") or {}
    if not marked:
        return []
    today = datetime.now().date()
    _fmt = _request_time_format()
    out = []
    for m in marked.values():
        if not isinstance(m, dict):
            continue
        try:
            marked_at = float(m["marked_at"]) if m.get("marked_at") is not None else None
        except (TypeError, ValueError):
            marked_at = None
        delete_on = remaining = None
        if marked_at is not None:
            # The shared clock again; an unusable epoch clears the mark's
            # display date rather than 500ing the window.
            delete_on = shared.delete_on_date(marked_at, m.get("delay_days") or 1)
            if delete_on is None:
                marked_at = None
            else:
                remaining = max(0, (delete_on - today).days)
        size_bytes = int(m.get("size_bytes") or 0)
        out.append({
            "time": "" if marked_at is None else _format_log_timestamp_for_display(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(marked_at)), _fmt),
            "marked_at": marked_at,
            "title": str(m.get("title") or ""),
            "media": "tv", "season": m.get("season"),
            "path": str(m.get("path") or ""),
            "size_bytes": size_bytes,
            "size": _format_reclaimed_size(size_bytes) if size_bytes else "",
            "score": m.get("score"),
            "marked": True,
            "days_remaining": remaining,
            "delete_on": delete_on.isoformat() if delete_on else None,
        })
    return out


def _tv_eligible_entries(cfg: dict, marked_keys) -> list[dict]:
    """Seasons in the current plan order that are NOT marked — the eligible
    TV tail of the pool's deletion order, in the movie entry shape. No dates:
    eligibility is visible order, not a schedule, exactly like the eligible
    movies behind the marked prefix."""
    if not bool(cfg.get("TV_CLEANUP_ENABLED", False)):
        return []
    try:
        data, _err = _read_library_snapshot()
        tv_rows = [r for r in ((data or {}).get("movies") or [])
                   if isinstance(r, dict) and r.get("media_type") == "tv"]
    except Exception:
        return []
    if not tv_rows:
        return []
    out = []
    for e in _tv_season_plan(tv_rows, cfg)["order"]:
        if _tv_mark_key(e) in marked_keys:
            continue
        size_bytes = int(e.get("size_bytes") or 0)
        out.append({
            "time": "", "marked_at": None,
            "title": str(e.get("title") or ""),
            "media": "tv", "season": e.get("season"),
            "path": str(e.get("path") or ""),
            "size_bytes": size_bytes,
            "size": _format_reclaimed_size(size_bytes) if size_bytes else "",
            "score": e.get("score"),
            "marked": False, "days_remaining": None, "delete_on": None,
        })
    return out


def _pool_marked_entries(cfg: dict | None = None) -> list[dict]:
    """Movies and TV seasons as ONE marked & eligible window: the movie queue
    plus the TV marks AND the eligible season order, renumbered and re-lined
    as one list — marked first (soonest deletion first), then the eligible
    tail interspliced across both types by the shared retention score, ties
    to the larger item, matching the merged pool order the daily passes
    split. Used where the display means the whole pool; the movie-only
    callers (notify items, the forecast) keep pending_deletion_entries."""
    cfg = cfg or load_config()
    out = pending_deletion_entries(cfg)
    marked_keys = set(_tv_cleanup_state().get("marked") or {})
    tv = _tv_marked_entries(cfg) + _tv_eligible_entries(cfg, marked_keys)
    if not tv:
        return out
    out = out + tv

    def _order(r):
        if r.get("marked"):
            return (0, r["days_remaining"] if r.get("days_remaining") is not None else 0,
                    0.0, 0)
        return (1, 0, float(r.get("score") or 0.0), -(r.get("size_bytes") or 0))
    out.sort(key=_order)
    for n, r in enumerate(out, start=1):
        r["when"] = f"#{n}"
        media = (f"TV Show · S{r.get('season')}" if r.get("media") == "tv" else "Movie")
        parts = [p for p in (r["time"], r["title"], media) if p]
        if r["size"]:
            parts.append(r["size"])
        if r.get("score") is not None:
            parts.append(f"score {r['score']}")
        parts.extend((r["when"], r["path"]))
        r["line"] = " | ".join(parts)
    return out


def _delete_delay_days(cfg: dict | None = None) -> int:
    try:
        # Defensive floor of 1: a marked movie is never deleted the same day. Valid
        # config is always >= 1 (the file guard rejects less); max() covers the rest.
        return max(1, int(float((cfg or load_config()).get("DELETE_DELAY_DAYS", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def _daily_run_time(cfg: dict | None = None) -> str:
    try:
        return _validate_daily_run_time((cfg or load_config()).get("DAILY_RUN_TIME"))
    except ValueError:
        return "00:00"


def _run_time_moved_into_past(old_time: str, new_time: str, now_hhmm: str) -> bool:
    """True when a save moves the daily run time to a slot already behind today's
    clock. The engine would otherwise fire an immediate catch-up run (the window
    is unused and now >= the new time), so today's window is burned and the new
    time takes effect tomorrow. An unchanged time, or one still ahead today (which
    simply fires later), returns False — HH:MM strings compare correctly within a
    day."""
    return new_time != old_time and now_hhmm > new_time


def _run_time_moved_ahead_today(old_time: str, new_time: str, now_hhmm: str) -> bool:
    """True when a save moves the daily run time to a slot still ahead of the clock
    today. If today's run already fired (its window is burned), it is reopened so
    the new, later time can run again today — safe, since a same-day re-run only
    re-marks. Unchanged times, and times already behind the clock, return False."""
    return new_time != old_time and now_hhmm < new_time


def pending_delete_forecast(cfg: dict | None = None) -> dict:
    """For the dashboard's breach note and red countdown: queue size, marks ripe now,
    and the next deletion EVENT — the exact batch the next deleting daily run removes
    (ripe marks, or failing any, the earliest upcoming eligibility date's batch) with
    its true movie count and bytes. Pass an already-loaded cfg when you have one."""
    cfg = cfg or load_config()
    entries = pending_deletion_entries(cfg, with_lines=False)
    # Redline-only mode schedules nothing: the queue is the deletion-order preview
    # and only a Redline breach deletes, so there is no ripe set, no next event,
    # and no delay to age against.
    if _redline_only_mode_cfg(cfg):
        return {"count": len(entries), "marked": 0, "ripe": 0, "event_on": None,
                "event_count": 0, "event_bytes": 0}
    # Only CLOCKED entries are scheduled; the eligible remainder of the queue
    # (days_remaining None) has no dates and never enters the forecast.
    clocked = [e for e in entries if e["days_remaining"] is not None]
    ripe = [e for e in clocked if e["days_remaining"] <= 0]
    if ripe:
        event_on, batch = None, ripe   # deletable at the next daily run
    else:
        event_on = min((e["delete_on"] for e in clocked), default=None)
        batch = [e for e in clocked if e["delete_on"] == event_on] if event_on else []
    return {
        "count": len(entries),
        "marked": len(clocked),
        "ripe": len(ripe),
        "event_on": event_on,
        "event_count": len(batch),
        "event_bytes": int(sum(e.get("size_bytes") or 0 for e in batch)),
    }


def _marked_clocked_count(cfg: dict, forecast: dict) -> int:
    """Everything running a delay clock — marked movies AND marked seasons. What
    the Config page gates the delay-reset button on, and what its save prompt
    counts, so both cover exactly what a reset re-dates. The forecast itself
    stays movie-only: its ripe/event fields describe the movie batch the next
    daily run removes, which seasons are not part of. Redline-only marks no
    seasons at all, so that branch's zero stands."""
    if _redline_only_mode_cfg(cfg):
        return int(forecast.get("marked") or 0)
    return int(forecast.get("marked") or 0) + _tv_marked_count()

# ── Routes — pages ────────────────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    """Browsers ask for /favicon.ico at the site root on their own — before any
    markup is parsed, and again for bookmarks and shortcuts — so the <link> tags
    in base.html do not cover it. Without this the request 404s on every fresh
    page load, once in the access log and once in the console."""
    return send_from_directory(app.static_folder, "favicon.ico",
                               mimetype="image/x-icon")


@app.route("/")
def dashboard():
    cfg  = load_config()
    monitoring = _has_monitored_dirs(cfg)
    disk = disk_stats()
    cleanup_state = _cleanup_button_state(cfg, disk)
    deleted = deleted_stats()
    stats = library_stats()
    return render_template("dashboard.html",
                           run_mode=cfg.get("RUN_MODE", "paused"),
                           autopause_reason=(str(cfg.get("_RUN_MODE_AUTOPAUSE_REASON") or "")
                                             if cfg.get("RUN_MODE") == "paused" else ""),
                           # Rendered at page load too, so the paused line is
                           # right before the first status poll lands.
                           resting_reason=(_scan_lock_reason(cfg)
                                           if (cfg.get("RUN_MODE") == "off"
                                               and not cfg.get(RUN_MODE_USER_PAUSED_KEY)) else ""),
                           thresholds_configured=_has_monitored_dirs(cfg),
                           code_reset_pending=code_reset_pending(),
                           headroom_gb=cfg.get("HEADROOM_GB"),
                           redline_gb=cfg.get("REDLINE_GB"),
                           redline_only=_redline_only_mode_cfg(cfg),
                           max_library_gb=cfg.get("MAX_LIBRARY_GB"),
                           headroom_window_used_today=_headroom_window_used_today(),
                           disk=(disk if monitoring else None),
                           library_gb=(stats.get("library_gb") if monitoring else None),
                           cleanup_state=cleanup_state,
                           run_active=_run_active,
                           # What KIND of run, not just that there is one.
                           # Loading this page mid-run painted the header badge
                           # from run_active alone, so it opened as a blue
                           # "Running" whatever was happening and corrected
                           # itself on the first status poll; a visible blink,
                           # and during a real Cleanup the red "files are going
                           # away" cue was missing for those first seconds.
                           run_cleanup=_run_active and _run_cleanup,
                           run_debug_cleanup=_run_active and _run_debug_cleanup,
                           last_run=last_run_time(),
                           last_run_ts=last_run_epoch(),
                           deleted_count=deleted["count"],
                           deleted_reclaimed_bytes=deleted["reclaimed_bytes"],
                           deleted_reclaimed_label=deleted["reclaimed_label"],
                           marked_count=pending_count() + _tv_eligible_count(cfg),
                           marked_imminent=_marked_imminent_count(cfg),
                           delete_forecast=pending_delete_forecast(cfg),
                           delete_delay_days=_delete_delay_days(cfg),
                           daily_run_time=_daily_run_time(cfg),
                           library_root=FILESYSTEM_CHECK_PATH)

@app.route("/config")
def config_page():
    with open(DEFAULT_CFG_PATH) as _f:
        defaults = json.load(_f)
    cfg = load_config()
    connection_onboarding_active = _connection_onboarding_needed(cfg)
    if connection_onboarding_active:
        cfg = _mark_connection_onboarding_seen(cfg)
    disk = disk_stats()
    url_defaults = _connection_url_defaults(cfg)
    url_placeholders = {k: (url_defaults.get(k) or _GENERIC_URL_PLACEHOLDERS[k])
                        for k in _GENERIC_URL_PLACEHOLDERS}
    # The Space Thresholds panel draws the same disk bar as the Dashboard's
    # Storage card, so it is seeded the same way: nothing monitored means no
    # figures rather than a reading of whatever /config happens to sit on.
    monitoring = _has_monitored_dirs(cfg)
    return render_template(
        "config.html",
        config=cfg,
        defaults=defaults,
        disk=disk,
        thresholds_configured=monitoring,
        library_gb=(library_stats().get("library_gb") if monitoring else None),
        space_thresholds=_space_threshold_state(cfg, disk),
        connection_health=_connection_health_for_ui(cfg),
        connection_onboarding_active=connection_onboarding_active,
        # Override the context processor's value: the nav badge that nudges the
        # user toward Configuration is noise while they're already on this page.
        connection_onboarding_needed=False,
        connection_url_defaults=url_defaults,
        connection_url_placeholders=url_placeholders,
        run_active=_run_active,
        summary_active=_summary_active,
        time_zone_options=_time_zone_options(),
        library_root=FILESYSTEM_CHECK_PATH,
        marked_clocked_count=_marked_clocked_count(cfg, pending_delete_forecast(cfg)),
        # Monitor Only's third requirement: an up-to-date library database.
        monitor_scan_ok=_library_db_fresh(cfg),
        monitor_locked_reason=_scan_lock_reason(cfg),
    )

@app.route("/explorer")
def explorer():
    # Only the keys the page uses; never the full config (it holds tokens/keys that
    # must not appear in page source).
    cfg = load_config()
    page_cfg = _score_page_config(cfg)
    health = _connection_health_for_ui(cfg)
    jellyfin_connected = bool(cfg.get("USE_JELLYFIN")) and bool(health.get("jellyfin_connected"))
    return render_template("deletion_score_explorer.html", config=page_cfg,
                           jellyfin_connected=jellyfin_connected, scoring=SCORING,
                           run_active=_run_active)


def _score_page_config(cfg: dict) -> dict:
    """Only the keys the Score Explorer uses — never the full config (it holds
    tokens/keys that must not reach the page or the score-config response)."""
    return {
        "SCORE_BALANCE": cfg.get("SCORE_BALANCE", 50),
        "MAX_IMDB_RATING": cfg.get("MAX_IMDB_RATING"),
        "SKIP_UNPLAYED_MOVIES": bool(cfg.get("SKIP_UNPLAYED_MOVIES")),
        "MOVIE_CLEANUP_ENABLED": bool(cfg.get("MOVIE_CLEANUP_ENABLED", False)),
        "TV_CLEANUP_ENABLED": bool(cfg.get("TV_CLEANUP_ENABLED", False)),
        "TV_SERIES_WATCH_BUMP": cfg.get("TV_SERIES_WATCH_BUMP", 10),
        "TV_WATCH_WEIGHT": cfg.get("TV_WATCH_WEIGHT", 100),
        "TV_SEASON_ELIGIBILITY": cfg.get("TV_SEASON_ELIGIBILITY", "oldest"),
        "TV_MAX_SEASON_EPISODES": cfg.get("TV_MAX_SEASON_EPISODES",
                                         TV_MAX_SEASON_EPISODES_DEFAULT),
        "GRACE_PERIOD_DAYS": cfg.get("GRACE_PERIOD_DAYS", 0),
        "PROTECT_JELLYFIN_FAVORITES": bool(cfg.get("PROTECT_JELLYFIN_FAVORITES")),
        "NEAR_TIE_PTS": cfg.get("NEAR_TIE_PTS", 2),
        "MAX_STALENESS_MONTHS": cfg.get("MAX_STALENESS_MONTHS", 36),
        "USE_JELLYFIN": bool(cfg.get("USE_JELLYFIN")),
        # Last entered values of the optional fields, kept while disabled so the
        # grayed-out inputs still show them (surviving restarts).
        "_MAX_IMDB_RATING_LAST": cfg.get("_MAX_IMDB_RATING_LAST"),
        "_NEAR_TIE_PTS_LAST": cfg.get("_NEAR_TIE_PTS_LAST"),
    }

# ── Routes — API ──────────────────────────────────────────────────────────────

@app.route("/api/library-snapshot")
def api_library_snapshot():
    """The full-library snapshot from the last completed scan (the store's
    "library_snapshot"): every monitored-path movie with merged Plex+Jellyfin
    data. Built by Simulate and full-scan Cleanups; the Filtering & Scoring
    page paginates and scores it client-side."""
    data, pool_err = _read_library_snapshot()
    if pool_err == "missing":
        resp = jsonify({"ok": False, "reason": "no_pool",
                        "message": "No library snapshot yet — run a Simulate to build it."})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    movies = (data or {}).get("movies") or []
    if pool_err or not isinstance(movies, list):
        resp = jsonify({"ok": False, "reason": "bad_pool",
                        "message": "The library snapshot is unreadable — run a Simulate to rebuild it."})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    if not movies:
        resp = jsonify({"ok": False, "reason": "empty_pool",
                        "message": "The last scan found no movies under the monitored library paths."})
        resp.headers["Cache-Control"] = "no-store"
        return resp
    # Whether an IMDb dataset is on disk; when the snapshot has no ratings,
    # the explorer uses this to explain whether the next run will annotate.
    tsv = imdb_ratings_path()
    imdb_on_disk = tsv.exists() or tsv.with_name(tsv.name + ".gz").exists()
    # The snapshot carries each movie's /library path as an internal join key for
    # the engine's incremental re-verify. Paths never leave the server (the debug
    # report redacts them too), so strip it from the browser payload.
    movies = [{k: v for k, v in m.items() if k != "path"} if isinstance(m, dict) else m
              for m in movies]
    resp = jsonify({"ok": True, "built_at": data.get("built_at"), "movies": movies,
                    "imdb_dataset_on_disk": imdb_on_disk})
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/status")
def api_status():
    cfg = load_config()
    # With nothing monitored the scheduler is truly idle (see _scheduled_tick), so
    # there are no meaningful filesystem/library numbers or a next tick to show —
    # the dashboard dashes the storage card and reads "Scheduler paused".
    monitoring = _has_monitored_dirs(cfg)
    stats = library_stats()
    disk = cached_disk_stats(stats)
    cleanup_state = _cleanup_button_state(cfg, disk)
    # The next tick is the EARLIER of the 15-minute interval job and the
    # precise daily-slot one-shot; when the daily run time lands inside the
    # current interval, the daily scan/cleanup fires first and the countdown
    # must count to IT (the flag lets the dashboard label it as the daily
    # scan rather than a routine refresh).
    job = scheduler.get_job("engine")
    _slot_job = scheduler.get_job("daily-slot")   # fetched ONCE — a fire between two reads skews the flag
    _fires = [j.next_run_time for j in (job, _slot_job) if j and j.next_run_time]
    _next_fire = min(_fires) if _fires else None
    next_tick_is_daily = bool(_slot_job and _slot_job.next_run_time
                              and _next_fire == _slot_job.next_run_time)
    next_run = None
    mode = cfg.get("RUN_MODE")
    # _summary_active matters here too: a background Summary pauses the interval
    # job (its next_run_time goes None), leaving only the daily-slot fire —
    # possibly hours away; as _next_fire. Publishing that would swing the
    # countdown to "Daily scan in ~13 h" for the Summary's whole duration.
    if (
        _next_fire and _is_cleanup_mode(mode) and not _run_active and not _summary_active
        and cleanup_state.get("connection_health", {}).get("critical_ok", True)
        and cleanup_state["space_thresholds"].get("ok_for_cleanup", False)
    ):
        next_run = _next_fire.isoformat()
    # The raw next scheduler tick, ANY active mode (unlike next_run_time, which is
    # gated to a run that will actually delete). Monitor Only surfaces this as its
    # ~15-minute refresh / daily-scan countdown; None while a run or Summary holds
    # the clock, nothing is monitored, or the scheduler is fully paused ("off") —
    # in all of those the tick no-ops (see _scheduled_tick), so counting down to
    # it lies.
    next_tick = (_next_fire.isoformat()
                 if _next_fire and not _run_active and not _summary_active
                 and monitoring and mode != "off"
                 else None)
    deleted = deleted_stats()
    _forecast = pending_delete_forecast(cfg)
    _delay_days = _delete_delay_days(cfg)
    resp = jsonify({
        "run_active":              _run_active,
        "run_cleanup":                _run_active and _run_cleanup,
        # A Debug Cleanup drives the cleanup path but deletes nothing; the dashboard
        # header badge and run pill read "Debugging" in yellow for it.
        "run_debug_cleanup":          _run_active and _run_debug_cleanup,
        # Started from a button rather than the scheduler. Informational only.
        "run_manual":                 _run_active and _run_manual,
        "summary_active":          _summary_active,
        "last_run":                last_run_time(),
        "last_run_ts":             last_run_epoch(),
        "deleted_count":           deleted["count"],
        "deleted_reclaimed_bytes": deleted["reclaimed_bytes"],
        "deleted_reclaimed_label": deleted["reclaimed_label"],
        # Movies and seasons in one eligible order, one count — the same
        # merged list the deletion-history window shows.
        "marked_count":            _forecast["count"] + _tv_eligible_count(cfg),
        # Redline-only: how many queue-front entries cover the current deficit
        # — the "marked" half of the dashboard's Marked & Queued button.
        "marked_imminent_count":   _marked_imminent_count(cfg),
        "marked_ripe_count":       _forecast["ripe"],
        # How many marks carry a running delay clock; the Config page gates the
        # delay-reset button on this (nothing marked = nothing to reset).
        "marked_clocked_count":    _marked_clocked_count(cfg, _forecast),
        "marked_event_on":         _forecast["event_on"],
        "marked_event_count":      _forecast["event_count"],
        "marked_event_bytes":      _forecast["event_bytes"],
        "delete_delay_days":       _delay_days,
        # Redline-only mode (Headroom disabled): the marked queue is a standing
        # deletion-order preview, not a schedule; drives UI wording.
        "redline_only":            _redline_only_mode_cfg(cfg),
        # Time of day (24h HH:MM, operating zone) the daily cleanup may fire; drives the
        # dashboard breach-note wording and red-countdown gating.
        "daily_run_time":          _daily_run_time(cfg),
        "next_run_time":           next_run,
        "next_tick_time":          next_tick,
        # True when the countdown above lands on the DAILY slot (scan/cleanup)
        # rather than a routine 15-minute refresh; drives the countdown label.
        "next_tick_is_daily":      next_tick_is_daily,
        # The live Scheduler Mode ("off"/"paused"/"headroom"), so an open
        # dashboard tracks mode changes made elsewhere (an autopause, a second
        # tab, the CLI) instead of trusting its page-load render forever.
        "run_mode":                _ui_run_mode(mode),
        # Why Automatic Cleanup is paused, when the app (not the user) paused it; startup
        # safety, forced pause on save, etc.
        "run_mode_autopause_reason": (str(cfg.get("_RUN_MODE_AUTOPAUSE_REASON") or "")
                                      if cfg.get("RUN_MODE") == "paused" else ""),
        # The engine wiped the store on a code change and no scan has rebuilt it
        # — the dashboard says the plan is gone and why. Clears itself once a
        # Simulate lands, so it can never ask for one that already happened.
        "code_reset_pending":      code_reset_pending(),
        # A fully-paused scheduler has two very different causes: the user chose
        # it, or the system is RESTING until a scan lands. Both render as
        # "Paused", so the dashboard needs the reason to avoid describing a
        # deliberate pause to someone whose Simulate just failed.
        "run_mode_resting_reason": (_scan_lock_reason(cfg)
                                    if (cfg.get("RUN_MODE") == "off"
                                        and not cfg.get(RUN_MODE_USER_PAUSED_KEY)) else ""),
        # Dashed out when nothing is monitored; no paths means no filesystem/media
        # size worth showing (the storage card and bar go to "—").
        "disk":                    (disk if monitoring else None),
        "library_gb":              (stats.get("library_gb") if monitoring else None),
        # Storage-bar threshold markers: Headroom/Redline are free-space targets;
        # the library cap is a library-size target, drawn against the library.
        "headroom_gb":             _threshold_gb_or_none(cfg.get("HEADROOM_GB")),
        "redline_gb":              _threshold_gb_or_none(cfg.get("REDLINE_GB")),
        "library_cap_gb":          _threshold_gb_or_none(cfg.get("MAX_LIBRARY_GB")),
        # Monitored dirs exist; the dashboard's Cleanup Targets card renders
        # real values ("Off"/"Disabled" included) instead of "Not set".
        "thresholds_configured":   _has_monitored_dirs(cfg),
        # Once-per-day headroom window: a headroom-only breach won't prune again today
        # once it's used (redline/cap ignore it); the dashboard red countdown keys off
        # this.
        "headroom_window_used_today": _headroom_window_used_today(),
        "cleanup_state":              cleanup_state,
        # Drives the red Configuration tab live (no reload): onboarding cue or an API
        # error in the saved config's cached health.
        "config_attention":        _connection_onboarding_needed(cfg) or _api_connection_error(cfg),
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/welcome/seen", methods=["POST"])
def api_welcome_seen():
    """Persist that the first-run welcome/quick-start popup was dismissed."""
    try:
        return jsonify({"ok": _mark_welcome_guide_seen()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route("/api/config/check", methods=["POST", "GET"])
def api_config_check():
    """Run a fast configuration health check for the Config page."""
    cfg = load_config()
    if request.method == "POST":
        posted = request.get_json(silent=True)
        if isinstance(posted, dict):
            cfg.update(posted)
            _normalize_library_paths(cfg)
            cfg["CHECK_PATH"] = FILESYSTEM_CHECK_PATH
            cfg["TAUTULLI_APPDATA"] = TAUTULLI_APPDATA_DIR
            cfg["RADARR_APPDATA"] = RADARR_APPDATA_DIR
            cfg["SONARR_APPDATA"] = SONARR_APPDATA_DIR
            # The form posts 'auto' for an enabled Radarr cleanup; a save normalizes
            # that to the cached detection. Mirror it here so a clean form's check
            # carries the SAVED config's signature; otherwise every manual check looks
            # like an unsaved-values probe and stops driving the red Configuration tab.
            if cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None:
                cached_detected_section = str(cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip()
                cfg["RADARR_OVERSEERR_SECTION_ID"] = cached_detected_section or "auto"
            # Match save semantics: scheme-normalize the posted URLs. Blank
            # URLs resolve to their defaults inside the health check itself.
            _normalize_saved_service_urls(cfg)
    # Check for Errors is one of the media-path check's two triggers (the
    # other is a save that changes the monitored paths): it always re-judges
    # against the current disk, active error or not.
    health = _connection_health_state(cfg, probe=True, media_paths=True)
    # Cache both manual and automatic checks so highlighted override fields stay stable
    # if the user leaves Config and returns, instead of falling back to the non-probed
    # startup state. Keyed by the connection-related signature, so stale form values
    # aren't reused after the saved config changes.
    with _connection_health_cache_lock:
        _connection_health_cache.update({
            "signature": _connection_health_signature(cfg),
            "health": health,
            "checked_at": time.time(),
        })
    return jsonify(health)


@app.route("/api/connections/verify")
def api_verify_connections():
    """What each appdata mount can actually contribute to setup.

    The mounts exist for ONE reason: so Auto Detect can read API keys out of a
    service's own config file. Nothing else in MediaReducer reads them — the
    engine talks to every service over HTTP — so a missing mount is never a
    failure, only "you'll type the key in yourself".

    Reported per service as {mounted, keys}, derived from the SAME detection Auto
    Detect runs, so a card can never claim a key the button wouldn't actually
    fill. It used to report only whether a marker FILE existed, which said
    "verified" for a mount whose config held no key at all.

    Deliberately NOT gated on the Plex/Jellyfin checkboxes: reading a file off
    disk doesn't depend on which server is selected, and a setup that hasn't
    picked one yet is exactly when knowing the key is available is useful.
    """
    mounts = _appdata_mount_state()
    detected = _autodetected_connection_values()
    tautulli_keys = []
    # One file, two services: Tautulli's config.ini carries its own API key AND
    # the Plex token/URL it was configured with.
    if detected.get("tautulli_key"):
        tautulli_keys.append("Tautulli")
    if detected.get("plex_token"):
        tautulli_keys.append("Plex")
    return jsonify({
        "tautulli": {"mounted": bool(mounts["tautulli"].get("ok")),
                     "keys": tautulli_keys},
        "radarr":   {"mounted": bool(mounts["radarr"].get("ok")),
                     "keys": ["Radarr"] if detected.get("radarr_key") else []},
        "sonarr":   {"mounted": bool(mounts["sonarr"].get("ok")),
                     "keys": ["Sonarr"] if detected.get("sonarr_key") else []},
    })


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Deliver a test alert to the currently-entered notification destinations.

    Uses the notification fields from the POST body overlaid on the saved config,
    so a user can verify wiring BEFORE saving. Bounded and best-effort (notify.py
    never raises and times the send out), so this can't hang the request."""
    payload = request.get_json(silent=True) or {}
    cfg = load_config()
    for k in notify.CONFIG_KEYS:
        if k in payload:
            cfg[k] = payload[k]
    if not notify.has_destinations(cfg):
        return jsonify({"ok": False,
                        "error": "No notification services are configured. Add a service "
                                 "(or a custom Apprise URL) first, then test."}), 400
    # A test is a real message to a real service, so it is rate-limited like any
    # alert. Answer that as 429 with the wait, not as a delivery failure; the
    # wiring may be perfectly fine.
    waiting = notify.cooldown_remaining(cfg)
    if waiting > 0:
        return jsonify({"ok": False, "rate_limited": True,
                        "error": notify.cooldown_message(waiting)}), 429
    ok, detail = notify.send_test(cfg)
    if ok:
        return jsonify({"ok": True, "message": detail})
    return jsonify({"ok": False, "error": detail}), 502


@app.route("/api/connections/autodetect", methods=["POST", "GET"])
def api_connections_autodetect():
    """One-shot appdata auto-detect used only by the Config Auto Detect button."""
    values = _autodetected_connection_field_values()
    found = {k: bool(str(v or "").strip()) for k, v in values.items()}
    return jsonify({
        "ok": any(found.values()),
        "values": values,
        "found": found,
        "appdata": _appdata_mount_state(),
    })


@app.route("/api/library/browse")
def api_library_browse():
    """List folders under the library root for the Media Library Paths browser."""
    root = FILESYSTEM_CHECK_PATH
    root_name = root.rsplit("/", 1)[-1] or "library"
    # Two namespaces, deliberately kept apart. `root` is the one the config, this
    # endpoint's own query string, and every path on screen are written in; `base`
    # is where that lands once symlinks are followed, and it is used ONLY to prove
    # containment. Answering in the resolved namespace handed the browser paths
    # this endpoint could not accept back: one symlinked library folder 404'd the
    # moment it was clicked, and a /library that is itself a symlink 404'd on
    # every folder in it.
    root_path = Path(root)
    base = root_path.resolve(strict=False)
    raw = (request.args.get("path") or root).strip().replace("\\", "/")

    if raw in ("", root, root_name):
        target = root_path
    elif raw.startswith(root + "/"):
        target = Path(raw)
    else:
        target = root_path / raw.lstrip("/")
    # Collapse any ".." for display without following symlinks — going up from a
    # symlinked folder returns to where the browser came from, not to wherever
    # the link points. Containment is still judged on the resolved form below.
    target = Path(os.path.normpath(str(target)))

    try:
        target.resolve(strict=False).relative_to(base)
    except ValueError:
        return jsonify({"ok": False, "error": f"Path must be inside {root}."}), 400

    if not target.exists() or not target.is_dir():
        return jsonify({"ok": False, "error": f"Folder not found: {target}"}), 404

    dirs = []
    try:
        for child in target.iterdir():
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    dirs.sort(key=lambda item: item["name"].lower())
    parent = target.parent if target != root_path else root_path
    try:
        parent.resolve(strict=False).relative_to(base)
    except ValueError:
        parent = root_path

    return jsonify({
        "ok": True,
        "path": str(target),
        "parent": str(parent),
        "dirs": dirs,
    })

@app.route("/api/library/validate")
def api_library_validate():
    """Validate that a monitored library folder exists under the library root."""
    root = FILESYSTEM_CHECK_PATH
    base = Path(root).resolve(strict=False)
    raw = (request.args.get("path") or "").strip().replace("\\", "/")
    normalized = _normalize_library_path(raw)
    if not normalized:
        return jsonify({"ok": False, "error": f"Enter a folder under {root}."}), 400

    target = Path(normalized).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError:
        return jsonify({"ok": False, "error": f"Path must be inside {root}."}), 400

    if not target.exists() or not target.is_dir():
        return jsonify({"ok": False, "error": f"Folder not found: {target}"}), 404

    return jsonify({"ok": True, "path": str(target)})


@app.route("/api/score-config", methods=["POST"])
def api_save_score_config():
    """Save only the scoring/filter fields edited from the Score Explorer."""
    if _run_active:
        # Same rule as the Configuration save: the engine loaded its scoring at run
        # start, so a mid-run save would silently apply to the NEXT run while the UI
        # implies it changed this one.
        return _run_active_response("error")
    blocked = _invalid_config_response()
    if blocked:
        return blocked
    try:
        payload = request.get_json(force=True) or {}

        # Error strings surface as explorer toasts; use the page's labels, not raw
        # config keys ("SCORE_BALANCE must be a number.").
        _FIELD_LABELS = {
            "SCORE_BALANCE": "The scoring balance",
            "GRACE_PERIOD_DAYS": "Minimum age (grace period)",
            "MAX_IMDB_RATING": "Maximum IMDb rating",
            "NEAR_TIE_PTS": "The file-size-optimization window",
            "MAX_STALENESS_MONTHS": "Max staleness",
            "TV_SERIES_WATCH_BUMP": "The all-season watch boost",
            "TV_WATCH_WEIGHT": "The TV watch weight",
        }

        def _float_field(name, minimum=None, maximum=None):
            label = _FIELD_LABELS.get(name, name)
            try:
                val = float(payload.get(name))
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a number.")
            if minimum is not None and val < minimum:
                raise ValueError(f"{label} must be at least {minimum}.")
            if maximum is not None and val > maximum:
                raise ValueError(f"{label} must be at most {maximum}.")
            return val

        updates = {
            "SCORE_BALANCE": max(0, min(100, round(_float_field("SCORE_BALANCE", 0, 100)))),
        }

        # The Filtering & Scoring page edits every scoring/filter field, so
        # any of them may arrive here.
        if "GRACE_PERIOD_DAYS" in payload:
            try:
                grace = int(float(payload.get("GRACE_PERIOD_DAYS")))
            except (TypeError, ValueError):
                raise ValueError("Minimum age (grace period) must be a whole number of days.")
            if grace < 0:
                raise ValueError("Minimum age (grace period) must be zero or greater.")
            updates["GRACE_PERIOD_DAYS"] = grace

        if "MAX_IMDB_RATING" in payload:
            raw_cutoff = payload.get("MAX_IMDB_RATING")
            if not _is_blank(raw_cutoff):
                try:
                    cutoff_val = float(raw_cutoff)
                except (TypeError, ValueError):
                    raise ValueError("Maximum IMDb rating must be a number above 0, up to 10, or disabled.")
                if cutoff_val <= 0:
                    # 0 would match nothing; unchecking is the off switch.
                    raise ValueError("Maximum IMDb rating must be above 0 — uncheck it instead.")
            updates["MAX_IMDB_RATING"] = _clamp_max_imdb_rating(raw_cutoff)

        if "NEAR_TIE_PTS" in payload:
            raw_tie = payload.get("NEAR_TIE_PTS")
            if not _is_blank(raw_tie):
                try:
                    tie_val = float(raw_tie)
                except (TypeError, ValueError):
                    raise ValueError("The file-size-optimization window must be a number of points, or disabled.")
                if tie_val < 0.5:
                    raise ValueError("The file-size-optimization window must be at least 0.5 points — uncheck it instead.")
            updates["NEAR_TIE_PTS"] = _clamp_near_tie_pts(raw_tie)

        if "TV_SERIES_WATCH_BUMP" in payload:
            bump_val = _float_field("TV_SERIES_WATCH_BUMP", 0, 25)
            updates["TV_SERIES_WATCH_BUMP"] = round(bump_val * 10) / 10

        if "TV_WATCH_WEIGHT" in payload:
            weight_val = _float_field("TV_WATCH_WEIGHT", 100, 200)
            updates["TV_WATCH_WEIGHT"] = int(round(weight_val))

        if "TV_SEASON_ELIGIBILITY" in payload:
            elig = str(payload.get("TV_SEASON_ELIGIBILITY") or "").strip().lower()
            if elig not in ("oldest", "except_newest", "all"):
                raise ValueError("Season eligibility must be one of the listed options.")
            updates["TV_SEASON_ELIGIBILITY"] = elig

        if "TV_MAX_SEASON_EPISODES" in payload:
            try:
                cap_val = int(float(payload.get("TV_MAX_SEASON_EPISODES")))
            except (TypeError, ValueError):
                raise ValueError("The season episode cap must be a whole number of episodes.")
            if not 0 <= cap_val <= 999:
                raise ValueError("The season episode cap must be 0\u2013999 episodes (0 turns it off).")
            updates["TV_MAX_SEASON_EPISODES"] = cap_val

        if "MAX_STALENESS_MONTHS" in payload:
            try:
                stale_val = float(payload.get("MAX_STALENESS_MONTHS"))
            except (TypeError, ValueError):
                raise ValueError("Max staleness must be a number of months (1–120).")
            if not 1 <= stale_val <= 120:
                raise ValueError("Max staleness must be 1–120 months.")
            updates["MAX_STALENESS_MONTHS"] = _clamp_staleness_months(stale_val)

        cfg = load_config()
        _old_cfg = dict(cfg)   # pre-save values, for the reconcile's changed-key check
        if "SKIP_UNPLAYED_MOVIES" in payload:
            updates["SKIP_UNPLAYED_MOVIES"] = _coerce_bool(payload.get("SKIP_UNPLAYED_MOVIES"))
        if "PROTECT_JELLYFIN_FAVORITES" in payload:
            updates["PROTECT_JELLYFIN_FAVORITES"] = _coerce_bool(payload.get("PROTECT_JELLYFIN_FAVORITES"))
        # The per-type cleanup switches live on the Filtering & Scoring page:
        # movies off empties the deletion plan (a pure reconcile below), TV off
        # stops the season plan preview from proposing seasons.
        if "MOVIE_CLEANUP_ENABLED" in payload:
            updates["MOVIE_CLEANUP_ENABLED"] = _coerce_bool(payload.get("MOVIE_CLEANUP_ENABLED"), default=False)
        if "TV_CLEANUP_ENABLED" in payload:
            updates["TV_CLEANUP_ENABLED"] = _coerce_bool(payload.get("TV_CLEANUP_ENABLED"), default=False)
        # Disabled optional fields keep their last entered value so the grayed-out field
        # still shows it (surviving restarts). Prefer the text still in the disabled
        # input (posted as _<key>_LAST), then the value this save is disabling, then the
        # memory on disk. Saving the field enabled clears its memory.
        for _opt_key, _opt_clamp in (("MAX_IMDB_RATING", _clamp_max_imdb_rating),
                                     ("NEAR_TIE_PTS", _clamp_near_tie_pts)):
            if _opt_key not in updates:
                continue
            _last_key = f"_{_opt_key}_LAST"
            if updates[_opt_key] is None:
                _last = next((v for v in (_opt_clamp(payload.get(_last_key)),
                                          _opt_clamp(cfg.get(_opt_key)),
                                          _opt_clamp(cfg.get(_last_key)))
                              if v is not None), None)
                if _last is not None:
                    updates[_last_key] = _last
                else:
                    cfg.pop(_last_key, None)
            else:
                cfg.pop(_last_key, None)
        cfg.update(updates)
        # Turning BOTH types off from this page leaves nothing for an armed
        # Automatic Cleanup to delete, and the Configuration page's own gate
        # cannot help — it is a different page, and this save never passes
        # through it. So it drops to Monitor Only here, the same way a
        # threshold or connection change does, with a reason the dashboard can
        # explain. Refusing the save instead would make turning cleanup off
        # require switching modes first, which is backwards for the safer
        # direction of travel.
        if (_is_cleanup_mode(cfg.get("RUN_MODE"))
                and not (cfg.get("MOVIE_CLEANUP_ENABLED") or cfg.get("TV_CLEANUP_ENABLED"))):
            cfg["RUN_MODE"] = "paused"
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = ("neither movies nor TV shows are allowed to be "
                                                 "cleaned up — allow a type in Filtering & Scoring, "
                                                 "then re-enable Automatic Cleanup.")
        if not save_config(cfg):
            return _invalid_config_response() or (jsonify({
                "ok": False, "error": "Save was refused — config.json changed on disk. Reload the page.",
            }), 409)
        # Rebuild the marked/eligible queue in place from the snapshot under the new
        # scoring/filter settings; no Simulate needed (favorites re-fetch from
        # Jellyfin; everything else is a pure recompute).
        reconcile = _reconcile_after_save(_old_cfg, cfg, source="filtering & scoring")
        return jsonify({"ok": True, "config": _score_page_config(cfg), "reconcile": reconcile})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(load_config())


@app.route("/api/config/reset", methods=["POST"])
def api_reset_config():
    """Wipe configuration and operational state back to first-time setup.

    Removes the state files the app creates: config.json, the engine's cache (which
    includes the library snapshot), progress state, and saved debug reports.
    LOGS ARE NEVER LOST — deleted.log and the archived logs/ folder survive, and
    lastrun.log is moved into logs/ so the Dashboard run panel starts empty while the
    run stays archived. The IMDb ratings dataset is kept so a reset never forces a
    re-download. Uses an explicit allowlist, never a blanket directory wipe. An active
    run is stopped first."""
    if _run_active:
        stop_script()
        # Give the worker a moment to tear down the subprocess and clear the
        # active flag before we delete its config out from under it.
        for _ in range(50):  # up to ~5s
            if not _run_active:
                break
            time.sleep(0.1)
    # A storage summary in flight would recreate the store with pre-reset stats
    # moments after this wipe (its engine subprocess merges into the cache on
    # exit), resurrecting stale numbers on the "first-time" dashboard, and the
    # summary worker's clock restart would resume the schedule the reset just
    # paused. Wait briefly; if one is stuck, tell the user instead of half-resetting.
    for _ in range(100):  # up to ~10s
        if not _summary_active:
            break
        time.sleep(0.1)
    if _summary_active:
        return jsonify({
            "ok": False,
            "error": "A background storage refresh is still running — "
                     "try the reset again in a moment.",
        }), 409

    out_dir = output_dir()
    files = [
        CONFIG_PATH,
        out_dir / "progress.json",
        out_dir / "progress.json.tmp",
    ]
    try:
        files.extend(out_dir.glob("progress.json.*.tmp"))  # pid-unique writer tmps
        files.extend(out_dir.glob("debug_report_*.txt"))
    except OSError:
        pass
    # Close the check→unlink window under _run_lock (see api_clear_cache): a run /
    # summary / reconcile starting between the checks above and the wipe would write
    # into the just-unlinked store. Hold the lock across a final re-check + the wipe.
    errors = []
    with _run_lock:
        if _run_active or _summary_active:
            return jsonify({
                "ok": False,
                "error": "A run started — try the reset again in a moment.",
            }), 409
        for p in files:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                errors.append(f"{p.name}: {e}")
        try:
            db.reset_store(out_dir / "mediareducer.db")  # store + sidecars, under the init lock
        except OSError as e:
            errors.append(str(e))
        _drop_store_caches()

    # lastrun.log drives the Last Run timestamp, the detailed-log window, and the
    # run-stat jump targets; after a reset those must read as "no runs yet". It is
    # archived into logs/ (never deleted). out_dir was captured above, BEFORE config.json
    # was deleted; the helper takes it explicitly so it can't re-resolve to the default.
    _archive_lastrun_log(out_dir)

    # Return to the first-time (paused) default and drop stale in-memory caches.
    try:
        scheduler.pause_job("engine")
    except Exception:
        pass
    with _connection_health_cache_lock:
        _connection_health_cache.update({"signature": None, "health": None, "checked_at": None})
    # Re-probe the post-reset defaults now: the red Configuration tab reads cached
    # health, and an empty cache would let the first visit clear the onboarding cue even
    # though nothing is configured yet.
    threading.Thread(
        target=lambda: _refresh_connection_health_cache(load_config(), probe=True),
        daemon=True, name="engine-postreset-health",
    ).start()

    if errors:
        return _clear_appearance_cookies(jsonify(
            {"ok": False,
             "message": "Reset completed with problems: " + "; ".join(errors)}), 500)
    return _clear_appearance_cookies(jsonify({"ok": True, "message": "Configuration reset."}))


@app.route("/api/config/reset-invalid", methods=["POST"])
def api_reset_invalid_config():
    """Reset ONLY the hand-edited invalid values in config.json to their defaults,
    leaving valid settings untouched; an unparseable file is replaced wholesale. The
    targeted way out of the invalid-config lockout (Reset MediaReducer is the full one)."""
    if _run_active:
        # Consistency with every other config-mutating endpoint: never rewrite the
        # config (and re-apply the startup safeties) out from under an active run.
        return _run_active_response("error")
    with _config_io_lock:
        defaults = _CONFIG_DEFAULTS
        saved = _read_saved_config_file()
        if saved is None:
            fixed = ["config.json"]
            save_config(defaults, overwrite_invalid=True)
        else:
            # Defaulting a flagged key can surface a NEW issue (the REDLINE_GB default
            # may exceed a valid smaller HEADROOM_GB), so iterate to a fixed point; a key
            # flagged while already at its default drags its cross-check partner along.
            # Every key the validator can flag exists in default_config.json.
            partners = {"REDLINE_GB": ("HEADROOM_GB",)}
            fixed = []
            for _ in range(3):
                issues = _config_file_issues(saved)
                if not issues:
                    break
                for issue in issues:
                    key = issue["key"]
                    if saved.get(key) == defaults.get(key):
                        for partner in partners.get(key, ()):
                            saved[partner] = defaults[partner]
                            fixed.append(partner)
                    saved[key] = defaults[key]
                    fixed.append(key)
            if fixed:
                fixed = list(dict.fromkeys(fixed))
                save_config(saved, overwrite_invalid=True)
    cfg = load_config()  # refreshes _CONFIG_FILE_ISSUES
    # The startup safeties were skipped while the file was invalid; re-apply them now
    # that it is writable, so a hand-edited live mode never rides through the reset.
    if not _CONFIG_FILE_ISSUES:
        force_paused_run_mode_on_startup()
        burn_daily_window_on_startup(reason="invalid-config reset")
        # Both once-per-day mechanisms, like the startup burns: the maintenance
        # latch is keyed on the run epoch, so a reset that changed DAILY_RUN_TIME
        # would otherwise leave it re-armed for an instant catch-up scan.
        burn_todays_maintenance_slot_on_startup()
        cfg = load_config()
    health = _refresh_connection_health_cache(cfg, probe=True)
    residual = _CONFIG_FILE_ISSUES
    return jsonify({
        "ok": not residual,
        "invalid_config": residual,
        "error": ("Some values could not be reset: "
                  + "; ".join(f"{i['key']} {i['message']}" for i in residual)) if residual else "",
        "connection_health": health,
    })


def _carry_forward_omitted_fields(cfg: dict, saved_cfg: dict) -> list:
    """Fill in what this form did not send, and remember a disabled field's value.

    Two different reasons a key can be missing. The scoring fields live on
    another page, so their absence means "unchanged" and they are carried from
    disk — the caller re-reads them just before writing, because this handler
    runs multi-second probes and a scoring save landing mid-probe must not be
    reverted by a stale snapshot. That is why the carried keys come back.

    A disabled threshold is different: it is absent because it is OFF, and its
    last value is kept in a _LAST key so the grayed-out field still shows it
    after a restart. Headroom spells "off" as 0 rather than null, which is why
    it needs the same rule written out a second time.
    """
    # Every key the Filtering & Scoring page owns must be here: the
    # Configuration form posts none of them, and a key missing from this list
    # is silently reset to its default by the next Configuration save.
    # test_score_field_carry derives the required set from _score_page_config,
    # so adding a knob there without adding it here fails a test instead of
    # quietly reverting someone's setting.
    _score_fields = ("GRACE_PERIOD_DAYS", "MAX_IMDB_RATING", "SCORE_BALANCE",
                     "SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES",
                     "NEAR_TIE_PTS", "MAX_STALENESS_MONTHS",
                     "MOVIE_CLEANUP_ENABLED", "TV_CLEANUP_ENABLED",
                     "TV_SEASON_ELIGIBILITY", "TV_MAX_SEASON_EPISODES",
                     "TV_WATCH_WEIGHT", "TV_SERIES_WATCH_BUMP",
                     "_MAX_IMDB_RATING_LAST", "_NEAR_TIE_PTS_LAST")
    _carried_score_fields = [k for k in _score_fields if k not in cfg]
    for key in _carried_score_fields:
        if key in saved_cfg:
            cfg[key] = saved_cfg[key]
    # Disabled optional thresholds keep their last entered value so the grayed-out
    # field still shows it (surviving restarts). The form posts _REDLINE_GB_LAST /
    # _MAX_LIBRARY_GB_LAST with the disabled field's text; fall back to the value
    # being disabled, then the memory on disk (also written by the startup
    # undersized-cap reset). Saving the field enabled clears its memory. Underscore
    # keys bypass the file validator, so only a positive number is ever kept.
    for _opt_key in ("REDLINE_GB", "MAX_LIBRARY_GB"):
        _last_key = f"_{_opt_key}_LAST"
        if cfg.get(_opt_key) is None:
            _last = next((n for n in (_config_num(cfg.get(_last_key)),
                                      _config_num(saved_cfg.get(_opt_key)),
                                      _config_num(saved_cfg.get(_last_key)))
                          if n is not None and n > 0), None)
            if _last is not None:
                cfg[_last_key] = int(_last) if float(_last).is_integer() else _last
            else:
                cfg.pop(_last_key, None)
        else:
            cfg.pop(_last_key, None)
    # Headroom's value memory works the same way, with 0 (its disable toggle,
    # redline-only mode) as the off spelling instead of null.
    _hr_last_key = "_HEADROOM_GB_LAST"
    if _config_num(cfg.get("HEADROOM_GB")) == 0:
        _hr_last = next((n for n in (_config_num(cfg.get(_hr_last_key)),
                                     _config_num(saved_cfg.get("HEADROOM_GB")),
                                     _config_num(saved_cfg.get(_hr_last_key)))
                         if n is not None and n > 0), None)
        if _hr_last is not None:
            cfg[_hr_last_key] = int(_hr_last) if float(_hr_last).is_integer() else _hr_last
        else:
            cfg.pop(_hr_last_key, None)
    else:
        cfg.pop(_hr_last_key, None)
    return _carried_score_fields


def _coerce_notify_fields(cfg: dict, saved_cfg: dict) -> None:
    """Normalize the notification block in place.

    Nothing here can fail a save. A scripted POST that omits these keeps what
    was saved, and a bad service URL is the test button's problem — refusing a
    whole config over an unreachable webhook would block settings that have
    nothing to do with it.
    """
    # Notifications (outbound alerting). A partial/scripted POST that omits
    # these keeps whatever was saved; bad service URLs never block a save —
    # the "Send test notification" button surfaces those instead.
    for _k in notify.CONFIG_KEYS:
        if _k not in cfg:
            cfg[_k] = saved_cfg.get(_k)
    cfg["NOTIFY_ENABLED"] = bool(cfg.get("NOTIFY_ENABLED"))
    for _k, _dflt in notify.TOGGLE_DEFAULTS.items():
        if _k == "NOTIFY_ENABLED":
            continue
        _v = cfg.get(_k)
        cfg[_k] = _dflt if _v is None else bool(_v)  # absent → default
    try:
        cfg["NOTIFY_LOW_SPACE_GB"] = max(1, int(float(
            cfg.get("NOTIFY_LOW_SPACE_GB") or notify.NUMBER_DEFAULTS["NOTIFY_LOW_SPACE_GB"])))
    except (TypeError, ValueError):
        cfg["NOTIFY_LOW_SPACE_GB"] = notify.NUMBER_DEFAULTS["NOTIFY_LOW_SPACE_GB"]
    for _k in notify.STRING_KEYS:
        cfg[_k] = str(cfg.get(_k) or "").strip()
    _custom = cfg.get("NOTIFY_CUSTOM_URLS")
    if isinstance(_custom, str):
        _custom = _custom.replace("\r", "\n").split("\n")
    if not isinstance(_custom, (list, tuple)):
        _custom = []
    cfg["NOTIFY_CUSTOM_URLS"] = [str(u).strip() for u in _custom if str(u).strip()]


@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        if _run_active:
            return _run_active_response("error")
        blocked = _invalid_config_response()
        if blocked:
            return blocked
        cfg = request.get_json(force=True)
        if cfg is None:
            return jsonify({"ok": False, "error": "Invalid JSON"}), 400

        saved_cfg = load_config()
        cfg = _preserve_connection_onboarding_flags(cfg, saved_cfg)
        # Filtering & Scoring lives on its own page (/explorer, saved via
        # /api/score-config). The Config form doesn't send those fields, so carry the
        # saved values through untouched. Remember WHICH keys were carried: they are
        # re-read from disk right before the final write, because this handler runs
        # multi-second probes and a /api/score-config save landing mid-probe must not be
        # reverted by this save's stale snapshot.
        _carried_score_fields = _carry_forward_omitted_fields(cfg, saved_cfg)
        # Compare signatures on a copy normalized EXACTLY like the save below (and like
        # api_config_check); the form posts RADARR_OVERSEERR_SECTION_ID='auto' and raw
        # URL text, while the saved file holds the concrete detected section id and
        # scheme-normalized URLs. Signing the raw body would read every save with
        # Radarr cleanup enabled as "connection settings changed" and force-pause
        # Automatic Cleanup for nothing.
        _sig_cfg = dict(cfg)
        if _sig_cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None:
            _sig_cached = str(_sig_cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip()
            _sig_cfg["RADARR_OVERSEERR_SECTION_ID"] = _sig_cached or "auto"
        _normalize_saved_service_urls(_sig_cfg)
        api_config_changed = _api_config_signature(_sig_cfg) != _api_config_signature(saved_cfg)
        radarr_section_credentials_changed = (
            _radarr_section_detection_signature(cfg)
            != _radarr_section_detection_signature(saved_cfg)
        )
        saved_was_cleanup = _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
        forced_pause_for_api_change = False
        forced_pause_for_tautulli = False
        forced_pause_for_threshold_change = False
        save_health = None
        radarr_section_detection = None
        radarr_section_cache_incomplete = _radarr_section_detection_cache_incomplete(cfg)
        if radarr_section_credentials_changed:
            # The cached section was detected with different Radarr/Plex values. Clear
            # it; a successful one-shot detection below repopulates it for the UI and
            # future cleanup enables.
            _clear_radarr_section_detection_cache(cfg)

        # Two overlapping key lists serve two different rules; keep them apart:
        #   • _PLAN_CONFIG_KEYS (plan staleness): everything that changes WHAT a
        #     run would delete; any mismatch with the Simulate stamp ghosts Automatic Cleanup.
        #   • _threshold_snapshot (this): just the space targets + delay; a save
        #     that changes one of THESE while limits are satisfied unschedules
        #     the delay clocks. DELETE_DELAY_DAYS lives here deliberately and is
        #     NOT a plan key; changing the delay re-paces marks, it doesn't
        #     change which movies are in the plan.
        def _threshold_snapshot(config_obj: dict) -> dict:
            def _number_or_none(value):
                if value is None or value == "":
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return value

            return {
                "HEADROOM_GB": _number_or_none(config_obj.get("HEADROOM_GB")),
                "REDLINE_GB": _number_or_none(config_obj.get("REDLINE_GB")),
                "REDLINE_ONLY_MODE": bool(config_obj.get("REDLINE_ONLY_MODE")),
                "MAX_LIBRARY_GB": _number_or_none(config_obj.get("MAX_LIBRARY_GB")),
                "DELETE_DELAY_DAYS": _number_or_none(config_obj.get("DELETE_DELAY_DAYS", 1)),
            }

        # The form posts a number while Headroom is enabled and 0 when its toggle is
        # off — blank means the enabled field was left empty.
        if _is_blank(cfg.get("HEADROOM_GB")):
            return jsonify({"ok": False, "error": "Enter a Headroom target of at "
                                                  "least 1 GB, or untick Headroom."}), 400

        # Changing thresholds while automatic mode is armed is allowed; the
        # save force-pauses Automatic Cleanup below (same pattern as connection/path
        # changes) so the scheduler never runs on targets no Simulate has
        # previewed. The user reviews with Simulate and re-enables.
        threshold_change_while_cleanup = (
            _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
            and _threshold_snapshot(cfg) != _threshold_snapshot(saved_cfg)
        )

        try:
            headroom_gb = float(cfg.get("HEADROOM_GB"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Headroom must be a number of GB (blank = 0)."}), 400
        if headroom_gb < 0:
            return jsonify({"ok": False, "error": "HEADROOM_GB must be zero or greater."}), 400

        if cfg.get("REDLINE_GB") is not None:
            try:
                redline_gb = float(cfg.get("REDLINE_GB"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Enter a Redline value or disable it."}), 400
            if redline_gb <= 0:
                return jsonify({"ok": False, "error": "REDLINE_GB must be greater than zero, or disabled."}), 400
            # Under an ARMED Headroom (>= 1), Redline must sit STRICTLY below
            # its value — a tie would enforce the full target on every check.
            # With the trigger off there is no ceiling. Keyed to the VALUE,
            # never the flag, matching every other validator.
            if headroom_gb > 0 and redline_gb >= headroom_gb:
                return jsonify({"ok": False, "error": "Redline must be lower than the Headroom "
                                                      "target — or untick Headroom."}), 400

        # The flag is DERIVED from the value (armed iff >= 1), here as at load,
        # so no client can persist a contradiction. Every threshold may be off
        # at once: the lifecycle below rests the scheduler at Paused until one
        # is armed, so nothing needs refusing here.
        cfg["REDLINE_ONLY_MODE"] = headroom_gb <= 0

        try:
            max_headroom_pct = float(cfg.get("MAX_HEADROOM_PCT", 15))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Enter a valid Headroom Safety Percentage."}), 400
        if max_headroom_pct <= 0:
            return jsonify({"ok": False, "error": "MAX_HEADROOM_PCT must be greater than zero."}), 400
        if max_headroom_pct > 100:
            # The percentage caps how much of the disk one run may free; past 100 it
            # neutralizes that guardrail entirely.
            return jsonify({"ok": False, "error": "The Headroom safety percentage cannot exceed 100."}), 400

        if cfg.get("MAX_LIBRARY_GB") is not None:
            try:
                cap_ok = float(cfg.get("MAX_LIBRARY_GB")) > 0
            except (TypeError, ValueError):
                cap_ok = False
            if not cap_ok:
                return jsonify({"ok": False, "error": "Enter a Library Size Cap value or disable it."}), 400


        # Deletion delay: whole days, minimum 1. A marked movie is never deleted
        # the same day it is marked; the earliest is the next day's daily run,
        # so 1 is the floor. Blank = 1.
        raw_delay = cfg.get("DELETE_DELAY_DAYS", 1)
        if _is_blank(raw_delay):
            raw_delay = 1
        try:
            delay_f = float(raw_delay)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Deletion delay must be a whole number of days."}), 400
        if not delay_f.is_integer() or not 1 <= delay_f <= 365:
            return jsonify({"ok": False, "error": "Deletion delay must be a whole number of days from 1 to 365."}), 400
        cfg["DELETE_DELAY_DAYS"] = int(delay_f)

        try:
            grace = int(float(cfg.get("GRACE_PERIOD_DAYS", 30)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Minimum age (grace period) must be a whole number of days."}), 400
        if grace < 0:
            return jsonify({"ok": False, "error": "Minimum age (grace period) must be zero or greater."}), 400
        cfg["GRACE_PERIOD_DAYS"] = grace
        try:
            cfg["SCORE_BALANCE"] = max(0, min(100, round(float(cfg.get("SCORE_BALANCE", 50)))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Score balance must be a number from 0 to 100."}), 400
        raw_cutoff = cfg.get("MAX_IMDB_RATING")
        if not _is_blank(raw_cutoff):
            try:
                if float(raw_cutoff) <= 0:
                    return jsonify({"ok": False, "error": "Maximum IMDb rating must be above 0 — disable it instead."}), 400
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "Maximum IMDb rating must be a number above 0, up to 10, or disabled."}), 400
        cfg["MAX_IMDB_RATING"] = _clamp_max_imdb_rating(raw_cutoff)
    
        cfg["PROTECT_JELLYFIN_FAVORITES"] = _coerce_bool(cfg.get("PROTECT_JELLYFIN_FAVORITES"))
        cfg["SKIP_UNPLAYED_MOVIES"] = _coerce_bool(cfg.get("SKIP_UNPLAYED_MOVIES"))

        try:
            cfg["IMDB_RATINGS_URL"] = _validate_imdb_url(cfg.get("IMDB_RATINGS_URL"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        try:
            cfg["TIME_ZONE"] = _validate_time_zone(cfg.get("TIME_ZONE", "auto"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        cfg["DISPLAY_TIME_FORMAT"] = _validate_display_time_format(cfg.get("DISPLAY_TIME_FORMAT", "12h"))
        try:
            cfg["DAILY_RUN_TIME"] = _validate_daily_run_time(cfg.get("DAILY_RUN_TIME"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        if cfg.get("RUN_MODE") not in ("off", "paused", "headroom"):
            return jsonify({"ok": False, "error": "Choose Paused, Monitor Only or Automatic Cleanup before saving."}), 400
        cfg["RUN_MODE"] = _ui_run_mode(cfg.get("RUN_MODE"))

        # Optional Radarr cleanup is a plain on/off; a Radarr node maps to exactly one
        # Plex section, so the section ID is always auto-detected. Normalize any value
        # (including hand-edited IDs) to the cached detection, or "auto" for the
        # detection below / the engine to resolve.
        if cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None:
            cached_detected_section = str(cfg.get("_RADARR_DETECTED_SECTION_ID") or "").strip()
            cfg["RADARR_OVERSEERR_SECTION_ID"] = cached_detected_section or "auto"

        # Scheme-normalize the URL fields (blank stays blank; defaults apply at
        # connection time) and snapshot the current defaults for consumers without a
        # request context: the engine runs headless, and the address the browser used
        # for this save is the best-known server address.
        _normalize_saved_service_urls(cfg)
        cfg["_SERVICE_URL_DEFAULTS"] = {k: v for k, v in _connection_url_defaults(cfg).items() if v}

        # Connection health for this save; probed only when the answer could have
        # moved (see _health_for_config_save). A run and the scheduler tick both
        # re-probe and refuse to act on a failure, so this is not the safety net
        # it once looked like; it just decided how long Save waited.
        save_health = _health_for_config_save(cfg)

        # A selected server whose API didn't connect on this save (bad credentials,
        # unreachable, blank key) is deselected automatically; the user re-enables it
        # after fixing the connection. Health is recomputed for the deselected state: an
        # unchecked server is off, not an error, so its failure must not keep the UI red.
        server_software_auto_disabled = []
        if bool(cfg.get("USE_PLEX")) and not save_health.get("tautulli_connected"):
            cfg["USE_PLEX"] = False
            server_software_auto_disabled.append("Plex")
        if bool(cfg.get("USE_JELLYFIN")) and not save_health.get("jellyfin_connected"):
            cfg["USE_JELLYFIN"] = False
            server_software_auto_disabled.append("Jellyfin")
        if server_software_auto_disabled:
            save_health = _refresh_connection_health_cache(cfg, probe=True)
        # The TV inventory refreshes on save, while the probe that just ran is
        # fresh: connect a media server, save, and the Filtering page's TV
        # view fills. In its OWN thread: the inventory is every episode from
        # every server plus the per-user watch sweeps — minutes of HTTP on a
        # big library — and the save response must return immediately, not
        # time the browser's fetch out.
        if _tv_inventory_configured(cfg):
            _tv_save_cfg = dict(cfg)
            threading.Thread(target=lambda: _refresh_tv_inventory(_tv_save_cfg),
                             daemon=True, name="tv-inventory-refresh").start()
        # A save is never rejected over connection state; it lands, dependent fields
        # re-lock from the fresh probe, and Automatic Cleanup is just never left armed on shaky ground:
        #   - API/connection edits while Automatic Cleanup was on force a pause (the changed values
        #     invalidate what the scheduler relied on).
        #   - Monitored-path changes force a pause whenever Automatic Cleanup would stay armed: the
        #     new paths change what gets measured, so the library size is stale until the
        #     rescan finishes. Re-enabling Automatic Cleanup is a separate two-click-confirm save.
        #   - Automatic Cleanup (kept or requested) with a failing connection forces a pause.
        forced_pause_for_monitor_change = False
        monitoring_changed = (
            _monitoring_summary_signature(cfg) != _monitoring_summary_signature(saved_cfg)
        )
        # Forced pauses drop to Monitor Only, but never override a mode the user
        # is switching AWAY from cleanup themselves in this same save; either
        # paused mode they chose is already the forced state or safer, and
        # stamping an autopause reason onto a deliberate choice would make the
        # UI explain a "flip" that never happened.
        if api_config_changed and saved_was_cleanup and _is_cleanup_mode(cfg.get("RUN_MODE")):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_api_change = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = "connection settings changed."
        if monitoring_changed and _is_cleanup_mode(cfg.get("RUN_MODE")):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_monitor_change = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = "monitored paths changed — run Simulate under the new paths, then re-enable Automatic Cleanup."
        # A threshold change drops Automatic Cleanup to Monitor Only ONLY when the new
        # thresholds leave the library actually OVER a limit; a run WOULD delete, so
        # pause and let the user review the rebuilt plan first. Within all limits a run
        # would delete nothing, so the reconcile just rebuilds the plan in place and
        # Automatic Cleanup keeps running. Covers both "the change pushes it over a
        # threshold" and "it was already over"; either way the new thresholds are
        # breached against the current disk/library. _deletion_limits_exceeded is
        # fail-safe (pauses when a value can't be read cleanly).
        _disk = disk_stats()
        _library_gb = library_stats().get("library_gb")
        if (threshold_change_while_cleanup and _is_cleanup_mode(cfg.get("RUN_MODE"))
                and _deletion_limits_exceeded(cfg, _disk, _library_gb)):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_threshold_change = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = ("space thresholds changed and the library is over a "
                                                 "limit — review the rebuilt deletion plan, then "
                                                 "re-enable Automatic Cleanup.")
        if _is_cleanup_mode(cfg.get("RUN_MODE")) and not save_health.get("critical_ok", True):
            cfg["RUN_MODE"] = "paused"
            forced_pause_for_tautulli = True
            cfg["_RUN_MODE_AUTOPAUSE_REASON"] = (save_health.get("required_tooltip")
                                                 or "the media server connection is not healthy.")

        # Onboarding lifecycle, AFTER the forced-pause chain so an explicit
        # Automatic Cleanup request gets the arming gates' real answer first:
        # incomplete setup rests Monitor Only at fully-Paused; setup just
        # completed while resting wakes it into Monitor Only automatically.
        setup_mode_lifecycle = _apply_setup_mode_lifecycle(
            cfg, saved_cfg, save_health,
            forced_pause=(forced_pause_for_api_change or forced_pause_for_tautulli
                          or forced_pause_for_monitor_change
                          or forced_pause_for_threshold_change))

        if _is_cleanup_mode(cfg.get("RUN_MODE")):
            # candidate_cfg: the plan stamp must be judged against the config
            # BEING SAVED, not the old file it replaces; otherwise one save
            # could change thresholds AND arm automatic mode against a stamp
            # only the old thresholds match.
            thresholds = _space_threshold_state(cfg, _disk, candidate_cfg=True)
            if not thresholds.get("ok_for_cleanup"):
                # A threshold change that leaves the library over a limit force-paused
                # above; a within-limits change stays armed. So this is the unsafe /
                # no-target case (invalid values, or nothing driving cleanup); point at
                # the fix rather than saving an armed config that can't run.
                return jsonify({
                    "ok": False,
                    "error": thresholds.get("cleanup_tooltip") or "Fix Space Thresholds first.",
                }), 400
            # Arming automatic mode always requires that a Simulate has seen the
            # library: over breached limits that means a deletion plan stamped
            # under exactly these thresholds; within limits, proof that at least
            # one Simulate has run (the library snapshot). Without it the first
            # automatic run would act sight-unseen.
            if (not _is_cleanup_mode(saved_cfg.get("RUN_MODE"))
                    and thresholds.get("simulate_required")):
                return jsonify({
                    "ok": False,
                    "error": (thresholds.get("simulate_required_message")
                              or "Run Simulate first, then enable Automatic Cleanup."),
                }), 400

        # An empty MONITOR_DIRS is valid and means "manage nothing" until the
        # user explicitly adds at least one monitored library path.
        _normalize_library_paths(cfg)

        base = Path(FILESYSTEM_CHECK_PATH).resolve(strict=False)
        for monitored_path in cfg.get("MONITOR_DIRS", []) or []:
            try:
                target = Path(monitored_path).resolve(strict=False)
                target.relative_to(base)
            except ValueError:
                return jsonify({"ok": False, "error": f"Monitored directories must stay inside {FILESYSTEM_CHECK_PATH}."}), 400
            if not target.exists() or not target.is_dir():
                return jsonify({"ok": False, "error": f"Monitored directory does not exist: {target}"}), 400

        cfg["CHECK_PATH"] = FILESYSTEM_CHECK_PATH
        cfg["TAUTULLI_APPDATA"] = TAUTULLI_APPDATA_DIR
        cfg["RADARR_APPDATA"] = RADARR_APPDATA_DIR
        cfg["SONARR_APPDATA"] = SONARR_APPDATA_DIR
        # OUTPUT_DIR is infrastructure, not a setting: it decides where app and engine
        # write their files, so it is never accepted from the request body; only the
        # value already on disk carries through.
        cfg.pop("OUTPUT_DIR", None)
        if "OUTPUT_DIR" in saved_cfg:
            cfg["OUTPUT_DIR"] = saved_cfg["OUTPUT_DIR"]

        radarr_cleanup_forced_disabled = False
        if (
            cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None
            and save_health
            and not save_health.get("radarr_connected")
        ):
            # Optional Radarr cleanup can't run without a verified Radarr API. If a save
            # breaks or removes that connection, turn cleanup off instead of keeping a
            # stale enabled value and highlighting the cleanup section.
            cfg["RADARR_OVERSEERR_SECTION_ID"] = None
            radarr_cleanup_forced_disabled = True
            save_health = dict(save_health)
            save_health["radarr_cleanup_forced_disabled"] = True

        if (
            (radarr_section_credentials_changed or radarr_section_cache_incomplete)
            and save_health
            and save_health.get("radarr_connected")
            and save_health.get("plex_connected")
        ):
            # One-shot section auto-detection: runs after saved Radarr/Plex credentials
            # connect, not when the cleanup checkbox is toggled. The cache-repair branch
            # re-runs detection when the cached entry has a section ID but is missing its
            # name/match metadata.
            radarr_section_detection = _detect_radarr_plex_section(cfg)
            detected_section_id = str(radarr_section_detection.get("section_id") or "").strip()
            if _store_radarr_section_detection_cache(cfg, radarr_section_detection):
                if str(cfg.get("RADARR_OVERSEERR_SECTION_ID") or "").strip().lower() == "auto":
                    cfg["RADARR_OVERSEERR_SECTION_ID"] = detected_section_id

            elif radarr_section_credentials_changed:
                _clear_radarr_section_detection_cache(cfg)

        # Logging settings (Advanced)
        try:
            cfg["LOG_RETENTION_DAYS"] = max(0, int(float(cfg.get("LOG_RETENTION_DAYS", 30))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Keep run logs for (days) must be a whole number (0 = keep forever)."}), 400
        cfg["KEEP_INTERRUPTED_LOGS"] = bool(cfg.get("KEEP_INTERRUPTED_LOGS"))
        cfg["DEBUG_MODE"] = bool(cfg.get("DEBUG_MODE"))

        _coerce_notify_fields(cfg, saved_cfg)

        # IMDb refresh interval: the file validator requires >= 1, so clamp here too —
        # otherwise a blank/0 saves fine, then the next load flags it and locks the app.
        try:
            cfg["IMDB_RATINGS_MAX_AGE_DAYS"] = max(1, int(float(cfg.get("IMDB_RATINGS_MAX_AGE_DAYS", 7))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "IMDb refresh interval must be a whole number of days (1 or more)."}), 400

        # Server software (Plex / Jellyfin). Missing/wrong credentials never block a
        # save — the health check flags them, dependent features lock, and runs stay
        # disabled until every selected server connects or the broken one is deselected.
        cfg["USE_PLEX"] = bool(cfg.get("USE_PLEX"))
        cfg["USE_JELLYFIN"] = bool(cfg.get("USE_JELLYFIN"))
        # Both may be off; the default onboarding state. Nothing is functional
        # until a server is enabled and its API connects.

        cfg["JELLYFIN_API_KEY"] = str(cfg.get("JELLYFIN_API_KEY") or "").strip()
        jf_prot = cfg.get("JELLYFIN_PROTECTED_COLLECTIONS", [])
        if isinstance(jf_prot, str):
            jf_prot = [p.strip() for p in jf_prot.split(",")]
        cfg["JELLYFIN_PROTECTED_COLLECTIONS"] = [p for p in (jf_prot or []) if str(p).strip()]

        # Leaving cleanup mode aborts an active run, but only a DELETING one
        # (_run_cleanup). The save handler's probes take seconds, so the daily
        # maintenance Simulate (or any other no-delete run) can start mid-save;
        # killing it would cost the day's scan and notification for nothing.
        should_abort_active_run = (_is_cleanup_mode(saved_cfg.get("RUN_MODE"))
                                   and not _is_cleanup_mode(cfg.get("RUN_MODE")))
        # Refresh the carried Filtering & Scoring fields from the CURRENT file, not the
        # snapshot taken before the multi-second probes above; a /api/score-config save
        # that landed mid-probe would otherwise be reverted by this write.
        _latest_cfg = load_config()
        for key in _carried_score_fields:
            if key in _latest_cfg:
                cfg[key] = _latest_cfg[key]
        # Never persist a config the loader would flag. The form can't produce these,
        # but a scripted POST could (HEADROOM_GB=1e999, a non-list MOVIE_EXTENSIONS, a
        # non-string API key…); saved unchecked it would wedge every endpoint behind
        # the "config.json was edited outside MediaReducer" lockout on next load.
        _outgoing_issues = _config_file_issues(cfg)
        if _outgoing_issues:
            return jsonify({
                "ok": False,
                "error": "Save rejected — " + "; ".join(
                    f"{i['key']} {i['message']}" for i in _outgoing_issues),
                "invalid_config": _outgoing_issues,
            }), 400
        if not save_config(cfg):
            # A hand edit landed between the entry guard and the write.
            return _invalid_config_response() or (jsonify({
                "ok": False, "error": "Save was refused — config.json changed on disk. Reload the page.",
            }), 409)
        if save_health is not None:
            with _connection_health_cache_lock:
                _connection_health_cache.update({
                    "signature": _connection_health_signature(cfg),
                    "health": save_health,
                    "checked_at": time.time(),
                })
        if should_abort_active_run and _run_cleanup:
            stop_script()
        # Any transition into or out of Automatic Cleanup resets the background clock to a FULL
        # interval — the interval job keeps counting across mode changes, and
        # re-enabling Automatic Cleanup must never inherit a near-expired timer that fires an
        # automatic run almost immediately. (Covers user changes and every forced
        # pause, which all mutate RUN_MODE before this save.)
        if _is_cleanup_mode(saved_cfg.get("RUN_MODE")) != _is_cleanup_mode(cfg.get("RUN_MODE")):
            _restart_schedule_clock()
        # Point the process clock at the saved zone. Moving the zone moves the midnight
        # boundary, so re-burn today's run window; like the startup burn, a clock change
        # must never grant an immediate run.
        _tz_changed = _apply_configured_time_zone(cfg)
        if _tz_changed:
            burn_daily_window_on_startup(reason="time zone changed")
            # The maintenance latch is keyed on the run EPOCH, so moving the
            # zone re-arms it implicitly; burn it in step with the window or
            # the next tick fires an instant catch-up scan (both burns keep a
            # slot still ahead under the new clock armed).
            burn_todays_maintenance_slot_on_startup()
        # Adjusting the daily run time within the same day, in the configured zone
        # (already applied above), matching the engine's own comparison:
        #   • moved to a slot already behind the clock (e.g. 3am → 1am at 2:20am):
        #     treat it as missed and burn today's window so the new time starts
        #     tomorrow — never an instant catch-up run.
        #   • moved to a slot still ahead when today's run already fired: reopen the
        #     window so it can run again today at the new time. Safe with the delay
        #     (a same-day re-run only re-marks). Skipped if the zone also changed —
        #     that burn is a deliberate safety reset we must not undo here.
        _old_rt, _new_rt = _daily_run_time(saved_cfg), _daily_run_time(cfg)
        _now_hhmm = time.strftime("%H:%M")
        if _run_time_moved_into_past(_old_rt, _new_rt, _now_hhmm):
            burn_daily_window_on_startup(reason="daily run time moved earlier")
            # Same for the maintenance latch: the new (already-passed) run
            # epoch differs from the stamped one, so without this burn the
            # next tick would treat the moved slot as owed and launch an
            # instant catch-up Simulate; exactly what "moved earlier means
            # missed, starts tomorrow" rules out.
            burn_todays_maintenance_slot_on_startup()
        elif not _tz_changed and _run_time_moved_ahead_today(_old_rt, _new_rt, _now_hhmm):
            reopen_daily_window(reason="daily run time moved later")
        # Re-aim the precise daily-slot shot at the (possibly new) run time.
        _arm_daily_slot_job(cfg)
        # Removing or lowering a space limit unschedules the marked prefix: the
        # running delay clocks were for a breach that no longer exists. The queue
        # itself stays — it is the standing eligible deletion order in every mode,
        # exactly matching the engine's own satisfied-limits upkeep
        # (_revalidate_pending_marks). Reconcile here ONLY when this save actually
        # changed a threshold: an unrelated save judging "satisfied" off a stale
        # library size would silently reset live delay clocks. Disk figures are
        # read fresh for the same reason. Redline-only mode is exempt, since its
        # queue carries no clocks.
        #
        # The check-and-write runs under _run_lock: the entry guard's run check
        # is stale by now (the probes above take seconds), and a run starting
        # mid-write does its own load/modify/save of the same file.
        pending_unscheduled = 0
        with _run_lock:
            if (not _run_active and not _summary_active and pending_count()
                    and not _redline_only_mode_cfg(cfg)
                    and _threshold_snapshot(cfg) != _threshold_snapshot(saved_cfg)):
                try:
                    _disk = disk_stats()
                    _lib_gb = library_stats().get("library_gb")
                except Exception:
                    _disk, _lib_gb = None, None
                if not _deletion_limits_exceeded(cfg, _disk, _lib_gb):
                    pending_unscheduled = _unschedule_pending_marks()

        # Storage stats only refresh when the saved config changes what the size scan
        # measures. Runs do their own precheck, so threshold-only saves stay quick and
        # don't kick off disk work.
        summary_started = False
        summary_message = "Summary refresh not needed."
        if _should_refresh_summary_after_config_save(saved_cfg, cfg):
            summary_started, summary_message = run_summary()

        # Rebuild the marked/eligible queue in place from the snapshot when this save
        # changed a threshold, protected collection, or favorites setting; no Simulate.
        # A collections/favorites change re-fetches the live sets; if the server it
        # needs is unreachable, the reconcile is held and the Connections check flags it.
        reconcile_status = _reconcile_after_save(saved_cfg, cfg, source="configuration")

        return jsonify({
            "ok": True,
            "config": cfg,
            "connection_health": save_health,
            "api_config_changed": api_config_changed,
            "server_software_auto_disabled": server_software_auto_disabled,
            "radarr_section_detection": radarr_section_detection,
            "reconcile": reconcile_status,
            # Setup just completed and the resting "off" mode woke into Monitor
            # Only — the UI announces the switch.
            "run_mode_auto_enabled": setup_mode_lifecycle == "auto_enabled",
            # Monitor Only lost a piece it needs (server, path, or a current
            # library database) and rested at Paused; say so, or the flipped
            # radio just reads as a setting that didn't stick.
            "run_mode_forced_off": setup_mode_lifecycle == "forced_off",
            "automatic_run_mode_paused": (forced_pause_for_api_change or forced_pause_for_tautulli
                                          or forced_pause_for_monitor_change
                                          or forced_pause_for_threshold_change),
            "automatic_run_mode_paused_reason": (
                "connection settings changed."
                if forced_pause_for_api_change else
                "monitored paths changed — run Simulate under the new paths, then re-enable Automatic Cleanup."
                if forced_pause_for_monitor_change else
                "space thresholds changed and the library is over a limit — review the rebuilt deletion plan, then re-enable Automatic Cleanup."
                if forced_pause_for_threshold_change else
                (save_health.get("required_tooltip") if save_health else "")
                or "the media server connection is not healthy."
                if forced_pause_for_tautulli else ""
            ),
            "radarr_cleanup_forced_disabled": radarr_cleanup_forced_disabled,
            "summary_refresh_started": summary_started,
            "pending_unscheduled": pending_unscheduled,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400



@app.route("/api/imdb/status")
def api_imdb_status():
    state = _imdb_download_state()
    try:
        state["url"] = str(load_config().get("IMDB_RATINGS_URL") or "").strip()
    except Exception:
        state["url"] = ""
    return jsonify(state)


@app.route("/api/imdb/download", methods=["POST"])
def api_imdb_download():
    """Force-download the latest IMDb ratings TSV, throttled to once every 24 hours."""
    blocked = _filesystem_write_block_response(_imdb_download_state())
    if blocked:
        return blocked
    if _run_active:
        return _run_active_response(status=_imdb_download_state())

    status = _imdb_download_state()
    if status.get("exists") and status.get("download_locked"):
        return jsonify({
            "ok": False,
            "message": "IMDb ratings were already downloaded within the last 24 hours.",
            "status": status,
        }), 429

    try:
        cfg = load_config()
        url = _validate_imdb_url(cfg.get("IMDB_RATINGS_URL"))
        target = imdb_ratings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")

        tsv_data = _download_imdb_gz(url)
        if not tsv_data.startswith(b"tconst\taverageRating\tnumVotes"):
            raise ValueError("Downloaded file did not look like the IMDb title.ratings.tsv dataset.")

        tmp.write_bytes(tsv_data)
        tmp.replace(target)

        new_status = _imdb_download_state()
        return jsonify({
                "ok": True,
            "message": f"IMDb ratings downloaded and extracted ({new_status.get('size_mb')} MB).",
            "status": new_status,
        })
    except Exception as e:
        try:
            imdb_ratings_path().with_name(imdb_ratings_path().name + ".tmp").unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"ok": False, "message": str(e), "status": _imdb_download_state()}), 400

@app.route("/api/cache/status")
def api_cache_status():
    return jsonify(_cache_clear_state())

@app.route("/api/cache/clear", methods=["POST"])
def api_clear_cache():
    """Delete the entire cache store, including daily cleanup and dashboard stats."""
    blocked = _filesystem_write_block_response(_cache_clear_state())
    if blocked:
        return blocked
    if _run_active:
        return _run_active_response(status=_cache_clear_state())
    # A storage summary in flight merges its stats back into the store on exit —
    # a wipe racing that write gets silently resurrected. Same wait-then-refuse as
    # the full reset. (No run/summary is writing, so nothing else holds the DB.)
    for _ in range(100):  # up to ~10s
        if not _summary_active:
            break
        time.sleep(0.1)
    if _summary_active:
        return jsonify({
            "ok": False,
            "message": "A background storage refresh is still running — "
                       "try again in a moment.",
            "status": _cache_clear_state(),
        }), 409

    p = db_path()
    if not p.exists():
        return jsonify({
            "ok": False,
            "message": "No cache store exists yet.",
            "status": _cache_clear_state(),
        }), 404

    removed_movies = 0
    try:
        with db.connect(p) as conn:
            removed_movies = conn.execute(
                "SELECT COUNT(*) FROM metadata_cache").fetchone()[0]
    except Exception:
        # Corrupt/unreadable store still gets deleted below.
        pass

    # Full wipe — the library snapshot goes with it, so the Filtering & Scoring
    # table stays empty until the next Simulate or Cleanup rewrites it. reset_store
    # deletes the .db and its WAL/SHM sidecars under the init lock, so a concurrent
    # status-poll read never lands on a table-less DB.
    #
    # Close the check→unlink window under _run_lock: run_script / run_summary /
    # run_reconcile all take it to set their active flag, so holding it across a
    # final re-check + the wipe stops one from starting mid-reset and writing into
    # the just-unlinked store (its engine subprocess would then hit a table-less DB).
    with _run_lock:
        if _run_active or _summary_active:
            return jsonify({
                "ok": False,
                "message": "A run started — try clearing the cache again in a moment.",
                "status": _cache_clear_state(),
            }), 409
        try:
            db.reset_store(p)
        except OSError as e:
            return jsonify({
                "ok": False,
                "message": f"Could not clear the cache: {e}",
                "status": _cache_clear_state(),
            }), 500
        _drop_store_caches()

    # The wipe also lost the once-per-day window; re-stamp it so clearing the cache
    # never grants the scheduler a free immediate daily run.
    burn_daily_window_on_startup(reason="cache cleared")

    details = f"Cache cleared ({removed_movies} movie entr{'y' if removed_movies == 1 else 'ies'})."

    summary_started, summary_message = run_summary()
    if summary_started or _summary_active:
        details += " Refreshing storage stats…"
    else:
        details += f" Storage refresh not started: {summary_message}"
    summary_refresh_active = summary_started or _summary_active
    return jsonify({
        "ok": True,
        "message": details,
        "status": _cache_clear_state(),
        "summary_refresh_started": summary_refresh_active,
    })

@app.route("/api/run", methods=["POST"])
def api_run():
    if _run_active:
        return jsonify({"ok": False, "started": False,
                        "message": "A run is already active. Try again when it finishes."}), 409
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    mode = data.get("mode")

    # SAFETY: a missing/unparseable mode must never become a live deletion. Both
    # Dashboard buttons send an explicit mode ("debug_sim" for Simulate, "headroom" for
    # a manual Cleanup, which works even while the scheduler is paused). Anything
    # ambiguous (empty body, garbled JSON, a scripted request with no mode) falls back
    # to Simulate, the non-destructive path. debug_info is not user-facing; the
    # Dashboard's ↻ storage refresh goes through /api/summary/run.
    if mode is None:
        mode = "debug_sim"
    if mode not in ("debug_sim", "headroom", "debug_cleanup"):
        return jsonify({"ok": False, "message": f"Unknown run mode: {mode}"}), 400
    # Debug mode ↔ live are mutually exclusive. "debug_cleanup" is the no-delete
    # diagnostic run that replaces the manual Cleanup while Debug mode is on, so
    # it requires Debug mode; conversely a real live "headroom" run is refused
    # while Debug mode is on (the dashboard morphs the button to Debug Cleanup).
    _debug_on = bool(cfg.get("DEBUG_MODE"))
    if mode == "debug_cleanup" and not _debug_on:
        return jsonify({"ok": False, "message": "Turn on Debug mode in Configuration to use Debug Cleanup."}), 400
    if mode == "headroom" and _debug_on:
        return jsonify({"ok": False, "message": "Debug mode is on — use Debug Cleanup (no deletions)."}), 400
    effective_mode = mode

    health = _refresh_connection_health_cache(cfg, probe=True)
    if not health.get("critical_ok", True):
        return jsonify({
            "ok": False,
            "message": health.get("required_tooltip") or "Connect the selected media server first.",
        }), 400

    disk = disk_stats()
    thresholds = _space_threshold_state(cfg, disk)
    if not _has_monitored_dirs(cfg):
        return jsonify({
            "ok": False,
            "message": NO_MONITORED_DIRS_MESSAGE,
        }), 400

    # debug_cleanup uses the SIMULATE gate, not the cleanup gate: like Simulate it runs
    # even past the 15% headroom safety cap (it deletes nothing), so it must not
    # be blocked by ok_for_cleanup; only by an actually-invalid config.
    if effective_mode in ("debug_sim", "debug_cleanup") and not thresholds.get("ok_for_simulate"):
        return jsonify({
            "ok": False,
            "message": thresholds.get("simulate_tooltip") or "Fix Space Thresholds first.",
        }), 400

    # Debug Cleanup replays the standing marked queue, so it needs a CURRENT plan.
    # Without one (no Simulate yet, or settings changed since), refuse with a hint
    # instead of spinning up a run that can only log "run Simulate first".
    if effective_mode == "debug_cleanup" and thresholds.get("simulate_required"):
        return jsonify({
            "ok": False,
            "message": (thresholds.get("simulate_required_message")
                        or "Run Simulate first — Debug Cleanup replays the marked "
                           "& eligible queue a Simulate builds."),
        }), 400

    if _is_cleanup_mode(effective_mode) and not thresholds.get("ok_for_cleanup"):
        return jsonify({
            "ok": False,
            "message": thresholds.get("cleanup_tooltip") or "Fix Space Thresholds first.",
        }), 400

    # A manual Cleanup deletes immediately (no delay, no daily window), so over
    # breached limits it needs a deletion plan computed under the current
    # thresholds. Within satisfied limits the run is a harmless no-op; the
    # fresh-stats check below reports "already satisfied" instead, which is the
    # same reason the dashboard ghosts the button; don't demand a Simulate here.
    if _is_cleanup_mode(effective_mode) and thresholds.get("simulate_required"):
        try:
            _breached_now = _deletion_limits_exceeded(cfg, disk, library_stats().get("library_gb"))
        except Exception:
            _breached_now = True   # can't tell — keep the safe gate
        if _breached_now:
            return jsonify({"ok": False, "message": thresholds.get("simulate_required_message")
                                                    or "Run Simulate first."}), 400

    # Same pre-check the automatic cleanup tick uses: if every space limit is already
    # satisfied (nothing over Headroom/Redline against the current filesystem, library
    # under the cap), a Simulate or Cleanup would delete nothing, so report it instead
    # of spinning one up. debug_info (status refresh) is exempt.
    if effective_mode in ("debug_sim", "headroom"):
        refresh_ok, refresh_msg, fresh_stats = run_summary_sync()
        if refresh_ok:
            fresh_disk = cached_disk_stats(fresh_stats)
            if fresh_disk:
                disk = fresh_disk
                thresholds = _space_threshold_state(cfg, disk)
                if effective_mode == "debug_sim" and not thresholds.get("ok_for_simulate"):
                    return jsonify({
                        "ok": False,
                        "message": thresholds.get("simulate_tooltip") or "Fix Space Thresholds first.",
                    }), 400
                if _is_cleanup_mode(effective_mode) and not thresholds.get("ok_for_cleanup"):
                    return jsonify({
                        "ok": False,
                        "message": thresholds.get("cleanup_tooltip") or "Fix Space Thresholds first.",
                    }), 400
            # A Simulate is exempt from the satisfied skip in EVERY mode: its job
            # includes building/refreshing the standing marked & eligible queue,
            # which exists regardless of breach state. Only cleanups skip —
            # and "already satisfied" outranks "run Simulate first", matching
            # the dashboard's ghost reason for the same state.
            if effective_mode != "debug_sim" and not _deletion_limits_exceeded(cfg, disk, fresh_stats.get("library_gb")):
                return jsonify({
                    "ok": True,
                    "started": False,
                    "message": "Space limits are already satisfied.",
                })
            if _is_cleanup_mode(effective_mode) and thresholds.get("simulate_required"):
                return jsonify({"ok": False, "message": thresholds.get("simulate_required_message")
                                                        or "Run Simulate first."}), 400
            # A manual Cleanup needs no daily-window pre-check: it prunes every breached
            # target immediately, ignoring the once-per-day schedule and deletion delay
            # (both pace automatic runs).
        else:
            # Degrade, don't block: this precheck only skips a pointless run. The engine
            # measures disk and library at run start and no-ops when every limit is
            # satisfied, so a summary that timed out or was busy must not make
            # Simulate and Cleanup unstartable.
            print(f"Storage precheck skipped, starting the run anyway: {refresh_msg}", flush=True)

    ok, msg = run_script(mode_override=mode, manual=True)
    return jsonify({"ok": ok, "started": ok, "message": msg})

@app.route("/api/summary/run", methods=["POST"])
def api_summary_run():
    """Trigger a quiet background Summary (debug_info) to refresh dashboard stats. Never
    touches lastrun.log or the progress panel, and is mutually exclusive with real runs
    via the run lock."""
    ok, msg = run_summary()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/run/stop", methods=["POST"])
def api_stop():
    stopped = stop_script()
    return jsonify({"ok": stopped, "message":
                    ("Stopped — deletions already made are permanent." if stopped else "No active run.")})


@app.route("/api/run/force-stop", methods=["POST"])
def api_force_stop():
    """The escape hatch behind Config → Advanced: SIGKILL a run that Stop can't
    end, so a wedged engine can't leave the settings locked forever."""
    ok, message = force_stop_script()
    return jsonify({"ok": ok, "message": message}), (200 if ok else 409)

_progress_file_memo_lock = threading.Lock()
_progress_file_memo = {"key": None, "data": {}}


@app.route("/api/run/progress")
def api_run_progress():
    """Structured run progress for the dashboard panel (see engine.emit_progress).
    Parsed once per file version ((path, mtime, size) memo): during a run every
    write changes the key, and on an idle server the polls all hit the memo."""
    p = progress_path()
    data = {}
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        with _progress_file_memo_lock:
            memo_hit = _progress_file_memo["key"] == key
            if memo_hit:
                data = _progress_file_memo["data"]
        if not memo_hit:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            with _progress_file_memo_lock:
                _progress_file_memo["key"] = key
                _progress_file_memo["data"] = data
        data = dict(data)   # the fallbacks below may mutate; never edit the memo copy
    except OSError:
        data = {}
    # If the process is gone but the file never reached a terminal state (crash/kill),
    # show the last phase as failed instead of a phantom running state.
    if not _run_active and data.get("status") in ("starting", "running"):
        data["status"] = "error"
        # NOT `or data["message"]`: this branch only fires while the file still
        # holds an IN-PROGRESS line ("Checking connections…"), and the panel now
        # prints the message as the failure itself — in red, behind a ×. Keeping
        # it would name whatever the run was doing as the thing that went wrong.
        data["message"] = RUN_ENDED_UNEXPECTEDLY
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

# ── Log section markers ───────────────────────────────────────────────────────
# Match the engine's exact log strings (sim and live) so the dashboard's stat tiles
# can deep-link into the run log. A section reports available only once the run has
# genuinely written its marker. Each stage opens with a one-line
# "====== <TITLE> ======" banner from the engine's log_stage(); the content markers
# are fallbacks in case a banner is ever missing from a partial log.
_LOG_SECTION_RES = {
    # SCAN was renamed SCORING when the log stages were aligned with the
    # dashboard's steps; both match so links still work in ARCHIVED logs, which
    # were written before the rename and are never rewritten.
    "scan":      re.compile(r"={3,} (SCORING|SCAN) ={3,}|Processing [\d,]+ unique movie entries\."),
    "library":   re.compile(r"={3,} READING LIBRARY ={3,}|Resolving file paths for"),
    # "Marked-queue re-verify" is the Debug Cleanup / fast-path analog of the full
    # scan's ELIGIBLE CANDIDATES stage; the eligible & marked queue, re-scored.
    "eligible":  re.compile(r"={3,} ELIGIBLE CANDIDATES ={3,}|Candidate stats:|candidates sorted by deletion priority|Marked-queue re-verify"),
    # A Debug Cleanup previews the deletions ("Delete-from-queue preview" banner →
    # the WOULD DELETE list); a real Cleanup logs "Deleted file:".
    "deletions": re.compile(r"={3,} (SIMULATION|DELETIONS) ={3,}|Simulating deletions — target:|DRY RUN DELETE #1:|Deleted file: |Delete-from-queue preview"),
    "summary":   re.compile(r"SUMMARY {1,2}\["),
    "errors":    re.compile(r"ERROR|ABORT|WARN(ING)?[ :]|SKIP identity_mismatch|COMPLETED WITH ERRORS"),
}
_LOG_SECTION_MAX_LINES = 30000


def _log_section_indexes(lines: list) -> dict:
    """First matching line index for each section marker (or None)."""
    idx = {k: None for k in _LOG_SECTION_RES}
    for i, line in enumerate(lines):
        for kind, rx in _LOG_SECTION_RES.items():
            if idx[kind] is None and rx.search(line):
                idx[kind] = i
        if all(v is not None for v in idx.values()):
            break
    return idx


# /api/logs/last is polled every 750 ms while a run streams and lastrun.log grows to
# many MB, so re-scanning the whole file per poll is O(file) forever. The section flags
# only need which markers EXIST, so scan incrementally: remember the byte position and
# found flags, read only appended bytes, and reset when the file shrinks or is replaced.
_log_scan_lock = threading.Lock()
_log_scan_state = {"key": None, "pos": 0, "found": {}, "partial": ""}


def _log_sections_found(p: Path) -> dict:
    """{kind: bool} for the section jump buttons, scanning only new bytes."""
    try:
        st = p.stat()
        key = (st.st_dev, st.st_ino)
    except OSError:
        return {k: False for k in _LOG_SECTION_RES}
    with _log_scan_lock:
        s = _log_scan_state
        if s["key"] != key or st.st_size < s["pos"]:
            s.update({"key": key, "pos": 0,
                      "found": {k: False for k in _LOG_SECTION_RES}, "partial": ""})
        if not all(s["found"].values()) and st.st_size > s["pos"]:
            with open(p, "rb") as f:
                f.seek(s["pos"])
                chunk = f.read()
                s["pos"] += len(chunk)
            lines = (s["partial"] + chunk.decode("utf-8", errors="replace")).split("\n")
            s["partial"] = lines.pop()  # the last piece may be mid-write
            for line in lines:
                for kind, rx in _LOG_SECTION_RES.items():
                    if not s["found"][kind] and rx.search(line):
                        s["found"][kind] = True
        return dict(s["found"])


def _read_tail_lines(p: Path, n: int) -> list:
    """Last n lines without reading the whole file: seek back in growing blocks from the
    end until enough newlines are covered."""
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        want = min(size, max(4096, n * 200))
        while True:
            f.seek(size - want)
            data = f.read(want)
            if want == size or data.count(b"\n") > n:
                break
            want = min(size, want * 2)
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    if want < size and lines:
        lines = lines[1:]  # first line is partial unless we read the whole file
    return lines[-n:]


def _error_banner_start(lines: list):
    """Index of the one-line "!!!!!! COMPLETED WITH … !!!!!!" banner that opens
    the run-end error report, or None when the run has none.

    The prefix, not the whole phrase: the banner is run_issues.headline()
    uppercased, so it reads like "COMPLETED WITH 1 KIND OF ERROR AND 1 WARNING"
    and varies with the run. Matching a fixed literal finds nothing, and the
    errors view then falls through to its flagged-lines fallback without a word
    — a filtered list where the structured block was available. A warnings-only
    run prints the same headline in a dashed, un-uppercased banner, which this
    still skips.
    """
    return next((i for i, ln in enumerate(lines) if "COMPLETED WITH " in ln), None)


def _extract_errors_report(lines: list, idx: dict):
    """The errors view, in order of preference:

    1. The run-end error report — the "!!!!!" banner block the engine prints
       right before the summary, which lays out every skipped file with its
       explanation. Served whole, from the banner to the summary border.
    2. For runs that died early: everything from the first ABORT/ERROR line to
       the end of the file (the failure and its context).
    3. Fallback: every flagged line in the run (errors, warnings, identity
       mismatches) as a filtered list.
    """
    start = _error_banner_start(lines)
    if start is not None:
        summary_i = idx.get("summary")
        end = summary_i if (summary_i is not None and summary_i > start) else len(lines)
        return True, "".join(lines[start:end][:_LOG_SECTION_MAX_LINES])

    abort_re = re.compile(r"ABORT|ERROR")
    abort_i = next((i for i, ln in enumerate(lines) if abort_re.search(ln)), None)
    if abort_i is not None:
        # Present a run that died partway like the identity-mismatch report: a banner,
        # then the failure and its context.
        banner = "!!!!!! RUN FAILED — stopped before finishing !!!!!!\n\n"
        return True, banner + "".join(lines[abort_i:][:_LOG_SECTION_MAX_LINES])

    rx = _LOG_SECTION_RES["errors"]
    hits = [ln for ln in lines if rx.search(ln)]
    if not hits:
        return False, ""
    header = f"{len(hits)} flagged line(s) in this run (errors, warnings, identity mismatches):\n\n"
    return True, header + "".join(hits[:_LOG_SECTION_MAX_LINES])


def _extract_log_section(lines: list, kind: str):
    """Return (found, text) for one section of a run log.

    Sections are contiguous slices between the boundary markers above, except
    "errors" which is the filtered set of flagged lines from the whole run.
    """
    idx = _log_section_indexes(lines)
    if kind == "errors":
        return _extract_errors_report(lines, idx)

    if idx.get(kind) is None:
        return False, ""

    # Each stage opens on its one-line "====== TITLE ======" banner; a section
    # runs from its banner to the next later-ordered section's banner.
    starts = {k: idx.get(k) for k in ("scan", "eligible", "deletions", "summary")}
    start = starts[kind]

    if kind == "summary":
        end = len(lines)
    else:
        later = [v for k, v in starts.items()
                 if v is not None and v > start and _SECTION_ORDER[k] > _SECTION_ORDER[kind]]
        # The run-end error report sits between deletions and summary; it belongs
        # to the errors view, so cap content sections there instead of absorbing it.
        banner = _error_banner_start(lines)
        if banner is not None and banner > start:
            later.append(banner)
        end = min(later) if later else len(lines)
    return True, "".join(lines[start:end][:_LOG_SECTION_MAX_LINES])


_SECTION_ORDER = {"scan": 0, "eligible": 1, "deletions": 2, "summary": 3}


@app.route("/api/logs/last")
def api_logs_last():
    p = log_path()
    if not p.exists():
        resp = jsonify({"content": "No log file yet — run the script first.", "sections": {}})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    raw = request.args.get("lines", "500")
    if str(raw).strip().lower() == "all":
        n = 50000
    else:
        try:
            n = max(1, min(50000, int(raw)))
        except (TypeError, ValueError):
            n = 500
    tail = _read_tail_lines(p, n)
    resp = jsonify({
        "content": _format_log_text_for_display("".join(tail)),
        "sections": _log_sections_found(p),
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/logs/section")
def api_logs_section():
    kind = str(request.args.get("kind") or "").strip()
    if kind not in _LOG_SECTION_RES:
        return jsonify({"ok": False, "error": "Unknown log section."}), 400
    p = log_path()
    if not p.exists():
        return jsonify({"ok": False, "found": False, "content": ""})
    with open(p, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    found, text = _extract_log_section(all_lines, kind)
    resp = jsonify({"ok": True, "found": found,
                    "content": _format_log_text_for_display(text) if found else ""})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/logs/deleted")
def api_logs_deleted():
    try:
        raw_limit = request.args.get("limit")
        limit = None if raw_limit in (None, "", "all") else max(1, min(20000, int(raw_limit)))
    except (TypeError, ValueError):
        limit = 100
    entries = deleted_entries(limit)
    deleted = deleted_stats()
    # Newest first for display; marked-for-deletion entries ride along so the history
    # modal can pin them on top.
    entries = list(reversed(entries))
    marked = _pool_marked_entries()
    resp = jsonify({
        "count": deleted["count"],
        "reclaimed_bytes": deleted["reclaimed_bytes"],
        "reclaimed_label": deleted["reclaimed_label"],
        "entries": entries,
        "marked": marked,
        "marked_count": len(marked),
    })
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/logs/deleted/clear", methods=["POST"])
def api_logs_deleted_clear():
    blocked = _filesystem_write_block_response()
    if blocked:
        return blocked
    if _run_active:
        return _run_active_response(
            count=deleted_count(),
            reclaimed_bytes=deleted_stats()["reclaimed_bytes"],
            reclaimed_label=deleted_stats()["reclaimed_label"])
    p = deleted_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        resp = jsonify({"ok": True, "message": "Deleted history erased.", "count": 0, "reclaimed_bytes": 0, "reclaimed_label": "0.0 GB", "entries": []})
    except OSError as e:
        deleted = deleted_stats()
        resp = jsonify({"ok": False, "message": f"Could not erase deleted.log: {e}", "count": deleted["count"], "reclaimed_bytes": deleted["reclaimed_bytes"], "reclaimed_label": deleted["reclaimed_label"]})
        resp.status_code = 500
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/api/logs/archived")
def api_logs_archived():
    d = logs_dir()
    if not d.exists():
        return jsonify({"files": []})
    try:
        files = sorted(
            [f.name for f in d.iterdir() if f.suffix == ".log"],
            reverse=True
        )
    except OSError:
        files = []
    return jsonify({"files": files[:50]})

@app.route("/api/logs/archived/<filename>")
def api_logs_archived_file(filename):
    d = logs_dir()
    try:
        return send_from_directory(d, filename, as_attachment=False)
    except Exception:
        return jsonify({"error": "File not found"}), 404


def _archived_logs_state() -> dict:
    """Count + total size of archived run logs, for the Clear-logs control."""
    d = logs_dir()
    count = 0
    total = 0
    try:
        files = list(d.glob("*.log")) if d.is_dir() else []
    except OSError:
        files = []
    for f in files:
        try:
            total += f.stat().st_size
            count += 1
        except OSError:
            continue
    if total >= 1024 * 1024:
        size_label = f"{total / (1024 * 1024):.1f} MB"
    elif total >= 1024:
        size_label = f"{total / 1024:.0f} KB"
    else:
        size_label = f"{total} B"
    return {
        "ok": True,
        "count": count,
        "empty": count == 0,
        "label": ("Archived log directory is empty."
                  if count == 0
                  else f"{count} archived run log{'s' if count != 1 else ''} · {size_label}"),
    }


@app.route("/api/logs/archived/status")
def api_logs_archived_status():
    resp = jsonify(_archived_logs_state())
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/logs/archived/clear", methods=["POST"])
def api_logs_archived_clear():
    """Delete every archived run log in logs/ (keeps the folder, lastrun.log,
    and the deletion history)."""
    blocked = _filesystem_write_block_response(_archived_logs_state())
    if blocked:
        return blocked
    if _run_active:
        return _run_active_response(status=_archived_logs_state())
    d = logs_dir()
    removed = 0
    errors = []
    if d.is_dir():
        for f in d.glob("*.log"):
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                errors.append(f"{f.name}: {e}")
    if errors:
        resp = jsonify({"ok": False,
                        "message": "Some logs could not be removed: " + "; ".join(errors),
                        "status": _archived_logs_state()})
        resp.status_code = 500
    else:
        resp = jsonify({"ok": True,
                        "message": f"Cleared {removed} archived run log{'s' if removed != 1 else ''}.",
                        "status": _archived_logs_state()})
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

# ── Scheduler ─────────────────────────────────────────────────────────────────

def _deletion_limits_exceeded(cfg: dict, disk: dict | None, library_gb) -> bool:
    """True if any deletion trigger is currently breached, shared by the automatic cleanup
    tick and the manual Simulate/Cleanup launch path so both decide the same way.

    Mirrors engine.py's trigger formula: over_limit (used ≥ total − Headroom),
    redline_hit (free ≤ Redline), library_cap_hit (library size > Library Size Cap).
    Disk figures are live; the library size is the last value the engine wrote to
    the store (kept fresh by the Summary after every Cleanup, on save, on startup, and
    on paused/idle ticks).

    ONLY an optimization to avoid spinning up a run that would delete nothing (e.g. the
    library already shrank back under the cap). It never authorizes a deletion — the
    engine independently recomputes all of this from fresh on-disk sizes first. So
    whenever a value can't be read cleanly, or a cap is set but its size is unknown, this
    returns True (don't skip; let the engine be the authority)."""
    disk = disk or {}
    try:
        used_gb  = float(disk.get("used_gb"))
        total_gb = float(disk.get("total_gb"))
        free_gb  = float(disk.get("free_gb"))
    except (TypeError, ValueError):
        return True  # can't read disk → don't skip

    headroom_gb, ok = _coerce_float(cfg.get("HEADROOM_GB"))
    if not ok or headroom_gb is None:
        return True  # malformed headroom → don't skip
    over_limit = used_gb >= (total_gb - headroom_gb)

    redline_hit = False
    if cfg.get("REDLINE_GB") is not None:
        redline_gb, ok = _coerce_float(cfg.get("REDLINE_GB"))
        if ok and redline_gb is not None:
            redline_hit = free_gb <= redline_gb

    library_cap_hit = False
    cap_value = cfg.get("MAX_LIBRARY_GB")
    if cap_value is not None:
        cap_gb, ok = _coerce_float(cap_value)
        if ok and cap_gb is not None and cap_gb > 0:
            if library_gb is None:
                return True  # cap active but size unknown → let the engine check
            try:
                library_cap_hit = float(library_gb) > cap_gb
            except (TypeError, ValueError):
                return True

    return over_limit or redline_hit or library_cap_hit


def _redline_breached_now(cfg: dict) -> bool:
    """Best-effort 'the Redline floor is breached RIGHT NOW while Automatic
    Cleanup is armed', from a plain filesystem read — no engine, no library
    walk. Used to keep the emergency path ahead of tick housekeeping and as
    the launch fallback when the Summary precheck fails. Unreadable free space
    reads as not-breached here; the main tick flow owns the fail-open policy."""
    try:
        redline = cfg.get("REDLINE_GB")
        if redline is None or not _is_cleanup_mode(cfg.get("RUN_MODE")):
            return False
        return float((disk_stats() or {}).get("free_gb")) <= float(redline)
    except (TypeError, ValueError):
        return False


_tick_lock = threading.Lock()


def _scheduled_tick():
    """Entry point for BOTH timers. The 15-minute interval job and the daily-slot
    one-shot are SEPARATE APScheduler jobs, so max_instances=1 does not stop them
    firing concurrently (an interval tick landing in the same second as the slot
    shot) — two overlapped ticks could double-launch the daily work or clobber
    the maintenance latch's stamp. One non-blocking lock serializes them: the
    loser drops its tick, because the winner IS this tick's work."""
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        _scheduled_tick_body()
    finally:
        _tick_lock.release()


def _scheduled_tick_body():
    """The single background clock's callback — fired by BOTH the 15-minute
    interval job and the one-shot daily-slot job (which lands exactly at the
    configured run time; see _arm_daily_slot_job). Every guard below applies
    identically whichever timer fired.

    In Automatic Cleanup it launches an automatic cleanup; otherwise (or when Automatic Cleanup is on but
    connections/thresholds aren't currently safe to delete) it runs a quiet
    Summary/debug_info refresh so dashboard disk/library numbers stay fresh.
    run_script()/run_summary() each pause this clock while working and restart it from
    zero when done, so the guard below is just belt-and-suspenders against an overlap."""
    if _run_active or _summary_active:
        return

    # Catch-all for a store that changed outside a run/Summary completion
    # (a manual wipe, a reset, or a snapshot ageing past the scan window).
    _sync_scheduler_mode_with_db()
    cfg = load_config()
    # Keep the precise daily-slot shot aimed at the NEXT slot (idempotent; the
    # slot-fired tick itself re-aims at tomorrow's). Done before the early
    # returns so a quiet tick can never leave the one-shot unarmed.
    _arm_daily_slot_job(cfg)
    # Fully paused: no daily Simulate, no reconcile retries, no tick-driven
    # notifications, but the dashboard's storage numbers still refresh via
    # the quiet Summary each tick. The UI keeps saying "Scheduler paused"
    # (api_status withholds next_tick, so no countdown surfaces), and leaving
    # this mode resumes the full cadence without any re-arming. Manual
    # Dashboard runs are unaffected.
    if cfg.get("RUN_MODE") == "off":
        if _has_monitored_dirs(cfg):
            run_summary()
        return
    # A reconcile held because a needed server was down: retry it now that the tick
    # is here — if the connection recovered (or the server was disabled) it fires and
    # IS this tick's work; otherwise it stays held for the next tick. EXCEPT under
    # a live Redline breach: the emergency outranks housekeeping (a reconcile
    # consuming this tick would defer the deletion pass ~15+ minutes), so the
    # retry waits a tick and the cleanup branch below runs now. That deferral is
    # capped: a breach the cleanup can't clear (nothing left that's deletable)
    # would otherwise hold the user's saved filtering/scoring change forever —
    # and that change is often exactly what decides what may be deleted.
    global _reconcile_deferred_ticks
    if _reconcile_held and _redline_breached_now(cfg) \
            and _reconcile_deferred_ticks < _RECONCILE_DEFER_TICKS:
        _reconcile_deferred_ticks += 1
    elif _retry_held_reconcile(cfg):
        _reconcile_deferred_ticks = 0
        return
    else:
        _reconcile_deferred_ticks = 0
    # No monitored library paths (onboarding, or the user removed them all): there
    # is nothing to scan, size, or clean up, so the scheduler stays truly idle —
    # no Summary, no maintenance Simulate. The dashboard shows "Scheduler paused"
    # and dashes the storage numbers. Adding a path resumes the ticks.
    if not _has_monitored_dirs(cfg):
        return

    # Outbound alerts that ride the tick, observed BEFORE this tick's work so a
    # run launched below can't race them: "a space check marked more movies"
    # (state left by the previous tick's maintenance; completed runs re-baseline
    # their own marks) and the approaching-Redline low-space warning. Both are
    # once-per-change, never once-per-tick.
    _check_marked_change_notification()
    _check_low_space_notification(cfg, disk_stats())

    # Season handling no longer rides the tick: it fires inside EVERY engine run
    # (run_script's worker), so the daily maintenance run, manual Simulates
    # and Cleanups all carry it — same gesture as the movies.

    if _is_cleanup_mode(cfg.get("RUN_MODE")):
        # Only launch a deletion pass when it's actually safe. If connections aren't
        # ready, fall back to a Summary so stats still refresh. Otherwise refresh storage
        # first, then decide whether a cleanup run is needed.
        if not _refresh_connection_health_cache(cfg, probe=True).get("critical_ok", True):
            run_summary()
            return
        # defer_reconcile + the finally below: a config-save reconcile queued
        # behind this refresh would otherwise launch the instant it finishes,
        # take _summary_active, and get the deletion pass three lines down
        # refused — this tick's emergency lost to a queue rebuild, silently, with
        # the next chance 15 minutes later while the disk kept filling. The run
        # goes first; the reconcile fires on the way out of whichever branch
        # decided not to launch one.
        refresh_ok, refresh_msg, fresh_stats = run_summary_sync(defer_reconcile=True)
        try:
            _tick_cleanup_branch(cfg, refresh_ok, refresh_msg, fresh_stats)
        finally:
            _maybe_launch_queued_reconcile()
        return

    # Paused (or any non-deleting mode). Once a day, after the configured daily
    # run time, run the maintenance Simulate; every other tick is the quiet
    # Summary (dashboard disk/library numbers stay fresh without touching
    # lastrun.log/progress).
    if not _maybe_run_daily_maintenance_sim(cfg):
        run_summary()


def _tick_cleanup_branch(cfg: dict, refresh_ok: bool, refresh_msg: str,
                         fresh_stats: dict) -> None:
    """The armed-Automatic-Cleanup half of a scheduled tick, after its storage
    refresh: decide whether this tick launches a deletion pass. Split out so the
    caller can hold a queued reconcile across every one of its exits."""
    if not refresh_ok:
        print(f"Scheduled cleanup precheck failed: {refresh_msg}", flush=True)
        # A failed library refresh must not silence a Redline emergency:
        # the fast path deletes from the standing queue and needs no
        # library walk (the very thing that just failed), so a breached
        # floor launches anyway; the engine stays the authority on what
        # actually gets deleted. Every other trigger waits for a tick
        # whose precheck succeeds.
        if _redline_breached_now(cfg):
            run_script(scheduled=True)
        return
    disk = cached_disk_stats(fresh_stats) or disk_stats()
    threshold_state = _space_threshold_state(cfg, disk, fresh_stats.get("library_gb"))
    if not threshold_state.get("ok_for_cleanup"):
        # Thresholds safe when Automatic Cleanup was armed can stop being safe later; e.g. files
        # copied in push the cap past the safety floor. Silently skipping every tick
        # left Automatic Cleanup armed with nothing running and no explanation: pause with the
        # reason, like every other forced pause. Re-arming takes the two-click confirm.
        fresh_cfg = load_config()   # fresh: don't clobber a save that landed mid-summary
        if _is_cleanup_mode(fresh_cfg.get("RUN_MODE")):
            fresh_cfg["RUN_MODE"] = "paused"
            fresh_cfg["_RUN_MODE_AUTOPAUSE_REASON"] = (threshold_state.get("cleanup_tooltip")
                                                       or "Space Thresholds are no longer safe.")
            if save_config(fresh_cfg):
                _restart_schedule_clock()
                print("Scheduled tick: Space Thresholds unsafe — Automatic Cleanup paused "
                      f"({fresh_cfg['_RUN_MODE_AUTOPAUSE_REASON']})", flush=True)
        return
    # Nothing to delete? Don't launch a Cleanup. The Summary precheck above already
    # refreshed both filesystem capacity and media library size.
    if not _deletion_limits_exceeded(cfg, disk, fresh_stats.get("library_gb")):
        # Within all limits; nothing to delete, but the daily cadence still
        # owes its maintenance Simulate (plan refresh, the 48h scan clock,
        # and the daily-summary notification), exactly like Monitor Only.
        # Without this, an armed-but-quiet library would never see a daily
        # run of any kind.
        _maybe_run_daily_maintenance_sim(cfg)
        return
    # Daily-only breach (headroom/cap, no redline) with today's window already
    # used, or before the configured run time: the engine would do its full
    # startup — connection checks, IMDb check, another library walk; just to
    # log "waiting until tomorrow". Skip the launch; up to ~96 pointless engine
    # spins a day otherwise while marks wait out the delay. Redline breaches
    # always launch (they ignore the window), and any doubt fails toward
    # launching — the engine stays the authority.
    try:
        _free_gb = float((disk or {}).get("free_gb"))
        _redline = cfg.get("REDLINE_GB")
        _redline_hit = _redline is not None and _free_gb <= float(_redline)
    except (TypeError, ValueError):
        # Unreadable free space: fail toward launching ONLY when a Redline
        # is actually armed; it's the sole any-tick trigger. Without one,
        # treating "unreadable" as a hit would bypass the daily window /
        # run-time gate and spin the engine on every tick.
        _redline_hit = cfg.get("REDLINE_GB") is not None
    if not _redline_hit and (_headroom_window_used_today()
                             or time.strftime("%H:%M") < _daily_run_time(cfg)):
        return
    # run_script() owns the lock and rejects overlaps.
    run_script(scheduled=True)


def _maybe_run_daily_maintenance_sim(cfg: dict) -> bool:
    """Launch the once-a-day maintenance Simulate (a full scan, no deletions) if
    it is owed — keeping the plan and the "full scan within 48h" clock current,
    and producing the scheduled daily-summary notification. Runs only when
    connections are healthy, so a down API just leaves the quiet Summary to the
    caller. Returns True when a Simulate was launched."""
    global _last_maintenance_scan_epoch
    run_epoch = _todays_daily_run_epoch(cfg)
    if not (_daily_maintenance_scan_due(cfg) and _last_maintenance_scan_epoch != run_epoch):
        return False
    if not _refresh_connection_health_cache(cfg, probe=True).get("critical_ok", False):
        return False
    # Stamp the attempt BEFORE launching: a successful Simulate refreshes the
    # snapshot (clearing _daily_maintenance_scan_due on its own), but one that
    # fails past the probe would otherwise respin every tick; this holds it to
    # one attempt per daily window (a new day, a run-time change, a restart, or
    # a manual Simulate re-arms it).
    _last_maintenance_scan_epoch = run_epoch
    print("Scheduled tick: running the daily maintenance Simulate to keep the "
          "deletion plan current.", flush=True)
    started, _msg = run_script(mode_override="debug_sim", scheduled=True)
    if not started:
        # Lost the launch race (a user-started run took the lock between this
        # tick's guard and here); nothing ran, so give the attempt back
        # instead of burning today's slot on a run that never happened.
        _last_maintenance_scan_epoch = None
    return started


def burn_todays_maintenance_slot_on_startup() -> None:
    """A restart must not hand the scheduler an immediate daily maintenance
    Simulate: when today's run time is already behind the clock, consume the
    latch for today's slot so the next scan is tomorrow's — the maintenance
    mirror of the cleanup window's startup burn. A run time still AHEAD today
    keeps the latch armed and fires at its normal slot, and moving the run time
    re-arms it as usual (a new run epoch)."""
    global _last_maintenance_scan_epoch
    try:
        run_epoch = _todays_daily_run_epoch(load_config())
        if run_epoch is not None and time.time() >= run_epoch:
            _last_maintenance_scan_epoch = run_epoch
            print("Startup safety: today's daily maintenance scan slot marked used "
                  "(the daily run time has already passed).", flush=True)
    except Exception:
        pass

def validate_store_on_startup() -> None:
    """PRESERVE the store across a restart — an unchanged, recent plan stays
    usable with no re-scan. Nothing is invalidated here; whether the saved plan
    can still be trusted is decided LIVE:
      • monitored-path or scoring/threshold changes → the plan's stamp no longer
        matches (_pending_plan_current / _simulate_evidence), so Cleanup + arming
        ghost until a fresh Simulate;
      • a full scan older than two days → _full_scan_overdue() ghosts them the
        same way (a daily scan — the armed Cleanup, or the daily maintenance
        Simulate — normally refreshes it);
      • vanished files / newly-protected movies → the 15-minute upkeep drops those
        marks, and every deletion re-verifies protection fresh.
    deleted.log and the logs/ archive are kept, and the progress panel + last-run
    log carry over."""
    if not _rebuild_store_if_damaged(reason="startup"):
        print("Startup: cache store preserved — its saved plan stays usable unless the "
              "config changed or its last full scan is over two days old (then run Simulate).",
              flush=True)


def _rebuild_store_if_damaged(*, reason: str) -> bool:
    """Discard the store when it is no longer a readable database, returning
    True if it was rebuilt.

    The store is a CACHE — every byte in it is reproducible by a Simulate — so a
    damaged one is worth nothing and costs everything: the engine opens it early
    in every run and dies on a malformed page with an unhandled DatabaseError,
    which is a run that fails identically forever with no way out from the UI.
    (deleted.log, the run logs and config.json live outside the store and are
    untouched.) Wiping it lands the app in exactly the fresh-install state the
    mode lifecycle already handles: it rests at Paused and asks for a Simulate,
    which rebuilds. Checked at startup and before every run launch — 4 ms."""
    try:
        p = db_path()
        if db.store_is_readable(p):
            return False
        db.reset_store(p)
        _drop_store_caches()
        # The once-per-day cleanup window lived in the store, so the rebuild
        # just handed today's slot back; the same free catch-up run a restart
        # would have granted. Burn it again for the same reason.
        burn_daily_window_on_startup(reason=f"store rebuilt: {reason}")
        print(f"WARNING: the cache store was unreadable ({reason}) and has been "
              "rebuilt empty — run Simulate to restore the library snapshot and "
              "deletion plan. Deletion history and run logs are unaffected.",
              flush=True)
        return True
    except Exception as e:
        print(f"WARNING: could not check the cache store ({reason}): {e}", flush=True)
        return False


# Always start safe: preserve the store but re-validate its plan (locking Cleanup +
# arming if it went stale or the library changed), adopt the configured time zone
# before anything reads the clock, never resume a saved Automatic Cleanup after a restart,
# and burn today's daily-run window so a restart can't grant an immediate run. A
# Library Size Cap is left armed (see the note by force_paused_run_mode_on_startup).
validate_store_on_startup()
_apply_configured_time_zone()
force_paused_run_mode_on_startup()
burn_daily_window_on_startup()
burn_todays_maintenance_slot_on_startup()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    _scheduled_tick, "interval", minutes=SCHEDULE_INTERVAL_MINUTES,
    id="engine", max_instances=1,
    next_run_time=datetime.now() + timedelta(minutes=SCHEDULE_INTERVAL_MINUTES),
    # APScheduler's default misfire grace is 1 second; a busy executor would
    # silently skip the tick entirely; five minutes late is still a valid tick.
    misfire_grace_time=300,
)
scheduler.start()
# Precise shot at the next daily slot (the interval tick alone would fire the
# daily work up to ~15 minutes late).
_arm_daily_slot_job()
_kick_startup_health_check_and_summary(load_config())

# ── Entry point ───────────────────────────────────────────────────────────────

SERVICE_PORT_DEFAULT = 7474


def service_port() -> int:
    """The port to listen on: MEDIAREDUCER_PORT, else 7474.

    Under Docker the host side is remapped instead (-p 8080:7474) and this stays
    at the default; it exists for running outside a container, where 7474 may
    already be taken and there is nothing else to move.

    A bad value stops startup rather than falling back. Quietly listening on
    7474 after being asked for 9000 is the kind of thing you debug from the
    other end, wondering why nothing answers.
    """
    raw = str(os.environ.get("MEDIAREDUCER_PORT") or "").strip()
    if not raw:
        return SERVICE_PORT_DEFAULT
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"MEDIAREDUCER_PORT must be a number (got {raw!r}).")
    if not 1 <= port <= 65535:
        raise SystemExit(f"MEDIAREDUCER_PORT must be between 1 and 65535 (got {port}).")
    return port


if __name__ == "__main__":
    # Initialize config on first boot
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Through the atomic writer, not a bare copy: a kill mid-copy left a
        # truncated config.json that EXISTS, so the seed never re-ran and
        # every later boot sat in the invalid-JSON lockout.
        _atomic_write_json(CONFIG_PATH, json.loads(Path(DEFAULT_CFG_PATH).read_text(
            encoding="utf-8")), indent=2)
        print(f"Created default config at {CONFIG_PATH}")

    # Registered on the main thread (signal.signal requires it) and only when run
    # directly, never on import (tests). Lets a `docker stop` finish an in-flight
    # deletion cleanly instead of SIGKILLing the engine with the container.
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _graceful_shutdown)
        except (ValueError, OSError):
            pass

    _port = service_port()
    if _port != SERVICE_PORT_DEFAULT:
        print(f"Listening on port {_port} (MEDIAREDUCER_PORT).", flush=True)
    app.run(host="0.0.0.0", port=_port, debug=False, threaded=True)
