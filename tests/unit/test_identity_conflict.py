"""When two servers disagree about what a file IS, both rows must drop out.

Plex and Jellyfin each attach a film to a path. If they attach different ones,
one of them is wrong about this file and there is no way to tell which — so it
is skipped rather than deleted on a guess.

Skipping it once is not enough. The same file arrives as a Plex row and a
Jellyfin row; the Plex row records the path when it flags the conflict, and the
Jellyfin row has to recognise that and drop out too. Miss that second half and
the file stays deletable through the other server's row, which is exactly the
outcome the check exists to prevent — a disagreed-about file quietly deleted.

test_candidate_sources covers the first half through a full scan. It cannot
reach the second: its fixture folds the shared file into a single row, so the
Jellyfin-twin branch never executes there. Breaking that branch left the whole
suite green. It is reachable here because the check is now its own function.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)
E.log = lambda *a, **k: None

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


PATH = Path("/library/Movies/Shared (2020)/Shared.mkv")


def ask(is_plex_row, plex_ids, jf_ids, *, skip_paths=None, snap=None, resolved=None):
    """Put one row to the check and report what it decided, plus the state it
    left behind — the skip set and the counter are how the other row and the
    run summary find out."""
    # Keyed on the SAME spelling the check looks up by, which is the reported
    # path — that is how get_all_movies_from_jellyfin records it.
    E._JELLYFIN_IDS_BY_MATCH_KEY = (
        {k: jf_ids for k in E._match_keys(resolved or str(PATH))} if jf_ids else {})
    stats = {"identity_mismatch": 0}
    mismatches, skips = [], set(skip_paths or ())
    snap = snap if snap is not None else {}
    hit = E._identity_conflict_skip(
        is_plex_row=is_plex_row, title="Shared", meta=plex_ids, file_path=PATH,
        resolved_path=resolved or str(PATH), snap_entry=snap, mismatch_skip_paths=skips,
        identity_mismatches=mismatches, stats=stats)
    return hit, stats, mismatches, skips, snap


P = {"imdb_id": "tt30", "tmdb_id": "30"}

# ── The Plex row decides ─────────────────────────────────────────────────────
hit, stats, mm, skips, snap = ask(True, P, {"imdb": "tt99", "tmdb": "99", "title": "Other"})
check("a differing IMDb id is a conflict", hit is True)
check("...counted for the run summary", stats["identity_mismatch"] == 1, stats)
check("...recorded with both sides' ids",
      len(mm) == 1 and mm[0]["plex_imdb"] == "tt30" and mm[0]["jellyfin_imdb"] == "tt99", mm)
check("...naming both titles", "Shared (Plex)" in mm[0]["title"] and "Other (Jellyfin)" in mm[0]["title"],
      mm[0]["title"])
check("...and the snapshot row marked excluded", snap.get("excluded") is True, snap)
check("...with the path left for the other row to find", str(PATH) in skips, skips)

hit, stats, *_ = ask(True, P, {"imdb": "tt30", "tmdb": "30", "title": "Shared"})
check("agreeing ids are not a conflict", hit is False and stats["identity_mismatch"] == 0)

# IMDb settles it: two servers routinely hold different TMDb records for one
# film (remakes and alternate cuts are separate entries there), and the IMDb id
# is what fetches the rating this is scored on.
hit, stats, *_ = ask(True, {"imdb_id": "tt45", "tmdb_id": "390051"},
                     {"imdb": "tt45", "tmdb": "523098", "title": "Shared"})
check("a matching IMDb id settles a TMDb disagreement", hit is False, stats)

hit, *_ = ask(True, {"imdb_id": "", "tmdb_id": "390051"},
              {"imdb": "", "tmdb": "523098", "title": "Shared"})
check("with no IMDb on either side, TMDb arbitrates", hit is True)

hit, *_ = ask(True, {"imdb_id": "tt30", "tmdb_id": "30"},
              {"imdb": "", "tmdb": "", "title": "Shared"})
check("a server holding no ids at all cannot disagree", hit is False)

hit, *_ = ask(True, P, None)
check("no Jellyfin entry for the file is not a conflict", hit is False)

# ── The Jellyfin twin: the half nothing else reaches ─────────────────────────
hit, stats, mm, _, snap = ask(False, {}, None, skip_paths={str(PATH)})
check("the Jellyfin row for a flagged file is skipped too", hit is True)
check("...without counting the disagreement twice",
      stats["identity_mismatch"] == 0 and mm == [], (stats, mm))
check("...and re-flags the snapshot row the Jellyfin write reset",
      snap.get("excluded") is True, snap)

hit, *_ = ask(False, {}, None, skip_paths=set())
check("a Jellyfin row for an unflagged file passes through", hit is False)

hit, *_ = ask(False, {}, None, skip_paths={"/library/Movies/Other/Other.mkv"})
check("...and another file being flagged does not skip it", hit is False)

# The two spellings of one file. A row can name the path it was reported under
# while the file resolves somewhere else — a symlinked library, a mount with a
# relative segment — and the twin may arrive under either. Both go into the
# skip set, which only shows up when they actually differ: with a path that
# resolves to itself, either write alone looks sufficient.
REPORTED = "/library/reported/Shared.mkv"
hit, _, _, skips2, _ = ask(True, P, {"imdb": "tt99", "tmdb": "99", "title": "Other"},
                           resolved=REPORTED)
check("a conflict records the reported path", hit is True and REPORTED in skips2, skips2)
check("...and the resolved one as well", str(PATH.resolve()) in skips2, skips2)
check("a twin arriving under the reported spelling is skipped",
      ask(False, {}, None, skip_paths=skips2, resolved=REPORTED)[0] is True)
check("...and one arriving under the resolved spelling too",
      ask(False, {}, None, skip_paths=skips2)[0] is True)

# The end-to-end property, in the order build_candidates sees the two rows.
skips: set = set()
h1, s1, _, skips, snap1 = ask(True, P, {"imdb": "tt99", "tmdb": "99", "title": "Other"})
h2, s2, m2, _, snap2 = ask(False, {}, None, skip_paths=skips)
check("both rows for one disagreed file drop out", h1 is True and h2 is True)
check("...and the run reports exactly one disagreement",
      s1["identity_mismatch"] + s2["identity_mismatch"] == 1,
      (s1["identity_mismatch"], s2["identity_mismatch"]))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
