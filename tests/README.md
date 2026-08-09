# MediaReducer tests

```bash
tests/run_tests.sh                # unit + scoring parity (hermetic, no network, no browser)
tests/run_tests.sh --integration  # + the full run pipeline over HTTP, every server profile
tests/run_tests.sh --e2e          # + the browser page tests
```

Three tiers, cheapest first. `--integration` boots a real app against mock
servers and drives scan → score → queue over `fetch`, so it needs only python
and node. `--e2e` adds the chromium tests, the only ones needing playwright.

Each test is a standalone script that prints `PASS`/`FAIL` lines and exits
non-zero on failure. There is no framework: read one before writing one.

Tests keep their state out of `/config`. Every path both processes write hangs
off `OUTPUT_DIR`, whose default IS `/config`, so a test that leaves it at the
default writes into the deployment data directory on a real host. `_tmpout.py`
gives a test its own: `config()` before importing app, `redirect_engine()` after
importing engine, which repoints the path constants engine fixes at import.
`run_tests.sh` compares /config's file list either side of the unit tier and
fails on any difference. It plants a canary named `mediareducer.db` first, since
the destructive path unlinks the store BY NAME and a create-then-wipe otherwise
leaves nothing to notice. Directories are ignored: counting them made the check
fail about one run in five.

GitHub Actions runs the `--e2e` tier on every push and pull request
(`.github/workflows/tests.yml`). It needs no secrets, since the media servers
are mocks the harness starts itself. When something fails, the harness keeps its
temp directory and the workflow uploads it as the `test-logs` artifact, so the
log path named in the failure line is actually there to read.

## Unit (`tests/unit/`)

**Config and validation**

| Test | Guards |
|---|---|
| `test_threshold_matrix` | Every (mode, headroom, redline, cap) combination gets the same verdict from all three validators: the save handler, the file validator, and the engine |
| `test_optional_value_memory` | A disabled optional field keeps its value across saves; every threshold may be off at once; a blank enabled field is rejected rather than read as 0 |
| `test_redline_only` | Redline-only mode: validation, the always-on Simulate gate, the standing preview queue |
| `test_delete_delay` | Delay validation, queue composition, plan currency, and what a threshold-changing save does to existing clocks |
| `test_time_zone` | `TIME_ZONE` drives the daily run, delay aging, and log timestamps; `auto` means the container clock |
| `test_config_cache` | `load_config()`'s memo is invisible: isolated copies, writes invalidate, file issues still surface |
| `test_mode_roundtrip` | Scheduler mode through the real save handler. The flag derivation, forced-off cascade and auto-enable are decided in one request, so a regression in their ORDER only shows up here |
| `test_service_port` | The listening port: 7474 by default, `MEDIAREDUCER_PORT` when set, refused rather than ignored when it is not a usable port. The CLI default and the Docker healthcheck follow the same variable |
| `test_library_mount_state` | A missing or empty `/library` says so, instead of blaming Plex path alignment for a mount that is not there. Both states still block |
| `test_media_path_check` | The configuration check's media-path layer: the app-side fingerprint resolver mirroring the engine's, sampled sizes verified against the disk, a stale size or two passing, a MAJORITY of size mismatches or unmatched samples failing the check as the wrong library (a lone stale size or ghost entry warning instead), folder-shaped samples excluded from the check, and the explained variant's reasons (what the debug buttons print) |
| `test_basic_hardening` | The baseline, checked rather than assumed: config.json (a Plex token, three API keys, any webhook URLs) is written owner-only rather than at the default world-readable umask; every response refuses framing and sniffing; the mutating-request header gate is matched on its own words, since an empty body 400s either way and a status check alone would pass with the gate gone; both path-taking routes stay inside their root; no shell, no Werkzeug debugger |
| `test_appdata_mounts` | What the `/tautulli` and `/radarr` mounts contribute. Ports are never read from appdata; a mounted config with no key in it is not "verified" |

**Deletion safety**

| Test | Guards |
|---|---|
| `test_delete_guards` | The checks between a decision and an `unlink`, against real files: symlinks, outside the boundary, unmonitored, `..` escapes, empty allow-list, both debug modes. Asserts the files are still there afterwards. One case deletes for real, so a `delete_candidate` that never deleted anything could not pass |
| `test_delete_boundary` | `is_safe_to_delete()` — the last gate before `unlink` — under an adversarial path matrix rather than incidental coverage |
| `test_absent_config_disarms` | A config.json missing its threshold keys disarms the engine instead of falling back to example values that would delete |
| `test_engine_mode_gate` | `main()` refuses to run unless `RUN_MODE` is executable, so a paused scheduler stays safe even if something launches the engine anyway |
| `test_debug_mode` | Debug mode and Automatic Cleanup are mutually exclusive, and `debug_cleanup` can never delete |
| `test_protection_failclosed` | A protected collection matching nothing aborts a deleting run rather than running unprotected |
| `test_upkeep_guards` | Files confirmed gone are pruned everywhere; an offline library never reads as "everything was deleted" |
| `test_safety_autopause` | A tick with unsafe thresholds pauses Automatic Cleanup with the reason |
| `test_reset_run_guard` | Reset and clear-cache can't race a run: both hold the run lock across check and wipe |
| `test_force_stop` | The Advanced escape hatch. A kill that does NOT take is reported as a failure, and the run state is deliberately not cleared behind a process that may still be deleting |
| `test_graceful_shutdown` | SIGTERM to the app forwards the stop to the engine and waits for it |

**Scanning, scoring and merging**

| Test | Guards |
|---|---|
| `test_source_merge` | Plex + Jellyfin merge by FINGERPRINT (folder + file name identity key): one candidate, summed plays, oldest added, unioned protection, distinct users = the higher of the two — and a same-fingerprint pair under diverging deeper paths MERGES (favorite/protection landing on the deletable row) instead of being skipped as a twin |
| `test_candidate_sources` | `build_candidates` under all three server configurations, driving the real filter and protection branches; provider ids arbitrate identity — agreeing ids on a same-fingerprint pair merge to one candidate, conflicting ids skip it, same-named copies on both servers pair per-copy (exact resolved keys before the fingerprint), and a FLAT-layout file (name+size identity) with Jellyfin enabled skips until Tautulli, Jellyfin, and the disk all agree on the bytes |
| `test_jellyfin_fetch` | Jellyfin's shapes normalize to Tautulli-shaped rows; BoxSet protection applies by movie, IMDb and TMDb id; a missing BoxSet fails closed |
| `test_tautulli_refresh` | The scan forces a Tautulli media-info refresh, so a recently added but already watched movie isn't seen as a 0-play deletion candidate |
| `test_media_server_integration` | The Plex / Jellyfin / Radarr integrations over their real HTTP functions against a localhost mock: the URL, auth and parse layer the stubbed tests skip |
| `test_engine_helpers` | Engine internals no scenario reaches: Tautulli dedup, config coercion, and the IMDb pipeline including decompression-bomb caps |
| `test_library_snapshot` | The snapshot survives cache clears and interrupted runs; completed scans replace it |
| `test_fresh_watch_batching` | The delete-time Jellyfin re-verify batches its ids (one giant URL was an HTTP 414), and a partially-answered user is discarded whole rather than undercounting plays |
| `test_path_resolution` | Fingerprint-only resolution: folder+filename matching from any mount prefix (a stale size never rejects it — the disk is the size authority), sizes disambiguating same-named copies, the (filename, size) rescue with identical twins never guessed between, the whole-mount index, the per-run reset, folder-shaped server paths (section locations) excluded from the check entirely, the wrong-library tripwire for BOTH failure shapes (a MAJORITY of size mismatches or of unmatched samples aborts the pre-check; a lone stale size or ghost entry only warns), duplicate samples of one file counting once, and protection paths SURVIVING ambiguity (an unresolvable favorite path stays in the set raw and still protects every same-named copy through the fp key) |

**TV and the one pool**

| Test | Guards |
|---|---|
| `test_tv_inventory` | The TV inventory comes from the MEDIA SERVERS — each fetcher's row shape from its wire format, the title-merge carrying both server identities, and strictness: the deletion path raises when a configured server fails; Sonarr alone can never supply it |
| `test_tv_store` | The store contracts: each scan owns its type, a server blip leaves stored rows alone while an empty answer clears them, folder-name scope resolution, and the code-wipe → refresh sequence |
| `test_tv_watch_aggregation` | Per-series and per-season watch facts take the MAX across servers, never the sum — one household on two servers is one audience |
| `test_season_score` | The season recipe on the movie scale: plays as movie-watch equivalents through the movie play curve (weight 100–200%), season users counting what a movie's do, the season-added-date freshness fallback, the all-season boost's log curve, the 100-point clamp |
| `test_tv_season_plan` | Who steps aside (scope, protected, favorites under the shared toggle, per-season grace, IMDb rules, the latest-season shield incl. unknown status) and the season-eligibility modes — oldest-only default, except-newest by ADDED date, all |
| `test_pool_split` | One deficit, two executors: the merged movie+season order, the covering-prefix split, and the engine's tv_share handshake — a stale stamp reads 0 so the movie side covers everything |
| `test_shared_pass` | `shared.py`'s contracts (deficit arithmetic, the calendar-day delay clock, the deleted.log line round-tripping the history regex, the rung decisions) AND the wiring: bending one shared helper bends both executors' answers, proving neither keeps a private copy that could drift |
| `test_tv_cleanup` | The season deletion pass's safety contract: marks wait out the delay, Monitor Only never deletes, fail-closed aborts, Sonarr unmonitor-before-delete when configured (and none needed when absent), server-path escape guards, unmark-instead-of-delete when the plan moves, the per-run gate, and the pool's ONE marked & eligible window |

**The marked queue**

| Test | Guards |
|---|---|
| `test_reverify_marked` | A movie a recent watch lifted out of the delete set is un-marked and the next eligible backfills it. The one-way safety belt means a failed fetch can only spare a movie, never doom it |
| `test_reverify_tick` | The 15-minute maintenance re-sizes the marked set to the current deficit, and unschedules everything at zero |
| `test_redline_fastpath` | The incremental queue-delete path used by both a Redline emergency and a manual Cleanup, including which one tells Radarr to forget — and a file replaced since the plan deleting at its FRESH on-disk size, the corrected size persisted to the queue |
| `test_fastpath_protection_match` | The no-rescan paths recognize a protected movie even when the path differs by symlink, mount prefix or case |
| `test_debug_live_tick` | Debug Cleanup is "Cleanup minus deletion": it applies and persists the same queue upkeep, then only previews |
| `test_reconcile_from_snapshot` | Rebuilding the queue from the stored snapshot under new settings, with no library walk and no server call |
| `test_reconcile_trigger` | Which saves reconcile, which re-fetch, and which are held because the server is down |
| `test_daily_maintenance_sim` | The scheduler tick end to end, and what demotes or wakes each scheduler mode |
| `test_startup_invalidation` | Startup preserves the store; whether the plan can still be trusted is decided live |
| `test_startup_cap_survives` | A Library Size Cap stays armed across a restart, and a cap-only setup stays valid |

**The store**

| Test | Guards |
|---|---|
| `test_store_damage_recovery` | Only proven damage wipes the store. Merely locked or unopenable is left alone, because destroying a healthy store is worse than one failed run |
| `test_store_invalidation_inputs` | Schema fingerprint, engine checksum and scoring constants each invalidate what they should |
| `test_store_read_cache` | The read fast paths stay invisible, and an app-side write invalidates its own request's snapshot |

**App and UI behavior**

| Test | Guards |
|---|---|
| `test_app_coverage` | `/api/run`'s safety default, the run-log section extraction, and the hostile-input guards on the hand-editable queue |
| `test_live_button_state` | Cleanup ghosts when limits are satisfied while Simulate stays available; unknowns fail open |
| `test_radarr_health_gate` | Radarr health gates only real Cleanups, never a Simulate |
| `test_scan_lock_reason` | Why Monitor Only is still locked is answerable and specific, and an incomplete setup outranks the rest |
| `test_autopause_note` | The "Automatic Cleanup paused itself" banner: shown once with its reason, dismissible for good, never resurrected by a reload |
| `test_startup_keep_cleanup` | The "Set to Monitor Only at startup" opt-out: off means a restart resumes Automatic Cleanup instead of demoting |
| `test_run_slot_handoff` | The `_summary_active` slot shared by the quiet Summary, its sync twin, and the save reconcile: whoever clears it honours what queued behind it |
| `test_library_browse` | The library browser answers in the `/library` namespace even when the root is a symlink or bind mount |
| `test_abort_name_disclosure` | Abort sentences that name a film on the dashboard never carry that name into a webhook unless Movie names is opted in |
| `test_reset_then_setup` | After a reset, setting up again reaches Monitor Only, same as a fresh install |
| `test_connection_probe_parallel` | The parallel connection check reports exactly what the sequential one did; the media-path check re-walks fresh when it runs, runs only on Check for Errors or a monitored-paths change, every other probe replays its stored per-server verdict untouched, and a failed judge (sampler down) keeps the retained verdict instead of wiping it |
| `test_save_health_reuse` | A save re-probes connections only when the answer could have moved, always after a failure, and always on a monitored-paths change (which is one of the media-path check's two triggers; ordinary saves never re-run that check) |
| `test_save_nav_guard` | Navigation is blocked while a save is in flight or the form is dirty |
| `test_page_delivery` | Pages are gzipped and served entirely from this container. Fails if a template ever reintroduces a CDN link |
| `test_progress_phases` | Each progress step fills 0→100 once; path resolution reports under the indeterminate step |
| `test_log_stages` | Each log stage reports its own duration, in the right place. One `log_stage` call moves the dashboard step AND writes the banner, and a failure marks the stage that was actually open |
| `test_size_format` | Measured sizes always carry one decimal, typed settings stay whole, and a missing reading is a dash rather than a confident zero. Also pins app and notify to the same byte formatting, which each implement separately |
| `test_debug_report` | The report carries the decision state and never leaks names, paths or IPs |
| `test_deleted_log` | The history parser, with and without the optional rationale fields |
| `test_radarr_cleanup` | Radarr forgets a movie when its own copy is deleted, and never on someone else's |
| `test_run_context_log` | Every run opens its log with the RUN CONTEXT header, across every threshold combination |
| `test_run_issues` | The warning vocabulary every surface renders from. Reads engine.py for bare WARNING/ERROR lines that would never reach the dashboard, and flags a declared category nothing raises |
| `test_no_undefined_names` | pyflakes over the shipped modules. Exists because `marked_alert_armed` was referenced and never defined, in a branch nothing reached |

**Notifications**

| Test | Guards |
|---|---|
| `test_notify` | Apprise URL assembly, alert-type gating, and the fresh-install defaults |
| `test_notify_state` | One marked-change alert per change and never a repeat; the low-space warning arms once; a muted mode advances no baseline |
| `test_notify_rate_limit` | The per-destination floor, with no path around it. A blocked alert is held and merged rather than dropped, carries no note about the delay, and the wording is derived from the interval rather than hardcoding minutes |

## Parity (`tests/parity/`)

`gen_py_scores.py` scores a balance × age × users grid through the real engine.
`parity_check.cjs` replays the same grid through the Score Explorer's actual JS,
extracted from the template, and fails on drift over 0.01 points. This is the
guard for "the engine and the preview must never disagree".

## Integration and browser (`tests/e2e/`)

Both tiers boot mock Tautulli and Jellyfin serving the same disposable library,
config and ratings file built by `tests/fixtures/make_fixtures.py`. Nothing
outside the temp directory is touched, and the fixture's IMDb URL points at a
dead port so an accidental network fetch fails loudly.

**Integration** (`--integration`, no browser):

- `e2e_fullrun.mjs` runs a real Simulate to completion and checks it writes the
  snapshot and queue. Run once per server profile (`plex`, `jellyfin`, `both`)
  so the whole pipeline is exercised against each. Plex runs twice, to prove the
  metadata cache is reused.
- `cli_smoke.py` drives `cli.py` against the same running service.

**Browser** (`--e2e`, needs playwright and chromium):

| Test | Guards |
|---|---|
| `smoke_all` | All three pages load with no JS errors or stray `NaN`, plus the cross-page invariants: each engine phase lights its own stepper step, a theme flip never shows two palettes at once, the sticky header and title bar meet with no gap, no page scrolls sideways at phone widths, run issues share one left edge, and a big log opens without hanging (off-screen lines skipped, the tail still landing at the true bottom, and the whole log still copying) |
| `e2e_runlock` | Every Configuration section locks and unlocks with run state, and the sweep leaves each control exactly as it found it. Force stop is the only live control during a run |
| `e2e_status_pills` | The header badge, run pill and Last Run pill each wear their button's color, compared as numbers in both themes. Also the words: a Cleanup says "Cleaning", a clicked Simulate says so, and a page loaded mid-run opens on the right badge instead of blinking |
| `e2e_server_toggle` | Which Server software selections leave Media Library Paths and Space Thresholds editable. A server the saved config had off is unknown rather than broken, so ticking one back on does not lock both sections; a server the save did select is still held to its health, and a fresh install cannot unlock by ticking a box it has never connected to |
| `e2e_thresholds` | Space Thresholds states the figure each trigger needs and fills toward it, draws the shared disk bar, keeps working with nothing armed, and never quotes a value the save would discard |
| `e2e_confirm_modal` | The dialog in front of both destructive actions. Only an explicit answer acts, every exit is driven, and a Cleanup blocked while the dialog sat open does not run on the answer |
| `e2e_prune_confirm` | The save-time "this will prune" warning, including which schedule each threshold actually names |
| `e2e_debugghost` | Ticking Debug mode ghosts Automatic Cleanup immediately, and unticking restores the exact prior state |
| `e2e_debuglive_btn` | On a Debug-mode dashboard the Cleanup button is Debug Cleanup, using the Simulate gate, with its own running visuals |
| `e2e_countdown_label` | The countdown names the event it counts to, in every mode |
| `e2e_explorer_type_filter` | The Filtering page's library table with TV in the pool: the type filter scores and ranks what it shows, season rows carry the Type column and run the season eligibility ladder (latest-season shield, the eligibility dropdown, the per-type scope toggles), and the TV sliders re-score live |
| `e2e_breach_note` | The Dashboard's breach note and the arithmetic behind every panel quoting space figures — including armed-but-unable when a media server stops answering |
| `e2e_mode_stale` | The Configuration page never POSTs a Scheduler Mode the server has already left behind (an autopause between load and save) |
| `e2e_page_notes` | Page-level notes: still-in-effect ones pinned with no X, already-done ones dismissible underneath — and dismissed means gone |
| `e2e_startup_mode_option` | The checkbox living inside the click-to-select Automatic Cleanup card: ticking it must not also arm the deleting mode |
| `e2e_last_run_colon` | The Last run clock's colon ticks while a run is active and holds still when idle |
| `e2e_config_rhythm` | Every group on Configuration sits at one rhythm: same gap from rule to heading, same gap from heading to fields, measured rather than asserted from the CSS. Spacing had been written per instance in Bootstrap utilities and drifted to three divider weights and two heading gaps — one of them depending on whether the heading shared a grid column with its first field. Also holds the line: no divider may carry its own margin utility, and no heading may skip the shared class |
| `e2e_slider_scroll_guard` | Scrolling a finger down a slider-heavy page never moves a slider. Sweeps every page for `input[type=range]` rather than naming them, so a slider added later is covered the day it lands: each must carry `touch-action: pan-y`, snap back (and re-fire `input`) on a vertical gesture, and still follow a horizontal drag |

## Scenarios (`tests/scenarios/`)

Part of the `--e2e` tier, so every push runs it. 29 fixed scenarios, each a
whole world — movies and multi-season shows on disk, a mock Jellyfin serving
exactly what the disk holds, a config — driven through real Simulate and
Cleanup runs, with the deletion invariants checked after each one and an
explicit expectation of what should have happened.

Fixed, not random: two runs of the same commit must cover the same ground, or a
failure cannot be reproduced and a merge cannot be gated on it. See
`tests/scenarios/README.md`, which also lists the four ways an earlier version
of this reported a clean sweep while testing nothing. It found the
manual-Cleanup delay asymmetry that `test_tv_cleanup` now guards.

## Environment

| Var | Purpose |
|---|---|
| `PLAYWRIGHT_MODULE` | import path for playwright (default `playwright`) |
| `PW_CHROMIUM` | explicit chromium binary |
| `MR_E2E_PORT` | base port for the opt-in tiers (default 5057; +1/+2 also used) |
| `MR_E2E_SECOND_RUN` | `0` skips `e2e_fullrun`'s second, cache-reuse Simulate |
