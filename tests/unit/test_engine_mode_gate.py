"""The engine's last line of defense: main() refuses to proceed unless RUN_MODE
is one of EXECUTABLE_RUN_MODES.

This is the guard that makes a paused scheduler safe even if something upstream
launches the engine anyway — a direct/cron invocation, a stale mode written by
another process, or a config edited mid-flight. Everything else (the app's tick
guards, the mode lifecycle) is defense in depth ABOVE this; if this gate ever
falls through, "Paused" and "Monitor Only" would run the live deletion path.

Run as a subprocess against a real config so the gate is exercised exactly as
production hits it, rather than through a stub.
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-mode-gate.")
atexit.register(shutil.rmtree, _OUT, True)

import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


def run_engine(mode):
    """Launch engine.py with RUN_MODE=mode and return (returncode, output)."""
    cfg_path = Path(_OUT, f"config-{mode}.json")
    out_dir = Path(_OUT, f"out-{mode}")
    out_dir.mkdir(exist_ok=True)
    cfg_path.write_text(json.dumps({
        "RUN_MODE": mode, "OUTPUT_DIR": str(out_dir),
        "MONITOR_DIRS": [], "USE_PLEX": False, "USE_JELLYFIN": False,
        "HEADROOM_GB": 0, "REDLINE_GB": None, "MAX_LIBRARY_GB": None,
    }))
    env = dict(os.environ, MEDIAREDUCER_CONFIG=str(cfg_path),
               MEDIAREDUCER_LIBRARY=str(out_dir))
    env.pop("MEDIAREDUCER_MODE_OVERRIDE", None)
    p = subprocess.run([sys.executable, "-u", str(_ROOT / "engine.py")],
                       env=env, capture_output=True, text=True, timeout=120)
    log = Path(out_dir, "lastrun.log")
    return p.returncode, (log.read_text() if log.exists() else "") + p.stdout + p.stderr


# The non-executable modes are exactly the ones the scheduler rests in.
for _mode in ("paused", "off"):
    rc, out = run_engine(_mode)
    check(f"RUN_MODE={_mode!r} exits cleanly", rc == 0)
    check(f"RUN_MODE={_mode!r} reports itself as not executable",
          "not an executable mode" in out)
    # The live path announces itself; none of it may appear.
    check(f"RUN_MODE={_mode!r} never reaches the scan/delete path",
          "Reading library" not in out and "DELETE #" not in out
          and "DRY RUN DELETE" not in out)

# A typo or hand-edited garbage mode is refused the same way — the gate is a
# whitelist, not a blacklist of known-bad values.
rc, out = run_engine("headroom_typo")
check("an unrecognized mode is refused too",
      rc == 0 and "not an executable mode" in out)

# And the whitelist still contains every mode the app actually launches.
check("the executable whitelist covers the app's launch modes",
      set(E.EXECUTABLE_RUN_MODES) ==
      {"debug_info", "debug_sim", "headroom", "debug_cleanup", "reconcile"})
check("the resting modes are NOT in the whitelist",
      "paused" not in E.EXECUTABLE_RUN_MODES and "off" not in E.EXECUTABLE_RUN_MODES)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
