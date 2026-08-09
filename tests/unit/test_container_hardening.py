"""The two deployment files hand the container its privileges, and the code
they are sized for lives somewhere else.

entrypoint.py runs as root just long enough to hand /config to the mapped user
and become them, and the capability list is cut to exactly that. Nothing warns
you when a new call needs a capability the list no longer grants — the drop
happens at container start, the failure happens at the next chown. So the
needed set is derived HERE from the calls the entrypoint actually makes, and
compared against what each file grants.

The Unraid template and the compose file are the same deployment described
twice, so they are also checked against each other.
"""
import re
import sys
import pathlib
import xml.etree.ElementTree as ET

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


# ── What the privileged phase actually needs ─────────────────────────────────
# Read off the syscalls rather than restated by hand, so adding one to the
# entrypoint fails here instead of at someone's next container start.
entry = (ROOT / "entrypoint.py").read_text()
NEEDED = {
    # Giving away a file you do not own is CAP_CHOWN, every time.
    "CHOWN": r"\bos\.(l?chown)\(",
    "SETUID": r"\bos\.setuid\(",
    # setgroups is the same capability as setgid.
    "SETGID": r"\bos\.(setgid|setgroups)\(",
    # Not derivable from a call name: the chown walk has to descend a /config
    # left behind by any owner, restrictive modes included.
    "DAC_OVERRIDE": None,
    # Ones we deliberately do NOT grant, listed so a new use is caught.
    "FOWNER": r"\bos\.(chmod|utime)\(",
    "NET_BIND_SERVICE": r"\bbind\(\(?[^)]*\b(80|443)\b",
}
needed = {"DAC_OVERRIDE"}
for cap, pattern in NEEDED.items():
    if pattern and re.search(pattern, entry):
        needed.add(cap)
check("the entrypoint needs the capabilities we think it does",
      needed == {"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID"}, sorted(needed))

# The drop is only worth anything because the process does not stay root. If
# this stops being true the capability list is the least of it.
check("...and it drops to the mapped user itself",
      "os.setuid(" in entry and "os.setgid(" in entry)


# ── The Unraid template ──────────────────────────────────────────────────────
xml_path = ROOT / "unraid" / "mediareducer.xml"
tree = ET.parse(xml_path)                       # raises if the template is malformed
root = tree.getroot()
check("the template parses", root.tag == "Container", root.tag)

extra = (root.findtext("ExtraParams") or "").split()
xml_caps = {a.split("=", 1)[1] for a in extra if a.startswith("--cap-add=")}

check("the template drops every capability first", "--cap-drop=ALL" in extra, extra)
check("...adds back exactly what the entrypoint needs", xml_caps == needed,
      sorted(xml_caps))
check("...and asks for no new privileges",
      "--security-opt=no-new-privileges:true" in extra, extra)
# The two that were there before the hardening and are still load-bearing: the
# engine's subprocesses need reaping, and a stop that beats the ~8s finish of
# the file being deleted loses the deleted.log line for it.
check("--init survives", "--init" in extra, extra)
check("the 30s stop grace survives", "--stop-timeout=30" in extra, extra)
check("the container log is capped",
      "max-size=10m" in extra and "max-file=3" in extra, extra)

check("the template is not privileged", root.findtext("Privileged") == "false")

# The mounts: /library read-write because deleting is the job, the three
# Auto Detect ones read-only because reading a key is all they are for.
mounts = {c.get("Target"): c.get("Mode")
          for c in root.iter("Config") if c.get("Type") == "Path"}
check("/library is writable", mounts.get("/library") == "rw", mounts)
check("/config is writable", mounts.get("/config") == "rw", mounts)
for target in ("/tautulli", "/radarr", "/sonarr"):
    check(f"{target} is read-only", mounts.get(target) == "ro", mounts)
check("nothing else is mounted", set(mounts) == {
    "/library", "/config", "/tautulli", "/radarr", "/sonarr"}, sorted(mounts))

# The docker socket is the one mount that would turn "can delete media" into
# "is root on the host".
check("the docker socket is nowhere in the template",
      "docker.sock" not in xml_path.read_text())


# ── The compose file, describing the same deployment ─────────────────────────
compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
svc = compose["services"]["mediareducer"]

check("compose drops every capability first", svc.get("cap_drop") == ["ALL"],
      svc.get("cap_drop"))
check("...and grants the same set as the template", set(svc.get("cap_add") or []) == xml_caps,
      svc.get("cap_add"))
check("...and asks for no new privileges",
      "no-new-privileges:true" in (svc.get("security_opt") or []), svc.get("security_opt"))
check("compose caps the log too",
      (svc.get("logging") or {}).get("options", {}).get("max-size") == "10m",
      svc.get("logging"))
check("compose keeps init on", svc.get("init") is True)
# Stated as a duration string here and as seconds in the template; the number
# is the same claim about the same shutdown.
check("compose keeps the same stop grace", svc.get("stop_grace_period") == "30s",
      svc.get("stop_grace_period"))
check("the docker socket is not mounted either",
      not any("docker.sock" in v for v in (svc.get("volumes") or [])), svc.get("volumes"))

# Same read-only intent as the template, on the mounts that exist for one key.
vols = [v for v in (svc.get("volumes") or []) if not v.lstrip().startswith("#")]
for target in ("/tautulli", "/radarr", "/sonarr"):
    line = next((v for v in vols if f":{target}:" in v or v.endswith(":" + target)), "")
    check(f"compose mounts {target} read-only", line.endswith(":ro"), line or vols)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
