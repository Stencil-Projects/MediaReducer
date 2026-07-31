#!/usr/bin/env python3
"""MediaReducer command-line interface.

A thin HTTP client for a running MediaReducer service: everything the web UI does,
from a terminal, with no browser. It talks to the same API the GUI uses, so every
command goes through the identical server-side validation, safety gates, and run
state; there is no separate code path to drift out of sync.

    mediareducer status                      # dashboard summary
    mediareducer connections autodetect      # onboarding: detect + save API keys
    mediareducer dirs                        # onboarding: browse library folders
    mediareducer simulate                    # preview deletions (streams progress)
    mediareducer cleanup --yes               # delete to your thresholds now
    mediareducer config get                  # print the whole config
    mediareducer config set HEADROOM_GB=500 RUN_MODE=paused
    mediareducer notify test                 # test the configured notifications
    mediareducer queue                       # the marked & eligible deletion plan
    mediareducer history                     # deletion history

Global flags (--json, --url, --timeout) work before or after the subcommand.

The service URL defaults to http://127.0.0.1:7474; override with --url or the
MEDIAREDUCER_URL environment variable. Requires the MediaReducer service to be
running (it already is, if the scheduler is doing automatic cleanups)."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = os.environ.get("MEDIAREDUCER_URL", "http://127.0.0.1:7474")
GB = 1_000_000_000

# Runtime-only / env-derived keys the server fills in on load. Never write them
# back through a config save (they'd just be clutter, overridden on next load).
_DERIVED_KEYS = ("CHECK_PATH", "TAUTULLI_APPDATA", "RADARR_APPDATA")
_LIST_KEYS = {"MONITOR_DIRS", "PROTECTED_COLLECTIONS", "JELLYFIN_PROTECTED_COLLECTIONS",
              "MOVIE_EXTENSIONS"}
# Filtering & Scoring keys. These save through /api/score-config, not /api/config.
_SCORE_KEYS = ("SCORE_BALANCE", "GRACE_PERIOD_DAYS", "MAX_IMDB_RATING", "NEAR_TIE_PTS",
               "MAX_STALENESS_MONTHS", "SKIP_UNPLAYED_MOVIES", "PROTECT_JELLYFIN_FAVORITES")


class ApiError(Exception):
    pass


# Localhost service, so never route through an HTTP proxy (and don't let a shell
# HTTP_PROXY break a same-host call).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(method, path, base, *, body=None, timeout=60):
    """Call the service; return (status_code, parsed_body). Raises ApiError if the
    service is unreachable."""
    url = base.rstrip("/") + path
    headers = {"X-MediaReducer": "1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _parse(raw)
    except urllib.error.URLError as e:
        raise ApiError(f"Cannot reach MediaReducer at {base} ({e.reason}). "
                       f"Is the service running? Set --url or MEDIAREDUCER_URL.")
    except TimeoutError:
        raise ApiError(f"Request to {url} timed out after {timeout}s.")


def _parse(raw):
    raw = raw.strip()
    if raw[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw


# ── formatting helpers ────────────────────────────────────────────────────────
def gb(n):
    try:
        return f"{float(n):.1f} GB"
    except (TypeError, ValueError):
        return "—"


def bytes_gb(n):
    try:
        return f"{float(n) / GB:.1f} GB"
    except (TypeError, ValueError):
        return "—"



def out(obj, as_json):
    """Print a value as pretty JSON (scripting) or leave rendering to the caller."""
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
        return True
    return False


def parse_value(key, raw):
    """Turn a KEY=VALUE string value into a typed config value."""
    s = raw.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", ""):
        return None
    if key in _LIST_KEYS:
        if s.startswith("["):
            return json.loads(s)
        return [x.strip() for x in s.split(",") if x.strip()]
    if s.startswith(("[", "{")):
        try:
            return json.loads(s)
        except ValueError:
            pass
    try:
        return int(s) if not any(c in low for c in ".e") else float(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def _kv_pairs(items):
    updates = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"error: expected KEY=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        updates[k.strip()] = parse_value(k.strip(), v)
    return updates


# ── commands ──────────────────────────────────────────────────────────────────
def cmd_status(args, base):
    code, d = api("GET", "/api/status", base)
    if out(d, args.json):
        return 0
    if not isinstance(d, dict):
        print(d)
        return 1
    run = ("running (cleanup)" if d.get("run_cleanup") else
           "running (debug cleanup)" if d.get("run_debug_cleanup") else
           "running" if d.get("run_active") else
           "summary refreshing" if d.get("summary_active") else "idle")
    print(f"Run state:     {run}")
    _mode_labels = {"off": "Paused", "paused": "Monitor Only", "headroom": "Automatic Cleanup"}
    if d.get("run_mode"):
        print(f"Mode:          {_mode_labels.get(d['run_mode'], d['run_mode'])}")
    disk = d.get("disk") or {}
    if disk:
        print(f"Storage:       {gb(disk.get('free_gb'))} free of {gb(disk.get('total_gb'))} "
              f"({disk.get('pct_used', '?')}% used)")
    print(f"Library size:  {gb(d.get('library_gb'))}")
    print("Targets:       "
          f"headroom {gb(d.get('headroom_gb')) if d.get('headroom_gb') else 'off'}, "
          f"redline {gb(d.get('redline_gb')) if d.get('redline_gb') else 'disabled'}, "
          f"cap {gb(d.get('library_cap_gb')) if d.get('library_cap_gb') else 'disabled'}")
    print(f"Marked/queued: {d.get('marked_count', 0)} marked "
          f"({d.get('marked_ripe_count', 0)} due now)")
    print(f"Deleted total: {d.get('deleted_count', 0)} movies, "
          f"{d.get('deleted_reclaimed_label') or bytes_gb(d.get('deleted_reclaimed_bytes'))}")
    print(f"Last run:      {d.get('last_run') or '—'}")
    nxt = d.get("next_run_time")
    print(f"Next run:      {nxt or '—'}")
    if d.get("run_mode_autopause_reason"):
        print(f"Auto-paused:   {d['run_mode_autopause_reason']}")
    # config_attention mirrors the GUI's red Configuration tab.
    attention = d.get("config_attention")
    print(f"Config health: {'NEEDS ATTENTION — run `mediareducer connections check`' if attention else 'ok'}")
    return 0


def cmd_scoring_get(args, base):
    code, cfg = api("GET", "/api/config", base)
    if not isinstance(cfg, dict):
        print(cfg)
        return 1
    scoring = {k: cfg.get(k) for k in _SCORE_KEYS}
    if out(scoring, args.json):
        return 0
    for k in _SCORE_KEYS:
        v = scoring[k]
        print(f"{k} = {json.dumps(v) if isinstance(v, bool) or v is None else v}")
    return 0


def cmd_config_get(args, base):
    code, cfg = api("GET", "/api/config", base)
    if not isinstance(cfg, dict):
        print(cfg)
        return 1
    if args.key:
        if args.key not in cfg:
            print(f"error: no such config key: {args.key}", file=sys.stderr)
            return 1
        val = cfg[args.key]
        print(json.dumps(val) if args.json else (json.dumps(val) if isinstance(val, (list, dict)) else val))
        return 0
    if out(cfg, args.json):
        return 0
    for k in sorted(cfg):
        if k in _DERIVED_KEYS or k.startswith("_"):
            continue
        v = cfg[k]
        print(f"{k} = {json.dumps(v) if isinstance(v, (list, dict, bool)) or v is None else v}")
    return 0


def _post_config(base, updates, timeout):
    """Round-trip the FULL config with `updates` applied. POST /api/config replaces
    the file with the posted body, so send everything, not just the changed keys.
    Mirrors the GUI form's contract: internal underscore keys and server-derived
    paths are never posted (the server preserves/regenerates them; posting a stale
    _RUN_MODE_AUTOPAUSE_REASON back, for example, would stop it clearing), and an
    enabled Radarr cleanup posts as "auto" so the server re-resolves the section."""
    code, cfg = api("GET", "/api/config", base)
    if not isinstance(cfg, dict):
        raise ApiError("could not read current config")
    for k in list(cfg):
        if k in _DERIVED_KEYS or k == "OUTPUT_DIR" or k.startswith("_"):
            cfg.pop(k)
    if cfg.get("RADARR_OVERSEERR_SECTION_ID") is not None and "RADARR_OVERSEERR_SECTION_ID" not in updates:
        cfg["RADARR_OVERSEERR_SECTION_ID"] = "auto"
    cfg.update(updates)
    return api("POST", "/api/config", base, body=cfg, timeout=timeout)


def _post_score(base, updates, timeout):
    """Round-trip the FULL Filtering & Scoring payload with `updates` applied. The
    /api/score-config validator needs every score field present, not just the changed
    one."""
    code, cfg = api("GET", "/api/config", base)
    if not isinstance(cfg, dict):
        raise ApiError("could not read current config")
    payload = {k: cfg.get(k) for k in _SCORE_KEYS}
    for lk in ("_MAX_IMDB_RATING_LAST", "_NEAR_TIE_PTS_LAST"):
        if lk in cfg:
            payload[lk] = cfg[lk]
    payload.update(updates)
    return api("POST", "/api/score-config", base, body=payload, timeout=timeout)


def cmd_config_set(args, base):
    updates = _kv_pairs(args.assignments)
    score_updates = {k: v for k, v in updates.items() if k in _SCORE_KEYS}
    cfg_updates = {k: v for k, v in updates.items() if k not in _SCORE_KEYS}
    rc = 0
    if cfg_updates:
        code, d = _post_config(base, cfg_updates, args.timeout)
        rc = _report_save(d, code, args.json)
    if score_updates:
        code, d = _post_score(base, score_updates, args.timeout)
        rc = _report_save(d, code, args.json) or rc
    return rc


def _report_save(d, code, as_json):
    if out({"status": code, "response": d}, as_json):
        return 0 if (isinstance(d, dict) and d.get("ok")) else 1
    if isinstance(d, dict) and d.get("ok"):
        print("Saved.")
        if d.get("reconcile") == "started":
            print("  Deletion plan rebuilt in place from the last scan (no Simulate needed).")
        elif d.get("reconcile") == "held_connection":
            print("  Reconcile held — a needed server is unreachable; fix it or disable that server.")
        if d.get("automatic_run_mode_paused"):
            print(f"  Automatic Cleanup switched to Monitor Only: {d.get('automatic_run_mode_paused_reason', '')}")
        for name in (d.get("server_software_auto_disabled") or []):
            print(f"  {name} was deselected (its connection failed or the key is blank).")
        return 0
    msg = d.get("error") or d.get("message") if isinstance(d, dict) else str(d)
    print(f"Save failed ({code}): {msg}", file=sys.stderr)
    if isinstance(d, dict) and d.get("invalid_config"):
        for iss in d["invalid_config"]:
            print(f"  - {iss.get('key')}: {iss.get('message')}", file=sys.stderr)
    return 1


def cmd_config_reset(args, base):
    if not args.yes and not _confirm("Reset ALL configuration and state to first-time setup? Logs are kept."):
        return 1
    code, d = api("POST", "/api/config/reset", base, body={})
    return _report_ok(d, code, args.json, "Reset to first-time setup.")


def cmd_config_fix(args, base):
    code, d = api("POST", "/api/config/reset-invalid", base, body={})
    if out(d, args.json):
        return 0 if isinstance(d, dict) and d.get("ok") else 1
    if isinstance(d, dict) and d.get("ok"):
        print("Invalid hand-edited values reset to defaults.")
        return 0
    print(f"Could not fix config ({code}): {d.get('error') if isinstance(d, dict) else d}", file=sys.stderr)
    return 1


def cmd_connections_check(args, base):
    code, d = api("POST", "/api/config/check", base, body={}, timeout=args.timeout)
    if out(d, args.json):
        return 0
    if not isinstance(d, dict):
        print(d)
        return 1
    health = d.get("connection_health") or d
    print(f"Overall: {'ok' if health.get('critical_ok') else 'ATTENTION NEEDED'}  "
          f"(severity: {health.get('severity', '—')})")
    for svc in ("tautulli", "plex", "jellyfin", "radarr"):
        key = f"{svc}_connected"
        if key in health:
            print(f"  {svc:9} {'connected' if health.get(key) else 'not connected / disabled'}")
    for note in (health.get("messages") or health.get("errors") or []):
        print(f"  ! {note}")
    return 0


def cmd_connections_autodetect(args, base):
    """Detect API keys from the mounted appdata and SAVE the ones it found —
    the CLI equivalent of clicking Auto Detect and then Save in the GUI."""
    code, d = api("POST", "/api/connections/autodetect", base, body={}, timeout=args.timeout)
    if out(d, args.json):
        return 0
    values = d.get("values") if isinstance(d, dict) else None
    detected = {k: v for k, v in (values or {}).items() if str(v or "").strip()}
    for k in sorted((values or {})):
        print(f"  {k}: {'detected' if k in detected else '—'}")
    if not detected:
        print("Nothing detected — check the appdata mounts (tautulli/radarr).")
        return 1
    if args.dry_run:
        print("(--dry-run: nothing saved)")
        return 0
    code, resp = _post_config(base, detected, args.timeout)
    if isinstance(resp, dict) and resp.get("ok"):
        print(f"Saved {len(detected)} detected key(s).")
        return 0
    return _report_save(resp, code, False)


def cmd_collections(args, base):
    code, d = api("POST", "/api/collections", base, body={}, timeout=args.timeout)
    if out(d, args.json):
        return 0
    if not isinstance(d, dict):
        print(d)
        return 1
    _, cfg = api("GET", "/api/config", base)
    protected = {
        "plex": set((cfg or {}).get("PROTECTED_COLLECTIONS") or []),
        "jellyfin": set((cfg or {}).get("JELLYFIN_PROTECTED_COLLECTIONS") or []),
    }
    shown = False
    for server in ("plex", "jellyfin"):
        block = d.get(server)
        if not isinstance(block, dict) or not block.get("enabled"):
            continue
        shown = True
        print(f"{server.title()} collections (checked = protected):")
        names = block.get("names") or []
        if not names:
            print(f"  ({block.get('error') or 'none found'})")
        for name in names:
            print(f"  [{'x' if name in protected[server] else ' '}] {name}")
    if not shown:
        print("No collection-capable server is enabled (need a Plex token or Jellyfin).")
    return 0


def cmd_queue(args, base):
    code, d = api("GET", "/api/logs/deleted", base, body=None)
    if out(d, args.json):
        return 0
    marked = (d or {}).get("marked") or []
    if not marked:
        print("The marked & eligible queue is empty. Run a Simulate to build it.")
        return 0
    print(f"Marked & eligible ({len(marked)} in deletion order):")
    for i, m in enumerate(marked[: args.limit], 1):
        state = "MARKED" if m.get("marked") else "eligible"
        print(f"  {i:>4} [{state:>8}] {(m.get('title') or '?')[:48]:<48} "
              f"score={m.get('score', '?')} {bytes_gb(m.get('size_bytes'))}")
    if len(marked) > args.limit:
        print(f"  … {len(marked) - args.limit} more (use --limit)")
    return 0


def cmd_history(args, base):
    code, d = api("GET", f"/api/logs/deleted?limit={args.limit}", base)
    if out(d, args.json):
        return 0
    if not isinstance(d, dict):
        print(d)
        return 1
    print(f"Deleted total: {d.get('count', 0)} movies, {d.get('reclaimed_label') or bytes_gb(d.get('reclaimed_bytes'))}")
    entries = d.get("entries") or []
    for e in entries[: args.limit]:
        print(f"  {e.get('when') or e.get('date') or ''}  {(e.get('title') or '?')[:48]:<48} "
              f"{bytes_gb(e.get('size_bytes'))}")
    if not entries:
        print("  (no deletions recorded)")
    return 0


def cmd_history_clear(args, base):
    if not args.yes and not _confirm("Erase the deletion history (deleted.log)?"):
        return 1
    code, d = api("POST", "/api/logs/deleted/clear", base, body={})
    return _report_ok(d, code, args.json, "Deletion history cleared.")


def cmd_library(args, base):
    code, d = api("GET", "/api/library-snapshot", base)
    if out(d, args.json):
        return 0
    rows = (d or {}).get("movies") or (d or {}).get("rows") or []
    if not rows:
        print("The library table is empty. Run a Simulate first.")
        return 0
    start = (args.page - 1) * args.per_page
    page = rows[start: start + args.per_page]
    print(f"Library ({len(rows)} movies) — page {args.page}, showing {start + 1}-{start + len(page)}:")
    for i, m in enumerate(page, start + 1):
        flags = " ".join(f for f, on in (("protected", m.get("protected")),
                                         ("favorite", m.get("favorite")),
                                         ("excluded", m.get("excluded"))) if on)
        rating = m.get("rating")
        print(f"  {i:>5}  {(m.get('title') or '?')[:40]:<40} "
              f"plays={str(m.get('plays', 0)):>4}  imdb={rating if rating is not None else '—'}"
              + (f"  [{flags}]" if flags else ""))
    return 0


def cmd_logs(args, base):
    if args.section:
        code, d = api("GET", f"/api/logs/section?kind={args.section}", base)
    else:
        code, d = api("GET", f"/api/logs/last?lines={args.lines}", base)
    if out(d, args.json):
        return 0
    text = d.get("content") if isinstance(d, dict) else d
    print(text or "(no log yet)")
    return 0


def cmd_refresh(args, base):
    code, d = api("POST", "/api/summary/run", base, body={})
    return _report_ok(d, code, args.json, "Storage refresh started.")


def cmd_cache_status(args, base):
    code, d = api("GET", "/api/cache/status", base)
    if out(d, args.json):
        return 0
    print(json.dumps(d, indent=2) if isinstance(d, dict) else d)
    return 0


def cmd_cache_clear(args, base):
    if not args.yes and not _confirm("Clear the cache/store (movie metadata + library snapshot + deletion plan)?"):
        return 1
    code, d = api("POST", "/api/cache/clear", base, body={})
    return _report_ok(d, code, args.json, "Cache cleared.")


def cmd_imdb_status(args, base):
    code, d = api("GET", "/api/imdb/status", base)
    if out(d, args.json):
        return 0
    print(json.dumps(d, indent=2) if isinstance(d, dict) else d)
    return 0


def cmd_imdb_download(args, base):
    code, d = api("POST", "/api/imdb/download", base, body={}, timeout=args.timeout)
    return _report_ok(d, code, args.json, "IMDb dataset download started.")


def cmd_stop(args, base):
    code, d = api("POST", "/api/run/stop", base, body={})
    if out(d, args.json):
        return 0
    print((d.get("message") if isinstance(d, dict) else None) or "Stop requested.")
    return 0


def cmd_run(args, base, mode, label):
    if mode == "headroom" and not args.yes:
        if not _confirm(f"Run a real Cleanup now? This DELETES files to your thresholds "
                        f"(no recycle bin)."):
            return 1
    code, d = api("POST", "/api/run", base, body={"mode": mode}, timeout=args.timeout)
    if not isinstance(d, dict) or not d.get("ok", False):
        msg = d.get("message") or d.get("error") if isinstance(d, dict) else str(d)
        print(f"{label} refused ({code}): {msg}", file=sys.stderr)
        return 1
    if d.get("started") is False:
        print(d.get("message") or "Nothing to do.")
        return 0
    if args.json or args.no_follow:
        print(json.dumps(d) if args.json else f"{label} started.")
        return 0
    print(f"{label} started — streaming progress (Ctrl-C to detach; the run keeps going):")
    return _stream_run(base)


_PHASE_LABELS = {"checking": "Checking connections", "library": "Reading library",
                 "scanning": "Scanning & scoring", "deleting": "Deleting",
                 "simulating": "Simulating", "done": "Done"}


def _stream_run(base):
    last_line = None
    last_phase = None
    try:
        while True:
            code, p = api("GET", "/api/run/progress", base, timeout=15)
            if not isinstance(p, dict) or not p.get("status"):
                time.sleep(1.0)
                continue
            status = p.get("status")
            phase = p.get("phase") or ""
            if phase != last_phase and phase in _PHASE_LABELS:
                print(f"  · {_PHASE_LABELS[phase]}")
                last_phase = phase
            line = _progress_line(p)
            if line and line != last_line:
                print(f"    {line}")
                last_line = line
            if status in ("done", "stopped", "error"):
                print(f"  {status.upper()}: {p.get('message') or ''}".rstrip())
                return 0 if status == "done" else 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n(detached — the run continues in the background; `mediareducer status` to check)")
        return 0


def _progress_line(p):
    phase = p.get("phase")
    if phase == "scanning" and p.get("total"):
        return f"scanned {p.get('scanned', 0)}/{p.get('total')} — eligible {p.get('eligible', 0)}"
    if phase in ("deleting", "simulating"):
        tgt = p.get("target_bytes") or 0
        freed = p.get("bytes_freed") or 0
        verb = "would free" if phase == "simulating" else "freed"
        base_ = f"{verb} {bytes_gb(freed)}"
        return base_ + (f" / {bytes_gb(tgt)}" if tgt else "") + f" — {p.get('deleted', 0)} movies"
    return ""


def cmd_report(args, base):
    code, d = api("POST", "/api/debug/report", base, body={}, timeout=args.timeout)
    if out(d, args.json):
        return 0 if isinstance(d, dict) and d.get("ok") else 1
    text = d.get("text") if isinstance(d, dict) else None
    if not text:
        msg = d.get("error") if isinstance(d, dict) else d
        print(f"Could not build report ({code}): {msg}", file=sys.stderr)
        return 1
    dest = args.output or (d.get("filename") if isinstance(d, dict) else None)
    if dest and dest != "-":
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Diagnostic report written to {dest}")
    else:
        print(text)
    return 0


def cmd_notify_test(args, base):
    """Send a test notification to the configured destinations."""
    code, d = api("POST", "/api/notify/test", base, body={}, timeout=args.timeout)
    if out(d, args.json):
        return 0 if isinstance(d, dict) and d.get("ok") else 1
    if isinstance(d, dict) and d.get("ok"):
        print(d.get("message") or "Test notification sent.")
        return 0
    msg = d.get("error") if isinstance(d, dict) else d
    print(f"Test failed ({code}): {msg}", file=sys.stderr)
    return 1


def cmd_dirs(args, base):
    """Browse the library root: the folders MONITOR_DIRS can point at."""
    q = f"?path={urllib.parse.quote(args.path)}" if args.path else ""
    code, d = api("GET", f"/api/library/browse{q}", base)
    if out(d, args.json):
        return 0
    if not isinstance(d, dict) or not d.get("ok", True):
        print(f"Browse failed ({code}): {d.get('error') if isinstance(d, dict) else d}", file=sys.stderr)
        return 1
    print(f"{d.get('path')}:")
    dirs = d.get("dirs") or []
    for entry in dirs:
        print(f"  {entry.get('path')}")
    if not dirs:
        print("  (no subfolders)")
    else:
        print('Monitor one with:  mediareducer config set MONITOR_DIRS="<path>[,<path>…]"')
    return 0


def _report_ok(d, code, as_json, ok_msg):
    if out({"status": code, "response": d}, as_json):
        return 0 if (isinstance(d, dict) and d.get("ok")) else 1
    if isinstance(d, dict) and d.get("ok"):
        print(d.get("message") or ok_msg)
        return 0
    msg = d.get("error") or d.get("message") if isinstance(d, dict) else str(d)
    print(f"Failed ({code}): {msg}", file=sys.stderr)
    return 1


def _confirm(prompt):
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ── argument parser ───────────────────────────────────────────────────────────
def build_parser():
    # Global flags accepted BOTH before and after the subcommand
    # (`mediareducer --json status` and `mediareducer status --json`). Two
    # separate parent parsers on purpose: the top level carries the real
    # defaults, while the one shared with subparsers uses SUPPRESS so a
    # subparser only writes the attribute when the flag was actually given,
    # never clobbering a value parsed at the top level. They must not share
    # action objects (set_defaults/action.default would leak between them).
    def _global_flags(suppress):
        d = argparse.SUPPRESS if suppress else None
        gp = argparse.ArgumentParser(add_help=False)
        gp.add_argument("--url", default=d if suppress else DEFAULT_URL,
                        help=f"service URL (default {DEFAULT_URL})")
        gp.add_argument("--json", action="store_true",
                        default=d if suppress else False,
                        help="raw JSON output (for scripting)")
        gp.add_argument("--timeout", type=float,
                        default=d if suppress else 120.0,
                        help="request timeout seconds (default 120)")
        return gp

    common = _global_flags(suppress=True)
    p = argparse.ArgumentParser(prog="mediareducer", description=__doc__,
                                parents=[_global_flags(suppress=False)],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add(parser_or_sub, name, help, **kw):
        return parser_or_sub.add_parser(name, help=help, parents=[common], **kw)

    add(sub, "status", "dashboard summary").set_defaults(func=cmd_status)

    c = add(sub, "config", "view / change configuration")
    cs = c.add_subparsers(dest="sub", required=True)
    g = add(cs, "get", "print config (all keys, or one KEY)")
    g.add_argument("key", nargs="?")
    g.set_defaults(func=cmd_config_get)
    s = add(cs, "set", "set KEY=VALUE (one or more); rebuilds the plan in place")
    s.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    s.set_defaults(func=cmd_config_set)
    r = add(cs, "reset", "reset ALL config + state to first-time setup")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=cmd_config_reset)
    add(cs, "fix", "reset only invalid hand-edited values").set_defaults(func=cmd_config_fix)

    sc = add(sub, "scoring", "filtering & scoring settings")
    scs = sc.add_subparsers(dest="sub", required=True)
    add(scs, "get", "print the scoring/filter settings").set_defaults(func=cmd_scoring_get)
    ss = add(scs, "set", "set a scoring/filter KEY=VALUE")
    ss.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    ss.set_defaults(func=cmd_config_set)

    cn = add(sub, "connections", "check / auto-detect API connections")
    cns = cn.add_subparsers(dest="sub", required=True)
    add(cns, "check", "probe every selected API").set_defaults(func=cmd_connections_check)
    ad = add(cns, "autodetect", "detect API keys from mounted appdata and save them")
    ad.add_argument("--dry-run", action="store_true", help="show what would be detected; save nothing")
    ad.set_defaults(func=cmd_connections_autodetect)

    d = add(sub, "dirs", "browse the library folders MONITOR_DIRS can point at")
    d.add_argument("path", nargs="?", help="folder to list (default: the library root)")
    d.set_defaults(func=cmd_dirs)

    add(sub, "collections", "list protected-collection pickers").set_defaults(func=cmd_collections)

    nt = add(sub, "notify", "outbound notifications")
    nts = nt.add_subparsers(dest="sub", required=True)
    add(nts, "test", "send a test notification to the configured services").set_defaults(func=cmd_notify_test)

    sim = add(sub, "simulate", "preview deletions (no files touched)")
    sim.add_argument("--no-follow", action="store_true", help="don't stream; return immediately")
    sim.set_defaults(func=lambda a, b: cmd_run(a, b, "debug_sim", "Simulate"))

    cl = add(sub, "cleanup", "delete to your thresholds NOW")
    cl.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    cl.add_argument("--no-follow", action="store_true")
    cl.set_defaults(func=lambda a, b: cmd_run(a, b, "headroom", "Cleanup"))

    dc = add(sub, "debug-cleanup", "dry-run cleanup (Debug mode); deletes nothing")
    dc.add_argument("--no-follow", action="store_true")
    dc.set_defaults(func=lambda a, b: cmd_run(a, b, "debug_cleanup", "Debug Cleanup"))

    add(sub, "stop", "stop the active run").set_defaults(func=cmd_stop)
    add(sub, "refresh", "refresh storage/library stats").set_defaults(func=cmd_refresh)

    q = add(sub, "queue", "the marked & eligible deletion plan")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(func=cmd_queue)

    h = add(sub, "history", "deletion history")
    h.add_argument("--limit", type=int, default=50)
    hs = h.add_subparsers(dest="sub")
    hc = add(hs, "clear", "erase deleted.log")
    hc.add_argument("--yes", action="store_true")
    hc.set_defaults(func=cmd_history_clear)
    h.set_defaults(func=cmd_history)

    lib = add(sub, "library", "the Filtering & Scoring library table")
    lib.add_argument("--page", type=int, default=1)
    lib.add_argument("--per-page", type=int, default=25)
    lib.set_defaults(func=cmd_library)

    lg = add(sub, "logs", "print the run log (or one section)")
    lg.add_argument("--lines", type=int, default=200)
    # Listed in the order a run writes them; must stay in step with app.py's
    # _LOG_SECTION_RES, which is what /api/logs/section validates against.
    lg.add_argument("--section", choices=["library", "scan", "eligible", "deletions",
                                          "summary", "errors"])
    lg.set_defaults(func=cmd_logs)

    ca = add(sub, "cache", "cache/store tools")
    cas = ca.add_subparsers(dest="sub", required=True)
    add(cas, "status", "cache clear availability").set_defaults(func=cmd_cache_status)
    cac = add(cas, "clear", "wipe the cache/store")
    cac.add_argument("--yes", action="store_true")
    cac.set_defaults(func=cmd_cache_clear)

    im = add(sub, "imdb", "IMDb ratings dataset")
    ims = im.add_subparsers(dest="sub", required=True)
    add(ims, "status", "dataset status").set_defaults(func=cmd_imdb_status)
    add(ims, "download", "download/refresh the dataset").set_defaults(func=cmd_imdb_download)

    rp = add(sub, "report", "build a scrubbed diagnostic report")
    rp.add_argument("-o", "--output",
                    help="output file (default: the server-suggested name; '-' for stdout)")
    rp.set_defaults(func=cmd_report)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    base = args.url
    try:
        return args.func(args, base) or 0
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
