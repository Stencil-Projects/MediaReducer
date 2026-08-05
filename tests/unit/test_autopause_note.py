"""Dismissing a note has to actually clear what made it true.

The Configuration banner explains why Automatic Cleanup switched itself to
Monitor Only. It reports something already done, so once read there is nothing
left for it to warn about — but hiding it in the browser would leave the stored
reason behind, and it would be back on the next load, still there on every other
device, and ready to resurface the next time the mode happened to be Monitor
Only. So the X writes: it clears the one key and nothing else.

The banner only renders while the mode IS Monitor Only, which is what keeps a
dismissal from being confused with the mode itself changing — the mode is the
app's decision and stays where the autopause put it.

The dashboard's store-reset note works the same way on the engine's marker, with
one addition: it also stops on its own once a Simulate has rebuilt the snapshot,
because by then it would be asking for a run that already happened.
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
note = re.search(r'<div class="config-inline-note is-dismissible[^>]*id="autopause-note".*?</div>',
                 page, re.S)
check("...inside a dismissible banner", note is not None)
check("...with an X on it", note and 'data-dismiss-note="autopause"' in note.group(0),
      note.group(0)[:200] if note else "")
check("...labelled for a screen reader too",
      note and 'aria-label="Dismiss this note"' in note.group(0))

# ── Dismissing clears the stored reason, so it does not come back ─────────────
r = client.post("/api/notes/autopause/dismiss", headers=WRITE)
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
r2 = client.post("/api/notes/autopause/dismiss", headers=WRITE)
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
client.post("/api/notes/autopause/dismiss", headers=WRITE)
st = client.get("/api/status").get_json()
check("...and stops carrying it once dismissed",
      not st.get("run_mode_autopause_reason"), st.get("run_mode_autopause_reason"))

# ── The store-reset note: the engine wiped the plan, and says so ─────────────
# ensure_code_current drops the snapshot and queue whenever the running engine's
# checksum changes. Without a note for it the plan is simply not there any more,
# and the dashboard asks for a Simulate without ever saying why.
import db  # noqa: E402

def seed_store(*, prior_checksum, snapshot=None):
    """Wipe in place rather than deleting the store: recreating it resets the
    generation counter the app memoizes its reads against, and every seed after
    the first would then be invisible to _cache_file_data."""
    with db.transaction(A.db_path()) as conn:
        conn.execute("DELETE FROM meta WHERE key != '_gen'")
        for table in ("metadata_cache", "movies", "queue"):
            conn.execute(f"DELETE FROM {table}")
        if prior_checksum is not None:
            db.set_meta(conn, "code_checksum", prior_checksum)
        db.ensure_code_current(conn, "the-new-code")
        if snapshot is not None:
            db.replace_movies(conn, snapshot)
            db.set_meta(conn, "snapshot_built_at", 1)
    A._invalidate_request_store_cache()

# A first run has nothing to lose and no business being told its database was
# reset — the marker is only for a wipe that followed a REAL code change.
seed_store(prior_checksum=None)
check("a fresh install raises no reset note", A.code_reset_pending() is False)

seed_store(prior_checksum="the-old-code")
check("a code change that wiped the store raises the note", A.code_reset_pending() is True)
page = client.get("/").get_data(as_text=True)
reset_note = re.search(r'<div class="config-inline-note is-dismissible[^>]*id="store-reset-note".*?</div>',
                       page, re.S)
check("...and the dashboard renders it, not hidden",
      reset_note is not None and "hidden" not in reset_note.group(0).split(">")[0],
      reset_note.group(0)[:160] if reset_note else "")
check("...asking for the Simulate that rebuilds the plan",
      reset_note and "Simulate" in reset_note.group(0))
check("...with its own dismiss control",
      reset_note and 'data-dismiss-note="store-reset"' in reset_note.group(0))
check("...and the status poll agrees",
      client.get("/api/status").get_json().get("code_reset_pending") is True)

# It stops on its own once a Simulate has rebuilt the snapshot: by then it would
# be asking for a run that already happened.
seed_store(prior_checksum="the-old-code",
           snapshot=[A_MOVIE := {"path": "/library/m/A.mkv", "title": "A", "year": 2020,
                                 "rating": None, "votes": 0, "plays": 0, "users": 0,
                                 "last_played": 0, "added_at": 0, "size_bytes": 10,
                                 "protected": False, "favorite": False, "excluded": False,
                                 "source_id": "1", "jf_source_id": None,
                                 "tmdb_id": None, "section_id": "1"}])
check("a rebuilt snapshot retires the note without anyone dismissing it",
      A.code_reset_pending() is False)

# ...and dismissing clears the marker, so it does not come back on reload.
seed_store(prior_checksum="the-old-code")
r = client.post("/api/notes/store-reset/dismiss", headers=WRITE)
check("dismissing the reset note answers ok",
      r.status_code == 200 and r.get_json().get("dismissed") is True, r.get_json())
check("...and it stays gone", A.code_reset_pending() is False)
r2 = client.post("/api/notes/store-reset/dismiss", headers=WRITE)
check("a second dismiss is a no-op, not a failure",
      r2.status_code == 200 and r2.get_json().get("dismissed") is False, r2.get_json())

check("an unknown note name is refused, not silently accepted",
      client.post("/api/notes/nonsense/dismiss", headers=WRITE).status_code == 404)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
