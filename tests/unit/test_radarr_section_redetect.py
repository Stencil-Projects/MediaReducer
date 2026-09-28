"""Radarr's Plex section is looked for again while Radarr cleanup is on and none
has been found — not only when the credentials change.

The section is auto-detected by matching Radarr's movie paths to a Plex movie
library. It ran once, when Radarr and Plex credentials were first saved. In the
test lab Radarr held no films at that moment ("Radarr returned no movies to
compare."), and after the films were imported and "Remove deleted movies from
Radarr" was ticked and saved, nothing ran it again: the page said the section
"is detected automatically after Radarr and Plex connect" while both were
connected, and only re-entering Radarr's credentials would have retried.

Pinned through the real save handler: a save with the cleanup on and nothing
cached detects; a failure leaves it on "auto" for the engine to resolve at run
time and is tried again on the next save; a success is cached and never
re-detected on later saves; with the cleanup off nothing is looked up.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-radarr-redetect.")
atexit.register(shutil.rmtree, _OUT, True)
_LIB = tempfile.mkdtemp(prefix="mr-radarr-redetect-lib.")
atexit.register(shutil.rmtree, _LIB, True)
(Path(_LIB) / "movies").mkdir(parents=True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ["MEDIAREDUCER_LIBRARY"] = _LIB
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


CREDS = {"RADARR_URL": "http://radarr.test:7878", "RADARR_API_KEY": "rk",
         "PLEX_URL": "http://plex.test:32400", "PLEX_TOKEN": "pt"}
_state = {"cfg": {
    "RUN_MODE": "paused", "MONITOR_DIRS": ["/library/movies"],
    "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf:1", "JELLYFIN_API_KEY": "k",
    "USE_PLEX": False, "TAUTULLI_URL": "", "TAUTULLI_API_KEY": "",
    "HEADROOM_GB": 500, "REDLINE_GB": None, "MAX_LIBRARY_GB": None,
    "REDLINE_ONLY_MODE": False, "MAX_HEADROOM_PCT": 15, "OUTPUT_DIR": _OUT,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
    "RADARR_OVERSEERR_SECTION_ID": None, **CREDS,
}}
A.load_config = lambda: dict(_state["cfg"])
A.save_config = lambda cfg, **k: _state.update(cfg=dict(cfg)) or True
A.run_summary = lambda *a, **k: (False, "skip")
A._invalid_config_response = lambda: None
_HEALTH = {"critical_ok": True, "probed": True, "errors": [], "warnings": [],
           "severity": "", "required_tooltip": "", "jellyfin_connected": True,
           "tautulli_connected": False, "radarr_connected": True, "plex_connected": True}
A._health_for_config_save = lambda cfg: dict(_HEALTH)
A._refresh_connection_health_cache = lambda cfg, probe=True: dict(_HEALTH)
A._library_db_fresh = lambda c=None: True
A._full_scan_overdue = lambda: False
A._pending_plan_current = lambda *a, **k: True
A._deletion_limits_exceeded = lambda *a, **k: False
A._simulate_evidence = lambda cfg: True
A._pending_raw = lambda: {}

calls = []
answer = {"d": {"ok": False, "section_id": None, "message": "Radarr returned no movies to compare."}}


def _detect(cfg=None):
    calls.append(1)
    return dict(answer["d"])


A._detect_radarr_plex_section = _detect
client = A.app.test_client()


def save(section):
    payload = dict(_state["cfg"])
    payload.pop("OUTPUT_DIR", None)
    payload["RADARR_OVERSEERR_SECTION_ID"] = section
    r = client.post("/api/config", json=payload, headers={"X-MediaReducer": "1"})
    return r.status_code, (r.get_json() or {})


# ── Off: nothing is looked up ─────────────────────────────────────────────
status, _ = save(None)
check("with Radarr cleanup off, a save looks nothing up", status == 200 and not calls, (status, calls))

# ── On, Radarr still empty: tried, reported, left on auto ────────────────
status, body = save("auto")
check("switching the cleanup on with nothing detected looks for the section",
      status == 200 and len(calls) == 1, (status, calls))
check("...and the save reports why it was not found",
      (body.get("radarr_section_detection") or {}).get("message") == "Radarr returned no movies to compare.",
      body.get("radarr_section_detection"))
check("...leaving the cleanup on 'auto' for the run to resolve",
      _state["cfg"].get("RADARR_OVERSEERR_SECTION_ID") == "auto"
      and not _state["cfg"].get("_RADARR_DETECTED_SECTION_ID"), _state["cfg"].get("RADARR_OVERSEERR_SECTION_ID"))

# ── Films imported since: the next save, credentials unchanged, finds it ──
answer["d"] = {"ok": True, "section_id": "1", "section_name": "Movies", "method": "path-prefix"}
status, body = save("auto")
check("a later save with the same credentials looks again", len(calls) == 2, calls)
check("...caches what it found", _state["cfg"].get("_RADARR_DETECTED_SECTION_ID") == "1",
      _state["cfg"].get("_RADARR_DETECTED_SECTION_ID"))
check("...and resolves the cleanup to that section",
      str(_state["cfg"].get("RADARR_OVERSEERR_SECTION_ID")) == "1",
      _state["cfg"].get("RADARR_OVERSEERR_SECTION_ID"))

# ── Found once, kept: later saves do not look it up again ────────────────
status, _ = save("auto")
check("once a section is cached, saves stop looking it up", len(calls) == 2, calls)

# ── The page's resting note no longer promises a detection that already ran
cfg_html = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
cfg_js = (ROOT / "static" / "js" / "config.js").read_text(encoding="utf-8")
check("the note says when detection happens, true whether or not it has run",
      "detected when you save with Radarr and Plex connected" in cfg_html
      and "detected when you save with Radarr and Plex connected" in cfg_js
      and "detected automatically after Radarr and Plex connect" not in cfg_html + cfg_js)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
