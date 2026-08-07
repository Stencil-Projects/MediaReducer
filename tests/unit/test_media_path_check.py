"""The configuration check's media-path layer, fingerprint-only.

_resolve_reported_media_path mirrors the engine's resolver: parent folder +
filename against a walk of the library mount, prefixes never lining up, the
server's byte count only disambiguating same-named copies (never rejecting a
folder+filename match). _media_path_compatibility_state verifies each sampled
size against the disk and trips the shared wrong-library rule — MOST samples
mismatching fails the check — while a stale size or two stays a pass (quality
upgrades the server hasn't rescanned are normal).
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-mediacheck.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
Path(_OUT, "config.json").write_text("{}")
import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

LIB = Path(_OUT) / "library"
A.FILESYSTEM_CHECK_PATH = str(LIB)

def mk(rel, size):
    p = LIB / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p

solo = mk("movies/Solo (2020)/Solo (2020).mkv", 1234)
hd = mk("movies/Heat (1995)/Heat (1995).mkv", 5000)
lq = mk("movies-lq/Heat (1995)/Heat (1995).mkv", 800)
A._MEDIA_FP_INDEX["at"] = 0.0   # force a fresh walk of the fixture

# ── The resolver mirrors the engine's fingerprint ladder ────────────────────
check("a unique folder+filename resolves from any prefix",
      A._resolve_reported_media_path("/data/x/Solo (2020)/Solo (2020).mkv") == solo)
check("a stale size never rejects a folder+filename match",
      A._resolve_reported_media_path("/data/x/Solo (2020)/Solo (2020).mkv",
                                     expected_size=999_999) == solo)
check("same-named copies need the size to disambiguate",
      A._resolve_reported_media_path("/y/Heat (1995)/Heat (1995).mkv") is None
      and A._resolve_reported_media_path("/y/Heat (1995)/Heat (1995).mkv",
                                         expected_size=800) == lq)
check("a bare filename never matches", A._resolve_reported_media_path("/Solo (2020).mkv") is None)
check("a path already under the library shortcut-checks existence",
      A._resolve_reported_media_path(str(solo)) == solo)
check("a FOLDER path never resolves — media matches by file name + size only",
      A._resolve_reported_media_path("/x/movies/Solo (2020)") is None
      and A._resolve_reported_media_path("/data/movies-lq") is None)
lookalike = mk("other/movies/Flat (2011).mkv", 999)
flat_real = mk("movies/Flat Films/Flat (2011).mkv", 5678)
A._MEDIA_FP_INDEX["at"] = 0.0
check("a size-exact match outranks a look-alike folder+name hit (flat layouts)",
      A._resolve_reported_media_path("/movies/Flat (2011).mkv", expected_size=5678)
      == flat_real
      and A._resolve_reported_media_path("/movies/Flat (2011).mkv") == lookalike)

# The explained variant is what the debug buttons print: the reason must say
# HOW a match happened and exactly why a miss missed.
_p, _why = A._resolve_media_path_explained("/x/movies/Solo (2020)/Solo (2020).mkv")
check("a match explains itself", _p == solo and "folder + file name" in _why)
_p, _why = A._resolve_media_path_explained("/Solo (2020).mkv")
check("a bare name explains the refusal", _p is None and "bare name" in _why)
_p, _why = A._resolve_media_path_explained("/y/Heat (1995)/Heat (1995).mkv")
check("ambiguity without a size names the problem",
      _p is None and "no size to pick" in _why)
_p, _why = A._resolve_media_path_explained("/y/Heat (1995)/Heat (1995).mkv", expected_size=42)
check("a size matching neither copy names the problem",
      _p is None and "none at the reported" in _why)
_p, _why = A._resolve_media_path_explained("/nowhere/Nothing Here (1900)/x.mkv")
check("a genuine miss names what was looked for",
      _p is None and "x.mkv" in _why and "Nothing Here (1900)" in _why)

# ── The check state: sizes verified, the wrong-library rule applied ─────────
def sample(path, size):
    return (path, size)

GOOD = [sample("/d/movies/Solo (2020)/Solo (2020).mkv", 1234),
        sample("/d/movies/Heat (1995)/Heat (1995).mkv", 5000),
        sample("/d/movies-lq/Heat (1995)/Heat (1995).mkv", 800)]
st = A._media_path_compatibility_state("Plex", GOOD)
check("all sizes agreeing is a clean pass",
      st["ok"] and st["matched"] == 3 and st["size_checked"] == 3
      and st["size_mismatched"] == 0 and not st["mismatch_problem"], st)

BAD = [sample("/d/movies/Solo (2020)/Solo (2020).mkv", 1),
       sample("/d/movies/Heat (1995)/Heat (1995).mkv", 5000),
       sample("/d/movies-lq/Heat (1995)/Heat (1995).mkv", 800)]
st = A._media_path_compatibility_state("Plex", BAD)
check("one stale size (a quality upgrade) stays a pass",
      st["ok"] and st["size_mismatched"] == 1 and not st["mismatch_problem"], st)

# Most samples mismatching: paths all resolve, but the bytes describe a
# different library — the check must FAIL with the mismatch evidence.
extra1 = mk("movies/One (2001)/One (2001).mkv", 10)
extra2 = mk("movies/Two (2002)/Two (2002).mkv", 20)
A._MEDIA_FP_INDEX["at"] = 0.0
WRONG = [sample("/d/movies/Solo (2020)/Solo (2020).mkv", 1),
         sample("/d/movies/One (2001)/One (2001).mkv", 2),
         sample("/d/movies/Two (2002)/Two (2002).mkv", 3),
         sample("/d/movies/Heat (1995)/Heat (1995).mkv", 5000)]
st = A._media_path_compatibility_state("Plex", WRONG)
check("a mismatch majority fails the check as the wrong library",
      not st["ok"] and st["mismatch_problem"] and st["size_mismatched"] == 3, st)
check("...with the evidence attached (reported vs on-disk bytes)",
      st["mismatch_examples"]
      and st["mismatch_examples"][0]["reported_bytes"] == 1
      and st["mismatch_examples"][0]["disk_bytes"] == 1234, st)

# A stale server entry — a ghost file renamed, upgraded, or removed since the
# server's last scan — warns instead of failing: it scans as missing and is
# never deleted. MOST samples unmatched is the wrong library, and that fails.
st = A._media_path_compatibility_state("Plex", [sample("/nope/Nowhere (1999)/x.mkv", 5)])
check("a lone stale entry (ghost file) warns instead of failing the check",
      st["ok"] and st["unmatched"] == 1 and not st["unmatched_problem"], st)
st = A._media_path_compatibility_state("Plex", [
    sample(f"/gone/Ghost {i} (1999)/Ghost {i} (1999).mkv", 5) for i in range(4)
] + [sample("/d/movies/Solo (2020)/Solo (2020).mkv", 1234)])
check("mostly-unmatched fails the check as the wrong library",
      not st["ok"] and st["unmatched_problem"] and st["unmatched"] == 4, st)

# Folder-shaped samples (section locations, folder items) are not movie file
# entries — the check drops them entirely rather than judging path layouts:
# they can neither fail the match nor feed the size tally.
st = A._media_path_compatibility_state("Plex", [
    sample("/x/movies/Solo (2020)", 999),        # a folder with a junk size
    sample("/data/movies", 0),                   # a section location
    sample("/d/movies/Solo (2020)/Solo (2020).mkv", 1234)])
check("folder samples are excluded from the check entirely",
      st["ok"] and st["checked"] == 1 and st["folder_entries"] == 2
      and st["unmatched"] == 0 and st["size_mismatched"] == 0, st)
st = A._media_path_compatibility_state("Plex", [sample("/x/movies/Solo (2020)", 0)])
check("nothing but folder samples reads as nothing to check, not a failure",
      st["ok"] and st["checked"] == 0, st)

# ── The Media path mapping debug renders the stored verdict ─────────────────
# Never judged: says so instead of pretending. Judged: shows each server's
# slot with its age, counts, and any error/warning lines it carries.
import time  # noqa: E402
A._MEDIA_PATH_VERDICT["servers"].clear()
with A.app.test_client() as c:
    body = c.post("/api/debug/media-paths", headers={"X-MediaReducer": "1"}).get_json()
check("the media-paths debug renders with an empty verdict",
      body and body.get("ok") and "never judged yet" in body.get("text", ""), body)
A._MEDIA_PATH_VERDICT["servers"]["Jellyfin"] = {
    "compat": {"checked": 4, "matched": 3, "unmatched": 1, "size_checked": 3,
               "size_mismatched": 0, "folder_entries": 2, "ok": True},
    "errors": [], "warnings": ["1 of 4 sampled Jellyfin entries match no file under /library"],
    "blocker": False, "at": time.time() - 120}
with A.app.test_client() as c:
    text = (c.post("/api/debug/media-paths", headers={"X-MediaReducer": "1"}).get_json() or {}).get("text", "")
check("...and a judged slot with its age, counts, and lines",
      "Jellyfin: judged 2 min ago" in text and "checked=4" in text
      and "folder entries ignored=2" in text
      and "warning: 1 of 4 sampled Jellyfin entries" in text, text[-400:])
A._MEDIA_PATH_VERDICT["servers"].clear()

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
