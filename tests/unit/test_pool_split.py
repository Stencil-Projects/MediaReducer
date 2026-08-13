"""The one-pool split: movies and seasons in ONE deletion order, one deficit,
two executors that never double-cover it.

The split is computed by the ENGINE from the run's own numbers on both sides
(_split_pool_with_seasons): the fresh scan's scored candidates merge with the
season order the app's season side stamped minutes earlier, worst-first on the
shared 0-100 scale, and the covering prefix decides which seasons the season
side takes and how many bytes the movie side targets. The app executes those
takes on its next pass (_engine_takes_for_pass), intersected with today's
eligible plan and truncated to today's deficit.

The identity and freshness gates ARE the safety contract, pinned here: a
season order from another run reads as no seasons (a standalone engine run
must never claim bytes the app-side executor will not free), and a stale
share/takes stamp (>26h — season handling stopped) reads as 0 so the movie
side covers everything.

Hermetic: temp store, stubbed disk/library measurements.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-pool.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}))
import app as A      # noqa: E402
import engine as E   # noqa: E402
import db            # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

GB = 1_000_000_000

# The engine binds DB_FILE when it loads the config, exactly as a run does,
# and both sides pair the order to the run by the same identity stamp.
E._load_config_from_file()
check("app and engine agree on the store file",
      str(E.DB_FILE) == str(A.db_path()), (str(E.DB_FILE), str(A.db_path())))
RUN = 1000.5
os.environ["MEDIAREDUCER_RUN_STARTED_AT"] = repr(RUN)
E.NEAR_TIE_PTS = 2.0


def season(score, n, gb=5):
    return {"title": f"Show {n}", "season": n, "sid": f"sonarr:{n}",
            "path": f"/tv/Show {n}", "size_bytes": int(gb * GB), "eps": 10,
            "eps_watched": 0, "last_played": 0, "score": score, "take": False}


def movie(score, gb):
    return {"retention_score": score, "file_size": int(gb * GB)}


def stamp_order(order, run_started_at=RUN):
    A._stamp_tv_season_order(order, run_started_at)


# ── The merged order and the split ──────────────────────────────────────────
# Deficit 10 GB. Movies score 5 and 20 (4 GB each); seasons score 1 and 30
# (5 GB each). Worst-first merges to: season(1), movie(5), movie(20),
# season(30) — the 10 GB covering prefix is season(1) + both movies, so the
# season side takes ONE season (5 GB) and the movies cover the rest.
stamp_order([season(1.0, 1), season(30.0, 2)])
share, takes = E._split_pool_with_seasons([movie(5.0, 4), movie(20.0, 4)], 10 * GB)
check("the worst season is inside the covering prefix — taken",
      [t["key"] for t in (takes or [])] == ["sonarr:1|S1"], takes)
check("the season share is exactly the taken season's bytes",
      share == 5 * GB, share)

# Takes are always a PREFIX of the season order (a merged order can interleave
# movies between them, but never skip a worse season for a better one).
big = [season(float(s), i) for i, s in enumerate([0, 2, 8, 40, 90])]
stamp_order(big)
_, takes_big = E._split_pool_with_seasons([], 12 * GB)
keys = [t["key"] for t in takes_big]
check("takes are a prefix of the season order",
      keys == [f"sonarr:{i}|S{i}" for i in range(len(keys))], keys)
check("with no movie candidates the seasons cover the deficit alone",
      sum(t["size_bytes"] for t in takes_big) >= 12 * GB, takes_big)

# An EXACT score tie sorts the LARGER item first — fewest units disturbed.
# Visible with the near-tie window off (on, the window's least-wasteful pick
# takes over at the boundary, which test_pool_least_waste pins).
E.NEAR_TIE_PTS = None
stamp_order([season(10.0, 7, gb=6)])
share_tie, takes_tie = E._split_pool_with_seasons([movie(10.0, 4)], 3 * GB)
check("an exact score tie is taken larger-first, so one item covers the deficit",
      share_tie == 6 * GB and len(takes_tie) == 1, (share_tie, takes_tie))
E.NEAR_TIE_PTS = 2.0

# ── The identity gates: who may claim season bytes ──────────────────────────
# An order stamped by a DIFFERENT run is not this run's plan: a standalone
# engine run (cron/CLI — no app-side pass) must merge movies-only, and it must
# NOT overwrite the takes the app is going to execute.
stamp_order([season(1.0, 1)], run_started_at=999.25)
share_x, takes_x = E._split_pool_with_seasons([movie(5.0, 4)], 10 * GB)
check("a season order from another run reads as no seasons",
      share_x == 0 and takes_x is None, (share_x, takes_x))
with db.transaction(A.db_path()) as conn:
    db.set_meta(conn, "tv_season_order", None)
share_n, takes_n = E._split_pool_with_seasons([movie(5.0, 4)], 10 * GB)
check("no season order at all reads the same way",
      share_n == 0 and takes_n is None, (share_n, takes_n))

# An order THIS run stamped with no deficit behind it takes nothing — and the
# empty takes are a real answer (stamped), so yesterday's marks unmark.
stamp_order([season(1.0, 1)])
share_0, takes_0 = E._split_pool_with_seasons([movie(5.0, 4)], 0)
check("a pool within its limits takes nothing, as a stamped answer",
      share_0 == 0 and takes_0 == [], (share_0, takes_0))

# A season the pass could never act on (no server id / no resolved folder) is
# excluded at STAMP time — it must never claim share bytes the movie side
# would then leave uncovered.
sidless = season(1.0, 1)
sidless["sid"] = None
stamp_order([sidless, season(30.0, 2)])
order_doc = None
with db.connect(A.db_path()) as conn:
    order_doc = db.get_meta(conn, "tv_season_order")
check("an unactionable season is never stamped into the order",
      [e["key"] for e in order_doc["entries"]] == ["sonarr:2|S2"], order_doc)

# ── The app executes the engine's takes ─────────────────────────────────────
A.disk_stats = lambda: {"used_gb": 500.0, "total_gb": 10_000.0, "free_gb": 9_500.0}
A.library_stats = lambda: {"library_gb": 110.0}
CFG = {"MAX_LIBRARY_GB": 100, "HEADROOM_GB": 0}   # 10 GB cap deficit

plan_order = [season(1.0, 1), season(2.0, 2), season(3.0, 3)]
E._stamp_tv_takes([{"key": "sonarr:1|S1", "size_bytes": 5 * GB},
                   {"key": "sonarr:9|S9", "size_bytes": 5 * GB},
                   {"key": "sonarr:2|S2", "size_bytes": 5 * GB},
                   {"key": "sonarr:3|S3", "size_bytes": 5 * GB}], 20 * GB)
takes_app, pool = A._engine_takes_for_pass(plan_order, CFG)
check("the pass executes the stamped takes in order",
      list(takes_app) == ["sonarr:1|S1", "sonarr:2|S2"]
      and plan_order[0]["take"] and plan_order[1]["take"], takes_app)
check("a take that left today's eligible plan is skipped, never re-marked",
      "sonarr:9|S9" not in takes_app, takes_app)
check("the tail past today's deficit unmarks — the set mirrors the need",
      not plan_order[2]["take"] and pool["tv_share_bytes"] == 10 * GB, pool)
check("the pool figures describe today, not stamp time",
      pool["target_bytes"] == 10 * GB and pool["movie_share_bytes"] == 0, pool)

# A stale takes stamp is stopped season handling: nothing executes.
with db.transaction(A.db_path()) as conn:
    db.set_meta(conn, "tv_takes", {"run_started_at": RUN,
                                   "at": time.time() - 27 * 3600,
                                   "entries": [{"key": "sonarr:1|S1",
                                                "size_bytes": 5 * GB}]})
for e in plan_order:
    e["take"] = False
takes_stale, pool_stale = A._engine_takes_for_pass(plan_order, CFG)
check("takes older than 26h execute nothing",
      not takes_stale and pool_stale["tv_share_bytes"] == 0, pool_stale)

# Within limits nothing executes, whatever the stamp says.
E._stamp_tv_takes([{"key": "sonarr:1|S1", "size_bytes": 5 * GB}], 5 * GB)
A.library_stats = lambda: {"library_gb": 90.0}
takes_lim, pool_lim = A._engine_takes_for_pass(plan_order, CFG)
check("a pool within its limits executes no takes",
      not takes_lim and pool_lim["target_bytes"] == 0, pool_lim)
A.library_stats = lambda: {"library_gb": 110.0}

# ── The run's own target follows ITS merge, not the stamp ───────────────────
# The fresh share replaces the pre-scan stamp subtraction for the daily
# target; a Simulate still previews the Redline deficit combined in, and a
# share above the deficit clamps at zero rather than going negative.
gb, by = E._movie_target_after_split(10 * GB, 4 * GB, 0.0, False)
check("the daily movie target is the deficit minus the fresh share",
      by == 6 * GB, by)
gb, by = E._movie_target_after_split(10 * GB, 4 * GB, 8.0, True)
check("a Simulate still previews the larger of the remainder and Redline",
      by == 8 * GB, by)
gb, by = E._movie_target_after_split(10 * GB, 50 * GB, 0.0, False)
check("a share above the deficit clamps the movie target at zero", by == 0, by)
# The wiring, not just the arithmetic: main() applies the fresh merge to
# to_free between the scan and the run paths (a stamped-but-unapplied merge
# would quietly fall back to the stale pre-scan subtraction).
import inspect  # noqa: E402
_main_src = inspect.getsource(E.main)
check("main() recomputes the target from the fresh split",
      "_split_pool_with_seasons" in _main_src
      and "_movie_target_after_split" in _main_src
      and "_stamp_tv_takes" in _main_src, None)

# ── The engine's share subtraction (the between-run handshake) ──────────────
E.MAX_LIBRARY_GB = 100
A._stamp_tv_share(4 * GB)
check("a fresh stamp reads back", E._tv_share_bytes() == 4 * GB, E._tv_share_bytes())
check("the engine subtracts the season share from the movie target",
      E._daily_deficit_bytes(500.0, 10_000.0, 110.0) == 6 * GB,
      E._daily_deficit_bytes(500.0, 10_000.0, 110.0))

# A stale stamp is stopped season handling — the movie side must cover everything.
with db.transaction(A.db_path()) as conn:
    db.set_meta(conn, "tv_share", {"bytes": 4 * GB, "at": time.time() - 27 * 3600})
check("a stamp older than 26h reads as 0", E._tv_share_bytes() == 0)
check("...so the movie target is the whole deficit again",
      E._daily_deficit_bytes(500.0, 10_000.0, 110.0) == 10 * GB)

# The share never drives the target below zero, and an absent stamp is 0.
A._stamp_tv_share(50 * GB)
check("a share above the deficit clamps the movie target at 0",
      E._daily_deficit_bytes(500.0, 10_000.0, 110.0) == 0)
with db.transaction(A.db_path()) as conn:
    db.set_meta(conn, "tv_share", None)
check("no stamp at all reads as 0", E._tv_share_bytes() == 0)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
