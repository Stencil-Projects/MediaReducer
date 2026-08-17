"""The Unraid template as a LISTING, not as a container spec.

test_container_hardening.py covers what the template grants the container.
This covers what Community Applications shows a stranger, and what it fetches
while showing it — a different failure mode entirely. Nothing here can break a
running install; all of it can break an install that never happens, or send
someone somewhere that does not exist.

Three kinds of thing are pinned.

Links that must resolve. The icon and the template itself are fetched from the
published repo by URL, so a file renamed here and a URL left alone is a broken
listing that nothing else notices — the template keeps working for everyone who
already has it.

The dev repo must never appear. It is private. A link to it in a public listing
is a dead end for every reader and puts the private repo's name in front of
them, and the two names differ by one word.

And the warning. Someone installing from Community Applications reads the
Overview and nothing else — not the README, not the dashboard. This deletes
files with no recycle bin, so the Overview is the only place that fact can
reach them before it is too late.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


XML = ROOT / "unraid" / "mediareducer.xml"
root = ET.parse(XML).getroot()
raw = XML.read_text()


def text(tag):
    el = root.find(tag)
    return (el.text or "").strip() if el is not None else None


# ── Everything the listing is built from ─────────────────────────────────────
# A missing one of these is not a crash; it is a blank field on a page someone
# uses to decide whether to trust an app that deletes their media.
for tag in ("Name", "Repository", "Registry", "Network", "Support", "Project",
            "Overview", "Category", "WebUI", "TemplateURL", "Icon"):
    check(f"{tag} is present and not blank", bool(text(tag)), text(tag))

check("the container is not privileged", text("Privileged") == "false", text("Privileged"))

# ── The private repo must not leak into a public listing ─────────────────────
# The dev repo and the published one differ by a suffix, which is exactly the
# kind of thing a copy-paste gets wrong.
DEV = re.compile(r"mediareducer-dev", re.I)
check("no link points at the private dev repo", not DEV.search(raw),
      next((ln for ln in raw.splitlines() if DEV.search(ln)), None))

PUBLIC = "https://github.com/Stencil-Projects/MediaReducer"
check("Project points at the published repo", text("Project") == PUBLIC, text("Project"))
check("Support points somewhere in the published repo",
      text("Support").startswith(PUBLIC), text("Support"))

# ── Fetched-by-URL files have to exist where the URL says ────────────────────
RAW = "https://raw.githubusercontent.com/Stencil-Projects/MediaReducer/main/"


def published_file(url):
    """The repo file a raw.githubusercontent URL resolves to, or None if the
    URL does not point into the published repo at all."""
    return ROOT / url[len(RAW):] if url.startswith(RAW) else None


icon = published_file(text("Icon"))
check("the icon URL points into the published repo", icon is not None, text("Icon"))
check("...at a file that exists", icon is not None and icon.is_file(), str(icon))
check("...which is a PNG", icon is not None and icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")

tpl = published_file(text("TemplateURL"))
check("TemplateURL points into the published repo", tpl is not None, text("TemplateURL"))
check("...at this very file", tpl is not None and tpl.resolve() == XML.resolve(), str(tpl))

# Held-back paths cannot be published, so a URL into one is broken on arrival.
for url_tag in ("Icon", "TemplateURL"):
    check(f"{url_tag} does not point at a file publish.sh holds back",
          not text(url_tag)[len(RAW):].startswith("tools/"), text(url_tag))

# ── The image the listing installs ───────────────────────────────────────────
repo_img = text("Repository")
check("Registry and Repository name the same image",
      repo_img.split(":")[0] in text("Registry"), (repo_img, text("Registry")))
check("the image is tagged, not left floating on latest-by-default",
      ":" in repo_img.rsplit("/", 1)[-1], repo_img)

# publish.yml builds the image name from $GITHUB_REPOSITORY, lowercased, so the
# owner in this template has to be the owner that publishes — and neither half
# is visible from the other. Getting it wrong breaks nothing at publish time and
# nothing at install time either: the workflow pushes happily to the new path
# while every listing keeps pulling the old one, which simply stops receiving
# updates. That silence is why this is pinned to the publishing repo's own name
# rather than to a literal.
PUBLIC_OWNER, PUBLIC_NAME = PUBLIC[len("https://github.com/"):].split("/")
expected_image = f"ghcr.io/{PUBLIC_OWNER.lower()}/{PUBLIC_NAME.lower()}"
check("the image lives under the publishing repo's own owner",
      repo_img.split(":")[0] == expected_image, (repo_img, expected_image))
check("...and the Registry link agrees",
      text("Registry").rstrip("/") == f"https://{expected_image}", text("Registry"))

# ── The port the WebUI button opens ──────────────────────────────────────────
# Three places have to agree, and only one of them is visible when it is wrong:
# the button just fails to open anything.
port_cfg = next(c for c in root.findall("Config") if c.get("Type") == "Port")
INTERNAL = 7474
check("the WebUI link uses the port variable, not a hardcoded one",
      f"[PORT:{port_cfg.get('Default')}]" in text("WebUI"), text("WebUI"))
check("the port config targets the port the container serves on",
      port_cfg.get("Target") == str(INTERNAL), port_cfg.get("Target"))
check("...which is the port the Dockerfile exposes",
      f"EXPOSE {INTERNAL}" in (ROOT / "Dockerfile").read_text())
check("...and the app's own default",
      f"SERVICE_PORT_DEFAULT = {INTERNAL}" in (ROOT / "app.py").read_text())

# ── Every Config row the install form renders ────────────────────────────────
for c in root.findall("Config"):
    nm = c.get("Name")
    for attr in ("Name", "Target", "Default", "Mode", "Description", "Type",
                 "Display", "Required", "Mask"):
        check(f"[{nm}] has {attr}", c.get(attr) is not None, c.attrib)
    check(f"[{nm}] Required is a bool CA understands",
          c.get("Required") in ("true", "false"), c.get("Required"))
    check(f"[{nm}] Display is a value CA understands",
          c.get("Display") in ("always", "advanced", "advanced-hide"), c.get("Display"))
    # A required field with no default is a form someone has to guess at.
    if c.get("Required") == "true":
        check(f"[{nm}] is required, so it ships with a default",
              bool((c.get("Default") or "").strip()), c.attrib)

# The two mounts the app cannot run without, writable, exactly as the entrypoint
# and the engine expect them.
paths = {c.get("Target"): c for c in root.findall("Config") if c.get("Type") == "Path"}
for target in ("/library", "/config"):
    check(f"{target} is offered", target in paths, sorted(paths))
    check(f"...and is writable", paths[target].get("Mode") == "rw", paths[target].get("Mode"))
    check(f"...and required", paths[target].get("Required") == "true")

# Everything else is read-only. These exist so Auto Detect can read one key out
# of each; write access to another app's appdata is not something to ask for.
for target, cfg in paths.items():
    if target not in ("/library", "/config"):
        check(f"the optional {target} mount is read-only", cfg.get("Mode") == "ro",
              cfg.get("Mode"))
        check(f"...and optional", cfg.get("Required") == "false", cfg.get("Required"))

# ── What a stranger is told before installing ────────────────────────────────
overview = text("Overview")
low = overview.lower()
check("the Overview says it deletes", "delet" in low, overview[:80])
check("...that there is no recycle bin", "no recycle bin" in low, overview[:200])
check("...that it is an alpha", "alpha" in low, overview[:200])
check("...that it starts paused, so nothing goes before it is configured",
      "starts" in low and "paused" in low, overview[:200])
check("...and that there is no login on it",
      "no login" in low, overview[:400])
check("the deletion warning is emphasised, not buried in a sentence",
      re.search(r"\[b\][^[]*(delete|recycle bin)", low) is not None, overview[:300])

check("the Category is one CA renders",
      set(text("Category").split()) <= {"MediaApp:Video", "Tools:Utilities"},
      text("Category"))

# ── The development twin, when it is here ────────────────────────────────────
# Held back from the public repo with the rest of the dev tooling, so this only
# runs in the repo that has it. Everything above is about a listing a stranger
# reads; this is about two containers on one machine. The dev one is an
# unreleased build that deletes files, and the two things it must not share are
# the port (the release container is already on it) and the config directory —
# a dev build writing the release container's store is how testing a migration
# destroys the install it was being tested against.
DEV_XML = ROOT / "unraid" / "mediareducer-dev.xml"
if DEV_XML.is_file():
    dev = ET.parse(DEV_XML).getroot()

    def dev_text(tag):
        el = dev.find(tag)
        return (el.text or "").strip() if el is not None else None

    dev_paths = {c.get("Target"): c for c in dev.findall("Config")
                 if c.get("Type") == "Path"}
    dev_port = next(c for c in dev.findall("Config") if c.get("Type") == "Port")

    check("[dev] the two containers cannot both claim one host port",
          dev_port.get("Default") != port_cfg.get("Default"),
          (dev_port.get("Default"), port_cfg.get("Default")))
    # Only the HOST side moves. The image serves on 7474 whatever the template
    # says, so a dev template that remapped the container port would install a
    # container whose health check probes a port nothing is listening on.
    check("[dev] ...while the container port stays the one the image serves",
          dev_port.get("Target") == str(INTERNAL), dev_port.get("Target"))
    # Unraid resolves [PORT:n] by looking up the CONTAINER port n and printing
    # the host port mapped to it. Naming the host port here would send the WebUI
    # button to a port that only happens to be right while the two agree.
    check("[dev] ...and the WebUI link names the container port, so it follows",
          f"[PORT:{INTERNAL}]" in (dev_text("WebUI") or ""), dev_text("WebUI"))

    check("[dev] the config directories are separate",
          dev_paths["/config"].get("Default") != paths["/config"].get("Default"),
          (dev_paths["/config"].get("Default"), paths["/config"].get("Default")))

    # The capabilities are derived from what entrypoint.py does, by
    # test_container_hardening, against the release template. A dev container
    # granted anything more would be testing a container nobody ships.
    check("[dev] it runs under exactly the release container's hardening",
          dev_text("ExtraParams") == text("ExtraParams"),
          dev_text("ExtraParams"))
    check("[dev] and is not privileged either",
          dev_text("Privileged") == "false", dev_text("Privileged"))

    # The mirror image of the check above: this listing is the one that MUST
    # name the private repo, and it must not install the public image by
    # accident — an alpha tag pulled into a dev container looks like it worked.
    dev_owner, dev_name = dev_text("Project")[len("https://github.com/"):].split("/")
    check("[dev] it installs the image the dev repo publishes, not the public one",
          dev_text("Repository").split(":")[0]
          == f"ghcr.io/{dev_owner.lower()}/{dev_name.lower()}",
          dev_text("Repository"))
    check("[dev] ...at a tag, like the release one",
          ":" in dev_text("Repository").rsplit("/", 1)[-1], dev_text("Repository"))
    # publish.yml only pushes this tag from the dev repo, so the two have to
    # name it identically or the template tracks something never built.
    dev_tag = dev_text("Repository").rsplit(":", 1)[-1]
    check("[dev] ...which the publish workflow actually builds",
          f'IMAGE:{dev_tag}"' in (ROOT / ".github" / "workflows" / "publish.yml").read_text(),
          dev_tag)
    # A raw.githubusercontent URL into a private repo 404s for the Unraid box
    # fetching it, so the dev template carries no TemplateURL at all. The icon
    # is the public repo's copy on purpose, and that one has to resolve.
    check("[dev] it claims no template URL it cannot serve",
          not (dev_text("TemplateURL") or ""), dev_text("TemplateURL"))
    dev_icon = published_file(dev_text("Icon"))
    check("[dev] the icon is the published repo's, so it loads without a login",
          dev_icon is not None and dev_icon.is_file(), dev_text("Icon"))
    # It ships nowhere, so nothing else would ever notice it had rotted out of
    # the exclude list and started being published. The path is derived rather
    # than written out: publish.sh warns about any published file naming a
    # held-back path, and this test is published.
    check("[dev] publish.sh holds this file back from the public repo",
          DEV_XML.relative_to(ROOT).as_posix()
          in (ROOT / "tools" / "publish.sh").read_text())

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
