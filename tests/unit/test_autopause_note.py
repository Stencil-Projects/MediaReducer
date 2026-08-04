"""Dismissing the autopause note has to actually clear it.

The Configuration banner explains why Automatic Cleanup switched itself to
Monitor Only. It reports something already done, so once read there is nothing
left for it to warn about — but hiding it in the browser would leave the stored
reason behind, and it would be back on the next load, still there on every other
device, and ready to resurface the next time the mode happened to be Monitor
Only. So the X writes: it clears the one key and nothing else.

The banner only renders while the mode IS Monitor Only, which is what keeps a
dismissal from being confused with the mode itself changing — the mode is the
app's decision and stays where the autopause put it.
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-autopause.")
atexit.register(shutil.rmtree, _OUT, True)
CONFIG = Path(_OUT) / "config.json"
os.environ["MEDIAREDUCER_CONFIG"] = str(CONFIG)
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(CONFIG, "w"))

import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


A.app.config["TESTING"] = True
client = A.app.test_client()
REASON = "Automatic Cleanup is paused automatically after every restart."
# base.html wraps fetch() to put this on every mutating request; the server
# rejects writes without it, so a test client has to look like a browser.
WRITE = {"X-MediaReducer": "1"}


def seed(**over):
    cfg = A.load_config()
    cfg.update({"OUTPUT_DIR": _OUT, "RUN_MODE": "paused",
                "_RUN_MODE_AUTOPAUSE_REASON": REASON})
    cfg.update(over)
    A.save_config(cfg)
    return cfg


def stored_reason():
    return json.loads(CONFIG.read_text()).get("_RUN_MODE_AUTOPAUSE_REASON")


# ── The banner carries a dismiss control while there is something to dismiss ──
seed()
page = client.get("/config").get_data(as_text=True)
check("the note renders with the stored reason", REASON in page)
note = re.search(r'<div class="config-inline-warning is-dismissible[^>]*id="autopause-note".*?</div>',
                 page, re.S)
check("...inside a dismissible banner", note is not None)
check("...with an X on it", note and 'id="autopause-note-dismiss"' in note.group(0),
      note.group(0)[:200] if note else "")
check("...labelled for a screen reader too",
      note and 'aria-label="Dismiss this note"' in note.group(0))

# ── Dismissing clears the stored reason, so it does not come back ─────────────
r = client.post("/api/run-mode/autopause-note/dismiss", headers=WRITE)
body = r.get_json()
check("the dismiss endpoint answers ok", r.status_code == 200 and body.get("ok") is True, body)
check("...and reports it dismissed something", body.get("dismissed") is True, body)
check("the reason is gone from config.json", stored_reason() is None, stored_reason())
check("a reload no longer shows the note", REASON not in client.get("/config").get_data(as_text=True))

# It is only the NOTE being dismissed. The mode the autopause chose is the app's
# decision and must survive being told the explanation was read.
check("the mode the autopause set is untouched",
      json.loads(CONFIG.read_text()).get("RUN_MODE") == "paused")

# ── Dismissing twice is not an error, and writes nothing the second time ──────
before = CONFIG.read_text()
r2 = client.post("/api/run-mode/autopause-note/dismiss", headers=WRITE)
check("a second dismiss is a no-op, not a failure",
      r2.status_code == 200 and r2.get_json().get("ok") is True
      and r2.get_json().get("dismissed") is False, r2.get_json())
check("...and leaves config.json byte-identical", CONFIG.read_text() == before)

# ── No reason, no banner: nothing to dismiss and nothing rendered ─────────────
seed(_RUN_MODE_AUTOPAUSE_REASON="")
check("no note renders without a stored reason",
      'id="autopause-note"' not in client.get("/config").get_data(as_text=True))

# ── The dashboard tooltip falls back cleanly once the reason is cleared ───────
# It is the other reader of this key; with it gone the tooltip must describe
# Monitor Only rather than trail off after "switched to Monitor Only:".
seed()
st = client.get("/api/status").get_json()
check("the status payload carries the reason while it is stored",
      st.get("run_mode_autopause_reason") == REASON, st.get("run_mode_autopause_reason"))
client.post("/api/run-mode/autopause-note/dismiss", headers=WRITE)
st = client.get("/api/status").get_json()
check("...and stops carrying it once dismissed",
      not st.get("run_mode_autopause_reason"), st.get("run_mode_autopause_reason"))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
