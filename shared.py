"""Pass mechanics shared by the two executors.

The movie pass (engine.py, a subprocess) and the TV season pass (app.py,
in-process) are ONE cleanup with two deletion units. Everything that must
mean the same thing on both sides lives here, imported by both, so the two
passes cannot drift:

  • the pool deficit arithmetic (how far over Headroom / the Library Size
    Cap the disk is — what a day's deletions must cover);
  • the delay clock (the calendar date a mark becomes deletable);
  • the deleted.log line format (one history, both writers);
  • the shared eligibility rungs (IMDb rules, the grace period, the
    unplayed skip) — each ladder keeps its own rung ORDER and its own
    per-type rungs, but the decision inside a shared rung exists once.

Scoring shares the same way through scoring_constants (both curves are
assembled from one set of term helpers there). What legitimately differs
stays out: the deletion unit, the inventory source, the queue storage, and
Redline (movie-only by design).
"""
import datetime as _dt
import time as _time


# ── The pool deficit ─────────────────────────────────────────────────────────

# ── Numeric config domains ───────────────────────────────────────────────────
# ONE table of what each numeric setting may be. The app and the engine have
# genuinely different jobs with it — the app REFUSES a bad value (a save is
# rejected, a hand-edited file is flagged and locks the run buttons), the
# engine COERCES to something usable and records an error that aborts the run —
# so their code stays separate. What must never differ is the rule itself, and
# it used to: each side spelled the bounds out at its own call site, and they
# had drifted apart on five keys. A null HEADROOM_GB was a lockout to the app
# and a silent 0 (redline-only mode) to the engine; REDLINE_GB 0 was refused by
# one and armed by the other; a fractional GRACE_PERIOD_DAYS or DELETE_DELAY_DAYS
# was refused by the app and quietly truncated by the engine; and
# IMDB_RATINGS_MAX_AGE_DAYS disagreed about whether 0 was allowed. The app is
# the gate and the engine is what actually deletes, so a gate that disagrees
# with the actor is not a gate.
#
# nullable means the setting has an OFF state written as null/blank — not that
# a missing key is acceptable (an absent key falls back to its default before
# it ever reaches here).
_NUM = {
    "HEADROOM_GB":              (0,      None, False, False, "must be a number of GB, zero or greater"),
    "REDLINE_GB":               (0,      None, False, True,  "must be a number of GB above zero, or null"),
    "MAX_HEADROOM_PCT":         (0,      100,  False, False, "must be a percentage above 0 and at most 100"),
    "MAX_LIBRARY_GB":           (0,      None, False, True,  "must be a number of GB above zero, or null"),
    "GRACE_PERIOD_DAYS":        (0,      None, True,  False, "must be a whole number of days, zero or greater"),
    # Floor of 1: a marked movie is never deleted the same day, so 0 has no
    # meaning here and the GUI never writes it.
    "DELETE_DELAY_DAYS":        (1,      365,  True,  False, "must be a whole number of days from 1 to 365"),
    "LOG_RETENTION_DAYS":       (0,      None, True,  False, "must be a whole number of days, zero or greater"),
    "IMDB_RATINGS_MAX_AGE_DAYS": (1,     None, False, False, "must be a number of days, one or greater"),
    "SCORE_BALANCE":            (0,      100,  False, False, "must be a number from 0 to 100"),
    "MAX_IMDB_RATING":          (0,      10,   False, True,  "must be a number from 0 to 10, or null"),
    "NEAR_TIE_PTS":             (0.5,    25,   False, True,  "must be a number from 0.5 to 25 points, or null"),
    "TV_SERIES_WATCH_BUMP":     (0,      25,   False, False, "must be a number of points from 0 to 25"),
    "TV_WATCH_WEIGHT":          (100,    200,  False, False, "must be a percentage from 100 to 200"),
    # 0 is the off switch: no season is ever too big. The GUI writes it, so
    # unlike DELETE_DELAY_DAYS's 0 this is a real value, not a lockout.
    "TV_MAX_SEASON_EPISODES":   (0,      999,  True,  False, "must be a whole number of episodes from 0 to 999 (0 turns it off)"),
    "MAX_STALENESS_MONTHS":     (1,      120,  False, False, "must be a number of months from 1 to 120"),
}
# Keys whose minimum is a floor the value must stay ABOVE rather than reach.
# Spelled here rather than as a fractional bound (the engine used 0.000001 for
# the percentage) so the rule reads as what it means.
_EXCLUSIVE_MIN = {"REDLINE_GB", "MAX_HEADROOM_PCT", "MAX_LIBRARY_GB"}

NUMERIC_KEYS = tuple(_NUM)


def numeric_rule(key):
    """(min, max, integer, nullable, text) for a numeric setting, or None when
    the key is not one."""
    return _NUM.get(key)


def is_blank(value) -> bool:
    """The spellings of "not set" a config file or a form field can carry."""
    return value is None or (isinstance(value, str)
                             and value.strip().lower() in ("", "none", "null"))


def coerce_number(key, raw):
    """(value, error) for one numeric setting, against the one rule table.

    value is the number to use — int when it is whole, None for a nullable
    setting that is switched off — and is None whenever there is an error, so a
    caller that ignores the error cannot accidentally use a rejected value.
    error is the human sentence, or "" when the value is fine.

    Bools are refused rather than read as 0/1: JSON true in a threshold is a
    mistake, and silently arming a 1 GB floor from it is the wrong direction.
    """
    rule = _NUM.get(key)
    if rule is None:
        return None, ""
    low, high, integer, nullable, text = rule
    if is_blank(raw):
        return (None, "") if nullable else (None, text)
    if isinstance(raw, bool):
        return None, text
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, text
    # inf/nan slip past every comparison below (nan fails them all, and a
    # bound-less field never catches inf), so they go first.
    if value != value or value in (float("inf"), float("-inf")):
        return None, text
    if key in _EXCLUSIVE_MIN:
        if value <= low:
            return None, text
    elif value < low:
        return None, text
    if high is not None and value > high:
        return None, text
    if integer and not float(value).is_integer():
        return None, text
    return (int(value) if float(value).is_integer() else value), ""


def pool_deficit_gb(used_gb, used_limit_gb, library_gb, cap_gb) -> float:
    """GB a day's deletions must free: the LARGER of the headroom overage
    (used space past its limit) and the Library Size Cap overage (the cap
    measures every monitored directory, movies and TV together). A None
    limit/cap contributes nothing; Redline is an emergency, never sized
    here."""
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    used = _num(used_gb)
    limit = _num(used_limit_gb)
    headroom_deficit = max(0.0, used - limit) if (used is not None and limit is not None) else 0.0
    lib = _num(library_gb)
    cap = _num(cap_gb)
    cap_deficit = max(0.0, lib - cap) if (lib is not None and lib > 0 and cap is not None) else 0.0
    return max(headroom_deficit, cap_deficit)


# ── The delay clock ──────────────────────────────────────────────────────────

def overshoot_note(freed_bytes, target_bytes) -> str:
    """A sentence naming the excess when a deletion freed materially more than
    it needed, else "".

    Deletion units are atomic — a movie file, a whole season — so a run stops
    at the FIRST item that covers what is left, and that item can be far
    bigger than the remainder. The selection step keeps the waste as small as
    the near-tied candidates allow, but when nothing near-tied is small enough
    the excess is real and irreversible, and both numbers were already in the
    log for anyone who thought to subtract them. This says it out loud.

    Deliberately quiet about small change: at least 1 GB over AND at least
    half the target again, so an ordinary run that lands a little past its
    goal says nothing.
    """
    try:
        freed, target = float(freed_bytes), float(target_bytes)
    except (TypeError, ValueError):
        return ""
    excess = freed - target
    if target <= 0 or excess < 1_000_000_000 or freed < target * 1.5:
        return ""
    return (f"freed {freed / 1e9:.1f} GB to satisfy a {target / 1e9:.1f} GB target "
            f"— {excess / 1e9:.1f} GB more than needed. Deletion units are whole "
            "files and whole seasons, so a run stops at the first item that "
            "covers the rest.")


def delete_on_date(marked_at, delay_days):
    """The calendar date a mark becomes deletable: the LOCAL date it was
    marked plus the delay it was marked under. Whole calendar days — a mark
    made at 23:59 with a 1-day delay is deletable at the next day's run, same
    as one made at 00:01. None for an unusable epoch."""
    try:
        days = max(1, int(delay_days))
        t = _time.localtime(float(marked_at))
        return _dt.date(t.tm_year, t.tm_mon, t.tm_mday) + _dt.timedelta(days=days)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def delete_on_str(marked_at, delay_days) -> str:
    """delete_on_date as the ISO string both sides compare against today
    ("" for an unusable epoch)."""
    d = delete_on_date(marked_at, delay_days)
    return str(d) if d else ""


# ── The deletion history ─────────────────────────────────────────────────────

def deleted_log_line(title, path, size_bytes, *, score=None, plays=None,
                     last_played_text=None, media=None, season=None) -> str:
    """One deleted.log line — the format both writers append and one parser
    reads. Carries the WHY (score/plays/last watch) and, for TV, the type
    fields the history view renders. Rationale fields are best-effort: the
    deletion record itself must land even when a value is junk."""
    try:
        size_part = (f" | size_bytes={int(size_bytes)}"
                     if size_bytes is not None and int(size_bytes) >= 0 else "")
    except (TypeError, ValueError):
        size_part = ""
    why = ""
    try:
        if score is not None:
            why += f" | score={round(float(score), 1)}"
        if plays is not None:
            why += f" | plays={int(plays)}"
        if last_played_text is not None:
            why += f" | last_played={last_played_text}"
        if media:
            why += f" | media={media}"
            if season is not None:
                why += f" | season={int(season)}"
    except Exception:
        pass
    return (f"{_time.strftime('%Y-%m-%d %H:%M:%S')} | {title} | "
            f"{path}{size_part}{why}\n")


# ── The wrong-library tripwire ───────────────────────────────────────────────

def wrong_library_problem(checked, bad) -> bool:
    """True when enough sampled movie files disagree with the disk to look
    like the WRONG library — a stale backup or copy mounted at /library, or a
    server database describing different files. Applied to BOTH failure
    shapes: files whose bytes differ from the server's count, and files that
    match nothing at all. A few of either are normal library churn (a quality
    upgrade the server hasn't rescanned; a stale entry whose file was renamed
    or removed) — those warn, never block, because an unresolvable or
    misdescribed entry simply scans as missing and is never deleted.

    The rule the engine pre-check (fails the run) and the configuration check
    (fails the health check) both apply: at least 3 disagreeing files AND more
    than half of the samples."""
    try:
        checked, bad = int(checked), int(bad)
    except (TypeError, ValueError):
        return False
    return bad >= 3 and checked > 0 and bad * 2 > checked


# ── The shared eligibility rungs ─────────────────────────────────────────────
# Each pass keeps its own ladder ORDER and its own per-type rungs (the
# movie switch, TV's scope/latest-season/season-eligibility rules); the
# decision INSIDE a rung both ladders have exists once here.

def imdb_rung(rating, votes, *, in_use, require_votes, cutoff):
    """'no_imdb_data' (IMDb is in use but there isn't enough data to judge),
    'high_rated' (above the cutoff), or None. Movies require a vote count
    too (their ratings come row-by-row and a rating without votes is
    suspect); series ratings come from the dataset, which always carries
    votes, so absence of the rating alone is the gap."""
    if in_use and (rating is None or (require_votes and not votes)):
        return "no_imdb_data"
    if cutoff is not None and rating is not None and float(rating) > float(cutoff):
        return "high_rated"
    return None


def grace_rung(added_at, grace_days, now) -> bool:
    """True = added within the grace period, held back."""
    try:
        added = float(added_at or 0)
        days = float(grace_days or 0)
    except (TypeError, ValueError):
        return False
    return bool(days and added > 0 and (now - added) < days * 86400)


def unplayed_rung(played, skip_unplayed) -> bool:
    """True = never played while the skip-unplayed switch is on."""
    return bool(skip_unplayed) and not played
