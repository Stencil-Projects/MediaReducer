"""Filtering & Scoring help text: every setting explains itself the same way.

The page had drifted into two voices. "Allow movie cleanup" described only
what happens when it is OFF, while its neighbour "Allow TV show cleanup"
described the ON case in a paragraph four times the length — so the matched
pair read as two unrelated features, and the longer one read as a warning.

What is pinned here:

  • Every help line leads with what the setting DOES when it is on. An "Off =
    …" note may follow, never open.
  • The Cleanup scope pair — the same control for the two media types — stays
    a matched pair: same shape, comparable length.
  • No line is a wall. A settings hint people actually read is a sentence or
    two, not a paragraph.
  • The stale "daily pass" story is gone: seasons ride every run, and no
    surface says otherwise (test_tv_cleanup guards the wider nomenclature).

Text, not markup: the assertions run on the rendered strings with tags
stripped, so re-wrapping a line or bolding a word never trips them.
"""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "deletion_score_explorer.html"

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra}"))
    ok = ok and cond

src = TEMPLATE.read_text()

def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

# Every help line on the page, keyed by the id of the control it sits under,
# so a failure names the setting rather than an index. One chunk per settings
# card; the card's first form control names it.
chunks = src.split('<div class="filter-score-card')
helps = {}
for chunk in chunks[1:]:
    cid = re.search(r'\sid="(c-[^"]+)"', chunk)
    if not cid:
        continue
    lines = [_text(h) for h in re.findall(
        r'<div class="filter-score-help"(?![^>]*connection-unlock-note)[^>]*>(.*?)</div>',
        chunk, re.S)]
    if lines:
        helps[cid.group(1)] = {"text": " ".join(lines)}

check("the page's settings help text was found to check",
      len(helps) >= 10, sorted(helps))

# 1) On-behavior first. A line that opens with the off case describes the
#    setting by its absence — the exact drift this test exists to stop.
openers = {cid: h["text"] for cid, h in helps.items()
           if re.match(r"^(off|disabled|when off|turning this off)\b", h["text"], re.I)}
check("no help line opens with what happens when the setting is OFF",
      not openers, openers)

# 2) The Cleanup scope pair is one control in two flavours; it must read that
#    way. Same opening move, and neither side may be several times the other.
movies = helps.get("c-movies-on", {}).get("text", "")
tv = helps.get("c-tv-on", {}).get("text", "")
check("both cleanup-scope halves are present", bool(movies and tv), (movies, tv))
check("...and neither is a paragraph next to a sentence",
      movies and tv and max(len(movies), len(tv)) <= 1.5 * min(len(movies), len(tv)),
      (len(movies), len(tv)))
# The shared shape, not shared wording: each names the one order it joins,
# says marks wait out the delay, and closes with what off means. The second
# of the pair is free to say "the same order" rather than repeat itself.
def _same_shape(t):
    return ("order" in t and "marked" in t
            and "delay" in t and "Off =" in t)
check("...and both name the order, the delay, and what off means",
      _same_shape(movies) and _same_shape(tv), (movies, tv))

# 3) No walls. The longest line on the page is a two-pole slider, which needs
#    a clause per pole; everything else is comfortably shorter.
walls = {cid: len(h["text"]) for cid, h in helps.items() if len(h["text"]) > 260}
check("no help line is a wall of text", not walls, walls)

# 4) Seasons ride every run — nothing may describe a separate daily TV pass.
stale = {cid: h["text"] for cid, h in helps.items()
         if re.search(r"daily pass|TV pass|separate pass", h["text"], re.I)}
check("no help line describes a separate TV pass", not stale, stale)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
