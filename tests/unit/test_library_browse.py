"""The folder picker must speak one path language.

/library is the namespace the config, the browse endpoint's query string, and
every path on screen are written in. Where that lands after symlinks is a
different thing, and a library root that is a symlink or a bind is routine on
Unraid and Synology. The endpoint answered in the RESOLVED namespace while
accepting only the /library one, so what it handed the browser it could not take
back: with /library behind a link, every folder in it 404'd on the first click.

Containment is still judged on the resolved form, and that is deliberate rather
than incidental: a branch symlinked to somewhere outside the library resolves
out of it, and engine.is_safe_to_delete refuses those on exactly the same test
("must still resolve inside /library"). Letting the picker walk into one would
let someone configure a monitored path the engine can never clean. So the two
cases below have different right answers, and both are pinned.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MEDIAREDUCER_CONFIG", tempfile.mktemp())
import app as A  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond


def build(link_the_root):
    """A library holding a real branch and one symlinked out of the library,
    reached either directly or through a symlinked root."""
    td = Path(tempfile.mkdtemp(prefix="mr-browse."))
    real = td / "real"
    (real / "Movies" / "Heat (1995)").mkdir(parents=True)
    (td / "elsewhere" / "UHD").mkdir(parents=True)
    (real / "4K").symlink_to(td / "elsewhere" / "UHD")
    if link_the_root:
        (td / "library").symlink_to(real)
        return td, td / "library"
    return td, real


def browse(root, path=None):
    A.FILESYSTEM_CHECK_PATH = str(root)
    args = {"path": path} if path is not None else {}
    with A.app.test_request_context("/api/library/browse", query_string=args):
        resp = A.api_library_browse()
    body, status = (resp if isinstance(resp, tuple) else (resp, 200))
    return body.get_json(), status


for label, linked_root in (("plain root", False), ("symlinked library root", True)):
    _td, root = build(linked_root)

    top, status = browse(root)
    check(f"{label}: the root lists", status == 200 and top.get("ok"), top)
    listed = {d["name"]: d["path"] for d in top.get("dirs", [])}
    check(f"{label}: every child is named in the library's own namespace",
          listed and all(v.startswith(str(root) + "/") for v in listed.values()), listed)

    # The regression: hand a listed path straight back, as the picker's click
    # handler does. A real branch must survive the round trip.
    body, status = browse(root, listed.get("Movies"))
    check(f"{label}: clicking a real folder is accepted back",
          status == 200 and body.get("ok"), f"{status} {body}")
    check(f"{label}: ...and it lists in that same namespace",
          body.get("path") == str(root) + "/Movies", body.get("path"))
    check(f"{label}: ...and its parent is the library root, not the link target",
          body.get("parent") == str(root), body.get("parent"))

    # A branch symlinked OUT of the library stays refused: the engine will not
    # delete anything that resolves outside /library, so a picker that walked
    # into one would only let someone configure a path nothing ever cleans.
    _out, status = browse(root, listed.get("4K"))
    check(f"{label}: a branch pointing outside the library is refused",
          status in (400, 404), f"{status} {_out}")
    _out, status = browse(root, str(Path(_td) / "elsewhere"))
    check(f"{label}: an outright outside path is refused", status in (400, 404), status)
    _out, status = browse(root, str(root) + "/../elsewhere")
    check(f"{label}: ..-escape is refused", status in (400, 404), status)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
