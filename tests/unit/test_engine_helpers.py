"""Engine internals that no scenario test exercises directly: the Tautulli
intra-source dedup, the config coercion helpers that turn hand-edited JSON into
safe values, compute_config_hash's cache-invalidation surface, and the IMDb
ratings pipeline (bounded decompression + TSV parsing).

These are small, load-bearing, and hostile-input facing — a wrong coercion
persists a bad config, a wrong dedup double-counts plays, an unbounded gunzip is
a decompression bomb, and a lax TSV parser skews every deletion score. All run
hermetically (monkeypatched API, in-memory gzip, temp files)."""
import gzip
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E
import _tmpout

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond

E.log = lambda *a, **k: None
E.emit_progress = lambda *a, **k: None

# ── Tautulli intra-source dedup (same file across sections) ───────────────────
# Two movie sections; the same physical file appears in both. The merged row
# keeps the HIGHEST play_count and MOST-RECENT last_played, and the Radarr
# section id wins if EITHER copy was in it (so cleanup still finds the movie).
E.RADARR_OVERSEERR_SECTION_ID = "2"
_pages = {
    "1": [{"file": "/m/Dup (2020)/dup.mkv", "play_count": 5, "last_played": 100, "rating_key": "a"},
          {"file": "/m/Solo (2019)/solo.mkv", "play_count": 1, "last_played": 10, "rating_key": "b"}],
    "2": [{"file": "/m/Dup (2020)/dup.mkv", "play_count": 2, "last_played": 999, "rating_key": "c"}],
}
E.get_movie_section_ids = lambda: ["1", "2"]
# Watch history, keyed by the same rating_keys. Dup was watched by two people
# through section 1 and one of the same two through section 2; Solo by one.
_history = {
    "1": ([{"rating_key": "a", "user_id": 7}] * 3 + [{"rating_key": "a", "user_id": 9}]
          + [{"rating_key": "b", "user_id": 7}]),
    "2": [{"rating_key": "c", "user_id": 7}, {"rating_key": "c", "user_id": 7}],
}
_history_pages = []      # (section_id, start, length) actually requested
def _fake_tautulli(cmd, **params):
    if cmd == "get_library_media_info":
        sid = str(params.get("section_id"))
        start = int(params.get("start", 0))
        return {"data": _pages.get(sid, [])[start:start + int(params.get("length", 1000))]}
    if cmd == "get_history":
        sid = str(params.get("section_id"))
        start = int(params.get("start", 0))
        length = int(params.get("length", 1000))
        _history_pages.append((sid, start, length))
        rows = _history[sid]
        return {"recordsFiltered": len(rows), "data": rows[start:start + length]}
    return {}
E.tautulli_api = _fake_tautulli

merged = {r.get("file"): r for r in E.get_all_movies_from_tautulli()}
check("a file in two sections collapses to one row", len(merged) == 2)
dup = merged["/m/Dup (2020)/dup.mkv"]
check("dedup keeps the HIGHEST play_count", E.parse_int(dup["play_count"], 0) == 5)
check("dedup keeps the MOST-RECENT last_played", E.parse_int(dup["last_played"], 0) == 999)
check("dedup preserves the Radarr section when either copy is in it",
      str(dup["_section_id"]) == "2")
check("a single-section movie passes through untouched",
      E.parse_int(merged["/m/Solo (2019)/solo.mkv"]["play_count"], 0) == 1)

# ── Distinct viewers come from the history, not from "was it ever played" ─────
# The media-info rows carry a play count and nothing about who watched. Four
# plays by two people is the case that matters: it is the movie two of the
# household would miss, and counting it as one watcher is what feeds it to the
# retention score as something nobody cares about.
check("a movie's distinct viewers are counted, not assumed",
      E.parse_int(dup["_plex_users"], 0) == 2)
check("...and repeat plays by one person are still one viewer",
      E.parse_int(merged["/m/Solo (2019)/solo.mkv"]["_plex_users"], 0) == 1)
# Section 2's copy of Dup was watched by user 7, who also watched section 1's.
# Adding the two sections' counts would invent a third viewer.
check("the same viewer through two sections is not counted twice",
      E._distinct_users_for_row(dup) == 2)
check("the sweep asks each section for its own history",
      {sid for sid, _, _ in _history_pages} == {"1", "2"})

# A server free to cap the page size below what was asked for: "short page"
# means "that is all I send at once", not "that is all there is". Reading it as
# the end stops at page one and quietly under-counts every viewer whose plays
# sit further back — the movie four people watched last year reads as one.
_capped = {"1": [{"rating_key": "z", "user_id": u} for u in range(1, 8)]}
def _capping_tautulli(cmd, **params):
    if cmd == "get_history":
        rows = _capped.get(str(params.get("section_id")), [])
        start = int(params.get("start", 0))
        return {"recordsFiltered": len(rows), "data": rows[start:start + 2]}   # caps at 2
    return {}
_saved_api = E.tautulli_api
E.tautulli_api = _capping_tautulli
try:
    _swept = E._plex_distinct_users_by_key(["1"])
finally:
    E.tautulli_api = _saved_api
check("a server that caps the page size does not truncate the sweep",
      _swept.get("z") == 7)

# ...and one that ignores `start` entirely must not spin forever. Bounded, with
# whatever it did manage to read.
_stuck_calls = []
def _stuck_tautulli(cmd, **params):
    if cmd == "get_history":
        _stuck_calls.append(1)
        return {"data": [{"rating_key": "y", "user_id": 1}]}   # no recordsFiltered, same page
    return {}
E.tautulli_api = _stuck_tautulli
try:
    _stuck = E._plex_distinct_users_by_key(["1"])
finally:
    E.tautulli_api = _saved_api
check("a server that ignores paging is bounded, not an endless run",
      len(_stuck_calls) <= 1002 and _stuck.get("y") == 1)

# A play on record with no attributable viewer still means someone watched it:
# never zero while the play count says otherwise.
check("a played movie with no history rows still counts one viewer",
      E._distinct_users_for_row({"play_count": 3, "_plex_users": 0}) == 1)
check("an unplayed movie counts none",
      E._distinct_users_for_row({"play_count": 0, "_plex_users": 0}) == 0)

# ── Config coercion ──────────────────────────────────────────────────────────
# The bounds themselves are shared with the app and their agreement is pinned
# in test_config_rule_parity; what belongs here is the ENGINE's half of the
# contract — that a rejected value records an error (which aborts a simulation
# or a cleanup) and falls back to the caller's default rather than to None.
E.CONFIG_ERRORS = []
check("a whole-numbered value comes back as an int",
      E._num("SCORE_BALANCE", "50.0") == 50)
check("a genuinely fractional value keeps its fraction",
      E._num("NEAR_TIE_PTS", "2.5") == 2.5)
check("a non-number takes the default AND records an error",
      E._num("SCORE_BALANCE", "abc", default=7) == 7 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("an out-of-range value takes the default AND records an error",
      E._num("SCORE_BALANCE", "-1", default=0) == 0 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("a blank value is None for a setting that can be switched off",
      E._num("NEAR_TIE_PTS", "") is None and not E.CONFIG_ERRORS)
check("...and an error for one that cannot",
      E._num("SCORE_BALANCE", "", default=50) == 50 and E.CONFIG_ERRORS)
# The scripted-POST non-finite vector: a hand-edited Infinity/NaN must be
# rejected OUTRIGHT — even a bound-less field would otherwise let it through
# (every comparison with nan is False, and inf passes any lone lower bound).
E.CONFIG_ERRORS = []
check("inf is rejected outright, even where no upper bound applies",
      E._num("HEADROOM_GB", "1e999", default=0) == 0 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("nan is rejected outright",
      E._num("HEADROOM_GB", "NaN", default=0) == 0 and E.CONFIG_ERRORS)
E.CONFIG_ERRORS = []
check("the error names the setting, so the log says which one to fix",
      E._num("SCORE_BALANCE", "abc", default=0) == 0
      and E.CONFIG_ERRORS and E.CONFIG_ERRORS[0].startswith("SCORE_BALANCE "))
E.CONFIG_ERRORS = []

check("_coerce_string_list splits comma/newline strings, trims, dedups",
      E._coerce_string_list("a, b\nb , c", "L") == ["a", "b", "c"])
check("_coerce_string_list passes a list through, trimming blanks",
      E._coerce_string_list(["x", " ", "y"], "L") == ["x", "y"])
E.CONFIG_ERRORS = []
check("_coerce_string_list rejects a non-list/non-string",
      E._coerce_string_list(42, "L") == [] and E.CONFIG_ERRORS)

check("_coerce_movie_extensions normalizes to lowercased dotted set",
      E._coerce_movie_extensions("MKV, .mp4") == {".mkv", ".mp4"})

check("_coerce_config_bool reads truthy strings", E._coerce_config_bool("Yes") is True)
check("_coerce_config_bool reads falsey/garbage as False",
      E._coerce_config_bool("nope") is False and E._coerce_config_bool(0) is False)

# _normalize_library_path maps every entry shape under the library root.
root = str(E.LIBRARY_ROOT)
name = root.rsplit("/", 1)[-1]
check("_normalize_library_path accepts a bare folder name",
      E._normalize_library_path("Movies") == f"{root}/Movies")
check("_normalize_library_path treats '/' as the whole root",
      E._normalize_library_path("/") == root)
check("_normalize_library_path keeps only the leaf of a foreign absolute path",
      E._normalize_library_path("/mnt/user/Movies") == f"{root}/Movies")
check("_normalize_library_path returns None for blank", E._normalize_library_path("  ") is None)

# ── compute_config_hash: metadata-source sensitivity ─────────────────────────
E.TAUTULLI_URL = "http://tautulli"; E.TAUTULLI_API_KEY = "k"
E.PLEX_URL = "http://plex"; E.PLEX_TOKEN = "t"
E.PROTECTED_COLLECTIONS = ["Keep"]
E.USE_PLEX = True; E.USE_JELLYFIN = False
E.JELLYFIN_URL = ""; E.JELLYFIN_API_KEY = ""; E.JELLYFIN_PROTECTED_COLLECTIONS = set()
E.MONITOR_DIRS = ["/library/Movies"]
E.MAX_LIBRARY_GB = 100
base_hash = E.compute_config_hash()
# A threshold/scoring/path change must NOT bust the metadata cache.
E.MAX_LIBRARY_GB = 200
E.MONITOR_DIRS = ["/library/Other"]
check("compute_config_hash ignores thresholds and monitored paths",
      E.compute_config_hash() == base_hash)
# A metadata-source change (Tautulli key) MUST bust it.
E.TAUTULLI_API_KEY = "different"
check("compute_config_hash changes when a metadata source changes",
      E.compute_config_hash() != base_hash)

# ── imdb_dataset_needed ──────────────────────────────────────────────────────
E.QUALITY_WEIGHT = 0.0; E.MAX_IMDB_RATING = None
check("IMDb dataset not needed at zero quality weight and no cutoff",
      E.imdb_dataset_needed() is False)
E.MAX_IMDB_RATING = 6.0
check("a Max IMDb cutoff alone requires the dataset", E.imdb_dataset_needed() is True)
E.MAX_IMDB_RATING = None; E.QUALITY_WEIGHT = 0.5
check("a positive quality weight alone requires the dataset", E.imdb_dataset_needed() is True)

# ── _bounded_gunzip: decompression-bomb caps ─────────────────────────────────
small = gzip.compress(b"tconst\taverageRating\tnumVotes\ntt1\t7.0\t100\n")
check("_bounded_gunzip round-trips a small archive",
      b"tconst" in E._bounded_gunzip(small))
_saved_gz = E._IMDB_GZ_MAX_BYTES
E._IMDB_GZ_MAX_BYTES = 4  # smaller than the compressed blob
try:
    E._bounded_gunzip(small)
    check("_bounded_gunzip rejects an oversized compressed archive", False)
except ValueError:
    check("_bounded_gunzip rejects an oversized compressed archive", True)
E._IMDB_GZ_MAX_BYTES = _saved_gz
_saved_tsv = E._IMDB_TSV_MAX_BYTES
E._IMDB_TSV_MAX_BYTES = 4  # decompressed output exceeds this
try:
    E._bounded_gunzip(small)
    check("_bounded_gunzip rejects a decompression bomb (output cap)", False)
except ValueError:
    check("_bounded_gunzip rejects a decompression bomb (output cap)", True)
E._IMDB_TSV_MAX_BYTES = _saved_tsv

# ── _load_imdb_ratings_from_disk: header + row validation ─────────────────────
tsv_dir = Path(tempfile.mkdtemp(prefix="mr-imdb."))
good = tsv_dir / "good.tsv"
good.write_text("tconst\taverageRating\tnumVotes\ntt1\t7.5\t1000\ntt2\tBAD\tROW\ntt3\t6.0\t50\n",
                encoding="utf-8")
E.IMDB_RATINGS_PATH = good
ratings = E._load_imdb_ratings_from_disk()
check("valid rows parse to (rating, votes)", ratings.get("tt1") == (7.5, 1000))
check("a malformed row is skipped, not fatal", "tt2" not in ratings and ratings.get("tt3") == (6.0, 50))

# ── Per-mark delay: a mark ages against the delay it was made under ──────────
import time as _t
import datetime as _dt2
E.DELETE_DELAY_DAYS = 2
_now = _t.time()
_today = _dt2.date.fromtimestamp(_now)
check("_entry_delay_days honors the stamped delay over the setting",
      E._entry_delay_days({"delay_days": 7}) == 7
      and E._entry_delay_days({"delay_days": None}) == 2   # unstamped → setting
      and E._entry_delay_days({}) == 2
      and E._entry_delay_days({"delay_days": "garbage"}) == 2
      and E._entry_delay_days({"delay_days": 0}) == 2)     # 0 is never a real stamp
check("_mark_delete_on dates by the stamped delay when given",
      E._mark_delete_on(_now, 7) == str(_today + _dt2.timedelta(days=7))
      and E._mark_delete_on(_now) == str(_today + _dt2.timedelta(days=2)))

# ── write_run_report: a stray Path in a caller's items must not kill it ──────
# (The Debug Cleanup preview once passed pathlib.Paths; json.dumps raised and
# the bare except silently dropped the whole report + its notification.)
import atexit
import json as _json
import shutil as _sh
from pathlib import Path as _P
_tmpout.redirect_engine(E, _P(tempfile.mkdtemp(prefix="mr-report.")))
atexit.register(_sh.rmtree, E.OUTPUT_DIR, True)
E.write_run_report(mode="debug_cleanup", deleted_count=1,
                   deleted_items=[{"title": "X", "size": 1, "path": _P("/m/x.mkv")}])
try:
    _rep = _json.loads((E.OUTPUT_DIR / "last_run_report.json").read_text())
    check("write_run_report survives a Path in items (coerced to str)",
          _rep["deleted_items"][0]["path"] == "/m/x.mkv" and _rep["mode"] == "debug_cleanup")
except FileNotFoundError:
    check("write_run_report survives a Path in items (report was never written)", False)
bad_header = tsv_dir / "bad.tsv"
bad_header.write_text("id\trating\tvotes\ntt1\t7.5\t1000\n", encoding="utf-8")
E.IMDB_RATINGS_PATH = bad_header
try:
    E._load_imdb_ratings_from_disk()
    check("a wrong TSV header is rejected", False)
except ValueError:
    check("a wrong TSV header is rejected", True)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
