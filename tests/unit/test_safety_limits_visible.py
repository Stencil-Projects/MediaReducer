"""The safety percentage guards two things, in two directions, against two totals.

  • HEADROOM and REDLINE reserve free space, so each is capped at a share of the
    whole FILESYSTEM: at most total × MAX_HEADROOM_PCT%.
  • The LIBRARY SIZE CAP is a size to trim down to, so the danger is setting it
    too LOW, and the limit is a share of the LIBRARY: no lower than
    library × (100 − MAX_HEADROOM_PCT)%.

Reported: "a 21,000 GB setting on a 53,984 GB disk is 39%, why is that allowed?"
It is the Library Size Cap, and against the 23,489 GB library it trims 10.6% —
inside the 15% allowance. Reading it against the filesystem instead gives 39%
and a refusal that should not happen.

That mistake was easy to make because the app never showed its work: the only
output was "over the safety percentage", which names neither the limit nor which
total the percentage is of, so the line could only be found by moving the value
until the app objected. Both limits are now published in GB with the numbers
they came from, and both refusals state the figure.

The limits are published even when the threshold they bound is off — you read
them BEFORE typing a value, which is the moment they are worth having.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-safety.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


# The reporter's own figures, so the numbers below are the ones they read.
TOTAL, LIB, PCT = 53984.0, 23489.4, 15
CEILING = 8097.6          # 53,984 × 15%   — the Headroom/Redline ceiling
FLOOR = 19966.0           # 23,489.4 × 85% — the Library Size Cap floor
DISK = {"used_gb": 36829.2, "total_gb": TOTAL, "free_gb": 17154.8, "pct_used": 68.2}
A.disk_stats = lambda: dict(DISK)
A.library_stats = lambda: {"library_gb": LIB}
HEALTHY = {"critical_ok": True, "required_tooltip": "", "jellyfin_connected": True,
           "errors": [], "warnings": [], "blocked": False}
A._refresh_connection_health_cache = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_state = lambda cfg=None, **k: dict(HEALTHY)
A._connection_health_for_ui = lambda cfg=None: dict(HEALTHY)

BASE = {"OUTPUT_DIR": _OUT, "MONITOR_DIRS": ["/library/Movies HD"],
        "MAX_HEADROOM_PCT": PCT, "HEADROOM_GB": 0, "REDLINE_GB": None,
        "REDLINE_ONLY_MODE": False, "MAX_LIBRARY_GB": None,
        "MOVIE_CLEANUP_ENABLED": True, "USE_JELLYFIN": True,
        "JELLYFIN_URL": "http://jf.test", "JELLYFIN_API_KEY": "k"}

A.app.config["TESTING"] = True
client = A.app.test_client()


def state(**over):
    A.CONFIG_PATH.write_text(json.dumps({**BASE, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    return A._space_threshold_state(A.load_config(), dict(DISK), LIB)


# ── The two limits, at the values the arithmetic gives ────────────────────
st = state()
check("the filesystem ceiling is total × the percentage",
      st["safety_limit_gb"] == CEILING, st["safety_limit_gb"])
check("the cap floor is the library minus the percentage",
      st["cap_floor_gb"] == FLOOR, st["cap_floor_gb"])
check("...published with what they were worked out from",
      (st["safety_pct"], st["total_gb"], st["library_gb"]) == (PCT, TOTAL, LIB), st)
check("both are published with every threshold OFF — you read them before typing",
      st["safety_limit_gb"] and st["cap_floor_gb"], st)

# ── The reported case: a cap is judged against the LIBRARY ────────────────
# 21,000 GB is 39% of the filesystem and would be refused by the wrong rule.
st = state(MAX_LIBRARY_GB=21000)
check("a 21,000 GB cap on a 53,984 GB disk is fine — the cap is not a disk rule",
      st["safety_blocked"] is False, st["cleanup_tooltip"])
check("...and the same 21,000 GB as HEADROOM is refused, because that one is",
      state(HEADROOM_GB=21000)["safety_blocked"] is True)

# ── Each limit is exact, not approximate ──────────────────────────────────
for label, over, blocked in (
        ("headroom 1 GB under the ceiling", {"HEADROOM_GB": 8097}, False),
        ("headroom 1 GB over the ceiling", {"HEADROOM_GB": 8098}, True),
        ("redline-only 1 GB under the ceiling",
         {"REDLINE_GB": 8097, "REDLINE_ONLY_MODE": True}, False),
        ("redline-only 1 GB over the ceiling",
         {"REDLINE_GB": 8098, "REDLINE_ONLY_MODE": True}, True),
        ("cap exactly at the floor", {"MAX_LIBRARY_GB": 19966}, False),
        ("cap 1 GB below the floor", {"MAX_LIBRARY_GB": 19965}, True)):
    check(f"{label}: blocked={blocked}", state(**over)["safety_blocked"] is blocked)

# Redline needs no ceiling test of its own while Headroom is armed: it must sit
# strictly below Headroom, which is itself under the ceiling. Pin that, or the
# rule above would look like a hole.
st = state(HEADROOM_GB=8000, REDLINE_GB=9000)
check("with Headroom armed, a Redline above it is refused outright",
      st["ok_for_simulate"] is False and "lower than Headroom" in st["cleanup_tooltip"],
      st["cleanup_tooltip"])

# ── Every refusal states the figure it is refusing against ────────────────
for label, over, want in (
        ("headroom", {"HEADROOM_GB": 21000}, ["8,098 GB", "15%", "53,984 GB"]),
        ("redline-only", {"REDLINE_GB": 21000, "REDLINE_ONLY_MODE": True},
         ["8,098 GB", "53,984 GB"]),
        ("library cap", {"MAX_LIBRARY_GB": 19000}, ["19,966 GB", "15%", "23,489 GB"])):
    tip = state(**over)["cleanup_tooltip"]
    check(f"the {label} refusal names the limit and the total it is of",
          all(w in tip for w in want), tip)

# ── A check that cannot run HOLDS Cleanup; it does not wave it through ────
# Both limits need a measurement — the filesystem size for one, the library
# size for the other — and when the measurement was missing the check was
# skipped entirely. Skipping does not relax the limit, it deletes it: a 1 GB
# cap against a 23 TB library came back ok_for_cleanup=True, with the button
# that deletes immediately live and nothing between it and the whole library.
# Every other safety decision in _space_threshold_state fails closed; these two
# were the exceptions.
# Passing None for disk/library means "not supplied, go and read it" — the real
# absence is the READING coming back empty, so break the readers themselves.
def unmeasured(disk, lib, **over):
    A.CONFIG_PATH.write_text(json.dumps({**BASE, **over}))
    with A._config_memo_lock:
        A._config_memo["key"] = object()
    _d, _l = A.disk_stats, A.library_stats
    A.disk_stats = lambda: (dict(disk) if disk is not None else {})
    A.library_stats = lambda: ({"library_gb": lib} if lib is not None else {})
    try:
        return A._space_threshold_state(A.load_config(), None, None)
    finally:
        A.disk_stats, A.library_stats = _d, _l


ZERO_DISK = {"used_gb": 0, "total_gb": 0, "free_gb": 0, "pct_used": 0}
for label, disk, lib, over in (
        ("library reads 0", DISK, 0.0, {"MAX_LIBRARY_GB": 1}),
        ("library reading missing", DISK, None, {"MAX_LIBRARY_GB": 22000}),
        ("filesystem reads 0", ZERO_DISK, LIB, {"HEADROOM_GB": 50000}),
        ("filesystem reading missing", None, LIB, {"HEADROOM_GB": 4000}),
        ("filesystem reading missing, redline-only", None, LIB,
         {"REDLINE_GB": 4000, "REDLINE_ONLY_MODE": True})):
    st = unmeasured(disk, lib, **over)
    check(f"{label}: Cleanup is held, not allowed", st["ok_for_cleanup"] is False,
          st["cleanup_tooltip"])
    check(f"...and says the measurement is the problem ({label})",
          "can't be checked" in st["cleanup_tooltip"], st["cleanup_tooltip"])
    check(f"...while Simulate stays open — it is how the measurement arrives ({label})",
          st["ok_for_simulate"] is True)

# ...and only when there is something to check. A missing measurement for a
# threshold that is switched off is not a reason to hold anything, or the hold
# would fire on every fresh install.
check("no cap set: an unmeasured library holds nothing",
      unmeasured(DISK, None, HEADROOM_GB=4000)["ok_for_cleanup"] is True)
check("a measured library and disk with the reported settings stays clickable",
      unmeasured(DISK, LIB, MAX_LIBRARY_GB=22000)["ok_for_cleanup"] is True)

# ── And the page can show them without recomputing anything ───────────────
# A second copy of the formula in the browser is how a control comes to
# disagree with the rule it is reporting; these read the server's numbers.
html = client.get("/config").get_data(as_text=True)
for el in ("headroom-safety-note", "redline-safety-note", "cap-safety-note"):
    check(f"the config page has somewhere to put it ({el})", f'id="{el}"' in html)

js = (ROOT / "static/js/config.js").read_text(encoding="utf-8")
body = js.split("function _applySafetyLimitNotes()")[1].split("\nfunction ")[0]
check("the note reads the server's limits rather than deriving its own",
      all(k in body for k in ("safety_limit_gb", "cap_floor_gb", "safety_pct"))
      and "/ 100" not in body and "* 0.15" not in body, body[:200])
check("...and is refreshed by the status poll, not only at first paint",
      js.count("_applySafetyLimitNotes()") >= 3)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
