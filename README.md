# MediaReducer

> [!WARNING]
> **This is early, experimental software. Use it at your own risk.**
> MediaReducer is young, it is bound to have bugs I haven't found yet, and its
> whole job is deleting files. Assume something will eventually go wrong: keep
> backups, lean hard on Simulate, and don't point it at media you can't stand
> to lose.

MediaReducer is a Dockerized web app for keeping large movie libraries under
control. It reads your library from Plex/Tautulli, Jellyfin, or both, scores
every movie by watch history and IMDb rating, and deletes the lowest-value
files first when your storage thresholds are crossed.

It's built for one specific situation: a NAS where the media is secondary to
everything else on the box. The backups, photos and documents come first, and
the movie library lives in the leftover space. When that space starts
running out, MediaReducer's job is to guess which movies you're least likely
to miss and clear those first.

It will probably always be exactly that. If you want fine-grained, rule-based
control over what leaves your library and when, you probably want
[Maintainerr](https://github.com/jorenn92/Maintainerr) instead.

It is a normal Docker Compose app and runs anywhere the container can see your
movie files at `/library`.

> **This app deletes movie files, and there is no recycle bin.** Stay on Monitor Only
> and lean on Simulate until the output matches exactly what you expect before
> you ever run a real Cleanup.

> **Do not expose the web UI to the internet.** There is no login, so anyone who
> can reach it can reconfigure MediaReducer and delete your media. Keep it on
> your LAN, and use a VPN to reach it from outside.
>
> If you already reverse-proxy it, `MEDIAREDUCER_TRUSTED_HOSTS` lets that
> domain past the DNS-rebinding guard. It is not a substitute for a login:
> whoever reaches the domain has full control, so put authentication in front
> of the proxy yourself.

## What It Does

- Reads your library from Plex/Tautulli, Jellyfin, or both.
- Only ever touches the `/library` folders you tell it to manage.
- Leaves alone anything in a protected collection, recently added, unplayed,
  highly rated, or marked a Jellyfin favorite (the last four are optional).
- Scores the rest on watch history and IMDb rating, blended by a single dial,
  and deletes the lowest scores first.
- Kicks in on a free-space target, an emergency floor, or a library size cap.
- Lets you preview all of it against your full library, scored live with the
  settings on screen, before anything runs.
- Shows progress, logs, storage, and a full deletion history on the
  Dashboard.
- Can tell Radarr to forget a movie once its copy is deleted, so it doesn't
  get re-downloaded.
- Can send you an alert (Discord, Telegram, and more) when a run finishes,
  deletes movies, or queues one for delayed deletion.

## Requirements

- Docker Compose (or Unraid's Compose Manager).
- A movie library mounted into the container at `/library`.
- Plex with Tautulli, Jellyfin, or both:
  - Plex mode requires Tautulli. A Plex URL + token additionally unlock
    protected Plex collections.
  - Jellyfin mode requires a Jellyfin URL + API key.
- Internet access from the container for the IMDb ratings dataset.
- Radarr is optional.

## Path Requirement

MediaReducer deletes files from its own `/library` mount. Plex, Tautulli, and
Jellyfin may report different path prefixes, but each movie's path suffix must
line up with a real file under `/library`.

This is OK:

```text
MediaReducer: /library/movies/Bob (2020)/Bob.mkv
Plex:         /data/movies/Bob (2020)/Bob.mkv
Jellyfin:     /data/library/movies/Bob (2020)/Bob.mkv
```

This is not OK:

```text
MediaReducer: /library/movies/Bob (2020)/Bob.mkv
Plex:         /downloads/Bob.mkv
```

If the suffix cannot be matched, the health check blocks setup and run
controls. Click **Check for Errors** after fixing mounts.

## Install

Clone or extract this project on your server:

```bash
git clone https://github.com/Stencil923/MediaReducer.git /mnt/user/appdata/mediareducer
cd /mnt/user/appdata/mediareducer
```

Copy the example environment file and edit it for your paths:

```bash
cp .env.example .env
```

```env
PLEX_LIBRARY_PATH=/mnt/user/media
TAUTULLI_APPDATA=/mnt/user/appdata/tautulli
RADARR_APPDATA=/mnt/user/appdata/radarr
MEDIAREDUCER_DATA=/mnt/user/appdata/mediareducer/config
WEBUI_PORT=7474
```

`WEBUI_PORT` only moves the host side; the container keeps serving on 7474, so
its health check is unaffected. Change it if something else already owns 7474.

`PLEX_LIBRARY_PATH` is the root folder that contains the movie folders you want
available to MediaReducer. For example, if your movies are at
`/mnt/user/media/Movies`, set `PLEX_LIBRARY_PATH` to `/mnt/user/media` and add
`Movies` as a monitored path in the UI.

Start the container and open the web UI:

```bash
docker compose up -d --build
```

```text
http://your-server-ip:7474
```

## Docker Volumes

| Container path | Required | Purpose |
| --- | --- | --- |
| `/library` | **yes** | Movie files MediaReducer may scan and delete from. |
| `/config` | **yes** | MediaReducer config, logs, cache, IMDb data, and deletion history. |
| `/tautulli` | no | Lets **Auto Detect** read the Tautulli API key *and* the Plex token/URL out of Tautulli's `config.ini`. |
| `/radarr` | no | Lets **Auto Detect** read the Radarr API key out of its `config.xml`. |

The appdata mounts are a setup convenience: Auto Detect reads API keys out of
each service's config file, and everything after that goes over HTTP. Skip them
and type the keys in yourself. There is no Jellyfin mount because Jellyfin
issues API keys from its own dashboard and stores none on disk. Only `/library`
and `/config` are required, and `/library` must be writable for Cleanups to
delete files.

Every URL field defaults to its service's documented port: 8181 Tautulli, 32400
Plex, 8096 Jellyfin, 7878 Radarr. Ports are never read out of appdata, so
if you run a service on a non-standard port, enter its URL yourself.

## Running Outside Docker

You don't have to use Docker. MediaReducer is just `app.py` (the web UI) and
`engine.py` (the worker); run `python3 app.py` and it serves on port 7474 (the
same port the Docker image publishes, so the URL is identical either way). The
paths it expects default to the container mounts above, and the port to 7474,
but each can point anywhere via an environment variable:

| Variable | Default | Points at |
| --- | --- | --- |
| `MEDIAREDUCER_LIBRARY` | `/library` | Your movie library — the only place it can delete from. |
| `MEDIAREDUCER_CONFIG` | `/config/config.json` | Where config and state are written. |
| `MEDIAREDUCER_TAUTULLI_APPDATA` | `/tautulli` | Tautulli's config folder — Auto Detect only. |
| `MEDIAREDUCER_RADARR_APPDATA` | `/radarr` | Radarr's config folder — Auto Detect only. |
| `MEDIAREDUCER_PORT` | `7474` | Port to listen on, if 7474 is already taken. |

For example, point it at a library on a NAS:

```bash
MEDIAREDUCER_LIBRARY=/mnt/tank/media \
MEDIAREDUCER_CONFIG=~/.mediareducer/config.json \
python3 app.py
```

The library path stays the deletion boundary wherever you put it. The appdata
paths are only for auto-detecting API keys, so skip them if you'd rather type
your URLs and keys into the UI.

## Container User & Health

The container runs as root by default, which works on any host. To run as a
specific user instead, set `PUID`/`PGID` in the compose file. That user needs
write access to your movie files and `/config`. On Unraid that's usually
`PUID=99` / `PGID=100` (`nobody:users`); on other Linux hosts use your own
user's ids (`id -u` / `id -g`, commonly `1000`/`1000`). Deletions then happen
with exactly that user's permissions.

The image has a built-in health check, so `docker ps` and the Unraid dashboard
show it as `healthy` rather than just `running`.

## First-Time Setup

On first launch the web UI shows a welcome guide with a quick start and safety
disclaimers. Reopen it any time with the **?** button in the header.

Work through the Configuration tab from top to bottom.

### 1. Scheduler Mode

A fresh install starts on **Paused** with the other modes ghosted. Monitor Only
unlocks once you connect a media server, add a monitored path, arm a space
threshold, and run **Simulate**; it then switches on by itself when that first
scan finishes. Leave the rest of this section alone until then.

**Paused** stops all scheduled activity. Storage numbers still refresh and
Dashboard runs still work.

**Monitor Only** never deletes on its own. A 15-minute refresh keeps the marked
queue and library size current, and a daily Simulate keeps the plan fresh.

**Automatic Cleanup** stays locked until setup is complete and the health check
passes.

Restarts keep Paused and Monitor Only as you left them. Automatic Cleanup drops
back to Monitor Only, since a restart is usually an upgrade or a crash and the
plan on disk may no longer describe the library. **Set to Monitor Only at
startup**, at the bottom of that mode, is what does it; clear it and Automatic
Cleanup comes back armed instead. Either way, a library database that is missing
or badly out of date still demotes, because resuming deletions against a plan
nothing has re-checked is the thing the demote exists to prevent. Run Simulate
and turn it back on.

### 2. Connections

Pick your server software: Plex, Jellyfin, or both if they point at the same
files. **Auto Detect API Keys** fills in what it can from the mounted appdata;
add anything it misses. URL fields can usually stay blank, since MediaReducer
assumes your server's address on its standard port. The API key is the on/off
switch for each service, so leaving an optional one blank just keeps it off.

- Tautulli, required for Plex. Default port 8181.
- Plex token, optional. Unlocks protected Plex collections.
- Jellyfin, required for Jellyfin. Default port 8096.
- Radarr, optional. Default port 7878.

Hit **Check for Errors** to confirm MediaReducer can reach each API and match
your server's paths to real files under `/library`. When something is failing,
the Configuration tab turns red and jumps you to the bad fields.

### 3. Movie Library Paths

Add the `/library` folders MediaReducer is allowed to manage:

```text
Movies
Kids Movies
Holiday Movies
```

An empty list means it manages nothing, and run controls stay locked until at
least one path is saved.

Within those folders it handles the video formats both Plex and Jellyfin
support: `.mkv .mp4 .m4v .avi .mov .wmv .ts .m2ts .mts .webm .mpg .mpeg .m2v
.flv .3gp .3g2 .asf .divx .ogm .ogv`. Everything else is never counted toward
the library size and never deleted, including subtitles, artwork, and
disc-structure rips where one movie spans many files. To change the set:
`mediareducer config set MOVIE_EXTENSIONS=…`, then run a fresh Simulate.

When Plex or Jellyfin is connected this section also shows protected collection
pickers; selected collections are always skipped. When Radarr is connected,
**Optional Radarr cleanup** removes a deleted movie from Radarr so it doesn't
get re-downloaded. It never asks Radarr to delete files.

### 4. Space Thresholds

Space Thresholds unlock after a monitored path is saved.

The panel at the top shows what your storage is doing right now: free space,
used against the total, and how much of that is your library, on the same disk
bar the Dashboard draws with a marker for each armed threshold. Underneath, each
threshold states the figure it needs to reach and how far away that is. The
numbers move as you type, so you can try a value against the real disk before
saving it.

- **Headroom target** — cleanup runs when free space drops below this, and frees
  back up to it. Untick it to turn the trigger off, and drive cleanup from a
  Redline floor or a Library Size Cap instead.
- **Redline emergency floor** — optional. When free space drops below it,
  cleanup runs immediately and frees just enough to get back above. Must sit
  below the Headroom target.
- **Library Size Cap** — optional cap on the total size of your monitored
  movies, cleaned up on the same daily schedule as Headroom.
- **Deletion delay** — how many whole days a movie stays marked before a daily
  cleanup deletes it. Redline and manual runs skip the wait. Each mark keeps the
  delay it was made under, so changing this affects only future marks; use
  **Reset marked movies' delays** to move the existing ones onto the new value.

Anything that could delete more than you expect, or delete right away, opens a
dialog that spells out the consequence first.

### Running without a Headroom target

Every threshold can be off at once. With none armed there is nothing to enforce,
so Monitor Only and Automatic Cleanup stay unavailable and the scheduler rests
at Paused. A fresh install ships this way. With something else armed:

- **Redline only** (a floor, no cap) makes Redline the only thing that ever
  deletes. The deletion delay is retired, and Simulate keeps every eligible
  movie visible in deletion order so you always know what's next.
- **Library Size Cap** keeps the normal daily schedule and the deletion delay,
  just without a free-space target. A Redline floor alongside still handles
  emergencies.

### 5. Notifications

Optional outbound alerts. See [Notifications](#notifications) below.

### 6. Advanced

IMDb dataset settings, display/time settings, log retention, cache tools,
debug mode, the headroom safety cap, and **Reset MediaReducer**.

## Filtering & Scoring

This tab holds every rule that decides *what* can be deleted and *in what
order*, and previews all of it against your full library. Changes show in the
preview as you make them; **Save** keeps them.

### Eligibility filters

- **Minimum age (grace period)** — movies added within this many days are
  skipped.
- **Don't delete unplayed movies** — optional. Skips anything with no play
  history.
- **Maximum IMDb rating** — optional. Movies rated above the cutoff are never
  deleted.
- **Jellyfin favorites** — optional. Skips movies any user favorited.
- **No IMDb data** — always on. Without a rating or votes there isn't enough to
  judge a movie on, so it is left alone.

Protected collections also affect eligibility, and are set on the Configuration
tab.

### Scoring & Ordering

Every eligible movie scores from 0 to 100. Higher means keep, and the lowest
scores delete first. The score blends two things:

- **Watch history** — how often it's been played, how recently, and by how many
  people. A never-watched movie still gets credit for being recently added, and
  **Max staleness** sets how long until it counts as fully stale.
- **IMDb rating** — the rating, weighted by how many votes back it up.

The dial starts at an even split. If your library has little play history, lean
on IMDb; if it has a lot, lean on watch history.

Deletions go in score order, lowest first. **File size optimization** (on by
default) breaks near-ties by deleting bigger files first, so you lose the fewest
movies, and picks the lower-quality copy when a movie is duplicated.

### Library table

The table shows your entire library as of the last run, scored with the settings
on screen. Each row shows its score breakdown, and a filtered movie says exactly
which rule filtered it. The **#** column is the real deletion order: type an
**Over headroom calculator** target and it reorders live, across the whole
library rather than just the visible page.

It is empty until your first Simulate, and every run refreshes it. Ratings come
from the run itself, which downloads the IMDb dataset when scoring needs it and
stops rather than score against stale ratings. There are also sliders for
dialing up a hypothetical movie and watching its score react.

## Dashboard

- **Storage** — free space, used space, and monitored library size, with a ↻
  button to refresh the numbers on demand.
- **Cleanup Targets** — the configured headroom and library cap. When a
  target is currently breached it shows the ~GB a run would free.
- **Last Run** — outcome, trigger, and the current automatic mode.
- **Run Controls** — Simulate, one-time Cleanup (confirms first, naming the ~GB
  it will delete), and Stop.
- **Detailed log** — streams the active or most recent run. Every log opens with
  a **RUN CONTEXT** block (the mode, your targets, current disk and library
  size, and which target is breached), so it's the first thing to read when a
  run didn't do what you expected.
- **Marked & Eligible Deletions** — the standing plan as "X - Y movies": how
  many are marked to delete, and how many are eligible behind them. The full
  ordered list is a click away.
- **Deleted Movie History** — every real deletion, including why it was picked.
  Erasable if you want a clean slate.

Buttons disable while setup is incomplete, a selected API is unhealthy, or a run
is already active. The Cleanup button also ghosts while every space limit is
satisfied, since a run would delete nothing. The tooltip always says which.

### Reading a run

The progress stepper and the log are the same five steps: **Checking**,
**Reading library**, **Scoring**, **Simulating** or **Deleting**, **Done**. Each
stage prints how long it took, and the slow parts that stay quiet (reading a
large library line by line would bury everything else) still report their
timing.

If a step shows a red ×, the dashboard says what failed and what to do about it,
and the log carries the same words on an `ABORT:` line, the technical detail on
`ABORT detail:`, and the stage on `ABORT stage:`. Quote that stage name when
reporting a problem.

Step 1 happens inside the run. It is not the separate pre-check that decides
whether to start one at all; that leaves no run log.

## Run Modes

### Simulate

Scans, scores, and logs exactly what would be deleted, without deleting
anything. It also writes the deletion plan Cleanups work from: the entire
eligible list goes into the queue in deletion order, and only the movies needed
to meet your current targets are **marked**. The rest sit behind them, next in
line if more space is ever needed.

### Cleanup

The manual button deletes to every breached target immediately, with no delay
and no waiting for the daily schedule. It stays ghosted until a Simulate has
shown you the plan for your current settings, so you always see what a run
removes before it can, and while every limit is satisfied.

### Automatic Cleanup

Set Scheduler Mode to **Automatic Cleanup** and MediaReducer runs on its own.
Arming it always requires a Simulate to have seen your library first.

Once armed, the scheduler checks every 15 minutes:

- **Headroom** and **Library Size Cap** cleanups run at most once a day, at the
  **Daily run time** you pick. On a day nothing needs deleting, that slot runs a
  maintenance Simulate instead, so the plan stays fresh and the daily summary
  still arrives.
- **Redline** fires on any check that finds free space below the floor, and
  frees just enough to get back above it. No delay, no waiting for the window.
- **Deletion delay** holds daily deletions for N days. A run marks its
  candidates, and a later daily run deletes each one as it comes due. Marked
  movies sit at the top of the **Marked & Eligible Deletions** list with their
  dates; protecting a movie or changing the rules unmarks it.
- **The marked list keeps itself current.** Every 15 minutes MediaReducer
  re-checks the disk and re-sizes the marked set: it drops movies whose files
  are gone, that you've protected, or that a recent watch pulled out of the
  running, and marks more or fewer as the library changes.
- **The plan stays matched to your config.** Change a scoring, filter or
  threshold setting and the plan is rebuilt in place from the last scan, no
  Simulate needed. Changing your *monitored paths* still needs one, since the
  saved scan only knows the old folders.
- **Time zone** is the clock all of this runs on. Auto follows the container
  clock, which is often UTC, so set your zone if you care when daily runs fire.

After a container restart, Automatic Cleanup drops back to Monitor Only unless
you clear **Set to Monitor Only at startup** and the library database is still
current. Stopping or restarting mid-run is safe: the engine finishes the file it's on,
records it, and shuts down cleanly. And if your thresholds stop being safe while
it's armed, say a bulk copy pushes the cap past the safety percentage, the
scheduler switches back to Monitor Only and tells you why.

## Notifications

MediaReducer can reach out when something happens, using
[Apprise](https://github.com/caronc/apprise) under the hood. Everything lives in
the **Notifications** tab and is off by default.

**Master switch.** Nothing is ever sent until you turn **Enable notifications**
on. With it off, the rest of the section is just settings.

**When to alert.** The three alert types below start on, so switching
notifications on gives you a working setup straight away. Untick anything you
don't want:

- **Run summaries** — the scheduled daily Simulate and every cleanup. One
  message per run: the plan, what it removed and freed, and the current storage
  picture. A manually started Simulate never notifies, since you're already
  watching the dashboard, but a manual Cleanup does.
- **Alert in Monitor Only** (off by default) — Monitor Only never deletes on
  its own, so it stays completely silent unless you tick this. Paused always
  sends nothing. Nothing is lost while a mode is quiet: the first alert after
  you tick this reports what changed meanwhile.
- **Marked between runs** — the 15-minute space check can mark more movies as
  the disk fills. Alerts once per change, never repeatedly.
- **Low space warning** — free space comes within a margin you set of the
  Redline floor. Sent once when the zone is entered, and re-arms only after
  space recovers.

Every alert says which mode produced it, so a date is never ambiguous. Under
Automatic Cleanup the dates read as deletions; under Monitor Only the same
dates read as eligibility, because nothing is deleted automatically.

**What the alerts include.** Both start off, so the alerts you get out of the
box stay short:

- **Movie names** — lists the movies deleted, newly marked, or first in line.
  Long lists are trimmed. Without it you still get the counts and dates.
- **Failed runs and errors** — alerts when a run stops dead, and lists what went
  wrong on runs that finished with errors. Each kind of problem gets its own
  line with a count and a one-line fix, in the same words the log and the
  dashboard use. Off means a failed run sends nothing at all — a run that
  stopped has no result to summarize — and a finished one doesn't mention its
  errors.

**Rate limit.** Each destination gets at most one message every 10 seconds, as a
backstop against anything looping. You should never notice it: the alerts above
fire once per change, and the check behind them runs every 15 minutes. If two do
land together the second is held and delivered a few seconds later, reading
exactly as it would have on time. **Send test notification** obeys the same
limit and tells you how long to wait.

A user-initiated **Stop** is silent, and so is a **Debug Cleanup**.

**Where to send.** Fill in only the services you use:

| Service   | What to enter                                             |
|-----------|-----------------------------------------------------------|
| Discord   | The channel's webhook URL                                 |
| Telegram  | Bot token + chat ID                                       |
| Slack     | An incoming-webhook URL                                   |
| Pushover  | User key + application token                              |
| ntfy      | Topic (and an optional server for self-hosted)            |
| Gotify    | Server URL + application token                            |
| Custom    | Any [Apprise URL](https://github.com/caronc/apprise/wiki) — email via `mailto://`, Matrix, Home Assistant, and 100+ more, one per line |

Use **Send test notification** to fire a test message to whatever you've
entered, before saving. It verifies the wiring without waiting for a run.

Alerts are strictly best-effort: they're delivered *after* a run's work is done
and recorded, on a separate thread with a timeout, so a slow or unreachable
notification service can never delay, block, or abort a cleanup.

## Safety Rules

MediaReducer is intentionally conservative:

- No monitored paths means no scan and no deletion. The scheduler stays fully
  idle and the dashboard reads "Scheduler paused". Adding a path resumes it.
- Every deletion must resolve inside `/library` *and* inside a monitored path.
- A required media API failing during a run aborts it, and a run won't start
  until every selected server is healthy. Radarr is optional: it blocks a real
  Cleanup, so it can forget what you delete, but never a Simulate.
- Protections fail closed. A protected collection that no longer matches
  anything aborts the run rather than running unprotected.
- Plex/Jellyfin identity mismatches are skipped, never deleted.
- Protected collections and filtered movies are hard exclusions, not score
  penalties.
- Editing connection or monitoring settings while Automatic Cleanup is on drops
  it back to Monitor Only, since those change what the scheduler relied on. A
  threshold change keeps it running and just rebuilds the plan, unless the change
  leaves the library over a limit. Settings lock only while a run is active.
- Every run does a fresh safety pre-check before acting. Stop is always safe:
  deletions already made are permanent and recorded, but nothing is left
  half-done. (**Force stop** in Advanced skips that graceful path; see
  [Troubleshooting](#a-run-wont-stop-and-everything-stays-locked).)

## Command Line

Everything the web UI does is also available from a terminal through `cli.py`, a
thin client for the running MediaReducer service, no browser needed. It calls the
same API the GUI uses, so every command goes through the identical validation, safety
gates, and run state; the CLI and the browser can be used interchangeably.

```bash
python3 cli.py status                      # dashboard summary
python3 cli.py connections autodetect       # onboarding: detect + save API keys
python3 cli.py dirs                         # onboarding: browse library folders
python3 cli.py simulate                     # preview deletions (streams progress live)
python3 cli.py cleanup --yes                # delete to your thresholds now
python3 cli.py config get                   # print the whole config (or one KEY)
python3 cli.py config set HEADROOM_GB=500 RUN_MODE=paused
python3 cli.py scoring set SCORE_BALANCE=80 SKIP_UNPLAYED_MOVIES=true
python3 cli.py notify test                  # test the configured notifications
python3 cli.py queue                        # the marked & eligible deletion plan
python3 cli.py history                      # deletion history
python3 cli.py connections check            # probe every selected API
python3 cli.py logs --section summary       # print a run-log section
```

Inside the Docker container the CLI is on the PATH as `mediareducer`, with `mr`
as a short alias, with no path or `python3` needed:

```bash
docker exec -it mediareducer mr status
docker exec -it mediareducer mediareducer config set HEADROOM_GB=500
docker exec -it mediareducer sh        # then: mr simulate, mr queue, …
```

The service URL defaults to `http://127.0.0.1:7474`; override with `--url` or the
`MEDIAREDUCER_URL` environment variable (handy from another machine on your LAN).
Add `--json` to any command for machine-readable output. `simulate`, `cleanup`, and
`debug-cleanup` stream progress until the run finishes (Ctrl-C detaches; the run
keeps going in the background). `cleanup` asks for confirmation unless you pass
`--yes`. Run `python3 cli.py --help`, or `python3 cli.py <command> --help`, for the
full command list.

The CLI needs the service running. It drives that service rather than doing the
work itself, so the scheduler and every gate stay the single source of truth.

## Debug Mode

**Debug mode** (Advanced) adds Debug buttons around the app that dump raw
connection and run state into copyable popups. **Download report** builds a
diagnostic snapshot that's safe to attach to a bug report. Everything
identifying (titles, hosts, keys, paths, IPs) is scrubbed or replaced with
anonymous tokens.

## Persistent Files

These live in the `/config` mount (`MEDIAREDUCER_DATA` on the host).

| File or folder | Purpose |
| --- | --- |
| `config.json` | Saved configuration. |
| `lastrun.log` | Most recent run log, overwritten each run. Every Simulate, Cleanup, and Debug Cleanup archives a copy into `logs/`; quiet Summary refreshes don't. |
| `deleted.log` | Deletion history (erasable from the Dashboard). |
| `logs/` | Archived run logs. |
| `mediareducer.db` | All cached state in one SQLite file: movie metadata, schedule state, storage stats, the Filtering & Scoring library snapshot, and the deletion plan the last Simulate built. Preserved across restarts. Settings changes rebuild the plan in place, so a fresh Simulate is only required when your monitored paths changed or no full scan has run in over two days. |
| `progress.json` | Run progress for the web UI, carried across restarts. Reset by Clear cache or Reset. |
| `last_run_report.json` | Summary of the last completed run, used to build the outbound notification. |
| `title.ratings.tsv` | IMDb ratings dataset. |

**Reset MediaReducer** (Advanced) removes the configuration and state files
but always keeps the deletion history, the run logs and the IMDb dataset. The
final `lastrun.log` is archived into `logs/` on the way out.

You can hand-edit `config.json`, but it's checked against the same rules the
UI uses, so an invalid edit locks things down until it's fixed. Easier to use
the UI and leave the file alone.

## Tests

`tests/run_tests.sh` runs the unit and scoring-parity suites (hermetic, no
network). Add `--integration` for the full run pipeline over real HTTP against
mock servers, or `--e2e` for that plus the browser tests (skipped cleanly if
Playwright isn't installed). See `tests/README.md`.

New to the codebase? [ARCHITECTURE.md](ARCHITECTURE.md) maps how `app.py` and
`engine.py` fit together: the run modes, the request-to-run flow, the
plan-currency and deletion-delay models, and the state files.

## Updating

```bash
cd /mnt/user/appdata/mediareducer
git pull
docker compose up -d --build
```

Settings and logs live in `/config`, so rebuilding the image never removes
them.

## Troubleshooting

### The Configuration tab is highlighted red

A selected connection is failing. Opening the page jumps to the failing
fields; fix the values or the mounts, then **Check for Errors**.

### A Config section is locked

**While a run is active, every section is locked**, on both tabs, with a banner
saying so. That is deliberate: the engine has already read its settings, so an
edit would either be discarded or silently apply to the next run. It unlocks on
its own when the run finishes.

Otherwise, read the warning banner on the locked section. Common causes:

- No server software selected, or credentials missing/failing.
- `/config` cannot be read or written.
- Server-reported media paths do not match files under `/library`.
- No monitored path has been saved.
- Scheduler Mode is Automatic Cleanup.

### A run won't stop, and everything stays locked

**Stop** on the dashboard asks the engine to finish the file it's on and exit,
which is what you want almost always. If a run is somehow wedged and Stop has
had a minute with nothing happening, **Configuration → Advanced → Force stop**
kills it outright. It's the one control that stays available while a run locks
everything else.

It's a blunter tool, so it costs something: the run's log isn't archived, and a
file being deleted at that instant can be missing from the deletion history,
which is exactly what Stop's graceful path exists to prevent. Files already
deleted stay deleted either way.

If Force stop reports that the engine didn't die, it is stuck in a system call
the kernel won't interrupt, nearly always a network share that stopped
answering. No signal can end it in that state; check the mount for your movie
library, and restart the host if it doesn't come back.

### Protected collections do not appear

Collections load only after the relevant API connects. Use **Check for
Errors**, then return to Movie Library Paths. Debug mode can show the raw
collection API output.

### The library table is empty

The Filtering & Scoring table is built by runs: run a Simulate (with a
connected media server and at least one saved monitored path) and it fills in
with every monitored-path movie. It refreshes on every subsequent run.

### Library size looks stale

Use the storage card's ↻ button or clear the cache. Stats also refresh on the
15-minute clock and before every run.

### Radarr did not remove a movie

Radarr cleanup runs only when it is enabled, Radarr is connected, and the
deleted file was the copy in Radarr's detected Plex section. A copy that's
known to live in a different section never touches Radarr. Only when the section
can't be determined does it fall back to checking whether Radarr's own folder was
the one deleted. Redline emergency deletions from the marked
queue skip Radarr cleanup.

## License

MIT. See `LICENSE`.
