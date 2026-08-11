"""The gate and the actor judge a config value the same way.

The app REFUSES a bad numeric setting — the save fails, a hand-edited file is
flagged and the run buttons lock. The engine COERCES to something usable and
records an error that aborts a simulation or a cleanup. Different jobs, so
different code; but they must never disagree about WHICH values are bad,
because the app is the gate and the engine is what actually deletes. A value
the Configuration page refuses to store must not be a value a CLI run happily
deletes on.

They did disagree, on five keys, for as long as each spelled its own bounds
out at its own call site:

  HEADROOM_GB null            app: locked out    engine: silently 0 (redline-only!)
  REDLINE_GB 0                app: refused       engine: armed a zero floor
  GRACE_PERIOD_DAYS 2.5       app: refused       engine: used 2.5
  DELETE_DELAY_DAYS 0 / 2.7   app: refused       engine: clamped to 1 / truncated to 2
  IMDB_RATINGS_MAX_AGE_DAYS 0 app: refused       engine: accepted

This drives BOTH sides over one matrix of values and requires the same
verdict for each. It is deliberately a behavioural comparison rather than an
assertion that both import the same table: sharing a table is how they agree
today, not the property worth protecting.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
_tmpout.config()
import shared                                      # noqa: E402
import app as A                                    # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def app_refuses(key, value) -> bool:
    """Does the app flag this value in a config file?"""
    return any(i["key"] == key for i in A._config_file_issues({key: value}))


def engine_refuses(key, value) -> bool:
    """Does the engine record a config error for this value? That error aborts
    simulation and live cleanup, which is the engine's form of refusal."""
    before = len(E.CONFIG_ERRORS)
    E._num(key, value, default=None)
    grew = len(E.CONFIG_ERRORS) > before
    del E.CONFIG_ERRORS[before:]
    return grew


# Values worth disagreeing about: the OFF spellings, the boundaries either side
# of every limit, fractions where whole numbers are required, and the junk a
# hand-edited file can carry.
VALUES = [None, "", "null", "none", 0, 1, 2, 2.5, 2.7, 0.5, 15, 25, 100, 101,
          120, 200, 365, 366, 999, 1000, -1, -0.5, 10, 10.5, True, False,
          "abc", "5", float("inf"), float("nan"), 1e999]

disagreements = []
for key in shared.NUMERIC_KEYS:
    for value in VALUES:
        a, e = app_refuses(key, value), engine_refuses(key, value)
        if a != e:
            disagreements.append(
                f"{key}={value!r}: app {'refuses' if a else 'accepts'}, "
                f"engine {'refuses' if e else 'accepts'}")

check(f"both sides agree on all {len(shared.NUMERIC_KEYS)} keys "
      f"x {len(VALUES)} values", not disagreements, disagreements[:6])

# The five that used to differ, named so a regression says which one broke
# rather than only that something did.
for key, value, why in (
        ("HEADROOM_GB", None, "null is not 'redline-only mode'"),
        ("REDLINE_GB", 0, "a zero floor is not a floor"),
        ("GRACE_PERIOD_DAYS", 2.5, "half a day of grace is not a setting"),
        ("DELETE_DELAY_DAYS", 0, "a mark is never deleted the same day"),
        ("DELETE_DELAY_DAYS", 2.7, "part of a day is not a delay"),
        ("IMDB_RATINGS_MAX_AGE_DAYS", 0, "a zero-day dataset age refetches forever")):
    check(f"{key}={value!r} is refused by both — {why}",
          app_refuses(key, value) and engine_refuses(key, value),
          (app_refuses(key, value), engine_refuses(key, value)))

# ...and the ordinary values still pass both, so the parity above is not two
# sides agreeing to refuse everything.
for key, value in (("HEADROOM_GB", 0), ("HEADROOM_GB", 500), ("REDLINE_GB", None),
                   ("REDLINE_GB", 200), ("MAX_LIBRARY_GB", None),
                   ("GRACE_PERIOD_DAYS", 30), ("DELETE_DELAY_DAYS", 1),
                   ("DELETE_DELAY_DAYS", 365), ("MAX_HEADROOM_PCT", 15),
                   ("MAX_IMDB_RATING", None), ("NEAR_TIE_PTS", 2),
                   ("SCORE_BALANCE", 0), ("TV_MAX_SEASON_EPISODES", 0),
                   ("MAX_STALENESS_MONTHS", 12)):
    check(f"{key}={value!r} is accepted by both",
          not app_refuses(key, value) and not engine_refuses(key, value),
          (app_refuses(key, value), engine_refuses(key, value)))

# A rejected value never comes back as a usable number: a caller that ignores
# the error must not be handed something to delete on.
for key, value in (("REDLINE_GB", 0), ("DELETE_DELAY_DAYS", 0), ("HEADROOM_GB", None)):
    v, err = shared.coerce_number(key, value)
    check(f"a refused {key}={value!r} yields no value", err and v is None, (v, err))

# The engine falls back to its stated default rather than to None, so a bad
# value cannot turn a threshold into "unset" behind the error.
check("a refused value falls back to the caller's default",
      E._num("DELETE_DELAY_DAYS", 0, default=7) == 7)
del E.CONFIG_ERRORS[:]

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
