"""A test that replaces a function must accept the calls the real one accepts.

Most tests here work by monkeypatching: `A._space_threshold_state = lambda cfg,
disk, lib: ...` puts a stand-in where the app expects a function, so the test
can drive a branch without a media server behind it. The stand-in is a promise
about a signature, and nothing has ever checked that promise — so a parameter
added to the real function leaves every stub of it a call away from TypeError,
and the failure surfaces in whatever unrelated assertion happened to run first.

That is not theoretical. `_tv_season_relpaths` grew a fourth argument and the
break showed up as test_review_fixes dying inside `_delete_tv_season`, three
files from the stub that caused it.

The rule is measured against the calls the SHIPPED code actually makes, not
against the full signature. Both halves of that matter. Checking the signature
alone flags a stub for not answering to a parameter name nobody passes by name,
which is noise; checking the call sites finds exactly the stubs that would
raise, and finds them wherever the call lives. `**kwargs` is the honest escape
for a stub that returns a constant and does not care which flags it was handed.

Aliases come from each file's own imports, so `import app as A` and `import
app` are both followed and a new alias needs nothing added here.
"""
import ast
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_OUT = tempfile.mkdtemp(prefix="mr-stubsig.")
os.environ["MEDIAREDUCER_CONFIG"] = str(Path(_OUT) / "config.json")
os.environ.setdefault("MEDIAREDUCER_LIBRARY", _OUT)
json.dump({"OUTPUT_DIR": _OUT}, open(os.environ["MEDIAREDUCER_CONFIG"], "w"))

import app          # noqa: E402
import db           # noqa: E402
import engine       # noqa: E402
import notify       # noqa: E402
import run_issues   # noqa: E402
import shared       # noqa: E402

MODULES = {"app": app, "db": db, "engine": engine, "notify": notify,
           "run_issues": run_issues, "shared": shared}

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


def aliases(tree):
    """{local name: module} for the modules this file imports."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name in MODULES:
                    out[a.asname or a.name] = MODULES[a.name]
    return out


SHIPPED = ("app.py", "engine.py", "db.py", "notify.py", "shared.py",
           "run_issues.py", "cli.py", "entrypoint.py")


def call_shapes():
    """{function name: {(positional count, keyword names) it is called with}}.

    Matched on the called NAME, so `_space_threshold_state(...)`,
    `A._space_threshold_state(...)` and a call through any other attribute all
    count. Over-matching is the safe direction here: an extra shape can only
    make a stub required to be wider. A call that splats *args or **kwargs is
    skipped — its shape is not knowable from the source."""
    shapes: dict[str, set] = {}
    for name in SHIPPED:
        path = ROOT / name
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            called = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if not called:
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) \
                    or any(k.arg is None for k in node.keywords):
                continue
            shapes.setdefault(called, set()).add(
                (len(node.args), frozenset(k.arg for k in node.keywords)))
    return shapes


CALLS = call_shapes()


def accepts(lam, n_pos, kwnames):
    """Would this lambda survive a call of that shape?"""
    a = lam.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    optional = set(pos[len(pos) - len(a.defaults):]) if a.defaults else set()
    kwonly = {x.arg for x in a.kwonlyargs}
    kwonly_optional = {x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
    if not a.vararg and n_pos > len(pos):
        return False
    named = set(pos[n_pos:]) | kwonly
    if not a.kwarg and not kwnames <= named:
        return False
    filled = set(pos[:n_pos]) | (kwnames & set(pos))
    unfilled = [p for p in pos if p not in filled and p not in optional]
    missing_kwonly = [k for k in kwonly if k not in kwnames and k not in kwonly_optional]
    return not unfilled and not missing_kwonly


problems, checked, unused = [], 0, 0
for f in sorted(ROOT.joinpath("tests").rglob("*.py")):
    src = f.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        continue
    mods = aliases(tree)
    if not mods:
        continue
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda)):
            continue
        for tgt in node.targets:
            if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)):
                continue
            mod = mods.get(tgt.value.id)
            fn = getattr(mod, tgt.attr, None) if mod is not None else None
            if not callable(fn):
                continue
            shapes = CALLS.get(tgt.attr)
            if not shapes:
                unused += 1       # nothing in the shipped code calls it by name
                continue
            checked += 1
            bad = sorted(s for s in shapes if not accepts(node.value, *s))
            if bad:
                how = "; ".join(
                    f"{n} positional" + (" + " + ", ".join(f"{k}=" for k in sorted(kw))
                                         if kw else "")
                    for n, kw in bad)
                problems.append(
                    f"{f.relative_to(ROOT)}:{node.lineno}  {tgt.value.id}.{tgt.attr}\n"
                    f"          the code calls it with: {how}\n"
                    f"          real: {tgt.attr}{inspect.signature(fn)}\n"
                    f"          stub: {src.splitlines()[node.lineno - 1].strip()[:96]}")

# The counts are asserted too. This finds stubs by pattern-matching test source
# and call shapes by pattern-matching shipped source; either pattern silently
# ceasing to match would report a clean run having checked nothing.
check(f"{checked} stubs stand in for a function the code actually calls",
      checked > 100, checked)
check(f"{len(CALLS)} distinct call names were read out of the shipped modules",
      len(CALLS) > 500, len(CALLS))
check("every stub survives every call the shipped code makes", not problems)
for p in problems:
    print("      " + p)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
