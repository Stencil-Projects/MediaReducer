"""The run-issue vocabulary: one set of categories, one wording, four surfaces.

A run used to report trouble three different ways — a bespoke banner in the log,
a sentence the engine glued onto the progress message, and "Completed with
errors" in the notification. Three descriptions of one event is three chances to
disagree, and none of them said WHAT went wrong or HOW MUCH.

So the categories live in run_issues.py and every surface renders from it. What
is asserted here is the contract that makes that hold:

  * a countable category reports its count ("43 movies skipped"), because the
    number is the fact — "there were errors" is not;
  * a category that can only happen once per run states itself instead, since a
    "1" next to "IMDb ratings download failed" is noise;
  * every engine warning and failure goes through record_issue(), enforced by
    reading engine.py — a hand-picked list is what drifted last time;
  * folding is idempotent, because the engine attaches the folded form to the
    run report (the dashboard is JS and can't fold) and the notifier folds
    whatever it is handed.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import run_issues

ok = True
def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok = ok and cond


# ── The catalog is well-formed ────────────────────────────────────────────
bad = [name for name, spec in run_issues.CATEGORIES.items()
       if spec.get("severity") not in (run_issues.ERROR, run_issues.WARNING)
       or not spec.get("label") or not spec.get("note")]
check("every category has a severity, a label and a fix note", not bad)
check("a countable category also says what happened to the things it counts",
      all(spec.get("verb") for spec in run_issues.CATEGORIES.values() if spec.get("unit")))

# ── Counting vs. stating ────────────────────────────────────────────────────
many = run_issues.summarize(
    [{"category": "identity_mismatch", "detail": f"Movie {i}"} for i in range(43)])
check("a countable category reports HOW MANY, not just that it happened",
      len(many) == 1 and many[0]["count"] == 43
      and many[0]["line"] == "Identity mismatch: 43 movies skipped")
one = run_issues.summarize([{"category": "identity_mismatch", "detail": "Solo"}])
check("...and reads singular when there is one of it",
      one[0]["line"] == "Identity mismatch: 1 movie skipped")

single = run_issues.summarize(
    [{"category": "imdb_download_failed", "detail": "HTTP 503"}])
check("a once-per-run category states itself instead of counting to 1",
      single[0]["line"] == "IMDb ratings download failed — HTTP 503"
      and "1" not in single[0]["line"])
# Two attempts at the same one-per-run problem is still one problem.
twice = run_issues.summarize([{"category": "imdb_load_failed", "detail": "a"},
                              {"category": "imdb_load_failed", "detail": "b"}])
check("...and stays one even if the engine records it twice", twice[0]["count"] == 1)

# ── Ordering and the headline ───────────────────────────────────────────────
mixed = run_issues.summarize([
    {"category": "cap_unreachable"},
    {"category": "delete_failed", "detail": "/a.mkv"},
    {"category": "library_paths_missing", "detail": "/library/x", "count": 2},
    {"category": "identity_mismatch", "detail": "Bob"},
])
check("errors sort above warnings",
      [e["severity"] for e in mixed]
      == [run_issues.ERROR, run_issues.ERROR, run_issues.WARNING, run_issues.WARNING])
check("the headline counts KINDS, since the per-category counts are on the lines",
      run_issues.headline([{"category": "identity_mismatch"},
                           {"category": "identity_mismatch"},
                           {"category": "cap_unreachable"}])
      == "Completed with 1 kind of error and 1 warning")
check("a clean run has no headline at all", run_issues.headline([]) == "")

# ── Folding is idempotent (engine folds, notifier re-folds) ─────────────────
raw = [{"category": "identity_mismatch", "detail": f"M{i}"} for i in range(3)]
once_folded = run_issues.summarize(raw)
twice_folded = run_issues.summarize(once_folded)
check("re-folding an already-folded list changes nothing", once_folded == twice_folded)
check("...and keeps the specifics rather than dropping them",
      twice_folded[0]["details"] == ["M0", "M1", "M2"])

# ── A miscategorised problem is still reported ──────────────────────────────
unknown = run_issues.summarize([{"category": "something_new", "detail": "x"}])
check("an unknown category surfaces as an error rather than vanishing",
      len(unknown) == 1 and unknown[0]["severity"] == run_issues.ERROR
      and "Something new" in unknown[0]["line"])
check("junk entries are ignored without taking the list down",
      run_issues.summarize([None, {}, {"category": ""}, "nope"]) == [])


# ── Nothing in the engine reports trouble outside the vocabulary ────────────
# The old bug was a hand-maintained list that drifted. This is the guard: a new
# failure written as a bare WARNING/ERROR log line would never reach the
# dashboard or the notification, and this test is where that gets caught.
source = (REPO / "engine.py").read_text(encoding="utf-8")
strays = re.findall(r'log(?:_raw)?\(\s*f?"(?:\s*)(WARNING|ERROR)\b[^"]*"', source)
check("no engine warning or failure bypasses record_issue()",
      not strays)
if strays:
    print("   found bare log lines for:", strays)

used = set(re.findall(r'record_issue\(\s*"([a-z_]+)"', source))
unknown_used = sorted(u for u in used if u not in run_issues.CATEGORIES)
check("every category the engine raises is declared in the catalog",
      not unknown_used)
if unknown_used:
    print("   undeclared:", unknown_used)
# The reverse is a real signal too: a category nothing can raise is dead weight
# that will rot, and a reader has no way to tell it from a live one.
unused = sorted(set(run_issues.CATEGORIES) - used)
check("every declared category is actually raised somewhere", not unused)
if unused:
    print("   declared but never raised:", unused)

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
