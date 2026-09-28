"""A connection that fails says how, and where — never with the key.

Check for Errors reported every failure as "<service> did not connect. Check
the URL and API key.": a wrong port, a stopped server and a bad key all read
the same. Auto Detect fills keys in from appdata but never ports, so anyone
whose Tautulli is off the default port met this message right after a CORRECT
key, and it sent them to the key first. (Found in the test lab, where every
service runs on its standard port plus 10000.)

Pinned against real sockets on localhost: a refused port, an HTTP server that
answers 401 / 404 / not-JSON, and one that never answers. Each names the host
and port and what to check — and the probe URL's key never reaches the text,
since Tautulli's and Plex's probes carry it in the query string and some
errors quote the URL they failed on.
"""
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-probe.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
# Straight to the local sockets: a proxy from the environment would answer
# for them, and every case would read as the proxy's reply.
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
Path(_OUT, "config.json").write_text(json.dumps({"OUTPUT_DIR": _OUT}), encoding="utf-8")
import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


KEY = "SECRETKEY-do-not-show-123"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = {"/401": (401, b'{"error": "Invalid apikey"}'),
                      "/404": (404, b"not here"),
                      "/html": (200, b"<html>some other web app</html>")}.get(
            self.path.split("?")[0], (200, b"{}"))
        self.send_response(code); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
HTTP = f"127.0.0.1:{srv.server_address[1]}"

silent = socket.socket(); silent.bind(("127.0.0.1", 0)); silent.listen(1)   # accepts, never answers
SILENT = f"127.0.0.1:{silent.getsockname()[1]}"
REFUSED = f"127.0.0.1:{free_port()}"


def probe(url, **kw):
    good, msg = A._probe_json(url, timeout=1, **kw)
    return good, msg


cases = [
    ("a refused port", f"http://{REFUSED}/api/v2?apikey={KEY}", {},
     [f"nothing is listening at {REFUSED}", "connection refused", "check the URL and port"]),
    ("a rejected key", f"http://{HTTP}/401?apikey={KEY}", {},
     [f"{HTTP} refused the API key (HTTP 401)", "check the API key"]),
    ("a rejected Plex token", f"http://{HTTP}/401?X-Plex-Token={KEY}", {"key_word": "token"},
     [f"{HTTP} refused the token (HTTP 401)", "check the token"]),
    ("the wrong service on that port (404)", f"http://{HTTP}/404", {},
     [f"{HTTP} answered, but not as this service (HTTP 404)", "check the URL and port"]),
    ("a web page where an API was expected", f"http://{HTTP}/html?apikey={KEY}", {},
     [f"{HTTP} answered, but not with this service's API", "check the URL and port"]),
    ("a server that never answers", f"http://{SILENT}/api?apikey={KEY}", {},
     [f"{SILENT} did not answer in time", "that the server is running"]),
    ("a host that does not exist", f"http://no-such-host.invalid:18181/api?apikey={KEY}", {},
     ["no-such-host.invalid could not be found", "check the URL"]),
]
for label, url, kw, want in cases:
    good, msg = probe(url, **kw)
    check(f"{label}: fails and says so", good is False and all(w in msg for w in want), msg)
    check(f"{label}: without the key in the message", KEY not in msg, msg)

good, msg = probe(f"http://{HTTP}/ok")
check("a server that answers JSON is reachable", good is True and msg == "reachable", msg)

# ── The Configuration check carries it into its own message ───────────────
A.app.config["TESTING"] = True
A.CONFIG_PATH.write_text(json.dumps({
    "OUTPUT_DIR": _OUT, "USE_PLEX": True, "USE_JELLYFIN": False,
    "TAUTULLI_URL": f"http://{REFUSED}", "TAUTULLI_API_KEY": KEY,
    "MONITOR_DIRS": [], "HEADROOM_GB": 0}), encoding="utf-8")
with A._config_memo_lock:
    A._config_memo["key"] = object()
body = A.app.test_client().post("/api/config/check", json={}, headers={"X-MediaReducer": "1"}).get_json()
errs = [e for e in (body.get("errors") or []) if "Tautulli" in e]
check("Check for Errors names the host, the port and the cause",
      any(f"Tautulli did not connect: nothing is listening at {REFUSED}" in e for e in errs), errs)
check("...and never the key", KEY not in json.dumps(body))

srv.shutdown(); silent.close()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
