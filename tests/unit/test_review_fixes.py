"""The review batch: four fixes, each pinned at the behavior that was wrong.

Every one of these passed the whole suite while broken, which is why each
check here states the failure it now prevents rather than restating the code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
_tmpout.config()
import tempfile                                    # noqa: E402
import app as A                                    # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


TMP = Path(tempfile.mkdtemp(prefix="mr-review-"))

# ── 1. Protection must fail CLOSED on an empty member answer ────────────────
# The collection LISTS already raised on an empty-body 200; the MEMBER fetches
# read it as "or {}" — an empty protected set with no exception, which is a
# protected show deletable for the pass.
orig_plex, orig_jf = A._plex_get, A._jellyfin_get


def plex_stub(url, token, path, timeout=15):
    if "/library/sections" == path.rstrip("?").split("?")[0] and "collections" not in path:
        return {"MediaContainer": {"Directory": [{"type": "show", "key": "1"}]}}
    if path.endswith("/collections"):
        return {"MediaContainer": {"Metadata": [{"title": "Keep", "ratingKey": "9"}]}}
    return None   # the members: an empty-body 200


def jf_stub(url, key, path, params=None, timeout=15):
    if (params or {}).get("IncludeItemTypes") == "BoxSet":
        return {"Items": [{"Name": "Keep", "Id": "b1"}]}
    return None   # the members again

A._plex_get, A._jellyfin_get = plex_stub, jf_stub
try:
    try:
        A._plex_protected_series_titles({"plex_url": "http://p", "plex_token": "t"}, ["Keep"])
        plex_raised = False
    except RuntimeError:
        plex_raised = True
    try:
        A._jellyfin_protected_series_titles("http://j", "k", ["Keep"])
        jf_raised = False
    except RuntimeError:
        jf_raised = True
finally:
    A._plex_get, A._jellyfin_get = orig_plex, orig_jf
check("an empty Plex member answer raises instead of unprotecting", plex_raised)
check("an empty Jellyfin member answer raises instead of unprotecting", jf_raised)

# ── 2. One folder claimed by two inventory rows puts BOTH out of scope ──────
# A same-named show in a monitored and an unmonitored library resolves by name
# into the monitored copy's folder; one show splits into two rows when the
# servers disagree on its year. Either way, before this guard the plan treated
# one on-disk folder as two shows.
lib = TMP / "lib"
(lib / "tv" / "Twin Show").mkdir(parents=True)
(lib / "tv" / "Other Show").mkdir(parents=True)
cfg2 = {"MONITOR_DIRS": [str(lib / "tv")]}

rows = [{"title": "Twin Show", "path": "/data/tv/Twin Show"},
        {"title": "Twin Show", "path": "/archive/classics/Twin Show"}]
A._resolve_tv_scope(rows, cfg2)
check("two rows resolving to one folder are BOTH refused",
      all(r["tv_in_scope"] == 0 and not r["path"] for r in rows), rows)
check("...with the shared folder recorded as the reason",
      all(r["tv_scope_conflict"] for r in rows), rows)

rows = [{"title": "Twin Show", "path": "/data/tv/Twin Show"},
        {"title": "Other Show", "path": "/data/tv/Other Show"}]
A._resolve_tv_scope(rows, cfg2)
check("two rows to two different folders both stay in scope",
      all(r["tv_in_scope"] == 1 for r in rows), rows)

# ── 3. A name+size identity survives the pre-resolve rewrite ────────────────
# get_all_movies resolves every Plex row once and stores the /library path;
# the scan then re-resolves that path on the trivial prefix rung. Without the
# recorded rung, a FLAT-layout row whose identity rests on name+size looked
# folder-anchored and the size-agreement gate never fired in production.
flat = TMP / "flat"
flat.mkdir()
movie = flat / "Solo Film (2019).mkv"
movie.write_bytes(b"x" * 4096)
saved = (E.LIBRARY_ROOT, E.MONITOR_DIRS)
E.LIBRARY_ROOT = flat
E.MONITOR_DIRS = ["."]
try:
    E._DIR_INDEX_CACHE = None
    p = E.resolve_under_library("/foreign/mount/Solo Film (2019).mkv", expected_size=4096)
    first_rung = E._LAST_RESOLVE_RUNG
    row = {"file": str(p), "_resolve_rung": first_rung}
    p2 = E.resolve_under_library(row["file"], expected_size=4096)
    second_rung = E._LAST_RESOLVE_RUNG
finally:
    (E.LIBRARY_ROOT, E.MONITOR_DIRS) = saved
    E._DIR_INDEX_CACHE = None
check("a flat-layout foreign path attaches by name+size",
      p == movie and first_rung == "name_size", (p, first_rung))
check("...and re-resolving the stored path forgets that (the bug)",
      p2 == movie and second_rung == "prefix", (p2, second_rung))
check("...so the gate reads the RECORDED rung, not the re-resolution",
      (row.get("_resolve_rung") or second_rung) == "name_size")

# ── 4. A season whose files are all already gone is VANISHED, not deleted ───
# Reporting it as "DELETED … 0 file(s)" cleared the mark but skipped the share
# re-stamp and the inventory refresh, so the phantom re-entered every plan,
# claiming its stale server size while the movie side under-covered daily.
series = lib / "tv" / "Gone Show"
series.mkdir(parents=True)
orig_rel, orig_json = A._tv_season_relpaths, A._json_request
A._tv_season_relpaths = lambda cfg, sid, n, name="": ([(Path("Season 1/e1.mkv"), 100),
                                                       (Path("Season 1/e2.mkv"), 100)], None)
A._json_request = lambda *a, **k: None
report = {"skipped": [], "deleted_seasons": [], "deleted_files": 0,
          "freed_bytes": 0, "vanished_seasons": 0, "vanished_bytes": 0}
try:
    done = A._delete_tv_season(
        {"MONITOR_DIRS": [str(lib / "tv")]},
        {"sid": "jf:abc", "title": "Gone Show", "season": 1,
         "path": str(series), "size_bytes": 5_000_000_000}, report)
finally:
    A._tv_season_relpaths, A._json_request = orig_rel, orig_json
check("the mark is finished (the files ARE gone)", done is True, report)
check("...but it is counted as vanished, not deleted",
      report["vanished_seasons"] == 1 and not report["deleted_seasons"], report)
check("...at its claimed size, so the share re-stamp can release it",
      report["vanished_bytes"] == 5_000_000_000, report["vanished_bytes"])
check("...and no files/bytes are claimed freed",
      report["deleted_files"] == 0 and report["freed_bytes"] == 0, report)

# ── 5. "…and N more" tells the truth past the report's item cap ─────────────
# The report file carries at most 200 items while the counts stay real, so a
# tail computed from len(items) contradicted the header for any bigger run:
# "Removed 500 movie(s)" followed by a list that ends "…and 160 more".
import notify                                      # noqa: E402
items = [{"title": f"M{i}", "size": 1} for i in range(200)]
lines, _ = notify._bounded_lines(items, lambda it: it["title"], 10**6, total=500)
check("the tail counts the RUN, not the capped file",
      lines[-1] == f"…and {500 - (len(lines) - 1)} more", lines[-1])
lines, _ = notify._bounded_lines(items[:5], lambda it: it["title"], 10**6, total=5)
check("...and stays silent when everything is shown",
      not lines[-1].startswith("…"), lines[-1])
lines, _ = notify._bounded_lines(items[:60], lambda it: it["title"], 10**6)
check("...with no total, the old arithmetic still holds",
      lines[-1] == f"…and {60 - (len(lines) - 1)} more", lines[-1])

# ── 6. A favorite protects its FILE, not just its row ───────────────────────
# The row flag only survives a successful Plex/Jellyfin pairing. A favorited
# movie whose Jellyfin row dies unpaired (different mount roots, stale size)
# protected nothing while the Plex row for the same file stayed deletable —
# the full scan was strictly weaker at favorites than the fast path it feeds.
(flat / "Loved Film (2020)").mkdir()
fav = flat / "Loved Film (2020)" / "Loved Film (2020).mkv"
fav.write_bytes(b"y" * 2048)
saved = (E.LIBRARY_ROOT, E.MONITOR_DIRS, set(E._JELLYFIN_FAVORITE_MATCH_KEYS))
E.LIBRARY_ROOT, E.MONITOR_DIRS = flat, ["."]
try:
    E._DIR_INDEX_CACHE = None
    # The Jellyfin fetch registered the favorite under ITS spelling of the path.
    E._JELLYFIN_FAVORITE_MATCH_KEYS = set(
        E._match_keys("/jf-mount/Movies/Loved Film (2020)/Loved Film (2020).mkv"))
    hit = bool(E._match_keys(str(fav)) & E._JELLYFIN_FAVORITE_MATCH_KEYS)
    miss = bool(E._match_keys(str(flat / "Other Film (2021).mkv"))
                & E._JELLYFIN_FAVORITE_MATCH_KEYS)
finally:
    (E.LIBRARY_ROOT, E.MONITOR_DIRS, E._JELLYFIN_FAVORITE_MATCH_KEYS) = saved
    E._DIR_INDEX_CACHE = None
check("a favorite registered under the server's path matches the disk file", hit)
check("...and does not smear onto other files", not miss)
check("the scan consults the key set, not only the row flag",
      "jf_favorite = bool(_match_keys(str(file_path)) & _JELLYFIN_FAVORITE_MATCH_KEYS)"
      in open(Path(__file__).resolve().parents[2] / "engine.py").read())

# ── 7. The small tier: alerts that survive a failed send, honest wording ────
# A held alert whose flush send fails goes back on hold, not into the void.
notify._held.clear(); notify._held_urls.clear()
notify._held["k1"] = [("T", "held body")]
notify._held_urls["k1"] = "json://x"
_o_send, _o_cool = notify._send_now, notify._cooldown_remaining_locked
notify._send_now = lambda urls, t, b: (False, "endpoint down")
notify._cooldown_remaining_locked = lambda key, now: 0
try:
    notify._flush_held()
finally:
    notify._send_now, notify._cooldown_remaining_locked = _o_send, _o_cool
check("a failed flush send re-holds the merged alert",
      "k1" in notify._held and notify._held["k1"], dict(notify._held))
notify._held.clear(); notify._held_urls.clear()

# Re-dated marks are described as re-dated, not as new marks.
mcfg = {"NOTIFY_ENABLED": True, "NOTIFY_ON_MARKED_CHANGES": True,
        "NOTIFY_SHOW_MOVIES": True, "RUN_MODE": "headroom"}
_t, body = notify.build_marked_change_message(
    mcfg, new_items=[{"title": "A", "delete_on": "2026-08-20"}],
    marked_total=3, redated=True)
check("a delay change is announced as a re-date", "re-dated 1 marked movie" in body, body)
check("...never as an addition", "more movie(s) for deletion" not in body, body)
check("...with the list headed Re-dated", "Re-dated:" in body, body)
_t, body = notify.build_marked_change_message(
    mcfg, new_items=[{"title": "A", "delete_on": "2026-08-20"}], marked_total=3)
check("a real addition still reads as one", "marked 1 more movie(s)" in body, body)

# The plan stamp compares MOVIE_CLEANUP_ENABLED the way the engine writes it:
# the EFFECTIVE bool, where absent means OFF.
check("an absent cleanup switch compares as the engine's stamped False",
      A._plan_config_value({}, "MOVIE_CLEANUP_ENABLED") is False,
      A._plan_config_value({}, "MOVIE_CLEANUP_ENABLED"))
check("...and an explicit true stays true",
      A._plan_config_value({"MOVIE_CLEANUP_ENABLED": True}, "MOVIE_CLEANUP_ENABLED") is True)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
