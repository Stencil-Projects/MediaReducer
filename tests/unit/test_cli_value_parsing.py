"""KEY=VALUE typing, and what the CLI does with a reply it did not expect.

`config set HEADROOM_GB=500` has to decide, from a shell string, whether the
user meant the number 500 or the text "500". The answer goes straight into the
config file the server writes, so the wrong guess is not a display problem: a
threshold stored as a string is a threshold the validator rejects or, worse,
compares as text. The same function decides that MONITOR_DIRS is a list, that
"true" is a boolean, and that a time like 03:30 is none of those.

The transport half is here for the same reason. When the server answers with
something other than the JSON the CLI hoped for — an error body, a proxy's
HTML, a timeout — the useful behaviour is a message naming what happened and a
non-zero exit. A traceback is not a message, and a swallowed error body is how
`Save failed (400):` ends up with nothing after the colon.

Nothing here touches the network: the opener is stubbed at the socket boundary.
"""
import io
import json
import sys
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import cli  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


BASE = "http://127.0.0.1:7575"


# ── Numbers stay numbers ───────────────────────────────────────────────────
# The config file is JSON, so a quoted threshold is a different value to the
# server than an unquoted one.
v = cli.parse_value("HEADROOM_GB", "500")
check("a whole number parses as an int", v == 500 and isinstance(v, int), repr(v))

v = cli.parse_value("MAX_IMDB_RATING", "6.5")
check("a decimal parses as a float", v == 6.5 and isinstance(v, float), repr(v))

v = cli.parse_value("HEADROOM_GB", "  500  ")
check("surrounding whitespace is not part of the value", v == 500, repr(v))

v = cli.parse_value("SOME_KEY", "1e3")
check("exponent notation parses as a float", v == 1000.0, repr(v))

v = cli.parse_value("HEADROOM_GB", "-50")
check("a negative number keeps its sign for the validator to reject",
      v == -50 and isinstance(v, int), repr(v))


# ── Booleans and unset ─────────────────────────────────────────────────────
for raw in ("true", "True", "TRUE"):
    v = cli.parse_value("SKIP_UNPLAYED_MOVIES", raw)
    check(f"{raw!r} is the boolean true", v is True, repr(v))
for raw in ("false", "False"):
    v = cli.parse_value("SKIP_UNPLAYED_MOVIES", raw)
    check(f"{raw!r} is the boolean false", v is False, repr(v))
for raw in ("null", "none", "None", ""):
    v = cli.parse_value("REDLINE_GB", raw)
    check(f"{raw!r} clears the value", v is None, repr(v))

# The string "0" must survive as a number: several settings use 0 as "off",
# and reading it as false would be the same value by accident and the wrong
# type on purpose.
v = cli.parse_value("REDLINE_GB", "0")
check("0 stays the number zero, not false", v == 0 and v is not False, repr(v))


# ── List keys ──────────────────────────────────────────────────────────────
# MONITOR_DIRS is the one people type by hand most often, and a string where
# the server wants a list means nothing is monitored.
v = cli.parse_value("MONITOR_DIRS", "/movies,/movies4k")
check("a comma list becomes a list", v == ["/movies", "/movies4k"], repr(v))

v = cli.parse_value("MONITOR_DIRS", " /movies , /movies4k ")
check("...with each entry trimmed", v == ["/movies", "/movies4k"], repr(v))

v = cli.parse_value("MONITOR_DIRS", "/movies,,")
check("...and empty entries dropped, so a trailing comma is harmless",
      v == ["/movies"], repr(v))

v = cli.parse_value("MONITOR_DIRS", '["/movies", "/tv"]')
check("explicit JSON is accepted for a list key", v == ["/movies", "/tv"], repr(v))

v = cli.parse_value("MONITOR_DIRS", "/movies")
check("a single path is still a list", v == ["/movies"], repr(v))

# A path with a dot in it must not be mistaken for a number by the numeric
# branch — the list branch runs first, which is what keeps this true.
v = cli.parse_value("MONITOR_DIRS", "/mnt/user/media.old")
check("a path containing a dot stays a path", v == ["/mnt/user/media.old"], repr(v))


# ── Values that are not numbers, and must not be mangled into them ─────────
for key, raw in (("DAILY_RUN_TIME", "03:30"), ("RUN_MODE", "headroom"),
                 ("TAUTULLI_URL", "http://nas:8181"), ("MEDIA_SERVER", "jellyfin")):
    v = cli.parse_value(key, raw)
    check(f"{raw!r} stays a string", v == raw and isinstance(v, str), repr(v))

v = cli.parse_value("SOME_JSON", '{"a": 1}')
check("a JSON object parses as a dict", v == {"a": 1}, repr(v))

v = cli.parse_value("SOME_JSON", "{not json")
check("malformed JSON falls back to the raw string, not an exception",
      v == "{not json", repr(v))


# ── KEY=VALUE splitting ────────────────────────────────────────────────────
check("a pair splits into key and typed value",
      cli._kv_pairs(["HEADROOM_GB=500"]) == {"HEADROOM_GB": 500}, "")
check("only the first = splits, so a value may contain one",
      cli._kv_pairs(["TAUTULLI_URL=http://nas:8181/?a=b"])
      == {"TAUTULLI_URL": "http://nas:8181/?a=b"}, "")
check("the key is trimmed", cli._kv_pairs([" HEADROOM_GB =500"]) == {"HEADROOM_GB": 500}, "")
check("several pairs accumulate",
      cli._kv_pairs(["A=1", "B=2"]) == {"A": 1, "B": 2}, "")

# An argument with no '=' is a typo, and guessing at it would write something
# nobody asked for.
try:
    cli._kv_pairs(["HEADROOM_GB"])
    raised = None
except SystemExit as ex:
    raised = str(ex)
check("an argument with no = is refused", raised is not None, raised)
check("...naming the offending word", raised and "HEADROOM_GB" in raised, raised)


# ── Reading a body ─────────────────────────────────────────────────────────
check("a JSON object is parsed", cli._parse('{"ok": true}') == {"ok": True}, "")
check("a JSON array is parsed", cli._parse("[1, 2]") == [1, 2], "")
check("surrounding whitespace does not stop it",
      cli._parse('\n  {"ok": true}\n ') == {"ok": True}, "")
check("plain text comes back as text", cli._parse("Not Found") == "Not Found", "")
# A proxy or a crashed handler can return something that starts like JSON and
# is not. Returning the raw text keeps it printable instead of raising.
check("a truncated JSON body comes back as text, not an exception",
      cli._parse('{"ok": tr') == '{"ok": tr', "")
check("an empty body is an empty string", cli._parse("   ") == "", "")


# ── The HTTP boundary ──────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SEEN = []


def opener(result):
    """Stand in for the urllib opener; records the request it was given."""
    class _O:
        def open(self, req, timeout=None):
            SEEN.append(req)
            if isinstance(result, BaseException):
                raise result
            return result
    return _O()


cli._OPENER = opener(FakeResponse(200, '{"ok": true}'))
del SEEN[:]
code, body = cli.api("GET", "/api/status", BASE)
check("a 200 returns its parsed body", (code, body) == (200, {"ok": True}), (code, body))
req = SEEN[0]
check("the request is tagged as coming from MediaReducer",
      req.get_header("X-mediareducer") == "1", dict(req.headers))
check("a GET sends no body", req.data is None, req.data)

cli._OPENER = opener(FakeResponse(200, "{}"))
del SEEN[:]
cli.api("POST", "/api/config", BASE, body={"HEADROOM_GB": 500})
req = SEEN[0]
check("a POST sends JSON", json.loads(req.data.decode()) == {"HEADROOM_GB": 500}, req.data)
check("...declared as JSON", req.get_header("Content-type") == "application/json",
      dict(req.headers))

# An error status is a normal return, not an exception: the body carries the
# reason a save was rejected, and every caller prints it.
cli._OPENER = opener(urllib.error.HTTPError(
    BASE, 400, "Bad Request", {}, io.BytesIO(b'{"ok": false, "error": "bad value"}')))
code, body = cli.api("POST", "/api/config", BASE, body={})
check("a 400 comes back as a status and a body, not an exception", code == 400, code)
check("...with the server's reason intact",
      isinstance(body, dict) and body.get("error") == "bad value", body)

# The service being down is the one thing worth an exception: every command
# would otherwise print its own version of the same confusion.
cli._OPENER = opener(urllib.error.URLError("Connection refused"))
try:
    cli.api("GET", "/api/status", BASE)
    err = None
except cli.ApiError as ex:
    err = str(ex)
check("an unreachable service raises ApiError", err is not None, err)
check("...naming the URL it tried", err and BASE in err, err)
check("...and how to point it elsewhere", err and "MEDIAREDUCER_URL" in err, err)

cli._OPENER = opener(TimeoutError())
try:
    cli.api("GET", "/api/status", BASE, timeout=5)
    err = None
except cli.ApiError as ex:
    err = str(ex)
check("a timeout raises ApiError naming the limit", err and "5s" in err, err)

# main() turns that into exit 2 rather than a traceback, which is what a
# cron job or a shell chain actually reads.
cli._OPENER = opener(urllib.error.URLError("Connection refused"))
buf, errbuf = io.StringIO(), io.StringIO()
with redirect_stdout(buf), redirect_stderr(errbuf):
    rc = cli.main(["--url", BASE, "status"])
check("an unreachable service exits 2, not a traceback", rc == 2, rc)
check("...with the explanation on stderr", "Cannot reach MediaReducer" in errbuf.getvalue(),
      errbuf.getvalue())


# ── Sizes with nothing in them ─────────────────────────────────────────────
# Every status field is optional; a missing one renders as a dash rather than
# "None GB" or a crash mid-table.
check("gb renders a number", cli.gb(5) == "5.0 GB", cli.gb(5))
check("gb renders a numeric string", cli.gb("5") == "5.0 GB", cli.gb("5"))
check("gb of nothing is a dash", cli.gb(None) == "—", cli.gb(None))
check("gb of nonsense is a dash", cli.gb("n/a") == "—", cli.gb("n/a"))
check("bytes_gb converts", cli.bytes_gb(5 * cli.GB) == "5.0 GB", cli.bytes_gb(5 * cli.GB))
check("bytes_gb of nothing is a dash", cli.bytes_gb(None) == "—", cli.bytes_gb(None))
check("bytes_gb of nonsense is a dash", cli.bytes_gb([]) == "—", cli.bytes_gb([]))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
