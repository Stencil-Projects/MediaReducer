# Changelog

## Unreleased

- Unraid installs follow the :latest image tag now. A container installed from an earlier template is still pointing at :alpha, which has stopped moving — change its Repository field to ghcr.io/stencil-projects/mediareducer:latest to keep receiving updates. (15affdc)
- The version number now reads 0.7.0. That is not a downgrade from 1.0.0-alpha.21 — the old counter never tracked how finished the app was, and 0.7.0 says plainly that this is pre-1.0 software still settling. (15affdc)

### Changed

- The welcome guide's warning no longer names port 7474, which is wrong once the port is moved, and its start button now opens Configuration (0c7937f)
- When a connection fails, Configuration now says why and where — nothing listening on that port, the API key refused, the wrong service on that port, no answer — instead of 'Check the URL and API key' for everything (60916ce)
- In Automatic Cleanup, TV seasons now wait out the deletion delay from the run that chose them, as films do — they used to go a day later (8147c5d)

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
