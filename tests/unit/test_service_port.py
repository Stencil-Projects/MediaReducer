"""Which port the service listens on.

Under Docker the host side is remapped (-p 8080:7474) and nothing here changes.
This is for running outside a container, where 7474 may already be taken and
there is no host side to move; it used to be hardcoded, so the answer was "edit
app.py".

Three things worth holding still:

  * the default stays 7474, because the README, the Dockerfile's EXPOSE, the
    compose file, the Unraid template and every URL default all say 7474
  * a bad value stops startup instead of falling back, since a service quietly
    listening somewhere other than where it was told is debugged from the far
    end, wondering why nothing answers
  * cli.py follows it, or moving the port leaves the CLI talking to nothing
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" — {extra}"))
    ok = ok and cond


def _run(code, port=None):
    """Fresh interpreter per case: both modules read the environment at import."""
    env = dict(os.environ)
    env.pop("MEDIAREDUCER_PORT", None)
    env.pop("MEDIAREDUCER_URL", None)
    if port is not None:
        env["MEDIAREDUCER_PORT"] = port
    return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); {code}"],
                          capture_output=True, text=True, env=env)


# ── The port the app binds ───────────────────────────────────────────────────
PORT = "import app; print(app.service_port())"

r = _run(PORT)
check("unset means 7474", r.stdout.strip().endswith("7474"), r.stdout + r.stderr)

for blank in ("", "   "):
    r = _run(PORT, blank)
    check(f"blank ({blank!r}) means 7474", r.stdout.strip().endswith("7474"), r.stdout + r.stderr)

for value in ("8080", "1", "65535"):
    r = _run(PORT, value)
    check(f"{value} is honored", r.stdout.strip().endswith(value), r.stdout + r.stderr)

# Refused, not quietly ignored. Each names the offending value so the message is
# actionable without reading this file.
for bad in ("abc", "80.5", "0", "-1", "65536", "70000"):
    r = _run(PORT, bad)
    check(f"{bad!r} is refused", r.returncode != 0 and bad in r.stderr,
          f"rc={r.returncode} err={r.stderr.strip()[:80]}")


# ── The CLI drives the same service, so it has to follow ─────────────────────
URL = "import cli; print(cli.DEFAULT_URL)"

r = _run(URL)
check("CLI defaults to 7474", r.stdout.strip().endswith("127.0.0.1:7474"), r.stdout + r.stderr)

r = _run(URL, "8099")
check("CLI follows MEDIAREDUCER_PORT", r.stdout.strip().endswith("127.0.0.1:8099"), r.stdout + r.stderr)

r = _run(URL, "")
check("CLI treats a blank port as the default", r.stdout.strip().endswith("127.0.0.1:7474"), r.stdout + r.stderr)

# A bad port is REPORTED and ignored here, not fatal as it is in app.py.
# DEFAULT_URL is only argparse's default, so exiting would also break
# `--url http://nas:9000`, which is exactly what someone reaches for when their
# environment is wrong.
for bad in ("abc", "0", "-1", "99999"):
    r = _run(URL, bad)
    check(f"CLI reports and ignores {bad!r}",
          r.returncode == 0
          and r.stdout.strip().endswith("127.0.0.1:7474")
          and "MEDIAREDUCER_PORT" in r.stderr,
          f"rc={r.returncode} out={r.stdout.strip()[-30:]} err={r.stderr.strip()[:60]}")

# MEDIAREDUCER_URL is the more specific setting and stays in charge.
env = dict(os.environ)
env["MEDIAREDUCER_PORT"] = "abc"          # garbage, and still irrelevant
env["MEDIAREDUCER_URL"] = "http://nas.local:9999"
r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); {URL}"],
                   capture_output=True, text=True, env=env)
check("MEDIAREDUCER_URL still outranks the port",
      r.stdout.strip().endswith("nas.local:9999"), r.stdout + r.stderr)


# ── The Dockerfile probe reads the same variable ─────────────────────────────
# A container whose port moved would otherwise run fine and report unhealthy.
dockerfile = (ROOT / "Dockerfile").read_text()
check("the healthcheck is not pinned to a literal 7474 URL",
      "127.0.0.1:7474/api/status" not in dockerfile)
check("the healthcheck reads MEDIAREDUCER_PORT", "MEDIAREDUCER_PORT" in dockerfile)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
