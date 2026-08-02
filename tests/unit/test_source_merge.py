"""Plex + Jellyfin source merge and dedup — get_all_movies() and its helpers.

When both servers are enabled the SAME physical file must collapse to ONE
candidate (matched by /library path across differing mount roots), combining
play stats without double-counting; a movie on only one server passes through;
and a movie that SHOULD have matched but whose paths diverged is flagged as an
unreconciled twin and skipped (never deleted on an ambiguous identity). This is
the layer that prevents a two-server setup from doubling every play count or
deleting the wrong copy."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.emit_progress = lambda *a, **k: None
E.extract_file_path = lambda row, quiet=True: row.get("file")  # rows already carry paths

def plex_row(**kw):
    r = {"rating_key": "p1", "title": "Film", "file": "/plex/Movies/Film (2020)/film.mkv",
         "play_count": 0, "last_played": 0, "added_at": 0, "_section_id": "1"}
    r.update(kw); return r

def jf_row(**kw):
    r = {"rating_key": "jf:1", "title": "Film", "file": "/jf/Movies/Film (2020)/film.mkv",
         "play_count": 0, "last_played": 0, "added_at": 0, "protected": False,
         "_jf_users": 0, "_jf_favorite": False, "tmdb_id": None, "imdb_id": None}
    r.update(kw); return r

def run_merge(plex, jelly):
    E.USE_PLEX = bool(plex is not None)
    E.USE_JELLYFIN = bool(jelly is not None)
    E.get_all_movies_from_tautulli = lambda: list(plex or [])
    E.get_all_movies_from_jellyfin = lambda: list(jelly or [])
    E._tag_jellyfin_metadata = lambda r: r   # identity — we assert on raw fields
    return E.get_all_movies()

# ── Single-source passthrough ────────────────────────────────────────────────
plex_only = run_merge([plex_row()], None)
check("Plex-only passes through unchanged", len(plex_only) == 1 and plex_only[0]["rating_key"] == "p1")
jf_only = run_merge(None, [jf_row()])
check("Jellyfin-only passes through tagged", len(jf_only) == 1 and jf_only[0]["rating_key"] == "jf:1")

# ── Same file on both servers collapses to one, combining stats ──────────────
# Different mount roots (/plex vs /jf) but the same trailing folder+file, so the
# suffix match key ties them (no on-disk resolution needed for the test).
merged = run_merge(
    [plex_row(play_count=2, last_played=1_700_000_000, added_at=1_500_000_000)],
    [jf_row(play_count=3, last_played=1_650_000_000, added_at=1_400_000_000,
            protected=True, _jf_users=4)],
)
check("same file on both servers merges to ONE candidate", len(merged) == 1)
m = merged[0]
check("play counts are SUMMED across servers", E.parse_int(m["play_count"], 0) == 5)
check("last_played takes the MORE RECENT", E.parse_int(m["last_played"], 0) == 1_700_000_000)
check("added_at takes the OLDEST", E.parse_int(m["added_at"], 0) == 1_400_000_000)
check("protection is unioned (Jellyfin-protected wins)", m.get("_jf_protected") is True)
check("the merged row is marked present on both servers", m.get("_jf_matched") is True)

# ── Distinct users: HIGHER of the two, never the sum ─────────────────────────
# Plex played (=1 Plex watcher) vs Jellyfin's 4 distinct users → 4, not 5.
check("distinct users on a both-servers row is the max, not the sum",
      E._distinct_users_for_row(m) == 4)
check("distinct users on a Jellyfin-only row reads its per-user count",
      E._distinct_users_for_row(jf_row(_jf_users=3)) == 3)
check("distinct users on a played Plex-only row is 1",
      E._distinct_users_for_row(plex_row(play_count=2)) == 1)
check("distinct users on an unplayed Plex-only row is 0",
      E._distinct_users_for_row(plex_row(play_count=0, last_played=0)) == 0)

# ── _merge_added_at: oldest positive wins, 0/unknown ignored ─────────────────
check("merge_added_at ignores 0 and keeps the real date",
      E._merge_added_at(0, 1_500_000_000) == 1_500_000_000)
check("merge_added_at keeps the older of two real dates",
      E._merge_added_at(1_600_000_000, 1_500_000_000) == 1_500_000_000)
check("merge_added_at of two unknowns is 0", E._merge_added_at(0, 0) == 0)

# ── Near-miss twin: same filename, paths that don't match → skip, not delete ─
# Same folder+filename ("Film (2020)/film.mkv") but the 3rd-from-last segment
# differs, so the suffix keys diverge and they DON'T merge — yet the twin check
# catches it and flags the Jellyfin copy as an unreconciled twin.
twin = run_merge(
    [plex_row(file="/plex/A/Film (2020)/film.mkv")],
    [jf_row(file="/jf/B/Film (2020)/film.mkv")],
)
jf_twin = next((r for r in twin if r["rating_key"] == "jf:1"), None)
px_twin = next((r for r in twin if r["rating_key"] != "jf:1"), None)
check("a same-filename path divergence stays two rows (no false merge)", len(twin) == 2)
check("the unmerged twin is flagged so the scan skips it",
      jf_twin is not None and "_unmerged_plex_twin" in jf_twin)
# BOTH sides. The rows point at one film and the Plex row carries no Jellyfin
# favorite or protection — those are written only onto rows that merged — so
# flagging the Jellyfin copy alone left the Plex copy a full deletion candidate
# with its Jellyfin protection stripped, deleted while the run summary said
# "Identity mismatch: 1 (skipped, not deleted)".
check("...and so is the PLEX row, which is the one that could be deleted",
      px_twin is not None and "_unmerged_jf_twin" in px_twin)
check("each side names the other", px_twin and jf_twin
      and px_twin["_unmerged_jf_twin"]["file"] == jf_twin["file"]
      and jf_twin["_unmerged_plex_twin"]["file"] == px_twin["file"])
# The favorite that makes this dangerous: a Jellyfin-favorited film whose paths
# did not reconcile has no _jf_favorite on the Plex row to protect it.
fav = run_merge(
    [plex_row(file="/plex/A/Fav (2020)/fav.mkv")],
    [jf_row(file="/jf/B/Fav (2020)/fav.mkv", _jf_favorite=True)],
)
fav_px = next((r for r in fav if r["rating_key"] != "jf:1"), None)
check("a favorited twin leaves the Plex row unprotected — so it must be flagged",
      fav_px is not None and not fav_px.get("_jf_favorite")
      and "_unmerged_jf_twin" in fav_px)

# ── One enabled server returning nothing is a broken source, not a library ───
# Degrading to single-source mode here deletes a full run's worth of movies with
# the missing server's watch history, favorites and collection protection
# silently absent. Both empty is fine — there is nothing to delete either way —
# and so is an empty library on a single-server setup.
def merge_aborts(plex, jelly):
    try:
        run_merge(plex, jelly)
        return False
    except SystemExit:
        return True

check("an empty Jellyfin catalog beside a stocked Plex aborts", merge_aborts([plex_row()], []))
check("...and the reverse aborts too", merge_aborts([], [jf_row()]))
check("both servers empty is not an abort", not merge_aborts([], []))
check("an empty library on one enabled server is not an abort",
      not merge_aborts([], None) and not merge_aborts(None, []))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
