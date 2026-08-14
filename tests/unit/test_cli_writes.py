"""What the CLI sends when you change something, and what it tells you afterwards.

POST /api/config REPLACES the config file with the posted body. That one fact
is the reason `config set` reads the whole config back before it writes: a save
that posted only the changed key would silently erase every other setting —
thresholds, connections, schedule — and the CLI would print "Saved." over the
top of it. Nothing in the suite checked the shape of that body, so the round
trip is pinned here, key by key, including the three exclusions that exist for
their own reasons:

  • OUTPUT_DIR and the server-derived paths are regenerated on load, so posting
    them back is at best clutter;
  • underscore keys are internal state, and one of them is load-bearing in the
    wrong direction — posting a stale _RUN_MODE_AUTOPAUSE_REASON back is how an
    auto-pause notice would survive the very save that fixed its cause;
  • an enabled Radarr section posts as "auto" so the server re-resolves it.

The Filtering & Scoring payload is the mirror image and must not be unified
with it: /api/score-config validates the whole score block, so every score key
goes even when one changed, and two underscore keys ride along on purpose.

Also here: the commands that had never run at all. `connections autodetect`
writes API keys it discovers (and --dry-run must write nothing, which is a
safety promise nothing was checking), `report` writes a file to disk, and
`config fix` rewrites hand-edited values.

No network: api() is stubbed and every request body is captured.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import cli  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


BASE = "http://127.0.0.1:7575"
SENT = []

# A config with one of everything the round trip has to reason about.
SAVED_CONFIG = {
    "HEADROOM_GB": 500,
    "RUN_MODE": "headroom",
    "MONITOR_DIRS": ["/movies"],
    "TAUTULLI_API_KEY": "abc123",
    "GRACE_PERIOD_DAYS": 30,
    "SCORE_BALANCE": 50,
    "MAX_IMDB_RATING": 6.5,
    "NEAR_TIE_PTS": 2.0,
    "MAX_STALENESS_MONTHS": 12,
    "SKIP_UNPLAYED_MOVIES": False,
    "PROTECT_JELLYFIN_FAVORITES": True,
    "RADARR_OVERSEERR_SECTION_ID": 3,
    "OUTPUT_DIR": "/config",
    "CHECK_PATH": "/movies",
    "TAUTULLI_APPDATA": "/appdata/tautulli",
    "RADARR_APPDATA": "/appdata/radarr",
    "_RUN_MODE_AUTOPAUSE_REASON": "a server was unreachable",
    "_MAX_IMDB_RATING_LAST": 7.0,
    "_NEAR_TIE_PTS_LAST": 3.0,
}


def server(reply=None, config=None):
    """api() stub: GET /api/config serves a config, POSTs are recorded."""
    cfg = dict(SAVED_CONFIG if config is None else config)

    def _api(method, path, base, *, body=None, timeout=60):
        SENT.append((method, path, body))
        if method == "GET" and path == "/api/config":
            return 200, cfg
        return 200, dict(reply or {"ok": True})
    return _api


def drive(fn, *a, **kw):
    del SENT[:]
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, buf.getvalue(), err.getvalue()


def posted(path):
    for method, p, body in SENT:
        if method == "POST" and p == path:
            return body
    return None


def args(**kw):
    base = {"json": False, "timeout": 60, "yes": True}
    base.update(kw)
    return SimpleNamespace(**base)


# ── config set posts the WHOLE config, not the delta ───────────────────────
cli.api = server()
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=750"]), BASE)
body = posted("/api/config")
check("a config save succeeds", rc == 0 and "Saved." in o, (rc, o, e))
check("the changed key is applied", (body or {}).get("HEADROOM_GB") == 750, body)
check("...and every untouched key rides along, because the POST replaces the file",
      all((body or {}).get(k) == SAVED_CONFIG[k]
          for k in ("RUN_MODE", "MONITOR_DIRS", "TAUTULLI_API_KEY")), body)

check("OUTPUT_DIR is not posted back — the server owns it",
      "OUTPUT_DIR" not in (body or {}), body)
check("server-derived paths are not posted back",
      not any(k in (body or {}) for k in cli._DERIVED_KEYS), body)
# The auto-pause reason is regenerated per run. Sending the old one back would
# re-assert a warning the user may have just fixed.
check("no underscore key is posted back",
      not any(k.startswith("_") for k in (body or {})), body)
check("an enabled Radarr section posts as auto, for the server to re-resolve",
      (body or {}).get("RADARR_OVERSEERR_SECTION_ID") == "auto", body)

# Setting that key explicitly must win over the "auto" rewrite.
cli.api = server()
rc, o, e = drive(cli.cmd_config_set,
                 args(assignments=["RADARR_OVERSEERR_SECTION_ID=9"]), BASE)
check("...unless the caller set it, in which case their value stands",
      (posted("/api/config") or {}).get("RADARR_OVERSEERR_SECTION_ID") == 9,
      posted("/api/config"))

# A config with no Radarr section stays absent rather than being invented.
cli.api = server(config={k: v for k, v in SAVED_CONFIG.items()
                         if k != "RADARR_OVERSEERR_SECTION_ID"})
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=750"]), BASE)
check("a config with no Radarr section does not gain one",
      "RADARR_OVERSEERR_SECTION_ID" not in (posted("/api/config") or {}),
      posted("/api/config"))


# ── Scoring keys take the other door ───────────────────────────────────────
# /api/score-config validates the block as a whole, so a one-key change still
# sends every score key. Routing one of these through /api/config instead would
# be accepted and then ignored by the Filtering & Scoring page.
cli.api = server()
rc, o, e = drive(cli.cmd_config_set, args(assignments=["GRACE_PERIOD_DAYS=45"]), BASE)
score = posted("/api/score-config")
check("a scoring key goes to /api/score-config", score is not None, SENT)
check("...and not to /api/config", posted("/api/config") is None, SENT)
check("the changed score key is applied", (score or {}).get("GRACE_PERIOD_DAYS") == 45, score)
check("...alongside every other score key the validator needs",
      all(k in (score or {}) for k in cli._SCORE_KEYS), score)
# The opposite rule to the config payload, and deliberately so: these two carry
# the last non-zero value of sliders whose zero means "off".
check("the two remembered slider values ride along",
      (score or {}).get("_MAX_IMDB_RATING_LAST") == 7.0
      and (score or {}).get("_NEAR_TIE_PTS_LAST") == 3.0, score)
check("...but no other underscore key does",
      not [k for k in (score or {}) if k.startswith("_")
           and k not in ("_MAX_IMDB_RATING_LAST", "_NEAR_TIE_PTS_LAST")], score)

# One command, both destinations, when the assignments straddle the split.
cli.api = server()
rc, o, e = drive(cli.cmd_config_set,
                 args(assignments=["HEADROOM_GB=750", "GRACE_PERIOD_DAYS=45"]), BASE)
check("a mixed set posts to both endpoints",
      posted("/api/config") is not None and posted("/api/score-config") is not None, SENT)
check("...and reports success once per save", rc == 0 and o.count("Saved.") == 2, o)


# ── What a save reports back ───────────────────────────────────────────────
# Each of these is a side effect the server performed that the user did not
# ask for. Swallowing them means the CLI says "Saved." and nothing else while
# Automatic Cleanup has just switched itself off.
cli.api = server(reply={"ok": True, "reconcile": "started"})
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=750"]), BASE)
check("a rebuilt plan is reported, so nobody runs a needless Simulate",
      "rebuilt in place" in o, o)

cli.api = server(reply={"ok": True, "reconcile": "held_connection"})
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=750"]), BASE)
check("a held reconcile says which way to fix it", "unreachable" in o, o)

cli.api = server(reply={"ok": True, "automatic_run_mode_paused": True,
                        "automatic_run_mode_paused_reason": "Tautulli is unreachable"})
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=750"]), BASE)
check("a save that auto-paused Automatic Cleanup says so",
      "Monitor Only" in o and "Tautulli is unreachable" in o, o)

cli.api = server(reply={"ok": True, "server_software_auto_disabled": ["Plex", "Radarr"]})
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=750"]), BASE)
check("servers the save deselected are each named",
      "Plex was deselected" in o and "Radarr was deselected" in o, o)

# A rejected save must exit non-zero and put the reason where a script looks.
cli.api = server(reply={"ok": False, "error": "HEADROOM_GB must be a number",
                        "invalid_config": [{"key": "HEADROOM_GB",
                                            "message": "not a number"}]})
rc, o, e = drive(cli.cmd_config_set, args(assignments=["HEADROOM_GB=nope"]), BASE)
check("a rejected save exits 1", rc == 1, rc)
check("...with the error on stderr, not stdout",
      "HEADROOM_GB must be a number" in e and "Saved." not in o, (o, e))
check("...and each invalid key listed", "HEADROOM_GB: not a number" in e, e)

# --json is the scripting contract: the exit code still carries the verdict.
cli.api = server(reply={"ok": False, "error": "nope"})
rc, o, e = drive(cli.cmd_config_set, args(json=True, assignments=["HEADROOM_GB=1"]), BASE)
check("--json still exits 1 on a rejected save", rc == 1, rc)
check("...and prints a parseable object", json.loads(o).get("status") == 200, o)


# ── connections autodetect: the command that saves what it finds ───────────
def detector(values, save_reply=None):
    def _api(method, path, base, *, body=None, timeout=60):
        SENT.append((method, path, body))
        if path == "/api/connections/autodetect":
            return 200, {"ok": True, "values": values}
        if method == "GET" and path == "/api/config":
            return 200, dict(SAVED_CONFIG)
        return 200, dict(save_reply or {"ok": True})
    return _api


cli.api = detector({"TAUTULLI_API_KEY": "found-key", "RADARR_API_KEY": ""})
rc, o, e = drive(cli.cmd_connections_autodetect, args(dry_run=False), BASE)
saved = posted("/api/config")
check("autodetect saves what it found", rc == 0 and saved is not None, (rc, SENT))
check("...the detected value", (saved or {}).get("TAUTULLI_API_KEY") == "found-key", saved)
check("...without wiping the rest of the config",
      (saved or {}).get("RUN_MODE") == "headroom", saved)
check("...and reports the count", "Saved 1 detected key(s)." in o, o)
check("a blank value is not a detection", "RADARR_API_KEY: —" in o, o)
check("...and is not saved over an existing key",
      "RADARR_API_KEY" not in (saved or {}), saved)

# --dry-run is the whole point of the flag: look, change nothing.
cli.api = detector({"TAUTULLI_API_KEY": "found-key"})
rc, o, e = drive(cli.cmd_connections_autodetect, args(dry_run=True), BASE)
check("--dry-run saves nothing at all", posted("/api/config") is None, SENT)
check("...says so, and succeeds", rc == 0 and "--dry-run: nothing saved" in o, (rc, o))

cli.api = detector({"TAUTULLI_API_KEY": "", "RADARR_API_KEY": "   "})
rc, o, e = drive(cli.cmd_connections_autodetect, args(dry_run=False), BASE)
check("detecting nothing exits 1 rather than reporting an empty success", rc == 1, rc)
check("...and points at the mounts", "appdata mounts" in o, o)
check("...having saved nothing", posted("/api/config") is None, SENT)

# A detection whose save is then rejected must not claim it saved.
cli.api = detector({"TAUTULLI_API_KEY": "found-key"},
                   save_reply={"ok": False, "error": "config locked"})
rc, o, e = drive(cli.cmd_connections_autodetect, args(dry_run=False), BASE)
check("a rejected save after a detection exits 1", rc == 1, rc)
check("...and does not print Saved", "Saved 1" not in o, o)


# ── config fix ─────────────────────────────────────────────────────────────
cli.api = server(reply={"ok": True})
rc, o, e = drive(cli.cmd_config_fix, args(), BASE)
check("config fix reports what it did", rc == 0 and "reset to defaults" in o, (rc, o))

cli.api = server(reply={"ok": False, "error": "nothing to fix"})
rc, o, e = drive(cli.cmd_config_fix, args(), BASE)
check("a failed fix exits 1 with the reason on stderr",
      rc == 1 and "nothing to fix" in e, (rc, e))

cli.api = server(reply={"ok": True})
rc, o, e = drive(cli.cmd_config_fix, args(json=True), BASE)
check("config fix --json exits 0 and prints an object",
      rc == 0 and json.loads(o).get("ok") is True, (rc, o))


# ── report: the command that writes a file ─────────────────────────────────
def reporter(payload):
    def _api(method, path, base, *, body=None, timeout=60):
        SENT.append((method, path, body))
        return 200, payload
    return _api


TMP = tempfile.mkdtemp(prefix="mediareducer-cli-report.")
dest = os.path.join(TMP, "report.txt")

cli.api = reporter({"ok": True, "text": "DIAGNOSTIC BODY", "filename": "server-name.txt"})
rc, o, e = drive(cli.cmd_report, args(output=dest), BASE)
check("report -o writes the file it names", rc == 0 and os.path.exists(dest), (rc, o, e))
check("...with the report in it", Path(dest).read_text() == "DIAGNOSTIC BODY", "")
check("...and says where it went", dest in o, o)

# '-' is the pipe case: the body on stdout, no file anywhere.
before = set(os.listdir(TMP))
cli.api = reporter({"ok": True, "text": "DIAGNOSTIC BODY", "filename": "server-name.txt"})
rc, o, e = drive(cli.cmd_report, args(output="-"), BASE)
check("report -o - prints the report instead of writing it",
      rc == 0 and "DIAGNOSTIC BODY" in o, (rc, o))
check("...and writes no file", set(os.listdir(TMP)) == before, os.listdir(TMP))

# A report with no text is a failure, and must not leave a file behind.
cli.api = reporter({"ok": False, "error": "debug mode is off"})
rc, o, e = drive(cli.cmd_report, args(output=os.path.join(TMP, "never.txt")), BASE)
check("a report the server could not build exits 1", rc == 1, rc)
check("...with the reason on stderr", "debug mode is off" in e, e)
check("...and writes nothing", not os.path.exists(os.path.join(TMP, "never.txt")), "")

# With no -o, the server names the file and it lands in the working directory.
# That is the documented default and the one most people hit, so it is pinned
# from inside a temp dir rather than left to litter whatever directory the
# suite happens to run in.
cwd = os.getcwd()
os.chdir(TMP)
try:
    cli.api = reporter({"ok": True, "text": "BODY", "filename": "server-name.txt"})
    rc, o, e = drive(cli.cmd_report, args(output=None), BASE)
    landed = os.path.exists(os.path.join(TMP, "server-name.txt"))
finally:
    os.chdir(cwd)
check("report with no -o uses the server's filename", rc == 0 and landed, (rc, o, e))

# No -o and no filename from the server leaves stdout as the only destination.
before = set(os.listdir(TMP))
cli.api = reporter({"ok": True, "text": "BODY"})
rc, o, e = drive(cli.cmd_report, args(output=None), BASE)
check("with nowhere to write it, the report goes to stdout",
      rc == 0 and "BODY" in o and set(os.listdir(TMP)) == before, (rc, o))

cli.api = reporter({"ok": True, "text": "BODY"})
rc, o, e = drive(cli.cmd_report, args(json=True, output=None), BASE)
check("report --json exits 0 and prints the payload",
      rc == 0 and json.loads(o).get("text") == "BODY", (rc, o))

for f in os.listdir(TMP):
    os.unlink(os.path.join(TMP, f))
os.rmdir(TMP)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
