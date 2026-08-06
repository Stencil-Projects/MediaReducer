"""resolve_under_library, fingerprint-only — the join between what a media
server SAYS about a file and what is actually on disk.

Resolution never lines path prefixes up: the reported path's parent folder +
filename match against the per-run walk index of the library mount, so mount
layouts never need to agree. The server's byte size only DISAMBIGUATES
same-named copies — it never rejects a folder+filename match, because the
local file is the size authority and server metadata goes stale after quality
upgrades. Pinned here:

  • a unique folder+filename resolves from ANY prefix, size or no size —
    including a stale (wrong) size;
  • same-named copies are disambiguated by size, and unresolvable without it;
  • the (filename, size) index rescues a path whose folder name matches
    nothing — and refuses to guess between identical twins;
  • a bare filename never matches (folder identity is required);
  • the index covers the whole library mount, not just the monitored dirs;
  • the per-run index reset;
  • the wrong-library tripwire: MOST samples mismatching in size fails the
    pre-check run outright, while a few only warn.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import engine as E  # noqa: E402
import shared  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

_OUT = tempfile.mkdtemp(prefix="mr-resolve.")
atexit.register(shutil.rmtree, _OUT, True)
LIB = Path(_OUT) / "library"
E.LIBRARY_ROOT = LIB
E.MONITOR_DIRS = [str(LIB / "Movies"), str(LIB / "Movies LQ")]
E.log = lambda *a, **k: None

def mk(rel, size):
    p = LIB / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p

hd = mk("Movies/Heat (1995)/Heat (1995).mkv", 5000)
lq = mk("Movies LQ/Heat (1995)/Heat (1995).mkv", 800)
solo = mk("Movies/Solo Film (2020)/Solo Film (2020).mkv", 1234)
twin_a = mk("Movies/Twin (2021)/Twin (2021).mkv", 400)
twin_b = mk("Movies LQ/Twin (2021)/Twin (2021).mkv", 400)
extra = mk("Other/Extra Film (2019)/Extra Film (2019).mkv", 555)

E._reset_library_file_index()

# ── Folder + filename is the identity; prefixes never matter ────────────────
check("a unique folder+filename resolves from any prefix, no size needed",
      E.resolve_under_library("/data/Movies/Solo Film (2020)/Solo Film (2020).mkv") == solo
      and E.resolve_under_library("/entirely/unrelated/Solo Film (2020)/Solo Film (2020).mkv") == solo)
check("a STALE size never rejects a unique folder+filename match",
      E.resolve_under_library("/data/Movies/Solo Film (2020)/Solo Film (2020).mkv",
                              expected_size=999_999) == solo)
check("a bare filename never matches — folder identity is required",
      E.resolve_under_library("/Solo Film (2020).mkv") is None)
check("a path already under the library shortcut-checks existence",
      E.resolve_under_library(str(solo)) == solo
      and E.resolve_under_library(str(LIB / "Movies" / "nope.mkv")) is None)

# ── Same-named copies: the size names the one the server means ──────────────
check("two same-named copies are unresolvable without a size",
      E.resolve_under_library("/films/Heat (1995)/Heat (1995).mkv") is None)
check("the size fingerprint picks the copy the server is describing",
      E.resolve_under_library("/films/Heat (1995)/Heat (1995).mkv",
                              expected_size=800) == lq)
check("...and the other size picks the other copy",
      E.resolve_under_library("/films/Heat (1995)/Heat (1995).mkv",
                              expected_size=5000) == hd)
check("a size matching NEITHER copy resolves nothing (never guess)",
      E.resolve_under_library("/films/Heat (1995)/Heat (1995).mkv",
                              expected_size=42) is None)

# ── No folder match: the (filename, size) index is the rescue ───────────────
check("a flat/renamed layout resolves through the (filename, size) index",
      E.resolve_under_library("/completely/other/root/Solo Film (2020).mkv",
                              expected_size=1234) == solo)
check("identical twins (same name AND size) are never guessed between",
      E.resolve_under_library("/other/root/Twin (2021).mkv",
                              expected_size=400) is None)

# ── The index is the whole mount, not just the monitored dirs ───────────────
# A server section outside the monitored dirs must still resolve (the scan
# counts it as outside_monitored_dirs; the pre-check must not read it as a
# broken mount). Deletability is is_safe_to_delete's question, not this one's.
check("a file under the library but outside every monitored dir still resolves",
      E.resolve_under_library("/srv/Other/Extra Film (2019)/Extra Film (2019).mkv") == extra)

# ── Folder paths never resolve — media matches by file name + size only ─────
# Servers also report DIRECTORY paths (section locations, folder-shaped
# items). Those are not media entries and the fingerprint never matches
# them: knowing whether media lines up is a file-name + size question, and
# the only path that matters is where a deletion actually happens.
check("a movie FOLDER path resolves nothing",
      E.resolve_under_library("/x/Movies/Solo Film (2020)") is None)
check("a section location resolves nothing",
      E.resolve_under_library("/data/Movies") is None)

# ── The index is per-run ────────────────────────────────────────────────────
moved = mk("Movies/Moved Film (2022)/Moved Film (2022).mkv", 777)
check("a file added after the index was built is invisible to it...",
      E.resolve_under_library("/x/Moved Film (2022)/Moved Film (2022).mkv") is None)
E._reset_library_file_index()
check("...and visible after the per-run reset",
      E.resolve_under_library("/x/Moved Film (2022)/Moved Film (2022).mkv") == moved)

# ── The wrong-library tripwire ──────────────────────────────────────────────
# Every path resolves, but MOST files' bytes differ from what the server
# reports: that is a stale backup (or the wrong copy) mounted at /library —
# the pre-check fails the run in the checking phase rather than scoring and
# deleting against a library the server is not describing. A single stale
# size (a quality upgrade) only warns.
E.USE_PLEX, E.USE_JELLYFIN = True, False
_aborted = {}
def _fake_abort(msg, **kw):
    _aborted.update({"msg": msg, "phase": kw.get("phase")})
    raise SystemExit(2)
E._abort_api_failure = _fake_abort

def _samples_with(mismatches):
    good = [(f"/data/Movies/Heat (1995)/Heat (1995).mkv", 5000)]
    bad = [("/d/Movies/Solo Film (2020)/Solo Film (2020).mkv", 1),
           ("/d/Other/Extra Film (2019)/Extra Film (2019).mkv", 2),
           ("/d/Movies/Moved Film (2022)/Moved Film (2022).mkv", 3)]
    return good + bad[:mismatches]

E._sample_tautulli_reported_paths = lambda: _samples_with(3)   # 3 of 4 differ
_aborted.clear()
try:
    E.verify_media_path_compatibility()
    check("a mismatch MAJORITY fails the pre-check", False, "no abort raised")
except SystemExit:
    check("a mismatch MAJORITY fails the pre-check",
          "WRONG library" in _aborted.get("msg", ""), _aborted)
    check("...in the checking phase, under the progress bar",
          _aborted.get("phase") == "checking", _aborted)

E._sample_tautulli_reported_paths = lambda: _samples_with(1)   # 1 of 2 differs
_aborted.clear()
try:
    E.verify_media_path_compatibility()
    check("a single stale size (a quality upgrade) does NOT fail the run",
          not _aborted, _aborted)
except SystemExit:
    check("a single stale size (a quality upgrade) does NOT fail the run",
          False, _aborted)

# FOLDER samples are not movie file entries: the pre-check drops them before
# judging anything, so a server that hands out section locations can neither
# fail the path match nor feed the size tally.
E._sample_tautulli_reported_paths = lambda: (
    [("/x/Movies/Solo Film (2020)", 999)] * 4          # folders: ignored
    + [("/d/Movies/Heat (1995)/Heat (1995).mkv", 5000)])   # one honest file
_aborted.clear()
try:
    E.verify_media_path_compatibility()
    check("folder samples are excluded from the check entirely", not _aborted, _aborted)
except SystemExit:
    check("folder samples are excluded from the check entirely", False, _aborted)

check("the tripwire needs at least 3 mismatches AND a majority",
      shared.size_mismatch_problem(4, 3) is True
      and shared.size_mismatch_problem(4, 2) is False
      and shared.size_mismatch_problem(2, 2) is False
      and shared.size_mismatch_problem(6, 3) is False
      and shared.size_mismatch_problem("x", "y") is False)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
