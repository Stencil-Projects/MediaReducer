"""What the read-only commands render, including the states with nothing in them.

cli_smoke drives these against a booted app and checks they exit 0, which
proves the plumbing and almost nothing about the rendering. The states that
matter most are the ones a live smoke test cannot arrange on demand: an empty
queue, a library nobody has scanned yet, a server that answered with an error,
a config key that does not exist.

Two of these renderings are load-bearing rather than cosmetic:

  • `collections` draws the checkbox that says whether a collection is
    protected from deletion. Inverting it, or reading the Plex list against the
    Jellyfin setting, would tell someone their films are safe when they are
    queued;
  • `status` maps stored run modes onto their labels, and the mapping is not
    the obvious one — "off" is Paused while "paused" is Monitor Only. Anyone
    tidying that dictionary by matching the names would have the CLI report the
    opposite of the mode in force.

The rest is the difference between an empty result and a broken one: "the queue
is empty, run a Simulate" is a state, and it must not read like a failure or
exit like one.

No network: api() is stubbed per command.
"""
import io
import json
import sys
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


def serving(**by_path):
    """api() stub: each path prefix maps to the body to return for it."""
    def _api(method, path, base, *, body=None, timeout=60):
        for prefix, reply in by_path.items():
            if path.startswith(prefix):
                return (reply[0], reply[1]) if isinstance(reply, tuple) else (200, reply)
        raise AssertionError(f"unexpected request: {method} {path}")
    return _api


def drive(fn, *a, **kw):
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, buf.getvalue(), err.getvalue()


def args(**kw):
    base = {"json": False, "timeout": 60}
    base.update(kw)
    return SimpleNamespace(**base)


# ── collections: the checkbox that means "protected from deletion" ─────────
COLLECTIONS = {
    "plex": {"enabled": True, "names": ["Marvel", "Christmas", "Criterion"]},
    "jellyfin": {"enabled": True, "names": ["Kids", "Marvel"]},
}
CONFIG = {"PROTECTED_COLLECTIONS": ["Christmas"],
          "JELLYFIN_PROTECTED_COLLECTIONS": ["Kids"]}

cli.api = serving(**{"/api/collections": COLLECTIONS, "/api/config": CONFIG})
rc, o, e = drive(cli.cmd_collections, args(), BASE)
check("collections lists both servers", rc == 0
      and "Plex collections" in o and "Jellyfin collections" in o, o)
check("a protected collection is ticked", "[x] Christmas" in o, o)
check("an unprotected one is not", "[ ] Marvel" in o and "[ ] Criterion" in o, o)
# The same name in both servers, protected in only one: reading one server's
# list against the other's setting would tick both.
check("each server is read against its OWN protected list",
      "[x] Kids" in o and o.count("[x]") == 2, o)

cli.api = serving(**{"/api/collections": {"plex": {"enabled": False, "names": ["Marvel"]},
                                          "jellyfin": {"enabled": False}},
                     "/api/config": CONFIG})
rc, o, e = drive(cli.cmd_collections, args(), BASE)
check("a disabled server is not listed", rc == 0 and "Marvel" not in o, o)
check("...and the reason is explained", "No collection-capable server" in o, o)

cli.api = serving(**{"/api/collections": {"plex": {"enabled": True, "names": [],
                                                   "error": "token rejected"}},
                     "/api/config": CONFIG})
rc, o, e = drive(cli.cmd_collections, args(), BASE)
check("an enabled server that failed shows its error where its list would be",
      rc == 0 and "token rejected" in o, o)

cli.api = serving(**{"/api/collections": {"plex": {"enabled": True, "names": []}},
                     "/api/config": CONFIG})
rc, o, e = drive(cli.cmd_collections, args(), BASE)
check("an enabled server with no collections says none found", "none found" in o, o)

# A body that is not JSON — a reverse proxy's 502 page, a truncated reply — is
# a string by the time it reaches the renderer. `(d or {}).get(...)` guards the
# empty case and not the wrong-type one, so these three used to end in an
# AttributeError traceback instead of a message.
cli.api = serving(**{"/api/collections": COLLECTIONS, "/api/config": "<html>502</html>"})
rc, o, e = drive(cli.cmd_collections, args(), BASE)
check("an unreadable config still lists the collections", rc == 0 and "Marvel" in o, o)
# Drawing every box empty would state as fact that nothing is protected.
check("...but says the checkboxes are not authoritative",
      "not authoritative" in e, e)

cli.api = serving(**{"/api/collections": "gateway error"})
rc, o, e = drive(cli.cmd_collections, args(), BASE)
check("a non-JSON reply exits 1 rather than rendering nothing", rc == 1, rc)

cli.api = serving(**{"/api/collections": COLLECTIONS})
rc, o, e = drive(cli.cmd_collections, args(json=True), BASE)
check("--json returns the raw payload without the second request",
      rc == 0 and json.loads(o).get("plex", {}).get("enabled") is True, o)


# ── status: the mode labels, which do not match their stored names ─────────
def status_of(**kw):
    d = {"run_mode": "headroom", "library_gb": 900, "marked_count": 0}
    d.update(kw)
    return d


for stored, label in (("off", "Paused"), ("paused", "Monitor Only"),
                      ("headroom", "Automatic Cleanup")):
    cli.api = serving(**{"/api/status": status_of(run_mode=stored)})
    rc, o, e = drive(cli.cmd_status, args(), BASE)
    check(f"run_mode {stored!r} renders as {label!r}", f"Mode:          {label}" in o, o)

# An unrecognised mode prints itself rather than disappearing.
cli.api = serving(**{"/api/status": status_of(run_mode="experimental")})
rc, o, e = drive(cli.cmd_status, args(), BASE)
check("an unknown mode is printed as-is", "experimental" in o, o)

# Run state has a precedence order: the more specific claim wins, so a cleanup
# in progress never reads as a plain run.
for flags, expected in (
        ({"run_cleanup": True, "run_active": True}, "running (cleanup)"),
        ({"run_debug_cleanup": True, "run_active": True}, "running (debug cleanup)"),
        ({"run_active": True}, "running"),
        ({"summary_active": True}, "summary refreshing"),
        ({}, "idle")):
    cli.api = serving(**{"/api/status": status_of(**flags)})
    rc, o, e = drive(cli.cmd_status, args(), BASE)
    check(f"run state renders as {expected!r}", f"Run state:     {expected}" in o, o)

cli.api = serving(**{"/api/status": status_of(
    disk={"free_gb": 400, "total_gb": 2000, "pct_used": 80},
    headroom_gb=500, redline_gb=100, library_cap_gb=1500)})
rc, o, e = drive(cli.cmd_status, args(), BASE)
check("storage renders free of total", "400.0 GB free of 2000.0 GB" in o, o)
check("...with the percentage used", "(80% used)" in o, o)
check("all three targets are shown when set",
      "headroom 500.0 GB" in o and "redline 100.0 GB" in o and "cap 1500.0 GB" in o, o)

# A target of 0 is off, and must say so rather than printing "0.0 GB", which
# reads like a threshold that deletes everything.
cli.api = serving(**{"/api/status": status_of(headroom_gb=0, redline_gb=0,
                                              library_cap_gb=0)})
rc, o, e = drive(cli.cmd_status, args(), BASE)
check("an unset headroom says off, not 0.0 GB", "headroom off" in o, o)
check("...and unset limits say disabled",
      "redline disabled" in o and "cap disabled" in o, o)

cli.api = serving(**{"/api/status": status_of(
    run_mode_autopause_reason="Tautulli unreachable", config_attention=True)})
rc, o, e = drive(cli.cmd_status, args(), BASE)
check("an auto-pause is surfaced with its reason",
      "Auto-paused:   Tautulli unreachable" in o, o)
check("a config needing attention points at the command that explains it",
      "NEEDS ATTENTION" in o and "connections check" in o, o)

cli.api = serving(**{"/api/status": status_of()})
rc, o, e = drive(cli.cmd_status, args(), BASE)
check("a healthy config says ok", "Config health: ok" in o, o)
check("missing optional fields render as dashes, not None",
      "Last run:      —" in o and "Next run:      —" in o and "None" not in o, o)

cli.api = serving(**{"/api/status": "<html>502</html>"})
rc, o, e = drive(cli.cmd_status, args(), BASE)
check("a non-JSON status body exits 1", rc == 1, rc)


# ── config get ─────────────────────────────────────────────────────────────
FULL = {"HEADROOM_GB": 500, "MONITOR_DIRS": ["/movies"], "SKIP_UNPLAYED_MOVIES": True,
        "REDLINE_GB": None, "CHECK_PATH": "/movies", "TAUTULLI_APPDATA": "/appdata",
        "RADARR_APPDATA": "/appdata/r", "_RUN_MODE_AUTOPAUSE_REASON": "stale"}

cli.api = serving(**{"/api/config": FULL})
rc, o, e = drive(cli.cmd_config_get, args(key=None), BASE)
check("the listing shows real settings", rc == 0 and "HEADROOM_GB = 500" in o, o)
# Derived and internal keys are not settable, so listing them invites someone
# to try to set them.
check("server-derived keys are not listed",
      not any(k in o for k in cli._DERIVED_KEYS), o)
check("underscore keys are not listed", "_RUN_MODE_AUTOPAUSE_REASON" not in o, o)
check("a list value prints as JSON, so it can be pasted back",
      'MONITOR_DIRS = ["/movies"]' in o, o)
check("a boolean prints as JSON, not Python's True",
      "SKIP_UNPLAYED_MOVIES = true" in o, o)
check("an unset value prints as null", "REDLINE_GB = null" in o, o)

cli.api = serving(**{"/api/config": FULL})
rc, o, e = drive(cli.cmd_config_get, args(key="HEADROOM_GB"), BASE)
check("one key prints its bare value, for $(…) capture", rc == 0 and o.strip() == "500", o)

cli.api = serving(**{"/api/config": FULL})
rc, o, e = drive(cli.cmd_config_get, args(key="MONITOR_DIRS"), BASE)
check("a list key prints as JSON even alone", o.strip() == '["/movies"]', o)

# A derived key is hidden from the listing but still readable by name — it is
# not a secret, it is just not something you set.
cli.api = serving(**{"/api/config": FULL})
rc, o, e = drive(cli.cmd_config_get, args(key="CHECK_PATH"), BASE)
check("a derived key is still readable when asked for by name",
      rc == 0 and o.strip() == "/movies", o)

cli.api = serving(**{"/api/config": FULL})
rc, o, e = drive(cli.cmd_config_get, args(key="NO_SUCH_KEY"), BASE)
check("an unknown key exits 1", rc == 1, rc)
check("...with the error on stderr, so stdout stays capturable",
      "no such config key" in e and o.strip() == "", (o, e))

cli.api = serving(**{"/api/config": "not json"})
rc, o, e = drive(cli.cmd_config_get, args(key=None), BASE)
check("an unreadable config exits 1", rc == 1, rc)

cli.api = serving(**{"/api/config": FULL})
rc, o, e = drive(cli.cmd_scoring_get, args(), BASE)
check("scoring get prints every score key", rc == 0
      and all(k in o for k in cli._SCORE_KEYS), o)
check("...and nothing else", "HEADROOM_GB" not in o, o)
check("a score key the config lacks prints as null rather than vanishing",
      "SCORE_BALANCE = null" in o, o)


# ── The empty states ───────────────────────────────────────────────────────
cli.api = serving(**{"/api/logs/deleted": {"marked": []}})
rc, o, e = drive(cli.cmd_queue, args(limit=10), BASE)
check("an empty queue exits 0 — it is a state, not a failure", rc == 0, rc)
check("...and says how to fill it", "Run a Simulate" in o, o)

MARKS = [{"title": f"Film {i}", "score": i, "size_bytes": i * cli.GB,
          "marked": i % 2 == 0} for i in range(1, 6)]
cli.api = serving(**{"/api/logs/deleted": {"marked": MARKS}})
rc, o, e = drive(cli.cmd_queue, args(limit=3), BASE)
check("the queue is numbered in deletion order",
      "1 [" in o and "2 [" in o and "3 [" in o, o)
check("--limit truncates", "Film 4" not in o, o)
check("...and says how many it held back", "2 more (use --limit)" in o, o)
check("a marked entry is distinguished from a merely eligible one",
      "MARKED" in o and "eligible" in o, o)

cli.api = serving(**{"/api/logs/deleted": "<html>502 Bad Gateway</html>"})
rc, o, e = drive(cli.cmd_queue, args(limit=10), BASE)
check("a queue body that is not JSON exits 1 instead of raising", rc == 1, rc)
check("...printing what the service actually returned", "502" in o, o)

cli.api = serving(**{"/api/logs/deleted": {"count": 0, "entries": []}})
rc, o, e = drive(cli.cmd_history, args(limit=10), BASE)
check("an empty history exits 0", rc == 0, rc)
check("...and says nothing was deleted", "no deletions recorded" in o, o)

cli.api = serving(**{"/api/logs/deleted": {
    "count": 2, "reclaimed_label": "1.2 TB",
    "entries": [{"when": "2026-08-01", "title": "Film", "size_bytes": 5 * cli.GB}]}})
rc, o, e = drive(cli.cmd_history, args(limit=10), BASE)
check("history prefers the server's own reclaimed label", "1.2 TB" in o, o)
check("...and lists each deletion with its date and size",
      "2026-08-01" in o and "5.0 GB" in o, o)

cli.api = serving(**{"/api/library-snapshot": {"movies": []}})
rc, o, e = drive(cli.cmd_library, args(page=1, per_page=5), BASE)
check("an empty library table exits 0 and says to scan", rc == 0 and "Simulate" in o, (rc, o))

ROWS = [{"title": f"Film {i}", "plays": i, "rating": 7.5, "protected": i == 1,
         "favorite": i == 2, "excluded": i == 3} for i in range(1, 8)]
cli.api = serving(**{"/api/library-snapshot": {"movies": ROWS}})
rc, o, e = drive(cli.cmd_library, args(page=2, per_page=3), BASE)
check("page 2 starts where page 1 stopped", "showing 4-6" in o, o)
check("...and shows only that page", "Film 4" in o and "Film 1" not in o, o)
check("the row count is the whole table, not the page", "Library (7 " in o, o)

cli.api = serving(**{"/api/library-snapshot": {"movies": ROWS}})
rc, o, e = drive(cli.cmd_library, args(page=1, per_page=3), BASE)
check("flags are named on the rows that carry them",
      "[protected]" in o and "[favorite]" in o, o)
check("a row with no flags gets no empty brackets", "[]" not in o, o)

cli.api = serving(**{"/api/library-snapshot": "<html>502 Bad Gateway</html>"})
rc, o, e = drive(cli.cmd_library, args(page=1, per_page=5), BASE)
check("a library body that is not JSON exits 1 instead of raising", rc == 1, rc)

cli.api = serving(**{"/api/library-snapshot": {
    "movies": [{"title": "No Rating", "plays": 0}]}})
rc, o, e = drive(cli.cmd_library, args(page=1, per_page=3), BASE)
check("a missing rating renders as a dash, not None",
      "imdb=—" in o and "None" not in o, o)


# ── dirs, logs, and the small commands ─────────────────────────────────────
cli.api = serving(**{"/api/library/browse": {
    "ok": True, "path": "/movies", "dirs": [{"path": "/movies/4k"},
                                            {"path": "/movies/kids"}]}})
rc, o, e = drive(cli.cmd_dirs, args(path=None), BASE)
check("dirs lists the folders under the root",
      rc == 0 and "/movies/4k" in o and "/movies/kids" in o, o)
check("...and shows the command that monitors one", "config set MONITOR_DIRS" in o, o)

cli.api = serving(**{"/api/library/browse": {"ok": True, "path": "/movies/4k", "dirs": []}})
rc, o, e = drive(cli.cmd_dirs, args(path="/movies/4k"), BASE)
check("a leaf folder says it has no subfolders", rc == 0 and "no subfolders" in o, (rc, o))
check("...and does not suggest monitoring nothing", "MONITOR_DIRS" not in o, o)

cli.api = serving(**{"/api/library/browse": {"ok": False, "error": "outside the library"}})
rc, o, e = drive(cli.cmd_dirs, args(path="/etc"), BASE)
check("a refused browse exits 1 with the reason on stderr",
      rc == 1 and "outside the library" in e, (rc, e))

cli.api = serving(**{"/api/logs/last": {"content": "line one\nline two"}})
rc, o, e = drive(cli.cmd_logs, args(section=None, lines=50), BASE)
check("logs prints the log", rc == 0 and "line one" in o, o)

cli.api = serving(**{"/api/logs/section": {"content": "SCAN SECTION"}})
rc, o, e = drive(cli.cmd_logs, args(section="scan", lines=50), BASE)
check("--section fetches that section instead", "SCAN SECTION" in o, o)

cli.api = serving(**{"/api/logs/last": {"content": ""}})
rc, o, e = drive(cli.cmd_logs, args(section=None, lines=50), BASE)
check("an empty log says so rather than printing a blank line",
      "(no log yet)" in o, o)

cli.api = serving(**{"/api/config/check": {"connection_health": {
    "critical_ok": False, "severity": "error", "tautulli_connected": True,
    "plex_connected": False, "messages": ["Plex token rejected"]}}})
rc, o, e = drive(cli.cmd_connections_check, args(), BASE)
check("a failing check reports attention needed", "ATTENTION NEEDED" in o, o)
check("...per service", "tautulli  connected" in o and "not connected" in o, o)
check("...with the server's note", "! Plex token rejected" in o, o)

cli.api = serving(**{"/api/config/check": {"connection_health": {
    "critical_ok": True, "severity": "ok", "tautulli_connected": True}}})
rc, o, e = drive(cli.cmd_connections_check, args(), BASE)
check("a healthy check says ok", rc == 0 and "Overall: ok" in o, (rc, o))
check("a service that is not in the payload is not invented",
      "jellyfin" not in o, o)

cli.api = serving(**{"/api/notify/test": {"ok": True, "message": "Sent to 2 services."}})
rc, o, e = drive(cli.cmd_notify_test, args(), BASE)
check("a sent test notification exits 0 with the server's message",
      rc == 0 and "Sent to 2 services." in o, (rc, o))

cli.api = serving(**{"/api/notify/test": {"ok": False, "error": "no destinations configured"}})
rc, o, e = drive(cli.cmd_notify_test, args(), BASE)
check("a failed test exits 1 with the reason on stderr",
      rc == 1 and "no destinations configured" in e, (rc, e))

cli.api = serving(**{"/api/run/stop": {"ok": True, "message": "Stopping after this file."}})
rc, o, e = drive(cli.cmd_stop, args(), BASE)
check("stop passes on what the server will do",
      rc == 0 and "Stopping after this file." in o, (rc, o))

cli.api = serving(**{"/api/run/stop": {"ok": True}})
rc, o, e = drive(cli.cmd_stop, args(), BASE)
check("...or falls back to its own wording", "Stop requested." in o, o)

cli.api = serving(**{"/api/summary/run": {"ok": True}})
rc, o, e = drive(cli.cmd_refresh, args(), BASE)
check("refresh reports it started", rc == 0 and "refresh started" in o, (rc, o))

cli.api = serving(**{"/api/summary/run": {"ok": False, "error": "a run is active"}})
rc, o, e = drive(cli.cmd_refresh, args(), BASE)
check("a refused refresh exits 1", rc == 1 and "a run is active" in e, (rc, e))

cli.api = serving(**{"/api/imdb/download": {"ok": True}})
rc, o, e = drive(cli.cmd_imdb_download, args(), BASE)
check("imdb download reports it started", rc == 0 and "download started" in o, (rc, o))

cli.api = serving(**{"/api/imdb/status": {"present": True, "rows": 1234}})
rc, o, e = drive(cli.cmd_imdb_status, args(), BASE)
check("imdb status prints the payload", rc == 0 and "1234" in o, (rc, o))

cli.api = serving(**{"/api/cache/status": {"can_clear": True}})
rc, o, e = drive(cli.cmd_cache_status, args(), BASE)
check("cache status prints the payload", rc == 0 and "can_clear" in o, (rc, o))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
