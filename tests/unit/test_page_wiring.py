"""Small promises the pages make, each found wrong in the test lab.

  • The welcome guide's warning named port 7474 as the one to keep off the
    internet. The port is configurable (WEBUI_PORT; the dev template serves on
    7475), so for anyone who moved it the warning named the wrong port.
  • Its first button read "Get started" and only closed the guide; the start
    the quick-start steps describe is Configuration, the other button.
  • The Filtering & Scoring page registers a resize listener in its inline
    state script, before the deferred explorer.js defines renderT. A resize in
    that gap — a phone's address bar showing or hiding while the page loads —
    threw "ReferenceError: renderT is not defined".
  • On a phone the Marked & Eligible tile broke "0 ITEMS · 14 ITEMS" between
    the 14 and its unit, where the Lifetime tile beside it keeps each figure
    whole in a no-wrap chunk.
  • The Lifetime tile counted deleted.log lines — one per FILE, so a TV season
    counts once per episode — and called them items, beside a Marked tile whose
    items are films and seasons. "14 items" there meant 8 films and 2 seasons.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


base = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
# From the modal's opening tag to the <style> block that follows it.
guide = base.split('id="welcome-modal"')[1].split("<style>")[0]
check("the welcome guide names no fixed port", "7474" not in guide)
check("...but still says to keep the port off the internet",
      "Keep its port on your" in guide and "never forward it" in guide)
check("the guide's start button is the one that opens Configuration",
      'onclick="prWelcomeOpenConfig()">Get started in Configuration</button>' in guide)
check("...and the dismiss button says it only closes",
      'data-bs-dismiss="modal">Close</button>' in guide and ">Get started</button>" not in guide)

explorer = (ROOT / "templates" / "deletion_score_explorer.html").read_text(encoding="utf-8")
listener = [ln for ln in explorer.splitlines() if "addEventListener('resize'" in ln]
check("the explorer's early resize listener exists", len(listener) == 1, listener)
check("...and only calls renderT once explorer.js has defined it",
      listener and "typeof renderT==='function'" in listener[0], listener)

dash = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
marked = dash.split('id="pruned-marked-count"')[1].split("</span>\n")[0]
check("the Marked tile keeps each figure whole on a phone",
      marked.count('class="dh-stat-chunk"') == 2, marked[:200])
dash_js = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
redraw = dash_js.split("countEl.innerHTML =")[1].split(";")[0]
check("...and its live redraw keeps the same chunks",
      redraw.count('class="dh-stat-chunk"') == 2, redraw[:200])

unit = dash.split('id="pruned-count-unit">')[1].split("</span>")[0]
check("the Lifetime tile counts files and says so", "'FILES'" in unit and "ITEM" not in unit, unit)
check("...in its live redraw and the history summary too",
      "unit.textContent = safe === 1 ? 'FILE' : 'FILES';" in dash_js
      and "'file has' : 'files have'} been pruned" in dash_js
      and "items have been pruned" not in dash_js)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
