"""One vocabulary for everything a run can report as wrong.

"Completed with errors" says nothing on its own, and four surfaces — the log,
the dashboard, the notification and the debug report — each have to say what
went wrong. Wording them separately is three chances for them to disagree about
one event.

So the categories live here, and every surface labels them from this table.
`engine.record_issue()` is the only
way to raise one, which is what keeps a new failure from quietly inventing its
own wording in a log line nobody parses.

Each category declares how it should READ, which is the part worth getting
right:

  * `unit` set: the thing can happen to many movies, files or paths, so the
    interesting fact is how many. Renders as "Identity mismatch: 43 movies".
  * `unit` None: it can only happen once in a run (the IMDb download either
    failed or it didn't), so a count would be noise. Renders as the statement
    itself, with whatever detail the engine had.

`note` is the "what do I do about it" sentence. It is deliberately not in the
one-line label: the line is for scanning a list, the note is for the one entry
you stopped on.
"""

ERROR = "error"
WARNING = "warning"

# Declaration order is display order within a severity, roughly "what stopped a
# deletion" before "what degraded the scoring" before "what only affects the
# numbers you're reading".
CATEGORIES = {
    # ── Errors: something the run wanted to do did not happen ───────────────
    "identity_mismatch": {
        "severity": ERROR,
        "label": "Identity mismatch",
        "unit": ("movie", "movies"),
        "verb": "skipped",
        "note": ("Plex and Jellyfin disagree on what movie that file is, so its "
                 "score can't be trusted. NOT deleted. Re-identify it on whichever "
                 "server is wrong."),
    },
    "delete_failed": {
        "severity": ERROR,
        "label": "Deletion failed",
        "unit": ("movie", "movies"),
        "verb": "left in place",
        "note": ("Usually permissions on /library, or the file is open. It stays "
                 "in the deletion plan and the next run retries."),
    },
    "history_write_failed": {
        "severity": ERROR,
        "label": "Deletion history not recorded",
        "unit": ("movie", "movies"),
        "verb": "missing from deleted.log",
        "note": ("The file was deleted but not recorded in deleted.log. Check "
                 "that /config is writable."),
    },
    "connection_invalid": {
        "severity": ERROR,
        "label": "Connection settings rejected",
        "unit": ("problem", "problems"),
        "verb": "found",
        "note": "The run stopped before scanning. Fix these on the Configuration page.",
    },
    "imdb_download_failed": {
        "severity": ERROR,
        "label": "IMDb ratings download failed",
        "unit": None,
        "note": ("Scoring falls back to whatever ratings data is already on "
                 "disk. Check the container's internet access."),
    },
    "imdb_load_failed": {
        "severity": ERROR,
        "label": "IMDb ratings file unreadable",
        "unit": None,
        "note": ("Every movie scores as though it had no rating, which changes "
                 "the deletion order. Clear the IMDb cache in Advanced to "
                 "re-download it."),
    },

    # ── Warnings: the run carried on, but you should know ───────────────────
    "library_paths_missing": {
        "severity": WARNING,
        "label": "Monitored path not found",
        "unit": ("path", "paths"),
        "verb": "unreachable",
        "note": ("Configured but not on disk, so its movies are not in this run "
                 "at all. Check the mount."),
    },
    "folder_cleanup_failed": {
        "severity": WARNING,
        "label": "Leftover folder",
        "unit": ("folder", "folders"),
        "verb": "not removed",
        "note": ("The movie was deleted but its now-empty folder could not be "
                 "removed. Harmless, but it accumulates."),
    },
    "snapshot_not_written": {
        "severity": WARNING,
        "label": "Library snapshot not updated",
        "unit": None,
        "note": ("No monitored path was mounted, so the last good snapshot was "
                 "kept instead of being overwritten with an empty one. Nothing "
                 "was deleted."),
    },
    "library_size_failed": {
        "severity": WARNING,
        "label": "Library size unavailable",
        "unit": None,
        "note": ("The on-disk library size could not be measured, so the "
                 "library cap could not be checked this run."),
    },
    "imdb_config_invalid": {
        "severity": WARNING,
        "label": "IMDb setting invalid",
        "unit": None,
        "note": "A saved value was out of range and a default was used instead.",
    },
    "imdb_decompress_failed": {
        "severity": WARNING,
        "label": "IMDb download unreadable",
        "unit": None,
        "note": "The downloaded ratings archive could not be decompressed and was re-fetched.",
    },
    "headroom_unreachable": {
        "severity": WARNING,
        "label": "Headroom target out of reach",
        "unit": None,
        "note": ("Even deleting every eligible movie would not free enough. "
                 "Lower the Headroom target, or protect fewer movies."),
    },
    "cap_unreachable": {
        "severity": WARNING,
        "label": "Library cap out of reach",
        "unit": None,
        "note": ("Even deleting every eligible movie would leave the library "
                 "above its cap. Raise the cap, or protect fewer movies."),
    },
}
# No "cleanup blocked by settings" category: a threshold over the safety
# percentage is a standing configuration state, not something a run hit. The
# app refuses to arm Automatic Cleanup on it, ghosts the manual Cleanup button
# with the reason, and words the Cleanup Targets breach note around it. A
# fourth copy in the run panel said nothing the other three didn't, in the one
# place reserved for what went wrong during the run. The engine logs it.

_ORDER = {name: i for i, name in enumerate(CATEGORIES)}
_SEVERITY_RANK = {ERROR: 0, WARNING: 1}


def severity_of(category) -> str:
    """An unknown category is treated as an error rather than dropped: a
    miscategorised problem should still be loud."""
    return CATEGORIES.get(category, {}).get("severity", ERROR)


def _plural(category, count):
    unit = CATEGORIES.get(category, {}).get("unit")
    if not unit:
        return ""
    return unit[0] if count == 1 else unit[1]


def line_for(category, count=1, detail=""):
    """The one-line form: what the log block, the dashboard list and the
    notification all print for this category."""
    spec = CATEGORIES.get(category)
    label = spec["label"] if spec else str(category).replace("_", " ").capitalize()
    detail = str(detail or "").strip()
    if not spec or not spec.get("unit"):
        # Only-ever-one: the statement IS the information, so a "1" would be
        # noise. The detail carries whatever the engine knew.
        return f"{label}{' — ' + detail if detail else ''}"
    verb = spec.get("verb")
    text = f"{label}: {count:,} {_plural(category, count)}"
    if verb:
        text += f" {verb}"
    return text


def summarize(issues):
    """Fold raw issues into one entry per category, ordered errors-first.

    issues: an iterable of {"category", "detail"?, "details"?, "count"?}.
    Entries of a countable category add up; their details are kept (bounded) so
    a reader can see WHICH movies, not just how many. An unknown category still
    comes through: losing a problem because it was miscategorised is worse than
    printing it plainly.

    Accepting `details` as well as `detail` makes this idempotent: the engine
    attaches the summarized form to the run report (the dashboard can't fold it
    itself, it's JS), and the notifier re-folds whatever it is handed without
    caring which shape it got or dropping the specifics.

    Returns [{category, label, severity, count, line, note, details}].
    """
    folded = {}
    for raw in issues or []:
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "").strip()
        if not category:
            continue
        try:
            count = max(1, int(raw.get("count", 1)))
        except (TypeError, ValueError):
            count = 1
        entry = folded.setdefault(category, {"count": 0, "details": []})
        entry["count"] += count
        incoming = raw.get("details")
        if not isinstance(incoming, (list, tuple)):
            incoming = [raw.get("detail")]
        for detail in incoming:
            detail = str(detail or "").strip()
            if detail and detail not in entry["details"]:
                entry["details"].append(detail)

    out = []
    for category, entry in folded.items():
        spec = CATEGORIES.get(category, {})
        # A one-per-run category folded from several raws (a retry, say) is
        # still one thing; don't let a stray count turn it into "3".
        count = 1 if (spec and not spec.get("unit")) else entry["count"]
        detail = entry["details"][0] if entry["details"] else ""
        out.append({
            "category": category,
            "label": spec.get("label") or category.replace("_", " ").capitalize(),
            "severity": severity_of(category),
            "count": count,
            "line": line_for(category, count, detail),
            "note": spec.get("note", ""),
            "details": entry["details"],
        })
    out.sort(key=lambda e: (_SEVERITY_RANK.get(e["severity"], 0),
                            _ORDER.get(e["category"], len(_ORDER))))
    return out


def counts_by_severity(issues):
    """(errors, warnings): how many CATEGORIES of each, not how many events.
    "2 kinds of error" is the useful headline; the per-category counts are on
    the lines themselves."""
    summary = summarize(issues)
    return (sum(1 for e in summary if e["severity"] == ERROR),
            sum(1 for e in summary if e["severity"] == WARNING))


def headline(issues, *, lead="Completed with"):
    """The one-line verdict for a run that had issues, or "" when it was clean.

    `lead` is a parameter because a run that STOPPED did not complete: the
    failure alert prints the same counts under an opening that says so.
    """
    errors, warnings = counts_by_severity(issues)
    if not errors and not warnings:
        return ""
    parts = []
    if errors:
        parts.append(f"{errors} kind{'' if errors == 1 else 's'} of error")
    if warnings:
        parts.append(f"{warnings} warning{'' if warnings == 1 else 's'}")
    return f"{lead} " + " and ".join(parts)
