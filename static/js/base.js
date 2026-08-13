/* Chrome shared by every page: theme, time formatting, the toast and confirm
   dialogs, the header badge, the log window and the welcome guide.
   Server-rendered values arrive as the window.PR_* globals base.html sets
   inline; this file is deferred and cannot carry template expressions. */

(function prInitBrowserTimeZone() {
  if (!window.PR_TZ_NEEDS_INIT) return;
  window.PR_TZ_NEEDS_INIT = false;   // don't double-fire within this session
  let tz = '';
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) {}
  fetch('/api/timezone/init', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-MediaReducer': '1' },
    body: JSON.stringify({ tz }),
  }).then(r => (r.ok ? r.json() : null)).then(d => {
    if (d && d.ok && d.time_zone && d.time_zone !== 'auto') {
      window.PR_SERVER_TIME_ZONE = d.time_zone;   // times now render in the detected zone
      // Let the Config page (if open) reflect it in the dropdown without a reload.
      window.dispatchEvent(new CustomEvent('pr-timezone-initialized', { detail: { tz: d.time_zone } }));
    }
  }).catch(() => {});
})();

function prResolvedTimeZone() {
  // The app runs on ONE clock — the operating zone set in Configuration
  // (auto = container clock). All times render in that zone so what you read
  // matches when things actually happen.
  return String(window.PR_SERVER_TIME_ZONE || 'UTC').trim() || 'UTC';
}
function prTimeFormat() {
  const raw = String(window.PR_DISPLAY_TIME_FORMAT || '12h').toLowerCase();
  return raw === '24' || raw === '24h' ? '24h' : '12h';
}
function prTimeQuery() {
  return new URLSearchParams({ time_format: prTimeFormat() }).toString();
}
function prTimeLabel(hhmm) {
  // A canonical 24-hour "HH:MM" — the daily run time, and the Config page's
  // dropdown values — rendered in the chosen display format. Anything that
  // isn't HH:MM comes back untouched.
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm ?? '').trim());
  if (!m) return String(hhmm ?? '');
  const h = Number(m[1]);
  if (!Number.isFinite(h) || h > 23) return String(hhmm);
  if (prTimeFormat() === '24h') return `${String(h).padStart(2, '0')}:${m[2]}`;
  return `${h % 12 || 12}:${m[2]} ${h < 12 ? 'AM' : 'PM'}`;
}
function prZonedHHMM(when) {
  // "HH:MM", always 24-hour, for a Date or epoch-ms in the OPERATING zone.
  // This is the form the scheduler's run-time comparisons use — a sortable
  // key, never a display string, so it ignores the 12/24-hour setting.
  // Returns null when the zone can't be resolved, so each caller keeps its
  // own fallback.
  try {
    return new Intl.DateTimeFormat('en-GB', {
      timeZone: prResolvedTimeZone(),
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).format(when instanceof Date ? when : new Date(when));
  } catch (_) { return null; }
}
function prServerEpochNow() {
  // Server clock estimate: render-time epoch plus client elapsed time.
  return Number(window.PR_SERVER_EPOCH || 0)
       + (Date.now() / 1000 - Number(window.PR_CLIENT_EPOCH_AT_LOAD || 0));
}
function prClockSkewSeconds() {
  // Positive = server clock ahead of this device. Includes page-load latency,
  // so treat small values as noise.
  return Number(window.PR_SERVER_EPOCH || 0) - Number(window.PR_CLIENT_EPOCH_AT_LOAD || 0);
}
function prDateTimeFormat(withSeconds = true) {
  const is24 = prTimeFormat() === '24h';
  return new Intl.DateTimeFormat(undefined, {
    timeZone: prResolvedTimeZone(),
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: is24 ? '2-digit' : 'numeric',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
    hour12: !is24,
    hourCycle: is24 ? 'h23' : 'h12',
  });
}
function prFormatDate(date, withSeconds = true) {
  try { return prDateTimeFormat(withSeconds).format(date); }
  catch (_) { return date.toLocaleString(undefined, { hour12: prTimeFormat() !== '24h' }); }
}
function prFormatEpoch(epochSeconds, withSeconds = true) {
  if (epochSeconds === null || epochSeconds === undefined || epochSeconds === '') return '—';
  const n = Number(epochSeconds);
  if (!Number.isFinite(n)) return '—';
  return prFormatDate(new Date(n * 1000), withSeconds);
}
function prNormalizeDisplayedTimestamps(text) {
  // Defensive browser-side cleanup for raw log lines that still start with the
  // script's server-clock 24-hour timestamp. The backend normally converts log
  // output before sending it; this keeps Dashboard output consistent if any raw
  // text slips through. It only changes what is displayed, never the log file.
  const raw = String(text ?? '');
  if (!raw) return raw;
  const re = /(^|\n)(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})(\s+-\s+|\s+\|\s*)/g;
  return raw.replace(re, (match, lineStart, datePart, hh, mm, ss, sep) => {
    if (prTimeFormat() === '24h') return match;
    const h = Number(hh);
    if (!Number.isFinite(h)) return match;
    const suffix = h >= 12 ? 'PM' : 'AM';
    const h12 = h % 12 || 12;
    return `${lineStart}${datePart} ${h12}:${mm}:${ss} ${suffix}${sep}`;
  });
}

// Only the NEWEST theme swap may lift the transition suppression. Each call
// takes a ticket; a restore from an earlier one is ignored. Without this an
// earlier call's backstop timer fires mid-flip and un-suppresses transitions
// while the new palette is still resolving — html eases alone against
// surfaces that already snapped, which is the seam wearing a different hat.
let _prThemeSwapSeq = 0;
function prSetTheme(theme, opts) {
  const t = theme === 'light' ? 'light' : 'dark';
  const root = document.documentElement;
  const seq = ++_prThemeSwapSeq;
  // The palette lands in ONE frame either way. Both attributes are set
  // together — ours drives the app's tokens, Bootstrap's drives its own — and
  // .pr-theme-swap holds every transition off while they do, so no element is
  // ever caught wearing half of each theme. Reading offsetWidth forces the new
  // colors to be computed before transitions come back.
  const apply = () => {
    root.setAttribute('data-theme', t);
    root.setAttribute('data-bs-theme', t);
    void root.offsetWidth;
  };
  // Two frames: the first is the one the new palette paints in, so the class
  // can only come off after it. requestAnimationFrame may not run in a
  // background tab, so a timer backstops it — leaving the class on would
  // silently kill every transition on the page.
  const restore = () => {
    if (seq === _prThemeSwapSeq) root.classList.remove('pr-theme-swap');
  };
  const settle = () => {
    requestAnimationFrame(() => requestAnimationFrame(restore));
    setTimeout(restore, 400);
  };
  root.classList.add('pr-theme-swap');
  // A deliberate flip (the toggle) cross-fades; syncing on load must not.
  // Where view transitions are missing the flip is simply instant — the same
  // page, one frame sooner, never a seamed one.
  const wantsFade = !!(opts && opts.animate)
    && typeof document.startViewTransition === 'function'
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    // Reduce visual effects covers this too: the cross-fade is the largest
    // animation the app runs — a snapshot of the whole page fading over
    // another — and it is decoration. The palette still lands in one frame,
    // which is the part that keeps the page from wearing both themes at once.
    && document.documentElement.getAttribute('data-effects') !== 'off';
  if (wantsFade) {
    const vt = document.startViewTransition(apply);
    vt.finished.then(settle, settle);
  } else {
    apply();
    settle();
  }
  try { localStorage.setItem('pr-theme', t); } catch (e) {}
  const label = document.getElementById('theme-toggle-label');
  const btn = document.getElementById('theme-toggle');
  // Button advertises the theme you'll switch TO.
  if (label) label.textContent = (t === 'light') ? 'Dark' : 'Light';
  if (btn) btn.title = (t === 'light') ? 'Switch to dark theme' : 'Switch to light theme';
}
function prToggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  prSetTheme(current === 'light' ? 'dark' : 'light', { animate: true });
}
// Sync the label/title with whatever the pre-paint script already applied.
document.addEventListener('DOMContentLoaded', function () {
  prSetTheme(document.documentElement.getAttribute('data-theme') || 'dark');
});

// ── First-run welcome / About & quick start ─────────────────────────────────
// Auto-opens once for a fresh install; any dismissal (Get started, ×, Esc,
// backdrop) persists the seen flag so it never nags again. The "?" header
// button reopens it on demand without touching the flag (already seen).

function prWelcomeSeenPending() {
  try {
    const ts = Number(sessionStorage.getItem('pr-welcome-seen-pending') || 0);
    return !!ts && Date.now() - ts < 120000;
  } catch (_) {
    return false;
  }
}

function prSetWelcomeSeenPending() {
  try { sessionStorage.setItem('pr-welcome-seen-pending', String(Date.now())); } catch (_) {}
}

function prClearWelcomeSeenPending() {
  try { sessionStorage.removeItem('pr-welcome-seen-pending'); } catch (_) {}
}

window.PR_WELCOME_NEEDED = window.PR_WELCOME_SERVER_NEEDED && !prWelcomeSeenPending();
if (!window.PR_WELCOME_SERVER_NEEDED) prClearWelcomeSeenPending();

async function prPersistWelcomeSeen({ keepalive = false } = {}) {
  if (!window.PR_WELCOME_NEEDED && !window.PR_WELCOME_SERVER_NEEDED) return;
  window.PR_WELCOME_NEEDED = false;
  prSetWelcomeSeenPending();
  try {
    const opts = keepalive
      ? { method: 'POST', keepalive: true }
      : { method: 'POST', cache: 'no-store' };
    await fetch('/api/welcome/seen', opts);
  } catch (_) {}
}

function prShowWelcome() {
  const el = document.getElementById('welcome-modal');
  if (el && window.bootstrap) bootstrap.Modal.getOrCreateInstance(el).show();
}

async function prWelcomeOpenConfig() {
  // Persist the seen flag before navigating. The session flag covers the short
  // page-transition window if config.json has not finished writing yet.
  await prPersistWelcomeSeen();
  window.location.href = '/config';
}

(function initWelcomeGuide() {
  const el = document.getElementById('welcome-modal');
  if (!el) return;
  el.addEventListener('hidden.bs.modal', () => {
    if (!window.PR_WELCOME_NEEDED) return;
    prPersistWelcomeSeen({ keepalive: true });
  });
  if (window.PR_WELCOME_NEEDED) {
    document.addEventListener('DOMContentLoaded', prShowWelcome);
    if (document.readyState !== 'loading') prShowWelcome();
  }
})();

// ── Shared debug output popup ─────────────────────────────────────────────────
// Every debug button routes through here: a fixed panel with the raw text and
// a copy-to-clipboard button. Created lazily so any page can use it.
function _prDebugBox() {
  let overlay = document.getElementById('pr-debug-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'pr-debug-overlay';
    overlay.className = 'pr-debug-overlay';
    overlay.innerHTML =
      '<div class="pr-debug-box" role="dialog" aria-modal="true" aria-label="Debug output" tabindex="-1">' +
        '<div class="pr-debug-head">' +
          '<span class="pr-debug-title" id="pr-debug-title"></span>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" id="pr-debug-copy" onclick="prCopyDebugBox(this)">Copy</button>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" id="pr-debug-download" onclick="prDownloadDebugBox(this)">Download</button>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" onclick="prHideDebugBox()">Close</button>' +
        '</div>' +
        '<pre id="pr-debug-text"></pre>' +
      '</div>';
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) prHideDebugBox(); });
    document.body.appendChild(overlay);
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && overlay.classList.contains('show')) prHideDebugBox();
    });
  }
  return overlay;
}
// Pin the page-title bar exactly below the site header and give it its
// floating backdrop only while actually stuck.
(function () {
  const bar = document.querySelector('.page-title-bar');
  if (!bar) return;
  // offsetHeight is rounded to whole pixels, and the stacked mobile header is
  // 86.72px tall — reported as 87. The title bar then sticks a quarter-pixel
  // BELOW where the header actually ends, and scrolling content shows through
  // that seam. Measure the real height and floor it, so the bar laps under the
  // header instead: the header paints above it (z-index 100 vs 60), so an
  // overlap is invisible where a gap is not.
  const headerH = () => {
    const el = document.querySelector('.site-header');
    if (!el) return 58;
    return Math.floor(el.getBoundingClientRect().height) || 58;
  };
  const syncTop = () => {
    document.documentElement.style.setProperty('--pr-header-h', headerH() + 'px');
    document.documentElement.style.setProperty('--pr-titlebar-h', (bar.offsetHeight || 54) + 'px');
  };
  const syncStuck = () => bar.classList.toggle('is-stuck', bar.getBoundingClientRect().top <= headerH() + 1);
  syncTop(); syncStuck();
  window.addEventListener('resize', () => { syncTop(); syncStuck(); });
  window.addEventListener('scroll', syncStuck, { passive: true });
})();

// Swap a button's label in place without the button jumping width — the label
// carries the state ("Save" → "Saving", "Reset everything" → "Are you sure?").
function _measureMorphButtonWidth(btn, text) {
  if (!btn) return 0;
  const probe = btn.cloneNode(false);
  probe.removeAttribute('id');
  probe.removeAttribute('onclick');
  probe.disabled = false;
  probe.style.position = 'absolute';
  probe.style.visibility = 'hidden';
  probe.style.pointerEvents = 'none';
  probe.style.left = '-9999px';
  probe.style.top = '-9999px';
  probe.style.width = 'auto';
  probe.style.minWidth = '0';
  const span = document.createElement('span');
  span.className = 'btn-morph-label';
  span.textContent = text;
  probe.appendChild(span);
  document.body.appendChild(probe);
  const width = Math.ceil(probe.offsetWidth);
  probe.remove();
  return width;
}

function _morphButtonText(btn, text, opts = {}) {
  if (!btn) return;
  const nextText = String(text || '');
  btn.classList.add('btn-morph');

  if (btn._morphTimer) {
    clearTimeout(btn._morphTimer);
    btn._morphTimer = null;
  }
  let label = btn.querySelector('.btn-morph-label');
  if (!label) {
    const currentText = (btn.textContent || '').trim();
    btn.textContent = '';
    label = document.createElement('span');
    label.className = 'btn-morph-label';
    label.textContent = currentText;
    btn.appendChild(label);
  }

  if ((label.textContent || '').trim() === nextText) {
    btn.setAttribute('aria-label', nextText);
    return;
  }

  const currentWidth = Math.ceil(btn.getBoundingClientRect().width || btn.offsetWidth);
  const nextWidth = _measureMorphButtonWidth(btn, nextText);
  // noShrink (used when arming "Are you sure?"): never make the tap target
  // smaller than it was. On touch, the confirming tap lands where the button
  // WAS — if the arming morph shrinks it, that tap hits the page instead and
  // the outside-click canceller disarms the confirmation.
  const targetWidth = opts.noShrink ? Math.max(currentWidth, nextWidth) : nextWidth;

  btn.setAttribute('aria-label', nextText);
  btn.style.width = `${currentWidth}px`;
  btn.style.minWidth = `${currentWidth}px`;
  btn.offsetWidth; // force the width transition to start from the current size

  label.textContent = nextText;
  requestAnimationFrame(() => {
    btn.style.width = `${targetWidth}px`;
    btn.style.minWidth = `${targetWidth}px`;
  });

  btn._morphTimer = setTimeout(() => {
    btn.style.width = '';
    // Keep the widened footprint for the whole armed state; the disarm morph
    // (without noShrink) clears it.
    btn.style.minWidth = opts.noShrink ? `${targetWidth}px` : '';
    btn._morphTimer = null;
  }, 210);
}
let _prDebugReturnFocus = null;
// Copy and Download act on the box's TEXT, so while a probe is still running
// they would hand over the "Running debug…" placeholder — a file or clipboard
// full of nothing, with no sign anything went wrong. Ghost them until the
// output lands. Close stays live: leaving is always allowed.
// pending defaults to false, so every terminal call — results, failures, and
// the collection debugs' own renderers — un-ghosts them by simply not asking.
function prShowDebugBox(title, text, opts) {
  const overlay = _prDebugBox();
  overlay.querySelector('#pr-debug-title').textContent = title || 'Debug output';
  const textEl = overlay.querySelector('#pr-debug-text');
  textEl.textContent = text || '(empty)';
  // Content-creation stamp for the Save button's filename, when the endpoint
  // supplied one (the cache dump names itself after the store's write time).
  // Set on EVERY call — a popup without one must not inherit the previous
  // popup's stamp.
  textEl.dataset.stamp = (opts && opts.stamp) || '';
  const pending = !!(opts && opts.pending);
  for (const id of ['pr-debug-copy', 'pr-debug-download']) {
    const el = overlay.querySelector('#' + id);
    if (!el) continue;
    el.disabled = pending;
    el.setAttribute('aria-disabled', pending ? 'true' : 'false');
    el.title = pending ? 'Available when the output finishes' : '';
  }
  overlay.classList.add('show');
  // The box declares role=dialog aria-modal, so actually move focus into it
  // (and back on close) — otherwise keyboard focus stays on the now-disabled
  // trigger button and Tab wanders the page behind the overlay. Focus the
  // dialog itself, not the Copy button: a focused .btn-outline-secondary
  // paints the filled hover style, which made Copy open in a different color
  // than its neighbors.
  if (!overlay.contains(document.activeElement)) {
    _prDebugReturnFocus = (document.activeElement instanceof HTMLElement) ? document.activeElement : null;
    overlay.querySelector('.pr-debug-box')?.focus();
  }
}
function prHideDebugBox() {
  const overlay = document.getElementById('pr-debug-overlay');
  if (!overlay || !overlay.classList.contains('show')) return;
  overlay.classList.remove('show');
  if (_prDebugReturnFocus && document.contains(_prDebugReturnFocus)) {
    try { _prDebugReturnFocus.focus(); } catch (_) {}
  }
  _prDebugReturnFocus = null;
}
function prDownloadDebugBox(btn) {
  // Same text the Copy button grabs, saved as a file named after the popup —
  // debug output is what gets pasted into bug reports, and a download
  // survives outputs too large for a clipboard.
  const textEl = document.getElementById('pr-debug-text');
  const text = textEl?.textContent || '';
  const title = document.getElementById('pr-debug-title')?.textContent || 'debug';
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'debug';
  // The filename carries the CONTENT's time when the endpoint declared one;
  // the click time is only the fallback for popups generated on demand.
  const stamp = textEl?.dataset.stamp || prFileStamp();
  const ok = prDownloadText('mediareducer-' + slug + '_' + stamp + '.txt', text);
  if (btn) {
    const old = btn.textContent;
    btn.textContent = ok ? 'Saved' : 'Save failed';
    setTimeout(() => { btn.textContent = old; }, 1600);
  }
}
async function prCopyDebugBox(btn) {
  const text = document.getElementById('pr-debug-text')?.textContent || '';
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) { /* fall through to the textarea fallback */ }
  if (!ok) {
    // http:// LAN origins have no navigator.clipboard — classic fallback.
    // readonly + focus + an explicit selection range: iOS Safari ignores a
    // bare select() on an unfocused textarea and silently copies nothing,
    // which made this button the one header action that "didn't work".
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
  }
  if (btn) {
    const old = btn.textContent;
    btn.textContent = ok ? 'Copied' : 'Copy failed';
    setTimeout(() => { btn.textContent = old; }, 1600);
  }
}
// Keep the "…" loading state visible for a beat before results replace it: a
// fast (LAN) response can resolve before the browser paints the loading frame,
// so the panel would flash straight old→new and never look like it refreshed.
const PR_DEBUG_MIN_MS = 400;
function prDebugMinVisible(startedAt) {
  const remaining = PR_DEBUG_MIN_MS - (performance.now() - startedAt);
  return remaining > 0 ? new Promise(res => setTimeout(res, remaining)) : Promise.resolve();
}
// POST a {ok, text} debug endpoint and pop the result.
async function prRunDebug(url, title, btn, body) {
  if (btn) btn.disabled = true;
  const startedAt = performance.now();
  prShowDebugBox(title, 'Running debug…', { pending: true });
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.error || 'Debug request failed.');
    await prDebugMinVisible(startedAt);
    prShowDebugBox(title, d.text || '(empty response)', { stamp: d.stamp || '' });
  } catch (e) {
    await prDebugMinVisible(startedAt);
    prShowDebugBox(title, 'Debug failed: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Ask before an action that deletes, or that arms deleting.
//
// Replaces a two-click "Are you sure?" on the button itself. That pattern had
// to park its warning in a sticky toast beside the button, which meant the
// question and the reason for it lived in two different places — and a click
// anywhere else silently disarmed it, so a user who read the toast and then
// clicked back was starting over without being told. A modal puts the
// consequence and the choice together and has to be answered.
//
// body: a string, or an array of paragraphs. A paragraph may be
// {text, warn: true} to render in the danger color. Text only — nothing here
// is ever parsed as HTML.
//
// Resolves 'confirm', 'extra' (the optional third button), or null for
// cancel/escape/backdrop, so the caller treats "went away" as "no".
let _prConfirmOpen = false;
function prConfirm({ title = 'Are you sure?', body = '', confirmText = 'Confirm',
                     extraText = '', danger = true } = {}) {
  const modal = document.getElementById('pr-confirm-modal');
  if (!modal || typeof bootstrap === 'undefined') {
    // No dialog available (a page without base, or Bootstrap failed to load):
    // refuse rather than silently proceeding with a destructive action, and say
    // so — otherwise the button looks like it did nothing.
    showToast('Could not open the confirmation. Reload the page and try again.', 'danger');
    return Promise.resolve(null);
  }
  // One question at a time. There is a single dialog element, so a second call
  // while one is open would rewrite the open question's text and leave both
  // callers listening on the same buttons — one answer resolving two actions.
  if (_prConfirmOpen) return Promise.resolve(null);
  const bodyEl = document.getElementById('pr-confirm-body');
  const okBtn = document.getElementById('pr-confirm-ok');
  const extraBtn = document.getElementById('pr-confirm-extra');
  document.getElementById('pr-confirm-title').textContent = title;

  bodyEl.textContent = '';
  for (const part of (Array.isArray(body) ? body : [body])) {
    if (!part) continue;
    const p = document.createElement('p');
    const isObj = typeof part === 'object';
    p.textContent = isObj ? part.text : part;
    if (isObj && part.warn) p.className = 'pr-confirm-warn';
    bodyEl.appendChild(p);
  }

  okBtn.textContent = confirmText;
  okBtn.className = 'btn ' + (danger ? 'btn-danger-action' : 'btn-outline-primary');
  extraBtn.hidden = !extraText;
  if (extraText) extraBtn.textContent = extraText;

  return new Promise((resolve) => {
    let outcome = null;
    const inst = bootstrap.Modal.getOrCreateInstance(modal);
    const pick = (value) => { outcome = value; inst.hide(); };
    const onOk = () => pick('confirm');
    const onExtra = () => pick('extra');
    // Resolve on hidden, not on click: the caller's next step often opens
    // another dialog or moves focus, and Bootstrap is still tearing this one
    // down until then. Whatever closed it, an unanswered dialog is a "no".
    const onHidden = () => {
      okBtn.removeEventListener('click', onOk);
      extraBtn.removeEventListener('click', onExtra);
      modal.removeEventListener('hidden.bs.modal', onHidden);
      _prConfirmOpen = false;
      resolve(outcome);
    };
    okBtn.addEventListener('click', onOk);
    extraBtn.addEventListener('click', onExtra);
    modal.addEventListener('hidden.bs.modal', onHidden);
    _prConfirmOpen = true;
    inst.show();
  });
}

// Transient status line. Toasts report what happened; anything that needs an
// answer before it happens goes through prConfirm() instead.
function showToast(msg, type = 'success') {
  const wrap = document.getElementById('toast-wrap');
  const cls  = ({ success: 'pr-toast--success', danger: 'pr-toast--danger', warning: 'pr-toast--warning', info: 'pr-toast--info' })[type] || 'pr-toast--info';
  const text = String(msg ?? '')
    .replace(/\u2026/g, '...')
    .replace(/[\u2013\u2014]/g, '-')
    // Emoji only — \p{S} also covered ordinary math symbols, which mangled
    // real messages ("HEADROOM_GB=1000" lost its '=', "~1200 GB" its '~').
    .replace(/\p{Extended_Pictographic}/gu, '')
    .replace(/\s+/g, ' ')
    .trim();
  const el = document.createElement('div');
  el.className = `pr-toast ${cls}`;
  el.textContent = text;
  el.style.cursor = 'pointer';
  el.title = 'Dismiss';
  wrap.appendChild(el);
  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    el.classList.add('pr-toast--out');
    setTimeout(() => el.remove(), 200);
  };
  el.addEventListener('click', dismiss);
  // Errors and warnings carry instructions ("Reload the page", "check the
  // connection...") — give them longer than the 5s success blip.
  setTimeout(dismiss, (type === 'danger' || type === 'warning') ? 9000 : 5000);
}

// Header "running" badge. Toggled via a class on the header so CSS decides which
// badge (mobile vs desktop) is shown. The Dashboard drives it directly from its
// own status polling; every other page runs the lightweight poller below so the
// badge is visible no matter where you are while a run is active.
// What a run in flight is called, and which button's color it borrows. One
// place decides, because the header badge and the run pill report the same run
// and must not word it differently.
//
// Red is reserved for the run that means to delete something, and it says
// "Cleaning" rather than "Running": red plus a word that describes any run is
// the alarming half of a warning without the informative half.
//   red    — a Cleanup, files are going away
//   yellow — a Debug Cleanup, which drives that path but deletes nothing
//   blue   — every other run, nothing is being deleted
// Blue says "Running" whichever way the run started. What matters here is
// whether anything is being deleted, and the colour-plus-word pair already
// carries that; who pressed the button does not change what is happening, and
// the run-progress card names the trigger beside its own pill anyway.
window.prRunTone = function (s) {
  if (s.debugCleanup) return ['Debugging', 'is-warning'];
  if (s.cleanup)      return ['Cleaning', 'is-danger'];
  return ['Running', 'is-accent'];
};
window.prSetHeaderRunning = function (on, live, debugLive) {
  const h = document.querySelector('.site-header');
  if (!h) return;
  const [label, tone] = window.prRunTone(
    { cleanup: !!live, debugCleanup: !!debugLive });
  h.classList.toggle('is-running', !!on);
  h.querySelectorAll('.header-run-badge').forEach(el => {
    el.classList.remove('is-danger', 'is-accent', 'is-warning');
    el.classList.add(tone);
  });
  h.querySelectorAll('.run-badge-label').forEach(el => { el.textContent = label; });
};
// Dismissing a note CLEARS whatever made it true, server-side. Hidden in the
// browser it would be back on the next load, still there on every other device,
// and — for the ones keyed off stored state — ready to resurface later saying
// something that had since stopped being so. Delegated from the document so a
// note rendered after load (the dashboard's, from a status poll) needs no extra
// wiring, and keyed by data-dismiss-note so a new note is markup alone.
document.addEventListener('click', async (e) => {
  const btn = e.target.closest?.('[data-dismiss-note]');
  if (!btn) return;
  const note = btn.closest('.config-inline-note, .config-inline-warning');
  btn.disabled = true;
  try {
    const r = await fetch(`/api/notes/${encodeURIComponent(btn.dataset.dismissNote)}/dismiss`,
                          { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (!d.ok) throw new Error(d.message || 'Could not dismiss the note.');
    note?.remove();
  } catch (err) {
    // The note stays put. It is still true, and swallowing this would hide a
    // write problem behind something that looked like it worked.
    btn.disabled = false;
    showToast(err.message || 'Could not dismiss the note.', 'danger');
  }
});

// The badge links to the Dashboard's run-progress card. Already on the
// Dashboard, smooth-scroll to it instead of a full reload.
document.querySelectorAll('.header-run-badge').forEach(a => {
  a.addEventListener('click', (e) => {
    const target = document.getElementById('run-progress');
    if (!target) return;   // other pages navigate normally
    e.preventDefault();
    history.replaceState(null, '', '#run-progress');
    prScrollTo(target);
  });
});
// ── IMDb manual-setup popup (shared by all pages) ──
// Which failure the popup was already shown for. Persisted so a page reload
// or tab switch does not re-open it — only a NEW failure (a new key) pops it
// again, keyed by the run's started_at.
let _imdbHelpShownFor = null;
function _imdbHelpAlreadyShown(key) {
  try { return localStorage.getItem('pr-imdb-help-shown') === String(key); }
  catch (_) { return _imdbHelpShownFor === key; }
}
function _markImdbHelpShown(key) {
  _imdbHelpShownFor = key;
  try { localStorage.setItem('pr-imdb-help-shown', String(key)); } catch (_) {}
}

async function showImdbHelpModal() {
  // Fill in the exact destination path and configured source URL so the steps
  // match this install, then open the modal.
  try {
    const r = await fetch('/api/imdb/status', { cache: 'no-store' });
    const s = await r.json();
    const path = s.path || '/config/title.ratings.tsv';
    const url = s.url || 'https://datasets.imdbws.com/title.ratings.tsv.gz';
    const tsvEl = document.getElementById('imdb-help-tsv-path');
    const gzEl = document.getElementById('imdb-help-gz-path');
    const urlEl = document.getElementById('imdb-help-url');
    if (tsvEl) tsvEl.textContent = path;
    if (gzEl) gzEl.textContent = path + '.gz';
    if (urlEl && url) urlEl.href = url;
  } catch (_) { /* keep the sensible defaults already in the markup */ }
  const modalEl = document.getElementById('imdb-help-modal');
  if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).show();
}


// Live red Configuration tab: /api/status polls call this so an API failure
// mid-run (or a recovery) repaints the header without a page reload.
window.prSetConfigTabError = function (on) {
  const tab = [...document.querySelectorAll('.header-tabs a')].find(a => a.getAttribute('href') === '/config');
  if (!tab) return;
  on = !!on;
  if (tab.classList.contains('config-needs-onboarding') === on) return;
  tab.classList.toggle('config-needs-onboarding', on);
  if (on) tab.title = 'Fix the API connection settings';
  else tab.removeAttribute('title');
};
// One question for every decorative effect below: has the user asked for a
// plain interface (Advanced -> Reduce visual effects)? The CSS blanket stops
// them animating, but these two BUILD things — a bar element, a wave overlay —
// and the honest way to not have an effect is to not create it.
// Appearance lives in a cookie per browser, not in config.json: it says how
// THIS browser draws the page, and a phone and a desktop rarely want the same
// answer. The server reads the cookie to stamp <html> before the first paint;
// these apply the change to the page already on screen, so nothing reloads.
function prSetAppearanceCookie(name, off) {
  try {
    document.cookie = name + '=' + (off ? 'off' : 'on')
      + ';path=/;max-age=' + (60 * 60 * 24 * 365) + ';samesite=lax';
  } catch (_) {}
}
// The stylesheet does the rest off these two attributes. Reduce visual effects
// covers the glass, same OR the server applies, so the page cannot end up
// showing blur that the next load would drop.
function prApplyAppearance({ effectsOff, glassOff }) {
  const root = document.documentElement;
  if (effectsOff) root.setAttribute('data-effects', 'off');
  else root.removeAttribute('data-effects');
  if (effectsOff || glassOff) root.setAttribute('data-glass', 'off');
  else root.removeAttribute('data-glass');
  // Everything CSS can express is already done. These two are effects that
  // exist by being BUILT, so turning the setting back on has to build them —
  // they were skipped entirely on a page that loaded with effects off. Both
  // are no-ops once they have run.
  if (!effectsOff) { initTabNavFeedback(); initTabIndicator(); }
}
const prEffectsOff = () => document.documentElement.getAttribute('data-effects') === 'off';

// Tab click feedback: mark the clicked tab while its navigation is in flight
// (see .is-navigating). Skipped for the current page and for modified clicks
// (new-tab etc., which don't navigate this document).
let _tabNavFeedbackBound = false;
function initTabNavFeedback() {
  if (prEffectsOff() || _tabNavFeedbackBound) return;
  _tabNavFeedbackBound = true;
  document.querySelectorAll('.header-tabs a').forEach((a) => {
    const dest = () => (a.getAttribute('href') || '').split('#')[0];
    // The wave starts the moment the pointer goes DOWN — click fires on
    // release, which would eat the whole press duration before anything
    // showed. pressT stamps the wave's true start so the next page can
    // continue it from the right offset.
    let pressT = 0;
    let clicked = false;
    a.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const d = dest();
      if (!d || d === location.pathname) return;
      pressT = Date.now();
      clicked = false;
      a.classList.add('is-navigating');
      // A press that never becomes a navigation (drag off, release elsewhere,
      // canceled) must not leave the tab lit.
      const settle = () => setTimeout(() => {
        if (!clicked) a.classList.remove('is-navigating');
      }, 250);
      window.addEventListener('pointerup', settle, { once: true });
      window.addEventListener('pointercancel', settle, { once: true });
    });
    a.addEventListener('click', (e) => {
      if (e.defaultPrevented || e.button !== 0
          || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const d = dest();
      if (!d || d === location.pathname) return;
      clicked = true;
      a.classList.add('is-navigating');   // keyboard activation has no pointerdown
      // Hand the wave to the next page: a fast load swaps documents before the
      // ripple finishes, and the arriving page plays out the remainder — timed
      // from the PRESS, where the wave actually began.
      const started = (pressT && Date.now() - pressT < 1000) ? pressT : Date.now();
      pressT = 0;
      try {
        sessionStorage.setItem('pr-tab-wave', JSON.stringify({ href: d, t: started }));
      } catch (err) {}
      // Safety: a canceled navigation (Esc) never fires pageshow — stop the
      // pulse eventually rather than blinking forever on a page that stayed.
      setTimeout(() => a.classList.remove('is-navigating'), 20000);
    });
  });
  // Arriving side of the hand-off: if this page was reached by a tab click
  // moments ago, finish the wave on the (now active) tab from where the
  // previous page left it.
  try {
    const raw = sessionStorage.getItem('pr-tab-wave');
    if (raw) {
      sessionStorage.removeItem('pr-tab-wave');
      const { href, t } = JSON.parse(raw);
      const elapsed = Date.now() - Number(t);
      if (href === location.pathname && elapsed >= 0 && elapsed < 320
          && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        const tab = [...document.querySelectorAll('.header-tabs a')]
          .find(x => (x.getAttribute('href') || '').split('#')[0] === href);
        if (tab) {
          tab.style.setProperty('--pr-wave-offset', `-${Math.round(elapsed)}ms`);
          tab.classList.add('is-arriving');
          const settle = () => {
            tab.classList.remove('is-arriving');
            tab.style.removeProperty('--pr-wave-offset');
          };
          tab.addEventListener('animationend', settle, { once: true });
          // Fallback: if the animation never runs (killed mid-flight, element
          // hidden), animationend never fires — settle shortly after the
          // wave's lifetime regardless.
          setTimeout(settle, 700);
        }
      }
    }
  } catch (err) {}
  // A back/forward-cache restore brings the old page back with the classes
  // still on the tab — clear both so the restored page reads as settled.
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('.header-tabs a.is-navigating, .header-tabs a.is-arriving')
      .forEach((a) => {
        a.classList.remove('is-navigating', 'is-arriving');
        a.style.removeProperty('--pr-wave-offset');
      });
  });
}

// Sliding tab underline: on hover-capable pointers the active tab's blue bar
// glides to the hovered tab and returns when the pointer leaves the tab row.
let _tabIndicatorBuilt = false;
function initTabIndicator() {
  if (prEffectsOff() || _tabIndicatorBuilt) return;
  if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  _tabIndicatorBuilt = true;
  const tabs = document.querySelector('.header-tabs');
  if (!tabs || !tabs.querySelector('a')) return;
  const bar = document.createElement('span');
  bar.className = 'header-tab-indicator';
  tabs.appendChild(bar);
  tabs.classList.add('has-indicator');
  const activeTab = () => tabs.querySelector('a.active');
  function moveTo(el, instant = false) {
    if (!el) { bar.style.opacity = '0'; return; }
    if (instant) bar.style.transition = 'none';
    bar.style.opacity = '1';
    bar.style.left = el.offsetLeft + 'px';
    bar.style.width = el.offsetWidth + 'px';
    bar.style.top = (el.offsetTop + el.offsetHeight - 2) + 'px';
    // The alert-red Configuration tab keeps its color under the bar too.
    bar.style.background = el.classList.contains('config-needs-onboarding')
      ? 'var(--text-danger)' : 'var(--text-accent)';
    if (instant) requestAnimationFrame(() => { bar.style.transition = ''; });
  }
  moveTo(activeTab(), true);
  tabs.querySelectorAll('a').forEach(a => {
    a.addEventListener('mouseenter', () => moveTo(a));
  });
  tabs.addEventListener('mouseleave', () => moveTo(activeTab()));
  window.addEventListener('resize', () => moveTo(activeTab(), true));
  // Fonts can settle after first paint and shift the tab widths.
  window.addEventListener('load', () => moveTo(activeTab(), true));
}

// Build them for a page that loaded with effects ON; prApplyAppearance builds
// them later for a page that did not.
initTabNavFeedback();
initTabIndicator();

(function pollHeaderRunBadge() {
  async function tick() {
    if (window.__prDashboardOwnsBadge) return;   // Dashboard updates it itself
    try {
      const r = await fetch('/api/status?_=' + Date.now(), { cache: 'no-store' });
      if (r.ok) {
        const d = await r.json();
        window.prSetHeaderRunning(!!d.run_active, !!d.run_cleanup, !!d.run_debug_cleanup);
        window.prSetConfigTabError(!!d.config_attention);
        // Page hook: lets a page react to run-state changes without its own
        // /api/status poll (the explorer locks Filtering & Scoring with it).
        if (typeof window.prOnStatusPoll === 'function') {
          try { window.prOnStatusPoll(d); } catch (_) {}
        }
      }
    } catch (_) { /* transient — try again next tick */ }
  }
  tick();
  setInterval(tick, 4000);
})();
