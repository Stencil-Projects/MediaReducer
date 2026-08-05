"""One rule for every size the app prints, and the two ways it breaks.

A size the app MEASURED — free space, a disk total, the library, one movie,
an amount reclaimed — always shows one decimal, even when it is .0. A size the
user TYPED — a Headroom target, a Redline floor, a Library Size Cap — is whole
GB, quoted as entered. The point is that one reading cannot appear as "400" on
one screen and "400.0" on another.

Two failure modes it guards against:

  • A missing reading rendering as a confident zero. Number(None)/float("") sit
    a hair away from 0, and "0.0 GB free" does not read as "no measurement" —
    it reads as a full disk, which is the one thing the user would act on.
  • The empty-state fallbacks drift from the formatter. The lifetime-reclaimed
    figure has two sources — a measured label and a hardcoded zero for "nothing
    yet" — so a precision change on one side alone prints the same zero as
    "0 GB" or "0.0 GB" depending on which path set it.

The JS twins (prMeasuredGb / prCommaNum) are exercised by the browser tests;
what is checked here is the server side plus the template literals that have to
agree with it.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MEDIAREDUCER_CONFIG",
                      str(Path(tempfile.mkdtemp()) / "config.json"))

import app as A  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "   " + str(extra)))


# ── Measured sizes: one decimal, always ─────────────────────────────────────
m = A._tpl_measured_gb
for value, want in [(1234.56, "1,234.6"), (400, "400.0"), (400.0, "400.0"),
                    (0, "0.0"), (-5, "-5.0"), ("400", "400.0"), (0.04, "0.0")]:
    check(f"measured {value!r} -> {want}", m(value) == want, m(value))

# ── A missing reading is a dash, never a fabricated zero ────────────────────
for value in (None, "", "abc", float("nan")):
    check(f"a missing reading ({value!r}) does not become a number",
          m(value) == "—", m(value))

# ── Settings: whole GB, quoted as entered ───────────────────────────────────
c = A._tpl_commafy
for value, want in [(500, "500"), (1500.7, "1,501"), (12730, "12,730")]:
    check(f"setting {value!r} -> {want}", c(value, 0) == want, c(value, 0))

# ── Reclaimed sizes carry one decimal at every magnitude ────────────────────
# Precision by magnitude — two decimals between 1 and 10 GB, none below 1 —
# would mix three precisions in one column.
r = A._format_reclaimed_size
for by, want in [(5_000_000_000, "5.0 GB"), (32_400_000_000, "32.4 GB"),
                 (1_500_000_000_000, "1.5 TB"), (512_000_000, "512.0 MB"),
                 (0, "0.0 GB"), (None, "0.0 GB")]:
    check(f"reclaimed {by} -> {want}", r(by) == want, r(by))
check("every reclaimed size has exactly one decimal",
      all(re.search(r"\.\d(?!\d)", r(b)) for b in
          (5_000_000_000, 32_400_000_000, 1_500_000_000_000, 512_000_000, 0)))

# ── The empty-state fallbacks agree with the formatter ──────────────────────
# The "nothing reclaimed yet" zero is written as a literal in three places and
# must match what the formatter produces for zero, or one zero prints two ways.
zero = r(0)
for rel in ("templates/dashboard.html", "app.py"):
    text = (ROOT / rel).read_text()
    stale = re.findall(r"""['"]0 GB['"]""", text)
    check(f"{rel} has no stale '0 GB' literal beside a '{zero}' formatter",
          not stale, stale)

# ── The notification formats a byte count the same way the dashboard does ───
# These are two implementations of one fact, and they diverge easily: divide by
# 1024**3 on one side and 1e9 on the other, and a run the dashboard reports as
# freeing 32.3 GB is announced on your phone as 30.1 GB, with 1.5 TB arriving as
# "1397.0 GB". Same bytes, two answers, and the wrong one leaves the house.
#
# They cannot be shared — app imports notify, so notify importing app would be
# circular — so they are pinned against each other here, the way tests/parity
# pins the engine's scoring against the Explorer's copy of it.
import notify as N  # noqa: E402

_SIZES = [0, 1, 999_999, 1_000_000, 512_000_000, 1_000_000_000, 8_000_000_000,
          32_300_000_000, 100_000_000_000, 999_000_000_000, 1_000_000_000_000,
          1_500_000_000_000, 38_000_000_000]
mismatched = [(b, r(b), N._fmt_size(b)) for b in _SIZES if r(b) != N._fmt_size(b)]
check("the notification and the dashboard format a byte count identically",
      not mismatched, mismatched[:4])
# Name the unit outright: a GiB divisor passes a "both agree" test the moment
# someone changes both, and decimal GB is what the disk figures use.
check("...in decimal GB, matching every other size in the app",
      N._fmt_size(32_300_000_000) == "32.3 GB", N._fmt_size(32_300_000_000))
check("...including the TB rollover", N._fmt_size(1_500_000_000_000) == "1.5 TB",
      N._fmt_size(1_500_000_000_000))
for junk in (None, "", "abc", float("nan")):
    check(f"a junk size ({junk!r}) formats without raising",
          isinstance(N._fmt_size(junk), str), N._fmt_size(junk))

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
