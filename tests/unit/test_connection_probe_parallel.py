"""The connection check probes every service at once, and says the same thing.

Every Config save runs this check — connections can fail without any field
changing, and the dependent UI locks read off the current probe. Run in
sequence it costs the SUM of every server's latency: four health probes plus a
media-path sample per media server, each waiting on the last, so a save against
four servers on a 200 ms LAN spends ~1.2 s before anything is written.

The probes share no state and none feeds another, so they run together and the
wall clock is the slowest single server instead of the total.

Two things have to hold:
  1. the verdict is unchanged — a parallel check that reports differently is
     worse than a slow one, so the whole health state is compared against the
     sequential order for every reachable/unreachable combination;
  2. one dead server can't stall the rest, which is the case that hurts most —
     an unreachable host burns its full timeout, and in sequence every other
     server waited behind it.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-probe-par.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A

# A real file under the library root: the check resolves each reported media
# path on disk, so without one every scenario would report a path mismatch and
# drown out the connection verdict being tested here.
_MOVIE = Path(_OUT, "movies", "A", "A.mkv")
_MOVIE.parent.mkdir(parents=True, exist_ok=True)
_MOVIE.write_bytes(b"x")

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


CFG = {"USE_PLEX": True, "TAUTULLI_URL": "http://taut:8181", "TAUTULLI_API_KEY": "k",
       "PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "t",
       "USE_JELLYFIN": True, "JELLYFIN_URL": "http://jelly:8096", "JELLYFIN_API_KEY": "j",
       "RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "r"}

DELAY = 0.0
DOWN: set = set()
_inflight = {"now": 0, "peak": 0}
_lock = threading.Lock()

def fake_request(url, headers=None, timeout=None, **kw):
    with _lock:
        _inflight["now"] += 1
        _inflight["peak"] = max(_inflight["peak"], _inflight["now"])
    try:
        if DELAY:
            time.sleep(DELAY)
        for bad in DOWN:
            if bad in url:
                raise OSError(f"simulated outage: {bad}")
        # Tautulli wraps every reply in a response/result envelope; returning the
        # bare payload makes its client raise, which would leave the media-path
        # sample untested while every assertion still passed.
        if "get_libraries" in url:
            return {"response": {"result": "success",
                                 "data": [{"section_id": "1", "section_type": "movie",
                                           "is_active": 1}]}}
        if "get_library_media_info" in url:
            return {"response": {"result": "success",
                                 "data": {"data": [{"file": str(_MOVIE)}]}}}
        if "/Items" in url:
            return {"Items": [{"Path": str(_MOVIE)}]}
        return {"response": {"result": "success", "data": {}}, "ok": True}
    finally:
        with _lock:
            _inflight["now"] -= 1

A._json_request = fake_request
A._appdata_mount_state = lambda: {k: {"ok": True, "mounted": True, "path": "/" + k}
                                  for k in ("tautulli", "plex", "radarr", "jellyfin")}
A._filesystem_rw_state = lambda: {"ok": True, "writable": True, "errors": [], "warnings": []}


def health(cfg=None, *, probe=True):
    h = A._connection_health_state(dict(cfg or CFG), probe=probe)
    h.pop("appdata", None)
    return h


# ── The verdict is what matters, so pin it for every combination ─────────────
# Each server's reachability is independent, so every subset is a distinct
# verdict the UI acts on (which sections lock, which fields highlight).
VERDICTS = {}
for _down in ((), ("taut",), ("plex",), ("jelly",), ("radarr",),
              ("taut", "jelly"), ("plex", "radarr"),
              ("taut", "plex", "jelly", "radarr")):
    DOWN.clear(); DOWN.update(_down)
    VERDICTS[_down] = health()

check("all four reachable is a clean bill of health",
      VERDICTS[()]["critical_ok"] is True and not VERDICTS[()]["errors"])
check("Tautulli down is a critical failure (Plex needs it)",
      VERDICTS[("taut",)]["critical_ok"] is False
      and any("Tautulli" in e for e in VERDICTS[("taut",)]["errors"]))
check("Jellyfin down is a critical failure while selected",
      VERDICTS[("jelly",)]["critical_ok"] is False
      and any("Jellyfin" in e for e in VERDICTS[("jelly",)]["errors"]))
check("Radarr down only warns — it is optional",
      VERDICTS[("radarr",)]["critical_ok"] is True
      and any("Radarr" in w for w in VERDICTS[("radarr",)]["warnings"]))
check("Plex down only warns — Tautulli carries the library",
      VERDICTS[("plex",)]["critical_ok"] is True
      and any("Plex" in w for w in VERDICTS[("plex",)]["warnings"]))
check("a combined outage reports BOTH failures, not just the first",
      any("Tautulli" in e for e in VERDICTS[("taut", "jelly")]["errors"])
      and any("Jellyfin" in e for e in VERDICTS[("taut", "jelly")]["errors"]))
check("everything down reports every service",
      all(any(name in " ".join(VERDICTS[("taut","plex","jelly","radarr")]["errors"]
                               + VERDICTS[("taut","plex","jelly","radarr")]["warnings"])
              for name in [n])
          for n in ("Tautulli", "Plex", "Jellyfin", "Radarr")))

# Order independence: a parallel check completes in whatever order the network
# answers, so the SAME inputs must always produce the same verdict.
DOWN.clear(); DOWN.update(("plex", "radarr"))
check("repeated checks agree (no completion-order dependence)",
      all(health() == VERDICTS[("plex", "radarr")] for _ in range(5)))

DOWN.clear()
check("probe=False stays offline — no requests, no connection claims",
      health(probe=False)["probed"] is False)


# ── They actually overlap, and a dead server doesn't hold up the rest ────────
DOWN.clear()
_inflight["peak"] = 0
DELAY = 0.05
_t0 = time.time(); health(); _elapsed = time.time() - _t0
DELAY = 0.0
check("probes overlap rather than queue (more than one in flight)",
      _inflight["peak"] > 1)
# Six requests at 50 ms each is 0.30 s in sequence. The two media-path samples
# each need a second, dependent call, so the floor is two waves — not one.
check(f"six 50 ms requests finish in about two waves, not six ({_elapsed:.2f}s)",
      _elapsed < 0.22)

# The case that hurts most: one host is a black hole while the others are fine.
DOWN.clear(); DOWN.update(("jelly",))
_inflight["peak"] = 0
DELAY = 0.05
_t0 = time.time(); _h = health(); _stalled = time.time() - _t0
DELAY = 0.0
check("one unreachable server does not serialize the healthy ones",
      _stalled < 0.22)
check("...and the outage is still reported",
      _h["critical_ok"] is False and any("Jellyfin" in e for e in _h["errors"]))

# A sample is only collected once its own server has answered. The samplers
# allow a LONGER timeout than the health probes (a big library legitimately
# takes longer than /System/Info), so waiting on a sample whose host just
# proved unreachable made an outage slower than checking in sequence — and the
# result was discarded anyway. Model it: the health probe fails fast, the
# sample hangs. The check must not wait for the sample.
SLOW_SAMPLE = {"on": False}
_base_request = fake_request
def hanging_sample(url, headers=None, timeout=None, **kw):
    # ONLY the unreachable server's sample hangs. Tautulli is healthy here and
    # its sample is legitimately waited for — slowing that one too would just
    # measure the wait we actually want.
    if SLOW_SAMPLE["on"] and "/Items" in url:
        time.sleep(1.0)                      # far beyond the health probe
    return _base_request(url, headers=headers, timeout=timeout, **kw)
A._json_request = hanging_sample
SLOW_SAMPLE["on"] = True
DOWN.clear(); DOWN.update(("jelly",))        # Jellyfin health fails immediately
_t0 = time.time(); _h = health(); _abandoned = time.time() - _t0
check(f"a sample for an unreachable server is not waited on ({_abandoned:.2f}s)",
      _abandoned < 0.5)
check("...and that outage is still reported",
      _h["critical_ok"] is False and any("Jellyfin" in e for e in _h["errors"]))
SLOW_SAMPLE["on"] = False
A._json_request = _base_request

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
