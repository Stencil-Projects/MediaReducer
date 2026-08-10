"""What the media-path check TELLS you, and which answers stop a run.

The check compares the file paths a media server reports against what is
actually under /library. Its verdict is the wrong-library tripwire: a server
describing a library that is not the one mounted must block a Cleanup rather
than let it delete against a backup. So two things matter and both are pinned
here — whether an outcome blocks, and the exact words, since the words are the
only instructions anyone gets for fixing a mount.

Each server names itself five separate times in those words: which one
reported, which debug button to press, which rescan clears it. The two halves
of this check are near-identical apart from that, which makes them a standing
invitation to be merged into one parameterised block — and a merge that gets a
slot wrong tells a Jellyfin user to go press the Tautulli button. That is what
these assertions exist to catch.

The compatibility state is stubbed. What is under test is the reading of a
verdict, not the sampling that produces one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
_tmpout.config()
import app as A                                    # noqa: E402

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def compat(server, **kw):
    c = {"server": server, "checked": 10, "ok": True, "unmatched": 0,
         "unmatched_examples": [], "folder_entries": 0, "mismatch_problem": False,
         "size_mismatched": 0, "size_checked": 0}
    c.update(kw)
    return c


def judge(verdicts, *, use_plex=True, use_jellyfin=True, raises=()):
    """Drive the judge with a canned verdict per server, and hand back the
    messages it produced. The stored verdict is cleared each time: a retained
    one from a previous case would replay into the next and read as a fresh
    finding."""
    A._MEDIA_PATH_VERDICT["servers"] = {}
    A._MEDIA_PATH_VERDICT["sig"] = None
    errors, warnings = [], []
    orig = A._media_path_compatibility_state

    def fake(server_name, _paths):
        if server_name in raises:
            raise RuntimeError("sampler down")
        return verdicts[server_name]
    A._media_path_compatibility_state = fake
    try:
        rows, blocker = A._judge_media_paths(
            pre={"tautulli_paths": [("x", 1)], "jellyfin_paths": [("x", 1)]},
            use_plex=use_plex, use_jellyfin=use_jellyfin,
            tautulli_connected=use_plex, jellyfin_connected=use_jellyfin,
            dirs_sig="sig-1", errors=errors, warnings=warnings,
            add_error=lambda m, *a, **k: errors.append(m),
            add_warning=lambda m, *a, **k: warnings.append(m))
    finally:
        A._media_path_compatibility_state = orig
    return errors, warnings, rows, blocker


ONLY = {"Plex/Tautulli": "Plex/Tautulli", "Jellyfin": "Jellyfin"}
# The five naming slots, per server, exactly as each half spells them.
NAMES = {
    "Plex/Tautulli": {"reports": "Plex", "button": "Tautulli", "stale": "Plex",
                      "clears": "a Plex/Tautulli rescan clears this",
                      "sampled": "sampled Plex paths", "entries": "sampled Plex entries"},
    "Jellyfin":      {"reports": "Jellyfin", "button": "Jellyfin", "stale": "Jellyfin",
                      "clears": "a Jellyfin library scan clears this",
                      "sampled": "sampled Jellyfin paths", "entries": "sampled Jellyfin entries"},
}

for srv, n in NAMES.items():
    other = "Jellyfin" if srv == "Plex/Tautulli" else "Plex"
    solo = {"use_plex": srv == "Plex/Tautulli", "use_jellyfin": srv == "Jellyfin"}

    # ── Nothing to check ─────────────────────────────────────────────────────
    e, w, rows, blk = judge({srv: compat(srv, checked=0, folder_entries=3)}, **solo)
    check(f"[{srv}] folder-only sample warns and does not block",
          not e and not blk and any("folder entries (3)" in x for x in w), (e, w, blk))
    check(f"[{srv}] ...naming that server", any(ONLY[srv] in x for x in w), w)

    e, w, _, blk = judge({srv: compat(srv, checked=0)}, **solo)
    check(f"[{srv}] connected but no paths warns and does not block",
          not e and not blk and any("no movie paths were available" in x for x in w), (e, w))

    # ── The wrong library: both spellings block ──────────────────────────────
    e, w, _, blk = judge({srv: compat(srv, ok=False, mismatch_problem=True,
                                      size_mismatched=8, size_checked=10)}, **solo)
    check(f"[{srv}] a size mismatch BLOCKS", blk and len(e) == 1, (e, blk))
    check(f"[{srv}] ...says which server reported the sizes",
          f"what {n['reports']} reports" in e[0] or f"what {n['reports']} " in e[0], e[0])
    check(f"[{srv}] ...and calls it the wrong library", "wrong library" in e[0], e[0])
    check(f"[{srv}] ...naming no other server", other not in e[0], e[0])

    e, w, _, blk = judge({srv: compat(srv, ok=False, unmatched=9,
                                      unmatched_examples=["/m/A (2020)/A.mkv"])}, **solo)
    check(f"[{srv}] unmatched paths BLOCK", blk and len(e) == 1, (e, blk))
    check(f"[{srv}] ...counted against that server's samples", n["sampled"] in e[0], e[0])
    check(f"[{srv}] ...quoting the example path", "/m/A (2020)/A.mkv" in e[0], e[0])
    check(f"[{srv}] ...pointing at the right debug button",
          f"{n['button']} debug button" in e[0], e[0])
    check(f"[{srv}] ...naming no other server", other not in e[0], e[0])

    # ── A few misses are ordinary ────────────────────────────────────────────
    e, w, _, blk = judge({srv: compat(srv, ok=True, unmatched=2,
                                      unmatched_examples=["/m/B (2021)/B.mkv"])}, **solo)
    check(f"[{srv}] a few unmatched entries warn and do NOT block",
          not e and not blk and len(w) == 1, (e, w, blk))
    check(f"[{srv}] ...described as that server's stale entries",
          f"stale {n['stale']} entry" in w[0], w[0])
    check(f"[{srv}] ...telling you the rescan that clears it", n["clears"] in w[0], w[0])
    check(f"[{srv}] ...naming no other server", other not in w[0], w[0])

    # ── A sampler that fell over ─────────────────────────────────────────────
    e, w, rows, blk = judge({}, raises=(srv,), **solo)
    check(f"[{srv}] a failed sampler warns, never blocks",
          not e and not blk and any("Could not check" in x for x in w), (e, w, blk))
    check(f"[{srv}] ...and records no verdict row", rows == [], rows)

# ── Both servers at once ─────────────────────────────────────────────────────
e, w, rows, blk = judge({"Plex/Tautulli": compat("Plex/Tautulli", ok=True),
                         "Jellyfin": compat("Jellyfin", ok=False, unmatched=9,
                                            unmatched_examples=["/m/C.mkv"])})
check("one healthy server does not excuse a broken one", blk is True, blk)
check("...and both are reported", len(rows) == 2, rows)
check("...with the error naming only the broken one",
      len(e) == 1 and "Jellyfin" in e[0] and "Plex" not in e[0], e)

# ── A verdict survives a probe that could not re-judge ───────────────────────
# One hiccup must not flap an active error to "no problem" for a single
# response and back again on the next probe.
A._MEDIA_PATH_VERDICT["servers"] = {}
errors, warnings = [], []
orig = A._media_path_compatibility_state
A._media_path_compatibility_state = lambda s, p: compat(s, ok=False, unmatched=9,
                                                        unmatched_examples=["/m/D.mkv"])
try:
    A._judge_media_paths(pre={"tautulli_paths": [("x", 1)]}, use_plex=True, use_jellyfin=False,
                         tautulli_connected=True, jellyfin_connected=False, dirs_sig="sig-1",
                         errors=errors, warnings=warnings,
                         add_error=lambda m, *a, **k: errors.append(m),
                         add_warning=lambda m, *a, **k: warnings.append(m))
    stored = bool(A._MEDIA_PATH_VERDICT["servers"].get("Plex/Tautulli"))
    # Second pass: the sampler now fails, so nothing fresh is judged.
    A._media_path_compatibility_state = lambda s, p: (_ for _ in ()).throw(RuntimeError("down"))
    e2, w2 = [], []
    rows2, blk2 = A._judge_media_paths(
        pre={"tautulli_paths": [("x", 1)]}, use_plex=True, use_jellyfin=False,
        tautulli_connected=True, jellyfin_connected=False, dirs_sig="sig-1",
        errors=e2, warnings=w2,
        add_error=lambda m, *a, **k: e2.append(m),
        add_warning=lambda m, *a, **k: w2.append(m))
finally:
    A._media_path_compatibility_state = orig
check("a judged verdict is stored", stored)
check("a probe that could not re-judge replays the stored error",
      blk2 is True and any("match nothing" in x for x in e2), (e2, blk2))
check("...and still reports the server's row", len(rows2) == 1, rows2)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
