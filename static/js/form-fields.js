/* Form field helpers, shared by Configuration and Filtering & Scoring.
   Loaded from <head> so both pages have them before their own script runs.
   Every script on the page is deferred, so that is plain document order. */

/* ── Form field helpers, shared by Configuration and Filtering & Scoring.
   Defined in <head> because the pages' inline scripts call them at parse
   time (initial validation/populate), before the footer script runs. ── */

// Lock a region of settings for a run: ghost it and disable every control
// inside. Shared by both pages, so a control added later is covered without
// anyone remembering to add it to a list.
//
// Only re-enables what IT disabled (data-run-locked), because a control
// switched off for its own reason must stay off afterwards. Anything inside
// [data-run-lock-exempt] is left alone: force-stop has to work during
// exactly the state that locks everything else.
//
// The dimming is CSS opacity, which multiplies down and cannot be undone by
// a descendant, so an exempt box would be grayed out by whatever wrapper
// above it got dimmed. Marking its ancestor chain lets the rule skip those
// wrappers and dim their other children instead, at any depth.
function _markPassthrough(root, locked) {
  root.querySelectorAll('.run-lock-passthrough')
      .forEach(el => el.classList.remove('run-lock-passthrough'));
  if (!locked) return;
  root.querySelectorAll('[data-run-lock-exempt]').forEach((box) => {
    for (let el = box.parentElement; el && el !== root; el = el.parentElement) {
      el.classList.add('run-lock-passthrough');
    }
  });
}

window.prSetRunLock = function (root, locked) {
  if (!root) return;
  root.classList.toggle('section-run-ghost', !!locked);
  _markPassthrough(root, !!locked);
  root.querySelectorAll('input, select, textarea, button').forEach((el) => {
    if (el.closest('[data-run-lock-exempt]')) return;
    if (locked) {
      if (el.disabled) return;             // already off for its own reason
      el.dataset.runLocked = '1';
      el.disabled = true;
      el.setAttribute('aria-disabled', 'true');
    } else if (el.dataset.runLocked === '1') {
      delete el.dataset.runLocked;
      el.disabled = false;
      el.setAttribute('aria-disabled', 'false');
    }
  });
};

/* Trimmed text of an input, '' when the element is missing. */
function prFieldRaw(id) {
  return document.getElementById(id)?.value?.trim() ?? '';
}

/* Finite number from raw text, else null (blank is null, not 0). */
function prNumOrNull(raw) {
  const n = Number(raw);
  return raw !== '' && Number.isFinite(n) ? n : null;
}

function prPositiveNumber(raw) {
  const n = prNumOrNull(raw);
  return n !== null && n > 0;
}

/* A field's value as a number: blank takes blankValue (0 where blank
   means zero, a default where blank means "use the default", null where
   a value is required), anything non-numeric is null. */
function prBlankNumber(id, blankValue = null) {
  const raw = prFieldRaw(id);
  return raw === '' ? blankValue : prNumOrNull(raw);
}

/* Invalid flags show only once armed (first blur or save attempt). Focus
   suppresses NEW flags so a retype never flashes red mid-edit, but an
   already-visible flag stays up while focused until the value is fixed (a
   failed save jumping to the field must not clear it). Pass the
   .field-invalid element as flagEl for that stickiness. */
function prInvalidVisible(invalid, armed, input, flagEl) {
  if (!invalid || !armed) return false;
  if (input && document.activeElement === input) {
    return !!flagEl?.classList.contains('field-invalid');
  }
  return true;
}

/* Blur-armed validation: focus re-runs quietly (no new flag mid-edit),
   clicking off an enabled field validates and shows the result. */
function prWireBlurValidation(inputId, validateFn) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.addEventListener('focus', () => validateFn({ show: false, focus: false }));
  input.addEventListener('blur', () => {
    if (!input.disabled) validateFn({ show: true, focus: false });
  });
}

/* Download text as a file through the browser (a client-side Blob), so
   logs and reports save to the user's machine instead of the container
   filesystem. Returns false when there is nothing to download. */
function prDownloadText(filename, text) {
  const body = String(text ?? '');
  if (!body.trim()) return false;
  let url = null;
  try {
    const blob = new Blob([body], { type: 'text/plain;charset=utf-8' });
    url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return true;
  } catch (_) {
    return false;
  } finally {
    // Revoke on every path — including an exception after createObjectURL —
    // so the object URL never leaks for the page's lifetime.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

/* Timestamp suffix for downloaded filenames, e.g. 2026-07-15_09-42-11. */
function prFileStamp() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`;
}

/* Apply/clear the red field style, its .field-error line, and the aria
   state in one place. */
function prApplyFieldFlag(flagEl, errEl, inputEl, visible) {
  flagEl?.classList.toggle('field-invalid', !!visible);
  errEl?.classList.toggle('show', !!visible);
  inputEl?.setAttribute('aria-invalid', visible ? 'true' : 'false');
}

/* A field that needs attention can be sitting inside a CLOSED section,
   where the only symptom is a Save button that refuses to work. So the
   section's header carries a marker while that is true, saying where to
   open next. Shared by both accordions; each page supplies the selector
   for what counts as an issue, and may refine it with isIssue().

   Resolved by id on every pass rather than held as an element, so this can
   be wired up before the markup it watches exists. */
window.prSectionIssues = function (accordionId, { selector, isIssue } = {}) {
  let queued = false;
  const root = () => document.getElementById(accordionId);

  function hidden(el) {
    if (!el) return true;
    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return true;
    if (el.classList?.contains('d-none')) return true;
    const s = getComputedStyle(el);
    return s.display === 'none' || s.visibility === 'hidden';
  }

  function update() {
    const acc = root();
    if (!acc) return;
    acc.querySelectorAll('.accordion-button').forEach((btn) => {
      if (btn.querySelector('.pr-section-issue')) return;
      const marker = document.createElement('span');
      marker.className = 'pr-section-issue';
      marker.textContent = '!';
      marker.setAttribute('aria-hidden', 'true');
      marker.title = 'This section has a field that needs attention.';
      btn.appendChild(marker);
    });
    acc.querySelectorAll('.accordion-item').forEach((item) => {
      const btn = item.querySelector(':scope > .accordion-header .accordion-button');
      const body = item.querySelector(':scope > .accordion-collapse');
      const flagged = !!body && [...body.querySelectorAll(selector)]
        .some(el => !hidden(el) && (!isIssue || isIssue(el)));
      btn?.classList.toggle('section-has-issue', flagged);
      if (btn) {
        const name = btn.textContent.replace('!', '').trim();
        btn.title = flagged ? `${name} has a field that needs attention.` : '';
      }
    });
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; update(); });
  }

  return { update, schedule };
};

/* Escape text for interpolation into HTML (element text or a
   double/single-quoted attribute). */
function prHtmlEsc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

/* Touch guard for sliders (pairs with the input[type=range] touch-action:
   pan-y rule): pan-y hands vertical gestures to the page scroller, but
   some mobile browsers still jump the slider's value on the INITIAL touch
   before deciding the gesture is a scroll. Remember the value at
   touchstart and restore it when the gesture turns out to be vertical —
   taps (no movement) and horizontal drags keep working untouched.

   Chromium-engine browsers only, deliberately. Firefox for Android does
   not deliver these events for a range input the way Chromium does, so the
   thumb still moves under a scrolling finger there. A pointer-event twin
   covers it and was tried; it is not worth carrying two gesture paths for,
   so Firefox mobile goes without. */
(function prGuardRangeTouchScroll() {
  let g = null;
  const restore = () => {
    if (g && g.el.value !== g.v) {
      g.el.value = g.v;
      g.el.dispatchEvent(new Event('input', { bubbles: true }));
      g.el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };
  document.addEventListener('touchstart', (e) => {
    const el = e.target;
    if (el?.matches?.('input[type="range"]') && e.touches.length === 1) {
      g = { el, v: el.value, x: e.touches[0].clientX, y: e.touches[0].clientY,
            horiz: false, moved: false, scrolling: false };
    }
  }, { capture: true, passive: true });
  document.addEventListener('touchmove', (e) => {
    if (!g || g.horiz || e.touches.length !== 1) return;
    const t = e.touches[0];
    const dx = Math.abs(t.clientX - g.x);
    const dy = Math.abs(t.clientY - g.y);
    if (!g.scrolling && dx < 6 && dy < 6) return;   // no clear intent yet
    g.moved = true;
    if (!g.scrolling && dx > dy) { g.horiz = true; return; }  // deliberate slide
    // Vertical: snap back IMMEDIATELY — mid-scroll, not at finger-up — and
    // keep undoing any further jumps while this scroll gesture continues.
    g.scrolling = true;
    restore();
  }, { capture: true, passive: true });
  // touchcancel = the scroller took the gesture (the common pan-y path);
  // touchend after unclassified vertical movement = a short flick. A final
  // restore covers a jump that landed after the last touchmove.
  document.addEventListener('touchcancel', () => { if (g && !g.horiz) restore(); g = null; }, true);
  document.addEventListener('touchend', () => {
    if (g && !g.horiz && g.moved) restore();
    g = null;
  }, true);
})();

/* Thousands-separated number (up to maxFrac decimals); mirrors the
   server's commafy filter so live updates match the initial render.
   Non-numeric passes through so a partial entry isn't mangled. */
function prCommaNum(v, maxFrac = 1) {
  const n = Number(String(v).replace(/,/g, ''));
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: maxFrac }) : String(v);
}

/* A MEASURED size in GB, always to one decimal — the JS twin of the
   measured_gb template filter.

   The app draws one line between numbers it measured (free space, disk
   totals, library and movie sizes) and numbers the user typed (whole GB).
   Measured figures keep their decimal even when it is .0, so one reading
   never appears as "400" in one place and "400.0" in another. prCommaNum
   is the wrong tool for them: it drops the trailing zero. Settings use
   prCommaNum(v, 0). */
function prMeasuredGb(gb) {
  /* A missing reading is not zero. Number(null) is 0 and Number('') is 0,
     both finite, so a naive guard turns "we have no figure" into a
     confident "0.0 GB free" — which reads as a full disk. Anything that is
     not a real number becomes the em dash the cards already use for an
     unknown value. A genuine 0 still formats as 0.0. */
  if (gb === null || gb === undefined || gb === '') return '—';
  const n = Number(gb);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

/* GB amount for messages ("a run would free ~X GB") — measured, so it
   follows the same rule. */
function prGbAmount(gb) {
  return prMeasuredGb(gb);
}

/* The disk bar, drawn the same way wherever it appears — the Dashboard's
   Storage card and Configuration's Space Thresholds panel both call this.
   Every lookup is scoped to `root` and done by class, so the two pages keep
   their own ids and only have to agree on the markup shape:

     .disk-bar-fill        the used-space fill
     .disk-bar-library     the media library's share of that fill
     .disk-bar-mark--KIND  headroom | redline | cap
     [data-dbl="KIND"]     the legend entry for each, plus "library"

   `s` is { disk, libraryGb, headroomGb, redlineGb, capGb, monitoring }.
   Thresholds come from the caller rather than a global because the two
   pages mean different things by them: the Dashboard draws what is SAVED,
   while Configuration must draw what the pending form would save.
   Returns which keys ended up shown, so a caller can react to it. */
function prRenderDiskBar(root, s) {
  const shown = { library: false, cap: false, headroom: false, redline: false };
  if (!root) return shown;
  const num = v => { const n = Number(v); return Number.isFinite(n) ? n : null; };
  const q = sel => root.querySelector(sel);
  const legend = (key, on) => {
    const e = root.querySelector(`[data-dbl="${key}"]`);
    if (e) e.hidden = !on;
    shown[key] = on;
  };
  const mark = (kind, on) => {
    const e = q(`.disk-bar-mark--${kind}`);
    if (e) e.hidden = !on;
    return e;
  };
  const fillEl = q('.disk-bar-fill');
  const libEl = q('.disk-bar-library');

  /* Nothing monitored: empty the bar and drop every marker and legend key,
     so no reading lingers from before the directories were removed. */
  if (!s.monitoring) {
    if (fillEl) fillEl.style.width = '0%';
    if (libEl) libEl.style.width = '0%';
    ['headroom', 'redline', 'cap'].forEach(k => mark(k, false));
    ['library', 'cap', 'headroom', 'redline'].forEach(k => legend(k, false));
    return shown;
  }

  const disk = s.disk || null;
  const usedPct = disk ? num(disk.pct_used) : null;
  /* The used fill follows any readable disk, even one whose total is not
     usable for the proportional parts below. */
  if (fillEl && usedPct !== null) fillEl.style.width = usedPct + '%';

  const total = disk ? num(disk.total_gb) : null;
  /* A poll can return no disk (the cache is not warmed yet) even though the
     page rendered with a live reading. Leave the last-known bar rather than
     blanking the markers and library while the used fill still shows —
     that mismatch is the flicker worth avoiding. */
  if (total === null || !(total > 0)) return shown;

  const lib = num(s.libraryGb);
  const haveGeom = lib !== null && lib >= 0;

  /* Anchor the library's right edge to the used fill's right edge so the
     two stay flush; clamp its width to the used fill (guards a stale
     library reading larger than used). */
  const usedRight = (usedPct !== null) ? usedPct : (haveGeom ? 100 : 0);
  let libW = 0, libLeft = usedRight;
  if (haveGeom) {
    libW = Math.max(0, Math.min(lib / total * 100, usedRight));
    libLeft = usedRight - libW;
  }
  const libShown = haveGeom && libW > 0;
  if (libEl) {
    libEl.style.left = libLeft + '%';
    libEl.style.width = libW + '%';
    if (libShown) libEl.title = `Media library: ${prMeasuredGb(lib)} GB`;
  }

  /* Headroom and Redline are free-space targets: the used edge at which
     that much free space remains. */
  const freeMark = (kind, targetGb, label, note) => {
    const t = num(targetGb);
    const on = t !== null && t > 0 && t < total;
    const el = mark(kind, on);
    if (on && el) {
      el.style.left = ((total - t) / total * 100) + '%';
      el.title = `${label}: ${prCommaNum(t, 0)} GB free — ${note}`;
    }
    return on;
  };
  const headroomShown = freeMark('headroom', s.headroomGb,
    'Headroom target', 'cleanup keeps at least this much free');
  const redlineShown = freeMark('redline', s.redlineGb,
    'Redline floor', 'emergency cleanup runs when free drops below this');

  /* Library cap: the used edge if the library were trimmed to the cap, i.e.
     the library's base (non-library used) plus the cap. Hidden when the cap
     is unreachable — reaching it would put the used edge past the end of
     the disk, so the line would run off the bar. The reach test is done in
     raw GB so it is exact; the marker itself stays anchored to the
     library's visual base and is clamped to the bar. */
  let capShown = false;
  const capEl = q('.disk-bar-mark--cap');
  if (capEl) {
    const cap = num(s.capGb);
    const usedGb = num(disk.used_gb);
    if (haveGeom && cap !== null && cap > 0 && usedGb !== null) {
      const baseGb = Math.max(0, usedGb - lib);
      if (baseGb + cap <= total) {
        capEl.style.left = Math.min(100, Math.max(0, libLeft + cap / total * 100)) + '%';
        capEl.title = `Library size cap: ${prCommaNum(cap, 0)} GB — `
          + 'cleanup keeps the media library at or under this';
        capShown = true;
      }
    }
    capEl.hidden = !capShown;
  }

  legend('library', libShown);
  legend('cap', capShown);
  legend('headroom', headroomShown);
  legend('redline', redlineShown);
  return shown;
}

/* Scroll an element into view; scroll-margin CSS on the target clears
   the sticky bars. delayMs (after a paint) lets layout/animation settle
   first — and staggers competing on-load scrolls so the later caller
   decides the final position. */
function prScrollTo(el, { delayMs = 0, smooth = true } = {}) {
  if (!el) return;
  const go = () => el.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'start' });
  if (delayMs > 0) window.requestAnimationFrame(() => window.setTimeout(go, delayMs));
  else go();
}
