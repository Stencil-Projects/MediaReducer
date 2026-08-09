"""Run the scenario table end to end and check invariants after every run.

  python3 tests/fuzz/run.py [workdir] [name,name,...]

Each scenario boots a real app against a mock Jellyfin serving exactly what the
scenario put on disk, then drives Simulate -> Cleanup -> Simulate -> Cleanup.
After every run: nothing may leave the library root or a monitored path, a
Simulate may delete nothing, a disabled media type may lose nothing, a favorite
may not go while it is protected, everything removed must be in deleted.log,
and no page or endpoint may 5xx afterwards. Each scenario also states what it
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
        # Simulate then Cleanup is enough to judge one factor. The repeat cycle
        # costs as much again and only pays off where a SECOND pass can differ —
        # a stale plan, a re-mark, an already-satisfied target — so the scenarios
        # that care ask for it rather than every scenario paying for it.
        phases = [("simulate", "debug_sim"), ("cleanup", "headroom")]
        if expect.get("repeat"):
            phases += [("simulate2", "debug_sim"), ("cleanup2", "headroom")]
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
        if not FAILS:
            shutil.rmtree(base, ignore_errors=True)   # keep only what failed


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
