"""The run log has to describe the WHOLE run, seasons included.

Four ways it did not, all found in one real diagnostic bundle: a 23,489 GB
library, a 5,000 GB cap (below the safety floor), 2,213 eligible movies and 41
eligible seasons worth 343 GB.

  1. The season side bailed at the safety gate BEFORE building its plan, so it
     persisted an all-zero report and the log said "Eligible: 2213" while that
     same run's Marked & Eligible window listed 2,213 movies AND 41 seasons.
     The movie side has no such bail — Simulate is exempt — so one gate
     produced two behaviours and the log described half the run.
  2. "Library cap out of reach" spent only the movie half and then drew its
     conclusion about the whole library, which the cap measures TV and all.
  3. A fail-closed TV inventory failure printed as "the movie side covered the
     target" — a server outage worded as a deliberate optimization, on a run
     that still reported COMPLETED with no issue raised.
  4. The engine matched the season report by AGE (an hour), not by run. A run
     whose season side sat out left the previous report in place and recent,
     so its counts — deleted_seasons included — were printed as this run's.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-season.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402
import run_issues  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


GB = 1_000_000_000
LIB, TOTAL = 23489.4, 53994.2
DISK = {"used_gb": 36828.9, "total_gb": TOTAL, "free_gb": 17165.4, "pct_used": 68.2}

A.output_dir = lambda: Path(_OUT)
A.disk_stats = lambda: dict(DISK)
A.cached_disk_stats = lambda s=None: dict(DISK)
A.library_stats = lambda: {"library_gb": LIB}
A._stamp_tv_share = lambda b: None
A._save_tv_cleanup_state = lambda st: _state.update(st)
A._tv_cleanup_state = lambda: dict(_state)
_state = {"marked": {}}

# 41 seasons, 343.0 GB — the season order from the real bundle.
SEASONS = [{"title": f"Show {i}", "season": 1, "sid": f"s{i}", "path": f"/library/TV Shows/Show {i}",
            "size_bytes": int(8.366 * GB), "eps": 10, "eps_watched": 0,
            "last_played": 0, "score": 20.0 + i, "take": False} for i in range(41)]
ROWS = [{"title": f"Show {i}", "tv_seasons": [{"n": 1}]} for i in range(41)]

A._tv_fresh_rows_strict = lambda cfg: list(ROWS)
A._tv_season_plan = lambda rows, cfg, now=None: {"order": list(SEASONS), "excluded": {}}
A._merged_pool_takes = lambda order, cfg: (
    {}, {"target_bytes": 0, "tv_share_bytes": 0, "movie_share_bytes": 0})

CFG = {"OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/Movies HD", "/library/TV Shows"],
       "MAX_HEADROOM_PCT": 15, "HEADROOM_GB": 0, "REDLINE_GB": None,
       "MAX_LIBRARY_GB": 5000, "TV_CLEANUP_ENABLED": True, "MOVIE_CLEANUP_ENABLED": True,
       "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k"}

# ── 1. A safety-blocked run still reports what it planned ─────────────────
# The 5,000 cap is far below the 19,966 floor, so the gate trips — and the
# season half must still say 41, exactly as the movie half still says 2,213.
A._space_threshold_state = lambda *a, **k: {"safety_blocked": True}
rep = A._run_tv_cleanup_pass(dict(CFG), execute=False, run_started_at=1000.5)
check("a safety-blocked run still counts the seasons it saw",
      rep["seasons_seen"] == 41, rep["seasons_seen"])
check("...and still reports them as eligible, like the movie side does",
      rep["eligible_seasons"] == 41, rep["eligible_seasons"])
check("...with the bytes behind that count",
      round(rep["eligible_season_bytes"] / 1e9, 1) == 343.0,
      rep.get("eligible_season_bytes"))
check("...while still marking nothing", rep["blocked_by_safety"] and not rep["marked_new"])

# ── The TV switch is a different answer: planned, but NOT eligible ────────
# Both numbers at once would have the log say 41 seasons are held back by the
# switch AND 41 are eligible.
A._space_threshold_state = lambda *a, **k: {"safety_blocked": False}
rep_off = A._run_tv_cleanup_pass({**CFG, "TV_CLEANUP_ENABLED": False},
                                 execute=False, run_started_at=1000.5)
check("the switch reports seasons as held back, not eligible",
      rep_off["cleanup_off"] == 41 and rep_off["eligible_seasons"] == 0,
      (rep_off["cleanup_off"], rep_off["eligible_seasons"]))
check("...and claims no bytes for the cap arithmetic",
      rep_off["eligible_season_bytes"] == 0)

# ── The overshoot note speaks only for THIS pass's marks ──────────────────
# A carried season mark was made by an earlier pass; sizing the note from the
# whole share re-announced "marked 343.0 GB" every pass until the marks aged
# out. First pass (marks made): the note speaks. Second pass (all carried):
# silence — nothing new happened.
A._space_threshold_state = lambda *a, **k: {"safety_blocked": False}
BIG = [SEASONS[0] | {"size_bytes": 42 * GB}]
A._tv_season_plan = lambda rows, cfg, now=None: {"order": list(BIG), "excluded": {}}
A._merged_pool_takes = lambda order, cfg: (
    {A._tv_mark_key(BIG[0]): BIG[0]},
    {"target_bytes": 2 * GB, "tv_share_bytes": 42 * GB, "movie_share_bytes": 0})
_state.clear(); _state.update({"marked": {}})
rep1 = A._run_tv_cleanup_pass(dict(CFG), execute=False, run_started_at=1000.5)
check("the pass that MADE the mark says so",
      "marked 42.0 GB" in (rep1.get("overshoot_note") or ""), rep1.get("overshoot_note"))
rep2 = A._run_tv_cleanup_pass(dict(CFG), execute=False, run_started_at=1001.5)
check("a pass that only CARRIED it stays quiet",
      not rep2.get("overshoot_note"), rep2.get("overshoot_note"))
A._tv_season_plan = lambda rows, cfg, now=None: {"order": list(SEASONS), "excluded": {}}
A._merged_pool_takes = lambda order, cfg: (
    {}, {"target_bytes": 0, "tv_share_bytes": 0, "movie_share_bytes": 0})

# ── 4. The report is matched to a RUN, not to a clock ─────────────────────
check("the report is stamped with the run it belongs to",
      rep["run_started_at"] == 1000.5, rep.get("run_started_at"))

import engine  # noqa: E402

_meta = {}
engine.db = type("D", (), {
    "connect": staticmethod(lambda p: __import__("contextlib").nullcontext(None)),
    "get_meta": staticmethod(lambda conn, k: _meta.get(k)),
})()

_meta["tv_cleanup"] = {"last_pass": {"at": time.time(), "run_started_at": 1000.5,
                                     "seasons_seen": 41, "eligible_seasons": 41,
                                     "deleted_seasons": ["a", "b"]}}
engine._run_started_at = lambda: 1000.5
check("this run's season report is read", engine._season_side_report().get("seasons_seen") == 41)

# The previous run's report is SECONDS old — an age window would hand it over.
engine._run_started_at = lambda: 2000.75
got = engine._season_side_report()
check("a previous run's report is not adopted as this run's", got == {}, got)
check("...so its season deletions cannot land under this run's Deleted line",
      not got.get("deleted_seasons"))

# A report with no run stamp (written before this fix) is not guessed at.
_meta["tv_cleanup"] = {"last_pass": {"at": time.time(), "seasons_seen": 41}}
engine._run_started_at = lambda: 2000.75
check("an unstamped report is ignored rather than assumed current",
      engine._season_side_report() == {})

# ── 2 & 3. The two messages ───────────────────────────────────────────────
check("a fail-closed TV inventory failure is a registered issue",
      "tv_inventory_unavailable" in run_issues.CATEGORIES)
check("...raised as an error, not a footnote",
      run_issues.severity_of("tv_inventory_unavailable") == run_issues.ERROR)
_note = run_issues.CATEGORIES["tv_inventory_unavailable"]["note"]
check("...and its note says no season was planned or deleted",
      "no season was planned or deleted" in _note, _note)
_cap = run_issues.CATEGORIES["cap_unreachable"]["note"]
check("the cap-unreachable note accounts for seasons too",
      "movie and season" in _cap, _cap)

src = (ROOT / "engine.py").read_text(encoding="utf-8")
# The emitted LINE, not the phrase: the old wording survives in the comment
# above the fix explaining what it used to say and why that was wrong.
check("the abort line no longer reads as a deliberate optimization",
      "aborted']} — the movie side covered the target" not in src)
check("...and names the real cause instead",
      "TV inventory unavailable — no seasons were planned" in src)
# The expression itself, not just the name: a revert that leaves _season_gb
# assigned but drops it from the subtraction is exactly the bug, and a check
# for the identifier alone still passes on it.
check("cap-unreachable spends the eligible season bytes",
      "_lib_after = effective_library_gb - bytes_to_gb(freed_bytes) - _season_gb" in src)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
