"""A config key that is ABSENT must never arm a deletion trigger.

engine.py reads config.json with `if "KEY" in cfg:` for every setting, so a key
that is missing keeps the module-level default. Those defaults were once example
values written before default_config.json existed — 1000 GB of headroom and a
200 GB Redline floor — which meant a config.json that lost those lines to a
hand-edit, a truncated write, or an install predating them came up ARMED to
delete against a 1 TB free-space target, on a fresh file whose shipped defaults
disarm everything.

Absent is the state nobody chose. It has to mean off.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_TD = tempfile.mkdtemp(prefix="mr-absent.")
Path(_TD, "config.json").write_text(json.dumps({"OUTPUT_DIR": _TD}))
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_TD, "config.json"))
os.environ["MEDIAREDUCER_LIBRARY"] = _TD
sys.path.insert(0, str(ROOT))
import engine as E

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {extra}"))
    ok = ok and cond

SHIPPED = json.loads((ROOT / "default_config.json").read_text())

# The keys that arm a deletion. Each is read behind `if "KEY" in cfg`, so the
# module default IS the behaviour for a config that does not mention it.
ARMING = ("HEADROOM_GB", "REDLINE_GB", "MAX_LIBRARY_GB", "REDLINE_ONLY_MODE")

# A config that names nothing but OUTPUT_DIR: the engine's own defaults, live.
E._load_config_from_file()
for key in ARMING:
    check(f"{key} absent from config.json matches the shipped default",
          getattr(E, key) == SHIPPED[key],
          f"engine={getattr(E, key)!r} shipped={SHIPPED[key]!r}")

# …and the point of all four together: nothing is armed, so nothing deletes.
check("no threshold is armed", not E.HEADROOM_GB and E.REDLINE_GB is None
      and E.MAX_LIBRARY_GB is None)

# The same guarantee stated the other way, so a NEW arming key added later
# without a matching module default is caught here rather than in someone's
# library. Any key whose shipped value disarms must not default to a live one.
DISARMED = {k: v for k, v in SHIPPED.items() if k in ARMING}
armed_by_default = [k for k, v in DISARMED.items()
                    if getattr(E, k, v) != v]
check("no arming key defaults to something the shipped config does not",
      not armed_by_default, armed_by_default)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
