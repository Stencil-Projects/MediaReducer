"""Everything that decides what's in the store is watched for changes.

Three inputs shape persisted data, and each is covered at the right level:

  • db.py     — schema + row<->dict mapping. Its own fingerprint; a change
                DROPs and recreates the tables at connect.
  • engine.py — everything that reads/writes/derives cached values. Its
                code_checksum wipes the code-derived rows on the next write.
  • scoring_constants.py — the retention curves. NOT hashed into either of the
                above (that would force a full API refetch for a tuning tweak),
                but the scores those curves produced ARE persisted in
                movies.score / queue.score, and the Redline fast path deletes
                worst-first by the stored score WITHOUT re-scoring. So the
                curves are fingerprinted into the deletion PLAN: a change
                stales the plan, which ghosts Cleanup/arming until a fresh
                Simulate and sends the fast path back to a full scan.

This test pins that mapping so a new input file can't quietly appear unwatched.
"""
import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-invalidation.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A
import db
import scoring_constants

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ── The watched set is exactly the modules that shape stored data ────────────
_MODULES = {p.name for p in _ROOT.glob("*.py")}
# run_issues.py is listed but deliberately NOT watched: it is the wording for
# warnings and failures, so editing a label changes what a run SAYS, never what
# it stored or how a movie scored. A new module here is a prompt to make that
# same call, which is the whole point of pinning the set.
check("the project's module set is the expected one",
      _MODULES == {"app.py", "cli.py", "db.py", "engine.py", "entrypoint.py",
                   "notify.py", "run_issues.py", "scoring_constants.py"})

# The container must ship every one of them. run_issues.py existed for a day
# without being in the Dockerfile's COPY line: every test passed (they run from
# the repo checkout, where the import always works) while the image itself
# couldn't boot. The check lives here with the module-set pin so a new module
# trips both questions at once — does it shape stored data, and does it ship.
_dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
_copied = set()
for _line in _dockerfile.splitlines():
    if _line.strip().startswith("COPY"):
        _copied.update(t for t in _line.split() if t.endswith(".py"))
_missing = sorted(m for m in _MODULES if m not in _copied)
check("the Dockerfile COPYs every module (a missing one only fails IN the container)",
      not _missing)
if _missing:
    print("   not in any COPY line:", ", ".join(_missing))

# db.py: fingerprint follows its own source.
_fp = db._schema_fingerprint()
check("db.py fingerprint is a real hash of its source",
      _fp == hashlib.sha256((_ROOT / "db.py").read_bytes()).hexdigest())

# engine.py: checksum follows its own source. Read in a subprocess so importing
# the engine here can't disturb the rest of the suite.
import subprocess
_probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import engine; print(engine.code_checksum())" % str(_ROOT)],
    capture_output=True, text=True, timeout=120, env=dict(os.environ))
check("engine.py checksum is a real hash of its source",
      _probe.stdout.strip() == hashlib.sha256((_ROOT / "engine.py").read_bytes()).hexdigest())

# scoring_constants.py: fingerprint follows the curve VALUES, not the file bytes
# (so comment edits are free) — and it is part of the plan stamp.
check("scoring fingerprint tracks the curve values",
      scoring_constants.FINGERPRINT == hashlib.sha256(
          json.dumps(scoring_constants.SCORING, sort_keys=True, default=str).encode()
      ).hexdigest()[:16])
check("the scoring curves are part of the deletion plan stamp",
      "_SCORING_CURVES" in A._PLAN_CONFIG_KEYS)

# The app and engine plan-key lists must stay identical or one side would
# consider a plan current that the other rejects.
_eng_keys = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import engine; print(','.join(engine._PLAN_CONFIG_KEYS))" % str(_ROOT)],
    capture_output=True, text=True, timeout=120, env=dict(os.environ)).stdout.strip()
check("app and engine plan-key lists are identical",
      _eng_keys.split(",") == list(A._PLAN_CONFIG_KEYS))


# ── A curve change stales the plan ───────────────────────────────────────────
# A stamp written under the CURRENT curves is current; the same stamp read back
# after the curves move is not — which is what ghosts Cleanup and sends the
# Redline fast path back to a full, re-scoring scan.
_cfg = {k: None for k in A._PLAN_CONFIG_KEYS if k != "_SCORING_CURVES"}
_cfg.update({"MONITOR_DIRS": ["/library/movies"], "OUTPUT_DIR": _OUT})
_stamp = {k: A._plan_config_value(_cfg, k) for k in A._PLAN_CONFIG_KEYS}
check("a stamp taken under the current curves carries the fingerprint",
      _stamp["_SCORING_CURVES"] == scoring_constants.FINGERPRINT)

_moved = dict(_stamp, _SCORING_CURVES="0000000000000000")
check("a stamp from different curves no longer matches",
      _moved["_SCORING_CURVES"] != A._plan_config_value(_cfg, "_SCORING_CURVES"))

# The pseudo-key must never be read from config.json — a user (or a hand edit)
# putting _SCORING_CURVES in the config must not be able to fake a fresh plan.
check("the curve fingerprint ignores any config.json value",
      A._plan_config_value({"_SCORING_CURVES": "attacker-supplied"}, "_SCORING_CURVES")
      == scoring_constants.FINGERPRINT)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
