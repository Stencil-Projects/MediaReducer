"""What the appdata mounts are for, and what their status cards may claim.

The two appdata mounts (/tautulli, /radarr) exist for exactly one reason: so
Auto Detect can read an API key out of a service's own config file.
Nothing else reads them — the engine talks to every service over HTTP — so a
missing mount is a convenience lost, never a failure.

So the contract asserted here is: report what the mount CONTRIBUTES, derived
from the same detection Auto Detect runs, independent of any checkbox. A card
keyed off a marker FILE and gated on the Plex/Jellyfin checkboxes instead gives
three inconsistent answers to one question on a clean install — "(Not enabled)"
for Tautulli and Jellyfin, "verified" for ungated Radarr — and claims
"verified" for a config file holding no key at all.

There is deliberately NO Jellyfin mount: Jellyfin issues API keys from its own
dashboard and stores none on disk, so the mount could only ever supply its HTTP
port, and ports are not taken from appdata at all. Every URL default uses the
service's documented port, so what the form suggests is predictable and matches
each project's own docs rather than a second source of truth that would only
matter for the setup least likely to rely on it.
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_TMP = tempfile.mkdtemp(prefix="mr-appdata.")
atexit.register(shutil.rmtree, _TMP, True)


def _appdata(name, files=None):
    """An appdata directory holding the given files, or a path that isn't there."""
    root = Path(_TMP, name)
    if files is None:
        return str(root / "absent")
    root.mkdir(parents=True, exist_ok=True)
    for fname, body in files.items():
        (root / fname).write_text(body, encoding="utf-8")
    return str(root)


_TAUT_FULL = """[General]
http_port = 8181
api_key = TAUTKEY
[PMS]
pms_ip = 10.0.0.5
pms_port = 32400
pms_token = PLEXTOK
"""
# A real config.ini that simply has no API key set yet — the case a
# file-exists check would report as "verified".
_TAUT_KEYLESS = """[General]
http_port = 8181
[PMS]
pms_ip = 10.0.0.5
"""

os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_TMP) / "config.json")
os.environ["MEDIAREDUCER_LIBRARY"] = _TMP
json.dump({"OUTPUT_DIR": _TMP}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))
os.environ["MEDIAREDUCER_TAUTULLI_APPDATA"] = _appdata("taut", {"config.ini": _TAUT_FULL})
os.environ["MEDIAREDUCER_RADARR_APPDATA"] = _appdata(
    "rad", {"config.xml": "<Config><ApiKey>RADKEY</ApiKey></Config>"})

import app as A

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


def verify(**dirs):
    """The /api/connections/verify payload, with appdata roots pointed wherever
    the case needs them."""
    for attr, value in dirs.items():
        setattr(A, attr, value)
    with A.app.test_request_context("/api/connections/verify"):
        return json.loads(A.api_verify_connections().get_data(as_text=True))


# ── Everything mounted, and NO server checkbox ticked ────────────────────────
# The clean-install case: nothing ticked. Reading a file off disk does not
# depend on which server is selected, so the status must not either.
A.load_config()["USE_PLEX"] = False
d = verify()
check("a mounted Tautulli config reports BOTH keys it carries",
      d["tautulli"]["keys"] == ["Tautulli", "Plex"] and d["tautulli"]["mounted"] is True)
check("Radarr reports its own key", d["radarr"]["keys"] == ["Radarr"])
check("no status depends on a server checkbox being ticked",
      not any("enabled" in json.dumps(v).lower() for v in d.values()))

# ── The claim is verified, not assumed ──────────────────────────────────────
# Asking "does config.ini exist" is not enough: a key-less config answers yes.
d = verify(TAUTULLI_APPDATA_DIR=_appdata("taut_empty", {"config.ini": _TAUT_KEYLESS}))
check("a mounted config with no key in it reports NO key",
      d["tautulli"]["mounted"] is True and d["tautulli"]["keys"] == [])
check("...and is still distinguishable from an absent mount",
      verify(TAUTULLI_APPDATA_DIR=_appdata("taut_gone"))["tautulli"]["mounted"] is False)

# Only the key that is actually there gets claimed: Tautulli configured without
# Plex must not advertise a Plex token.
d = verify(TAUTULLI_APPDATA_DIR=_appdata(
    "taut_nopms", {"config.ini": "[General]\napi_key = K\n[PMS]\npms_ip = 10.0.0.5\n"}))
check("a Tautulli config with no Plex token claims Tautulli only",
      d["tautulli"]["keys"] == ["Tautulli"])

# A card may never claim a key Auto Detect wouldn't fill — same source, so the
# button and the badge cannot disagree about what is available.
detected = A._autodetected_connection_values()
d = verify()
check("the card's claim matches what Auto Detect would actually fill",
      (("Tautulli" in d["tautulli"]["keys"]) == bool(detected.get("tautulli_key"))
       and ("Plex" in d["tautulli"]["keys"]) == bool(detected.get("plex_token"))
       and (d["radarr"]["keys"] != []) == bool(detected.get("radarr_key"))))

# ── No Jellyfin mount exists at all ─────────────────────────────────────────
check("there is no Jellyfin appdata mount to report on", "jellyfin" not in d)
check("...and no Jellyfin appdata path is derived for the engine either",
      not hasattr(A, "JELLYFIN_APPDATA_DIR")
      and "JELLYFIN_APPDATA" not in A._build_config()[0])

# JELLYFIN_APPDATA is not a setting, but a config.json can carry one — a hand
# edit, or a file written by an install that derived it. Left alone it shows up
# in `mr config show` as though it were real, so load prunes it.
_stale = json.load(open(os.environ["MEDIAREDUCER_CONFIG"]))
_stale.update({"JELLYFIN_APPDATA": "/jellyfin", "USE_JELLYFIN": True,
               "JELLYFIN_URL": "http://10.0.0.9:8096", "JELLYFIN_API_KEY": "k"})
json.dump(_stale, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))
_rebuilt = A._build_config()[0]
check("a key that is not a setting is pruned out of config.json on load",
      "JELLYFIN_APPDATA" not in _rebuilt)
check("...without disturbing Jellyfin the SERVICE, which is unaffected",
      _rebuilt.get("USE_JELLYFIN") is True
      and _rebuilt.get("JELLYFIN_URL") == "http://10.0.0.9:8096"
      and _rebuilt.get("JELLYFIN_API_KEY") == "k")

# ── Ports always come from the documentation, never from appdata ─────────────
# A detected port would be a second source of truth, and it only ever differs
# for a non-standard setup — exactly the setup that types its own URL anyway.
A.TAUTULLI_APPDATA_DIR = _appdata("taut_oddports", {"config.ini": """[General]
http_port = 9999
api_key = TAUTKEY
[PMS]
pms_ip = 10.0.0.5
pms_port = 55555
pms_token = PLEXTOK
"""})
det = A._autodetected_connection_values()
check("a non-standard Tautulli port in appdata is ignored",
      det["tautulli_url"].endswith(":8181"))
check("a non-standard Plex port in appdata is ignored",
      det["plex_url"].endswith(":32400"))
check("...while the keys and the Plex HOST are still read",
      det["tautulli_key"] == "TAUTKEY" and det["plex_token"] == "PLEXTOK"
      and det["host"] == "10.0.0.5")
with A.app.test_request_context("/", base_url="http://10.0.0.9:7474"):
    urls = A._connection_url_defaults()
check("every URL default lands on its documented port",
      all(urls[f].endswith(f":{port}")
          for f, port in A._SERVICE_DEFAULT_PORTS.items() if urls[f]))
check("Jellyfin's default is still offered — 8096 on the host that reached us",
      urls["JELLYFIN_URL"] == "http://10.0.0.9:8096")

# The key read must sit outside the host check: inside it, a Tautulli config
# with a key but NO pms_ip yields nothing at all.
A.TAUTULLI_APPDATA_DIR = _appdata(
    "taut_nohost", {"config.ini": "[General]\napi_key = ONLYKEY\n"})
check("a key with no host in the config is still detected",
      A._autodetected_connection_values()["tautulli_key"] == "ONLYKEY")

# ── A missing mount never blocks setup ──────────────────────────────────────
# The whole point: these are a convenience. Losing one must not produce an
# error, only the note that the key has to be typed in.
# Both roots must actually be ABSENT here, or the warning being asserted about
# could never fire and the check would pass for the wrong reason.
A.TAUTULLI_APPDATA_DIR = _appdata("taut_gone2")
A.RADARR_APPDATA_DIR = _appdata("rad_gone2")
assert not A._appdata_mount_state()["tautulli"]["ok"], "fixture must leave Tautulli unmounted"
health = A._connection_health_state(
    {"USE_PLEX": True, "TAUTULLI_URL": "http://taut:8181", "TAUTULLI_API_KEY": "k",
     "MONITOR_DIRS": []}, probe=False)
_blockers = " ".join(health.get("errors") or [])
check("an unmounted appdata never appears as a connection ERROR",
      "appdata" not in _blockers.lower())
# And with the key already entered, there is nothing left for the mount to do,
# so it should not still be warning about Auto Detect.
_warn = " ".join(health.get("warnings") or [])
check("...nor warns about Auto Detect once the key is entered by hand",
      "auto detect" not in _warn.lower())

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
