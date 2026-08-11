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
