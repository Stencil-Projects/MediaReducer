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
`run_tests.sh` fails the run if the unit tier leaves state there.

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
| `test_appdata_mounts` | What the `/tautulli` and `/radarr` mounts contribute. Ports are never read from appdata; a mounted config with no key in it is not "verified" |

**Deletion safety**

| Test | Guards |
|---|---|
| `test_delete_guards` | The checks between a decision and an `unlink`, against real files: symlinks, outside the boundary, unmonitored, `..` escapes, empty allow-list, both debug modes. Asserts the files are still there afterwards. One case deletes for real, so a `delete_candidate` that never deleted anything could not pass |
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
| `test_source_merge` | Plex + Jellyfin merge: one candidate, summed plays, oldest added, unioned protection, distinct users = the higher of the two. A path divergence is skipped, not double-counted |
| `test_candidate_sources` | `build_candidates` under all three server configurations, driving the real filter and protection branches |
| `test_jellyfin_fetch` | Jellyfin's shapes normalize to Tautulli-shaped rows; BoxSet protection applies by movie, IMDb and TMDb id; a missing BoxSet fails closed |
| `test_tautulli_refresh` | The scan forces a Tautulli media-info refresh, so a recently added but already watched movie isn't seen as a 0-play deletion candidate |
| `test_media_server_integration` | The Plex / Jellyfin / Radarr integrations over their real HTTP functions against a localhost mock: the URL, auth and parse layer the stubbed tests skip |
| `test_engine_helpers` | Engine internals no scenario reaches: Tautulli dedup, config coercion, and the IMDb pipeline including decompression-bomb caps |
| `test_library_snapshot` | The snapshot survives cache clears and interrupted runs; completed scans replace it |

**The marked queue**

| Test | Guards |
|---|---|
| `test_reverify_marked` | A movie a recent watch lifted out of the delete set is un-marked and the next eligible backfills it. The one-way safety belt means a failed fetch can only spare a movie, never doom it |
| `test_reverify_tick` | The 15-minute maintenance re-sizes the marked set to the current deficit, and unschedules everything at zero |
| `test_redline_fastpath` | The incremental queue-delete path used by both a Redline emergency and a manual Cleanup, including which one tells Radarr to forget |
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
| `test_reset_then_setup` | After a reset, setting up again reaches Monitor Only, same as a fresh install |
| `test_connection_probe_parallel` | The parallel connection check reports exactly what the sequential one did |
| `test_save_health_reuse` | A save re-probes only when the answer could have moved, and always after a failure |
| `test_save_nav_guard` | Navigation is blocked while a save is in flight or the form is dirty |
| `test_page_delivery` | Pages are gzipped and served entirely from this container. Fails if a template ever reintroduces a CDN link |
| `test_progress_phases` | Each progress step fills 0→100 once; path resolution reports under the indeterminate step |
| `test_log_stages` | Each log stage reports its own duration, in the right place. One `log_stage` call moves the dashboard step AND writes the banner, and a failure marks the stage that was actually open |
| `test_size_format` | Measured sizes always carry one decimal, typed settings stay whole, and a missing reading is a dash rather than a confident zero. Also pins app and notify to the same byte formatting, which had drifted to GiB on one side |
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
| `e2e_server_toggle` | Which Server software selections leave Movie Library Paths and Space Thresholds editable. A server the saved config had off is unknown rather than broken, so ticking one back on does not lock both sections; a server the save did select is still held to its health, and a fresh install cannot unlock by ticking a box it has never connected to |
| `e2e_thresholds` | Space Thresholds states the figure each trigger needs and fills toward it, draws the shared disk bar, keeps working with nothing armed, and never quotes a value the save would discard |
| `e2e_confirm_modal` | The dialog in front of both destructive actions. Only an explicit answer acts, every exit is driven, and a Cleanup blocked while the dialog sat open does not run on the answer |
| `e2e_prune_confirm` | The save-time "this will prune" warning, including which schedule each threshold actually names |
| `e2e_debugghost` | Ticking Debug mode ghosts Automatic Cleanup immediately, and unticking restores the exact prior state |
| `e2e_debuglive_btn` | On a Debug-mode dashboard the Cleanup button is Debug Cleanup, using the Simulate gate, with its own running visuals |
| `e2e_countdown_label` | The countdown names the event it counts to, in every mode |

## Environment

| Var | Purpose |
|---|---|
| `PLAYWRIGHT_MODULE` | import path for playwright (default `playwright`) |
| `PW_CHROMIUM` | explicit chromium binary |
| `MR_E2E_PORT` | base port for the opt-in tiers (default 5057; +1/+2 also used) |
| `MR_E2E_SECOND_RUN` | `0` skips `e2e_fullrun`'s second, cache-reuse Simulate |
