"""Write the tie-group picks engine._pop_from_tie_group makes, for
tiegroup_parity.cjs.

The Score Explorer replays the engine's deletion loop in JavaScript so the
preview can name what a run would remove next. engine.py says so itself, above
NEAR_TIE_PTS: "the Score Explorer's popNextDeletion() mirrors the logic below;
change one side only and the preview diverges from the run." Nothing was
checking it, and it had diverged — the mirror still picked the LOWEST-SCORING
member that covers the target, which is what the engine deliberately stopped
doing in favour of the SMALLEST that covers.

The grid is built to separate the two rules: every case has a group whose
members differ in both size and score, so a pick by score and a pick by size
name different movies.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import engine  # noqa: E402

GB = 1024 ** 3


def member(title, size_gb, score):
    return {"title": title, "file_size": int(size_gb * GB), "retention_score": score}


GROUPS = [
    # The case from the engine's own docstring: a huge low-scoring file and a
    # small higher-scoring one, both covering a small remainder.
    ("docstring case: 42 GB @10.0 vs 4 GB @11.0, need 2 GB",
     [member("Big Low", 42, 10.0), member("Small High", 4, 11.0)], 2),
    # ...and the same group against a target only the big one covers.
    ("only the big one covers",
     [member("Big Low", 42, 10.0), member("Small High", 4, 11.0)], 10),
    # Nothing covers: largest goes first, score breaking a size tie.
    ("nothing covers",
     [member("A", 3, 10.0), member("B", 5, 11.0), member("C", 5, 10.5)], 40),
    # ...and with size and score both tied there too, the earliest member wins.
    # Named so that a title tiebreak — which this arm also has to not have —
    # would pick the other one.
    ("nothing covers, size and score tie, alphabetically reversed",
     [member("Zebra", 5, 10.0), member("Alpha", 5, 10.0)], 40),
    # Several cover; the smallest wins regardless of where its score sits.
    ("three cover, smallest is the highest-scoring",
     [member("A", 20, 10.0), member("B", 12, 10.9), member("C", 8, 11.8)], 6),
    ("three cover, smallest is the lowest-scoring",
     [member("A", 20, 11.8), member("B", 12, 10.9), member("C", 8, 10.0)], 6),
    # Exact size tie among coverers: lower score wins.
    ("size tie among coverers",
     [member("A", 10, 11.0), member("B", 10, 10.0)], 5),
    # Exact size AND score tie: the earliest member wins (plan order), which is
    # what dropping the title tiebreak buys.
    ("size and score both tie — earliest wins",
     [member("Zebra", 10, 10.0), member("Alpha", 10, 10.0)], 5),
    ("size and score tie, alphabetical order reversed",
     [member("Alpha", 10, 10.0), member("Zebra", 10, 10.0)], 5),
    # Exactly-covers boundary: size >= remaining, not >.
    ("member covers the remainder exactly",
     [member("Exact", 6, 11.0), member("Bigger", 9, 10.0)], 6),
    ("single member", [member("Only", 7, 10.0)], 3),
]

CASES = []
for name, group, remaining_gb in GROUPS:
    picked = engine._pop_from_tie_group(
        [dict(m) for m in group], int(remaining_gb * GB), quiet=True)
    CASES.append({
        "name": name,
        "group": [{"title": m["title"], "sizeGb": m["file_size"] / GB,
                   "retention": m["retention_score"]} for m in group],
        "remainingGb": remaining_gb,
        "picked": picked["title"],
    })

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "py_tiegroup.json").write_text(json.dumps(CASES, indent=1), encoding="utf-8")
print(f"wrote {out_dir}/py_tiegroup.json ({len(CASES)} cases)")
