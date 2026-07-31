"""The app's store/config read caches — correctness of the fast paths:

  • load_config hands out a private copy (_copy_config): equal to the config,
    never aliasing the memo, so a caller mutating its copy can't corrupt it;
  • the per-request store snapshot serves repeated reads within one request,
    but an app-side write inside that request invalidates it, so a handler
    that writes then reads sees its own change;
  • a change made between requests is always picked up;
  • background threads (no request context) are unaffected;
  • single-flight compose — when a store change invalidates the memo, N
    concurrent refreshes compose the store ONCE between them rather than each
    rebuilding the same dict, and every one of them still sees the new data.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_OUT = tempfile.mkdtemp(prefix="mr-store-cache.")
atexit.register(shutil.rmtree, _OUT, True)
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/movies"]},
          open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ── load_config's handout copy: equal to deepcopy, never aliasing the memo ───
import copy as _copy

_cfg = A.load_config()
check("_copy_config matches deepcopy", A._copy_config(_cfg) == _copy.deepcopy(_cfg) == _cfg)
check("output_dir/db_path still resolve",
      str(A.output_dir()) == _OUT and A.db_path().name == "mediareducer.db")

_c = A.load_config()
_c["MONITOR_DIRS"].append("/tmp/must-not-stick")
check("mutating a handed-out list never reaches the memo",
      "/tmp/must-not-stick" not in (A.load_config().get("MONITOR_DIRS") or []))
_c2 = A.load_config()
_c2["_SERVICE_URL_DEFAULTS"] = dict(_c2.get("_SERVICE_URL_DEFAULTS") or {})
_c2["_SERVICE_URL_DEFAULTS"]["MUTATED"] = "x"
check("mutating a handed-out dict never reaches the memo",
      "MUTATED" not in (A.load_config().get("_SERVICE_URL_DEFAULTS") or {}))
check("an unexpected value type still round-trips",
      A._copy_config({"k": {"nested": [1, 2, {"deep": True}]}})
      == {"k": {"nested": [1, 2, {"deep": True}]}})


# ── Per-request store snapshot ───────────────────────────────────────────────
# Reads inside one request come from a single composition...
with A.app.test_request_context("/api/status"):
    first = A._cache_file_data()
    check("repeated reads in one request reuse the same snapshot",
          A._cache_file_data() is first)

# ...but an app-side write inside the request drops it, so the handler sees
# its own change rather than the snapshot it took a moment earlier.
with A.app.test_request_context("/api/status"):
    A.reopen_daily_window(reason="store-cache test")
    check("window reads open after reopen", A._headroom_window_used_today() is False)
    A.burn_daily_window_on_startup(reason="store-cache test")
    check("write-then-read inside one request sees the write",
          A._headroom_window_used_today() is True)

# Changes made between requests are always visible.
client = A.app.test_client()
A.reopen_daily_window(reason="store-cache test 2")
_open = client.get("/api/status").get_json().get("headroom_window_used_today")
A.burn_daily_window_on_startup(reason="store-cache test 2")
_burned = client.get("/api/status").get_json().get("headroom_window_used_today")
check("a change between requests is picked up", _open is False and _burned is True)

# No request context (the scheduler tick, run workers) still works.
A.reopen_daily_window(reason="store-cache test 3")
check("store reads work outside a request context",
      A._headroom_window_used_today() is False)

# One store validation per request instead of one per consult.
_calls = {"n": 0}
_orig_fp = A.db.data_fingerprint
def _counting_fp(p):
    _calls["n"] += 1
    return _orig_fp(p)
A.db.data_fingerprint = _counting_fp
client.get("/api/status")          # warm
_calls["n"] = 0
client.get("/api/status")
A.db.data_fingerprint = _orig_fp
check("one store fingerprint check per request", _calls["n"] == 1)


# ── Single-flight compose under concurrent refreshes ─────────────────────────
# Every run, Summary and tick changes the store. Without a compose lock, each
# open page's next poll composed the whole store independently — N tabs paid N
# identical composes at the same moment. One thread should compose; the rest
# wait and take its result.
import threading
import functools

_composes = {"n": 0}
_orig_compose = A.db.read_cache_dict

@functools.wraps(_orig_compose)
def _counting_compose(path):
    _composes["n"] += 1
    return _orig_compose(path)

A.db.read_cache_dict = _counting_compose

def _bump_store():
    """A real app-side write, exactly like a tick's window burn."""
    if A._headroom_window_used_today():
        A.reopen_daily_window(reason="cache concurrency test")
    else:
        A.burn_daily_window_on_startup(reason="cache concurrency test")

_client = A.app.test_client()
_client.get("/api/status")          # warm the memo
_bump_store()                       # …then move the store underneath it
_composes["n"] = 0
_threads = [threading.Thread(target=lambda: _client.get("/api/status")) for _ in range(8)]
[t.start() for t in _threads]
[t.join() for t in _threads]
check("8 concurrent pollers compose the store once, not eight times",
      _composes["n"] == 1)

# Correctness still comes first: after a write, concurrent readers must see the
# NEW value, never a memo entry from before it.
_bump_store()
_expected = A._headroom_window_used_today()
_seen = []
_threads = [threading.Thread(
    target=lambda: _seen.append(
        _client.get("/api/status").get_json().get("headroom_window_used_today")))
    for _ in range(8)]
[t.start() for t in _threads]
[t.join() for t in _threads]
check("every concurrent reader sees the post-write value",
      len(_seen) == 8 and all(v is _expected for v in _seen))

# A second version change must compose again — the lock must not pin stale data.
_composes["n"] = 0
_bump_store()
_client.get("/api/status")
check("a later store change composes again", _composes["n"] == 1)

# Background threads (no request context) share the same single-flight path.
_composes["n"] = 0
_bump_store()
_bg = [threading.Thread(target=A._cache_file_data) for _ in range(6)]
[t.start() for t in _bg]
[t.join() for t in _bg]
check("background readers also compose once", _composes["n"] == 1)

A.db.read_cache_dict = _orig_compose

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
