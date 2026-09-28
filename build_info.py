"""Which commit this copy of MediaReducer was built from: the short hash the
welcome guide shows beside the version, so two builds of one version can be
told apart.

An image has no .git. The Dockerfile runs this file once while it builds, in
a throwaway stage holding a copy of the build context (`python3 build_info.py
/ctx > /BUILD`), and ships only the answer. A checkout run straight from source
reads its own .git instead. Plain file reads, no git binary: the image has none.

Nothing here shapes stored data, so no store fingerprint watches it.
"""
import re
import sys
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def is_commit(text) -> bool:
    """A commit hash as git writes one: 7 to 40 lowercase hex digits."""
    return bool(_SHA.match(str(text or "").strip()))


def _git_dirs(repo: Path):
    """(the dir holding HEAD, the dir holding the refs) for a checkout, or
    (None, None). A worktree's .git is a FILE naming its own git dir, whose
    refs live in the main repository's (its commondir)."""
    dot = repo / ".git"
    if dot.is_dir():
        return dot, dot
    if dot.is_file():
        m = re.match(r"gitdir:\s*(.+)", dot.read_text(encoding="utf-8").strip())
        if m:
            own = Path(m.group(1).strip())
            own = own if own.is_absolute() else repo / own
            common = own
            pointer = own / "commondir"
            if pointer.is_file():
                c = Path(pointer.read_text(encoding="utf-8").strip())
                common = c if c.is_absolute() else own / c
            return own, common
    return None, None


def commit_of(repo) -> str:
    """The short commit hash a checkout's HEAD is at, or "" when it cannot tell:
    no .git, a worktree whose git dir is out of reach, a ref it cannot find."""
    try:
        own, common = _git_dirs(Path(repo))
        if own is None or common is None:
            return ""
        head = (own / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            loose = common / ref
            if loose.is_file():
                head = loose.read_text(encoding="utf-8").strip()
            else:
                packed = common / "packed-refs"
                head = ""
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref:
                            head = parts[0]
                            break
        return head[:7] if is_commit(head) else ""
    except (OSError, UnicodeDecodeError):
        return ""


if __name__ == "__main__":
    print(commit_of(sys.argv[1] if len(sys.argv) > 1 else "."))
