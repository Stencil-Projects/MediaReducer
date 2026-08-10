"""How much a run frees, and what it calls itself.

Three space targets can be breached at once — Headroom, the Library Size Cap,
and the Redline floor — and every kind of run combines them differently. An
emergency restores the free-space floor and stops. A scheduled run works the
daily, delay-clocked deficit. Simulate and the manual Cleanup button take
whichever is larger. Get the combination wrong and nothing raises: the run
simply frees the wrong amount, which is either a breach left standing or a
deletion nobody needed.

None of it was covered. Fifteen mutations went through the whole scenario
sweep and ten survived — the entire trigger-label builder, the season-share
subtraction in both directions, and two of the three target arms. The reason
is structural: a scenario asserts WHETHER files went, not how many bytes the
run aimed at, and the label only ever appears in log prose. Two of the inputs
also cannot be posed from a fixture. `over_limit` compares against the real
filesystem, so no fixture can breach Headroom on a machine with free disk, and
Redline and the manual button never coincide in the scenario table.

Reachable here because the block is now its own function, called with its
inputs spelled out. The stubs are the four things it reads from outside:
the season share, redline-only mode, and the two log sinks.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
import engine as E                                 # noqa: E402
_tmpout.redirect_engine(E)

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


GB = 1_000_000_000


def target(*, used=60.0, max_gb=80.0, free=500.0, library=200.0,
           over_limit=None, redline_hit=False, library_cap_hit=False,
           immediate=None, sim=False, manual=False,
           redline_gb=50.0, max_library_gb=150.0, tv_share_gb=0.0,
           redline_only=False):
    """Put one run's state to the target calculator and hand back what it
    decided, the deficits it reported to the log header, and anything it said
    on the way. Defaults describe a run breaching nothing.

    over_limit defaults to the comparison main() makes, and immediate to
    redline_hit, which is the only thing that sets it — both are still
    overridable so the combinations a fixture cannot reach are still posable.
    A breach has to be asked for: the defaults sit under every limit, so a
    case that names one target is not quietly testing two.
    """
    logged = []
    ctx = {}
    saved = (E.REDLINE_GB, E.MAX_LIBRARY_GB, E._tv_share_bytes,
             E._redline_only_mode, E.log, E.log_run_context)
    E.REDLINE_GB = redline_gb
    E.MAX_LIBRARY_GB = max_library_gb
    E._tv_share_bytes = lambda: int(tv_share_gb * GB)
    E._redline_only_mode = lambda: redline_only
    E.log = lambda msg="", *a, **k: logged.append(str(msg))
    E.log_run_context = lambda **kw: ctx.update(kw)
    try:
        gb, byts, trigger, breach = E._deletion_target(
            used_gb=used, max_gb=max_gb, free_gb=free, total_gb=1000.0,
            library_gb=library,
            over_limit=used >= max_gb if over_limit is None else over_limit,
            redline_hit=redline_hit, library_cap_hit=library_cap_hit,
            immediate_trigger=redline_hit if immediate is None else immediate,
            _cap_active=True, _is_sim=sim, _manual_cleanup=manual)
    finally:
        (E.REDLINE_GB, E.MAX_LIBRARY_GB, E._tv_share_bytes,
         E._redline_only_mode, E.log, E.log_run_context) = saved
    return gb, byts, trigger, breach, ctx, logged


# ── Each target's own deficit ────────────────────────────────────────────────
# Only ever reported to the log header, which is where someone reads why a run
# picked the size it did.
_, _, _, _, ctx, _ = target(used=100.0, max_gb=80.0)
check("the headroom deficit is what usage exceeds the limit by",
      round(ctx["headroom_deficit_gb"], 1) == 20.0, ctx["headroom_deficit_gb"])
check("...and is never negative when usage is under it",
      target(used=60.0, max_gb=80.0)[4]["headroom_deficit_gb"] == 0.0)

# Redline is measured in FREE terms, not used: on a filesystem with a root
# reserve, used-based math can read 0 while free is genuinely under the floor.
_, _, _, _, ctx, _ = target(free=30.0, redline_gb=50.0, redline_hit=True)
check("the redline deficit is what free space is short of the floor",
      round(ctx["redline_deficit_gb"], 1) == 20.0, ctx["redline_deficit_gb"])
check("...and is 0 when the floor is not breached",
      target(free=500.0, redline_gb=50.0, redline_hit=False)[4]["redline_deficit_gb"] == 0.0)

_, _, _, _, ctx, _ = target(library=200.0, max_library_gb=150.0, library_cap_hit=True)
check("the cap deficit is what the library exceeds the cap by",
      round(ctx["library_deficit_gb"], 1) == 50.0, ctx["library_deficit_gb"])
check("...and is 0 when the cap is not breached",
      target(library_cap_hit=False)[4]["library_deficit_gb"] == 0.0)

# ── The daily target takes the LARGER of the two daily breaches ──────────────
# They are separate limits on the same library, so satisfying the smaller one
# leaves the other still breached.
gb, *_ = target(used=100.0, max_gb=80.0, library=200.0, max_library_gb=190.0,
                library_cap_hit=True)
check("the daily target covers the bigger of headroom and cap", round(gb, 1) == 20.0, gb)
gb, *_ = target(used=100.0, max_gb=95.0, library=200.0, max_library_gb=150.0,
                library_cap_hit=True)
check("...whichever of the two that is", round(gb, 1) == 50.0, gb)

# ── The season side's share of the daily deficit ─────────────────────────────
# One pool, one deficit: whatever the season executor has claimed, the movie
# side must not free again.
gb, _, _, _, _, logged = target(used=100.0, max_gb=80.0, tv_share_gb=8.0)
check("a season share is taken off the movie target", round(gb, 1) == 12.0, gb)
check("...and the run says so", any("Season deletions cover" in x for x in logged), logged)
gb, *_ = target(used=100.0, max_gb=80.0, tv_share_gb=50.0)
check("a share larger than the deficit floors the target at 0", gb == 0.0, gb)

# A manual Cleanup is the exception, and it is not a rounding detail: the
# seasons behind the share are only MARKED, so they sit out the deletion delay.
# Subtracting them would report the button done with the breach still standing
# for another month.
gb, _, _, _, _, logged = target(used=100.0, max_gb=80.0, tv_share_gb=8.0, manual=True)
check("a manual Cleanup covers the whole deficit itself", round(gb, 1) == 20.0, gb)
check("...and does not claim the seasons covered any of it",
      not any("Season deletions cover" in x for x in logged), logged)

check("no season share leaves the target alone",
      round(target(used=100.0, max_gb=80.0, tv_share_gb=0.0)[0], 1) == 20.0)

# ── Which deficit each kind of run actually targets ──────────────────────────
# Simulate and the manual button mean "everything that is over", so they take
# whichever breach is larger. Neither combination below is reachable from a
# scenario: the manual button and a redline breach never coincide there.
gb, *_ = target(used=100.0, max_gb=80.0, free=10.0, redline_gb=50.0,
                redline_hit=True, manual=True)
check("a manual run targets the larger of daily and redline", round(gb, 1) == 40.0, gb)
gb, *_ = target(used=100.0, max_gb=85.0, free=10.0, redline_gb=50.0,
                redline_hit=True, sim=True)
check("...and so does Simulate", round(gb, 1) == 40.0, gb)
gb, *_ = target(used=100.0, max_gb=40.0, free=45.0, redline_gb=50.0,
                redline_hit=True, sim=True)
check("...taking the daily deficit when that is the larger one", round(gb, 1) == 60.0, gb)

# An emergency restores the floor and NOTHING else. Topping up to the headroom
# target here would delete past what the emergency needed, on files that have
# not served their deletion delay.
gb, *_ = target(used=100.0, max_gb=40.0, free=45.0, redline_gb=50.0, redline_hit=True)
check("an emergency targets the redline deficit alone", round(gb, 1) == 5.0, gb)

# And a scheduled run does not borrow the emergency's number.
gb, *_ = target(used=100.0, max_gb=80.0, free=45.0, redline_gb=50.0,
                redline_hit=False, immediate=False)
check("a scheduled run targets the daily deficit alone", round(gb, 1) == 20.0, gb)

# ── GB to bytes ──────────────────────────────────────────────────────────────
gb, byts, *_ = target(used=100.0, max_gb=80.0)
check("the byte target is the decimal-GB conversion, matching bytes_to_gb",
      byts == int(gb * GB) == 20_000_000_000, (byts, gb))

# ── The trigger label ────────────────────────────────────────────────────────
# It heads the run log and the summary. A label naming the wrong target sends
# someone to the wrong setting to explain a deletion.
check("an emergency is labelled REDLINE",
      target(redline_hit=True, free=10.0)[2] == "REDLINE",
      target(redline_hit=True, free=10.0)[2])
check("a breached cap is labelled LIBRARY CAP",
      target(library_cap_hit=True)[2] == "LIBRARY CAP",
      target(library_cap_hit=True)[2])

# The headroom breach is the one no fixture can pose: over_limit is measured
# against the real filesystem.
check("a scheduled headroom breach is labelled scheduled daily",
      target(over_limit=True)[2] == "scheduled daily", target(over_limit=True)[2])
check("the same breach under the manual button is labelled HEADROOM",
      target(over_limit=True, manual=True)[2] == "HEADROOM",
      target(over_limit=True, manual=True)[2])

# Two breaches, both named, headroom first — the order the log reads them in.
t = target(over_limit=True, library_cap_hit=True)[2]
check("two breaches are both named, headroom first", t == "scheduled daily + LIBRARY CAP", t)
t = target(over_limit=True, library_cap_hit=True, redline_hit=True, free=10.0,
           manual=True)[2]
check("all three under the manual button", t == "HEADROOM + REDLINE + LIBRARY CAP", t)

# The cap is a DAILY target, so an emergency is not working on its behalf and
# must not claim it. The reverse holds for redline on a scheduled run.
t = target(library_cap_hit=True, redline_hit=True, free=10.0)[2]
check("an emergency does not claim the daily cap as its reason", t == "REDLINE", t)
t = target(over_limit=True, redline_hit=True, free=10.0, immediate=False)[2]
check("a scheduled run does not claim a redline it is not acting on",
      t == "scheduled daily", t)

# Redline-only mode: nothing is breached and that is the normal state, because
# the run's whole job is the standing preview of what Redline will take first.
check("a redline-only Simulate with nothing breached says so",
      target(sim=True, redline_only=True)[2] == "REDLINE ORDER PREVIEW",
      target(sim=True, redline_only=True)[2])
check("...but a live redline-only run is not a preview",
      target(redline_only=True)[2] == "scheduled daily", target(redline_only=True)[2])
check("an ordinary run with nothing breached falls back to scheduled daily",
      target()[2] == "scheduled daily", target()[2])

# ── Is a daily-scheduled target breached at all ──────────────────────────────
# This is what the run gate reads to decide whether a scheduled run proceeds.
# Dropping either half means a run declines with a limit standing.
check("a headroom breach is a daily breach", target(over_limit=True)[3] is True)
check("a cap breach is a daily breach too", target(library_cap_hit=True)[3] is True)
check("a redline breach on its own is NOT a daily breach",
      target(redline_hit=True, free=10.0)[3] is False)
check("nothing breached is no daily breach", target()[3] is False)

# ── The log header gets the state it reports ─────────────────────────────────
gb, _, trig, _, ctx, _ = target(used=100.0, max_gb=80.0, library_cap_hit=True)
check("the header is handed the same target the run uses",
      ctx["to_free_gb"] == gb and ctx["trigger"] == trig, (ctx["to_free_gb"], gb))
check("...and every breach flag it prints",
      ctx["over_limit"] is True and ctx["library_cap_hit"] is True
      and ctx["redline_hit"] is False, ctx)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
