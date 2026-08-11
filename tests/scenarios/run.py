"""Run the scenario table end to end and check invariants after every run.

  python3 tests/scenarios/run.py [workdir] [name,name,...]

Each scenario boots a real app against a mock Jellyfin serving exactly what the
scenario put on disk, then drives Simulate -> Cleanup -> Simulate -> Cleanup.
After every run: nothing may leave the library root or a monitored path, a
Simulate may delete nothing, a disabled media type may lose nothing, a favorite
may not go while it is protected, everything removed must be in deleted.log,
no run may free or mark far past its own stated target, and no page or
endpoint may 5xx afterwards. Each scenario also states what it
EXPECTS to happen, so "did not crash" is not mistaken for "did the right thing".
"""
import json, os, re, shutil, signal, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import world as W                                    # noqa: E402
from scenarios import SCENARIOS                      # noqa: E402

REPO = HERE.parents[1]
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/mr-scenarios")
ONLY = set(sys.argv[2].split(",")) if len(sys.argv) > 2 else None
APP_PORT = int(os.environ.get("MR_SCENARIO_PORT", "5301"))
JF_PORT = APP_PORT + 1

FAILS, REFUSED = [], []
COV = {"deleting": set(), "movies": 0, "episodes": 0, "runs": 0}


# "The app is busy with its own work", as opposed to a real answer about this
# run. Deliberately not a bare "already": that also matches the no-op.
BUSY_RE = re.compile(r"try again in a moment|already (?:in progress|active)", re.I)

def bad(name, phase, msg, extra=None):
    FAILS.append((name, phase, msg))
    print(f"  FAIL [{name}/{phase}] {msg}" + (f"  {json.dumps(extra, default=str)[:300]}" if extra else ""))


def http(method, path, body=None, timeout=180):
    req = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}{path}", method=method)
    req.add_header("X-MediaReducer", "1")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}
    except Exception as e:
        return 0, {"_err": str(e)}


def snapshot(lib):
    out = {}
    for p in lib.rglob("*"):
        if p.is_file():
            try: out[str(p)] = p.stat().st_size
            except OSError: pass
    return out


def spec_files(spec):
    """How many media files the spec asks to exist, twin copy included."""
    n = len(spec.get("movies") or [])
    for sh in spec.get("shows") or []:
        eps = sum(int(sn.get("episodes", 6)) for sn in sh["seasons"])
        n += eps
        if (spec.get("twin") or ("", "", None))[2] == sh["name"]:
            n += eps          # copytree'd into the second monitored tree
    return n


def check_fixture(name, spec, before):
    """The library the scenario asked for is really on disk before it runs.

    Eleven of these forty scenarios assert only that NOTHING was deleted —
    grace holding everything back, a season too big to be a unit, two shows
    the resolver must refuse to choose between. An EMPTY library satisfies
    every one of those perfectly. So a fixture that quietly built nothing (a
    renamed spec key, a builder that stopped laying files down) would not
    fail: it would turn the strongest safety claims in the suite into passes
    that prove the opposite of what they say.

    Counted rather than merely non-empty, because the failure that matters is
    partial — a show whose seasons never materialised while its movies did."""
    want, got = spec_files(spec), len(before)
    if got != want:
        bad(name, "fixture", f"the spec asks for {want} media files, "
                             f"the library holds {got} — the run below proves nothing")
        return False
    return True


def wait_for_run(limit=300):
    """Wait for a just-POSTed run to START, then to finish.

    Polling straight for idle races the launch: /api/run returns as soon as the
    run is queued, and run_active is not set yet, so the first poll reads "idle"
    and the harness sails on to snapshot a library nothing has touched — then
    collides with the run it never waited for. Every scenario looked like it did
    nothing, which is indistinguishable from a product that does nothing."""
    t0 = time.time()
    started = False
    while time.time() - t0 < limit:
        s, d = http("GET", "/api/status", timeout=30)
        if s == 200:
            if d.get("run_active"):
                started = True
            elif started:
                return d
        time.sleep(0.25)
        # A run short enough to finish inside the first poll is real; give the
        # launch a fair window before deciding nothing ever started.
        if not started and time.time() - t0 > 20:
            return d if s == 200 else None
    return None


def check_run(name, phase, cfg, spec, before, after, base):
    removed = {p: s for p, s in before.items() if p not in after}
    COV["runs"] += 1
    if removed:
        COV["deleting"].add(name)
        COV["movies"] += sum(1 for p in removed if "/Season " not in p)
        COV["episodes"] += sum(1 for p in removed if "/Season " in p)

    lib = str(base / "library")
    monitored = [str(base / "library" / d) for d in cfg["MONITOR_DIRS"]]
    for p in removed:
        if not p.startswith(lib + "/"):
            bad(name, phase, "deleted OUTSIDE the library root", p)
        elif not any(p.startswith(m + "/") for m in monitored):
            bad(name, phase, "deleted outside every monitored path", p)

    if phase.startswith("simulate") and removed:
        bad(name, phase, "a Simulate deleted files", sorted(removed)[:4])
    if removed and not cfg["MOVIE_CLEANUP_ENABLED"]:
        mv = [p for p in removed if "/Season " not in p]
        if mv: bad(name, phase, "movie deleted with movie cleanup OFF", mv[:4])
    if removed and not cfg["TV_CLEANUP_ENABLED"]:
        tv = [p for p in removed if "/Season " in p]
        if tv: bad(name, phase, "episode deleted with TV cleanup OFF", tv[:4])
    if cfg.get("PROTECT_JELLYFIN_FAVORITES"):
        favs = {m["name"] for m in spec.get("movies", []) if m.get("favorite")}
        for p in removed:
            if Path(p).stem in favs:
                bad(name, phase, "deleted a protected favorite", p)

    # How MUCH went, not only what. The delete loop stops as soon as the bytes
    # it has planned cover the run's target, and it tests that BEFORE taking
    # the next candidate — so a run can overshoot by at most the single file it
    # was working on. Nothing checked that ceiling. Freeing exactly the target
    # and emptying the whole library look identical to every assertion above,
    # and both leave a scenario reporting "some movies were removed": deleting
    # the target gate outright went through the entire table unnoticed.
    #
    # Seasons are excluded from THIS check on purpose: they are removed by the
    # app's season executor against its own share of the deficit, not by this
    # loop, so counting them here would measure two targets against one. They
    # get their own ceiling below, against the pool deficit.
    log_p = base / "config" / "lastrun.log"
    run_log = log_p.read_text(errors="replace") if log_p.exists() else ""
    _t = re.search(r"Target to free: ([\d.]+) GB", run_log)
    target = float(_t.group(1)) * 1_000_000_000 if _t else None
    mv = {p: s for p, s in removed.items() if "/Season " not in p}
    if target is not None and mv:
        freed, biggest = sum(mv.values()), max(mv.values())
        if freed > target + biggest:
            bad(name, phase, "freed far past the run's target",
                f"{freed / 1e9:.2f} GB freed against a {target / 1e9:.2f} GB "
                f"target (largest single file {biggest / 1e9:.2f} GB)")
    # The SEASON side's own ceiling. The movie check above measures against the
    # engine's target, which already has the season share subtracted, so it can
    # say nothing about how much TV went — and the exclusion note below used to
    # leave the season side with no upper bound under test at all. A season is
    # the biggest deletion unit there is, so that is the side where an
    # unbounded overshoot costs the most.
    #
    # Every number here is measured, not reported: the deficit from the cap in
    # the config against the monitored bytes on disk BEFORE the run, and the
    # season sizes from the files that actually disappeared. Reading the app's
    # own "pool deficit" line instead would let a mutation that zeroes that
    # figure make the ceiling vacuous.
    seasons_gone = {p: s for p, s in removed.items() if "/Season " in p}
    cap_gb = cfg.get("MAX_LIBRARY_GB")
    if seasons_gone and cap_gb:
        monitored_before = sum(
            s for p, s in before.items()
            if any(p.startswith(m + "/") for m in monitored))
        deficit = max(0, monitored_before - float(cap_gb) * 1_000_000_000)
        by_season: dict = {}
        for p, s in seasons_gone.items():
            by_season[str(Path(p).parent)] = by_season.get(str(Path(p).parent), 0) + s
        freed_tv, biggest = sum(seasons_gone.values()), max(by_season.values())
        # One whole season of slack: the walk stops at the first unit covering
        # what is left, and that unit can be a season.
        if freed_tv > deficit + biggest:
            bad(name, phase, "seasons freed far past the pool deficit",
                f"{freed_tv / 1e9:.2f} GB of seasons against a {deficit / 1e9:.2f} GB "
                f"deficit (largest season removed {biggest / 1e9:.2f} GB)")

    # The same ceiling on the marked side, read from the QUEUE rather than
    # from the run's own summary line. The summary prints a figure the loop
    # computes from the very counter that decides when to stop, so a mutation
    # that stops the counter reports 0.0 GB marked while marking the whole
    # library — the log agreed with itself and the check saw nothing. The queue
    # rows and the sizes on disk are the two things the loop cannot restate.
    dbp = base / "config" / "mediareducer.db"
    if target is not None and dbp.exists():
        import sqlite3
        with sqlite3.connect(dbp) as _c:
            marks = [r[0] for r in _c.execute(
                "SELECT path FROM queue WHERE marked_at IS NOT NULL")]
        sizes = [before[p] for p in marks if p in before]
        if sizes and sum(sizes) > target + max(before.values(), default=0):
            bad(name, phase, "marked far past the run's target",
                f"{len(sizes)} movie(s) / {sum(sizes) / 1e9:.2f} GB marked "
                f"against a {target / 1e9:.2f} GB target")

    # What the run SAYS it freed has to be what actually left the disk. The
    # figure feeds the dashboard, the run summary and the notification, and it
    # is accumulated separately from the deletions themselves — so it can drift
    # from them without a single file being wrongly removed, and nothing else
    # here would notice.
    _sf = re.search(r"Space freed: ([\d.]+) GB", run_log)
    _dc = re.search(r"Deleted: (\d+)", run_log)
    if _sf and mv:
        claimed, real = float(_sf.group(1)) * 1e9, sum(mv.values())
        # Rounded to 2 dp in the log, and seasons are counted in it too.
        if abs(claimed - real) > 0.02e9 and not any(
                "/Season " in p for p in removed):
            bad(name, phase, "the run misreported how much it freed",
                f"said {claimed / 1e9:.2f} GB, actually {real / 1e9:.2f} GB")
    if _dc and not any("/Season " in p for p in removed):
        if int(_dc.group(1)) != len(mv):
            bad(name, phase, "the run misreported how many files it deleted",
                f"said {_dc.group(1)}, actually {len(mv)}")

    dl = base / "config" / "deleted.log"
    logged = dl.read_text(errors="replace") if dl.exists() else ""
    for p in removed:
        if Path(p).name not in logged:
            bad(name, phase, "deleted file missing from deleted.log", p)
    for p in removed:
        d = Path(p).parent
        if d.name.startswith("Season ") and d.exists() and not any(d.iterdir()):
            bad(name, phase, "empty season directory left behind", str(d))
    return removed


def run_scenario(name, spec, expect):
    base = ROOT / name
    if base.exists(): shutil.rmtree(base)
    base.mkdir(parents=True)
    W.build(base, spec)
    # A second monitored tree holding the SAME series folder name: the case the
    # scope resolver must refuse rather than guess between.
    twin = spec.get("twin")
    if twin:
        src, dst, show = twin
        shutil.copytree(base / "library" / src / show, base / "library" / dst / show)

    cfgp = base / "config" / "config.json"
    cfg = json.loads(cfgp.read_text())
    cfg["JELLYFIN_URL"] = f"http://127.0.0.1:{JF_PORT}"
    cfgp.write_text(json.dumps(cfg, indent=2))
    shutil.copy(base / "ratings" / "title.ratings.tsv", base / "config" / "title.ratings.tsv")

    jf = subprocess.Popen([sys.executable, str(HERE / "mock_jf.py"), str(JF_PORT),
                           str(base / "world.json")],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ, MEDIAREDUCER_CONFIG=str(cfgp),
               MEDIAREDUCER_LIBRARY=str(base / "library"),
               MEDIAREDUCER_PORT=str(APP_PORT), OUTPUT_DIR=str(base / "config"),
               NO_PROXY="127.0.0.1,localhost")
    applog = open(base / "app.log", "wb")
    app = subprocess.Popen([sys.executable, "-u", str(REPO / "app.py")], env=env,
                           cwd=str(REPO), stdout=applog, stderr=subprocess.STDOUT)
    gone = {"movies": 0, "episodes": 0}
    gone_paths: list = []   # every deleted path, across all phases, for gone_has/gone_not
    try:
        # Ready means the STARTUP storage refresh has finished, not just that the
        # port answers: /api/status replies a moment before that clears, and a run
        # fired inside the window is refused with "a background status refresh is
        # finishing". run_tests.sh waits the same way, for the same reason.
        for _ in range(240):
            s, d = http("GET", "/api/status", timeout=5)
            if s == 200 and not d.get("summary_active"): break
            time.sleep(0.5)
        else:
            bad(name, "boot", "app never became ready"); return

        lib = base / "library"
        before = snapshot(lib)
        if not check_fixture(name, spec, before):
            return
        # Simulate then Cleanup is enough to judge one factor. The repeat cycle
        # costs as much again and only pays off where a SECOND pass can differ —
        # a stale plan, a re-mark, an already-satisfied target — so the scenarios
        # that care ask for it rather than every scenario paying for it.
        phases = [("simulate", "debug_sim"), ("cleanup", "headroom")]
        if expect.get("repeat"):
            phases += [("simulate2", "debug_sim"), ("cleanup2", "headroom")]
        # A scheduled scenario is ONLY the scheduled run. Leaving the manual
        # phases in place afterwards deletes everything the scheduled run
        # correctly held back, and the scenario then fails its own expectation
        # for a reason that has nothing to do with what it is testing.
        if expect.get("scheduled") or expect.get("direct_manual"):
            phases = []

        # A cleanup fired from the API is a MANUAL one, and manual deliberately
        # ignores the deletion delay — you pressed the button, you meant now.
        # Every phase above is therefore manual, which left the delayed path
        # unexercised: across 29 scenarios the engine never once logged
        # "MARKED for deletion", so nothing was checking the mechanism the
        # whole safety story rests on. A scheduled run is the engine started
        # without MEDIAREDUCER_MANUAL, so it is run directly here.
        # direct_manual runs the engine as a MANUAL cleanup without going
        # through /api/run. The app refuses a manual cleanup when nothing is
        # over its limits — "Space limits are already satisfied" — so its own
        # within-limits branch is unreachable from the API by design. It is
        # still the last thing standing between a disagreement (the app reading
        # cached stats, the engine recomputing) and a deletion nobody wanted,
        # which is exactly the kind of second check worth having a test on.
        if expect.get("scheduled") or expect.get("direct_manual"):
            # The app marks today's daily window used at startup, so a
            # scheduled cleanup would refuse with "skipping until tomorrow"
            # before reaching anything worth testing. Clear the stamp.
            import sqlite3
            _db = base / "config" / "mediareducer.db"
            # Left in place for scenarios that are ABOUT the window already
            # being spent today; cleared otherwise, or the run refuses before
            # reaching whatever the scenario is really testing.
            if _db.exists() and not expect.get("day_used"):
                with sqlite3.connect(_db) as _c:
                    _c.execute("DELETE FROM meta WHERE key='last_cleanup_date'")
            _phase = "manual-direct" if expect.get("direct_manual") else "scheduled"
            env2 = dict(env, MEDIAREDUCER_MODE_OVERRIDE="headroom")
            if expect.get("waits"):
                # The engine's clock moves to the same synthetic noon the
                # config's run time was computed on — see world.wait_clock.
                env2["TZ"] = W.wait_clock()[0]
            if expect.get("direct_manual"):
                env2["MEDIAREDUCER_MANUAL"] = "1"
            else:
                env2.pop("MEDIAREDUCER_MANUAL", None)
            subprocess.run([sys.executable, "-u", str(REPO / "engine.py")],
                           env=env2, cwd=str(REPO), stdout=applog,
                           stderr=subprocess.STDOUT, timeout=300)
            after = snapshot(lib)
            _rm = check_run(name, _phase, cfg, spec, before, after, base)
            gone["movies"] += sum(1 for q in _rm if "/Season " not in q)
            gone["episodes"] += sum(1 for q in _rm if "/Season " in q)
            gone_paths.extend(_rm)
            before = after
            # "Deleted nothing" is also what a refused run looks like, so the
            # marking has to be shown positively or the scenario proves the
            # opposite of what it claims.
            # Which refusal it was, not merely that it refused. Several of
            # these paths delete nothing and are otherwise indistinguishable.
            if expect.get("log_has"):
                _log = (base / "config" / "lastrun.log").read_text(errors="replace")
                if expect["log_has"] not in _log:
                    bad(name, _phase, f"expected the run to say {expect['log_has']!r}",
                        _log[-300:])
            if expect.get("waits"):
                _log = (base / "config" / "lastrun.log").read_text(errors="replace")
                # Matched case-insensitively: the progress message capitalises
                # it, the log line has it mid-sentence.
                if "scheduled run time" not in _log.lower():
                    bad(name, _phase, "an early scheduled run did not wait", _log[-300:])
            if expect.get("marks"):
                _log = (base / "config" / "lastrun.log").read_text(errors="replace")
                if "MARKED for deletion" not in _log:
                    bad(name, _phase, "a delayed scheduled run marked nothing",
                        _log[-300:])

            # The other half of the mechanism: a mark that has aged past its
            # delay must actually delete on the next scheduled run. Marking and
            # never collecting would look identical in every check above, and
            # would mean a library that is never cleaned.
            if expect.get("marks_due"):
                with sqlite3.connect(_db) as _c:
                    _c.execute("UPDATE queue SET marked_at = marked_at - ?",
                               (float(cfg.get("DELETE_DELAY_DAYS", 1) + 2) * 86400,))
                    _c.execute("DELETE FROM meta WHERE key='last_cleanup_date'")
                subprocess.run([sys.executable, "-u", str(REPO / "engine.py")],
                               env=env2, cwd=str(REPO), stdout=applog,
                               stderr=subprocess.STDOUT, timeout=300)
                after = snapshot(lib)
                _rm = check_run(name, "marks-due", cfg, spec, before, after, base)
                gone["movies"] += sum(1 for q in _rm if "/Season " not in q)
                gone["episodes"] += sum(1 for q in _rm if "/Season " in q)
                gone_paths.extend(_rm)
                before = after

        for phase, mode in phases:
            # "Try again in a moment" refusals are the app being busy with its
            # own background refresh, not a verdict about this scenario. Retried
            # until it clears, because a scenario that silently loses a run
            # deletes less and then fails its own expectation — which turns a
            # timing race into a red build on an unrelated change, and makes two
            # runs of the same commit disagree.
            for _ in range(20):
                s, d = http("POST", "/api/run", {"mode": mode})
                msg = d.get("message") or ""
                # Matched on the message, not the status. api_run answers this
                # refusal with a bare jsonify(), so it arrives 200 with
                # started:false — the old condition required 400 or 409 and so
                # never retried the one case it was written for. The pattern is
                # exact rather than a bare "already" because "Space limits are
                # already satisfied" is a legitimate no-op that must NOT be
                # retried twenty times before being recorded.
                if not BUSY_RE.search(msg):
                    break
                time.sleep(1.5)
            if s == 400:
                REFUSED.append((name, phase, (d.get("message") or "")[:70])); continue
            if s not in (200, 202, 409):
                bad(name, phase, f"run POST returned {s}", d); continue
            # 200 does NOT mean a run began: the app answers 200 with
            # started:false for a no-op ("Space limits are already satisfied").
            # Reading only the status made every such scenario look like the
            # product had quietly done nothing, and the NEXT phase then failed
            # for want of the plan the run never wrote.
            if s == 200 and d.get("started") is False:
                REFUSED.append((name, phase, (d.get("message") or "no-op")[:70])); continue
            if wait_for_run() is None:
                bad(name, phase, "run never went idle"); return
            after = snapshot(lib)
            rm = check_run(name, phase, cfg, spec, before, after, base)
            gone["movies"] += sum(1 for p in rm if "/Season " not in p)
            gone["episodes"] += sum(1 for p in rm if "/Season " in p)
            gone_paths.extend(rm)
            before = after

        for path in ("/api/status", "/api/config", "/api/queue", "/api/history",
                     "/api/library/table", "/api/logs/last", "/api/logs/deleted",
                     "/api/storage", "/api/scoring"):
            s, d = http("GET", path, timeout=60)
            if s >= 500: bad(name, "probe", f"{path} -> {s}", d)
        for page in ("/", "/config", "/explorer"):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}{page}", timeout=60) as r:
                    if r.status >= 500: bad(name, "probe", f"{page} -> {r.status}")
            except Exception as e:
                bad(name, "probe", f"{page} raised {e}")

        # What the scenario SAID should happen.
        for kind in ("movies", "episodes"):
            want = expect.get(kind)
            if want == "some" and gone[kind] == 0:
                bad(name, "expect", f"expected {kind} to be deleted, none were")
            if want == "none" and gone[kind] > 0:
                bad(name, "expect", f"expected NO {kind} deleted, {gone[kind]} were")
        # WHICH paths, for the rules whose whole point is the selection. A
        # substring per expectation: gone_has must match something deleted,
        # gone_not must match nothing — the deletion that would prove a
        # season rule leaked.
        for frag in expect.get("gone_has", ()):
            if not any(frag in p for p in gone_paths):
                bad(name, "expect", f"expected a deletion matching {frag!r}, none happened",
                    sorted(gone_paths)[:6])
        for frag in expect.get("gone_not", ()):
            hits = [p for p in gone_paths if frag in p]
            if hits:
                bad(name, "expect", f"{frag!r} must not be deleted, but was", hits[:4])
        if expect.get("refused") and not any(r[0] == name for r in REFUSED):
            bad(name, "expect", "expected the app to refuse this run, it did not")
    finally:
        for p in (app, jf):
            try: p.send_signal(signal.SIGTERM); p.wait(timeout=15)
            except Exception:
                try: p.kill()
                except Exception: pass
        applog.close()
        log = (base / "app.log").read_text(errors="replace")
        if "Traceback (most recent call last)" in log:
            i = log.find("Traceback (most recent call last)")
            bad(name, "log", "unhandled exception in the app", log[i:i + 260])
        # Kept only when something failed, so a green run leaves no debris. The
        # override exists for diffing whole runs against each other: lastrun.log
        # is the engine's own transcript of what it decided, which is the closest
        # thing there is to a behavioural fingerprint of a run.
        if not FAILS and not os.environ.get("MR_SCENARIO_KEEP"):
            shutil.rmtree(base, ignore_errors=True)


picked = [(n, s, e) for n, s, e in SCENARIOS if not ONLY or n in ONLY]
print(f"{len(picked)} scenarios\n")
for n, s, e in picked:
    print(f"  {n} ...", flush=True)
    try:
        run_scenario(n, s, e)
    except Exception as exc:
        import traceback; traceback.print_exc()
        bad(n, "harness", f"harness raised {exc}")

print(f"\ncoverage: {len(COV['deleting'])}/{len(picked)} scenarios deleted; "
      f"{COV['movies']} movie + {COV['episodes']} episode files over {COV['runs']} runs")
if REFUSED:
    import collections
    print(f"{len(REFUSED)} runs refused on a safety rule:")
    for m, c in collections.Counter(r[2] for r in REFUSED).most_common():
        print(f"   {c}x {m}")
print(f"\n==== {len(FAILS)} findings ====")
for f in FAILS: print(f"  [{f[0]}/{f[1]}] {f[2]}")
sys.exit(1 if FAILS else 0)
