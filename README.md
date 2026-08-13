# MediaReducer

> [!WARNING]
> **This is early, experimental software, and its whole job is deleting files.**
> Assume something will eventually go wrong: keep backups, lean on Simulate,
> and don't point it at media you can't stand to lose. There is no recycle bin.

MediaReducer is a Dockerized web app for keeping a large media library under
control. It reads your library from Plex/Tautulli, Jellyfin, or both, scores
every movie and TV season by watch history and IMDb rating, and deletes the
lowest-value ones first when your storage thresholds are crossed.

It is built for one situation: a NAS where the media is secondary. The backups,
photos and documents come first, the library lives in the leftover space, and
when that runs out MediaReducer guesses what you are least likely to miss. If
you want fine-grained rule-based control instead, you probably want
[Maintainerr](https://github.com/jorenn92/Maintainerr).

> **Do not expose the web UI to the internet.** There is no login, so anyone who
> reaches it can reconfigure MediaReducer and delete your media. Keep it on your
> LAN and use a VPN from outside. If you already reverse-proxy it,
> `MEDIAREDUCER_TRUSTED_HOSTS` lets that domain past the DNS-rebinding guard —
> that is not a login, so put authentication in front of the proxy yourself.

## Contents

[Requirements](#requirements) · [How paths are matched](#how-paths-are-matched)
· [Install](#install) · [Volumes](#volumes) ·
[Outside Docker](#running-outside-docker) · [Setup](#first-time-setup) ·
[Filtering & Scoring](#filtering--scoring) · [Dashboard](#dashboard) ·
[Run modes](#run-modes) · [TV cleanup](#tv-cleanup) ·
[Notifications](#notifications) · [Safety rules](#safety-rules) ·
[Command line](#command-line) · [Files](#persistent-files) ·
[Troubleshooting](#troubleshooting)

## What it does

- Reads your library from Plex/Tautulli, Jellyfin, or both.
- Only ever touches the `/library` folders you tell it to manage.
- Skips anything protected, recently added, unplayed, highly rated, or
  favorited in Jellyfin (the last four optional).
- Scores the rest on watch history and IMDb rating, blended by one dial, and
  deletes the lowest first — movies per title, TV per season, one order.
- Triggers on a free-space target, an emergency floor, or a library size cap.
- Previews all of it against your real library before anything runs.
- Optionally tells Radarr to forget a deleted movie, or Sonarr to unmonitor a
  deleted season, so neither is re-downloaded.
- Optionally alerts you (Discord, Telegram, ntfy and more) when a run finishes.

## Requirements

- Docker Compose (or Unraid's Compose Manager).
- A media library mounted into the container at `/library`.
- Plex with Tautulli, Jellyfin, or both. Plex mode requires Tautulli; a Plex
  URL + token additionally unlock protected Plex collections. Jellyfin mode
  requires a Jellyfin URL + API key.
- Internet access from the container for the IMDb ratings dataset.
- Radarr and Sonarr are optional.

## How paths are matched

MediaReducer deletes from its own `/library` mount, and your media servers see
the same files under different paths. Movies are matched by **fingerprint** —
the movie's folder name + file name, with the server's byte count
disambiguating same-named copies — so mount prefixes never need to line up:

```text
MediaReducer: /library/movies/Bob (2020)/Bob.mkv
Plex:         /data/movies/Bob (2020)/Bob.mkv        ✓ same folder + file name
Jellyfin:     /media/x/films/Bob (2020)/Bob.mkv      ✓ same folder + file name
```

What must agree is the folder and file name. A server that sees a bare file with
no movie folder (`/downloads/Bob.mkv`) can only match through a unique
(file name, size) hit; a bare name alone never matches, since that could be a
different film. TV series match the same way at folder grain — the series folder
name under a monitored path. A series folder name found under **two** monitored
paths is ambiguous and is skipped rather than guessed at.

**The file on disk is the size authority.** A file whose bytes differ from the
server's count (usually a quality upgrade the server hasn't rescanned) still
matches, and plans, deletions and history all carry the on-disk bytes. Files
stored flat, with no movie folder, lean on name + size instead, so they are
deletable only once every enabled server and the disk agree.

When **most** sampled files disagree in size, that looks like the wrong library
— a stale backup mounted at `/library` — and it is treated as an error: the
configuration check fails and a run's pre-check aborts before anything is
scored. **Check for Errors**, and saving your monitored paths, both re-run the
whole check against the current disk.

## Install

```bash
git clone https://github.com/Stencil923/MediaReducer.git /mnt/user/appdata/mediareducer
cd /mnt/user/appdata/mediareducer
cp .env.example .env
```

```env
PLEX_LIBRARY_PATH=/mnt/user/media
TAUTULLI_APPDATA=/mnt/user/appdata/tautulli
RADARR_APPDATA=/mnt/user/appdata/radarr
SONARR_APPDATA=/mnt/user/appdata/sonarr
MEDIAREDUCER_DATA=/mnt/user/appdata/mediareducer/config
WEBUI_PORT=7474
```

`PLEX_LIBRARY_PATH` is the root folder containing your media folders: if your
movies are at `/mnt/user/media/Movies`, set it to `/mnt/user/media` and add
`Movies` as a monitored path in the UI. `WEBUI_PORT` moves only the host side;
the container keeps serving on 7474, so its health check is unaffected.

```bash
docker compose up -d --build      # then open http://your-server-ip:7474
```

## Volumes

| Container path | Required | Purpose |
| --- | --- | --- |
| `/library` | **yes** | Media files MediaReducer may scan and delete from. Must be writable. |
| `/config` | **yes** | Config, logs, cache, IMDb data, deletion history. |
| `/tautulli` | no | Lets **Auto Detect** read the Tautulli API key *and* the Plex token/URL from `config.ini`. |
| `/radarr` | no | Lets **Auto Detect** read the Radarr API key from `config.xml`. |
| `/sonarr` | no | Same, for Sonarr. |

The appdata mounts are a setup convenience only — everything after Auto Detect
goes over HTTP. Skip them and type the keys in yourself, or remove them once the
keys are saved. There is no Jellyfin mount because Jellyfin issues API keys from
its dashboard and stores none on disk.

Whatever is at `/library` is what MediaReducer *can* delete; monitored paths are
a setting on top of that. To put a folder beyond reach, mount it read-only at
the matching spot underneath — `/mnt/user/media/Music` → `/library/Music:ro`.
Docker layers that over the parent mount. Add it alongside the `/library` mount,
never instead of it: free space is measured on `/library` itself, so replacing
it with subfolder mounts reads the container's own layer rather than the array.

Every URL field defaults to its service's documented port (8181 Tautulli, 32400
Plex, 8096 Jellyfin, 7878 Radarr, 8989 Sonarr). Ports are never read from
appdata, so enter the URL yourself for a non-standard port.

## Running outside Docker

MediaReducer is `app.py` (web UI) and `engine.py` (worker). Run `python3 app.py`
and it serves on 7474 — the same URL either way. Each path can point anywhere:

| Variable | Default | Points at |
| --- | --- | --- |
| `MEDIAREDUCER_LIBRARY` | `/library` | Your media library — the only place it can delete from. |
| `MEDIAREDUCER_CONFIG` | `/config/config.json` | Where config and state are written. |
| `MEDIAREDUCER_TAUTULLI_APPDATA` | `/tautulli` | Auto Detect only. |
| `MEDIAREDUCER_RADARR_APPDATA` | `/radarr` | Auto Detect only. |
| `MEDIAREDUCER_SONARR_APPDATA` | `/sonarr` | Auto Detect only. |
| `MEDIAREDUCER_PORT` | `7474` | Port to listen on. |

```bash
MEDIAREDUCER_LIBRARY=/mnt/tank/media \
MEDIAREDUCER_CONFIG=~/.mediareducer/config.json \
python3 app.py
```

The library path stays the deletion boundary wherever you put it.

### Container user & health

The container runs as root, which works on any host. Set `PUID`/`PGID` in the
compose file to run as a specific user instead — that user needs write access to
your media files and `/config`. On Unraid that is usually `PUID=99` / `PGID=100`;
elsewhere use `id -u` / `id -g`. The image has a health check, so `docker ps`
shows `healthy` rather than just `running`.

## First-time setup

On first launch the UI shows a welcome guide; reopen it any time with **?** in
the header. Work down the Configuration tab.

### 1. Scheduler Mode

A fresh install starts **Paused** with the other modes ghosted. Monitor Only
unlocks once you connect a media server, add a monitored path, arm a space
threshold and run **Simulate**, then switches on by itself.

- **Paused** — no scheduled activity. Storage numbers still refresh and
  Dashboard runs still work.
- **Monitor Only** — never deletes on its own. A 15-minute refresh keeps the
  marked queue and library size current; a daily Simulate keeps the plan fresh.
- **Automatic Cleanup** — deletes on schedule. Locked until setup is complete,
  the health check passes, and at least one media type is allowed in
  [Cleanup scope](#cleanup-scope).

Restarts keep Paused and Monitor Only. Automatic Cleanup drops back to Monitor
Only, since a restart is usually an upgrade or a crash and the plan on disk may
no longer describe the library. Clear **Set to Monitor Only at startup** to have
it come back armed — though a missing or badly out-of-date library database
still demotes, which is the point of the demote.

### 2. Connections

Pick Plex, Jellyfin, or both if they point at the same files. **Auto Detect API
Keys** fills what it can from the mounted appdata. URL fields can stay blank —
MediaReducer assumes your server's address on its standard port. The API key is
the on/off switch for each service, so leaving an optional one blank keeps it
off.

**Check for Errors** confirms every API answers and that server paths match real
files under `/library`. When something fails the Configuration tab turns red and
jumps you to the bad fields.

### 3. Media Library Paths

Add the `/library` folders MediaReducer may manage — the deletion allow-list for
movies and TV alike:

```text
Movies
TV Shows
Kids Movies
```

An empty list means it manages nothing, and run controls stay locked. Within
those folders it handles the video formats both Plex and Jellyfin support
(`.mkv .mp4 .m4v .avi .mov .wmv .ts .m2ts .mts .webm .mpg .mpeg .m2v .flv .3gp
.3g2 .asf .divx .ogm .ogv`). Everything else — subtitles, artwork,
disc-structure rips — is never counted or deleted. Change the set with
`mediareducer config set MOVIE_EXTENSIONS=…`, then run a fresh Simulate.

With Plex or Jellyfin connected this section also offers protected collection
pickers; selected collections are always skipped.

Two optional integrations live here, **both off by default** — reaching into
another app's state is opt-in:

- **Optional Radarr cleanup** removes a deleted movie from Radarr so it is not
  re-downloaded. It never asks Radarr to delete files.
- **Optional Sonarr cleanup** unmonitors a season in Sonarr *before* the files
  go. Left off, seasons still delete, and Sonarr re-downloads any that stay
  monitored.

### 4. Space Thresholds

Unlocks after a monitored path is saved. The panel shows what your storage is
doing right now, and each threshold states the figure it needs to reach and how
far away it is — the numbers move as you type, so you can try a value against
the real disk before saving.

Everything the Configuration page locks, greys or explains describes your
**saved** settings, never the form in front of you: a section unlocks, a
Scheduler Mode becomes selectable and a button ungreys when a save makes it
true, not when you type it. The calculators are the exception, and the reason
for the rule — they follow what you type precisely because they decide
nothing. So a threshold you have edited but not saved shows you where it would
put you, while the mode it would allow stays greyed until you save it.

- **Headroom target** — cleanup runs when free space drops below this, and frees
  back up to it.
- **Redline emergency floor** — optional. Below it, cleanup runs immediately and
  frees just enough to get back above. Must sit below the Headroom target.
  Movies only; Redline never deletes TV.
- **Library Size Cap** — optional cap on the measured size of everything under
  your monitored paths, movies and TV together. The daily cleanup trims the
  whole pool back under it.
- **Deletion delay** — whole days a mark waits before a daily cleanup deletes
  it. Redline and manual runs skip the wait. Each mark keeps the delay it was
  made under; **Reset marked delays** moves existing ones to a new value.

Anything that could delete more than you expect opens a dialog spelling out the
consequence first.

**Every threshold can be off at once**, which is how a fresh install ships: with
none armed there is nothing to enforce, so Monitor Only and Automatic Cleanup
stay unavailable. With only a Redline floor, Redline becomes the only thing that
ever deletes and the deletion delay is retired. With only a Library Size Cap,
the normal daily schedule and delay apply without a free-space target.

### 5. Notifications

Optional outbound alerts — see [Notifications](#notifications).

### 6. Advanced

IMDb dataset settings, display/time settings, log retention, cache tools, debug
mode, the headroom safety cap, and **Reset MediaReducer**.

Two appearance settings, stored **per browser** in cookies rather than in the
config, so your phone and your desktop can differ. Both apply on Save, like
everything else here.

- **Reduce visual effects** — no animations, transitions or background blur.
- **Disable background blur** — the frosted glass on its own. Worth turning off
  if scrolling feels slow; it starts off on Firefox for Android, where it costs
  the most.

## Filtering & Scoring

Every rule deciding *what* can be deleted and *in what order*, previewed live
against your full library. Four collapsible categories; a category holding a
field that needs attention shows a red **!** on its header.

### Cleanup scope

Two per-type switches, **both off by default**. Deleting files is opt-in: a
fresh install scans, scores and shows you everything, and deletes nothing until
you say which types it may touch.

- **Movies** — off means no movie is ever eligible. Scans still run and score
  everything, but the deletion queue empties.
- **TV shows** — the master switch for [TV cleanup](#tv-cleanup). Off means no
  season is ever marked or deleted.

With **both** off there is nothing to delete, so Automatic Cleanup cannot be
selected. Simulate stays available, so you can preview what cleanup would do
before allowing a type. Turning both off while Automatic Cleanup is armed drops
the scheduler to Monitor Only and says why on the Dashboard.

### Scoring & ordering

One scale for the whole pool: every eligible movie and season scores 0–100,
higher means keep, lowest deletes first. The score blends:

- **Watch history** — how often played, how recently, by how many people. A
  never-watched title still gets credit for being recently added, and **Max
  staleness** sets how long until it counts as fully stale.
- **IMDb rating** — weighted by how many votes back it. A season reads its
  show's rating.

The dial starts even. Little play history: lean on IMDb. Plenty: lean on watch
history. **File size optimization** (on by default) breaks near-ties by deleting
bigger files first, so you lose the fewest titles, and picks the lower-quality
copy of a duplicated movie.

### Eligibility filters

Applied before scoring — movies per title, TV per season, judged on the show's
facts where noted:

- **Minimum age (grace period)** — added within this many days is skipped; a new
  show holds back all its seasons.
- **Don't delete unplayed titles** — optional. Skips movies with no play history
  and a show's never-watched seasons.
- **Maximum IMDb rating** — optional. Nothing rated above the cutoff is deleted,
  including any season of a show rated above it.
- **Jellyfin favorites** — optional. A favorited show holds back all its
  seasons. Jellyfin only; Plex has no favorites.
- **No IMDb data** — always on while IMDb has weight. Without a rating or votes
  there is not enough to judge on.

Protected collections also apply, and are set on the Configuration tab.

### TV show scoring

TV-only rules, all previewing live:

- **Season eligibility** — which seasons may delete: **only the oldest**
  (default), **any except the newest** (most recently *added*, which may not be
  the latest), or **all**. A continuing show's current season is always held
  back.
- **Season episode cap** (default 50) — a season with more episodes than this is
  held back whatever it scores. Not every show splits its run into seasons:
  anime that never re-numbers, long-running dailies, and flattened folders all
  present the whole show as one season, where deleting "a season" deletes the
  lot. Ordinary seasons top out around 26, so the default leaves roughly a
  season of headroom. A season whose episode count is unknown is judged by the
  other rules instead. **0** turns the cap off.
- **TV show watch weight** (100–200%) — converts season plays to movie-watch
  equivalents: plays ÷ episodes × this weight. At 100%, playing a full season
  once counts like one movie watch; at 200%, two.
- **All-season watch boost** (0–25 points) — every watched episode lifts *every*
  season of that show, growing as more is watched. This is what lifts a watched
  show's untouched seasons above the seasons of shows nobody watches. 0 turns it
  off.

### Library table

Your entire library as of the last run, scored with the settings on screen. Each
row shows its breakdown, and a filtered row says which rule filtered it. The
**#** column is the real deletion order; type an **Over headroom calculator**
target and it reorders live across the whole library. Empty until your first
Simulate. Sliders below let you dial up a hypothetical movie and watch its score
react.

## Dashboard

- **Storage** — free, used, and monitored library size, with ↻ to refresh.
- **Cleanup Targets** — configured headroom and cap; a breached target shows the
  ~GB a run would free.
- **Last Run** — outcome, trigger, current mode.
- **Run Controls** — Simulate, one-time Cleanup (confirms first, naming the ~GB),
  and Stop.
- **Detailed log** — streams the active or most recent run. Every log opens with
  a **RUN CONTEXT** block (mode, targets, disk and library size, which target is
  breached) — read that first when a run surprises you.
- **Marked & Eligible Deletions** — the standing plan: how many are marked, and
  how many are eligible behind them. Movies and seasons in one list.
- **Deleted history** — every real deletion and why it was picked. Erasable.

Buttons disable while setup is incomplete, a selected API is unhealthy, or a run
is active. Cleanup also ghosts while every space limit is satisfied. The tooltip
always says which.

### Reading a run

The stepper and the log share five steps: **Checking**, **Reading library**,
**Scoring**, **Simulating**/**Deleting**, **Done**. Each prints how long it took.

A red × means the dashboard says what failed and what to do; the log carries the
same words on `ABORT:`, the technical detail on `ABORT detail:`, and the stage on
`ABORT stage:`. Quote that stage name when reporting a problem.

## Run modes

**Simulate** scans, scores and logs exactly what would be deleted, deleting
nothing. It also writes the plan Cleanups work from: the whole eligible list in
deletion order, with only the entries needed to meet your targets **marked**.

**Cleanup** (the manual button) deletes to every breached target immediately, no
delay and no waiting for the schedule. Ghosted until a Simulate has shown you the
plan for your current settings.

**Automatic Cleanup** runs on its own; arming it always requires a Simulate
first. The scheduler then checks every 15 minutes:

- Headroom and Library Size Cap cleanups run at most once a day at your **Daily
  run time**. On a day nothing needs deleting, that slot runs a maintenance
  Simulate instead, so the plan stays fresh.
- **Redline** fires on any check that finds free space below the floor.
- **Deletion delay** holds daily deletions; a run marks candidates and a later
  run deletes each as it comes due.
- **The marked list keeps itself current** — every 15 minutes it drops entries
  whose files are gone, that you protected, or that a recent watch pulled out of
  the running, and marks more or fewer as the library changes.
- **The plan stays matched to your config.** Scoring, filter and threshold
  changes rebuild it in place from the last scan. Changing *monitored paths*
  still needs a Simulate.
- **Time zone** is the clock all of this runs on. Auto follows the container
  clock, often UTC.

Stopping or restarting mid-run is safe: the engine finishes the file it is on,
records it, and shuts down cleanly. If your thresholds stop being safe while
armed — say a bulk copy pushes the cap past the safety percentage — the
scheduler returns to Monitor Only and tells you why.

## TV cleanup

TV works in **whole seasons**, never individual episodes. Movies and TV share
one pool: the same monitored paths, the same space triggers, and one deletion
order on the same 0–100 scale.

The inventory — every show, its seasons, episode counts and on-disk sizes —
comes from your media servers (Jellyfin and/or Plex, title-merged when both
watch the same library); watch history comes from Jellyfin and Tautulli.
**Sonarr is optional and cleanup-only**: it never supplies inventory, and is only
asked to unmonitor a season before deletion.

With a media server connected, TV cleanup on, and a Headroom target or Library
Size Cap armed, every run handles seasons right before movies — one cleanup, one
gesture. Each run:

1. **Refreshes from the live servers.** If any configured source fails to
   answer, the season side aborts without touching a file.
2. **Rebuilds the season plan** — every in-scope season ranked worst-kept first,
   with protected and favorited shows, shows outside your monitored paths, and a
   continuing show's newest season held back entirely.
3. **Merges it with the movie queue** into one worst-first order, and walks it
   until the pool's deficit is covered. Seasons in that stretch are **marked**;
   the movies in it stay the movie cleanup's job — the two sides split one
   deficit, never double-covering it.
4. **Deletes due marks** (Automatic Cleanup only) that the fresh plan still
   takes: the season's episode files, freshly listed by the media server, joined
   under the resolved series folder and recorded in `deleted.log`. The disk is
   the size authority. With **Optional Sonarr cleanup** on, the season is
   unmonitored first — a refusal from Sonarr leaves it intact and still marked —
   and Sonarr is asked to rescan afterwards.

A season leaves the marked list the moment the plan stops taking it: the cap was
raised, the show got protected or favorited, someone started watching, or enough
space came back.

## Notifications

Optional outbound alerts via [Apprise](https://github.com/caronc/apprise),
entirely off until you turn **Enable notifications** on.

**When to alert** — the three below start on:

- **Run summaries** — the scheduled daily Simulate and every cleanup: the plan,
  what it removed and freed, and the storage picture. A manually started Simulate
  never notifies (you are watching the dashboard); a manual Cleanup does.
- **Marked between runs** — the 15-minute check can mark more as the disk fills.
  Once per change, never repeatedly.
- **Low space warning** — free space comes within your margin of the Redline
  floor. Sent once on entering the zone, re-armed only after space recovers.
- **Alert in Monitor Only** (off) — Monitor Only stays silent unless ticked.
  Paused always sends nothing, and nothing is lost: the first alert after you
  tick this reports what changed meanwhile.

Every alert says which mode produced it. Under Automatic Cleanup the dates read
as deletions; under Monitor Only the same dates read as eligibility.

**What to include** — both start off, so out-of-the-box alerts stay short:

- **Titles** — lists what was deleted, newly marked, or first in line. Long lists
  are trimmed; without it you still get counts and dates.
- **Failed runs and errors** — alerts when a run stops dead, and lists what went
  wrong on runs that finished with errors, in the same words the log uses.

**Where to send** — fill in only what you use:

| Service   | What to enter                                             |
|-----------|-----------------------------------------------------------|
| Discord   | The channel's webhook URL                                 |
| Telegram  | Bot token + chat ID                                       |
| Slack     | An incoming-webhook URL                                   |
| Pushover  | User key + application token                              |
| ntfy      | Topic (and an optional server for self-hosted)            |
| Gotify    | Server URL + application token                            |
| Custom    | Any [Apprise URL](https://github.com/caronc/apprise/wiki) — email via `mailto://`, Matrix, Home Assistant and 100+ more, one per line |

**Send test notification** fires a test to your saved destinations, so save
first — the button is greyed until a saved service exists. Each destination is
rate-limited to one message per 10 seconds as a backstop; you should never
notice. A user-initiated **Stop** is silent, and so is a **Debug Cleanup**.

**Debug** appears beside the test button while Debug mode is on, like the other
debug tools. It shows what the messages would say without sending anything: the
last completed run's summary rebuilt under your saved settings, plus — when one
is actually waiting — the pending marked-changes alert and the low-space
warning. That is the point of it: Debug mode runs only alongside Paused or
Monitor Only, where real notifications are muted, so this is how you read a
message you would otherwise never receive. It greys out when there is nothing
to show yet. Previewing never consumes a pending alert — leave Debug mode and
re-arm the scheduler, and the real notification still goes out.

Alerts are best-effort: delivered *after* a run's work is done and recorded, on a
separate thread with a timeout, so a slow notification service can never delay or
abort a cleanup.

## Safety rules

- No monitored paths means no scan and no deletion.
- Every deletion must resolve inside `/library` *and* inside a monitored path.
- Deleting is opt-in per media type, and both types ship off.
- A required media API failing during a run aborts it, and a run won't start
  until every selected server is healthy. Radarr blocks a real Cleanup (so it
  can forget what you delete) but never a Simulate.
- Protections fail closed: a protected collection that no longer matches
  anything aborts the run rather than running unprotected.
- Plex/Jellyfin identity mismatches are skipped, never deleted. So is a series
  folder name that matches under more than one monitored path.
- Protected collections and filtered titles are hard exclusions, not score
  penalties.
- The container drops every Linux capability but the four `entrypoint.py` needs
  to become the PUID/PGID user, runs with `no-new-privileges`, and never mounts
  the docker socket. `config.json` is written `0600` — it holds your tokens.
- Editing connection or monitoring settings while Automatic Cleanup is on drops
  it to Monitor Only. A threshold change keeps it running and rebuilds the plan,
  unless the change leaves the library over a limit. Settings lock only while a
  run is active.
- Every run does a fresh safety pre-check before acting. Stop is always safe:
  deletions already made are permanent and recorded, but nothing is left
  half-done.

## Command line

`cli.py` is a thin client for the running service — it calls the same API the
GUI does, so every command goes through identical validation, safety gates and
run state. The CLI and the browser are interchangeable.

```bash
python3 cli.py status                       # dashboard summary
python3 cli.py connections autodetect       # detect + save API keys
python3 cli.py dirs                         # browse library folders
python3 cli.py simulate                     # preview deletions (streams live)
python3 cli.py cleanup --yes                # delete to your thresholds now
python3 cli.py config get                   # print the config (or one KEY)
python3 cli.py config set HEADROOM_GB=500 RUN_MODE=paused
python3 cli.py scoring set SCORE_BALANCE=80 MOVIE_CLEANUP_ENABLED=true
python3 cli.py queue                        # the marked & eligible plan
python3 cli.py history                      # deletion history
python3 cli.py notify test                  # test the configured notifications
python3 cli.py logs --section summary       # print a run-log section
```

Inside the container it is on the PATH as `mediareducer`, with `mr` as a short
alias:

```bash
docker exec -it mediareducer mr status
docker exec -it mediareducer mr config set HEADROOM_GB=500
```

The service URL defaults to `http://127.0.0.1:7474`; override with `--url` or
`MEDIAREDUCER_URL`. Add `--json` for machine-readable output. `simulate`,
`cleanup` and `debug-cleanup` stream progress until the run finishes (Ctrl-C
detaches; the run continues). `cleanup` confirms unless you pass `--yes`.
`--help` works on every command.

## Debug mode

**Debug mode** (Advanced) adds buttons that dump raw connection and run state
into copyable popups. **Download report** builds a diagnostic snapshot safe to
attach to a bug report — titles, hosts, keys, paths and IPs are scrubbed or
replaced with anonymous tokens.

## Persistent files

In the `/config` mount (`MEDIAREDUCER_DATA` on the host):

| File | Purpose |
| --- | --- |
| `config.json` | Saved configuration. |
| `lastrun.log` | Most recent run log. Every Simulate, Cleanup and Debug Cleanup archives a copy into `logs/`. |
| `deleted.log` | Deletion history (erasable from the Dashboard). |
| `logs/` | Archived run logs. |
| `mediareducer.db` | All cached state in one SQLite file: metadata, schedule state, storage stats, the library snapshot, and the last plan. Survives restarts. |
| `progress.json` | Run progress for the web UI, carried across restarts. |
| `last_run_report.json` | Summary of the last run, used to build notifications. |
| `title.ratings.tsv` | IMDb ratings dataset. |

**Reset MediaReducer** (Advanced) removes configuration and state but always
keeps the deletion history, the run logs and the IMDb dataset. It also clears
the appearance cookies, though only for the browser that pressed it — they live
nowhere else.

You can hand-edit `config.json`, but it is checked against the same rules the UI
uses, so an invalid edit locks things down until fixed.

## Updating

```bash
cd /mnt/user/appdata/mediareducer
git pull
docker compose up -d --build
```

Settings and logs live in `/config`, so rebuilding never removes them.

## Tests

`tests/run_tests.sh` runs the unit and scoring-parity suites (hermetic, no
network). `--integration` adds the full run pipeline over real HTTP against mock
servers; `--e2e` adds the browser tests. See `tests/README.md`.

[ARCHITECTURE.md](ARCHITECTURE.md) maps how `app.py` and `engine.py` fit
together: run modes, the request-to-run flow, plan currency, the deletion-delay
model, and the state files.

## Troubleshooting

**The Configuration tab is red.** A selected connection is failing. Opening the
page jumps to the failing fields; fix the values or mounts, then **Check for
Errors**.

**A Config section is locked.** While a run is active every section is locked, on
both tabs — the engine has already read its settings, so an edit would be
discarded or silently apply to the next run. It unlocks when the run finishes.
Otherwise read the banner: usually no server selected, credentials
missing/failing, `/config` unreadable, server paths not matching `/library`, no
monitored path saved, or Scheduler Mode set to Automatic Cleanup.

**A run won't stop.** **Stop** asks the engine to finish the file it is on and
exit. If a run is wedged and Stop has had a minute, **Advanced → Force stop**
kills it outright — the one control that stays available while a run locks
everything else. It costs something: the run's log is not archived, and a file
being deleted at that instant can be missing from the history. If Force stop
reports the engine didn't die, it is stuck in a system call the kernel won't
interrupt, nearly always a network share that stopped answering; check the mount.

**Protected collections don't appear.** They load only after the relevant API
connects. **Check for Errors**, then return to Media Library Paths.

**The library table is empty.** It is built by runs — run a Simulate with a
connected server and at least one monitored path.

**Library size looks stale.** Use the storage card's ↻, or clear the cache.
Stats also refresh on the 15-minute clock and before every run.

**Radarr did not remove a movie.** Radarr cleanup runs only when enabled, Radarr
is connected, and the deleted file was the copy in Radarr's detected section.
Redline emergency deletions skip Radarr cleanup.

**Nothing is eligible.** If the Dashboard says you are over space limits but
nothing is eligible, a media type is off in Cleanup scope or a filter is
disqualifying everything — the Filtering & Scoring table names the rule for each
row.

## License

MIT. See `LICENSE`.
