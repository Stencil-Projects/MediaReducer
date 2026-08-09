# Scenario suite

```bash
python3 tests/scenarios/run.py                      # all of them
python3 tests/scenarios/run.py /tmp/out baseline,tv-off
```

Runs as part of `tests/run_tests.sh --e2e`, so every push exercises it.

Each scenario builds a whole world — movies and multi-season shows on disk, a
mock Jellyfin serving exactly what the disk holds, a config — then drives
Simulate and Cleanup against it and checks what happened.

**Fixed, not random.** An earlier version drew its libraries and settings from
a seeded RNG. That is the wrong thing to gate a merge on: two runs of the same
commit cover different ground, and a failure may not reproduce. Every scenario
here is a literal dict, so CI tests the same ground every time and a red build
names the case. The shape is one baseline that deletes both a movie and a
season, then one variant per factor — each changing a single thing, so a
failure points at that thing.

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
