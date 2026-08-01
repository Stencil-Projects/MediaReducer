"""Exhaustive space-threshold combination matrix: every (mode, headroom,
redline, cap) state is checked against ALL THREE validators — the /api/config
save handler, the hand-edit file validator, and the engine validator — which
must agree exactly on what is saveable. Valid states additionally check the
Cleanup/Simulate gating direction. Guards the contract:

  The REDLINE_ONLY_MODE flag is DERIVED from the headroom value (armed iff
  >= 1), so no flag combination is unsaveable by itself and the matrix verdict
  ignores it. One cross-field rule remains: under an ARMED headroom, redline
  sits STRICTLY below it. Every threshold off at once is a VALID saved state —
  the scheduler simply rests at Paused until one is armed (mode gating is
  covered in test_reset_then_setup / test_startup_invalidation, not here).
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import atexit
import shutil
import tempfile
_OUT_DIR = tempfile.mkdtemp(prefix="mr-test-out.")
atexit.register(shutil.rmtree, _OUT_DIR, True)
# Hermetic library root: point MEDIAREDUCER_LIBRARY at a temp dir with a "movies"
# subfolder (created BEFORE importing app/engine, which read the library root once
# at import). The save handler validates that monitored dirs exist on disk; the
# hardcoded DIRS "/library/movies" normalizes to <root>/movies, so the test no
# longer depends on a real /library mount.
_LIB_DIR = tempfile.mkdtemp(prefix="mr-test-lib.")
atexit.register(shutil.rmtree, _LIB_DIR, True)
(Path(_LIB_DIR) / "movies").mkdir(parents=True, exist_ok=True)
os.environ["MEDIAREDUCER_LIBRARY"] = _LIB_DIR
import app as A
import engine
import _tmpout
# engine fixes its path constants at import from the /config default.
_tmpout.redirect_engine(engine, _OUT_DIR)

# Seeded with OUTPUT_DIR, not left empty: output_dir() reads this dict, and
# anything resolving it before the test populates BASE (a scheduler tick, app
# startup work) would otherwise land in /config, the real data directory.
_state = {"cfg": {"OUTPUT_DIR": _OUT_DIR}}
def fake_load_config():
    return dict(_state["cfg"])
def fake_save_config(cfg, **k):
    _state["cfg"] = dict(cfg)
    return True

A.load_config = fake_load_config
A.save_config = fake_save_config
A.run_summary = lambda *a, **k: (False, "skip")
A._invalid_config_response = lambda: None
A._refresh_connection_health_cache = lambda cfg, probe=True: {
    "critical_ok": False, "tautulli_connected": False,
    "jellyfin_connected": False, "radarr_connected": False,
}
client = A.app.test_client()

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

DIRS = ["/library/movies"]
BASE_SAVED = {
    "RUN_MODE": "paused", "HEADROOM_GB": 500, "REDLINE_GB": 200,
    "REDLINE_ONLY_MODE": False,
    "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 60, "MONITOR_DIRS": DIRS,
    "USE_PLEX": False, "USE_JELLYFIN": False,
    "IMDB_RATINGS_URL": "https://example.test/r.tsv.gz",
    "OUTPUT_DIR": _OUT_DIR,
}

def expected_valid(mode, h, r, c):
    """The single source of truth all three validators must reproduce. The
    flag (mode) is derived from the value everywhere, so it cannot change a
    verdict; only an armed headroom constrains redline."""
    if h == 0:
        return True
    return r is None or r < h

def api_status_for(mode, h, r, c):
    _state["cfg"] = dict(BASE_SAVED)
    payload = dict(BASE_SAVED)
    payload.pop("OUTPUT_DIR")
    payload.update({"HEADROOM_GB": h, "REDLINE_GB": r,
                    "REDLINE_ONLY_MODE": mode, "MAX_LIBRARY_GB": c})
    resp = client.post("/api/config", json=payload, headers={"X-MediaReducer": "1"})
    return resp.status_code

_eng_saved = (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE,
              engine.MAX_LIBRARY_GB, engine.MONITOR_DIRS, engine.CONFIG_ERRORS)
try:
    engine.MONITOR_DIRS = list(DIRS)
    engine.CONFIG_ERRORS = []
    for mode in (False, True):
        for h in (0, 500):
            for r in (None, 200, 500, 900):
                for c in (None, 5000):
                    label = f"mode={mode} H={h} R={r} C={c}"
                    want = expected_valid(mode, h, r, c)

                    save_ok = api_status_for(mode, h, r, c) == 200
                    file_ok = not A._config_file_issues(
                        {"MONITOR_DIRS": DIRS, "HEADROOM_GB": h, "REDLINE_GB": r,
                         "REDLINE_ONLY_MODE": mode, "MAX_LIBRARY_GB": c})
                    engine.HEADROOM_GB, engine.REDLINE_GB = h, r
                    engine.REDLINE_ONLY_MODE, engine.MAX_LIBRARY_GB = mode, c
                    errs, _t, _m = engine._space_threshold_errors()
                    eng_ok = errs == []

                    check(f"{label}: save={'ok' if save_ok else '400'} "
                          f"file={'ok' if file_ok else 'flag'} "
                          f"engine={'ok' if eng_ok else 'flag'} — expect "
                          f"{'valid' if want else 'invalid'}",
                          save_ok == want and file_ok == want and eng_ok == want)

                    # Mode detection is value-derived: headroom off + a
                    # Redline floor (no cap key here) IS redline-only,
                    # whatever the stored flag claims.
                    check(f"{label}: mode detection",
                          A._redline_only_mode_cfg(
                              {"HEADROOM_GB": h, "REDLINE_GB": r,
                               "REDLINE_ONLY_MODE": mode}) is (h == 0 and r is not None))
finally:
    (engine.HEADROOM_GB, engine.REDLINE_GB, engine.REDLINE_ONLY_MODE,
     engine.MAX_LIBRARY_GB, engine.MONITOR_DIRS, engine.CONFIG_ERRORS) = _eng_saved

# ── Gating direction for each VALID state (60% safety cap on a 1 TB disk) ────
_disk = {"total_gb": 1000, "used_gb": 500, "free_gb": 500, "pct_used": 50}
def gate(mode, h, r, c):
    cfg = dict(BASE_SAVED, HEADROOM_GB=h, REDLINE_GB=r,
               REDLINE_ONLY_MODE=mode, MAX_LIBRARY_GB=c)
    return A._space_threshold_state(cfg, _disk, library_gb=100.0)

st = gate(False, 0, None, None)
check("no-thresholds: Cleanup blocked with setup message, Simulate open",
      st["ok_for_cleanup"] is False and "Set a Headroom target" in st["cleanup_tooltip"]
      and st["ok_for_simulate"] is True)
st = gate(False, 0, None, 5000)
check("cap-only: Cleanup allowed", st["ok_for_cleanup"] is True and st["ok_for_simulate"] is True)
st = gate(False, 500, 200, None)
check("normal + redline: Cleanup allowed", st["ok_for_cleanup"] is True)
st = gate(False, 500, None, 5000)
check("normal + cap: Cleanup allowed", st["ok_for_cleanup"] is True)
st = gate(True, 0, 200, None)
check("redline-only: Cleanup allowed (plan gate handled separately)",
      st["ok_for_cleanup"] is True)

# ── Onboarding (no dirs): the locked form's saved-through values stay valid ──
for mode, h, r, c in ((False, 0, None, None), (False, 500, 200, None)):
    issues = A._config_file_issues({"MONITOR_DIRS": [], "HEADROOM_GB": h,
                                    "REDLINE_GB": r, "REDLINE_ONLY_MODE": mode,
                                    "MAX_LIBRARY_GB": c})
    check(f"onboarding (no dirs) mode={mode} H={h} R={r}: no lockout", not issues)


# ══════════════════════════════════════════════════════════════════════════
# The Redline-below-Headroom ceiling on the dashboard (_space_threshold_state)
# must only apply while Headroom is TICKED. Unticking Headroom retires that ceiling —
# even when a Library Size Cap is ALSO armed (redline + cap), which is not a
# "redline-only" config but still has no Headroom target to sit under. Regression for
# the false "Redline must be lower than Headroom" error shown with Headroom off +
# Redline + Cap.
# ══════════════════════════════════════════════════════════════════════════

# Neutralize the DB/plan-stamp parts — this exercises the hard-error validation only.
A._full_scan_overdue = lambda: False
A._pending_plan_current = lambda *a, **k: True
A._deletion_limits_exceeded = lambda *a, **k: False
A._simulate_evidence = lambda cfg: True
A._pending_raw = lambda: None

DISK = {"total_gb": 1000, "used_gb": 400, "free_gb": 600, "pct_used": 40}

def tooltip(cfg):
    return A._space_threshold_state(cfg, DISK, library_gb=100).get("cleanup_tooltip", "")

CEIL = "lower than Headroom"

# 1. THE BUG: Headroom off (redline-only flag) + Redline + Library Size Cap.
cfg = {"REDLINE_ONLY_MODE": True, "HEADROOM_GB": 0, "REDLINE_GB": 500,
       "MAX_LIBRARY_GB": 10000, "MAX_HEADROOM_PCT": 15}
check("headroom off + redline + cap: no false 'Redline must be lower than Headroom'",
      CEIL not in tooltip(cfg))

# 2. Headroom off + Redline only (no cap): also no ceiling.
cfg2 = dict(cfg, MAX_LIBRARY_GB=None)
check("headroom off + redline only: no ceiling error", CEIL not in tooltip(cfg2))

# 3. Headroom TICKED with Redline at/above it: the ceiling error DOES still fire.
cfg3 = {"REDLINE_ONLY_MODE": False, "HEADROOM_GB": 100, "REDLINE_GB": 200,
        "MAX_LIBRARY_GB": None, "MAX_HEADROOM_PCT": 15}
check("headroom ticked + redline above it: ceiling error still fires", CEIL in tooltip(cfg3))

# 4. Headroom TICKED with Redline below it: valid.
cfg4 = dict(cfg3, HEADROOM_GB=300, REDLINE_GB=200)
check("headroom ticked + redline below it: no ceiling error", CEIL not in tooltip(cfg4))


print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
