"""Every control state on the Configuration page is a server verdict about the
SAVED config, delivered identically on all three channels.

The page holds no availability rules of its own. It applies four verdicts —
the two run-mode radios and the two notification buttons — and gets them from
the page render, the /api/status poll, and the save response. Nothing on the
page recomputes them, and nothing consults the form: an unsaved edit describes
a configuration that does not exist yet, and a page that gated itself on one
spent its life describing something that was not running.

So there are two properties, and this file is both of them:

  • the three channels agree, always. A verdict added to one and forgotten on
    another is the drift that produced a radio which ghosted at render and
    un-ghosted one poll later.
  • the verdicts follow the SAVED file. Writing config.json moves them;
    posting a candidate to any endpoint does not, because no such endpoint
    exists any more.
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
import notify  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


LIB = OUT / "library" / "movies"
LIB.mkdir(parents=True, exist_ok=True)
SAVED = {"OUTPUT_DIR": str(OUT), "MONITOR_DIRS": [str(LIB)], "RUN_MODE": "off",
         "HEADROOM_GB": 50, "REDLINE_GB": None, "MAX_LIBRARY_GB": None,
         "MAX_HEADROOM_PCT": 15, "USE_JELLYFIN": True,
         "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k",
         "MOVIE_CLEANUP_ENABLED": True,
         "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz"}
DISK = {"used_gb": 500.0, "total_gb": 1000.0, "free_gb": 500.0, "pct_used": 50.0}
A.disk_stats = lambda: dict(DISK)
A.library_stats = lambda: {"library_gb": 100.0}
HEALTHY = {"critical_ok": True, "required_tooltip": "", "jellyfin_connected": True,
           "errors": [], "warnings": [], "blocked": False}
A._refresh_connection_health_cache = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_state = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_for_ui = lambda cfg=None: dict(HEALTHY)

A.app.config["TESTING"] = True
client = A.app.test_client()
HDRS = {"X-MediaReducer": "1"}

# The four verdicts, as the helpers compute them for a given saved config.
VERDICTS = ("monitor_lock_reason", "notify_preview_reason", "notify_test_reason")


def write(**over):
    A.CONFIG_PATH.write_text(json.dumps({**SAVED, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    cfg = A.load_config()
    return {"monitor_lock_reason": A._monitor_mode_lock_reason(cfg),
            "notify_preview_reason": A._notify_preview_reason(cfg),
            "notify_test_reason": A._notify_test_reason(cfg),
            "ok_for_cleanup": A._space_threshold_state(cfg, dict(DISK),
                                                       100.0)["ok_for_cleanup"]}


def from_status():
    d = client.get("/api/status").get_json()
    out = {k: d.get(k) for k in VERDICTS}
    out["ok_for_cleanup"] = (d.get("cleanup_state") or {}).get(
        "space_thresholds", {}).get("ok_for_cleanup")
    return out


def from_render():
    """The verdicts the page is seeded with, read back out of its preamble."""
    html = client.get("/config").get_data(as_text=True)
    out = {}
    for name, var in (("monitor_lock_reason", "_monitorLockReason"),
                      ("notify_preview_reason", "_notifyPreviewReason"),
                      ("notify_test_reason", "_notifyTestReason")):
        m = re.search(rf"^let {var} = (.*);$", html, re.M)
        out[name] = json.loads(m.group(1)) if m else None
    m = re.search(r"^let _serverThresholds = (.*);$", html, re.M)
    out["ok_for_cleanup"] = json.loads(m.group(1))["ok_for_cleanup"] if m else None
    return out


# ── The three channels agree, across genuinely different states ───────────
STATES = {
    "everything configured": {},
    "no notification destination": {"NOTIFY_PUSHOVER_USER_KEY": "", "NOTIFY_PUSHOVER_TOKEN": ""},
    "a destination configured": {"NOTIFY_PUSHOVER_USER_KEY": "u", "NOTIFY_PUSHOVER_TOKEN": "t"},
    "no space trigger armed": {"HEADROOM_GB": 0, "REDLINE_GB": None, "MAX_LIBRARY_GB": None},
    "cleanup switches off": {"MOVIE_CLEANUP_ENABLED": False, "TV_CLEANUP_ENABLED": False},
    "no monitored path": {"MONITOR_DIRS": []},
}
for label, over in STATES.items():
    want = write(**over)
    check(f"the status poll matches the helpers ({label})", from_status() == want,
          f"{from_status()} != {want}")
    check(f"...and so does the page render ({label})", from_render() == want,
          f"{from_render()} != {want}")

# ── ...and the save response, on the config it just wrote ─────────────────
# The save is the moment a verdict is allowed to change, so it carries them
# rather than leaving the page a poll behind.
write()
post = {k: v for k, v in SAVED.items() if k != "OUTPUT_DIR"}
# The save checks that monitored paths exist under the library root, which this
# harness has no mount for; the verdicts are what is under test, not that
# validator, so save with none.
post.update({"MONITOR_DIRS": [],
             "NOTIFY_PUSHOVER_USER_KEY": "u", "NOTIFY_PUSHOVER_TOKEN": "t"})
r = client.post("/api/config", json=post, headers=HDRS)
saved_body = r.get_json() or {}
check("a save succeeds", r.status_code == 200 and saved_body.get("ok"),
      (r.status_code, saved_body.get("error")))
if saved_body.get("ok"):
    cfg_after = A.load_config()
    check("the save response carries every verdict",
          all(k in saved_body for k in VERDICTS) and "space_thresholds" in saved_body,
          sorted(saved_body))
    check("...computed on what it just wrote",
          saved_body.get("notify_test_reason") == A._notify_test_reason(cfg_after)
          and saved_body.get("monitor_lock_reason") == A._monitor_mode_lock_reason(cfg_after),
          saved_body.get("notify_test_reason"))
    check("...and the very next poll says the same",
          from_status()["notify_test_reason"] == saved_body["notify_test_reason"])

# ── The verdicts follow the SAVED file, and only it ───────────────────────
with_dest = write(NOTIFY_PUSHOVER_USER_KEY="u", NOTIFY_PUSHOVER_TOKEN="t")
check("a saved destination un-ghosts the test button",
      with_dest["notify_test_reason"] == "", with_dest)
without = write(NOTIFY_PUSHOVER_USER_KEY="", NOTIFY_PUSHOVER_TOKEN="")
check("...and removing it ghosts the button again",
      without["notify_test_reason"] != "", without)
check("...with notify.py's own answer, not a copy of the rules",
      bool(without["notify_test_reason"]) is not notify.has_destinations(A.load_config()))

# The endpoints that judged UNSAVED candidates are gone; nothing on the page
# can ask about a config that was never written.
for gone in ("/api/config/validate-thresholds", "/api/notify/destinations"):
    r = client.post(gone, json={"HEADROOM_GB": 1}, headers=HDRS)
    check(f"{gone} no longer exists", r.status_code == 404, r.status_code)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
