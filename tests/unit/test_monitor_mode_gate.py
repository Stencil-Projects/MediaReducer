"""What the Configuration page renders about Monitor Only must survive its own
status poll.

The page decides twice. The server renders the radio, and then
_updateRunModeAvailability() re-decides on every poll from the page's own copy
of the rules — and that one wins. So a rule the server enforces and the page
doesn't leaves a control that lies about being available; a rule the page
enforces and the server doesn't leaves one that comes up live and goes grey a
second later, for a reason the server had already worked out and not acted on.

Monitor Only needs three things: a media server and a monitored path, an armed
space trigger, and an up-to-date library database. _scan_lock_reason words all
three. The render gated on the first and third.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout  # noqa: E402
OUT = Path(_tmpout.config())
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


LIB = OUT / "library"
(LIB / "movies").mkdir(parents=True, exist_ok=True)
BASE = {"OUTPUT_DIR": str(OUT), "MONITOR_DIRS": [str(LIB / "movies")],
        "RUN_MODE": "off", "USE_JELLYFIN": True,
        "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k"}
DISK = {"used_gb": 500.0, "total_gb": 1000.0, "free_gb": 500.0, "pct_used": 50.0}
A.disk_stats = lambda: dict(DISK)
A.library_stats = lambda: {"library_gb": 100.0}
HEALTHY = {"critical_ok": True, "required_tooltip": "", "jellyfin_connected": True,
           "errors": [], "warnings": [], "blocked": False}
A._refresh_connection_health_cache = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_state = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_for_ui = lambda cfg=None: dict(HEALTHY)
# A completed, current scan — so the database rule is satisfied and the armed-
# trigger rule is the only one left that can hold Monitor Only.
A._library_db_fresh = lambda cfg=None: True

A.app.config["TESTING"] = True
client = A.app.test_client()


def render(**cfg_extra):
    A.CONFIG_PATH.write_text(json.dumps({**BASE, **cfg_extra}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    html = client.get("/config").get_data(as_text=True)
    m = re.search(r'<input[^>]*id="mode-paused".*?>', html, re.S)
    desc = re.search(r'id="desc-paused"[^>]*>(.*?)</span>', html, re.S)
    return {
        "disabled": m is not None and "disabled" in m.group(0),
        "reason": " ".join((desc.group(1) if desc else "").split()),
    }


# ── No trigger armed: Headroom off, no Redline, no cap ────────────────────
r = render(HEADROOM_GB=0, REDLINE_GB=None, MAX_LIBRARY_GB=None)
check("with no space trigger armed, Monitor Only renders disabled", r["disabled"], r)
check("...and the render says why, rather than showing the enabled blurb",
      "Arm a space threshold" in r["reason"], r["reason"][:110])

# ── Each trigger on its own arms it ───────────────────────────────────────
# Or the check above would pass just as well against a radio that is always
# ghosted, which is a different bug wearing the same result.
NONE_ARMED = {"HEADROOM_GB": 0, "REDLINE_GB": None, "MAX_LIBRARY_GB": None}
for label, extra in (("a Headroom target", {"HEADROOM_GB": 50}),
                     ("a Redline floor", {"REDLINE_GB": 20}),
                     ("a Library Size Cap", {"MAX_LIBRARY_GB": 90})):
    r = render(**{**NONE_ARMED, **extra})
    check(f"{label} alone makes Monitor Only selectable", not r["disabled"], r)

# ── The render and _monitor_mode_lock_reason cannot disagree ──────────────
# One function owns the radio now — the render, the status poll and the page's
# own gate all serve its verdict verbatim. Pin the render to the function
# rather than to a list of rules here: a rule added to the verdict reaches the
# markup by construction, and this loop proves the construction.
for name, extra in (("nothing armed", {"HEADROOM_GB": 0, "REDLINE_GB": None, "MAX_LIBRARY_GB": None}),
                    ("headroom armed", {"HEADROOM_GB": 50, "REDLINE_GB": None, "MAX_LIBRARY_GB": None}),
                    ("no monitored path", {"HEADROOM_GB": 50, "MONITOR_DIRS": []}),
                    ("no media server", {"HEADROOM_GB": 50, "USE_JELLYFIN": False})):
    r = render(**extra)
    cfg = A.load_config()
    locked = bool(A._monitor_mode_lock_reason(cfg))
    check(f"render agrees with _monitor_mode_lock_reason ({name})",
          r["disabled"] == locked, f"rendered disabled={r['disabled']} reason={locked}")

# ── The running-mode waiver: only the scan half is waived ─────────────────
# A mode already running scheduled scans keeps its own radio live even while
# the database check would fail (its daily scan is what satisfies it). Setup
# and armed-trigger failures are never waived: a running mode with its media
# server deselected is still missing its setup.
A._library_db_fresh = lambda cfg=None: False
r = render(HEADROOM_GB=50, RUN_MODE="paused")
check("a running Monitor Only never ghosts under itself over the scan check",
      not r["disabled"], r)
r = render(HEADROOM_GB=0, REDLINE_GB=None, MAX_LIBRARY_GB=None, RUN_MODE="paused")
check("...but the waiver does not cover a missing space trigger",
      r["disabled"], r)
r = render(HEADROOM_GB=50, RUN_MODE="off")
check("...and a scheduler at rest gets no waiver at all", r["disabled"], r)
A._library_db_fresh = lambda cfg=None: True

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
