# Changelog

## Unreleased

- Unraid installs follow the :latest image tag now. A container installed from an earlier template is still pointing at :alpha, which has stopped moving — change its Repository field to ghcr.io/stencil-projects/mediareducer:latest to keep receiving updates. (15affdc)
- The version number now reads 0.7.0. That is not a downgrade from 1.0.0-alpha.21 — the old counter never tracked how finished the app was, and 0.7.0 says plainly that this is pre-1.0 software still settling. (15affdc)

### Added

- The welcome guide shows which build you are running beside the version, as the commit it was built from (v0.7.0 · build 7ac4b2c), so two builds of the same version can be told apart (7ac4b2c)

### Changed

- The welcome guide's warning no longer names port 7474, which is wrong once the port is moved, and its start button now opens Configuration (0c7937f)
- When a connection fails, Configuration now says why and where — nothing listening on that port, the API key refused, the wrong service on that port, no answer — instead of 'Check the URL and API key' for everything (60916ce)
- In Automatic Cleanup, TV seasons now wait out the deletion delay from the run that chose them, as films do — they used to go a day later (8147c5d)
- When a connection fails, Configuration outlines only the field to fix — the URL for a wrong address or port, the API key when the key was refused (a7e6d78)
- A Radarr or Sonarr address that points at the other app's port now says which app is there, instead of reporting the API key as refused (a7b29e2)
- Pages load lighter: the tab icon, stylesheets and scripts are cached and fetched once per version instead of riding along with every page, and Configuration asks the server for each thing once instead of twice (a7aa181)
- The light and dark themes now reach every control (links, code, checkboxes, dropdowns), keyboard focus is visible on every button and field, and on phones the header, run stats and connection cards no longer overlap or break their numbers (a7aa181)
- Wording across the app is shorter and uses one name for each thing (Space Thresholds, Off, deleted and freed, Done), explains scoring in plain words, and capitalizes section headings alike; the Cleanup confirmation says the delay is skipped for seasons too (a7aa181, d8a08ba)

### Fixed

- A Cleanup that frees its space by deleting TV seasons now says what it deleted, instead of 'Nothing to do' with Deleted 0 (6d81675)
- The Library Size Cap's 'no lower than' figure is now always a value you can actually save and run with; saving a cap below it says so (9e04b85)
- Filtering & Scoring labels a show whose folder is claimed twice as 'ambiguous folder' instead of 'off monitored paths', which pointed at path settings that were fine (c11105b)
- In Monitor Only the dashboard no longer states a dated deletion as fact; it says what a run would delete, since nothing deletes on its own (0c7937f)
- Filtering & Scoring shows each TV season's own plays, watchers, last watch and added date instead of the whole show's (0c7937f)
- A manual Cleanup's log no longer lists every eligible film as 'spared' when it only checked the few it needed (0c7937f)
- The dashboard's Scanned tile counts TV seasons as well as films, so it can no longer read lower than Eligible; the lifetime tile says files, which is what it counts (11c7cbd)
- Turning on 'Remove deleted movies from Radarr' now looks for Radarr's Plex library again when it was not found before, instead of only when Radarr's details change (722d4c1)
- A manual Cleanup now deletes the lowest-scoring items of the whole pool, TV seasons included; after a threshold change it could delete higher-scoring films while lower-scoring seasons stayed (06acbc9)
- A Simulate whose plan is TV seasons now marks them and says so, instead of 'nothing marked' and a dashboard asking for another Simulate; the next-deletion line counts seasons too (8147c5d)
- After a settings save, the rebuilt deletion plan includes TV seasons, so the Marked list and the next-deletion line show what a Cleanup would really remove (a4d6bca)
- With both Plex and Jellyfin on, a show the two title differently (like 'The Office (US)' and 'The Office') is managed as one show again instead of being left out of cleanup (8a94c32)
- Right after a Cleanup the dashboard shows the new library size and keeps the Cleanup's result, instead of briefly claiming the library is still over its limits and replacing the result with a Simulate's (86b26b5)
- After a Cleanup, changing a setting no longer puts the films it just deleted back in the Marked list as the next deletion (281b959)
- Before deleting a TV season, MediaReducer now finds the show in Sonarr by its folder or IMDb id, so a show Sonarr names differently is unmonitored instead of being downloaded again (724baaa)
- A TV season MediaReducer deleted no longer comes back into the plan while Plex or Jellyfin still list it, so the dashboard stops counting seasons that are already gone (fdbe7e8)
- Automatic Cleanup no longer stays switched on while every daily run refuses to delete: when the library grows while marks wait out their delay, the run now judges the Library Size Cap the same way the app does (5d37b63)
- Messages that contradicted what the app does are corrected: a Simulate says it marked items (it does), a failed IMDb download says the run stopped, and the safety-percentage note says it blocks the Cleanup button as well (a7aa181)
- A Simulate no longer warns that the Headroom target is out of reach, or overstates the space left used, when films and TV seasons together reach the target (a7aa181)
- The IMDb help that opens when a run stops for lack of ratings now covers a ratings file that could not be read, and says to replace that file — a new .gz in the config folder only helps when the download failed (d8a08ba)
