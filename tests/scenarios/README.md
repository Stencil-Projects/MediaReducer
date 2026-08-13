# Scenario suite

```bash
python3 tests/scenarios/run.py                      # all of them
python3 tests/scenarios/run.py /tmp/out baseline,tv-off
```

Runs as part of `tests/run_tests.sh --e2e`, so every push exercises it.

Each scenario builds a whole world — movies and multi-season shows on disk, a
mock Jellyfin serving exactly what the disk holds, a config — then drives
Simulate and Cleanup against it and checks what happened.

## Manual runs and scheduled runs are different runs

A cleanup fired through `/api/run` is a **manual** one, and manual ignores the
deletion delay on purpose: you pressed the button, you meant now. Every
scenario here worked that way at first, which left the mark-and-wait path
untouched — across 29 scenarios and 51 runs the engine never once logged
`MARKED for deletion`, so the mechanism the whole safety story rests on had no
coverage. Disabling the delay outright changed nothing anywhere in the suite.

A scheduled run is the engine started without `MEDIAREDUCER_MANUAL`, so
`expect: {"scheduled": True}` runs `engine.py` directly instead of going
through the API, after clearing the daily-window stamp the app sets at startup.
It replaces the Simulate/Cleanup phases rather than preceding them — leaving
them in would delete everything the scheduled run correctly held back.

| expect key | what it adds |
| --- | --- |
| `scheduled` | run the engine directly, as the scheduler would |
| `marks` | require the run to log `MARKED for deletion` — "deleted nothing" is also what a refused run looks like |
| `marks_due` | then age every mark past its delay and run again, which must collect them |
| `redline_x_free` (spec) | arm `REDLINE_GB` at a multiple of the free space that actually exists, so the trigger fires on any machine |
| `run_time_ahead` (spec) | put `DAILY_RUN_TIME` ~90 minutes out, so a scheduled run arrives early |
| `waits` | require the run to refuse with "waiting for today's scheduled run time" |
| `day_used` | leave the daily-window stamp in place, for the branch that is about it |
| `log_has` | require an exact phrase in the run log — several refusals delete nothing and are otherwise identical |

## Diffing whole runs

`MR_SCENARIO_KEEP=1` keeps each scenario's directory on a green run.
`lastrun.log` is the engine's own transcript of what it decided, so normalizing
those across every scenario gives a behavioural fingerprint of a run — useful
before reshaping anything in `main()`. Scrub the timestamps, the run directory,
elapsed times, and the used/free/total figures, which come from the real host
filesystem rather than the fixture.

Two scenarios stay out of it entirely: `redline-deletes-immediately` arms its
floor from the free space that exists, and `scheduled-before-run-time` sets a
run time from the clock, so every number they log moves on its own. Both are
guarded by their own pass/fail expectations instead, which is what those are
for.

**Fixed, not random.** Every scenario here is a literal dict rather than
something drawn from a seeded RNG: a merge cannot be gated on runs that cover
different ground each time and a failure that may not reproduce. CI tests the
same ground every time and a red build names the case. The shape is one baseline
that deletes both a movie and a season, then one variant per factor — each
changing a single thing, so a failure points at that thing.

Each scenario states what it `expect`s: whether movies and episodes may lose
files, and whether the app should refuse the run outright. Without that, a
scenario that silently does nothing passes, and "it did not crash" gets
mistaken for "it did the right thing".

## Checked after every run

Nothing left the library root or a monitored path; a Simulate deleted nothing;
a disabled media type lost nothing; no protected favorite went; everything
removed is in `deleted.log`; no empty season directory was stranded; no page or
endpoint 5xx'd afterwards; no traceback in the app log.

## Three things that will bite you editing this

**Sparse files.** The library measure is `st_blocks * 512`, falling back to
`st_size` when blocks is zero, so a wholly sparse file counts at its apparent
size. That is how a scenario holds a 40 GB library on a runner with a few GB
free. Without it every library rounds to 0.0 GB, no threshold is breached, and
the deletion path is never reached.

**A 200 from `/api/run` does not mean a run started.** The app answers
`200 {"started": false}` for a no-op — "Space limits are already satisfied".
Reading only the status made every such scenario look like the product had
quietly done nothing, and the next phase then failed for want of a plan the run
never wrote.

**Ready is not "the port answers".** The startup storage refresh has to finish
first, or the run is refused with "a background status refresh is finishing".

Each of those three, plus a mock too thin to answer the season watch query,
produced a full green sweep that had tested nothing. Hence the coverage line in
the summary: it says how many scenarios deleted and how many files went. Treat
a run reporting no deletions as a broken harness, not a clean bill of health.
