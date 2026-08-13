# Architecture

A contributor's map of how MediaReducer fits together. For install/usage see
[README.md](README.md); this doc is about the code.

## The big picture

MediaReducer is two Python processes plus a set of Jinja templates:

```
 browser ──HTTP──▶  app.py  (Flask web server, launched by tini/entrypoint)
                      │
                      ├─ renders templates/  (dashboard, config, explorer)
                      ├─ reads/writes  /config/config.json
                      ├─ season side (runs in-process, BEFORE each engine run):
                      │    Jellyfin/Plex season inventory + watch facts;
                      │    marks/deletes whole seasons on /library
                      ├─ APScheduler tick every 15 min ──┐
                      └─ subprocess.Popen ───────────────┴──▶  engine.py
                                                                 │ scans Plex/Tautulli,
                                                                 │ Jellyfin, Radarr;
                                                                 │ scores; marks/deletes
                                                                 ▼
                                                          movie files on /library
```

Movies and TV are **one pool** with two executors: the engine deletes movies,
the app-side season handling deletes whole seasons, and they split a single
space deficit (see **TV: the season side** under Key models).

**Run lock.** While a run is active, every Configuration section and the
Filtering & Scoring card is ghosted. It is a sweep (`prSetRunLock` in
`base.html`) rather than a hand-picked list of controls, so a control added
later is covered without anyone remembering to add it.

The ordering in `_applyConfigRuntimeLocks` is the part to preserve: release the
lock, let the per-section rules set each control's real state, then lay the lock
over the top. `prSetRunLock` only re-enables what it disabled
(`data-run-locked`), because a control switched off for its own reason (a
disconnected server, an unticked dependent toggle) must stay off afterwards.

`[data-run-lock-exempt]` marks the one subtree the sweep skips: **Force stop**,
which has to work during exactly the state that locks everything else. Staying
*readable* takes more than the exemption, since the ghost dims via CSS opacity,
which multiplies down and cannot be undone by a descendant. So `prSetRunLock`
marks every ancestor of an exempt box `.run-lock-passthrough` and the ghost rule
skips those wrappers, dimming their other children instead. No depth assumption:
an exempt box can live wherever the layout wants it.

Each page reports the run once, above its settings, rather than in every
category. `_sectionNoticeText()` filters the run reason out of the per-section
notices while leaving their own reasons intact — repeated in every section, one
sentence buries the reasons that actually differ.

`force_stop_script` SIGKILLs the engine, waits for it to actually die, and
reports what happened. It never force-clears `_run_active` behind a process it
failed to kill: one wedged in an uninterruptible syscall can wake later, and
unlocking would let a second run start beside it.

**Run issues.** Trouble has to reach the log, the progress message and the
notification. Worded separately that is three descriptions of one event and
three chances to disagree.

`engine.record_issue(category, detail)` is the only way to raise one. It
appends to the run's issue list and writes the log line in a single call, from
`run_issues.CATEGORIES`, so `lastrun.log` uses the words the dashboard and the
notification use. `emit_progress` and `write_run_report` attach the folded list
themselves, and `completed_with_errors` is derived rather than passed by each
caller, so an outcome flag cannot disagree with its own issue list.

`test_run_issues` keeps it from rotting. It reads engine.py for bare
`log("WARNING…")` lines that would never reach the dashboard, and flags any
declared category nothing raises.

- **`app.py`** — the Flask server. Serves the three pages, exposes the JSON API
  the UI polls, launches `engine.py` as a subprocess for every run, runs the
  scheduler that fires automatic Cleanup deletions, gates cleanup behind
  plan-currency, and builds the sanitized debug report. Run state
  (`_run_active`, `_run_process`, …) is in-memory and resets on restart.
- **`engine.py`** — the deletion engine. A standalone script: it loads the same
  `config.json`, fetches the library from the media APIs, scores every movie,
  and either simulates or performs deletions to satisfy the space limits. It
  never imports `app.py`; they communicate through `config.json`, the state
  files below, and the subprocess exit code.
- **`db.py`** — the SQLite store both processes share (schema, transactions,
  WAL setup). Four tables; see **State files** below.
- **`notify.py`** — outbound alerting, an `apprise` wrapper. Only `app.py`
  calls it, and only after a run finishes: the engine just writes
  `last_run_report.json`, so a dead webhook can never stall a scan or a delete.
  It never raises, never blocks its caller, and never sends a destination more
  than one message per 10 seconds (see **Notification rate limit** below).
- **`run_issues.py`** — the warning/failure vocabulary: every category a run can
  report, with its label, severity, fix note, and whether it counts (43 movies
  skipped) or simply states itself (the IMDb download failed). The engine, the
  app, the notifier and the dashboard all render from this one table.
- **`scoring_constants.py`** — every scoring curve number, in one place, read by
  both the engine and the Filtering & Scoring page so they cannot drift. The
  movie and season retention curves also live here, assembled from one set of
  term helpers, so the two scales stay one scale.
- **`shared.py`** — the run mechanics BOTH executors import: the pool deficit
  arithmetic, the delay clock (mark date → deletable date), the deleted.log
  line builder, and the eligibility rung decisions the movie and TV ladders
  share (IMDb rules, the grace period, the unplayed skip). Each ladder keeps
  its own rung order and its own per-type rungs; what must mean the same thing
  on both sides exists once here.
- **`cli.py`** — `mediareducer` / `mr` inside the container. A thin HTTP client
  for the same API the UI uses, so there is no second code path to keep in sync.
- **`templates/`** — `base.html` (shared layout and the CSS design system) plus
  `dashboard.html`, `config.html`, `deletion_score_explorer.html`. Each carries
  a short inline preamble of the values its render decided, and nothing else:
  the code is in `static/js/`.
- **`static/js/`** — the browser code, as files a parser can read.
  `form-fields.js` and `base.js` load on every page; `config.js`,
  `dashboard.js` and `explorer.js` load on one each. They are classic scripts,
  not modules — no bundler, no build step — so every top-level name lands in
  one shared scope, which is how a page assembled from four files works.
  `eslint.config.mjs` derives that scope per page rather than listing it, so
  `no-undef` catches a name the preamble stopped rendering; `test_js_lint`
  runs it, and refuses a function declaration back inside a template. Loaded
  `defer` and stamped with a content hash by `asset_url()`, which is what lets
  them be cached `immutable` the way the vendored files are.
  The disk bar appears on two tabs, so its CSS (`.disk-bar*`) lives in
  `base.html` and its renderer (`prRenderDiskBar`) in `form-fields.js`. The
  renderer scopes every lookup to the root element it is handed and finds parts
  by class, so each page keeps its own ids. Thresholds are arguments rather than
  globals: the Dashboard draws what is saved, Configuration draws what the
  pending form would save.
- **A status pill wears the color of the button that does the thing.** The
  header badge, the run pill and Last Run's mode pill each borrow a button's
  rest colors: red is Cleanup (a run that deletes, or the mode that will),
  blue is the primary action, yellow is Debug Cleanup, neutral is Simulate.
  `.pr-pill--state` plus one tone class is the whole vocabulary. Pills are flat
  where buttons are glassy, and hover moves the border only, since a status that
  fills in under the pointer reads as clickable — except the header badge, which
  is a link only so the run is one click away and holds still under the pointer.
  `window.prRunTone()` picks the label and tone from one place so the pills
  cannot word the same run differently. What the label
  answers is whether a run is deleting: "Cleaning" red, "Debugging" yellow,
  "Running" blue for everything else, however that run started. Pinned by
  `e2e_status_pills`.
- **Sizes are formatted by where the number came from, not where it is shown.**
  Anything *measured* (free space, library size, a movie, an amount reclaimed)
  carries one decimal even when it is `.0`, so one reading never appears as
  `400` on one screen and `400.0` on another. Anything the user *typed* is whole
  GB, quoted as entered. Use `measured_gb` / `prMeasuredGb` for measurements and
  `commafy(0)` / `prCommaNum(v, 0)` for settings; the latter pair drops trailing
  zeros, which is why measurements avoid them. `notify.py` has its own copy of
  the byte formatter (it cannot import `app`), pinned against it by
  `test_size_format`, since two implementations can drift to different units.
- **`static/`** — the favicon set: an SVG, a multi-size `.ico` and the iOS touch
  icon, all three generated from one set of constants so the vector and the
  rasters cannot drift. `/favicon.ico` has its own route
  because browsers request it at the site root before any markup is parsed.
- **`static/vendor/`** — Bootstrap and the Inter webfont. Served from the
  container, never a CDN: both are render-blocking, and this app is normally
  deployed on a LAN with no outbound internet, where a CDN fetch blocks the
  page until it times out. Version-pinned in their filenames and sent
  `Cache-Control: immutable`. The app gzips them, and every page and API
  response, on the way out — Flask serves this app directly, so there is no
  proxy to do it.
- **`entrypoint.py`** — container entry: optional PUID/PGID drop, then
  `os.execvp` into `app.py` (so the app is the signalled process; `init: true`
  in compose runs tini as PID 1 to reap the engine and forward SIGTERM).

## Scheduler Mode: the names do not match the labels

The single most confusing thing in this codebase. `RUN_MODE` holds one of three
values, and **`"paused"` is not the GUI's "Paused"**:

| `RUN_MODE` | GUI label | Scheduler behavior |
| --- | --- | --- |
| `"off"` | **Paused** | Quiet storage refresh only. No scans, no deletions, no alerts. |
| `"paused"` | **Monitor Only** | Full cadence — 15-minute upkeep, daily Simulate — but never deletes on its own, and sends no alerts unless `NOTIFY_IN_MONITOR_ONLY` is on. |
| `"headroom"` | **Automatic Cleanup** | The above, plus it deletes at the daily run and on a Redline breach. |

So `RUN_MODE == "paused"` means the scheduler is *fully active and merely
non-deleting*. Use `_is_cleanup_mode()` ("may this delete?") and `_ui_run_mode()`
("which of the three?") instead of comparing strings. Comments throughout use
the GUI's words, so they can be matched against what a user sees.

`"headroom"` is overloaded: it is both this scheduler mode and the engine run
mode in the table below. They coincide because the automatic run *is* a headroom
run, but a `MEDIAREDUCER_MODE_OVERRIDE` of `headroom` is a manual Cleanup too.

**The lifecycle.** Automatic Cleanup is never simply "whatever was saved":

- **Startup** drops Automatic Cleanup to Monitor Only
  (`force_paused_run_mode_on_startup`); a Paused scheduler stays paused. The one
  way out is `PAUSE_CLEANUP_ON_STARTUP` ("Set to Monitor Only at startup", on by
  default) turned off, and even then only with `_library_db_fresh` — a stale
  plan demotes regardless, since resuming deletions against something nothing
  has re-checked is exactly what the demote is for. Absence of the key reads as
  ON in `_coerce_bool`, so an older config file cannot be mistaken for someone
  having turned the safety off.
- **Monitor Only requires a working set** — a connected server, a monitored
  path, an armed space trigger (a Headroom target, a Redline floor, or a
  Library Size Cap — `_any_space_trigger_armed`; with none there is nothing to
  monitor), and a library database that is both present and under the 48-hour
  scan window (`_library_db_fresh`). Lose any one and the mode drops to Paused.
  A save that removes a CONFIGURED piece (deselects the last server, disarms
  the last trigger) rests at Paused even when a forced pause fired in the same
  save; a probe blip or a stale scan parks at Monitor Only / waits instead,
  because those heal on their own.
- **A completed Simulate wakes a resting Paused** back into Monitor Only. Two
  paths reach this: `_apply_setup_mode_lifecycle` (the last missing piece
  arrives via a config save) and `_sync_scheduler_mode_with_db` (a scan finishes
  outside any save — called after every run and Summary, and at the top of each
  tick, because a store wipe can happen mid-session).
- **A pause the user CHOSE never wakes**, and that is the one thing recorded:
  `_RUN_MODE_USER_PAUSED`, set only by a save that moves Monitor Only or
  Automatic Cleanup to Paused. Storing the deliberate case rather than the
  resting one matters: its ABSENCE is what a fresh install, a config reset and a
  hand-edited file all have in common, so absence has to mean wakeable. Recording
  the resting state instead would strand all three at Paused forever, since a
  reset rewrites the shipped defaults while the app keeps running and no startup
  code follows it. The system's own demotes (startup, a stale database, a forced
  pause) clear the flag, since those must always wake.

## Run modes

The engine's behavior is chosen by `RUN_MODE` (from config) or, more often, the
`MEDIAREDUCER_MODE_OVERRIDE` env var the app sets per launch:

| Mode | Trigger | What it does |
| --- | --- | --- |
| `debug_info` | Summary refresh (dashboard/scheduler upkeep) | Status + library-size vs. limits, then exits. No scan, no delete. Quiet (log discarded, no progress events). |
| `debug_sim` | **Simulate** button | Full dry run: scans, scores, logs the ranked candidate list and what *would* be deleted. Writes the marked-for-deletion plan. |
| `headroom` | **Cleanup** button / scheduler tick | Enforces `HEADROOM_GB`/`REDLINE_GB`/`MAX_LIBRARY_GB` and actually deletes — both the manual Cleanup and the automatic (scheduler) run. |
| `debug_cleanup` | **Debug Cleanup** button (Debug mode) | "Cleanup minus deletion": runs the marked-queue upkeep from cache and PERSISTS it (drops gone/protected marks, refreshes plays/scores in the queue + snapshot) exactly like a cleanup tick, then only PREVIEWS what it would delete. Never unlinks a file or trims the queue for deletions. |
| `reconcile` | Config save (`_reconcile_after_save`) | Quietly rebuilds the deletion plan from the stored library snapshot under the just-saved settings — no library walk, no deletions. Re-stamps the plan so Cleanup stays armed through a settings change. |

`MEDIAREDUCER_MANUAL=1` marks a manual Cleanup (deletes immediately, no delay,
no daily-window gate — the user has just seen the plan).

## A run, end to end

1. UI POSTs `/api/run` (or the scheduler tick calls `run_script()`).
2. The app checks connection health + plan-currency, then a daemon worker thread takes over.
3. The worker first runs the **season side** in-process (when TV cleanup is armed): fail-closed fetch, season marks reconciled, due marks deleted on a real Cleanup, and the season share of the pool deficit stamped for the engine. Its failure never blocks the movie run.
4. Then `subprocess.Popen(["python3", "engine.py"])` with the mode in the environment; the worker `wait()`s on it.
5. The engine writes `progress.json` as it goes; the dashboard polls `/api/run/progress` and tails `lastrun.log` via `/api/logs/last`.
6. On exit, the app marks progress terminal (done/stopped/error); the engine archives the run's log under `logs/` (every Simulate/Cleanup/Debug Cleanup — quiet Summary refreshes are skipped). The run notification folds the season outcomes into the run's own numbers.

Only one run at a time — `_run_lock` + `_run_active` reject overlaps. **Stop**
(and a container SIGTERM, forwarded by `_graceful_shutdown`) sends SIGTERM to the
engine, which finishes the file it's on (unlink → `deleted.log`) before exiting.

**Stages: the log and the stepper are one call.** The dashboard's five-step
progress bar and the `====== TITLE ======` banners in `lastrun.log` drift if
written independently — the protected-collections query moves the step to
Scoring while the log is still inside RUN CONTEXT, so a failure there points at
a step the run has not reached. `engine.log_stage(title, phase=…)` does both:
it closes the previous stage with `------ TITLE finished in Xs ------`,
writes the new banner, records the phase, and emits the progress event. Adding a
stage means one call, not two.

Three details are load-bearing. `timed=False` is for the summary banners, which
report work already finished — they close the stage before them and claim no
duration of their own, which is what keeps a stage's timing from printing *after*
the section it preceded. `_CURRENT_PHASE` is what `_abort_api_failure` stamps on a
failure, so an abort raised from a helper marks whichever stage was actually open
rather than one hardcoded at the call site — the log's `ABORT stage:` line and the
dashboard's "Failed during …" then carry the same words, which is what a user is
asked to quote — the panel carries both as one entry at the top of the run
panel's issue list, marked with a red × rather than the `!` of something the run
got past. The failure text is split in two for the same reason:
`_abort_api_failure(message, detail=…)` puts a plain sentence on the dashboard
and the movie title, internal ids and exception repr on an `ABORT detail:` log
line, so the panel says what happened and what to do while the log keeps
everything needed to diagnose it. And the work that is deliberately quiet (reading 2,800 movies is
not worth 2,800 lines) still reports through `timed_step()`, which logs one line
on success and stays silent on exception, since a "finished in" line under an
abort would read as if the work had completed. `_RP_STEP_INDEX` in `dashboard.html`
maps phase → step, and `_rpMaxIdx` is a high-water mark so a step never goes
backwards. Pinned by `test_log_stages` and the stepper case in `smoke_all`.

## Key models

**Scoring** — `compute_retention_score()` in `engine.py` (the module docstring
has the full formula). Higher score = keep. A balance dial splits weight between
watch/added history and IMDb quality; deletion order is score ascending, with
documented tiebreaks. The Score Explorer's JS mirrors this exactly — the
`tests/parity/` check fails if the two drift.

**Space thresholds** — `HEADROOM_GB` (0 = trigger off; the GUI requires >= 1
when ticked, so 0 is what unticking stores, never a typed value) and
`MAX_LIBRARY_GB` share the once-per-day window + `DAILY_RUN_TIME` and the
deletion delay; either alone is a valid setup (cap-only included), and so is
EVERY threshold off at once — the scheduler then rests at Paused, with Monitor
Only and Automatic Cleanup unavailable, which is how a fresh install ships.
`REDLINE_GB` fires immediately on any 15-minute tick and frees only back to its
own floor; under an ARMED headroom it must sit strictly below the target.
`REDLINE_ONLY_MODE` (the GUI's Headroom checkbox unticked) is DERIVED from the
value on every load and save — armed iff `HEADROOM_GB >= 1` — so the flag can
never contradict the value and no validator ever has to referee between them.
TRUE redline-only — a Redline floor with NO cap — makes Redline the only trigger,
retires the delay, and has Simulate maintain a standing queue of every eligible
movie in deletion order; a cap armed alongside instead keeps running on the daily
schedule with the delay (`_redline_only_mode()` returns False in that case).

With a current plan, a Redline breach takes a fast path: it deletes straight
down the marked queue, re-verifying monitored roots, protected collections and
Jellyfin favorites fresh, instead of a full rescan; a background Simulate then
rebuilds the preview. **File size optimization is honored in EVERY delete
path.** The fast path (redline emergency and manual Cleanup), the full scan
(daily and manual) and the Debug Cleanup preview all pick via the same
`_pop_next_deletion`, re-applied against the live remaining target: when what's
left to free lands inside a group of near-tied-score movies, the cheapest cover
goes (the smallest-scoring single file that covers, else the largest tied file),
so one big movie can spare several small near-ties even after a since-watched
spare shifts the target.

**Marked & eligible queue** — every full plan (Simulate or a daily/manual
Cleanup) writes the ENTIRE eligible list to the store's `queue` table in deletion
order. Only the prefix covering the current space targets is *marked*
(`marked_at` set — the deletion-delay clock); the rest is merely *eligible*
(`marked_at` null), visible order that starts a fresh clock only if it is ever
marked. A Simulate that finds nothing eligible still writes its stamped
(empty) plan — that is a real answer, not a missing one. Satisfied limits stop
the clocks but keep the queue, on both sides: the engine's 15-minute upkeep
(`_revalidate_pending_marks`) and a config save that changes a threshold into
satisfied territory (`_unschedule_pending_marks`, reported as
`pending_unscheduled` — and only a save that actually changed a threshold
touches the clocks, so a stale cached library size can never reset them).

**Summary maintenance (the 15-minute pipeline)** — the quiet Summary keeps the
whole cache accurate between daily full scans, and every cached-queue cleanup run
(a manual Cleanup, a Debug Cleanup) runs the SAME pipeline as its pre-check, then
acts on the result — so even though Headroom/Library-Cap only *delete* at their
daily trigger, the marked set, scores, and disk numbers stay current every 15
minutes. The pipeline, in order:

1. **Refresh filesystem capacity + library size** and persist them (`emit_stats`).
2. **Drop dead marks** — files that are gone, or that joined a protected collection.
   A file confirmed **physically gone** is also pruned from the library snapshot in
   the same write (`save_pending(..., snapshot_delete_paths=…)`), so a title deleted
   outside MediaReducer doesn't linger as a phantom `movies` row until the next full
   scan. The redline fast path and the full-scan cleanup's external-vanish branch
   prune the snapshot the same way — every no-rescan path that confirms a file is
   gone sheds its row. (Protected-since marks leave the file on disk, so their
   snapshot row stays.)
3. **Re-size the marked set to the CURRENT headroom/cap deficit**
   (`_daily_deficit_bytes`) from the cached queue, no full scan: mark the
   File-size-optimized covering set (the SAME `_pop_next_deletion` a real delete
   uses), scored on FRESH watch data. A movie a recent watch lifted out of the set
   is dropped and the next in line — re-checked fresh before it's trusted — takes
   its place; the set GROWS or SHRINKS with the deficit (a newly-marked movie starts
   its delay clock now, a dropped one loses its clock). Within limits (or
   redline-only, or no deficit) nothing is scheduled — the clocks stop but the queue
   stays as the standing eligible order.
4. The fresh watch data is **persisted in ONE atomic write**
   (`save_pending(..., snapshot_watch_updates=…)`): each re-checked movie's belt-max
   plays/last-played/favorite into the library snapshot and its refreshed score into
   the queue entry, so the queue and snapshot can never drift apart on a half-failure
   (the snapshot's `built_at` is preserved — a watch refresh, not a rescan).
5. The marked list **displays in deletion-delay order** (soonest first) for
   headroom/cap.

A **Debug Cleanup runs this exact pipeline and persists it** — "Cleanup minus
deletion": it refreshes the cache like a real run, then only PREVIEWS what it would
delete (deletes nothing, trims nothing). The daily full scan is the one path that
rebuilds the whole queue + snapshot from a fresh library scan instead (adding
newly-added movies); when it carries a still-marked movie forward it refreshes that
entry's score/title too, so the marked prefix never keeps a stale score against a
freshly-rewritten snapshot. Redline emergencies delete straight from the queue and
skip the pipeline (the last tick already maintained it).

**Radarr cleanup** — fires the moment the deleted file is the copy in Radarr's
own section, regardless of surviving duplicates elsewhere. A copy KNOWN to be
in a different section never triggers it; only rows with unknown section
identity fall back to matching Radarr's folder against the deleted one. The
Redline fast path skips Radarr cleanup entirely (its queue entries carry no
TMDB/section identity).

**Deletion delay** (`DELETE_DELAY_DAYS`) — a daily Cleanup deletes a mark only
once its clock has aged N calendar days. Marks never authorize a deletion on
their own; a deletion re-verifies eligibility fresh (the full scan, or the
Redline fast path's own protection re-fetch), so a stale mark can never delete
a protected movie. Redline emergencies and manual Cleanups bypass the delay.

**Plan currency** — cleanup (arming automatic mode or pressing the manual
Cleanup button) is locked whenever the saved plan can no longer be trusted:

- **Config changed what gets deleted → the queue reconciles in place, no Simulate.**
  A completed Simulate — or a config-save **reconcile** — stamps the
  deletion-affecting keys (`_PLAN_CONFIG_KEYS`, kept identical in both files) plus
  the monitored paths into the `pending` record. Saving a scoring, filter, or
  threshold change — or a protected-collection or Jellyfin-favorites change — kicks
  a background reconcile (the engine's quiet `reconcile` mode; `_reconcile_after_save`
  → `run_reconcile` → `reconcile_from_snapshot`): it re-scores every movie in the
  stored snapshot, re-applies the shared eligibility ladder (`_hard_filter_reason`,
  the same predicate `build_candidates` uses), re-marks the covering prefix to the
  current target (keeping each mark's delay clock), and re-stamps — so cleanup
  un-ghosts with **no manual Simulate** and no library walk. Pure scoring / filter /
  threshold changes recompute from the snapshot with no server call; a collections /
  favorites change re-fetches the live protected / favorite sets first (fail-closed —
  if that server is unreachable the reconcile is HELD and the Connections check flags
  it, retried automatically once the connection recovers or the server is disabled,
  `_retry_held_reconcile`). `simulate_required` (the stamp mismatch) is the
  transient state shown until the reconcile lands, or the fallback when there is no
  snapshot yet. Note the two key sets: `_PLAN_CONFIG_KEYS` (what the stamp tracks)
  excludes the protected-collection lists and Radarr on/off — those are honored from
  the standing cache (the 15-min upkeep re-fetches protection and drops newly-protected
  marks; every deletion re-verifies fresh; the queue carries the Radarr identity) —
  while `_RECONCILE_*_KEYS` (what triggers a reconcile) *includes* collections and
  favorites. **Monitored-path changes** still force a manual Simulate: the snapshot
  reflects only the old paths, so it can't be reconciled (compared via the stamped
  `monitor_dirs`). The config page mirrors the trigger set as `_PLAN_REBUILD_KEYS`,
  for one reason: while a save is going to rebuild the plan, the marked set standing
  in front of the user is the one being replaced, so the delay dialog may not quote
  a count from it, and the re-date it offers has to wait for the rebuilt queue —
  `/api/queue/reset-delays` refuses (409) while the reconcile owns it.
- **No full scan within the last two days.** A full library scan (a Simulate, or
  the automatic daily Cleanup, or the daily maintenance Simulate — all rebuild
  the whole snapshot) must have completed within
  `_FULL_SCAN_MAX_AGE_SECONDS` (48h) — checked live off the snapshot's `built_at`
  (`_full_scan_overdue()`), no persistent flag. A daily scan normally keeps this
  fresh with a full day of slack (so moving `DAILY_RUN_TIME` later never trips
  it); if scans stop for two days (e.g. the APIs are unreachable) the plan ages
  out and cleanup ghosts until a manual Simulate. A completed scan refreshes
  `built_at` and lifts the lock on its own. The once-a-day maintenance Simulate
  (`_daily_maintenance_scan_due` + `_maybe_run_daily_maintenance_sim`,
  connections permitting) runs after `DAILY_RUN_TIME` in Monitor Only, and in
  Automatic Cleanup whenever the daily slot passes with nothing to delete — the
  plan stays current in every mode, and the scheduled daily-summary
  notification always has a run to ride.

Arming automatic mode additionally requires proof a Simulate has run at all —
within satisfied limits the queue can be legitimately empty, so the library
snapshot (written by every completed scan) serves as that evidence
(`_simulate_evidence()`). The manual Cleanup button stays ghosted while every
limit is satisfied regardless. See `_pending_plan_current()` /
`_full_scan_overdue()` (app) / `write_plan_to_queue()` (engine).

**Fail-closed protection** — protected collections (Plex/Jellyfin), identity
mismatches between servers, and (when IMDb is in use) movies with no rating are
*skipped*, never deleted. Any API failure aborts a deleting run rather than
guessing.

**Notifications** — three alert types, each its own toggle, all gated behind the
master `NOTIFY_ENABLED` and at least one configured service. `notify.py` owns
every default in `TOGGLE_DEFAULTS`, and `notify.flag(cfg, key)` is the only
correct way to read one: a key absent from a saved config must resolve the same
way on the app's gating side as in the message builder.

- **Mode muting** (`_notifications_muted_by_mode`) is checked first by all three
  paths: Paused, and Monitor Only without `NOTIFY_IN_MONITOR_ONLY`, send nothing
  at all. A muted mode also leaves every notification BASELINE untouched, so
  unmuting reports the interval in one alert rather than swallowing it. The
  opt-in deliberately covers every alert, not just the run summary — gating only
  the summary would leave "a space check marked 34 more movies" arriving on the
  next tick after run notifications were switched off in Monitor Only.
- **Run summaries** ride a completed run's `last_run_report.json`. A manual
  Simulate and a Debug Cleanup are always silent (you are watching them on the
  dashboard).
- **Marked between runs** and **low space** ride the 15-minute tick, fire once
  per *change* rather than per tick, and follow their own toggles in any mode
  that is not muted.

**Notification rate limit** — one delivery per destination per
`_MIN_SEND_INTERVAL_S` (10 seconds), enforced in `notify._deliver`, the single
function every alert *and* the Config page's test button pass through, so no
caller can route around it. It is one floor rather than a per-service table on
purpose: every documented per-request limit on the supported services is
seconds-scale, so a table would list numbers this floor already sits under and
never decide anything.

What actually paces MediaReducer is upstream of the floor — the tick that
produces the between-run alerts runs every 15 minutes and each alert type is
latched to fire once per state change — so the floor is only there to catch the
pathological case. The user-facing wording is derived from the constant
(`_interval_words`), not written out: hardcoded minutes read correctly at 15
minutes and say "every 0 minutes" at 10 seconds.

Two design points matter when reading it:

- A blocked alert is **held, not dropped**, and everything held for a
  destination flushes as one merged message when the window opens. Two alert
  types *can* legitimately land on the same tick (low space while new marks
  appear), and dropping the second would lose exactly the message worth having.
  A held message carries **no note about the delay** — the alert's own content
  is what the reader needs, and a line about MediaReducer's internal bookkeeping
  would make a merged message read as an apology for arriving. The test button is the
  one exception to holding — it refuses instead, so a user standing there
  checking their wiring is told immediately rather than later.
- The send stamps are **persisted** (hashed destination keys, so no webhook
  token is written), because an in-memory limiter forgets on restart and the
  next alert would go out inside a window that was still open. `app.py` injects
  the store via `notify.set_rate_state_store`, keeping `notify.py` free of any
  dependency on the app's state layer.

Every message states the mode it came from, because the same marks and the same
dates mean "these get deleted" in one mode and "these are merely eligible" in
the other. The suppression path is subtle and worth knowing: a run summary
suppressed by the Monitor Only opt-out deliberately does *not* re-baseline the
marked set, so those marks surface through the marked-changes alert instead of
vanishing unannounced.

**Connection health** — one health request per configured service plus a
media-path sample per media server: six round-trips with Plex, Jellyfin and
Radarr all set up. They share no state, so `_prefetch_connection_probes` issues
them together and a check waits for the slowest server rather than the sum.

A Config save reuses the cached probe unless the answer could have moved
(`_health_for_config_save`): a connection-relevant edit, a previous probe that
FAILED (recovery must be seen at once), or a cached answer older than one tick.
Saving a notification toggle cannot make a server unreachable, and the save is
not what guards anything — `/api/run` re-probes and refuses to start on a
failure, the tick re-probes before any automatic cleanup, and the engine fails
closed on any API error mid-run. The two media-path samples are
started speculatively alongside their server's health probe and discarded if it
fails; they are read-only GETs, and waiting for the probe first would put them in
a second round trip.

**TV: the season side and the one pool** — all app-side (`app.py`), the season
is the deletion unit, and Sonarr is optional and cleanup-only.

- *Inventory* (`_tv_inventory_rows`): Jellyfin and/or Plex supply every show's
  seasons — episode counts, on-disk sizes, per-season added dates (newest
  episode file), status, IMDb id — title-merged into one row carrying BOTH
  server identities. Stored as `media_type='tv'` rows in the library snapshot
  with the seasons as a JSON blob; each row's series folder is resolved under
  the monitored dirs BY NAME (`_resolve_tv_scope` — the server's own path is in
  its container namespace). Refreshed at run end, on config save (background
  thread), and fresh + strict (every configured source must answer) on the
  deletion path.
- *Scoring* (`season_retention_score` in `scoring_constants.py`, JS mirror
  `seasonScore`): the movie curve family at season grain. Plays convert to
  movie-watch equivalents (plays ÷ episodes × `TV_WATCH_WEIGHT`), the season's
  own users drive multi-user and decay, recency falls back last watch → season
  added → series added, `TV_SERIES_WATCH_BUMP` lifts every season by a log
  curve of the show's watched episodes, IMDb reads the series rating. History
  clamps at 100 so seasons never leave the movie scale.
- *Eligibility* (`_tv_season_plan`): whole-series shields (scope, protected
  collections, favorites under the shared toggle), the shared filters at
  season grain (grace on the season's own date, IMDb rules, skip-unplayed —
  the rung decisions come from `shared.py`, the same functions the engine's
  movie ladder calls), the latest season of any show not known ended, and the
  `TV_SEASON_ELIGIBILITY` mode (oldest-only default / except the most recently
  ADDED / all).
- *The one pool* (engine `_split_pool_with_seasons` ↔ app
  `_engine_takes_for_pass`): one deficit — `shared.pool_deficit_gb`,
  max(headroom overage, Library Size Cap overage; the cap measures every
  monitored dir, TV included). The SPLIT is computed by the engine from the
  run's own numbers on both sides: the season side stamps its eligible order
  (db meta `tv_season_order`, matched by run identity), the engine merges it
  with the fresh scan's scores into ONE worst-first order, and the covering
  prefix decides everything — the seasons inside it are stamped as `tv_takes`
  for the season side to execute, their byte share goes to `tv_share`, and
  the movie target becomes the remainder (`_movie_target_after_split`). Both
  halves of the merge come from one moment; the season side executes the
  takes on its NEXT pass, which costs no latency — marks wait out the
  deletion delay anyway, and plan-currency guarantees a full-scan run
  precedes any deleting one. Every gate fails toward the movie side covering
  everything: an order from another run reads as no seasons (a standalone
  engine run claims nothing it cannot free), and a stale stamp (>26h) reads
  as 0. Redline stays a movie-only emergency.
- *The season side* (`_run_tv_cleanup_pass`, fired by `run_script`'s worker before
  every engine launch): fail-closed strict fetch → the same safety-percentage
  refusal the movie side makes (a run-wide fact — it surfaces once, via the
  Space Thresholds module, never as a season-specific abort) → reconcile
  marks with the engine's stamped takes, intersected with TODAY's eligible
  plan and truncated to TODAY's deficit (a mark the takes stop covering is
  dropped, never deleted) → on a real Cleanup,
  delete due marks: optional Sonarr season-unmonitor FIRST (refusal skips the
  season), episode files freshly listed by the media server and joined to the
  resolved folder under the same escape/symlink guards as movies, one
  `deleted.log` line per file, empty season dirs tidied, Sonarr rescan →
  stamp the season order (minus anything just freed) for the engine that
  launches next. Marks live in db meta `tv_cleanup`, keyed by server id +
  season, each aging its own `DELETE_DELAY_DAYS` clock.
- *Surfaces*: season rows rank in the Filtering table's one deletion order,
  the marked & eligible window intersplices both types with a Type column and
  both count in the Dashboard's marked/eligible numbers, and the run
  notification folds season outcomes into the run's own lines — nothing
  user-facing calls the season side a separate pass.

## Caching: what you will trip over

Reads are cached at three levels, and a change that writes the store without
telling the right one produces stale numbers that look like a logic bug:

1. **`load_config()`** memoizes on `(mtime_ns, size)` of `config.json` and hands
   out a copy. Mutating what it returns is safe; expecting a re-read after an
   in-process write is not.
2. **Per-request store snapshot** (`g._pr_store_data`). One `/api/status` poll
   consults the store six times, and *validating* the fingerprint opens the
   file — so the first read in a request is reused by the rest, which also
   gives one coherent view instead of six independently-sampled ones. Any
   app-side write must drop it: use the `_store_write()` context manager, which
   does it for you. Background threads have no request context and skip this
   layer. **A subprocess that writes the store — a run, a Summary, a reconcile —
   is invisible to it**, so those paths invalidate explicitly.
3. **Cross-request memo** keyed on a data fingerprint, composed under a
   single-flight lock so a store change doesn't send every open tab off to
   rebuild the same dict at once.

The store is a **cache**: everything in it is reproducible by a Simulate. That is
what makes `_rebuild_store_if_damaged` safe — an unreadable store is wiped and
rebuilt empty rather than failing every run forever. It only wipes on *proven*
damage (a failed `quick_check`, or a `DatabaseError` that isn't an
`OperationalError`); merely busy or unopenable reads as fine, because destroying
a healthy store is far worse than one failed run.

## State files (all under `/config`, i.e. `OUTPUT_DIR`)

| File | Written by | Purpose |
| --- | --- | --- |
| `config.json` | app | Saved settings (single source of truth for both processes). |
| `mediareducer.db` | engine + app | SQLite store (`db.py`), four tables: `metadata_cache` (per-movie API facts, so a rescan skips the slow per-movie lookups), `movies` (the **library snapshot** every completed scan rewrites, and the Filtering & Scoring table — TV series ride here as `media_type='tv'` rows with their seasons as a JSON blob, each scan replacing only its own type), `queue` (the marked & eligible MOVIE deletion queue plus its plan-currency stamp) and `meta` (kv: **schedule state**, where the app burns and reopens the daily window, **storage stats**, the code/schema guards, plus the season marks + last report under `tv_cleanup`, the run's season order under `tv_season_order`, the engine's merge results under `tv_takes`, and the pool share under `tv_share`). |
| `lastrun.log` | engine | Most recent run log (overwritten each run; the engine archives it into `logs/` at run exit). |
| `logs/` | engine + app | Archived run logs — every Simulate, Cleanup, and Debug Cleanup (quiet Summary refreshes are skipped); Reset MediaReducer archives the final `lastrun.log` here too. |
| `deleted.log` | engine (app can truncate) | Deletion history (survives startup); the dashboard's Erase button empties it. |
| `progress.json` | engine (app resets) | Cleanup progress for the dashboard; carried across a restart with the preserved store (reset to "no runs yet" only by an explicit Clear-cache / Reset). |
| `last_run_report.json` | engine (app reads + clears) | Counts and movie lists from the last completed run — the source for the app's outbound notifications; cleared when a new run launches. |
| `title.ratings.tsv` | engine | IMDb ratings dataset (downloaded when needed). |

The file writes (`config.json`, `progress.json`, the logs) are atomic (temp file
+ `replace()`), so a crash or kill mid-write never leaves a torn file; the
`mediareducer.db` store gets the same guarantee from SQLite transactions (a
crash rolls back to the last commit).

**About the store.** WAL mode plus a `busy_timeout` serializes the engine
subprocess against Flask's request threads; WAL writes `-wal`/`-shm` sidecars
beside the `.db`. It survives a restart (`validate_store_on_startup`), so the
plan stays usable and whether it can still be trusted is decided live at the
gate (see **Plan currency**). Two guards keep it honest when the code or schema
changes: the engine's `code_checksum` (a hash of `engine.py`) flushes the
code-derived rows on the next write, and `db.py`'s `_schema_fingerprint`
rebuilds the tables at connect. Both keep `last_cleanup_date`.

## Development

Three tiers, each a superset of the one above it:

```bash
tests/run_tests.sh                # unit + parity — hermetic, no network, fast
tests/run_tests.sh --integration  # + the full run pipeline over real HTTP under
                                  # every server profile. Needs python + node.
tests/run_tests.sh --e2e          # + the browser page tests (playwright + chromium;
                                  # skips cleanly with a message if absent)
```

- **Unit tests** (`tests/unit/test_*.py`) are standalone scripts run against a
  temp config; each prints `PASS`/`FAIL` and exits non-zero on failure. No
  framework — read one before writing one.
- **Parity** (`tests/parity/`) pins the engine's Python scoring against the
  Explorer's JS mirror; it fails if the two drift.
- **Integration + e2e** boot a real app over mock Plex/Tautulli/Jellyfin servers
  (`tests/mocks/`) with generated fixtures (`tests/fixtures/`).

Config knobs and per-run overrides come through env vars — `MEDIAREDUCER_CONFIG`
(config path), `MEDIAREDUCER_MODE_OVERRIDE`, `MEDIAREDUCER_MANUAL`,
`MEDIAREDUCER_LIBRARY`, the `*_APPDATA` auto-detect paths, and
`MEDIAREDUCER_TRUSTED_HOSTS` (reverse-proxy Host allow-list).

Comments here encode real invariants — fail-closed skips, the plan/mark
contract, ordering rules — and are frequently the only record of *why* a guard
exists. Change behavior and the comment changes with it; touch only comments and
the code should be provably identical (parse both revisions and compare the ASTs
with docstrings blanked).
