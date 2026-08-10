"""What the Notifications form posts, and what gets stored.

This block is deliberately the one part of a config save that cannot fail:
an unreachable webhook must not stop someone changing a deletion threshold,
so bad values are normalized rather than rejected and the "Send test
notification" button is what reports them. That makes the coercion the only
thing standing between a typo and a silently disabled alert, and none of it
was covered — breaking the custom-URL split left the whole suite green.

The textarea is the interesting case. It posts one string, and the split is
on NEWLINES only: a comma is a legal character inside a service URL, so
splitting on it would quietly cut working URLs in half.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _tmpout                                     # noqa: E402
_tmpout.config()
import notify                                      # noqa: E402
import app as A                                    # noqa: E402

ok = True


def check(name, cond, extra=None):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   {extra!r}"))
    ok = ok and cond


def coerce(cfg, saved=None):
    cfg = dict(cfg)
    A._coerce_notify_fields(cfg, saved or {})
    return cfg


# ── The custom-URL textarea ──────────────────────────────────────────────────
c = coerce({"NOTIFY_CUSTOM_URLS": "discord://a\nslack://b"})
check("a newline-separated textarea becomes a list",
      c["NOTIFY_CUSTOM_URLS"] == ["discord://a", "slack://b"], c["NOTIFY_CUSTOM_URLS"])

c = coerce({"NOTIFY_CUSTOM_URLS": "discord://a\r\nslack://b"})
check("...and a browser's CRLF does not leave a stray \\r",
      c["NOTIFY_CUSTOM_URLS"] == ["discord://a", "slack://b"], c["NOTIFY_CUSTOM_URLS"])

# Commas appear inside real service URLs (ntfy topic lists, mailto recipients),
# so they are NOT separators. Pinned because "split on commas too" looks like
# a helpful addition right up until it truncates someone's working URL.
c = coerce({"NOTIFY_CUSTOM_URLS": "ntfy://host/a,b,c"})
check("a comma is part of the URL, not a separator",
      c["NOTIFY_CUSTOM_URLS"] == ["ntfy://host/a,b,c"], c["NOTIFY_CUSTOM_URLS"])

c = coerce({"NOTIFY_CUSTOM_URLS": "  discord://a  \n\n   \nslack://b\n"})
check("blank lines and padding are dropped",
      c["NOTIFY_CUSTOM_URLS"] == ["discord://a", "slack://b"], c["NOTIFY_CUSTOM_URLS"])

check("a list is accepted as-is",
      coerce({"NOTIFY_CUSTOM_URLS": ["a://1", " b://2 "]})["NOTIFY_CUSTOM_URLS"] == ["a://1", "b://2"])
for junk in (5, None, {"a": 1}, True):
    check(f"{type(junk).__name__} is not a URL list — it becomes empty",
          coerce({"NOTIFY_CUSTOM_URLS": junk})["NOTIFY_CUSTOM_URLS"] == [], junk)

# ── Toggles ──────────────────────────────────────────────────────────────────
c = coerce({})
check("an omitted toggle takes its shipped default, not False",
      all(c[k] is v for k, v in notify.TOGGLE_DEFAULTS.items() if k != "NOTIFY_ENABLED"),
      {k: c[k] for k in notify.TOGGLE_DEFAULTS})
check("...including the ones that default ON",
      c["NOTIFY_ON_RUN_SUMMARY"] is True and c["NOTIFY_ON_LOW_SPACE"] is True)

c = coerce({"NOTIFY_ON_RUN_SUMMARY": 0, "NOTIFY_SHOW_MOVIES": "yes"})
check("a posted toggle is coerced to a real bool",
      c["NOTIFY_ON_RUN_SUMMARY"] is False and c["NOTIFY_SHOW_MOVIES"] is True,
      (c["NOTIFY_ON_RUN_SUMMARY"], c["NOTIFY_SHOW_MOVIES"]))
check("the master switch is a bool too",
      coerce({"NOTIFY_ENABLED": "on"})["NOTIFY_ENABLED"] is True)

# ── The low-space threshold ──────────────────────────────────────────────────
# Two spellings of "too small" land in different places, because the coercion
# reads `value or DEFAULT` and 0 is falsy: 0 is treated as "not set" and gets
# the shipped 25, while a negative reaches the max(1, …) floor. Recorded as it
# behaves, not as it reads — the field's min is 1, so neither is reachable from
# the form, and the asymmetry only matters to a scripted POST.
check("0 reads as 'unset' and takes the shipped default",
      coerce({"NOTIFY_LOW_SPACE_GB": 0})["NOTIFY_LOW_SPACE_GB"]
      == notify.NUMBER_DEFAULTS["NOTIFY_LOW_SPACE_GB"],
      coerce({"NOTIFY_LOW_SPACE_GB": 0})["NOTIFY_LOW_SPACE_GB"])
check("a negative is floored at 1 instead",
      coerce({"NOTIFY_LOW_SPACE_GB": -9})["NOTIFY_LOW_SPACE_GB"] == 1)
check("a decimal is truncated to a whole GB",
      coerce({"NOTIFY_LOW_SPACE_GB": 7.9})["NOTIFY_LOW_SPACE_GB"] == 7)
check("text falls back to the default rather than failing the save",
      coerce({"NOTIFY_LOW_SPACE_GB": "lots"})["NOTIFY_LOW_SPACE_GB"]
      == notify.NUMBER_DEFAULTS["NOTIFY_LOW_SPACE_GB"])

# ── Service credentials ──────────────────────────────────────────────────────
c = coerce({"NOTIFY_DISCORD_WEBHOOK": "  https://x  ", "NOTIFY_NTFY_TOPIC": None})
check("a pasted credential is stripped", c["NOTIFY_DISCORD_WEBHOOK"] == "https://x",
      c["NOTIFY_DISCORD_WEBHOOK"])
check("...and a missing one is an empty string, never None",
      all(isinstance(c[k], str) for k in notify.STRING_KEYS),
      {k: c[k] for k in notify.STRING_KEYS if not isinstance(c[k], str)})

# ── A partial POST keeps what was saved ──────────────────────────────────────
# The CLI and any script can post a handful of keys. Treating the absent ones
# as "off" would turn every scripted save into a silent unsubscribe.
saved = {k: "kept" for k in notify.STRING_KEYS}
saved["NOTIFY_ENABLED"] = True
c = coerce({"NOTIFY_DISCORD_WEBHOOK": "https://new"}, saved)
check("a partial POST does not wipe the other services",
      c["NOTIFY_SLACK_WEBHOOK"] == "kept", c["NOTIFY_SLACK_WEBHOOK"])
check("...and does not silently switch notifications off",
      c["NOTIFY_ENABLED"] is True, c["NOTIFY_ENABLED"])
check("...while the posted one is taken", c["NOTIFY_DISCORD_WEBHOOK"] == "https://new")

print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
