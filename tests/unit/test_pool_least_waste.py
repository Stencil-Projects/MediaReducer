"""At the deletion boundary, the cheapest cover wins — movie or season alike.

Score decides which items reach the boundary; this decides only which of
several equally-scored ones is the cheapest way to finish the job. The two
questions were previously answered differently by media type: the movie loop
had a size-aware step at the boundary and the merged pool had none, so the
type with the bigger deletion unit — always TV — overshot every small deficit
by a whole season while a near-tied movie would have covered it with a
fraction of the waste.

The movie side was not innocent either. It picked the lowest-SCORING item that
covers rather than the smallest, so inside a band whose whole premise is
"these scores are equivalent", a fraction of a point could send a 42 GB file
to cover a 2 GB need while a 4 GB file survived.

What must NOT change: score still governs everything outside the band. A
season that scores well above the movies in front of it is not deletable
merely because it would waste fewer bytes.
"""
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


def split(target_gb, movies, seasons, near=2.0):
    """Run the real pool split. movies [(score, gb)], seasons [(title, score, gb)].
    Returns (freed_gb, tv_share_gb, [titles taken])."""
    A._pool_deletion_target_bytes = lambda cfg: int(target_gb * GB)
    A.db.read_pending_doc = lambda p: {"entries": {
        f"/m{i}": {"score": sc, "size_bytes": int(g * GB)}
        for i, (sc, g) in enumerate(movies)}}
    order = [{"score": sc, "size_bytes": int(g * GB), "title": t, "season": 1,
              "sid": "jf:x", "path": "/lib/" + t, "take": False}
             for t, sc, g in seasons]
    _takes, share = A._merged_pool_takes(order, {"NEAR_TIE_PTS": near})
    return ((share["tv_share_bytes"] + share["movie_share_bytes"]) / GB,
            share["tv_share_bytes"] / GB,
            [e["title"] for e in order if e["take"]])


# ── The cheapest cover wins across types ─────────────────────────────────────
# 10 GB needed; a 40 GB movie and a 30 GB season, near-tied. Both cover it
# alone, so the one that wastes less goes — and it is the season, which the
# old rule could never choose.
freed, tv, taken = split(10, movies=[(20.0, 40)], seasons=[("S", 21.0, 30)])
check("a near-tied season is taken over a larger movie", taken == ["S"], taken)
check("...and only its bytes are freed", freed == 30.0 and tv == 30.0, (freed, tv))

# The mirror image: the movie is the cheaper cover, so it goes.
freed, tv, taken = split(10, movies=[(21.0, 30)], seasons=[("S", 20.0, 40)])
check("a near-tied movie is taken over a larger season", taken == [], taken)
check("...and the season claims no share", tv == 0.0 and freed == 30.0, (freed, tv))

# ── Score still governs everything outside the band ──────────────────────────
freed, tv, taken = split(10, movies=[(20.0, 40)], seasons=[("S", 60.0, 30)])
check("a season scoring well above the movies is NOT taken to save bytes",
      taken == [] and freed == 40.0, (taken, freed))
freed, tv, taken = split(10, movies=[(60.0, 30)], seasons=[("S", 20.0, 40)])
check("...and a far-worse season is taken even though it wastes more",
      taken == ["S"] and freed == 40.0, (taken, freed))

# ── The whole band is needed: nothing is spared ──────────────────────────────
# The band totals less than the need, so every member goes and the selection
# rule has nothing to choose between. (The code short-circuits this case; the
# short-circuit cannot change the outcome, which is why the only thing worth
# asserting is that everything in the band is taken.)
freed, tv, taken = split(30, movies=[(20.0, 8), (21.0, 6)],
                         seasons=[("S", 20.5, 10)])
check("when the whole band is needed every member is taken",
      tv == 10.0 and freed == 24.0, (freed, tv, taken))

# ── Nothing in the band covers alone: largest first ──────────────────────────
# Same rule the movie loop uses — make the most progress per deletion.
freed, tv, taken = split(50, movies=[(20.0, 5)], seasons=[("Big", 21.0, 20)])
check("with no single cover, the largest in the band goes first",
      taken == ["Big"], taken)

# ── The optimization can be turned off ───────────────────────────────────────
freed, tv, taken = split(10, movies=[(20.0, 40)], seasons=[("S", 21.0, 30)], near=None)
check("with the near-tie window off, strict worst-first returns",
      taken == [] and freed == 40.0, (taken, freed))

# ── A season the pass could never act on claims nothing ──────────────────────
# It must not reach the share: the engine would subtract bytes nobody frees.
A._pool_deletion_target_bytes = lambda cfg: int(10 * GB)
A.db.read_pending_doc = lambda p: {"entries": {"/m0": {"score": 40.0,
                                                       "size_bytes": int(12 * GB)}}}
order = [{"score": 1.0, "size_bytes": int(30 * GB), "title": "NoId", "season": 1,
          "sid": "", "path": "", "take": False}]
_t, share = A._merged_pool_takes(order, {"NEAR_TIE_PTS": 2.0})
check("an unactionable season claims no share bytes",
      share["tv_share_bytes"] == 0 and not order[0]["take"], share)
check("...and the movies cover the deficit instead",
      share["movie_share_bytes"] == 12 * GB, share["movie_share_bytes"])

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
