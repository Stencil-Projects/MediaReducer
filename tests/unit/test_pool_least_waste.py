"""At the deletion boundary, the cheapest cover wins — movie or season alike.

Score decides which items reach the boundary; this decides only which of
several equally-scored ones is the cheapest way to finish the job. Without a
size-aware step at the merged pool's boundary, the type with the bigger
deletion unit — always TV — overshoots every small deficit by a whole season
while a near-tied movie would cover it with a fraction of the waste. And the
movie loop's own boundary must pick the smallest COVER, not the lowest-scoring
one: inside a band whose whole premise is "these scores are equivalent", a
fraction of a point must not send a 42 GB file to cover a 2 GB need.

What must NOT change: score still governs everything outside the band. A
season that scores well above the movies in front of it is not deletable
merely because it would waste fewer bytes.

The split under test is the engine's (_split_pool_with_seasons) — computed
from the run's fresh scan against the season order the app stamped for it.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
_tmpout.config()
import app as A                                    # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)
E.log = lambda *a, **k: None

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


GB = 1_000_000_000
RUN = 1000.5
os.environ["MEDIAREDUCER_RUN_STARTED_AT"] = repr(RUN)
E._load_config_from_file()


def split(target_gb, movies, seasons, near=2.0):
    """Run the real engine split. movies [(score, gb)], seasons
    [(title, score, gb)]. Returns (tv_share_gb, [titles taken])."""
    E.NEAR_TIE_PTS = near
    order = [{"score": sc, "size_bytes": int(g * GB), "title": t, "season": 1,
              "sid": "jf:" + t, "path": "/lib/" + t, "take": False}
             for t, sc, g in seasons]
    A._stamp_tv_season_order(order, RUN)
    cands = [{"retention_score": sc, "file_size": int(g * GB)}
             for sc, g in movies]
    share, takes = E._split_pool_with_seasons(cands, int(target_gb * GB))
    by_key = {"jf:" + t + "|S1": t for t, _sc, _g in seasons}
    return share / GB, [by_key[t["key"]] for t in (takes or [])]


# ── The cheapest cover wins across types ─────────────────────────────────────
# 10 GB needed; a 40 GB movie and a 30 GB season, near-tied. Both cover it
# alone, so the one that wastes less goes — the season.
tv, taken = split(10, movies=[(20.0, 40)], seasons=[("S", 21.0, 30)])
check("a near-tied season is taken over a larger movie", taken == ["S"], taken)
check("...and only its bytes claim the share", tv == 30.0, tv)

# The mirror image: the movie is the cheaper cover, so it goes.
tv, taken = split(10, movies=[(21.0, 30)], seasons=[("S", 20.0, 40)])
check("a near-tied movie is taken over a larger season",
      taken == [] and tv == 0.0, (tv, taken))

# ── Score still governs everything outside the band ──────────────────────────
tv, taken = split(10, movies=[(20.0, 40)], seasons=[("S", 60.0, 30)])
check("a season scoring well above the movies is NOT taken to save bytes",
      taken == [], taken)
tv, taken = split(10, movies=[(60.0, 30)], seasons=[("S", 20.0, 40)])
check("...and a far-worse season is taken even though it wastes more",
      taken == ["S"] and tv == 40.0, (tv, taken))

# ── The whole band is needed: nothing is spared ──────────────────────────────
# The band totals less than the need, so every member goes and the selection
# rule has nothing to choose between.
tv, taken = split(30, movies=[(20.0, 8), (21.0, 6)],
                  seasons=[("S", 20.5, 10)])
check("when the whole band is needed every member is taken",
      tv == 10.0 and taken == ["S"], (tv, taken))

# ── Nothing in the band covers alone: largest first ──────────────────────────
# Same rule the movie loop uses — make the most progress per deletion.
tv, taken = split(50, movies=[(20.0, 5)], seasons=[("Big", 21.0, 20)])
check("with no single cover, the largest in the band goes first",
      taken == ["Big"], taken)

# ── The optimization can be turned off ───────────────────────────────────────
tv, taken = split(10, movies=[(20.0, 40)], seasons=[("S", 21.0, 30)], near=None)
check("with the near-tie window off, strict worst-first returns",
      taken == [] and tv == 0.0, (tv, taken))

# ── The movie loop's own boundary pick ───────────────────────────────────────
# Inside the band the scores are equivalent by definition, so a fraction of a
# point must not send a 42 GB file to cover a 2 GB need.
E.NEAR_TIE_PTS = 2.0


def mv(title, score, gb):
    return {"title": title, "retention_score": score, "file_size": int(gb * GB)}


pick = E._pop_next_deletion([mv("Big", 18.0, 42), mv("Small", 19.0, 4)], [], 2 * GB)
check("the smallest near-tied cover goes, not the lowest-scoring one",
      pick["title"] == "Small", pick["title"])
pick = E._pop_next_deletion([mv("Small", 18.0, 4), mv("Big", 19.0, 42)], [], 2 * GB)
check("...and it is still the small one when it also scores worst",
      pick["title"] == "Small", pick["title"])
# Outside the band, score rules exactly as before.
pick = E._pop_next_deletion([mv("Big", 18.0, 42), mv("Small", 40.0, 4)], [], 2 * GB)
check("a far-better-scoring small file does not jump the queue",
      pick["title"] == "Big", pick["title"])
# The sparing property the window exists for survives: one item that covers
# beats several small ones that only cover together.
pick = E._pop_next_deletion(
    [mv("A", 1.0, 1), mv("B", 1.5, 1), mv("C", 1.8, 1), mv("BIG", 2.0, 6)], [], 5 * GB)
check("one covering item still spares the small near-ties", pick["title"] == "BIG",
      pick["title"])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
