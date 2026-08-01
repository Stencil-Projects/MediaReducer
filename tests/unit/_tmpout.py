"""Test helper: keep a test's state files out of /config.

Every state path both processes write (lastrun.log, logs/, progress.json,
deleted.log, mediareducer.db, the IMDb dataset) hangs off OUTPUT_DIR, and its
default is /config — the live data directory on a deployed host. A test that
leaves it at the default writes there for real, and a test that WIPES the store
there deletes a user's library snapshot and deletion history. On CI it simply
fails, since the runner cannot create /config at all.

app resolves output_dir() per call, so pointing MEDIAREDUCER_CONFIG at a config
carrying OUTPUT_DIR is enough:

    OUT = _tmpout.config(HEADROOM_GB=100)      # BEFORE `import app`

engine does not: it computes its path constants once at import from the /config
default and only rebuilds them when a config with OUTPUT_DIR is applied, which
importing alone does not do. So a test that imports engine repoints them:

    import engine
    OUT = _tmpout.redirect_engine(engine)      # AFTER `import engine`

Both make their own directory and remove it at exit, and both are safe to use
together (pass the same path).
"""
import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path


def make(prefix="mr-out.") -> str:
    """A temp directory that cleans itself up. Per test, never a fixed name: a
    shared /tmp path is one `sudo` away from being root-owned and unwritable for
    every later run."""
    out = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, out, True)
    return out


def config(out=None, **settings) -> str:
    """Point MEDIAREDUCER_CONFIG at a config carrying OUTPUT_DIR. Call before
    importing app, whose startup already writes into whatever it resolves."""
    out = out or make()
    cfg = Path(out) / "config.json"
    cfg.write_text(json.dumps({"OUTPUT_DIR": out, **settings}))
    os.environ["MEDIAREDUCER_CONFIG"] = str(cfg)
    return out


def redirect_engine(engine, out=None) -> str:
    """Repoint engine's already-computed path constants. Mirrors the block in
    engine's config loader that rebuilds them all when OUTPUT_DIR changes; if a
    path is added there, add it here."""
    out = out or make()
    base = Path(out)
    engine.OUTPUT_DIR = base
    engine.LOGFILE = base / "lastrun.log"
    engine.LOGS_DIR = base / "logs"
    engine.DELETED_LOG = base / "deleted.log"
    engine.IMDB_RATINGS_PATH = base / "title.ratings.tsv"
    engine.DB_FILE = base / "mediareducer.db"
    engine.PROGRESS_FILE = base / "progress.json"
    return out
