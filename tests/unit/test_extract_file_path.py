"""Every rung of the Tautulli path ladder, not just the first one.

extract_file_path turns a Tautulli row into the file on disk that a run may
unlink. It tries five places in order — the row's own keys, the row's nested
media_info, a live get_metadata call, that metadata's media_info, and its parts
— and hands each one a byte count to disambiguate two copies of the same movie.

Coverage said the suite only ever ran the FIRST rung: every mock row carries a
top-level "file", so four fifths of the function that decides what gets deleted
had never executed. A row from a real Plex library routinely does not — the
path is in media_info, or only in the metadata call — so the untested rungs are
not exotic, they are what a different library shape lands on.

The size argument is asserted at every rung, because it is not decoration: with
one movie in /movies and another in /movies-4k under the same folder and file
name, the byte count is the ONLY thing separating them, and passing the wrong
one deletes the wrong copy.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E  # noqa: E402
import _tmpout  # noqa: E402

OUT = Path(_tmpout.redirect_engine(E))
E.LOGFILE.write_text("", encoding="utf-8")

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


# resolve_under_library does its own filesystem work and has its own tests.
# Here it is a spy: what matters is WHICH path each rung found and WHICH size
# it decided to disambiguate with.
seen = []


def _spy(path_str, expected_size=None):
    seen.append((path_str, expected_size))
    return Path("/library/resolved.mkv")


E.resolve_under_library = _spy

# Same for the API call: the rungs below it only run when the row itself is
# short of a path, so the metadata it returns is the fixture.
metadata_reply = {}
api_calls = []


def _api(cmd, **kw):
    api_calls.append((cmd, kw))
    return metadata_reply


E.tautulli_api = _api

logged = []
E.log = lambda msg, *a, **k: logged.append(str(msg))


def resolve(item, quiet=False):
    del seen[:], api_calls[:], logged[:]
    return E.extract_file_path(item, quiet=quiet)


# ── Rung 1: the row's own keys ──────────────────────────────────────────────
for key in ("file", "file_path", "media_file", "location", "path"):
    resolve({key: f"/data/Film (2020)/{key}.mkv", "file_size": "700"})
    check(f"a row's {key!r} resolves without an API call",
          seen == [(f"/data/Film (2020)/{key}.mkv", 700)] and not api_calls, seen)

# Order matters: "file" wins when a row carries more than one.
resolve({"file": "/data/A/first.mkv", "path": "/data/A/second.mkv"})
check("the first key in the ladder wins", seen[0][0] == "/data/A/first.mkv", seen)

# ── Rung 2: the row's nested media_info ─────────────────────────────────────
resolve({"file_size": "500", "media_info": [{"file": "/data/B/b.mkv", "file_size": "900"}]})
check("a path in the row's media_info resolves", seen == [("/data/B/b.mkv", 900)], seen)
check("...without calling Tautulli", not api_calls, api_calls)

# The media entry's own size describes ITS file; the row total is the fallback
# when that entry has none.
resolve({"file_size": "500", "media_info": [{"file": "/data/B/b.mkv"}]})
check("a media entry with no size falls back to the row's",
      seen == [("/data/B/b.mkv", 500)], seen)
resolve({"file_size": "500", "media_info": [{"file": "/data/B/b.mkv", "file_size": "0"}]})
check("...and a zero size counts as none, not as zero bytes",
      seen == [("/data/B/b.mkv", 500)], seen)

# A media_info that is not a list is data, not a crash.
resolve({"media_info": {"file": "/data/B/b.mkv"}, "rating_key": None})
check("a media_info that is not a list gives up rather than raising", not seen, seen)

# ── No path anywhere and no rating_key: nothing to ask about ────────────────
out = resolve({"title": "Nameless"})
check("a row with no path and no rating_key resolves to None", out is None, out)
check("...without calling Tautulli", not api_calls, api_calls)

# ── Rung 3: the live get_metadata call ──────────────────────────────────────
metadata_reply = {"file": "/data/C/c.mkv"}
resolve({"rating_key": "77", "file_size": "1200"})
check("a row with only a rating_key is looked up",
      api_calls == [("get_metadata", {"rating_key": "77", "media_info": 1})], api_calls)
check("...and the metadata's path resolves with the ROW's size",
      seen == [("/data/C/c.mkv", 1200)], seen)

# ── Rung 4: the metadata's media_info ───────────────────────────────────────
metadata_reply = {"media_info": [{"file": "/data/D/d.mkv", "file_size": "2400"}]}
resolve({"rating_key": "78", "file_size": "1200"})
check("a path in the metadata's media_info resolves",
      seen == [("/data/D/d.mkv", 2400)], seen)

# ── Rung 5: the parts inside that media_info ────────────────────────────────
metadata_reply = {"media_info": [{"file_size": "2400",
                                  "parts": [{"file": "/data/E/e.mkv", "file_size": "2401"}]}]}
resolve({"rating_key": "79", "file_size": "1200"})
check("a path in a media part resolves", seen == [("/data/E/e.mkv", 2401)], seen)

metadata_reply = {"media_info": [{"file_size": "2400", "parts": [{"file": "/data/E/e.mkv"}]}]}
resolve({"rating_key": "80", "file_size": "1200"})
check("a part with no size falls back to its media entry's, not the row's",
      seen == [("/data/E/e.mkv", 2400)], seen)

metadata_reply = {"media_info": [{"parts": [{"file": "/data/E/e.mkv"}]}]}
resolve({"rating_key": "81", "file_size": "1200"})
check("...and back to the row's when the media entry has none either",
      seen == [("/data/E/e.mkv", 1200)], seen)

# The whole ladder in one reply: the earliest rung wins.
metadata_reply = {"file": "/data/F/top.mkv",
                  "media_info": [{"file": "/data/F/mid.mkv",
                                  "parts": [{"file": "/data/F/part.mkv"}]}]}
resolve({"rating_key": "82"})
check("the metadata's own key beats its media_info and parts",
      seen[0][0] == "/data/F/top.mkv", seen)

# ── The end of the ladder ───────────────────────────────────────────────────
# Nothing resolved, and the run has to be told which title it could not place —
# a silent None here is a movie quietly missing from every count.
metadata_reply = {"media_info": [{"parts": [{}]}], "title": "Ghost"}
out = resolve({"rating_key": "83", "title": "Ghost"})
check("a metadata reply with no path anywhere resolves to None", out is None, out)
check("...and says which title it gave up on",
      any("metadata_no_file_path" in m and "Ghost" in m for m in logged), logged)

out = resolve({"rating_key": "84", "title": "Ghost"}, quiet=True)
check("...unless the caller asked to be quiet (the pre-scan resolver)",
      out is None and not any("metadata_no_file_path" in m for m in logged), logged)

# ── Tautulli falling over mid-ladder aborts the run ─────────────────────────
# The alternative is a movie silently unresolvable, which reads downstream as
# "not eligible" — a run that quietly deletes a different film instead.
def _boom(cmd, **kw):
    raise RuntimeError("connection reset")


E.tautulli_api = _boom
aborted = []


def _abort(message, *, detail=None, **kw):
    """The real one raises SystemExit and the code after it counts on that:
    with a stub that merely records and returns, the next line reads a
    `metadata` that was never assigned. So this raises too."""
    aborted.append((message, detail))
    raise SystemExit(1)


E._abort_api_failure = _abort
try:
    resolve({"rating_key": "85", "title": "Interrupted"})
    raised = False
except SystemExit:
    raised = True
check("Tautulli failing during the lookup aborts the run", raised)
check("...rather than returning None, which downstream reads as 'not eligible'",
      len(aborted) == 1 and "Nothing was deleted" in aborted[0][0], aborted)
check("...naming the title and the rating_key for the log",
      aborted and "Interrupted" in (aborted[0][1] or "")
      and "85" in (aborted[0][1] or ""), aborted)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
