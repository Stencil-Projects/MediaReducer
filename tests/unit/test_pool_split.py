"""The one-pool split: movies and seasons in ONE deletion order, one deficit,
two executors that never double-cover it.

App side (_merged_pool_takes): the engine's movie queue and the season order
merge worst-first on the shared 0-100 score; the covering prefix decides which
seasons the season side takes and how many bytes each side frees. Engine side
(_tv_share_bytes / _daily_deficit_bytes): the stamped season share is
subtracted from the movie target while fresh, and a stale stamp (>26h — the TV
pass stopped running) reads as 0 so the movie side covers everything. That
fail direction is the safety contract pinned here.

Hermetic: temp store, stubbed disk/library measurements, stubbed movie queue.
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

# ── The merged order and the split ──────────────────────────────────────────
# Pool deficit 10 GB (library 110 against a 100 GB cap; headroom off). The
# movie queue holds scores 5 and 20 (4 GB each); the seasons score 1 and 30
# (5 GB each). Worst-first that merges to: season(1), movie(5), movie(20),
# season(30) — the 10 GB prefix is season(1) + movie(5) + movie(20), so the
# The season side takes ONE season (5 GB) and the movies cover 8 GB.
A.disk_stats = lambda: {"used_gb": 500.0, "total_gb": 10_000.0, "free_gb": 9_500.0}
A.library_stats = lambda: {"library_gb": 110.0}
db.read_pending_doc = lambda p: {"entries": {
    "/m/a.mkv": {"score": 5.0, "size_bytes": 4 * GB},
    "/m/b.mkv": {"score": 20.0, "size_bytes": 4 * GB},
}}

def season(score, n):
    return {"title": f"Show {n}", "season": n, "sid": f"sonarr:{n}",
            "path": f"/tv/Show {n}", "size_bytes": 5 * GB, "eps": 10,
            "eps_watched": 0, "last_played": 0, "score": score, "take": False}

CFG = {"MAX_LIBRARY_GB": 100, "HEADROOM_GB": 0}
order = [season(1.0, 1), season(30.0, 2)]
takes, share = A._merged_pool_takes(order, CFG)

check("the pool deficit is the cap overage", share["target_bytes"] == 10 * GB, share)
check("the worst season is inside the covering prefix — taken",
      list(takes) == ["sonarr:1|S1"] and order[0]["take"] is True, takes)
check("the better season sits behind two movies and is spared",
      order[1]["take"] is False, order)
check("the season share is exactly the taken season's bytes",
      share["tv_share_bytes"] == 5 * GB, share)
check("the movie share is the movie bytes inside the prefix",
      share["movie_share_bytes"] == 8 * GB, share)
check("together the shares cover the deficit",
      share["tv_share_bytes"] + share["movie_share_bytes"] >= share["target_bytes"], share)

# Takes are always a PREFIX of the season order (a merged order can interleave
# movies between them, but never skip a worse season for a better one).
big = [season(float(s), i) for i, s in enumerate([0, 2, 8, 40, 90])]
A.library_stats = lambda: {"library_gb": 112.0}
A._merged_pool_takes(big, CFG)
flags = [e["take"] for e in big]
check("takes are a prefix of the season order",
      flags == sorted(flags, reverse=True), flags)

# A season the pass could never act on (no server id / no resolved folder)
# claims NO share bytes: the engine would subtract them from the movie target
# and nobody would ever free them.
A.library_stats = lambda: {"library_gb": 110.0}
db.read_pending_doc = lambda p: {}
sidless = season(1.0, 1)
sidless["sid"] = None
takes_x, share_x = A._merged_pool_takes([sidless, season(30.0, 2)], CFG)
check("a sid-less season is skipped, never counted toward the TV share",
      list(takes_x) == ["sonarr:2|S2"] and share_x["tv_share_bytes"] == 5 * GB
      and sidless["take"] is False, share_x)

# Within its limits: no deficit, nothing taken, both shares zero.
A.library_stats = lambda: {"library_gb": 90.0}
takes0, share0 = A._merged_pool_takes([season(1.0, 1)], CFG)
check("a pool within its cap takes nothing",
      not takes0 and share0["target_bytes"] == 0
      and share0["tv_share_bytes"] == 0, share0)

# No movie plan yet: seasons alone cover what they can.
A.library_stats = lambda: {"library_gb": 110.0}
db.read_pending_doc = lambda p: {}
takes_solo, share_solo = A._merged_pool_takes([season(1.0, 1), season(30.0, 2)], CFG)
check("with no movie queue the seasons cover the deficit alone",
      len(takes_solo) == 2 and share_solo["tv_share_bytes"] == 10 * GB
      and share_solo["movie_share_bytes"] == 0, share_solo)

# ── The engine's side of the handshake ──────────────────────────────────────
# The app and engine share the store; the stamp travels through db meta. The
# engine binds DB_FILE when it loads the config, exactly as a run does.
E._load_config_from_file()
check("app and engine agree on the store file",
      str(E.DB_FILE) == str(A.db_path()), (str(E.DB_FILE), str(A.db_path())))

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
