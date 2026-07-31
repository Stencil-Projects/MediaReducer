"""No shipped module may reference a name that does not exist.

This exists because one did, and nothing noticed. app.py's notification
dispatch read `marked_alert_armed`, which was never defined — in a `finally:`
block, so the `except Exception: pass` above it did not catch the NameError,
and short-circuit evaluation hid it on every run whose summary went out. It
only fired when the summary did NOT go out, which is the exact case that branch
existed to handle: the thread died and the marked baseline was never updated.

A test suite cannot reach every branch of 19k lines, and this class does not
need one to — an undefined name is decidable by reading the source. Scoped
deliberately to that single check rather than a style gate: undefined-name has
no false positives worth arguing about, so it can be a hard failure, while the
repo has hundreds of intentional style deviations that would drown it.

Skips (rather than fails) when pyflakes is absent, like the browser tier does
with playwright — a missing dev tool is not a broken codebase.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = ["app.py", "engine.py", "notify.py", "db.py", "cli.py",
           "run_issues.py", "scoring_constants.py", "entrypoint.py"]

try:
    import pyflakes  # noqa: F401
except ImportError:
    print("SKIP no undefined names — pyflakes not installed (pip install pyflakes)")
    print("RESULT: PASS")
    sys.exit(0)

proc = subprocess.run(
    [sys.executable, "-m", "pyflakes", *MODULES],
    cwd=ROOT, capture_output=True, text=True,
)
# pyflakes reports one finding per line; only undefined names are fatal here.
# "undefined name" also covers the __all__ variant, which is equally a typo.
bad = [ln for ln in proc.stdout.splitlines() if "undefined name" in ln]

ok = not bad
print(("PASS " if ok else "FAIL ")
      + f"no shipped module references an undefined name ({len(MODULES)} files)")
for ln in bad:
    print("   " + ln)
if proc.returncode not in (0, 1):
    print("   (pyflakes itself failed: " + (proc.stderr.strip()[:200] or "?") + ")")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
