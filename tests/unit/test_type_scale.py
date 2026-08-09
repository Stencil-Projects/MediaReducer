"""Every font size comes from the shared type scale.

Sizes used to be written per rule, and drifted: 31 distinct values across the
templates, with seven of them inside a single 0.8px band — differences too
small to read as hierarchy and just large enough to look unsteady. The
Filtering & Scoring page was worse than untidy, being built entirely in px: it
alone ignored a reader's browser font-size setting while every other page
honoured it.

This is a lint, not a rendering check — it reads the templates rather than a
browser, so it runs in the fast tier and fails on the line that reintroduces
the problem. The rendered side is covered by the pages' own e2e tests.
"""
import re, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


base = (ROOT / "templates" / "base.html").read_text()
scale = set(re.findall(r'(--fs-[a-z0-9]+):', base))
check("base.html defines a type scale", len(scale) >= 6, sorted(scale))

# Anything below 1rem is body text, and body text is what drifted. At 1rem and
# up the sizes are display figures and page titles — few, far apart, and each
# deliberately its own; a scale there would be invented rather than observed.
literal = []
px_literal = []
for f in TEMPLATES:
    for m in re.finditer(r'font-size:\s*([^;{}"\']+)', f.read_text()):
        raw = m.group(1).strip()
        if raw.startswith("var(--fs") or raw.startswith("var(--log-line"):
            continue
        rem = re.fullmatch(r"([\d.]+)rem", raw)
        px = re.fullmatch(r"([\d.]+)px", raw)
        where = f"{f.name}: {raw}"
        if px:
            px_literal.append(where)
        elif rem and float(rem.group(1)) < 1:
            literal.append(where)

check("no font size below 1rem bypasses the scale", not literal, literal)
# px is the accessibility half of it: a reader who sets a larger default font
# gets nothing from a px size, so one page would stay small while the rest grew.
check("no font size is written in px", not px_literal, px_literal)

# Inline styles are the other way spacing and type drift — they are invisible
# to a stylesheet audit, so they have to reference the scale too.
inline = []
for f in TEMPLATES:
    for m in re.finditer(r'style="([^"]*font-size[^"]*)"', f.read_text()):
        if "var(--fs" not in m.group(1):
            inline.append(f"{f.name}: {m.group(1)}")
check("inline font sizes use the scale too", not inline, inline)

# The log terminal sizes its off-screen placeholder from the same token it sets
# line-height from. They have to agree EXACTLY — a half-pixel gap put "jump to
# latest" thousands of pixels above the real bottom of a long log — and only
# one shared value guarantees that, since a line box is rounded to 1/64px and
# an equivalent calc() is not.
check("the log's line box and its intrinsic-size hint share one token",
      "line-height: var(--log-line)" in base
      and "contain-intrinsic-size: auto var(--log-line)" in base)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
