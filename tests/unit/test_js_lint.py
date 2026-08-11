"""The browser code is JavaScript in files, and a parser reads it.

It used to be 8,400 lines inside <script> tags in Jinja templates, where no
tool could look at it: a template is not JavaScript, so `node --check` and
ESLint both refuse it, and a misspelled name was found by clicking the page it
broke. The files fixed that; this keeps it fixed.

What has to stay true for the split to be worth anything:

  • Every file parses. The cheapest check there is, and impossible before.
  • The templates keep VALUES, not logic. The server-rendered preamble each
    page still carries is what the deferred files read; a function declaration
    back in a template is code that has left the linter's reach again.
  • Every <script src> resolves. A renamed file is a page that loads and does
    nothing, which looks like a bug in whatever feature you notice first.
  • No two files claim one global. These are classic scripts sharing one
    scope on purpose, so a duplicated top-level name is a silent overwrite —
    and ESLint cannot see it, because it only ever reads one file at a time.
  • Every value the server renders is read by the file it was rendered for.
    The other direction (a file reading a name the preamble no longer sets) is
    ESLint's no-undef, which is the one check the split was mainly for.

ESLint itself is the last step. It is not a runtime dependency, so this skips
without it exactly as test_no_undefined_names skips without pyflakes — and CI
installs it for the same reason, or the check quietly does nothing there.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static" / "js"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def node(*args, **kw):
    return subprocess.run(("node",) + args, cwd=ROOT, capture_output=True,
                          text=True, timeout=180, **kw)


_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
_SRC = re.compile(r"<script[^>]*\bsrc=\"([^\"]+)\"")

sources = sorted(JS.glob("*.js"))
check("the browser code lives in files", len(sources) >= 5,
      [p.name for p in sources])

have_node = shutil.which("node") is not None
if not have_node:
    print("SKIP no node on PATH — nothing can parse the browser code here")
else:
    for p in sources:
        r = node("--check", str(p))
        check(f"{p.name} parses", r.returncode == 0,
              (r.stderr or "").strip().splitlines()[:3])


# ── The templates keep values, not logic ────────────────────────────────────
# Two inline blocks are meant to be there and are why this is a rule about
# FUNCTIONS rather than a byte count: base.html sets the theme and wraps fetch
# before first paint, and each page sets the handful of values its render
# decided. Both are straight-line statements. A named function in a template is
# something that wanted to be in a file.
for tpl in TEMPLATES:
    named = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", tpl.read_text(), re.M)
    check(f"{tpl.name} declares no functions of its own", not named, named)

# A bound, so the preamble cannot grow back into a program one value at a time.
# Generous on purpose — it is a ratchet, not a style rule.
inline_total = 0
for tpl in TEMPLATES:
    for block in _INLINE.findall(tpl.read_text()):
        n = len(block.strip().splitlines())
        inline_total += n
        check(f"{tpl.name}'s inline block stays a preamble ({n} lines)", n <= 100, n)
check(f"the templates hold {inline_total} lines of script between them",
      inline_total <= 300, inline_total)


# ── Every script the pages ask for exists ───────────────────────────────────
# The templates fetch these by URL, so a rename is not a NameError anywhere; it
# is a 404 the page carries on past, and the feature in that file is missing.
for tpl in TEMPLATES:
    for src in _SRC.findall(tpl.read_text()):
        m = re.search(r"(?:asset_url|url_for)\(\s*'static'\s*,\s*filename\s*=\s*'([^']+)'"
                      r"|(?:asset_url)\(\s*'([^']+)'", src)
        check(f"{tpl.name} names a script the app can serve", m is not None, src)
        if m:
            rel = m.group(1) or m.group(2)
            check(f"...static/{rel} is there", (ROOT / "static" / rel).is_file(), rel)


# ── One shared scope, so one owner per name ─────────────────────────────────
# The name list comes from eslint.config.mjs rather than a second parser here:
# if the derivation is wrong, it is wrong in both places at once and the lint
# result below is what says so, instead of two subtly different answers.
_GLOBALS_OF = """
import { globalsOf, PAGES, CHROME } from './eslint.config.mjs';
import { readFileSync } from 'node:fs';
const inline = (h) => [...h.matchAll(/<script(?![^>]*\\bsrc=)[^>]*>([\\s\\S]*?)<\\/script>/g)]
  .map((m) => m[1]).join('\\n');
const read = (f) => f.endsWith('.html')
  ? inline(readFileSync('templates/' + f, 'utf8'))
  : readFileSync('static/js/' + f, 'utf8');
const names = (f) => [...globalsOf(read(f))];
console.log(JSON.stringify({
  pages: PAGES.map(([js, tpls]) => [js, tpls]),
  chrome: CHROME,
  names: Object.fromEntries(
    [...CHROME, ...PAGES.flatMap(([js, tpls]) => [js, ...tpls])].map((f) => [f, names(f)])),
}));
"""

scope = {}
if have_node:
    r = node("--input-type=module", "-e", _GLOBALS_OF)
    check("the shared scope can be enumerated", r.returncode == 0,
          (r.stderr or "").strip().splitlines()[-3:])
    if r.returncode == 0:
        scope = json.loads(r.stdout)

if scope:
    # Per PAGE, because that is the real scope: three pages, each built from
    # base.html plus its own template, and nothing else loaded with it. Pooling
    # all five files would call _simulateRequired a clash — Configuration and
    # the Dashboard each render one, and they are never on screen together.
    total = 0
    for page_js, tpls in scope["pages"]:
        if not tpls:
            continue                     # the chrome files, checked in each page below
        loaded = scope["chrome"] + tpls + [page_js]
        owners, clashes = {}, []
        for where in loaded:
            for n in scope["names"][where]:
                if n in owners:
                    clashes.append(f"{n}: {owners[n]} and {where}")
                owners[n] = where
        total = max(total, len(owners))
        check(f"no two of the {len(loaded)} files behind {page_js} claim one global",
              not clashes, clashes[:8])
    # Proof the enumeration found the code rather than an empty set: a page's
    # scope is hundreds of names wide, so a derivation that quietly stopped
    # working would make every check above pass on nothing.
    check(f"...across a shared scope {total} names wide", total > 250, total)

    # Everything the render decides is read by the file it was rendered for.
    # A value nobody reads is work the server did for no one, and a hint that
    # the code that wanted it was deleted or renamed.
    body = "\n".join(p.read_text() for p in sources)
    for tpl in TEMPLATES:
        unread = [n for n in scope["names"].get(tpl.name, [])
                  if not re.search(r"\b" + re.escape(n) + r"\b", body)]
        check(f"{tpl.name}'s server-rendered values are all read", not unread, unread)


# ── ESLint ──────────────────────────────────────────────────────────────────
eslint = ROOT / "node_modules" / ".bin" / "eslint"
if not (have_node and eslint.is_file()):
    print("SKIP eslint is not installed — run: npm install --no-save eslint")
else:
    r = subprocess.run([str(eslint), "."], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    check("the browser code lints clean", r.returncode == 0)
    if r.returncode != 0:
        for line in (r.stdout or r.stderr).strip().splitlines()[:40]:
            print("      " + line)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
