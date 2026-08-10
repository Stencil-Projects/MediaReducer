"""A Configuration-page save must not undo a Filtering & Scoring save.

The two pages write the same file. The Configuration form posts none of the
scoring keys, so api_save_config rebuilds config.json from its own payload and
carries the scoring page's keys forward from disk by NAME, from a list inside
_carry_forward_omitted_fields. A scoring knob missing from that list is not an
error anywhere — the next Configuration save just quietly resets it to its
default. That is how the four TV knobs (eligibility, episode cap, watch
weight, series bump) were silently reverting: set the season cap to off, save
an unrelated connection setting, and the cap is back to 50.

The required set is DERIVED, not spelled out twice: every key
_score_page_config serves to the scoring page is a key that page can save, so
every one of them must survive a Configuration save. Adding a knob to the page
without adding it to the carry list fails here, before it eats a setting.
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


# The keys the scoring page owns, derived from what it is served. Sentinel
# values so a carried key is distinguishable from a default that happens to
# match.
#
# USE_JELLYFIN is served but not owned: it is the Configuration form's own
# checkbox (that form posts it on every save, so it needs no carrying) and
# api_save_score_config never writes it — the scoring page only reads it to
# know whether the favorites toggle applies.
owned = [k for k in A._score_page_config({}) if k != "USE_JELLYFIN"]
saved = {k: f"SENTINEL::{k}" for k in owned}

cfg: dict = {}
A._carry_forward_omitted_fields(cfg, saved)

for k in owned:
    check(f"{k} survives a Configuration save", cfg.get(k) == saved[k],
          cfg.get(k, "<dropped>"))

# A value the Configuration form DID post is the fresher one and must win.
cfg2 = {"GRACE_PERIOD_DAYS": 123}
A._carry_forward_omitted_fields(cfg2, saved)
check("a posted value is not overwritten by the carried one",
      cfg2["GRACE_PERIOD_DAYS"] == 123, cfg2["GRACE_PERIOD_DAYS"])

# The disabled-threshold memory this helper also owns: a threshold that is off
# keeps its last entered value so the grayed-out field still shows it.
cfg3 = {"REDLINE_GB": None}
A._carry_forward_omitted_fields(cfg3, {"REDLINE_GB": 75})
check("a disabled threshold remembers its last value",
      cfg3.get("_REDLINE_GB_LAST") == 75, cfg3.get("_REDLINE_GB_LAST"))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
