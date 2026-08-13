"""Write the deficit grid shared.pool_deficit_gb produces, for deficit_parity.cjs.

Companion to gen_py_scores.py: Python computes, JavaScript replays, the harness
compares. The grid deliberately includes the shapes that separate the three
arms — headroom breached alone, cap breached alone, both at once with different
sizes, redline crossed, and the unreadable-disk case the dashboard answers with
"unknown".
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import shared  # noqa: E402

CASES = []


def case(name, *, total, used, free, library_gb, headroom_gb, redline_gb, cap_gb):
    CASES.append({
        "name": name,
        "disk": {"total_gb": total, "used_gb": used, "free_gb": free},
        "library_gb": library_gb, "headroom_gb": headroom_gb,
        "redline_gb": redline_gb, "cap_gb": cap_gb,
        # The dashboard measures headroom against (total - headroom), which is
        # the used-space limit shared.py takes directly.
        "py_deficit": shared.pool_deficit_gb(
            used, (total - headroom_gb) if headroom_gb else None, library_gb, cap_gb),
    })


case("limits satisfied", total=1000, used=400, free=600,
     library_gb=300, headroom_gb=100, redline_gb=50, cap_gb=500)
case("headroom breached alone", total=1000, used=950, free=50,
     library_gb=300, headroom_gb=100, redline_gb=None, cap_gb=None)
case("cap breached alone", total=1000, used=400, free=600,
     library_gb=800, headroom_gb=None, redline_gb=None, cap_gb=500)
case("both breached, headroom larger", total=1000, used=980, free=20,
     library_gb=560, headroom_gb=100, redline_gb=None, cap_gb=500)
case("both breached, cap larger", total=1000, used=920, free=80,
     library_gb=900, headroom_gb=100, redline_gb=None, cap_gb=500)
case("redline crossed, nothing else", total=1000, used=970, free=30,
     library_gb=300, headroom_gb=None, redline_gb=50, cap_gb=None)
case("redline crossed with headroom too", total=1000, used=970, free=30,
     library_gb=300, headroom_gb=100, redline_gb=50, cap_gb=None)
case("free exactly on the redline floor", total=1000, used=950, free=50,
     library_gb=300, headroom_gb=None, redline_gb=50, cap_gb=None)
case("redline-only mode, above the floor", total=1000, used=400, free=600,
     library_gb=300, headroom_gb=0, redline_gb=50, cap_gb=None)
case("cap set but library size unknown", total=1000, used=400, free=600,
     library_gb=0, headroom_gb=None, redline_gb=None, cap_gb=500)
case("fractional figures", total=53994.2, used=36811.8, free=17182.5,
     library_gb=23472.0, headroom_gb=1000, redline_gb=500, cap_gb=20000)

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "py_deficits.json").write_text(json.dumps(CASES, indent=1), encoding="utf-8")
print(f"wrote {out_dir}/py_deficits.json ({len(CASES)} cases)")
