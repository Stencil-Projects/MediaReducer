"""Outbound alerting for MediaReducer.

A thin, best-effort wrapper around the `apprise` library. The web app (app.py)
calls in here after a run finishes to deliver a consolidated notification; the
engine never touches this module (it only writes a run-report file that the app
reads), so a slow or dead notification endpoint can never perturb, block, or
abort an actual scan/delete.

Two things this module guarantees:

  1. **It never raises.** Every public entry point swallows its own errors and
     returns a ``(ok, detail)`` tuple. A misconfigured Discord webhook must not
     take down the run worker.

  2. **It never hangs the caller.** The apprise send runs on a daemon thread
     with a bounded join, so an unresponsive server delays nothing beyond the
     timeout (and even then only the notifier thread, not the app).

  3. **It cannot exceed one delivery per destination per
     ``_MIN_SEND_INTERVAL_S``.** Enforced in ``_deliver``, the single point
     every alert and the Config page's test button pass through. See "Rate
     limiting" below for why one floor rather than a per-service table. Known
     trade: the send STAMPS survive a restart (via the injected store) but
     messages held for an open window do not — they hold alert text in memory
     only, so a restart inside a window can drop a held alert. Bounded to that
     window, and consistent with this module being best-effort end to end.

Destinations are assembled from friendly per-service fields (Discord, Telegram,
Slack, Pushover, ntfy, Gotify) plus a free-form list of raw Apprise URLs that
covers everything else apprise supports (email via ``mailto://``, Matrix, Home
Assistant, and ~100 others). Each assembler is independent and fail-soft: one
malformed field is skipped, the rest still send.
"""
import hashlib
import re
import threading
import time
from urllib.parse import urlsplit

import run_issues  # the shared warning/failure vocabulary

# How long (seconds) to wait on the actual network delivery before giving up.
# The run already finished and its results are persisted before we get here, so
# a timeout only means "the alert didn't go out", never data loss.
_SEND_TIMEOUT_S = 20

# Config keys whose values are secrets/URLs (used by the app's debug-report
# sanitizer so tokens never leak into a shared diagnostic dump).
SECRET_KEYS = (
    "NOTIFY_DISCORD_WEBHOOK",
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NOTIFY_TELEGRAM_CHAT_ID",
    "NOTIFY_SLACK_WEBHOOK",
    "NOTIFY_PUSHOVER_USER_KEY",
    "NOTIFY_PUSHOVER_TOKEN",
    "NOTIFY_NTFY_TOPIC",
    "NOTIFY_GOTIFY_TOKEN",
    "NOTIFY_CUSTOM_URLS",
)

# The alert-type toggles, with their defaults. Kept here so app.py can normalize
# a saved config against one source of truth.
# Nothing is delivered on a fresh install; the master switch is off and there
# is no service configured, so these describe what arrives the moment a user
# turns notifications on. The three alert types that report on the library are
# pre-selected; the ones that add detail or widen scope start off, so the first
# alerts are short and say only what happened.
TOGGLE_DEFAULTS = {
    "NOTIFY_ENABLED": False,
    "NOTIFY_ON_RUN_SUMMARY": True,       # scheduled daily Simulate + every cleanup
    "NOTIFY_ON_MARKED_CHANGES": True,    # a space check marks more movies
    "NOTIFY_ON_LOW_SPACE": True,         # free space nears the Redline floor
    "NOTIFY_SHOW_MOVIES": False,         # list movie names in the alerts
    "NOTIFY_ON_ERROR": False,            # flag errored runs / failed runs
    # Monitor Only stays quiet unless opted in: run notifications fire there
    # only with this on. The between-run alerts (marked changes, low space)
    # follow their own toggles in every mode and ignore this entirely.
    "NOTIFY_IN_MONITOR_ONLY": False,
}


def flag(cfg, key) -> bool:
    """A notification toggle's effective value — the single place the default
    for an absent key is decided (TOGGLE_DEFAULTS), so app-side gating and
    message building can never disagree about what "unset" means."""
    return _bool(cfg, key)

# Numeric notification settings (normalized on save like the toggles).
NUMBER_DEFAULTS = {
    "NOTIFY_LOW_SPACE_GB": 25,   # warn when free space is within this of Redline
}

# Free-text service fields (assembled into apprise URLs). Normalized to stripped
# strings on save.
STRING_KEYS = (
    "NOTIFY_DISCORD_WEBHOOK",
    "NOTIFY_TELEGRAM_BOT_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID",
    "NOTIFY_SLACK_WEBHOOK",
    "NOTIFY_PUSHOVER_USER_KEY", "NOTIFY_PUSHOVER_TOKEN",
    "NOTIFY_NTFY_TOPIC", "NOTIFY_NTFY_SERVER",
    "NOTIFY_GOTIFY_SERVER", "NOTIFY_GOTIFY_TOKEN",
)

# Every notification config key (toggles + numbers + strings + the custom-URL
# list). Used by the app to preserve/normalize/round-trip the whole group.
CONFIG_KEYS = (tuple(TOGGLE_DEFAULTS) + tuple(NUMBER_DEFAULTS)
               + STRING_KEYS + ("NOTIFY_CUSTOM_URLS",))

# Bounds on the movie list in a message body, so a 900-movie purge can't produce
# a novel or blow past a service's character limit (Discord ~2000, Telegram
# ~4096). Whichever bound is hit first wins; the rest is "…and N more".
_MAX_ITEMS = 40
_MOVIE_LIST_CHARS = 1400


def _s(cfg, key):
    """A stripped string for a config key (tolerates None/non-str)."""
    return str(cfg.get(key) or "").strip()


def _bool(cfg, key):
    return bool(cfg.get(key, TOGGLE_DEFAULTS.get(key, False)))


# ── Per-service Apprise URL assembly ────────────────────────────────────────
# Each returns a list of apprise URL strings (usually 0 or 1). They must never
# raise — a bad field yields [].

def _discord_urls(cfg):
    # Users copy the webhook URL straight out of Discord; convert it to the
    # native apprise scheme (discord://{id}/{token}).
    hook = _s(cfg, "NOTIFY_DISCORD_WEBHOOK")
    if not hook:
        return []
    m = re.search(r"/webhooks/(\d+)/([A-Za-z0-9_-]+)", hook)
    if m:
        return [f"discord://{m.group(1)}/{m.group(2)}"]
    # Already an apprise-style discord:// URL? pass it through.
    return [hook] if hook.startswith("discord://") else []


def _telegram_urls(cfg):
    token = _s(cfg, "NOTIFY_TELEGRAM_BOT_TOKEN")
    chat = _s(cfg, "NOTIFY_TELEGRAM_CHAT_ID")
    if token and chat:
        return [f"tgram://{token}/{chat}"]
    return []


def _slack_urls(cfg):
    hook = _s(cfg, "NOTIFY_SLACK_WEBHOOK")
    if not hook:
        return []
    m = re.search(r"/services/(T[A-Za-z0-9]+)/(B[A-Za-z0-9]+)/([A-Za-z0-9]+)", hook)
    if m:
        return [f"slack://{m.group(1)}/{m.group(2)}/{m.group(3)}"]
    return [hook] if hook.startswith("slack://") else []


def _pushover_urls(cfg):
    user = _s(cfg, "NOTIFY_PUSHOVER_USER_KEY")
    token = _s(cfg, "NOTIFY_PUSHOVER_TOKEN")
    if user and token:
        return [f"pover://{user}@{token}"]
    return []


def _host_and_secure(server):
    """Split a user-entered server into (host+path, is_https). A bare host or an
    https:// URL is treated as secure; only an explicit http:// is plain."""
    server = server.strip()
    if not server:
        return "", True
    parts = urlsplit(server if "//" in server else f"//{server}")
    secure = (parts.scheme or "https").lower() != "http"
    host = (parts.netloc + parts.path).strip("/")
    return host, secure


def _ntfy_urls(cfg):
    topic = _s(cfg, "NOTIFY_NTFY_TOPIC")
    if not topic:
        return []
    server = _s(cfg, "NOTIFY_NTFY_SERVER")
    if not server:
        # No server → the public ntfy.sh service.
        return [f"ntfy://{topic}"]
    host, secure = _host_and_secure(server)
    if not host:
        return [f"ntfy://{topic}"]
    scheme = "ntfys" if secure else "ntfy"
    return [f"{scheme}://{host}/{topic}"]


def _gotify_urls(cfg):
    token = _s(cfg, "NOTIFY_GOTIFY_TOKEN")
    server = _s(cfg, "NOTIFY_GOTIFY_SERVER")
    if not (token and server):
        return []
    host, secure = _host_and_secure(server)
    if not host:
        return []
    scheme = "gotifys" if secure else "gotify"
    return [f"{scheme}://{host}/{token}"]


def _custom_urls(cfg):
    raw = cfg.get("NOTIFY_CUSTOM_URLS")
    if isinstance(raw, str):
        raw = raw.replace("\r", "\n").split("\n")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(u).strip() for u in raw if str(u).strip()]


_ASSEMBLERS = (
    _discord_urls, _telegram_urls, _slack_urls,
    _pushover_urls, _ntfy_urls, _gotify_urls, _custom_urls,
)


def apprise_urls(cfg):
    """Every configured destination as a list of apprise URL strings. Each
    assembler is isolated so one bad field can't suppress the others."""
    urls = []
    for fn in _ASSEMBLERS:
        try:
            urls.extend(fn(cfg))
        except Exception:
            continue
    # De-dup while preserving order.
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def has_destinations(cfg):
    """True if at least one destination is configured (regardless of the master
    toggle) — used to warn the user that alerting is on but goes nowhere."""
    return bool(apprise_urls(cfg))


# ── Rate limiting ───────────────────────────────────────────────────────────
# One delivery per destination per interval, enforced in _deliver, which every
# alert and the test button pass through. There is no path around it.
#
# One floor rather than a per-service table: every documented per-request limit
# on the supported services is seconds-scale, so a table would list numbers this
# floor already sits under and never decide anything.
#
# It is short because what actually paces MediaReducer is upstream: the tick
# runs every 15 minutes and each alert fires once per state change. The floor is
# only here to catch a caller in a loop, and 10 seconds does that while staying
# invisible to someone clicking the test button.
_MIN_SEND_INTERVAL_S = 10

# A destination inside its window HOLDS the message rather than dropping it —
# an alert that free space is about to hit the Redline floor is worth arriving
# late — but the queue is bounded so a runaway caller can't grow it without
# limit. Past the cap the oldest held messages fall off.
_MAX_HELD_PER_DESTINATION = 20

# A held message carries no note explaining why it is late. The delay is a
# MediaReducer implementation detail, and the alert's own content is what the
# reader needs — a line about an internal rate limit is noise in a phone
# notification.

_rate_lock = threading.RLock()
_last_sent = {}     # destination key -> wall-clock seconds of the last delivery
_held = {}          # destination key -> [(title, body), ...] waiting out a window
_held_urls = {}     # destination key -> the apprise URL to flush that queue to
_flush_timer = None
_flush_at = None    # wall-clock moment the armed flush timer will fire
_state_io = None    # (read, write) injected by the app; see set_rate_state_store
_state_loaded = False


def _dest_key(url):
    """A destination's identity for rate-limiting. Hashed because these keys are
    persisted, and an apprise URL carries the webhook token."""
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]


def set_rate_state_store(read_fn, write_fn):
    """Persist the per-destination send stamps so the rate limit survives a
    restart — without this the limiter is in-memory only and a container bounce
    clears every cooldown. read_fn() returns the dict last written (or None);
    write_fn(dict) stores it. The dict maps hashed destination keys to
    timestamps, so no webhook URL or token is ever written out."""
    global _state_io, _state_loaded
    with _rate_lock:
        _state_io = (read_fn, write_fn)
        _state_loaded = False


def _load_state_locked():
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    if not _state_io:
        return
    try:
        now = time.time()
        for key, stamp in dict(_state_io[0]() or {}).items():
            stamp = float(stamp)
            # A stamp in the future; the clock moved back, or the store was
            # hand-edited — would strand that destination for as long as the
            # skew lasts. Treat it as no stamp at all.
            if 0 < stamp <= now:
                _last_sent[str(key)] = stamp
    except Exception:
        pass


def _save_state_locked():
    if not _state_io:
        return
    try:
        # Only stamps still inside a window can affect a decision; dropping the
        # rest keeps this from growing with every destination ever configured.
        cutoff = time.time() - _MIN_SEND_INTERVAL_S
        _state_io[1]({k: v for k, v in _last_sent.items() if v > cutoff})
    except Exception:
        pass


def _cooldown_remaining_locked(key, now):
    last = _last_sent.get(key)
    if last is None:
        return 0.0
    elapsed = now - last
    if elapsed < 0:
        return 0.0   # clock moved backwards; don't strand the destination
    return max(0.0, _MIN_SEND_INTERVAL_S - elapsed)


def cooldown_remaining(cfg):
    """Seconds until at least one configured destination can be sent to; 0.0
    when one is ready now. The test button asks first so it can say how long to
    wait instead of reporting a delivery failure."""
    urls = apprise_urls(cfg)
    if not urls:
        return 0.0
    now = time.time()
    with _rate_lock:
        _load_state_locked()
        return min(_cooldown_remaining_locked(_dest_key(u), now) for u in urls)


def _interval_words(seconds):
    """A duration in the largest unit that stays a whole number, so the wording
    survives a change to _MIN_SEND_INTERVAL_S. Reading "every 0 minutes" is how
    a hardcoded unit fails, and it fails silently."""
    seconds = max(1, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = seconds // 60 + (1 if seconds % 60 else 0)
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def cooldown_message(seconds):
    """The one wording for "you are inside the window", so the API and the UI
    can't drift apart."""
    return (f"MediaReducer sends at most one notification per destination every "
            f"{_interval_words(_MIN_SEND_INTERVAL_S)}. Try again in about "
            f"{_interval_words(seconds)}.")


def _merge_held(messages):
    """Fold everything held for one destination into a single message, so a
    window delays an alert but never discards one.

    The result is exactly what would have been sent on time — no note about the
    delay. Why a message is a few seconds late is MediaReducer's problem, not
    something to spend a line of a phone notification on."""
    if len(messages) == 1:
        return messages[0]
    parts = []
    for title, body in messages:
        # Strip the shared "MediaReducer; " prefix; it becomes the heading of
        # the merged message instead of repeating on every section.
        head = re.sub(r"^\s*MediaReducer\s*[—–-]\s*", "", str(title or "")).strip()
        parts.append(f"{head}\n{body}" if head else str(body))
    return (f"MediaReducer — {len(messages)} alerts", "\n\n---\n\n".join(parts))


def _arm_flush_locked(now):
    """Schedule the flush for the SOONEST window still holding something. A
    later hold can have an earlier deadline than the one already armed (a
    destination halfway through its window against one that just started), so
    an armed timer is re-armed rather than left to fire too late."""
    global _flush_timer, _flush_at
    if not _held:
        return
    # +1s so the timer lands just past the window rather than exactly on it.
    due_at = now + min(_cooldown_remaining_locked(k, now) for k in _held) + 1.0
    if _flush_timer is not None and _flush_timer.is_alive():
        if _flush_at is not None and _flush_at <= due_at:
            return                      # already armed for this moment or sooner
        _flush_timer.cancel()
    _flush_at = due_at
    _flush_timer = threading.Timer(max(0.0, due_at - now), _flush_held)
    _flush_timer.daemon = True
    _flush_timer.start()


def _flush_held():
    """Deliver everything whose window has opened — one merged message per
    destination — then re-arm for whatever is still waiting."""
    global _flush_timer, _flush_at
    due = []
    now = time.time()
    with _rate_lock:
        _flush_timer = None
        _flush_at = None
        for key in list(_held):
            if _cooldown_remaining_locked(key, now) > 0:
                continue
            messages = _held.pop(key, [])
            url = _held_urls.pop(key, None)
            if url and messages:
                _last_sent[key] = now   # reserve before sending, as in _deliver
                due.append((key, url, _merge_held(messages)))
        _save_state_locked()
        _arm_flush_locked(now)
    for key, url, (title, body) in due:
        try:
            sent, _detail = _send_now([url], title, body)
        except Exception:   # pragma: no cover - defensive
            sent = False
        if not sent:
            # "Delayed but never dropped" has to survive the send failing too:
            # the merged alert goes back on hold (the reservation above stands,
            # so the retry waits out one cooldown rather than hammering a dead
            # endpoint) and the timer re-arms for it.
            with _rate_lock:
                _held.setdefault(key, []).append((title, body))
                _held_urls.setdefault(key, url)
                _arm_flush_locked(time.time())


# ── Delivery ────────────────────────────────────────────────────────────────

def _send_now(urls, title, body):
    """Hand one message to apprise for the given destinations, with a hard
    timeout. Returns (ok, detail). No rate-limit logic — the caller has already
    decided these destinations may be sent to."""
    try:
        import apprise
    except Exception:
        return False, "apprise is not installed"

    ap = apprise.Apprise()
    added = 0
    for u in urls:
        try:
            if ap.add(u):
                added += 1
        except Exception:
            continue
    if not added:
        return False, "no valid destinations (check the service URLs/tokens)"

    result = {}

    def _run():
        try:
            result["ok"] = bool(ap.notify(body=body, title=title))
        except Exception as e:  # pragma: no cover - defensive
            result["err"] = str(e)

    t = threading.Thread(target=_run, daemon=True, name="notify-send")
    t.start()
    t.join(timeout=_SEND_TIMEOUT_S)
    if t.is_alive():
        return False, f"timed out after {_SEND_TIMEOUT_S}s"
    if result.get("ok"):
        return True, f"sent to {added} destination(s)"
    return False, result.get("err") or "delivery failed (endpoint rejected the message)"


def _deliver(urls, title, body, *, hold=True):
    """Rate-limited best-effort send. Returns (ok, detail). Never raises.

    Each destination gets at most one delivery per _MIN_SEND_INTERVAL_S. A
    destination inside its window HOLDS the message and it goes out — merged
    with anything else waiting for it — as soon as the window opens, so an
    automatic alert is delayed but never dropped.

    hold=False skips instead of holding, for the test button: a test that lands
    after the click says nothing about the wiring the user is standing there
    checking, and refusing tells them so immediately."""
    if not urls:
        return False, "no destinations configured"

    now = time.time()
    with _rate_lock:
        _load_state_locked()
        ready, waiting, wait_for = [], [], 0.0
        for u in urls:
            remaining = _cooldown_remaining_locked(_dest_key(u), now)
            if remaining > 0:
                waiting.append(u)
                wait_for = max(wait_for, remaining)
            else:
                ready.append(u)
        if waiting and hold:
            for u in waiting:
                key = _dest_key(u)
                queue = _held.setdefault(key, [])
                _held_urls[key] = u
                queue.append((title, body))
                del queue[:-_MAX_HELD_PER_DESTINATION]
            _arm_flush_locked(now)
        # Reserve every slot BEFORE the network call. _send_now can sit for up
        # to _SEND_TIMEOUT_S, and a concurrent caller reading a stale stamp in
        # that gap would put a second message inside the same window.
        for u in ready:
            _last_sent[_dest_key(u)] = now
        _save_state_locked()

    if not ready:
        if hold:
            # ok=True: the alert was ACCEPTED and will be delivered when the
            # window opens. Reported as a failure, the app's dispatcher would
            # leave the marked baseline alone for the next tick to announce,
            # and the flushed summary would announce the same marks again.
            return True, (f"held for the rate limit — {len(waiting)} destination(s) "
                          "are inside their window; it will be sent when they open")
        return False, cooldown_message(wait_for)

    ok, detail = _send_now(ready, title, body)
    if waiting:
        detail += (f"; {len(waiting)} held for the rate limit" if hold
                   else f"; {len(waiting)} skipped for the rate limit")
    return ok, detail


def send_test(cfg):
    """Deliver a fixed test message to the configured destinations. Returns
    (ok, detail) for the config page's "Send test notification" button. Sends
    even when NOTIFY_ENABLED is off — the point is to verify wiring.

    Subject to the same rate limit as every automatic alert, and it consumes the
    slot: a test is a real message to a real service, so exempting it would make
    the guarantee untrue exactly when someone is clicking a button repeatedly."""
    return _deliver(
        apprise_urls(cfg),
        "MediaReducer — test alert",
        "This is a test notification from MediaReducer. If you can read this, "
        "alerts are wired up correctly.",
        hold=False,
    )


# ── Message building from a run report ──────────────────────────────────────

def _fmt_size(num_bytes):
    """A byte count as the dashboard would show it.

    DECIMAL GB (10^9), like every other size in MediaReducer — disk figures,
    the library size, the deletion history. Divide by 1024^3 here instead and a
    run the dashboard reports as freeing 32.3 GB is announced as 30.1 GB, and
    1.5 TB arrives as "1397.0 GB" — same bytes, two answers, and the wrong one
    is the one that reaches your phone.

    Deliberately identical to app._format_reclaimed_size, including the TB and
    MB rollovers. The two cannot be shared (app imports notify, so notify
    importing app would be circular), so test_size_format pins them against
    each other the way tests/parity pins the engine against the Explorer."""
    try:
        n = max(0, int(float(num_bytes or 0)))
    except (TypeError, ValueError):
        return "0.0 GB"
    gb = n / 1_000_000_000
    if gb >= 1000:
        return f"{gb / 1000:.1f} TB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = n / 1_000_000
    if mb >= 1:
        return f"{mb:.1f} MB"
    return "0.0 GB"


def _bounded_lines(items, render, char_budget, total=None):
    """Render item lines until the item cap OR the character budget is hit,
    then a '…and N more' tail. Returns (lines, chars_used).

    `total` is the run's TRUE count when the item list itself is capped: the
    report file carries at most 200 items while deleted_count/marked_count
    stay real, so a tail computed from len(items) would contradict the
    header for any run past the cap ("Removed 500" … "and 160 more")."""
    items = items or []
    lines, used, shown = [], 0, 0
    for it in items:
        if shown >= _MAX_ITEMS:
            break
        line = render(it)
        if shown > 0 and used + len(line) + 1 > char_budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    extra = max(len(items), total or 0) - shown
    if extra > 0:
        lines.append(f"…and {extra} more")
    return lines, used


def _by_soonest(items):
    """Marked items sorted soonest delete_on first (ISO dates sort lexically);
    undated marks sink to the end. Stable, so equal dates keep queue order."""
    return sorted(items or [], key=lambda it: str(it.get("delete_on") or "") or "9999")


def _armed(cfg) -> bool:
    """True when the scheduler deletes on its own (Automatic Cleanup). Every
    message's wording turns on this: the same marks and the same dates mean
    "these get deleted" in one mode and "these are only eligible" in the
    other, and a message that doesn't say which is easy to misread."""
    return str(cfg.get("RUN_MODE") or "") == "headroom"


def _mode_note(cfg, *, dates=True, deleted_now=False) -> str:
    """The one line every alert carries, so the mode is never in doubt. Alerts
    that quote delay dates explain what those dates mean in this mode; ones
    that don't (the low-space warning) just state the mode. deleted_now marks a
    report whose run actually removed files, so the note doesn't contradict the
    removals listed right above it."""
    if _armed(cfg):
        return ("Automatic Cleanup is on — marked items are deleted at the "
                "daily run once their date arrives." if dates else
                "Automatic Cleanup is on — the scheduler deletes on its own.")
    if str(cfg.get("RUN_MODE") or "") == "off":
        note = ("The scheduler is paused — this cleanup was started by hand; "
                "nothing runs or deletes on a schedule." if deleted_now else
                "The scheduler is paused — it isn't running or deleting anything.")
    elif deleted_now:
        note = ("Monitor Only — this cleanup was started by hand; the scheduler "
                "still deletes nothing on its own.")
    else:
        note = "Monitor Only — nothing is deleted automatically."
    if dates:
        note += (" Each date is when that item becomes eligible, once "
                 "Automatic Cleanup is turned on.")
    return note


def _marked_sentence(items, armed):
    """The "what happens to these marks" sentence, in the mode's own terms."""
    dates = sorted({str(it.get("delete_on") or "").strip() for it in (items or [])} - {""})
    verb = "They delete " if armed else "They become eligible "
    tail = " unless protected or the rules change."
    if not dates:
        return verb.rstrip() + " after the deletion delay" + tail
    if len(dates) == 1:
        return f"{verb}on {dates[0]}{tail}"
    return f"{verb}{'starting' if armed else 'from'} {dates[0]}{tail}"


def _marked_when(items, armed=True):
    """The header's date phrase for a list of marks: one shared date (the
    normal case) reads " — delete on DATE"; mixed dates read " — delete
    starting DATE" (the soonest); no dates at all reads "". Monitor Only says
    "due"/"due from" instead — the date is real, the deletion isn't."""
    dates = {str(it.get("delete_on") or "").strip() for it in (items or [])}
    dates.discard("")
    if not dates:
        return ""
    if len(dates) == 1:
        one = next(iter(dates))
        return f" — delete on {one}" if armed else f" — due {one}"
    return f" — delete starting {min(dates)}" if armed else f" — due from {min(dates)}"


def _movie_name_blocks(report, debug, budget=_MOVIE_LIST_CHARS, *, armed=True):
    """The "Deleted:" / "Newly marked:" / "Already marked:" name lists for a
    run report. Shared character budget so a huge run can't overflow a
    service's message limit."""
    blocks = []
    d_lines, used = _bounded_lines(
        report.get("deleted_items"),
        lambda it: f"• {it.get('title', '?')} ({_fmt_size(it.get('size'))})",
        budget, total=report.get("deleted_count"))
    if d_lines:
        blocks.append(("Would delete:" if debug else "Deleted:") + "\n" + "\n".join(d_lines))
        budget -= used

    # This run's new marks: soonest-first, the date said once in the header.
    # The overflow tail counts marked_new_count, NOT marked_count: the count
    # field spans the whole marked set, carried marks included, while this list
    # holds only the run's additions, so the set total would tell a quiet day
    # "Newly marked: …and 50 more" with zero names. A report without the field
    # falls back to the list itself (its tail then only fires past the 200-item
    # cap it can no longer size, which lasts one run at most).
    marked_items = _by_soonest(report.get("marked_items"))
    m_lines, used = _bounded_lines(marked_items, lambda it: f"• {it.get('title', '?')}",
                                   max(200, budget), total=report.get("marked_new_count"))
    if m_lines:
        header = (("Would mark for deletion" if debug else "Newly marked")
                  + _marked_when(marked_items, armed or debug) + ":")
        blocks.append(header + "\n" + "\n".join(m_lines))
        budget -= used

    # Marks that predate this run, each with its own due date (these CAN
    # differ — they were made on different days), soonest at the top.
    existing = _by_soonest(report.get("existing_marked_items"))
    e_lines, _ = _bounded_lines(
        existing,
        lambda it: (f"• {it.get('title', '?')}"
                    + (f" — {it['delete_on']}" if it.get("delete_on") else "")),
        max(200, budget))
    if e_lines:
        blocks.append("Already marked:\n" + "\n".join(e_lines))
    return blocks


def _issue_block(report, *, lead=None, show_movies=True):
    """The categorized "what went wrong" list, or "" for a clean run.

    Reads the issue summary the engine attached to the report, so the categories
    and their wording are the ones in run_issues.CATEGORIES — identical to the
    log block and the dashboard's list. A category that can only happen once
    states itself; the rest carry a count, because "43 movies skipped" is the
    fact and "there were errors" is not.

    Falls back to a bare line for an older report with no issue list rather than
    saying nothing: a run that errored must never look clean.
    """
    summary = run_issues.summarize(report.get("issues"))
    if not summary:
        return ("Completed with errors — see the detailed log."
                if report.get("completed_with_errors") else "")
    lines = [run_issues.headline(report.get("issues"),
                                 **({"lead": lead} if lead else {})) + ":"]
    for entry in summary:
        mark = "!" if entry["severity"] == run_issues.ERROR else "-"
        lines.append(f"{mark} {entry['line']}")
        # One-per-run categories carry their specifics on the line already; a
        # counted one gets a single example so the reader has somewhere to start.
        # That example is a movie title or a full library path, so it follows the
        # Movie names opt-in: with it off nothing that names a film leaves the
        # box, and the counts and categories — the point of this block — still do.
        if show_movies and entry["count"] > 1 and entry["details"]:
            lines.append(f"    e.g. {entry['details'][0]}")
    return "\n".join(lines)


def build_run_message(cfg, report):
    """Turn a run report dict into (title, body), or None when run summaries
    are toggled off. One message per run: a plan block (eligible / marked /
    next deletion), a cleanup block when the run could delete, and the movie
    name lists when enabled.

    report keys (all optional, defensively defaulted): mode ("simulate" |
    "cleanup" | "debug_cleanup"), debug, eligible_count, marked_count (new
    this run), marked_total, deleted_count, bytes_freed, redline_only,
    next_event_on, next_event_ripe, deleted_items [{title, size}],
    marked_items [{title, delete_on}] (this run's new marks),
    existing_marked_items [{title, delete_on}] (marks that predate the run,
    attached app-side), completed_with_errors, message,
    seasons {marked_new, held_by_delay, deleted_seasons, skipped, freed_bytes,
    aborted, marked_total} (the run's season side, attached app-side — folded
    into the same numbers, never its own block), and
    storage {free_gb, total_gb, used_gb, library_gb, headroom_gb, redline_gb,
    max_library_gb} for the dashboard-style storage/limits block.
    """
    if not _bool(cfg, "NOTIFY_ON_RUN_SUMMARY"):
        return None
    mode = str(report.get("mode") or "cleanup")
    debug = bool(report.get("debug"))
    armed = _armed(cfg)
    deleted = int(report.get("deleted_count") or 0)
    eligible = report.get("eligible_count")
    marked_total = report.get("marked_total", report.get("marked_count"))
    # Seasons ride the same cleanup, so they report inside the run's own
    # numbers — marked totals and the removed line — not as their own block.
    seasons = report.get("seasons") if isinstance(report.get("seasons"), dict) else None
    if seasons and marked_total is not None:
        marked_total = int(marked_total or 0) + int(seasons.get("marked_total") or 0)

    body_parts = []
    message_used = False

    # Plan block: what the library's deletion plan looks like after this run.
    # One fact per line — joined into a single sentence they are unreadable on a
    # phone the moment there are more than two.
    if eligible is not None:
        rows = [("Eligible in deletion order", f"{int(eligible):,}")]
        if report.get("redline_only"):
            rows.append(("Deletes when", "free space hits the Redline floor"))
        else:
            rows.insert(0, ("Marked for deletion", f"{int(marked_total or 0):,}"))
            if marked_total:
                # Same dates either way, but only an armed scheduler will act
                # on them, so don't promise a deletion Monitor Only won't make.
                if report.get("next_event_ripe"):
                    rows.append(("Next deletion", "at the next daily run" if armed
                                 else "the soonest is already due"))
                elif report.get("next_event_on"):
                    rows.append(("Next deletion", str(report["next_event_on"]) if armed
                                 else f"{report['next_event_on']} (due, not deleted)"))
        body_parts.append("\n".join(f"{k}: {v}" for k, v in rows))
    elif report.get("message"):
        body_parts.append(str(report["message"]).strip())
        message_used = True

    # Cleanup block: only runs that could delete get one. Movies and seasons
    # come out as ONE removed line with the bytes summed — the run deleted
    # things, and what kind each was is the line's detail, not its identity.
    seasons_deleted = int(seasons.get("deleted_seasons") or 0) if seasons else 0
    if mode != "simulate":
        if debug:
            # The engine's dry-run message already reads cleanly and covers the
            # would-mark / would-remove variants, but never repeat it when the
            # plan block above already fell back to it.
            if not message_used:
                msg = str(report.get("message") or "").strip()
                body_parts.append(msg or "Dry run — nothing was changed.")
        elif deleted or seasons_deleted:
            what = []
            if deleted:
                what.append(f"{deleted} movie(s)")
            if seasons_deleted:
                what.append(f"{seasons_deleted} season(s)")
            freed = (int(report.get("bytes_freed") or 0)
                     + int((seasons or {}).get("freed_bytes") or 0))
            body_parts.append(f"Removed {' and '.join(what)} — freed "
                              f"{_fmt_size(freed)}.")
        else:
            body_parts.append("Nothing was removed.")

    # Season facts the numbers above can't carry: a fail-closed season-side
    # failure (the movie side covers the whole target) and skipped seasons
    # pointing at the log. The run-wide space-safety refusal is NOT one of
    # these — it reports once through the run's own message, never here.
    if seasons:
        if seasons.get("aborted"):
            body_parts.append(f"Season deletions skipped this run — "
                              f"{seasons['aborted']}; the movie side covers "
                              "the full target.")
        elif seasons.get("skipped"):
            body_parts.append(f"{int(seasons['skipped'])} season(s) skipped "
                              "— see the log.")

    # Storage block: where the library stands after the run, plus the armed
    # limits — the notification's version of the dashboard's storage card.
    st = report.get("storage") or {}
    if st.get("free_gb") is not None:
        def _gbnum(v):
            # Measured sizes carry one decimal at every magnitude, matching the
            # UI. The limits below are settings, so they stay whole.
            return f"{float(v):,.1f}"
        line = (f"Storage: {_gbnum(st['free_gb'])} GB free of "
                f"{_gbnum(st.get('total_gb') or 0)} GB")
        if st.get("library_gb") is not None:
            line += f" · library {_gbnum(st['library_gb'])} GB"
        limits = []
        if st.get("headroom_gb"):
            limits.append(f"Headroom {float(st['headroom_gb']):,g} GB")
        if st.get("redline_gb") is not None:
            limits.append(f"Redline {float(st['redline_gb']):,g} GB")
        if st.get("max_library_gb") is not None:
            limits.append(f"Library cap {float(st['max_library_gb']):,g} GB")
        if limits:
            line += "\nLimits: " + " · ".join(limits)
        body_parts.append(line)

    # Which mode produced this, stated once, right before the movie lists the
    # dates live in. A Debug Cleanup says its own dry-run line instead. The
    # date explanation is only added when there are dates on show.
    if not debug:
        body_parts.append(_mode_note(
            cfg, dates=bool(marked_total) or bool(report.get("marked_items")),
            deleted_now=bool(deleted)))

    if _bool(cfg, "NOTIFY_SHOW_MOVIES"):
        body_parts.extend(_movie_name_blocks(report, debug, armed=armed))

    # What went wrong, by category and count, in the same words the log and the
    # dashboard use. "Completed with errors; check the log" made the reader go
    # and look; a categorized list is the answer they were going to look for.
    issue_block = _issue_block(report, show_movies=_bool(cfg, "NOTIFY_SHOW_MOVIES"))
    if issue_block and _bool(cfg, "NOTIFY_ON_ERROR"):
        body_parts.append(issue_block)

    if mode == "simulate":
        title = "MediaReducer — daily summary"
    elif debug:
        title = "MediaReducer — cleanup (dry run)"
    else:
        title = "MediaReducer — cleanup"
    return title, "\n\n".join(p for p in body_parts if p)


def build_marked_change_message(cfg, *, new_items, marked_total, redated=False):
    """(title, body) for "a 15-minute space check marked more movies", or None
    when that alert is toggled off or nothing is new.

    `redated` items are EXISTING marks whose delete dates moved (a delay
    setting change) — no movie was newly marked, so "marked N more" would
    assert additions that never happened."""
    if not _bool(cfg, "NOTIFY_ON_MARKED_CHANGES") or not new_items:
        return None
    armed = _armed(cfg)
    n = len(new_items)
    new_items = _by_soonest(new_items)
    lead = (f"A delay change re-dated {n} marked movie(s) "
            if redated else
            f"A space check marked {n} more movie(s) for deletion ")
    body_parts = [lead + f"({int(marked_total or n)} marked in total). "
                  + _marked_sentence(new_items, armed),
                  _mode_note(cfg)]
    if _bool(cfg, "NOTIFY_SHOW_MOVIES"):
        lines, _ = _bounded_lines(new_items, lambda it: f"• {it.get('title', '?')}",
                                  _MOVIE_LIST_CHARS)
        if lines:
            body_parts.append(("Re-dated:" if redated else "Newly marked:")
                              + "\n" + "\n".join(lines))
    return "MediaReducer — movies marked for deletion", "\n\n".join(body_parts)


def build_low_space_message(cfg, *, free_gb, redline_gb, margin_gb, items=None):
    """(title, body) for the approaching-Redline warning, or None when that
    alert is toggled off."""
    if not _bool(cfg, "NOTIFY_ON_LOW_SPACE"):
        return None
    # Armed: crossing the floor triggers the emergency cleanup on the next
    # check — the one deletion that ignores the delay entirely. Monitor Only:
    # the floor is still worth watching, but nothing deletes.
    tail = ("If it crosses, an emergency cleanup deletes from the marked queue "
            "immediately — the Redline ignores the deletion delay."
            if _armed(cfg) else
            "With Automatic Cleanup on, crossing the floor would delete from "
            "the marked queue immediately, ignoring the deletion delay.")
    body_parts = [
        f"Free space is down to {float(free_gb):.1f} GB — within {float(margin_gb):g} GB "
        f"of the {float(redline_gb):g} GB Redline floor. {tail}",
        _mode_note(cfg, dates=False),
    ]
    if _bool(cfg, "NOTIFY_SHOW_MOVIES") and items:
        lines, _ = _bounded_lines(
            items, lambda it: f"• {it.get('title', '?')} ({_fmt_size(it.get('size'))})",
            _MOVIE_LIST_CHARS)
        if lines:
            body_parts.append("First in line:\n" + "\n".join(lines))
    return "MediaReducer — low space warning", "\n\n".join(body_parts)


def build_error_message(cfg, *, message="", stage="", issues=None, names=()):

    """(title, body) for a run that stopped dead.

    Shaped like the dashboard's run panel, because this is the same failure
    being read somewhere else: the engine's sentence first — what stopped
    answering, that nothing was deleted, what to check — then the stage to
    quote when reporting it, then whatever the run had already recorded before
    it died. "A run failed, check the log" sent the reader to go and look; this
    is what they were going to find.
    """
    show_movies = _bool(cfg, "NOTIFY_SHOW_MOVIES")
    line = str(message or "").strip().splitlines()
    line = line[0].strip() if line else ""
    # A few abort sentences name a film — the sample path in a mount mismatch,
    # the protected collections that went missing. The dashboard wants them; a
    # webhook with Movie names off must not have them. The engine reports
    # exactly what it interpolated rather than leaving this to a regex, which
    # would eat the /library mount out of the same sentence and split a
    # collection name on the apostrophe in "Dad's stuff".
    if not show_movies:
        for n in names or ():
            n = str(n)
            if n:
                line = line.replace(n, "[hidden]")
    body_parts = [f"× {line or 'The run stopped before it finished.'}"]
    if stage:
        body_parts.append(f"Failed during {stage} — this stage name appears in the log.")
    # Same categorized list the finished-with-errors summary uses: a run can
    # collect issues and THEN hit something fatal, and those are still the most
    # specific thing anyone has about what it was doing. Its own lead, because
    # that summary opens with "Completed with", which here would be a lie.
    block = _issue_block({"issues": issues}, lead="Recorded before it stopped —",
                         show_movies=show_movies)
    if block:
        body_parts.append(block)
    return "MediaReducer — run failed", "\n\n".join(body_parts)


def _dispatch_built(cfg, built, none_detail):
    if not _bool(cfg, "NOTIFY_ENABLED"):
        return False, "disabled"
    if built is None:
        return False, none_detail
    title, body = built
    return _deliver(apprise_urls(cfg), title, body)


def dispatch_run_report(cfg, report):
    """Build and deliver a run-summary notification. Returns (ok, detail);
    (False, 'disabled'/'nothing to report') when nothing is sent."""
    return _dispatch_built(cfg, build_run_message(cfg, report), "nothing to report")


def dispatch_marked_changes(cfg, *, new_items, marked_total, redated=False):
    """Deliver the "more movies were marked" alert for a 15-minute check."""
    return _dispatch_built(
        cfg, build_marked_change_message(cfg, new_items=new_items, marked_total=marked_total,
                                         redated=redated),
        "nothing to report")


def dispatch_low_space(cfg, *, free_gb, redline_gb, margin_gb, items=None):
    """Deliver the approaching-Redline low-space warning."""
    return _dispatch_built(
        cfg, build_low_space_message(cfg, free_gb=free_gb, redline_gb=redline_gb,
                                     margin_gb=margin_gb, items=items),
        "nothing to report")


def dispatch_error(cfg, message="", *, stage="", issues=None, names=()):
    """Deliver a run-failure alert (a nonzero engine exit the app caught).

    Gated on NOTIFY_ENABLED and NOTIFY_ON_ERROR. With that toggle off a failed
    run says NOTHING — there is no summary to fall back to, because a run that
    stopped has no result to report.
    """
    if not _bool(cfg, "NOTIFY_ENABLED") or not _bool(cfg, "NOTIFY_ON_ERROR"):
        return False, "disabled"
    title, body = build_error_message(cfg, message=message, stage=stage, issues=issues,
                                      names=names)
    return _deliver(apprise_urls(cfg), title, body)
