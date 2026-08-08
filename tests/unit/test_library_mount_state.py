"""What the health check says when /library is not there.

Three states that must not collapse into one message. Every path sample fails
when the library root is missing, so a check that only knows "paths don't line
up" reports "Plex paths do not line up with files under /library" and sends
people looking for a prefix mismatch that does not exist. The mount is simply
absent, and the message has to say so.

Empty is worth separating too: on a NAS that is usually a share or disk that has
not come up yet, not a library with no films in it.

Both states still BLOCK, which is the part that matters. Nothing about clearer
wording is allowed to loosen the gate.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {extra}"))
    ok = ok and cond


def health_for(library: str, sample: str = '/data/movies/X (2020)/X.mkv') -> dict:
    """A fresh interpreter per case: the library root is read at import.

    The probes are stubbed so Tautulli reads as CONNECTED. That matters: the
    library message replaces the path-alignment complaint exactly where that
    check runs, which is only with a connected media server. Probing for real
    here would leave Tautulli down, the check skipped, and the test passing
    while exercising nothing.
    """
    out = tempfile.mkdtemp(prefix="mr-libstate.")
    cfg = Path(out, "config.json")
    cfg.write_text(json.dumps({"OUTPUT_DIR": out, "USE_PLEX": True,
                               "TAUTULLI_API_KEY": "k", "TAUTULLI_URL": "http://127.0.0.1:1"}))
    env = dict(os.environ, MEDIAREDUCER_CONFIG=str(cfg), MEDIAREDUCER_LIBRARY=library)
    env.pop("MEDIAREDUCER_PORT", None)
    # Tautulli reads as connected, and the sample is the path it would report.
    code = (
        "import sys; sys.path.insert(0, " + repr(str(ROOT)) + "); import app, json;"
        "app._prefetch_connection_probes = lambda conn, **k: "
        "{'tautulli': (True, ''), 'tautulli_paths': [" + repr(sample) + "]};"
        "h = app._connection_health_state(probe=True);"
        "print('@@' + json.dumps({'errors': h['errors'],"
        " 'blocked': h['media_path_blocker']}))"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    line = [l for l in r.stdout.splitlines() if l.startswith("@@")]
    if not line:
        raise AssertionError(r.stderr[-400:])
    return json.loads(line[-1][2:])


BASE = tempfile.mkdtemp(prefix="mr-libroots.")
missing = str(Path(BASE, "not-created"))
empty = Path(BASE, "empty"); empty.mkdir()
real = Path(BASE, "real", "movies"); real.mkdir(parents=True)
(real / "A Movie (2020).mkv").write_text("x")

# ── Missing: name the mount, not the path prefixes ───────────────────────────
h = health_for(missing)
joined = " ".join(h["errors"]).lower()
check("missing library blocks", h["blocked"] is True, h["errors"])
check("missing library says no library is mounted",
      "no media library at" in joined, h["errors"])
check("...and does NOT blame path alignment",
      "line up" not in joined, h["errors"])

# ── Empty: a mount that has not come up looks exactly like this ──────────────
h = health_for(str(empty))
joined = " ".join(h["errors"]).lower()
check("empty library blocks", h["blocked"] is True, h["errors"])
check("empty library says mounted but empty",
      "mounted but empty" in joined, h["errors"])
check("...and does NOT blame path alignment",
      "line up" not in joined, h["errors"])
check("...and does not claim the mount is absent",
      "no media library at" not in joined, h["errors"])

# ── Present: no library complaint at all ─────────────────────────────────────
h = health_for(str(Path(BASE, "real")), sample="/data/movies/A Movie (2020).mkv")
joined = " ".join(h["errors"]).lower()
check("a real library raises no mount error",
      "no media library at" not in joined and "mounted but empty" not in joined, h["errors"])
check("...and does not block on the library",
      h["blocked"] is False, h["errors"])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
