"""Appearance is per BROWSER, not per install.

It is the one class of setting that says nothing about the library — only how
this browser should draw the page — so it lives in cookies rather than
config.json. Two things follow, and both are checked here: the stamp on <html>
has to be decided from the request (it must be right on the FIRST paint, so it
cannot be applied afterwards), and nothing about it may reach the saved config.

The one place the default is not simply "on" is Firefox mobile, where the
backdrop blur costs enough to be worth pre-empting rather than leaving someone
to find the setting after deciding the app is slow.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
import _tmpout                                    # noqa: E402
_tmpout.config()
import app as A                                   # noqa: E402

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


# ── Which browsers get the different default ─────────────────────────────────
FF_ANDROID = ("Mozilla/5.0 (Android 14; Mobile; rv:109.0) Gecko/121.0 Firefox/121.0")
FF_TABLET = ("Mozilla/5.0 (Android 14; Tablet; rv:109.0) Gecko/121.0 Firefox/121.0")
FF_DESKTOP = ("Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0")
# Firefox on iOS is WebKit under the badge — Apple requires it — so it does not
# carry Gecko's cost and must NOT be matched. This is the case a naive
# "Firefox" + "Mobile" test gets wrong.
FX_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
          "AppleWebKit/605.1.15 FxiOS/121.0 Mobile/15E148 Safari/605.1.15")
CHROME_ANDROID = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                  "Chrome/120.0.0.0 Mobile Safari/537.36")

check("Firefox on Android is matched", A._is_firefox_mobile(FF_ANDROID) is True)
check("...on an Android tablet too", A._is_firefox_mobile(FF_TABLET) is True)
check("Firefox on the desktop is not", A._is_firefox_mobile(FF_DESKTOP) is False)
check("Firefox for iOS is not — it is WebKit, and does not share the cost",
      A._is_firefox_mobile(FX_IOS) is False)
check("Chrome on Android is not", A._is_firefox_mobile(CHROME_ANDROID) is False)
check("a missing User-Agent is not", A._is_firefox_mobile("") is False)


# ── What gets stamped, from cookies + User-Agent ─────────────────────────────
class _Req:
    def __init__(self, cookies=None, ua=""):
        self.cookies = cookies or {}
        self.headers = {"User-Agent": ua}


f = A._appearance_flags(_Req(ua=CHROME_ANDROID))
check("no cookie, ordinary browser: everything on",
      f == {"reduce_effects": False, "glass_pref_off": False, "no_glass": False}, f)

f = A._appearance_flags(_Req(ua=FF_ANDROID))
check("no cookie, Firefox mobile: the glass alone defaults off",
      f == {"reduce_effects": False, "glass_pref_off": True, "no_glass": True}, f)

# The default is a default, not a rule: someone who wants the glass there says
# so once and is not argued with on every page load.
f = A._appearance_flags(_Req({"mr_glass": "on"}, ua=FF_ANDROID))
check("...and an explicit 'on' overrides it", f["no_glass"] is False, f)

f = A._appearance_flags(_Req({"mr_glass": "off"}, ua=CHROME_ANDROID))
check("the glass can be dropped on its own",
      f == {"reduce_effects": False, "glass_pref_off": True, "no_glass": True}, f)

# The OR lives here so no stylesheet rule has to know about both.
f = A._appearance_flags(_Req({"mr_effects": "off"}, ua=CHROME_ANDROID))
check("reduced effects takes the glass with it",
      f["reduce_effects"] is True and f["no_glass"] is True, f)
check("...without claiming the standalone switch was ticked",
      f["glass_pref_off"] is False, f)

f = A._appearance_flags(_Req({"mr_effects": "nonsense", "mr_glass": ""}, ua=FF_DESKTOP))
check("an unreadable cookie falls back to the default rather than raising",
      f == {"reduce_effects": False, "glass_pref_off": False, "no_glass": False}, f)


# ── And none of it reaches the saved config ──────────────────────────────────
# The point of the move: one person's phone must not restyle everyone else's
# browser, and Reset MediaReducer must not have an opinion about either.
import json  # noqa: E402
defaults = json.loads((pathlib.Path(A.__file__).parent / "default_config.json").read_text())
for key in ("REDUCE_VISUAL_EFFECTS", "DISABLE_GLASS_BLUR"):
    check(f"{key} is not a config key any more", key not in defaults)

client = A.app.test_client()
r = client.get("/api/config")
body = r.get_json() or {}
check("the config API does not report appearance either",
      not any(k in body for k in ("REDUCE_VISUAL_EFFECTS", "DISABLE_GLASS_BLUR")),
      sorted(k for k in body if "EFFECT" in k or "GLASS" in k))

# The stamp itself, end to end: it has to be in the HTML the server sends, not
# applied by script afterwards, or the first paint shows what was turned off.
html = client.get("/config", headers={"User-Agent": FF_ANDROID}).get_data(as_text=True)
check("Firefox mobile is served a page already stamped glass-off",
      'data-glass="off"' in html.split("\n", 3)[1])
html = client.get("/config", headers={"User-Agent": CHROME_ANDROID}).get_data(as_text=True)
check("...and an ordinary browser is not", 'data-glass="off"' not in html.split("\n", 3)[1])

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
