"""The baseline every app should hold, checked rather than assumed.

None of this is about facing the internet — MediaReducer is a LAN tool with no
login, and the README says so. It is about the two things that stay true on a
LAN: other people and other containers share the host, and a browser will do
what a web page tells it to.
"""
import os, stat, sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import _tmpout                                     # noqa: E402
_tmpout.config()
import app as A                                    # noqa: E402

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


client = A.app.test_client()
HDR = {"X-MediaReducer": "1"}

# ── Secrets on disk ──────────────────────────────────────────────────────────
# config.json holds a Plex token, three API keys and any notification webhook
# URLs in clear text. The default umask would leave it world-readable, which on
# a NAS means every other container sharing appdata can read them.
cfg = A.load_config()
A.save_config(cfg)
mode = stat.S_IMODE(os.stat(A.CONFIG_PATH).st_mode)
check("config.json is owner-only", mode == 0o600, oct(mode))

# The same writer persists the rest of the shared state, so it is covered too
# rather than being one exception away from leaking the next secret added.
probe = A.CONFIG_PATH.parent / "hardening-probe.json"
A._atomic_write_json(probe, {"x": 1})
pmode = stat.S_IMODE(os.stat(probe).st_mode)
check("...and so is everything else that writer persists", pmode == 0o600, oct(pmode))
probe.unlink(missing_ok=True)

# ── Response headers ─────────────────────────────────────────────────────────
r = client.get("/")
check("pages refuse to be framed", r.headers.get("X-Frame-Options") == "DENY",
      r.headers.get("X-Frame-Options"))
check("...content types are not sniffed",
      r.headers.get("X-Content-Type-Options") == "nosniff",
      r.headers.get("X-Content-Type-Options"))
check("...and no LAN hostname rides out in a Referer",
      r.headers.get("Referrer-Policy") == "no-referrer",
      r.headers.get("Referrer-Policy"))
# Every response, not just the pretty ones: an API reply framed or sniffed is
# the same problem as a page.
rapi = client.get("/api/status")
check("API responses carry them too", rapi.headers.get("X-Frame-Options") == "DENY")

# ── Cross-origin writes ──────────────────────────────────────────────────────
# The custom-header requirement IS the CSRF defence: a form post or a plain
# cross-origin fetch cannot set it, and anything that can has already been
# through a preflight this app never approves.
# Matched on the gate's own words, not the status: an empty body is ALSO a 400
# from the config validator, so a status check alone passes even if the gate is
# gone — which is exactly the regression worth catching.
def _gate_rejected(resp):
    return "X-MediaReducer" in ((resp.get_json() or {}).get("error") or "")

check("a write without the header is refused by the gate",
      _gate_rejected(client.post("/api/config", json={})))
check("...and with it, the request reaches the handler",
      not _gate_rejected(client.post("/api/config", json={}, headers=HDR)))
for path in ("/api/config/reset", "/api/cache/clear", "/api/queue/reset-delays"):
    check(f"the gate covers {path}", _gate_rejected(client.post(path)))
# Reads stay open: the CLI and the dashboard poll them constantly.
check("reads need no header", client.get("/api/status").status_code == 200)

# ── Path traversal ───────────────────────────────────────────────────────────
# Two routes take a path from the caller. Both must stay inside their root, and
# a symlink is the interesting case, since a naive string check passes it.
for attempt in ("../../etc", "/etc", "..%2f..%2fetc", "/library/../../etc"):
    r = client.get("/api/library/browse", query_string={"path": attempt})
    body = r.get_json() or {}
    escaped = r.status_code == 200 and body.get("ok") and any(
        not str(d.get("path", "")).startswith(A.FILESYSTEM_CHECK_PATH)
        for d in body.get("dirs", []))
    check(f"browse refuses to leave the library root: {attempt}", not escaped, body)

r = client.get("/api/logs/archived/" + "..%2f..%2f..%2fetc%2fpasswd")
check("the archived-log route serves nothing outside its directory",
      r.status_code in (400, 403, 404), r.status_code)

# ── The engine is never launched through a shell ─────────────────────────────
src = (pathlib.Path(A.__file__).parent / "app.py").read_text()
check("no shell=True anywhere", "shell=True" not in src)
check("no os.system", "os.system(" not in src)
# Werkzeug's debugger is an interactive Python console for anyone who can reach
# a traceback. It must never be on.
check("the Flask debugger is off", "debug=False" in src and "debug=True" not in src)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
