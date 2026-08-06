"""_fresh_watch_data's Jellyfin fetch must survive a marked set the size of the
whole queue. The ids ride the query string, and a Library Size Cap far below
the library puts EVERY marked movie in the re-verify set — thousands of ids in
one URL is an HTTP 414, which read every Jellyfin movie as unverifiable and
shrank a Debug Cleanup's deletable set to just the Plex-only stragglers.

Pinned here: the ids go out in batches small enough to fit a URL; every id is
still read; and a failed batch discards the WHOLE user's read rather than
keeping a partial sum — partial per-user data could undercount plays, and an
undercount leans toward deleting."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

E.log = lambda *a, **k: None
E.USE_PLEX = False           # Jellyfin half only; the Tautulli half is per-movie already
E.USE_JELLYFIN = True
E._jellyfin_user_ids = lambda: ["u1", "u2"]

IDS = [f"jf:{n:032x}" for n in range(250)]     # GUID-sized, like real Jellyfin ids

calls = []
def fake_request(path, params):
    ids = params["Ids"].split(",")
    calls.append((path, len(ids)))
    if len(ids) > 100:
        raise RuntimeError("HTTP Error 414: URI Too Long")
    return {"Items": [{"Id": i, "UserData": {"PlayCount": 1, "IsFavorite": False,
                                             "LastPlayedDate": None}} for i in ids]}
E._jellyfin_request = fake_request

out = E._fresh_watch_data(IDS)
check("every id is read despite being far past one URL's worth",
      len(out) == 250, len(out))
check("no single request carries more than a URL-safe batch",
      calls and max(n for _, n in calls) <= 100, calls[:3])
check("plays sum across BOTH users, batched exactly like unbatched",
      all(v["play_count"] == 2 for v in out.values()),
      {k: v for k, v in list(out.items())[:2]})

# A batch that fails mid-user: that user's whole read is discarded — the other
# user still answers, so the ids stay verifiable but count only ONE user's
# plays, exactly what a whole-request failure produced before batching.
fail_for = {"u2"}
def flaky_request(path, params):
    if any(f"Users/{u}/" in path for u in fail_for) and "00000000000000000000000000000064" in params["Ids"]:
        raise RuntimeError("boom mid-user")   # second batch (ids 100+) fails
    return fake_request(path, params)
E._jellyfin_request = flaky_request

calls.clear()
out = E._fresh_watch_data(IDS)
check("a mid-user batch failure discards that user's read entirely",
      all(v["play_count"] == 1 for v in out.values()),
      sorted({v["play_count"] for v in out.values()}))
check("...while the healthy user keeps every id verifiable", len(out) == 250, len(out))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
