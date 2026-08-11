/* The Configuration page. Server-rendered state arrives from the inline
   preamble in config.html; this file is deferred and cannot carry template
   expressions. */

// ── Dirty state ───────────────────────────────────────────────────────────────

let _dirty = false;
let _collectionSelections = { plex: new Set(), jellyfin: new Set() };
let _collectionOptions    = { plex: [], jellyfin: [] };
let _savePending = false;
let _imdbDownloadStatus = null;
let _cacheClearStatus = null;
const CONNECTION_OVERRIDE_FIELDS = new Set(['TAUTULLI_URL', 'TAUTULLI_API_KEY', 'PLEX_URL', 'PLEX_TOKEN', 'JELLYFIN_URL', 'JELLYFIN_API_KEY', 'RADARR_URL', 'RADARR_API_KEY', 'SONARR_URL', 'SONARR_API_KEY']);
const SECRET_OVERRIDE_FIELDS = new Set([
  'TAUTULLI_API_KEY', 'PLEX_TOKEN', 'JELLYFIN_API_KEY', 'RADARR_API_KEY', 'SONARR_API_KEY',
  // Notification credentials — masked with a Show toggle just like the API keys.
  'NOTIFY_DISCORD_WEBHOOK', 'NOTIFY_TELEGRAM_BOT_TOKEN', 'NOTIFY_SLACK_WEBHOOK',
  'NOTIFY_PUSHOVER_USER_KEY', 'NOTIFY_PUSHOVER_TOKEN', 'NOTIFY_NTFY_TOPIC', 'NOTIFY_GOTIFY_TOKEN',
]);
const OPTIONAL_CLEANUP_MOUNTS = new Set(['radarr']);
function _optionalCleanupEnabled() { return !!document.getElementById('section-enabled')?.checked; }

// Dim the Plex and Jellyfin connection fields when their server software is
// deselected. Values stay visible, editable and saved — they just don't
// participate until the server is enabled, so a key can be pasted in first and
// is there waiting. Neither server selected is a valid (idle) state; the note
// explains it. Dimming alone, so the fields stay in the tab order: they are
// live, and a keyboard user reaching one is fine. A run is the only thing that
// makes them inert, via the run lock.
function onServerSoftwareChange() {
  const usePlex = !!document.getElementById('USE_PLEX')?.checked;
  const useJf   = !!document.getElementById('USE_JELLYFIN')?.checked;
  const _ghostFields = (sel, off) => document.querySelectorAll(sel).forEach(el => {
    el.classList.toggle('conn-field-ghost', off);
  });
  _ghostFields('.conn-field-plex', !usePlex);
  _ghostFields('.conn-field-jellyfin', !useJf);
  const note = document.getElementById('server-software-note');
  if (note) note.hidden = usePlex || useJf;
  if (typeof renderMountBadges === 'function') renderMountBadges();
  if (typeof _applyConfigRuntimeLocks === 'function') _applyConfigRuntimeLocks();
  else if (typeof _applySpaceThresholdLock === 'function') _applySpaceThresholdLock();
  if (typeof _updateRunModeAvailability === 'function') _updateRunModeAvailability();
}

function _savedPlexApiConnected() {
  return !!_savedConfig?.USE_PLEX && !!_savedConfigHealth?.plex_connected;
}
function _savedJellyfinApiConnected() {
  return !!_savedConfig?.USE_JELLYFIN && !!_savedConfigHealth?.jellyfin_connected;
}
function _savedMediaApiConnected() {
  return _selectedMediaApisReady();
}
function _mediaPathsCompatible() {
  return !_savedConfigHealth?.media_path_blocker;
}
function _filesystemReady() {
  return !_savedConfigHealth?.filesystem_blocker;
}
function _filesystemBlockingText() {
  return 'Fix the /config read/write problem first.';
}
// Whether the media servers are in good enough shape to edit Movie Library
// Paths and Space Thresholds. It reads the LIVE checkboxes against the SAVED
// health, which needs care: health is only probed for the servers the saved
// config had selected, so a deselected server's flag reads false because
// nothing asked, not because it failed. Left at that, ticking one back on is
// indistinguishable from it being down, which locks both sections behind
// "Connect or uncheck the selected media server first" — advice that can't
// help, since Check for Errors only adopts a probe when the form still matches
// what was saved. A newly ticked server is therefore treated as unknown until a
// save probes it, while a server the SAVED config selected is still held to its
// health. Nothing is loosened: the save re-probes, and /api/run re-probes again
// before anything can delete.
function _selectedMediaApisReady() {
  const usePlex = !!document.getElementById('USE_PLEX')?.checked;
  const useJf = !!document.getElementById('USE_JELLYFIN')?.checked;
  if (!usePlex && !useJf) return false;
  const plexProbed = !!_savedConfig?.USE_PLEX;
  const jfProbed = !!_savedConfig?.USE_JELLYFIN;
  const plexOk = !usePlex || !plexProbed || !!_savedConfigHealth?.tautulli_connected;
  const jfOk = !useJf || !jfProbed || !!_savedConfigHealth?.jellyfin_connected;
  // At least one selected server has to be PROVEN good, or a fresh install
  // would unlock both sections just by ticking a box it has never connected to.
  const anyProven = (usePlex && plexProbed && !!_savedConfigHealth?.tautulli_connected)
    || (useJf && jfProbed && !!_savedConfigHealth?.jellyfin_connected);
  return plexOk && jfOk && anyProven
    && _mediaPathsCompatible()
    && _filesystemReady();
}
function _spaceThresholdsHaveUsableLibrary() {
  return _hasSavedMonitorDirs() && _selectedMediaApisReady();
}
function _configActivityLocked() {
  return !!_configRunActive;
}
function _savedLiveModeLocked() {
  return _isCleanupRunMode(_savedConfig?.RUN_MODE);
}
function _setNotice(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message || '';
  el.hidden = !message;
}
function _mediaServerBlockText() {
  // First-contact wording: with NO server selected yet, "connect or uncheck
  // the selected media server" names two actions that don't exist.
  const usePlex = !!document.getElementById('USE_PLEX')?.checked;
  const useJf = !!document.getElementById('USE_JELLYFIN')?.checked;
  return (!usePlex && !useJf)
    ? 'Select Plex or Jellyfin under Server software first.'
    : 'Connect or uncheck the selected media server first.';
}

// The run-active reason, named because it is filtered OUT of the per-section
// notices: the page-level note above the accordion already says it, and five
// copies of one sentence bury the section-specific reasons that actually
// differ from each other.
const _RUN_ACTIVE_REASON = 'A run is active. Try again when it finishes.';

// What a section's own inline notice should say for a given lock reason —
// nothing, when the reason is the run the page banner is already reporting.
function _sectionNoticeText(reason) {
  return reason === _RUN_ACTIVE_REASON ? '' : (reason || '');
}

function _sectionLockReason(section) {
  if (_configActivityLocked()) return _RUN_ACTIVE_REASON;
  // Connections, Media Library Paths, AND Space Thresholds all stay editable
  // while Automatic Cleanup is armed: saving a change to any of them auto-pauses Automatic Cleanup
  // server-side, so nothing needs a hard lock here.
  if ((section === 'paths' || section === 'space') && !_filesystemReady()) {
    return _filesystemBlockingText();
  }
  if (section === 'paths' && !_savedMediaApiConnected()) {
    if (!_mediaPathsCompatible()) return `Media paths do not line up with ${LIBRARY_ROOT}. Check the mounts, then recheck.`;
    return _mediaServerBlockText();
  }
  if (section === 'space') {
    if (!_selectedMediaApisReady()) {
      if (!_mediaPathsCompatible()) return `Media paths do not line up with ${LIBRARY_ROOT}. Check the mounts first.`;
      return _mediaServerBlockText();
    }
    if (!_hasSavedMonitorDirs()) return 'Save a monitored directory first.';
  }
  return '';
}

function _applyConnectionAvailability() {
  const reason = _sectionLockReason('connections');
  const locked = !!reason;
  _setNotice('connections-lock-note', _sectionNoticeText(reason));
  document.getElementById('s-conn')?.classList.toggle('section-run-ghost', locked);
  ['USE_PLEX', 'USE_JELLYFIN', 'btn-auto-detect-connections', 'btn-config-check'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = locked;
    el.setAttribute('aria-disabled', locked ? 'true' : 'false');
  });
  CONNECTION_OVERRIDE_FIELDS.forEach(name => {
    const input = document.getElementById(name);
    const wrapper = document.getElementById(`override-field-${name}`);
    const toggle = document.querySelector(`.secret-toggle[data-target="${name}"]`);
    if (input) input.disabled = locked || wrapper?.classList.contains('connection-disabled');
    if (toggle) toggle.disabled = locked || input?.disabled;
  });
}

function _applyMoviePathAvailability() {
  const reason = _sectionLockReason('paths');
  const enabled = !reason;
  _setNotice('movie-paths-api-note', _sectionNoticeText(reason));
  document.getElementById('s-paths')?.classList.toggle('section-run-ghost', !enabled);
  document.querySelectorAll('#monitor-dirs .monitor-dir, #monitor-dirs button').forEach(el => {
    el.disabled = !enabled;
    el.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  });
  const addBtn = document.getElementById('btn-add-monitor-dir');
  if (addBtn) {
    addBtn.disabled = !enabled;
    addBtn.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  }
  const plexOk = enabled && _savedPlexApiConnected();
  const jellyfinOk = enabled && _savedJellyfinApiConnected();
  document.querySelectorAll('#plex-protect-group input, #plex-protect-group button').forEach(el => {
    el.disabled = !plexOk;
    el.setAttribute('aria-disabled', plexOk ? 'false' : 'true');
  });
  document.querySelectorAll('#jellyfin-protect-group input, #jellyfin-protect-group button').forEach(el => {
    el.disabled = !jellyfinOk;
    el.setAttribute('aria-disabled', jellyfinOk ? 'false' : 'true');
  });
  const radarrReady = !!_savedConfigHealth?.radarr_connected;
  const sectionToggle = document.getElementById('section-enabled');
  if (sectionToggle) sectionToggle.disabled = !enabled || !radarrReady;
  const sonarrReady = !!_savedConfigHealth?.sonarr_connected;
  const sonarrToggle = document.getElementById('sonarr-cleanup-enabled');
  if (sonarrToggle) sonarrToggle.disabled = !enabled || !sonarrReady;
}

function _applyAdvancedActionAvailability() {
  const locked = _configActivityLocked() || !_filesystemReady();
  ['btn-clear-cache', 'btn-clear-logs', 'btn-imdb-download'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.dataset.runtimeLocked = locked ? '1' : '0';
    if (locked) {
      el.disabled = true;
      el.classList.add('disabled');
      el.setAttribute('aria-disabled', 'true');
    }
  });
  _applyClearCacheButtonState();
  _applyImdbDownloadButtonState();
  // The run lock has one note for the whole page; this one carries only the
  // filesystem problem, which that note wouldn't explain.
  _setNotice('advanced-run-lock-note', _filesystemReady() ? '' : _filesystemBlockingText());

  // The debug report ghosts only while a run is active (it would capture a
  // half-written run log) — NOT on filesystem problems, since diagnosing a
  // broken setup is exactly what the report is for. The API debug popups stay
  // available: they are read-only probes a run doesn't disturb.
  const reportBtn = document.getElementById('btn-create-report');
  if (reportBtn && !reportBtn.classList.contains('btn-busy')) {
    const runLocked = _configActivityLocked();
    reportBtn.disabled = runLocked;
    reportBtn.title = runLocked ? 'Available when the run finishes' : '';
  }
}

// EVERY section locks while a run is active, not a hand-picked subset. The
// per-section rules below decide each control's real state; the run lock then
// goes over the top of all of them, so nothing is editable mid-run whether or
// not someone remembered to add it to a list.
//
// Order matters. Releasing first and locking last means the per-section rules
// always see (and set) the true state, and the lock is only ever the outermost
// layer — a control the rules just disabled for its own reason is not handed
// back when the run ends.
function _applyConfigRuntimeLocks() {
  const locked = _configActivityLocked();
  if (!locked) _applyRunLockToSections(false);
  _applyConnectionAvailability();
  _applyMoviePathAvailability();
  _applySpaceThresholdLock();
  _applyAdvancedActionAvailability();
  if (locked) _applyRunLockToSections(true);
  _applyForceStopAvailability();
}

function _applyRunLockToSections(locked) {
  document.querySelectorAll('#cfg-accordion .accordion-collapse')
    .forEach(section => window.prSetRunLock(section, locked));
  _setNotice('config-run-lock-note',
    locked ? 'A run is active. Settings are locked until it finishes.' : '');
}

// Dependent blocks stay in the layout whatever the connection state — a
// disconnected server's block is ghosted (dimmed, controls disabled, unlock
// note shown) rather than hidden.
function _setConnectionGhost(id, connected) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.toggle('connection-ghost', !connected);
  const note = el.querySelector('.connection-unlock-note');
  if (note) note.hidden = !!connected;
}
function syncSavedVisibilitySections() {
  _setConnectionGhost('plex-protect-group', _savedPlexApiConnected());
  _setConnectionGhost('jellyfin-protect-group', _savedJellyfinApiConnected());
  _setConnectionGhost('optional-cleanup-box', !!_savedConfigHealth?.radarr_connected);
  _setConnectionGhost('sonarr-cleanup-box', !!_savedConfigHealth?.sonarr_connected);
  _applyConfigRuntimeLocks();
}
function _savedConnectionHasValues(names) {
  return names.every(name => String(_savedConfig?.[name] || '').trim() !== '');
}
function _savedConnectionReady(names, connected) {
  // Use the last saved/probed connection state for dependent-field gating.
  // Do not lock Optional Radarr cleanup or Protected collections while the user
  // is merely editing URL/API fields; wait until those edits are saved and the
  // backend probe says the saved connection no longer works.
  return !!connected && _savedConnectionHasValues(names);
}

// Where -webkit-text-security is available (Chromium/WebKit) we mask secret
// fields as type=text so password managers never offer to "save" the API key.
// Browsers without it (Firefox) keep a real type=password field for masking.
const _SECRET_CSS_MASK = !!(window.CSS && CSS.supports && CSS.supports('-webkit-text-security', 'disc'));

function _initSecretFields() {
  if (!_SECRET_CSS_MASK) return;
  SECRET_OVERRIDE_FIELDS.forEach(name => {
    const input = document.getElementById(name);
    if (!input) return;
    input.classList.add('secret-field');
    input.classList.remove('secret-shown');
    input.type = 'text';
  });
}

function _isSecretRevealed(input) {
  return _SECRET_CSS_MASK ? input.classList.contains('secret-shown') : input.type === 'text';
}

function _setSecretVisibility(name, visible) {
  const input = document.getElementById(name);
  const btn = document.querySelector(`.secret-toggle[data-target="${name}"]`);
  if (!input || !btn) return;
  if (_SECRET_CSS_MASK) {
    input.classList.add('secret-field');
    input.type = 'text';
    input.classList.toggle('secret-shown', visible);
  } else {
    input.type = visible ? 'text' : 'password';
  }
  btn.setAttribute('aria-pressed', visible ? 'true' : 'false');
  btn.setAttribute('aria-label', `${visible ? 'Hide' : 'Show'} ${name}`);
  btn.title = visible ? 'Hide value' : 'Show value';
  const label = btn.querySelector('.secret-toggle-label');
  if (label) label.textContent = visible ? 'Hide' : 'Show';
}

function _hideAllSecretFields() {
  SECRET_OVERRIDE_FIELDS.forEach(name => _setSecretVisibility(name, false));
}

// Auto Detect only fills credentials — URLs are handled by their placeholders
// and the blank-saves-to-default rule, so it never overwrites a URL field.
const CONNECTION_KEY_FIELDS = new Set(['TAUTULLI_API_KEY', 'PLEX_TOKEN', 'RADARR_API_KEY', 'SONARR_API_KEY', 'JELLYFIN_API_KEY']);
function _applyDetectedConnectionValues(values, { overwrite = false } = {}) {
  if (!values || typeof values !== 'object') return false;
  let changed = false;
  CONNECTION_KEY_FIELDS.forEach(name => {
    const value = values[name];
    if (value === null || value === undefined || String(value).trim() === '') return;
    const input = document.getElementById(name);
    if (!input) return;
    if (overwrite || !String(input.value || '').trim()) {
      const next = String(value);
      if (input.value !== next) {
        input.value = next;
        changed = true;
      }
    }
  });
  return changed;
}

async function autoDetectConnections({ overwrite = true, quiet = false } = {}) {
  const btn = document.getElementById('btn-auto-detect-connections');
  const status = document.getElementById('auto-detect-status');
  const lockReason = _sectionLockReason('connections');
  if (lockReason) {
    _applyConfigRuntimeLocks();
    if (!quiet) showToast(lockReason, 'warning');
    return false;
  }
  if (btn) { btn.disabled = true; btn.classList.add('btn-busy'); btn.textContent = 'Detecting…'; }
  if (status && !quiet) status.textContent = 'Detecting…';
  try {
    const r = await fetch('/api/connections/autodetect', { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Auto Detect failed.');
    const changed = _applyDetectedConnectionValues(d.values, { overwrite });
    _hideAllSecretFields();
    if (changed) {
      markDirty();
      if (_lastConfigHealth) _applyConfigHealth(_lastConfigHealth);
      if (status) status.textContent = 'Auto Detect filled the API keys it could read. Save them, then run Check for Errors.';
      if (!quiet) showToast('Auto Detect filled the available API keys.', 'success');
    } else {
      if (status) status.textContent = d.ok
        ? 'Auto Detect found keys, but the fields already matched them.'
        : 'No API keys found in appdata — enter them manually.';
      if (!quiet) showToast(status?.textContent || 'Auto Detect finished.', d.ok ? 'warning' : 'danger');
    }
    _syncDirtyState();
    return changed;
  } catch (e) {
    if (status) status.textContent = 'Auto Detect failed: ' + e.message;
    if (!quiet) showToast('Auto Detect failed: ' + e.message, 'danger');
    return false;
  } finally {
    if (btn) { btn.disabled = !!_sectionLockReason('connections'); btn.classList.remove('btn-busy'); btn.textContent = 'Auto Detect API Keys'; }
  }
}

// Animated open/close for the URLs and API Keys details, matching the
// accordion sections' slide. Opening animates on the toggle event — that
// covers the summary click AND the error-highlight code that sets .open
// directly. Closing intercepts the summary click so the content can slide
// shut before the details element actually closes.
function _animateSlideDetails(det) {
  const wrap = det?.querySelector('.details-anim');
  const summary = det?.querySelector('summary');
  if (!det || !wrap || !summary) return;
  // One persistent listener + a mode field instead of per-animation
  // add/remove: a missed transitionend (reduced motion, interrupted
  // animation) can then never leave a stale handler behind to sabotage
  // the next open.
  let mode = null; // 'open' | 'close' | null
  const settle = () => { wrap.style.height = ''; mode = null; };
  wrap.addEventListener('transitionend', (e) => {
    if (e.target !== wrap || e.propertyName !== 'height') return;
    if (mode === 'close') det.open = false;
    settle();
  });
  det.addEventListener('toggle', () => {
    if (!det.open || mode) return;
    mode = 'open';
    wrap.style.height = '0px';
    requestAnimationFrame(() => { wrap.style.height = wrap.scrollHeight + 'px'; });
    setTimeout(() => { if (mode === 'open') settle(); }, 500);
  });
  summary.addEventListener('click', (e) => {
    if (!det.open || mode) return; // opens are handled by the toggle event
    e.preventDefault();
    mode = 'close';
    wrap.style.height = wrap.scrollHeight + 'px';
    requestAnimationFrame(() => { wrap.style.height = '0px'; });
    setTimeout(() => { if (mode === 'close') { det.open = false; settle(); } }, 500);
  });
}
_animateSlideDetails(document.getElementById('manual-overrides'));
_animateSlideDetails(document.getElementById('notify-custom-details'));

_initSecretFields();
document.querySelectorAll('.secret-toggle').forEach(btn => {
  btn.addEventListener('click', (event) => {
    event.preventDefault();
    const name = btn.dataset.target;
    const input = document.getElementById(name);
    if (!name || !input || btn.disabled) return;
    _setSecretVisibility(name, !_isSecretRevealed(input));
  });
});


// ── Collapsed-section issue markers ──────────────────────────────────────────
// Validation and connection warnings can live inside collapsed accordion tabs.
// The section header marker that says so is in base.html, shared with the
// Filtering & Scoring accordion; this page only names what counts as an issue.
// A deselected server's fields dim and carry .connection-disabled — that is a
// state, not a fault, so only the highlighted ones flag the section.
const _cfgSectionIssues = prSectionIssues('cfg-accordion', {
  selector: ['.field-invalid', '.connection-highlight', '.mount-card-problem',
             '.config-status-box.status-error'].join(','),
  isIssue: el => !(el.classList.contains('connection-disabled')
                   && !el.classList.contains('connection-highlight')),
});

function _scheduleSectionIssueUpdate() { _cfgSectionIssues.schedule(); }

function _connectionOnboardingForceConnections() {
  try {
    return new URLSearchParams(window.location.search).get('onboarding') === 'connections';
  } catch (_) {
    return false;
  }
}

function _runConnectionOnboardingScroll() {
  // Jump to Connections (accordion open, API-field overrides expanded) both
  // during first-run onboarding and whenever the last probe found API errors
  // — the failing fields are already highlighted by _applyConfigHealth.
  if (!_connectionOnboardingActive && !_connectionErrorOnLoad) return;
  const forceConnections = _connectionOnboardingForceConnections();
  if (!_connectionErrorOnLoad && !forceConnections && window.location.hash && window.location.hash !== '#connections-section' && window.location.hash !== '#s-conn') return;

  const section = document.getElementById('s-conn');
  const target = document.getElementById('connections-section') || section;
  const details = document.getElementById('manual-overrides');
  if (details) details.open = true;

  if (section) {
    try {
      bootstrap.Collapse.getOrCreateInstance(section, { toggle: false }).show();
    } catch (_) {
      section.classList.add('show');
      document.querySelector('[data-bs-target="#s-conn"]')?.classList.remove('collapsed');
    }
  }

  prScrollTo(target, { delayMs: 140 });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _runConnectionOnboardingScroll, { once: true });
} else {
  _runConnectionOnboardingScroll();
}

// Deep link from the Dashboard's Cleanup Targets gear: open the collapsed
// accordion section and scroll to it. The explicit scroll (a beat after the
// connection-onboarding scroll's rAF+140ms) makes the user's clicked target
// win over the connection-error auto-scroll.
function _openSpaceThresholdsFromHash() {
  if (window.location.hash !== '#space-thresholds-section') return;
  _showSection('s-space');
  document.querySelector('[data-bs-target="#s-space"]')?.classList.remove('collapsed');
  prScrollTo(document.getElementById('space-thresholds-section'), { delayMs: 300 });
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _openSpaceThresholdsFromHash, { once: true });
} else {
  _openSpaceThresholdsFromHash();
}

// Show the delay/Redline advisory on first load too — the form is server-
// rendered (populateForm only runs after save/revert), so without this it would
// only appear once a field changed.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => _updateDelayRedlineAdvisory(), { once: true });
} else {
  _updateDelayRedlineAdvisory();
}

// First-launch time-zone auto-detect (base.html) fires this once the server has
// saved the browser's zone. Reflect it in the dropdown and treat it as the saved
// value so the form doesn't read as dirty (nothing was manually changed).
window.addEventListener('pr-timezone-initialized', (e) => {
  const tz = e && e.detail && e.detail.tz;
  if (!tz) return;
  const sel = document.getElementById('TIME_ZONE');
  if (sel) {
    if (![...sel.options].some(o => o.value === tz)) sel.add(new Option(tz, tz), sel.options[1] || null);
    sel.value = tz;
  }
  if (typeof _savedConfig === 'object' && _savedConfig) _savedConfig.TIME_ZONE = tz;
  window.PR_SERVER_TIME_ZONE = tz;
});

function _toggleButtonClass(el, enabledClass, disabledClass, enabled) {
  if (!el) return;
  el.classList.toggle(enabledClass, !!enabled);
  el.classList.toggle(disabledClass, !enabled);
  el.disabled = !enabled;
  el.setAttribute('aria-disabled', enabled ? 'false' : 'true');
}

function _revertStyle(on) {
  ['btn-revert'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const enabled = !!on && !_savePending;
    el.classList.toggle('btn-revert-active', enabled);
    el.disabled = !enabled;
    el.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  });
}

function _saveStyle(on) {
  ['btn-save'].forEach(id => {
    const el = document.getElementById(id);
    if (_savePending) {
      if (!el) return;
      el.classList.add('btn-success');
      el.classList.remove('btn-outline-secondary');
      el.disabled = true;
      el.setAttribute('aria-disabled', 'true');
      el.setAttribute('aria-busy', 'true');
      return;
    }
    if (el) el.removeAttribute('aria-busy');
    _toggleButtonClass(el, 'btn-success', 'btn-outline-secondary', on);
  });
}

function _sectionResetStyle(section, on) {
  const el = document.getElementById(`btn-reset-${section}`);
  if (!el) return;
  const locked = section === 'space' && _spaceThresholdsLocked();
  const missingSavedLibrary = section === 'space' && !_spaceThresholdsHaveUsableLibrary();
  const enabled = !!on && !locked && !missingSavedLibrary;
  el.disabled = !enabled;
  el.setAttribute('aria-disabled', enabled ? 'false' : 'true');
}

function _numOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function _positiveNumOrNull(value) {
  const n = _numOrNull(value);
  return n !== null && n > 0 ? n : null;
}
function _str(value) { return value === null || value === undefined ? '' : String(value); }
function _strOrNull(value) {
  return value === null || value === undefined || value === '' ? null : String(value);
}
function _arrayOfStrings(value) {
  return Array.isArray(value) ? value.map(v => String(v ?? '').trim()).filter(Boolean) : [];
}
function _librarySuffix(value) {
  let raw = String(value ?? '').trim().replaceAll('\\', '/');
  if (!raw) return '';
  if (raw === '/' || raw === LIBRARY_ROOT || raw === LIBRARY_ROOT_NAME) return '/';
  if (raw.startsWith(LIBRARY_ROOT + '/')) raw = raw.slice(LIBRARY_ROOT.length + 1);
  else if (raw.startsWith(LIBRARY_ROOT_NAME + '/')) raw = raw.slice(LIBRARY_ROOT_NAME.length + 1);
  else raw = raw.replace(/^\/+/, '');
  return raw.replace(/^\/+/, '').replace(/\/+$/, '');
}
function _libraryPath(value) {
  const suffix = _librarySuffix(value);
  if (suffix === '/') return LIBRARY_ROOT;
  return suffix ? `${LIBRARY_ROOT}/${suffix}` : '';
}
function _monitorDirsFromInputs() {
  return [...document.querySelectorAll('.monitor-dir')]
    .map(i => _libraryPath(i.value))
    .filter(Boolean);
}
function _hasSavedMonitorDirs() {
  return _monitorDirsFromConfig(_savedConfig).length > 0;
}
function _monitorDirsFromConfig(cfg) {
  return _arrayOfStrings(cfg?.MONITOR_DIRS).map(_libraryPath).filter(Boolean);
}
function _monitorDirInputSuffixes() {
  return [...document.querySelectorAll('.monitor-dir')].map(i => _librarySuffix(i.value));
}
function _monitorDirSuffixesFromConfig(cfg) {
  return _monitorDirsFromConfig(cfg).map(p => _librarySuffix(p));
}
function _arraysMatch(a, b) {
  return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
}
function _monitorDirRowsMatchConfig(cfg) {
  return _arraysMatch(_monitorDirInputSuffixes(), _monitorDirSuffixesFromConfig(cfg));
}
// The notification fields, once. Reading the form, writing it, comparing it for
// dirty state, resetting the section, and the test-send payload all need the
// same nineteen keys. Listed separately that is five lists to keep in step, and
// a default that drifts between two of them reads as a form dirty on load.
// kind: check = boolean · text = trimmed string · int = whole number, min 1 ·
// lines = one URL per line in the box, an array in the config.
const _NOTIFY_FIELDS = [
  ['NOTIFY_ENABLED',            'check', false],
  ['NOTIFY_ON_RUN_SUMMARY',     'check', true],
  ['NOTIFY_IN_MONITOR_ONLY',    'check', false],
  ['NOTIFY_ON_MARKED_CHANGES',  'check', true],
  ['NOTIFY_ON_LOW_SPACE',       'check', true],
  ['NOTIFY_SHOW_MOVIES',        'check', false],
  ['NOTIFY_LOW_SPACE_GB',       'int',   25],
  ['NOTIFY_ON_ERROR',           'check', false],
  ['NOTIFY_DISCORD_WEBHOOK',    'text',  ''],
  ['NOTIFY_TELEGRAM_BOT_TOKEN', 'text',  ''],
  ['NOTIFY_TELEGRAM_CHAT_ID',   'text',  ''],
  ['NOTIFY_SLACK_WEBHOOK',      'text',  ''],
  ['NOTIFY_PUSHOVER_USER_KEY',  'text',  ''],
  ['NOTIFY_PUSHOVER_TOKEN',     'text',  ''],
  ['NOTIFY_NTFY_TOPIC',         'text',  ''],
  ['NOTIFY_NTFY_SERVER',        'text',  ''],
  ['NOTIFY_GOTIFY_SERVER',      'text',  ''],
  ['NOTIFY_GOTIFY_TOKEN',       'text',  ''],
  ['NOTIFY_CUSTOM_URLS',        'lines', []],
];
const _NOTIFY_KEYS = _NOTIFY_FIELDS.map(([key]) => key);

function _canonicalConfig(cfg) {
  cfg = cfg || {};
  const monitorDirs = _monitorDirsFromConfig(cfg);
  return {
    RUN_MODE: cfg.RUN_MODE === 'headroom' ? 'headroom' : (cfg.RUN_MODE === 'off' ? 'off' : 'paused'),
    HEADROOM_GB: _numOrNull(cfg.HEADROOM_GB),
    REDLINE_GB: _numOrNull(cfg.REDLINE_GB),
    REDLINE_ONLY_MODE: !!cfg.REDLINE_ONLY_MODE,
    MAX_LIBRARY_GB: _positiveNumOrNull(cfg.MAX_LIBRARY_GB),
    DELETE_DELAY_DAYS: _numOrNull(cfg.DELETE_DELAY_DAYS) ?? 0,
    MAX_HEADROOM_PCT: _numOrNull(cfg.MAX_HEADROOM_PCT),
    OUTPUT_DIR: _str(cfg.OUTPUT_DIR),
    CHECK_PATH: LIBRARY_ROOT,
    MONITOR_DIRS: monitorDirs,
    MOVIE_EXTENSIONS: _arrayOfStrings(cfg.MOVIE_EXTENSIONS),
    PROTECTED_COLLECTIONS: [..._arrayOfStrings(cfg.PROTECTED_COLLECTIONS)].sort(),
    USE_PLEX: !!cfg.USE_PLEX,
    USE_JELLYFIN: !!cfg.USE_JELLYFIN,
    JELLYFIN_URL: _str(cfg.JELLYFIN_URL),
    JELLYFIN_API_KEY: _str(cfg.JELLYFIN_API_KEY),
    JELLYFIN_PROTECTED_COLLECTIONS: [..._arrayOfStrings(cfg.JELLYFIN_PROTECTED_COLLECTIONS)].sort(),
    // On/off is all the user controls now — the stored value may be "auto" or
    // the detected concrete section id, so comparing the raw string would mark
    // the form dirty forever after a detection lands.
    RADARR_OVERSEERR_SECTION_ID: _strOrNull(cfg.RADARR_OVERSEERR_SECTION_ID) === null ? null : 'auto',
    _RADARR_DETECTED_SECTION_ID: _strOrNull(cfg._RADARR_DETECTED_SECTION_ID),
    _RADARR_DETECTED_SECTION_NAME: _strOrNull(cfg._RADARR_DETECTED_SECTION_NAME),
    _RADARR_DETECTED_SECTION_METHOD: _strOrNull(cfg._RADARR_DETECTED_SECTION_METHOD),
    _RADARR_DETECTED_SECTION_METHOD_LABEL: _strOrNull(cfg._RADARR_DETECTED_SECTION_METHOD_LABEL),
    IMDB_RATINGS_URL: _str(cfg.IMDB_RATINGS_URL),
    IMDB_RATINGS_MAX_AGE_DAYS: _numOrNull(cfg.IMDB_RATINGS_MAX_AGE_DAYS),
    LOG_RETENTION_DAYS: (cfg.LOG_RETENTION_DAYS ?? 30),
    KEEP_INTERRUPTED_LOGS: !!cfg.KEEP_INTERRUPTED_LOGS,
    DEBUG_MODE: !!cfg.DEBUG_MODE,
    TIME_ZONE: _str(cfg.TIME_ZONE || 'auto'),
    DISPLAY_TIME_FORMAT: _str(cfg.DISPLAY_TIME_FORMAT || '12h'),
    DAILY_RUN_TIME: _str(cfg.DAILY_RUN_TIME || '00:00'),
    TAUTULLI_APPDATA: _str(cfg.TAUTULLI_APPDATA),
    SONARR_APPDATA: _str(cfg.SONARR_APPDATA),
    RADARR_APPDATA: _str(cfg.RADARR_APPDATA),
    TAUTULLI_URL: _str(cfg.TAUTULLI_URL),
    TAUTULLI_API_KEY: _str(cfg.TAUTULLI_API_KEY),
    PLEX_URL: _str(cfg.PLEX_URL),
    PLEX_TOKEN: _str(cfg.PLEX_TOKEN),
    RADARR_URL: _str(cfg.RADARR_URL),
    RADARR_API_KEY: _str(cfg.RADARR_API_KEY),
    SONARR_URL: _str(cfg.SONARR_URL),
    SONARR_API_KEY: _str(cfg.SONARR_API_KEY),
    SONARR_CLEANUP_ENABLED: cfg.SONARR_CLEANUP_ENABLED === true,
    // Notifications — included so edits here arm Save/Revert. The defaults in
    // _NOTIFY_FIELDS mirror notify.TOGGLE_DEFAULTS: the three alert types that
    // report on the library are pre-selected, the ones that add detail or
    // widen scope are not.
    ...Object.fromEntries(_NOTIFY_FIELDS.map(([key, kind, dflt]) => [key,
      kind === 'check' ? !!(cfg[key] ?? dflt)
      : kind === 'int' ? (_numOrNull(cfg[key]) ?? dflt)
      : kind === 'lines' ? _arrayOfStrings(cfg[key])
      : _str(cfg[key])])),
  };
}
function _stableStringify(value) {
  if (Array.isArray(value)) return '[' + value.map(_stableStringify).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + _stableStringify(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}
function _configsMatch(a, b) {
  const ca = _canonicalConfig(a);
  const cb = _canonicalConfig(b);
  // With no monitored directories saved, the Space Threshold inputs are locked
  // and the form deliberately reports them zeroed — the user can't touch them,
  // so a difference against the saved defaults isn't an unsaved change (it
  // made fresh installs load permanently "dirty" with Revert armed).
  if (!_spaceThresholdsHaveUsableLibrary() && !_hasSavedMonitorDirs()) {
    ['HEADROOM_GB', 'REDLINE_GB', 'MAX_LIBRARY_GB'].forEach(k => { delete ca[k]; delete cb[k]; });
  }
  // While the saved Automatic Cleanup is blocked (connection down / bad thresholds on
  // load), the form deliberately presents the saved non-deleting mode and the
  // Automatic Cleanup radio is
  // disabled — the user CANNOT reproduce the saved 'headroom' state. Compare
  // both sides as the effective presented mode, or the page loads dirty with
  // an un-cleanable Revert loop (Revert re-checks the disabled radio, which
  // immediately snaps back and differs again).
  // NOT during the transient run lock, though — that disables ALL the radios
  // without changing what the user can reproduce once the run ends, and
  // collapsing the modes there silently discarded an unsaved Automatic
  // Cleanup selection (form read "clean", Save disarmed, beforeunload guard
  // stopped warning) whenever a run started mid-edit.
  const cleanupRadio = document.getElementById('mode-headroom');
  if (cleanupRadio && cleanupRadio.disabled && !_configActivityLocked()) {
    if (ca.RUN_MODE === 'headroom') ca.RUN_MODE = 'paused';
    if (cb.RUN_MODE === 'headroom') cb.RUN_MODE = 'paused';
  }
  // Same collapse for a ghosted Monitor Only (incomplete setup): the form
  // presents fully-Paused, so compare a saved 'paused' as 'off' too — else a
  // forced pause that left 'paused' saved reads permanently dirty.
  const monitorRadioCmp = document.getElementById('mode-paused');
  if (monitorRadioCmp && monitorRadioCmp.disabled && !_configActivityLocked()) {
    if (ca.RUN_MODE === 'paused') ca.RUN_MODE = 'off';
    if (cb.RUN_MODE === 'paused') cb.RUN_MODE = 'off';
  }
  return _stableStringify(ca) === _stableStringify(cb);
}

const SECTION_DEFAULT_FIELDS = {
  space: ['HEADROOM_GB', 'REDLINE_GB', 'REDLINE_ONLY_MODE', 'MAX_LIBRARY_GB', 'DELETE_DELAY_DAYS'],
  advanced: ['IMDB_RATINGS_URL', 'IMDB_RATINGS_MAX_AGE_DAYS', 'LOG_RETENTION_DAYS', 'KEEP_INTERRUPTED_LOGS', 'DEBUG_MODE', 'MAX_HEADROOM_PCT'],
  notifications: _NOTIFY_KEYS,
};


function _isCleanupRunMode(mode) {
  return mode === 'headroom';
}

function _spaceThresholdsLocked() {
  // Only an active run locks the thresholds — while automatic mode is merely
  // armed they stay editable, and saving a change force-pauses Automatic Cleanup.
  return _configActivityLocked();
}

function _setDisabled(id, disabled) {
  const el = document.getElementById(id);
  if (el) el.disabled = !!disabled;
}

// ── Appearance ───────────────────────────────────────────────────────────────
// Stored per browser in a cookie rather than in config.json, but it still goes
// through Save. It COULD apply on the tick — nothing here needs the server —
// and it did for a while. It reads wrong: every other control on this page
// waits for Save, so one pair that does not makes the button look like it
// missed them, and leaves no way to change your mind but change it back.
// So these arm Save and Revert like everything else, and land with them.
// Kept in step with the markup above, so restoring it reads identically.
const _GLASS_HELP = 'The frosted glass behind the header and buttons. '
  + 'Turn it off if scrolling feels slow. This browser only.';

const _appearanceForm = () => ({
  effectsOff: !!document.getElementById('REDUCE_VISUAL_EFFECTS')?.checked,
  glassOff: !!document.getElementById('DISABLE_GLASS_BLUR')?.checked,
});
function _appearanceDirty() {
  const now = _appearanceForm();
  return now.effectsOff !== _savedAppearance.effectsOff
      || now.glassOff !== _savedAppearance.glassOff;
}
// Ticking only moves the form: the ghosting follows at once (it describes the
// pending state, not the live one) and Save arms. Nothing is applied yet.
function _onAppearanceToggled() {
  _applyGlassBlurAvailability();
  markDirty();
}
// Save landed. Write the cookies so the next load is stamped this way, and
// apply it to the page already on screen so the save is visible.
function commitAppearanceSettings() {
  const now = _appearanceForm();
  window.prSetAppearanceCookie('mr_effects', now.effectsOff);
  window.prSetAppearanceCookie('mr_glass', now.glassOff);
  window.prApplyAppearance(now);
  _savedAppearance = now;
}
function revertAppearanceSettings() {
  const cb = (id, on) => { const el = document.getElementById(id); if (el) el.checked = on; };
  cb('REDUCE_VISUAL_EFFECTS', _savedAppearance.effectsOff);
  cb('DISABLE_GLASS_BLUR', _savedAppearance.glassOff);
  _applyGlassBlurAvailability();
}

// Reduce visual effects already turns the blur off, so the standalone switch
// has nothing left to say while it is on — ghost it rather than leave a live
// control that changes nothing. Its own checked state is deliberately left
// alone: it is this browser's preference for when the broader setting goes
// back off, and silently rewriting it would lose that.
function _applyGlassBlurAvailability() {
  const covered = !!document.getElementById('REDUCE_VISUAL_EFFECTS')?.checked;
  // The run lock owns `disabled` while it holds; unghosting here would hand
  // back a control the lock had taken away.
  if (!covered && document.getElementById('DISABLE_GLASS_BLUR')?.dataset.runLocked) return;
  _setDisabled('DISABLE_GLASS_BLUR', covered);
  const help = document.getElementById('glass-blur-help');
  if (help) {
    help.textContent = covered
      ? 'Already off - Reduce visual effects includes it.'
      : _GLASS_HELP;
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _applyGlassBlurAvailability, { once: true });
} else {
  _applyGlassBlurAvailability();
}

function _applySpaceThresholdLock() {
  const locked = _spaceThresholdsLocked();
  const selectedApisReady = _selectedMediaApisReady();
  const hasSavedDirs = _hasSavedMonitorDirs();
  const hasMonitorDirs = _spaceThresholdsHaveUsableLibrary();
  const section = document.getElementById('space-thresholds-section');
  const note = document.getElementById('space-threshold-lock-note');
  const apiNote = document.getElementById('space-threshold-api-note');
  const pathsNote = document.getElementById('space-threshold-paths-note');
  const resetBtn = document.getElementById('btn-reset-space');
  const headroomInput = document.getElementById('HEADROOM_GB');
  const redlineToggle = document.getElementById('redline-enabled');
  const redlineInput = document.getElementById('REDLINE_GB');
  const redlineOn = !!document.getElementById('redline-enabled')?.checked;

  section?.classList.toggle('space-thresholds-locked', locked);
  document.getElementById('s-space')?.classList.toggle('section-run-ghost', locked);
  if (note) {
    const reason = locked ? _sectionNoticeText(_sectionLockReason('space')) : '';
    if (reason) {
      note.textContent = reason;
      note.hidden = false;
    } else if (!locked && _savedLiveModeLocked()) {
      // Not a lock — a heads-up that a threshold save reconciles the plan in place and
      // keeps automatic mode running, pausing only if the change leaves it over a limit.
      note.textContent = 'Automatic Cleanup is on — a threshold change rebuilds the deletion plan in place and keeps running; it only pauses to Monitor Only if the change leaves the library over a limit.';
      note.hidden = false;
    } else {
      note.hidden = true;
    }
  }
  if (apiNote) apiNote.hidden = selectedApisReady || locked;
  if (pathsNote) pathsNote.hidden = hasMonitorDirs || !selectedApisReady || locked;
  if (resetBtn) resetBtn.disabled = locked || !hasMonitorDirs;

  if (!hasSavedDirs) {
    // Pre-onboarding presentation mirrors the SAVED state — on a fresh
    // install that is every threshold off: headroom unticked, blank value.
    const headroomToggle = document.getElementById('headroom-enabled');
    if (headroomToggle) headroomToggle.checked = !_savedConfig?.REDLINE_ONLY_MODE;
    if (headroomInput) headroomInput.value =
      (Number(_savedConfig?.HEADROOM_GB) || 0) > 0 ? _savedConfig.HEADROOM_GB : '';
    if (redlineToggle) redlineToggle.checked = false;
    if (redlineInput) redlineInput.value = '';
    _headroomValidationArmed = false;
    _redlineValidationArmed = false;
  }

  const headroomOn = _headroomEnabled();
  _setDisabled('headroom-enabled', locked || !hasMonitorDirs);
  _setDisabled('HEADROOM_GB', locked || !hasMonitorDirs || !headroomOn);
  // Redline and the cap toggle freely whatever the Headroom state — with Headroom
  // off either one (or both) can be the trigger, so neither is force-locked.
  _setDisabled('redline-enabled', locked || !hasMonitorDirs);
  _setDisabled('REDLINE_GB', locked || !hasMonitorDirs || !redlineOn);
  // The delay paces daily marking runs. It is retired only in TRUE redline-only mode
  // (Headroom off AND no cap); a cap-driven daily cleanup still honors it.
  const capOnForDelay = !!document.getElementById('cap-enabled')?.checked;
  const delayRetired = locked || !hasMonitorDirs || (!headroomOn && !capOnForDelay);
  _setDisabled('DELETE_DELAY_DAYS', delayRetired);
  // The reset button additionally needs marks to reset, a current plan, and a
  // SAVED delay value — resetting applies the saved setting, so while the field
  // holds an unsaved edit the button waits for Save or Revert to settle it.
  _setDisabled('btn-reset-delays',
               delayRetired || _simulateRequired || !(_markedClockedCount > 0)
               || _delayValueEdited());
  _updateRedlineOnlyUI();
  _applySpaceThresholdAvailability();
}

function _applySpaceThresholdAvailability() {
  const locked = _spaceThresholdsLocked();
  const selectedApisReady = _selectedMediaApisReady();
  const hasSavedDirs = _hasSavedMonitorDirs();
  const hasMonitorDirs = _spaceThresholdsHaveUsableLibrary();
  const capToggle = document.getElementById('cap-enabled');
  const capInput = document.getElementById('MAX_LIBRARY_GB');
  const capNote = document.getElementById('cap-paths-note');
  const capGroup = document.getElementById('cap-input-group');

  if (capToggle && !hasSavedDirs) {
    capToggle.checked = false;
    _capValidationArmed = false;
  }
  if (capInput && !hasSavedDirs) capInput.value = '';

  const capOn = !!capToggle?.checked;
  // The cap rides the daily schedule and is independent of Headroom — a cap-only
  // config (Headroom off) is valid, so the toggle is never gated on Headroom.
  if (capToggle) capToggle.disabled = locked || !hasMonitorDirs;
  if (capInput) capInput.disabled = locked || !hasMonitorDirs || !capOn;
  if (capNote) {
    capNote.hidden = hasMonitorDirs;
    capNote.textContent = selectedApisReady
      ? 'Save a monitored directory first.'
      : (!_filesystemReady() ? _filesystemBlockingText() : _mediaServerBlockText());
  }
  capGroup?.classList.toggle('connection-disabled', !hasMonitorDirs);
}

function _applyLibraryCapAvailability() {
  _applySpaceThresholdAvailability();
}

function _sectionValues(cfg, section) {
  const canonical = _canonicalConfig(cfg);
  const out = {};
  (SECTION_DEFAULT_FIELDS[section] || []).forEach(k => { out[k] = canonical[k]; });
  return out;
}

function _sectionMatchesDefault(section) {
  try { return _stableStringify(_sectionValues(getFormConfig(), section)) === _stableStringify(_sectionValues(_defaultConfig, section)); }
  catch { return false; }
}
function _optionalEnabledInvalidDirty() {
  return (_spaceThresholdsHaveUsableLibrary() && document.getElementById('cap-enabled')?.checked && !_libraryCapConfigured())
    || (_spaceThresholdsHaveUsableLibrary() && document.getElementById('redline-enabled')?.checked && (!_nonNegativeNumberFilled('REDLINE_GB') || _redlineExceedsHeadroom()))
    || (_spaceThresholdsHaveUsableLibrary() && !_headroomHasValue());
}

function _hasUnsavedChanges() {
  try {
    return !_configsMatch(getFormConfig(), _savedConfig) || !_monitorDirRowsMatchConfig(_savedConfig) || _optionalEnabledInvalidDirty() || _appearanceDirty();
  } catch { return _dirty; }
}

function _syncSectionResetState() {
  Object.keys(SECTION_DEFAULT_FIELDS).forEach(section => {
    _sectionResetStyle(section, !_sectionMatchesDefault(section));
  });
}


function _syncDirtyState() {
  const isDirty = !_configInvalidLocked && _hasUnsavedChanges();
  _dirty = isDirty;
  _revertStyle(isDirty);
  _saveStyle(isDirty);
  _syncSectionResetState();
  _applyImdbDownloadButtonState();
  return isDirty;
}
function markDirty() { _syncDirtyState(); }
function clearDirty() {
  _dirty = false;
  _monitorDirsValidationArmed = false;
  validateMonitorDirs();
  _revertStyle(false);
  _saveStyle(false);
  _syncSectionResetState();
  _applyImdbDownloadButtonState();
}

// Detect any change inside the form
document.getElementById('cfg-form').addEventListener('input',  markDirty);
document.getElementById('cfg-form').addEventListener('change', markDirty);
document.getElementById('monitor-dirs')?.addEventListener('input', (event) => {
  if (event.target?.classList?.contains('monitor-dir')) {
    _applySpaceThresholdLock();
    validateHeadroom({ show: false, focus: false });
    validateRedline({ show: false, focus: false });
    validateLibraryCap({ show: false, focus: false });
    _updatePills();
  }
  if (event.target?.classList?.contains('monitor-dir') && _monitorDirsValidationArmed) {
    const row = event.target.closest('.monitor-dir-row');
    if (row) row.dataset.monitorIssue = '';
    validateMonitorDirs();
    _debounceMonitorDirRemoteValidation();
    _syncDirtyState();
  }
});

// ── Dynamic rows ──────────────────────────────────────────────────────────────

const _escapeAttr = prHtmlEsc;  // shared with Filtering & Scoring (base.html)

function addMonitorDir(value = '', { mark = true, focus = true } = {}) {
  const pathsEditable = _savedMediaApiConnected();
  if (!pathsEditable && mark) {
    _applyMoviePathAvailability();
    showToast(_mediaServerBlockText(), 'warning');
    return;
  }
  const el = document.getElementById('monitor-dirs');
  const row = document.createElement('div');
  row.className = 'monitor-dir-wrapper mb-2';
  row.innerHTML = `
    <div class="input-group monitor-dir-row">
      <input type="text" class="form-control monitor-dir" value="${_escapeAttr(_librarySuffix(value))}" placeholder="Movies" aria-label="Monitored library subfolder">
      <button type="button" class="btn btn-outline-secondary" onclick="openLibraryBrowser(this)">Browse…</button>
      <button type="button" class="btn btn-outline-secondary monitor-remove-btn" onclick="removeMonitorDir(this)" aria-label="Remove this monitored folder" title="Remove"><span aria-hidden="true">✕</span></button>
    </div>
    <div class="field-error monitor-dir-error"></div>`;
  el.appendChild(row);
  const input = row.querySelector('.monitor-dir');
  _applyMoviePathAvailability();
  _applySpaceThresholdLock();
  if (mark) markDirty();
  if (focus && input) setTimeout(() => input.focus(), 0);
}

function removeMonitorDir(button) {
  if (!_savedMediaApiConnected()) {
    _applyMoviePathAvailability();
    showToast(_mediaServerBlockText(), 'warning');
    return;
  }
  button?.closest?.('.monitor-dir-wrapper')?.remove();
  _applySpaceThresholdLock();
  if (_monitorDirsValidationArmed) validateMonitorDirs();
  validateHeadroom({ show: false, focus: false });
  validateRedline({ show: false, focus: false });
  validateLibraryCap({ show: false, focus: false });
  _updatePills();
  markDirty();
}

let _libraryBrowserTargetInput = null;
let _libraryBrowserCurrentPath = LIBRARY_ROOT;
let _libraryBrowserParentPath = LIBRARY_ROOT;

function _browserSuffix(path) {
  return _librarySuffix(path);
}


function openLibraryBrowser(button) {
  if (!_savedMediaApiConnected()) {
    _applyMoviePathAvailability();
    showToast(_mediaServerBlockText(), 'warning');
    return;
  }
  const row = button?.closest?.('.monitor-dir-row');
  _libraryBrowserTargetInput = row?.querySelector?.('.monitor-dir') || null;
  const start = _libraryBrowserTargetInput?.value ? _libraryPath(_libraryBrowserTargetInput.value) : LIBRARY_ROOT;
  browseLibraryPath(start);
  const modalEl = document.getElementById('library-browser-modal');
  if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

async function browseLibraryPath(path = LIBRARY_ROOT) {
  const list = document.getElementById('library-browser-list');
  const empty = document.getElementById('library-browser-empty');
  const err = document.getElementById('library-browser-error');
  const cur = document.getElementById('library-browser-current');
  const up = document.getElementById('library-browser-up');
  const choose = document.getElementById('library-browser-choose');
  if (list) list.innerHTML = '<div class="form-text">Loading…</div>';
  if (empty) empty.classList.add('d-none');
  if (err) { err.classList.add('d-none'); err.textContent = ''; }

  try {
    const r = await fetch('/api/library/browse?path=' + encodeURIComponent(path || LIBRARY_ROOT));
    const d = await r.json();
    if (!r.ok || d.ok === false) throw new Error(d.error || 'Unable to browse library.');

    _libraryBrowserCurrentPath = d.path || LIBRARY_ROOT;
    _libraryBrowserParentPath = d.parent || LIBRARY_ROOT;
    if (cur) cur.textContent = _libraryBrowserCurrentPath;
    // At the library root the suffix is '/' — truthy — so "!suffix" never
    // disabled anything. Up has nowhere to go at the root; Choose stays
    // enabled ('/' is a legal monitored value meaning the whole library).
    if (up) up.disabled = _browserSuffix(_libraryBrowserCurrentPath) === '/';
    if (choose) choose.disabled = !_browserSuffix(_libraryBrowserCurrentPath);

    if (list) {
      list.innerHTML = '';
      (d.dirs || []).forEach(item => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'list-group-item list-group-item-action library-browser-row';
        btn.textContent = '📁 ' + item.name;
        btn.addEventListener('click', () => browseLibraryPath(item.path));
        list.appendChild(btn);
      });
    }
    if (empty) empty.classList.toggle('d-none', !!(d.dirs || []).length);
  } catch (e) {
    if (list) list.innerHTML = '';
    if (err) { err.textContent = e.message; err.classList.remove('d-none'); }
  }
}

function chooseLibraryBrowserPath() {
  if (!_libraryBrowserTargetInput) return;
  // Refuse a folder another row already monitors — leave the picker open so a
  // different one can be chosen instead of landing a doomed duplicate row.
  const chosen = _libraryPath(_browserSuffix(_libraryBrowserCurrentPath));
  const taken = [...document.querySelectorAll('.monitor-dir')]
    .some(el => el !== _libraryBrowserTargetInput && _libraryPath(el.value) === chosen);
  if (chosen && taken) {
    showToast('That folder is already monitored — pick a different one.', 'warning');
    return;
  }
  _libraryBrowserTargetInput.value = _browserSuffix(_libraryBrowserCurrentPath);
  _libraryBrowserTargetInput.dispatchEvent(new Event('input', { bubbles: true }));
  const modalEl = document.getElementById('library-browser-modal');
  if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).hide();
}

// ── Populate form from a config object ───────────────────────────────────────

function populateForm(cfg) {
  const sv  = (id, val) => { const e = document.getElementById(id); if (e) e.value = val ?? ''; };
  const sc  = (id, on)  => { const e = document.getElementById(id); if (e) e.checked = !!on; };
  const sd  = (id, dis) => { const e = document.getElementById(id); if (e) e.disabled = dis; };
  const cfgHasMonitorDirs = _monitorDirsFromConfig(cfg).length > 0;

  // Absent means ON: the safety demote is the shipped behaviour, so a config
  // without the key must not render as someone having turned it off.
  sc('PAUSE_CLEANUP_ON_STARTUP', cfg.PAUSE_CLEANUP_ON_STARTUP !== false);
  const uiMode = cfg.RUN_MODE === 'headroom' ? 'headroom' : (cfg.RUN_MODE === 'off' ? 'off' : 'paused');
  const modeEl = document.querySelector(`input[name="RUN_MODE"][value="${uiMode}"]`);
  if (modeEl) modeEl.checked = true;

  // The checkbox mirrors the stored flag (derived server-side from the
  // value: armed iff >= 1). The unticked ghosted field shows the remembered
  // value like the other optional thresholds — never a literal 0, which is
  // what unticking stores, not something anyone typed.
  const hasHeadroom = !cfg.REDLINE_ONLY_MODE;
  sc('headroom-enabled', hasHeadroom); sd('HEADROOM_GB', !hasHeadroom);
  sv('HEADROOM_GB', hasHeadroom ? cfg.HEADROOM_GB : (cfg._HEADROOM_GB_LAST ?? ''));
  _headroomValidationArmed = false;
  validateHeadroom();
  const hasRedline = cfgHasMonitorDirs && cfg.REDLINE_GB !== null && cfg.REDLINE_GB !== undefined;
  sc('redline-enabled', hasRedline); sd('REDLINE_GB', !hasRedline);
  // A disabled field keeps its last entered value visible (grayed out).
  sv('REDLINE_GB', hasRedline ? cfg.REDLINE_GB : (cfg._REDLINE_GB_LAST ?? ''));
  _redlineValidationArmed = false;
  validateRedline();

  const hasCap = cfgHasMonitorDirs && cfg.MAX_LIBRARY_GB !== null && cfg.MAX_LIBRARY_GB !== undefined && Number(cfg.MAX_LIBRARY_GB) > 0;
  sc('cap-enabled', hasCap); sd('MAX_LIBRARY_GB', !hasCap);
  sv('MAX_LIBRARY_GB', hasCap ? cfg.MAX_LIBRARY_GB : (cfg._MAX_LIBRARY_GB_LAST ?? ''));
  _capValidationArmed = false;

  sv('DELETE_DELAY_DAYS', cfg.DELETE_DELAY_DAYS ?? 0);
  _deleteDelayValidationArmed = false;
  validateDeleteDelay();

  _monitorDirsValidationArmed = false;
  document.getElementById('monitor-dirs').innerHTML = '';
  (cfg.MONITOR_DIRS || []).forEach(dir => addMonitorDir(dir, { mark: false, focus: false }));
  validateMonitorDirs();
  _applySpaceThresholdLock();
  validateLibraryCap();
  // After monitored dirs are populated (so "usable library" is known) — the
  // per-field validators above ran before that, when the advisory would hide.
  _updateDelayRedlineAdvisory();

  sc('USE_PLEX', cfg.USE_PLEX);
  sc('USE_JELLYFIN', cfg.USE_JELLYFIN);
  sv('JELLYFIN_URL', cfg.JELLYFIN_URL);
  sv('JELLYFIN_API_KEY', cfg.JELLYFIN_API_KEY);
  initCollections(cfg);
  onServerSoftwareChange();
  syncSavedVisibilitySections();

  [   'TAUTULLI_URL','TAUTULLI_API_KEY','PLEX_URL','PLEX_TOKEN',
   'RADARR_URL','RADARR_API_KEY','SONARR_URL','SONARR_API_KEY'].forEach(k => sv(k, cfg[k]));
  sc('sonarr-cleanup-enabled', cfg.SONARR_CLEANUP_ENABLED === true);
  _hideAllSecretFields();

  const hasSection = cfg.RADARR_OVERSEERR_SECTION_ID !== null && cfg.RADARR_OVERSEERR_SECTION_ID !== undefined;
  sc('section-enabled', hasSection);
  _syncSavedRadarrSectionDetection(cfg);

  sv('IMDB_RATINGS_URL', cfg.IMDB_RATINGS_URL);
  sv('IMDB_RATINGS_MAX_AGE_DAYS', cfg.IMDB_RATINGS_MAX_AGE_DAYS);
  sv('LOG_RETENTION_DAYS', cfg.LOG_RETENTION_DAYS ?? 30);
  sc('KEEP_INTERRUPTED_LOGS', cfg.KEEP_INTERRUPTED_LOGS);
  sc('DEBUG_MODE', cfg.DEBUG_MODE);
  sv('TIME_ZONE', cfg.TIME_ZONE || 'auto');
  sv('DISPLAY_TIME_FORMAT', cfg.DISPLAY_TIME_FORMAT || '12h');
  sv('DAILY_RUN_TIME', cfg.DAILY_RUN_TIME || '00:00');
  // Save and Revert both land here — re-render the clocks for whichever
  // format the form now holds.
  _applyDisplayTimeFormat();
  sv('MAX_HEADROOM_PCT', cfg.MAX_HEADROOM_PCT);
  _maxHeadroomPctValidationArmed = false;
  validateMaxHeadroomPct();
  _NOTIFY_FIELDS.forEach(([key, kind, dflt]) => {
    if (kind === 'check') return sc(key, cfg[key] ?? dflt);
    if (kind === 'lines') return sv(key, Array.isArray(cfg[key]) ? cfg[key].join('\n')
                                                                : (cfg[key] || ''));
    // A number field keeps a saved 0 and falls back only when the key is
    // absent; a text field blanks on anything falsy, so a hand-edited 0 in a
    // webhook shows as empty rather than as the string "0".
    sv(key, kind === 'int' ? (cfg[key] ?? dflt) : (cfg[key] || ''));
  });
  syncNotifyEnabled();
  _updatePills();
  syncDebugModeVisibility();
  if (_lastConfigHealth) _applyConfigHealth(_lastConfigHealth);
  _applyConfigRuntimeLocks();
}

// ── Read form values ──────────────────────────────────────────────────────────

function getFormConfig() {
  const v   = (id) => document.getElementById(id)?.value ?? '';
  const n   = (id) => parseFloat(v(id)) || 0;
  const requiredNumber = (id) => prBlankNumber(id);  // blank/garbage -> null
  const chk = (id) => document.getElementById(id)?.checked;
  const thresholdsUsable = _spaceThresholdsHaveUsableLibrary();
  const savedHeadroom = _numOrNull(_savedConfig?.HEADROOM_GB) ?? 0;
  const savedRedline = _numOrNull(_savedConfig?.REDLINE_GB);
  const savedLibraryCap = _positiveNumOrNull(_savedConfig?.MAX_LIBRARY_GB);
  return {
    RUN_MODE: document.querySelector('input[name="RUN_MODE"]:checked')?.value ?? 'paused',
    PAUSE_CLEANUP_ON_STARTUP: chk('PAUSE_CLEANUP_ON_STARTUP'),
    // Headroom's toggle stores 0 when off (redline-only mode) — the same
    // value-memory pattern as Redline/Cap, with 0 instead of null. While the
    // section is locked (no saved dirs, or the media API is down) the SAVED
    // values ride through untouched: fabricating 0/null here read as a broken
    // redline-only request server-side and made onboarding saves impossible.
    HEADROOM_GB: thresholdsUsable ? (chk('headroom-enabled') ? (requiredNumber('HEADROOM_GB') ?? 0) : 0)
                                  : savedHeadroom,
    // The Headroom checkbox IS the mode switch — unticked saves the flag.
    REDLINE_ONLY_MODE: thresholdsUsable ? !chk('headroom-enabled')
                                        : !!_savedConfig?.REDLINE_ONLY_MODE,
    REDLINE_GB:  thresholdsUsable ? (chk('redline-enabled') ? requiredNumber('REDLINE_GB') : null) : savedRedline,
    MAX_LIBRARY_GB: thresholdsUsable ? (chk('cap-enabled') ? n('MAX_LIBRARY_GB') : null) : savedLibraryCap,
    // Whole days a movie stays marked before deletion; blank saves as 0.
    DELETE_DELAY_DAYS: thresholdsUsable ? (prBlankNumber('DELETE_DELAY_DAYS', 1) ?? 1)
                                        : (_numOrNull(_savedConfig?.DELETE_DELAY_DAYS) ?? 1),
    // Text still sitting in a disabled optional field rides along so the
    // server remembers it (shown grayed out; survives restarts).
    _REDLINE_GB_LAST: (thresholdsUsable && !chk('redline-enabled')) ? requiredNumber('REDLINE_GB') : undefined,
    _MAX_LIBRARY_GB_LAST: (thresholdsUsable && !chk('cap-enabled')) ? requiredNumber('MAX_LIBRARY_GB') : undefined,
    _HEADROOM_GB_LAST: (thresholdsUsable && !chk('headroom-enabled')) ? requiredNumber('HEADROOM_GB') : undefined,
    MAX_HEADROOM_PCT: n('MAX_HEADROOM_PCT'),
    OUTPUT_DIR: _savedConfig.OUTPUT_DIR,
    CHECK_PATH: LIBRARY_ROOT,
    MONITOR_DIRS: _monitorDirsFromInputs(),
    MOVIE_EXTENSIONS: _savedConfig.MOVIE_EXTENSIONS,
    PROTECTED_COLLECTIONS: Array.from(_collectionSelections.plex),
    USE_PLEX: chk('USE_PLEX'),
    USE_JELLYFIN: chk('USE_JELLYFIN'),
    JELLYFIN_URL: v('JELLYFIN_URL').trim(),
    JELLYFIN_API_KEY: v('JELLYFIN_API_KEY').trim(),
    JELLYFIN_PROTECTED_COLLECTIONS: Array.from(_collectionSelections.jellyfin),
    // Filtering & Scoring fields are edited on their own page (/explorer) and
    // saved via /api/score-config — the Config save deliberately omits them
    // and the backend carries the saved values through untouched.
    RADARR_OVERSEERR_SECTION_ID: chk('section-enabled') ? 'auto' : null,
    _RADARR_DETECTED_SECTION_ID: _radarrDetectedSectionValue(),
    _RADARR_DETECTED_SECTION_NAME: _radarrDetectedSectionName(),
    _RADARR_DETECTED_SECTION_METHOD: _radarrDetectedSectionMethod(),
    _RADARR_DETECTED_SECTION_METHOD_LABEL: _radarrDetectedSectionMethodLabel(),
    IMDB_RATINGS_URL:             v('IMDB_RATINGS_URL').trim(),
    // Blank falls back to the default (7): parseInt('') is NaN, which
    // serialized as null and made the save 400 with no field highlight.
    IMDB_RATINGS_MAX_AGE_DAYS:    Math.max(1, parseInt(v('IMDB_RATINGS_MAX_AGE_DAYS')) || 7),
    LOG_RETENTION_DAYS:           Math.max(0, parseInt(v('LOG_RETENTION_DAYS')) || 0),
    KEEP_INTERRUPTED_LOGS:        chk('KEEP_INTERRUPTED_LOGS'),
    DEBUG_MODE:                   chk('DEBUG_MODE'),
    TIME_ZONE:                    (v('TIME_ZONE').trim() || 'auto'),
    DAILY_RUN_TIME:               (v('DAILY_RUN_TIME').trim() || '00:00'),
    DISPLAY_TIME_FORMAT:          v('DISPLAY_TIME_FORMAT') || '12h',
    TAUTULLI_APPDATA:  TAUTULLI_APPDATA_DIR,
    RADARR_APPDATA:    RADARR_APPDATA_DIR,
    SONARR_APPDATA:    SONARR_APPDATA_DIR,
    TAUTULLI_URL:      v('TAUTULLI_URL'),
    TAUTULLI_API_KEY:  v('TAUTULLI_API_KEY'),
    PLEX_URL:          v('PLEX_URL'),
    PLEX_TOKEN:        v('PLEX_TOKEN'),
    RADARR_URL:        v('RADARR_URL'),
    SONARR_URL:        v('SONARR_URL'),
    SONARR_API_KEY:    v('SONARR_API_KEY'),
    SONARR_CLEANUP_ENABLED: chk('sonarr-cleanup-enabled') === true,
    RADARR_API_KEY:    v('RADARR_API_KEY'),
    ...Object.fromEntries(_NOTIFY_FIELDS.map(([key, kind, dflt]) => [key,
      kind === 'check' ? chk(key)
      : kind === 'int' ? Math.max(1, parseInt(v(key)) || dflt)
      : kind === 'lines' ? v(key).split('\n').map(s => s.trim()).filter(Boolean)
      : v(key).trim()])),
  };
}

// ── Save ─────────────────────────────────────────────────────────────────────

// ── Value pills in run mode cards ────────────────────────────────────────────

let _capValidationArmed = false;
let _headroomValidationArmed = false;
let _maxHeadroomPctValidationArmed = false;
let _redlineValidationArmed = false;
let _monitorDirsValidationArmed = false;

// prFieldRaw / prNumOrNull / prPositiveNumber / prInvalidVisible /
// prBlankNumber / prWireBlurValidation / prApplyFieldFlag are shared with
// the Filtering & Scoring page — defined in base.html.

function _selectNonDeletingMode() {
  // Fall back to the user's SAVED non-deleting mode: someone whose saved mode is
  // fully Paused ("off") must not be silently flipped to Monitor Only when the
  // Automatic Cleanup radio becomes unavailable mid-edit — a reflexive Save
  // would then partially re-start their stopped scheduler.
  const id = _savedConfig?.RUN_MODE === 'off' ? 'mode-off' : 'mode-paused';
  const status = document.getElementById(id);
  if (status && !status.checked) {
    status.checked = true;
    _syncDirtyState();
  }
}

function _libraryCapConfigured() {
  const capOn = document.getElementById('cap-enabled')?.checked;
  return !!capOn && prPositiveNumber(prFieldRaw('MAX_LIBRARY_GB'));
}

function _headroomEnabled() {
  return !!document.getElementById('headroom-enabled')?.checked;
}

// The Headroom the form would save: 0 when the toggle is off (redline-only
// mode), else the field number (blank/garbage = null = invalid).
function _headroomFormValue() {
  if (!_headroomEnabled()) return 0;
  return prBlankNumber('HEADROOM_GB');
}

function _headroomHasValue() {
  // Unticked is always a valid 0 (the trigger is off); ticked requires a real
  // target of at least 1 GB. There is no ticked-at-0 third state: 0 is what
  // unticking STORES, not a value anyone types. Blank/garbage stays invalid.
  if (!_headroomEnabled()) return true;
  const v = prBlankNumber('HEADROOM_GB');
  return v !== null && v >= 1;
}

// True when a REAL daily-cleanup target exists (ticked with a value >= 1) —
// the predicate the cap/delay availability and the headroom-off notes key on.
// The checkbox alone can't answer that: ticked with 0 is still "off".
function _headroomActive() {
  const v = _headroomFormValue();
  return v !== null && v >= 1;
}

function _maxHeadroomPctValid() {
  // Match the server rule (0 < n <= 100): accepting any n > 0 here let a
  // typo like 200 pass client validation, silently disarm the safety-limit
  // math (200% of the disk trips nothing), and only fail at save time as a
  // toast with no field highlight.
  const n = prBlankNumber('MAX_HEADROOM_PCT');
  return n !== null && n > 0 && n <= 100;
}

function _nonNegativeNumberFilled(id) {
  const n = prBlankNumber(id);
  return n !== null && n >= 0;
}

function _redlineExceedsHeadroom() {
  // While Headroom is ticked (a real target of >= 1 GB), Redline must sit
  // STRICTLY below it — a tie would enforce the full target on every check.
  // Unticked, there is no target and no ceiling.
  if (!_headroomEnabled()) return false;
  const redline = prNumOrNull(prFieldRaw('REDLINE_GB'));
  const headroom = prBlankNumber('HEADROOM_GB');
  return redline !== null && headroom !== null && redline >= headroom;
}

function _syncRedlineMax() {
  const input = document.getElementById('REDLINE_GB');
  const headroom = _headroomFormValue();
  if (!input) return;
  if (headroom !== null && headroom > 0) input.setAttribute('max', String(headroom));
  else input.removeAttribute('max');
}

function _redlineErrorText() {
  if (_redlineExceedsHeadroom()) {
    return prBlankNumber('HEADROOM_GB') === 0
      ? 'Headroom is 0 — untick it (redline-only mode) to run Redline on its own.'
      : 'Redline must be lower than Headroom — for the full target on every check, untick Headroom instead.';
  }
  return 'Enter a Redline above 0 GB before saving, or uncheck this option.';
}

function _showSection(sectionId) {
  const section = document.getElementById(sectionId);
  if (section && window.bootstrap) bootstrap.Collapse.getOrCreateInstance(section, { toggle: false }).show();
}

function _focusAfterSectionOpens(input) {
  if (input) setTimeout(() => input.focus(), 100);
}

function _headroomSafetyLimitGb() {
  const total = Number(_diskStats?.total_gb);
  const pct = Number(prFieldRaw('MAX_HEADROOM_PCT'));
  if (!Number.isFinite(total) || total <= 0 || !_maxHeadroomPctValid()) return null;
  return total * pct / 100;
}

function _headroomSafetyExceeded() {
  // The safety cap bounds the free-space floor the system maintains: the
  // Headroom target when one exists (>= 1), else the Redline floor.
  const floor = _headroomActive()
    ? _headroomFormValue()
    : (document.getElementById('redline-enabled')?.checked ? prNumOrNull(prFieldRaw('REDLINE_GB')) : null);
  const limit = _headroomSafetyLimitGb();
  return floor !== null && limit !== null && floor > limit;
}

function _headroomSafetyText() {
  const which = _headroomActive() ? 'Headroom' : 'Redline';
  const limit = _headroomSafetyLimitGb();
  if (limit === null) return `${which} exceeds the safety cap. Lower ${which} or raise the safety cap to enable Automatic Cleanup.`;
  return `${which} exceeds the safety cap (${Math.floor(limit).toLocaleString()} GB max). Lower ${which} or raise the safety cap to enable Automatic Cleanup.`;
}

// The safety percentage also floors the Library Size Cap: a Cleanup may delete
// at most that percentage of the library, so the cap can't sit below
// (library - max_pct% of library). Simulate is exempt (mirrors Headroom).
function _libraryCapSafetyFloorGb() {
  const lib = Number(_lastKnownLibraryGb);
  const pct = Number(prFieldRaw('MAX_HEADROOM_PCT'));
  if (!Number.isFinite(lib) || lib <= 0 || !_maxHeadroomPctValid()) return null;
  return lib * (100 - pct) / 100;
}

function _libraryCapSafetyExceeded() {
  if (!document.getElementById('cap-enabled')?.checked) return false;
  const cap = prNumOrNull(prFieldRaw('MAX_LIBRARY_GB'));
  const floor = _libraryCapSafetyFloorGb();
  return cap !== null && floor !== null && cap < floor;
}

function _libraryCapSafetyText() {
  const floor = _libraryCapSafetyFloorGb();
  if (floor === null) return 'Library Size Cap is below the safety floor. Raise the cap or the safety percentage to enable Automatic Cleanup.';
  return `Library Size Cap is below the safety floor (${Math.ceil(floor).toLocaleString()} GB min). Raise the cap or the safety percentage to enable Automatic Cleanup.`;
}

function _spaceThresholdBlockingText() {
  if (!_selectedMediaApisReady()) return 'Connect or uncheck the selected media server first.';
  if (!_hasSavedMonitorDirs()) return 'Save a monitored directory to enable Automatic Cleanup.';
  if (!_headroomHasValue()) return 'Enter a Headroom target of 1 GB or more (or disable Headroom) to enable Automatic Cleanup.';
  if (!_maxHeadroomPctValid()) return 'Enter a valid Headroom safety percentage.';
  const redlineOn = document.getElementById('redline-enabled')?.checked;
  if (redlineOn && !prPositiveNumber(prFieldRaw('REDLINE_GB'))) return 'Enter a Redline above 0 GB, or disable Redline.';
  if (redlineOn && _redlineExceedsHeadroom()) return 'Redline cannot be higher than Headroom.';
  const capOn = document.getElementById('cap-enabled')?.checked;
  if (capOn && !_libraryCapConfigured()) return 'Enter a Library Size Cap, or disable it.';
  // Automatic Cleanup needs at least one active space target; Headroom 0 with Redline and
  // the Library Size Cap both off leaves nothing to enforce.
  if (_noEffectiveSpaceLimit()) return 'Set a Headroom target, Redline, or Library Size Cap to enable Automatic Cleanup.';
  if (_headroomSafetyExceeded()) return _headroomSafetyText();
  if (_libraryCapSafetyExceeded()) return _libraryCapSafetyText();
  return '';
}

function _noEffectiveSpaceLimit() {
  const headroom = _headroomFormValue();
  const redlineOn = !!document.getElementById('redline-enabled')?.checked;
  const capOn = !!document.getElementById('cap-enabled')?.checked;
  return headroom === 0 && !redlineOn && !capOn;
}
function _spaceThresholdUnavailableText(action = 'configuring Space Thresholds') {
  return _selectedMediaApisReady()
    ? `Save a monitored directory before ${action}.`
    : `Connect or uncheck the selected media server before ${action}.`;
}
function _currentConnectionBlockingText() {
  const usePlex = !!document.getElementById('USE_PLEX')?.checked;
  const useJf = !!document.getElementById('USE_JELLYFIN')?.checked;
  if (!usePlex && !useJf) return 'Select Plex or Jellyfin first.';
  if (!_selectedMediaApisReady()) {
    // Prefer the server's specific reason (e.g. which connection failed and
    // why) over the generic instruction when the last probe reported one.
    return (!_requiredConnectionOk && _requiredConnectionMessage)
      || 'Connect or uncheck the selected media server first.';
  }
  return '';
}

function _monitorDirIssue(input) {
  const raw = String(input?.value ?? '').trim().replaceAll('\\', '/');
  if (!raw) return 'blank';
  if (raw.startsWith('/') && raw !== '/' && raw !== LIBRARY_ROOT && !raw.startsWith(LIBRARY_ROOT + '/')) return 'invalid';
  const suffix = _librarySuffix(raw);
  if (!suffix) return 'blank';
  const parts = suffix.split('/').filter(Boolean);
  if (suffix.includes('//') || parts.some(p => p === '.' || p === '..')) return 'invalid';
  if (_monitorDirIsDuplicate(input)) return 'duplicate';
  return input?.closest?.('.monitor-dir-row')?.dataset?.monitorIssue || '';
}

// True when an EARLIER row already monitors the same folder — the first row
// keeps its clean state, every repeat flags, so removing the repeats resolves
// it. Compared on the resolved path so "Movies", "/Movies" and the full
// library path all count as the same folder.
function _monitorDirIsDuplicate(input) {
  const mine = _libraryPath(input?.value);
  if (!mine) return false;
  const inputs = [...document.querySelectorAll('.monitor-dir')];
  const idx = inputs.indexOf(input);
  return inputs.slice(0, idx).some(el => _libraryPath(el.value) === mine);
}

function _monitorDirErrorText(issue) {
  if (issue === 'blank') return 'Enter a folder, use /, or remove this row.';
  if (issue === 'missing') return `That folder does not exist under ${LIBRARY_ROOT}.`;
  if (issue === 'duplicate') return 'Already monitored — remove this duplicate row.';
  return `Enter a valid ${LIBRARY_ROOT} folder, or remove this row.`;
}

function _setMonitorDirRowState(row, issue, visible) {
  const input = row?.querySelector?.('.monitor-dir');
  const wrapper = row?.closest?.('.monitor-dir-wrapper');
  const err = wrapper?.querySelector?.('.monitor-dir-error');
  if (err) err.textContent = visible ? _monitorDirErrorText(issue) : '';
  prApplyFieldFlag(row, err, input, visible);
}

async function _monitorDirExists(input) {
  const path = _libraryPath(input?.value);
  if (!path) return false;
  const r = await fetch('/api/library/validate?path=' + encodeURIComponent(path));
  const d = await r.json().catch(() => ({}));
  return !!(r.ok && d.ok);
}

let _monitorDirRemoteTimer = null;
function _debounceMonitorDirRemoteValidation() {
  clearTimeout(_monitorDirRemoteTimer);
  _monitorDirRemoteTimer = setTimeout(() => {
    if (_monitorDirsValidationArmed) validateMonitorDirsRemote({ show: false, focus: false });
  }, 350);
}

function validateMonitorDirs({ show = false, focus = false } = {}) {
  if (show) _monitorDirsValidationArmed = true;
  const rows = [...document.querySelectorAll('.monitor-dir-row')];
  let firstInvalid = null;
  let anyInvalid = false;

  rows.forEach(row => {
    const input = row.querySelector('.monitor-dir');
    const issue = _monitorDirIssue(input);
    const invalid = !!issue;
    anyInvalid = anyInvalid || invalid;
    const visible = invalid && _monitorDirsValidationArmed;
    _setMonitorDirRowState(row, issue, visible);
    if (visible && !firstInvalid) firstInvalid = input;
  });

  if (focus && firstInvalid) {
    _showSection('s-paths');
    _focusAfterSectionOpens(firstInvalid);
  }

  if (_monitorDirsValidationArmed && !anyInvalid) _monitorDirsValidationArmed = false;
  return _monitorDirsValidationArmed ? !anyInvalid : true;
}

async function validateMonitorDirsRemote({ show = false, focus = false } = {}) {
  if (show) _monitorDirsValidationArmed = true;

  // First catch blank/malformed values locally.
  const formatOk = validateMonitorDirs({ show, focus: false });
  const rows = [...document.querySelectorAll('.monitor-dir-row')];
  if (!formatOk) {
    if (focus) {
      const first = rows.find(row => row.classList.contains('field-invalid'))?.querySelector('.monitor-dir');
      if (first) { _showSection('s-paths'); _focusAfterSectionOpens(first); }
    }
    return false;
  }

  if (show) _monitorDirsValidationArmed = true;

  let firstInvalid = null;
  let anyInvalid = false;
  for (const row of rows) {
    const input = row.querySelector('.monitor-dir');
    const path = _libraryPath(input?.value);
    if (!path) continue;
    const ok = await _monitorDirExists(input).catch(() => false);
    row.dataset.monitorIssue = ok ? '' : 'missing';
    const issue = ok ? '' : 'missing';
    anyInvalid = anyInvalid || !!issue;
    const visible = !!issue && _monitorDirsValidationArmed;
    _setMonitorDirRowState(row, issue, visible);
    if (visible && !firstInvalid) firstInvalid = input;
  }

  if (focus && firstInvalid) {
    _showSection('s-paths');
    _focusAfterSectionOpens(firstInvalid);
  }
  if (_monitorDirsValidationArmed && !anyInvalid) _monitorDirsValidationArmed = false;
  return _monitorDirsValidationArmed ? !anyInvalid : true;
}

function validateHeadroom({ show = false, focus = false } = {}) {
  const input = document.getElementById('HEADROOM_GB');
  const field = document.getElementById('headroom-field');
  const err = document.getElementById('headroom-error');
  if (!_spaceThresholdsHaveUsableLibrary() || !_headroomEnabled()) {
    // Disabled (redline-only mode): the ghosted field only displays the
    // remembered value — never flag it.
    prApplyFieldFlag(field, err, input, false);
    return true;
  }
  const invalid = !_headroomHasValue();

  if (show) _headroomValidationArmed = true;
  // Same focus suppression as Redline/Cap/Safety-%: don't flash the error
  // mid-edit while the field is focused (select-all-and-retype went red).
  const visible = prInvalidVisible(invalid, _headroomValidationArmed, input, field);
  prApplyFieldFlag(field, err, input, visible);

  if (visible && focus && input) {
    _showSection('s-space');
    _focusAfterSectionOpens(input);
  }

  return !invalid;
}

function validateMaxHeadroomPct({ show = false, focus = false } = {}) {
  const input = document.getElementById('MAX_HEADROOM_PCT');
  const field = document.getElementById('max-headroom-pct-field');
  const err = document.getElementById('max-headroom-pct-error');
  const invalid = !_maxHeadroomPctValid();

  if (show) _maxHeadroomPctValidationArmed = true;
  const visible = prInvalidVisible(invalid, _maxHeadroomPctValidationArmed, input, field);
  prApplyFieldFlag(field, err, input, visible);

  if (visible && focus && input) {
    _showSection('s-adv');
    _focusAfterSectionOpens(input);
  }

  return !invalid;
}

function validateRedline({ show = false, focus = false } = {}) {
  const redlineOn = document.getElementById('redline-enabled')?.checked;
  const input = document.getElementById('REDLINE_GB');
  const field = document.getElementById('redline-field');
  const err = document.getElementById('redline-error');
  const noTriggerNote = document.getElementById('no-trigger-note');
  if (noTriggerNote) {
    noTriggerNote.hidden = !(_spaceThresholdsHaveUsableLibrary() && !_headroomActive()
                             && !redlineOn && !document.getElementById('cap-enabled')?.checked);
  }
  if (!_spaceThresholdsHaveUsableLibrary()) {
    prApplyFieldFlag(field, err, input, false);
    return true;
  }
  _syncRedlineMax();
  const missing = !!redlineOn && !prPositiveNumber(prFieldRaw('REDLINE_GB'));
  const aboveHeadroom = !!redlineOn && !missing && _redlineExceedsHeadroom();
  const invalid = missing || aboveHeadroom;

  if (show) _redlineValidationArmed = true;
  const visible = prInvalidVisible(invalid, _redlineValidationArmed, input, field);

  if (err) err.textContent = _redlineErrorText();
  prApplyFieldFlag(field, err, input, visible);

  if (visible && focus && input) {
    _showSection('s-space');
    _focusAfterSectionOpens(input);
  }

  _updateDelayRedlineAdvisory();
  return !invalid;
}

// A deletion delay defers freeing marks for N days. Redline is the only
// trigger that bypasses the delay (deletes immediately when free space is
// critically low). Without it, a fast-filling library can run out of space
// before marks age out — advise (don't require) a Redline floor in that case.
function _updateDelayRedlineAdvisory() {
  const note = document.getElementById('delay-redline-advisory');
  if (!note) return;
  const delay = prBlankNumber('DELETE_DELAY_DAYS', 1) ?? 1;
  const redlineOn = !!document.getElementById('redline-enabled')?.checked;
  // 1 day is the minimum grace everyone has; only flag a LONGER delay (2+),
  // where the overgrowth window without a Redline floor is meaningfully wider.
  // Redline-only mode retires the delay entirely, so the advisory is moot.
  const show = _spaceThresholdsHaveUsableLibrary() && _headroomEnabled() && delay >= 2 && !redlineOn;
  if (show) {
    note.textContent = `No Redline floor set — with a ${delay}-day delay, the disk could fill before marks age out. A Redline floor deletes immediately, skipping the delay.`;
  }
  note.hidden = !show;
}

function validateLibraryCap({ show = false, focus = false } = {}) {
  _applyLibraryCapAvailability();
  if (!_spaceThresholdsHaveUsableLibrary()) return true;

  const capOn = document.getElementById('cap-enabled')?.checked;
  const input = document.getElementById('MAX_LIBRARY_GB');
  const group = document.getElementById('cap-input-group');
  const err = document.getElementById('cap-error');
  const invalid = !!capOn && !_libraryCapConfigured();

  if (show) _capValidationArmed = true;
  const visible = prInvalidVisible(invalid, _capValidationArmed, input, group);
  prApplyFieldFlag(group, err, input, visible);

  if (visible && focus && input) {
    _showSection('s-space');
    _focusAfterSectionOpens(input);
  }

  return !invalid;
}

let _deleteDelayValidationArmed = false;
function _deleteDelayValid() {
  const n = prBlankNumber('DELETE_DELAY_DAYS', 1);   // blank = the 1-day minimum
  return n !== null && n >= 1 && n <= 365 && Number.isInteger(n);
}
function validateDeleteDelay({ show = false, focus = false } = {}) {
  const input = document.getElementById('DELETE_DELAY_DAYS');
  const group = document.getElementById('delete-delay-field');
  const err = document.getElementById('delete-delay-error');
  if (!_spaceThresholdsHaveUsableLibrary()) {
    prApplyFieldFlag(group, err, input, false);
    return true;
  }
  const invalid = !_deleteDelayValid();
  if (show) _deleteDelayValidationArmed = true;
  const visible = prInvalidVisible(invalid, _deleteDelayValidationArmed, input, group);
  prApplyFieldFlag(group, err, input, visible);
  if (visible && focus && input) {
    _showSection('s-space');
    _focusAfterSectionOpens(input);
  }
  _updateDelayRedlineAdvisory();
  return !invalid;
}

function toggleLibraryCap() {
  if (_spaceThresholdsLocked()) {
    _applySpaceThresholdLock();
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (!_spaceThresholdsHaveUsableLibrary()) {
    _applySpaceThresholdLock();
    showToast(_spaceThresholdUnavailableText('enabling Library Size Cap'), 'warning');
    return;
  }
  const enabled = document.getElementById('cap-enabled')?.checked;
  const input = document.getElementById('MAX_LIBRARY_GB');
  if (input) input.disabled = !enabled;
  _capValidationArmed = false;
  _applySpaceThresholdLock();   // the delay field/notes track the cap when Headroom is off
  validateLibraryCap();
  if (enabled && input) setTimeout(() => { input.focus(); validateLibraryCap(); }, 0);
  _updatePills();
}

// Headroom-off UI: the notes under Redline/Cap/Delay that explain what a
// disabled Headroom changes. The ghosting itself lives in the lock/availability
// functions so every path (populate, toggles, saves) stays consistent.
function _updateRedlineOnlyUI() {
  const usable = _spaceThresholdsHaveUsableLibrary();
  // The delay note explains TRUE redline-only mode: Headroom off AND no cap, so
  // Redline is the only trigger and the delay is retired. A cap-driven daily
  // cleanup (Headroom off WITH a cap) still uses the delay, so the note stays hidden.
  const capOn = !!document.getElementById('cap-enabled')?.checked;
  const trueRedlineOnly = usable && !_headroomEnabled() && !capOn;
  const delayNote = document.getElementById('delay-redline-only-note');
  if (delayNote) delayNote.hidden = !trueRedlineOnly;
  // The no-trigger warning: nothing armed at all — no real Headroom target,
  // no Redline floor, no Library Size Cap.
  const noTrigger = document.getElementById('no-trigger-note');
  if (noTrigger) {
    noTrigger.hidden = !(usable && !_headroomActive()
                         && !document.getElementById('redline-enabled')?.checked
                         && !document.getElementById('cap-enabled')?.checked);
  }
}

function toggleHeadroom() {
  const box = document.getElementById('headroom-enabled');
  if (_spaceThresholdsLocked()) {
    if (box) box.checked = !box.checked;   // revert — locked during a run
    _applySpaceThresholdLock();
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (!_spaceThresholdsHaveUsableLibrary()) {
    if (box) box.checked = !_savedConfig?.REDLINE_ONLY_MODE;   // revert to saved
    _applySpaceThresholdLock();
    showToast(_spaceThresholdUnavailableText('changing Headroom'), 'warning');
    return;
  }
  const enabled = !!box?.checked;
  _headroomValidationArmed = false;
  _applySpaceThresholdLock();
  validateHeadroom();
  validateRedline();
  validateLibraryCap();
  validateDeleteDelay();
  if (enabled) {
    const input = document.getElementById('HEADROOM_GB');
    setTimeout(() => { input?.focus(); validateHeadroom(); }, 0);
  }
  _updatePills();
  _syncDirtyState();
}

function toggleRedline() {
  if (_spaceThresholdsLocked()) {
    _applySpaceThresholdLock();
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (!_spaceThresholdsHaveUsableLibrary()) {
    _applySpaceThresholdLock();
    showToast(_spaceThresholdUnavailableText('enabling Redline'), 'warning');
    return;
  }
  const enabled = document.getElementById('redline-enabled')?.checked;
  const input = document.getElementById('REDLINE_GB');
  if (input) input.disabled = !enabled;
  _redlineValidationArmed = false;
  _applySpaceThresholdLock();   // the delay note tracks Redline when Headroom is off
  validateRedline();
  if (enabled && input) setTimeout(() => { input.focus(); validateRedline(); }, 0);
  _updatePills();
}

function toggleSectionId() {
  if (_lastConfigHealth) _applyConfigHealth(_lastConfigHealth);
}

function _setRunModeDisabled(modeName, disabled, alert = false) {
  const mode = document.getElementById(`mode-${modeName}`);
  const card = document.getElementById(`mode-card-${modeName}`);
  if (!mode || !card) return;
  mode.disabled = !!disabled;
  card.classList.toggle('run-mode-disabled', !!disabled);
  card.classList.toggle('run-mode-disabled--alert', !!disabled && !!alert);
  card.setAttribute('aria-disabled', disabled ? 'true' : 'false');
  // A setting that belongs to this mode goes with it. The card already paints
  // its controls not-allowed when the mode is blocked, and one that still took
  // a click would be making a promise the mode cannot keep.
  card.querySelectorAll('.run-mode-option input').forEach(el => { el.disabled = !!disabled; });
}

// True only when Automatic Cleanup is blocked specifically by a safety-percentage violation
// (headroom or library cap) — not a hard block like a missing connection.
function _liveBlockIsSafety() {
  if (_currentConnectionBlockingText()) return false;
  const t = _spaceThresholdBlockingText();
  return t !== '' && (t === _headroomSafetyText() || t === _libraryCapSafetyText());
}

function _updateRunModeAvailability() {
  // During a run the whole Scheduler Mode category is ghosted like every other
  // one. It says nothing itself — the page-level note above the accordion is
  // the single place that reports the run.
  const runLocked = _configActivityLocked();
  document.getElementById('s-mode')?.classList.toggle('section-run-ghost', runLocked);
  const offRadio = document.getElementById('mode-off');
  if (offRadio) offRadio.disabled = runLocked;
  // Monitor Only needs a working setup: a media server enabled AND a
  // monitored path saved AND an up-to-date library database; until then it is
  // ghosted and the scheduler rests at fully-Paused. Keyed to the SAVED
  // selection (like the startup check), not live connectivity — probe caches
  // can lag a save, and a transient API outage must never ghost a configured
  // Monitor Only; a failing selected server is auto-deselected on save, which
  // re-ghosts through this rule. A saved mode already running counts as
  // scan-ok so the ACTIVE mode's radio never ghosts under itself.
  const monitorSetupOk = _hasSavedMonitorDirs()
    && !!(_savedConfig?.USE_PLEX || _savedConfig?.USE_JELLYFIN);
  // Monitor Only also needs SOMETHING to monitor: an armed space trigger —
  // a Headroom target, a Redline floor, or a Library Size Cap. Keyed to the
  // SAVED values like the server selection above, so mid-edit field states
  // never flicker the radio.
  const monitorTriggerOk = (Number(_savedConfig?.HEADROOM_GB) || 0) > 0
    || _savedConfig?.REDLINE_GB != null || _savedConfig?.MAX_LIBRARY_GB != null;
  const monitorScanOk = _monitorScanOk
    || _savedConfig?.RUN_MODE === 'paused' || _savedConfig?.RUN_MODE === 'headroom';
  // The server words this (_scan_lock_reason) — it can tell "never scanned"
  // from "the last scan FAILED" and from "the scan aged out", and telling
  // someone to run the Simulate they just ran is how this reads as broken.
  // Only the setup-incomplete case is decidable client-side, and it is the one
  // the user can fix on this page without a reload.
  const monitorBlock = !monitorSetupOk
    ? 'Connect a media server and add a monitored path first.'
    : (!monitorTriggerOk
        ? 'Arm a space threshold first — a Headroom target, Redline floor, or Library Size Cap gives Monitor Only something to preview.'
        : (!monitorScanOk ? (_monitorLockedReason || 'Run Simulate first — this enables automatically once the scan finishes.') : ''));
  _setRunModeDisabled('paused', runLocked || !!monitorBlock);
  const descPaused = document.getElementById('desc-paused');
  if (descPaused) descPaused.textContent = monitorBlock || descPaused.dataset.enabledText;
  ['DAILY_RUN_TIME', 'TIME_ZONE', 'DISPLAY_TIME_FORMAT'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.disabled = runLocked; el.setAttribute('aria-disabled', runLocked ? 'true' : 'false'); }
  });

  // The simulate-required ghost only gates ARMING Automatic Cleanup (Monitor Only → Automatic Cleanup) — an
  // already-armed Automatic Cleanup must never be dropped by a status poll. The
  // server words the reason (first-scan proof, over-limits plan, or the
  // Redline preview).
  const simBlock = (_simulateRequired && !_isCleanupRunMode(_savedConfig?.RUN_MODE))
    ? (_simulateRequiredMsg || 'Run Simulate first, then enable Automatic Cleanup.') : '';
  // Debug mode and Automatic Cleanup are mutually exclusive: while Debug mode is ticked, Automatic Cleanup is
  // ghosted with this reason (Debug mode never runs cleanup deletions). It takes
  // precedence over the threshold/connection reasons — Debug mode is the immediate
  // blocker the user just toggled, and it can't be a safety-percentage alert — and
  // it ghosts on page load, not only after a click.
  const debugOn = !!document.getElementById('DEBUG_MODE')?.checked;
  const debugBlock = debugOn
    ? 'Debug mode is on — turn it off to enable Automatic Cleanup. Debug mode never runs cleanup deletions.' : '';
  const liveBlock = debugBlock || _currentConnectionBlockingText() || _spaceThresholdBlockingText() || simBlock;

  _setRunModeDisabled('headroom', runLocked || !!liveBlock, !debugOn && _liveBlockIsSafety());

  const descHeadroom = document.getElementById('desc-headroom');
  if (descHeadroom) {
    descHeadroom.textContent = liveBlock || descHeadroom.dataset.enabledText;
  }

  // Fall back to the saved non-deleting mode only when Automatic Cleanup is
  // genuinely unavailable — never for a
  // transient run lock, which must not change the saved selection.
  const cleanupMode = document.getElementById('mode-headroom');
  if (!runLocked && cleanupMode?.disabled && cleanupMode.checked) {
    _selectNonDeletingMode();
  }
  // Same for a ghosted Monitor Only: the presented selection falls back to
  // fully-Paused (the server's rest state) rather than sitting on a radio the
  // user can't reproduce.
  const monitorRadio = document.getElementById('mode-paused');
  if (!runLocked && monitorRadio?.disabled && monitorRadio.checked) {
    const offR = document.getElementById('mode-off');
    if (offR && !offR.checked) {
      offR.checked = true;
      _syncDirtyState();
    }
  }
}


// ── Where you stand ─────────────────────────────────────────────────────────
// Each threshold's trigger point, computed rather than left to the reader.
// The engine's formulas, mirrored (see _deletion_limits_exceeded in app.py):
//   Headroom  used >= total - target   ⟺  free space AT OR BELOW the target
//   Redline   free <= floor                (same axis: free space)
//   Cap       library size > cap           (a different axis: the library)
// `pct` is the fraction of the way to firing — 100% is the moment the
// threshold trips, not the moment the disk is full. The bar above the rows
// shows the disk itself, so the fraction is never drawn on its own — it exists
// to decide the "nearly there" warning color.
// `pending` is getFormConfig()'s output, passed in by the one caller so the
// form is read once per update rather than once per consumer.
function _thresholdRows(pending) {
  const total = Number(_diskStats?.total_gb);
  const free = Number(_diskStats?.free_gb);
  const used = Number(_diskStats?.used_gb);
  const lib = Number(_lastKnownLibraryGb);
  // Same gate as the figures above: with nothing monitored the last reading is
  // never cleared, and quoting "X GB free now" off a stale one is worse than
  // saying the disk is not readable yet.
  const diskOk = _configMonitoringActive
    && [total, free, used].every(v => Number.isFinite(v) && v >= 0) && total > 0;
  const libOk = _configMonitoringActive && Number.isFinite(lib) && lib > 0;
  // The thresholds come from getFormConfig() rather than the inputs: while the
  // section is locked a save ignores what the fields hold and writes the saved
  // values, and a panel quoting a trigger the save would discard is worse than
  // no panel. This is the same object the save posts.
  const armed = v => (Number.isFinite(Number(v)) && Number(v) > 0) ? Number(v) : null;
  const headroom = armed(pending.HEADROOM_GB);
  const redline = armed(pending.REDLINE_GB);
  const cap = armed(pending.MAX_LIBRARY_GB);

  // `then` is what happens once it fires — kept as its own sentence so the
  // trigger figure always leads and the consequence never runs on from it.
  // Both triggers are quoted in GB of disk, but they are not the same test: the
  // engine fires Headroom when USED space reaches total − target, and Redline
  // when FREE space falls to the floor. On any filesystem that holds blocks back
  // — ext4 reserves 5% for root by default — free is not total − used, so
  // measuring Headroom on free space painted the row red for a breach that would
  // never fire, while the percentage on the row's own next line, measured on the
  // used axis, quietly disagreed with it.
  const spaceRow = (id, name, trigger, then, { onUsed = false } = {}) => {
    if (!(trigger > 0)) return { id, name, off: true };
    if (!diskOk) return { id, name, unknown: 'Disk space is not readable yet.' };
    // The trigger point on the used axis; at it, the bar reads exactly full.
    const usedTrigger = total - trigger;
    const usedAtTrigger = Math.max(usedTrigger, 1);
    const over = onUsed ? used >= usedTrigger : free <= trigger;
    const toGo = onUsed ? usedTrigger - used : free - trigger;
    // Measured figures carry one decimal, the same as the panel's headline and
    // the Dashboard's Storage card; a configured threshold is quoted as
    // entered. Rounding the measurements here would have the same free space
    // read two different ways in one panel.
    return {
      id, name, trigger, over,
      state: over ? 'Triggered' : prMeasuredGb(toGo) + ' GB to go',
      pct: Math.max(0, Math.min(100, (used / usedAtTrigger) * 100)),
      note: (over
        ? (onUsed
            ? `Used space is <b>${prMeasuredGb(used)} GB</b>, at or above the `
              + `<b>${prMeasuredGb(usedTrigger)} GB</b> that leaves `
              + `${prCommaNum(trigger, 0)} GB spare.`
            : `Free space is <b>${prMeasuredGb(free)} GB</b>, at or below the `
              + `<b>${prCommaNum(trigger, 0)} GB</b> mark.`)
        : (onUsed
            ? `Fires when used space reaches <b>${prMeasuredGb(usedTrigger)} GB</b> — `
              + `<b>${prMeasuredGb(used)} GB</b> used now of `
              + `${prMeasuredGb(total)} GB.`
            : `Fires when free space falls to <b>${prCommaNum(trigger, 0)} GB</b> — `
              + `<b>${prMeasuredGb(free)} GB</b> free now of `
              + `${prMeasuredGb(total)} GB.`)) + ' ' + then,
    };
  };

  const rows = [
    spaceRow('ts-headroom', 'Headroom target', headroom,
             'The daily cleanup then frees space back above it.', { onUsed: true }),
    spaceRow('ts-redline', 'Redline floor', redline,
             'A cleanup then deletes immediately, without waiting for the daily run.'),
  ];
  if (!(cap > 0)) {
    rows.push({ id: 'ts-cap', name: 'Library Size Cap', off: true });
  } else if (!libOk) {
    rows.push({ id: 'ts-cap', name: 'Library Size Cap',
                unknown: _configSummaryActive
                  ? 'Measuring the library now\u2026'
                  : 'Library size not measured yet — refresh it with \u21bb on the '
                    + 'Dashboard\u2019s Storage card.' });
  } else {
    const over = lib > cap;
    rows.push({
      id: 'ts-cap', name: 'Library Size Cap', trigger: cap, over,
      state: over ? 'Triggered' : prMeasuredGb(cap - lib) + ' GB to go',
      pct: Math.max(0, Math.min(100, (lib / cap) * 100)),
      note: over
        ? `The library is <b>${prMeasuredGb(lib)} GB</b>, past the `
          + `<b>${prCommaNum(cap, 0)} GB</b> cap — the next cleanup trims back under it.`
        : `Trims once the library grows past <b>${prCommaNum(cap, 0)} GB</b>. `
          + `<b>${prMeasuredGb(lib)} GB</b> now.`,
    });
  }
  return rows;
}

// The disk figures at the top of the panel. These are deliberately independent
// of whether any threshold is armed: they answer "how much space is there, and
// how much of it is the library", which is the question the thresholds below
// are set against — and on a fresh install they are what shows a monitored
// directory took effect. Same source as the Dashboard's Storage card: the
// /api/status poll, applied in _applyStatusPayload.
function _updateThresholdFigures() {
  // Nothing monitored: dash the figures rather than leaving the last reading
  // up. Neither _diskStats nor _lastKnownLibraryGb is cleared when the paths go
  // away (both ignore the empty payloads a poll then returns), so the check has
  // to happen here.
  const disk = _configMonitoringActive ? (_diskStats || null) : null;
  const total = Number(disk?.total_gb);
  const usable = Number.isFinite(total) && total > 0;
  // One decimal, matching the Dashboard's Storage card exactly — the same
  // measurement must not read differently depending on which tab you are on.
  const set = (id, text) => { const e = document.getElementById(id); if (e) e.textContent = text; };
  set('ts-free-value', usable && Number.isFinite(Number(disk.free_gb))
    ? prMeasuredGb(disk.free_gb) : '—');
  const sub = document.getElementById('ts-disk-sub');
  if (sub) {
    sub.innerHTML = usable && Number.isFinite(Number(disk.used_gb))
      ? `${prMeasuredGb(disk.used_gb)} / ${prMeasuredGb(total)} GB&nbsp;(${disk.pct_used}% used)`
      : '— / — GB&nbsp;(—% used)';
  }
  const lib = _configMonitoringActive ? Number(_lastKnownLibraryGb) : NaN;
  set('ts-library-value', Number.isFinite(lib) && lib > 0 ? prMeasuredGb(lib) : '—');
}

function _updateThresholdStatus() {
  const panel = document.getElementById('threshold-status');
  if (!panel) return;
  _updateThresholdFigures();

  // The bar draws the PENDING thresholds, not the saved ones — the same values
  // _thresholdRows quotes, and the same object the save would post. (The
  // Dashboard's copy of this bar draws the saved ones; that difference is the
  // only reason prRenderDiskBar takes them as arguments.)
  const pending = getFormConfig();
  const armed = v => (Number.isFinite(Number(v)) && Number(v) > 0) ? Number(v) : null;
  prRenderDiskBar(document.getElementById('ts-bar-root'), {
    disk: _diskStats,
    libraryGb: _lastKnownLibraryGb,
    headroomGb: armed(pending.HEADROOM_GB),
    redlineGb: armed(pending.REDLINE_GB),
    capGb: armed(pending.MAX_LIBRARY_GB),
    monitoring: _configMonitoringActive,
  });

  let anyArmed = false;
  for (const row of _thresholdRows(pending)) {
    const el = document.getElementById(row.id);
    if (!el) continue;
    const state = el.querySelector('.ts-state');
    const note = el.querySelector('.ts-note');
    if (row.off || row.unknown) {
      el.classList.add('is-off');
      el.classList.remove('is-near', 'is-over');
      if (state) state.textContent = row.off ? 'Off' : '';
      if (note) note.textContent = row.off ? 'Not armed — this trigger is switched off.' : row.unknown;
      continue;
    }
    anyArmed = true;
    // "Near" once it is within a tenth of firing; the words say so too, so the
    // color is never the only signal.
    const near = !row.over && row.pct >= 90;
    el.classList.remove('is-off');
    el.classList.toggle('is-near', near);
    el.classList.toggle('is-over', !!row.over);
    if (state) state.textContent = row.state;
    if (note) note.innerHTML = row.note;
  }
  panel.hidden = !anyArmed && !_spaceThresholdsHaveUsableLibrary();
}

function _updatePills() {
  const h = document.getElementById('HEADROOM_GB')?.value;
  const l = document.getElementById('MAX_LIBRARY_GB')?.value?.trim() ?? '';
  const capEnabled = !!document.getElementById('cap-enabled')?.checked;
  const capConfigured = _libraryCapConfigured();
  const capHasInput = l !== '';
  const r = document.getElementById('REDLINE_GB')?.value?.trim() ?? '';
  const redlineEnabled = !!document.getElementById('redline-enabled')?.checked;
  const redlineValid = prPositiveNumber(prFieldRaw('REDLINE_GB'));
  const redlineHasInput = r !== '';
  // Every threshold appears even when unarmed — "Off" reads as a real state, so
  // a glance at the Automatic Cleanup card shows the whole picture. The label
  // above each value names it, so the value itself is just the number.
  const hTxt = (_headroomEnabled() && _headroomActive()) ? prCommaNum(h, 0) + ' GB' : 'Off';
  const rTxt = !redlineEnabled ? 'Off'
    : (redlineValid && r ? prCommaNum(r, 0) + ' GB' : '—');
  const lTxt = !capEnabled ? 'Off'
    : (capConfigured && l ? prCommaNum(l, 0) + ' GB' : '—');
  const spec = (id, text, off, invalid) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('is-off', off);
    el.classList.toggle('is-invalid', !!invalid);
  };
  spec('spec-headroom', hTxt, hTxt === 'Off', false);
  spec('spec-redline', rTxt, rTxt === 'Off',
       redlineEnabled && redlineHasInput && !redlineValid);
  spec('spec-cap', lTxt, lTxt === 'Off',
       capEnabled && capHasInput && !capConfigured);
  _updateThresholdStatus();
  _updateRunModeAvailability();
  _scheduleSectionIssueUpdate();
}
document.getElementById('HEADROOM_GB')?.addEventListener('input', () => {
  validateHeadroom({ show: true }); validateRedline({ show: true });
  // Crossing 0 ↔ >= 1 flips the cap/delay availability and the headroom-off
  // notes live, not just on the next toggle or save.
  _applySpaceThresholdLock();
  _updatePills();
});
document.getElementById('REDLINE_GB')?.addEventListener('input', () => { validateRedline({ show: true }); _updatePills(); });
document.getElementById('MAX_HEADROOM_PCT')?.addEventListener('input', () => { validateMaxHeadroomPct({ show: true }); _updatePills(); });

function _applyConnectivityGating(radarrConnected) {
  // Optional Radarr cleanup is only enterable once the Radarr API connects;
  // until then the box stays visible but ghosted.
  const secEnabled = document.getElementById('section-enabled');
  if (secEnabled) secEnabled.disabled = !radarrConnected;
  _setConnectionGhost('optional-cleanup-box', !!radarrConnected);
  // Radarr cleanup works against the PLEX library Radarr manages. With Plex
  // deselected, the Radarr connection fields are ghosted too — so the plain
  // "Connect Radarr to unlock this" was a dead-end instruction for
  // Jellyfin-only setups. Say what is actually required.
  const note = document.getElementById('section-requires-radarr');
  if (note) {
    const usePlex = !!document.getElementById('USE_PLEX')?.checked;
    note.textContent = usePlex
      ? 'Connect Radarr to unlock this.'
      : 'Radarr cleanup works with the Plex library — enable Plex under Server software, then connect Radarr to unlock this.';
  }
}

function _clearConnectionHighlights() {
  document.querySelectorAll('.connection-highlight').forEach(el => el.classList.remove('connection-highlight'));
  document.querySelectorAll('.connection-disabled').forEach(el => el.classList.remove('connection-disabled'));
  document.querySelectorAll('.mount-card-problem').forEach(el => el.classList.remove('mount-card-problem'));
}

var _configInvalidLocked = false;
var _invalidBannerScrolled = false;
function _applyInvalidConfigLock(issues) {
  const locked = Array.isArray(issues) && issues.length > 0;
  _configInvalidLocked = locked;
  const banner = document.getElementById('invalid-config-banner');
  if (banner) {
    banner.hidden = !locked;
    if (locked && !_invalidBannerScrolled) {
      // Once per page load: the onboarding auto-scroll can land past the
      // banner, and the banner holds the only working controls.
      _invalidBannerScrolled = true;
      prScrollTo(banner, { delayMs: 80, smooth: false });
    }
    const ul = document.getElementById('invalid-config-list');
    if (ul && locked) {
      ul.replaceChildren(...issues.map(i => {
        const li = document.createElement('li');
        const code = document.createElement('code');
        code.textContent = i.key;
        li.append(code, ' ' + i.message + '.');
        return li;
      }));
    }
  }
  document.getElementById('cfg-accordion')?.classList.toggle('config-invalid-lock', locked);
  if (locked) {
    ['btn-save', 'btn-revert'].forEach(id => {
      const b = document.getElementById(id);
      if (b) b.disabled = true;
    });
  }
}
function _bannerStatus(msg, danger) {
  const st = document.getElementById('invalid-config-status');
  if (st) { st.textContent = msg || ''; st.style.color = danger ? 'var(--text-danger)' : ''; }
}
async function _bannerPost(url, errField) {
  try {
    const d = await fetch(url, { method: 'POST' }).then(r => r.json());
    if (d && d.ok) return true;
    _bannerStatus((d && (d[errField] || d.error || d.message)) || 'Reset failed.', true);
  } catch (e) {
    _bannerStatus('Reset failed.', true);
  }
  return false;
}
async function onResetInvalidClick() {
  const btn = document.getElementById('btn-reset-invalid');
  if (btn) btn.disabled = true;
  _bannerStatus('Resetting invalid values…');
  if (await _bannerPost('/api/config/reset-invalid', 'error')) {
    _bannerStatus('Done — reloading…');
    window.location.reload();
    return;
  }
  if (btn) btn.disabled = false;
}
async function onBannerFullResetClick() {
  if (!window.confirm('Reset MediaReducer to first-time setup? Settings, appearance, cache, and the library snapshot are wiped. Logs are kept.')) return;
  if (await _bannerPost('/api/config/reset', 'message')) window.location.href = '/';
}
function _applyConfigHealth(d) {
  // A transient result (a fetch that never reached the server) paints the box
  // once but is NOT remembered — remembering it would turn a single network
  // blip into a persistent red error state re-applied on every keystroke.
  if (d && typeof d === 'object' && !d.transient) {
    _lastConfigHealth = d;
  }
  _applyInvalidConfigLock((d && d.invalid_config) || []);
  const box = document.getElementById('config-health-box');
  const summary = document.getElementById('config-health-summary');
  const list = document.getElementById('config-health-list');
  if (!box || !summary || !list) return;

  const cleanupEnabled = _optionalCleanupEnabled();
  // A transient (network-failure) result carries no *_connected flags — fall
  // back to the last real probe so one blip doesn't ghost the Radarr box.
  const radarrReady = _savedConnectionReady(['RADARR_URL', 'RADARR_API_KEY'],
    d.radarr_connected ?? (_lastConfigHealth && _lastConfigHealth.radarr_connected));
  _clearConnectionHighlights();
  _applyConnectivityGating(radarrReady);
  if (d.radarr_cleanup_forced_disabled) {
    const sectionToggle = document.getElementById('section-enabled');
    if (sectionToggle) sectionToggle.checked = false;
  }
  const displayErrors = [...(d.errors || [])];
  let displayWarnings = [...(d.warnings || [])];
  const displaySeverity = displayErrors.length ? 'error' : (displayWarnings.length ? 'warning' : 'ok');

  box.classList.remove('status-ok', 'status-warning', 'status-error');
  box.classList.add(`status-${displaySeverity}`);
  // First contact is SETUP, not breakage: an untouched install has done
  // nothing wrong yet, so don't greet it with "Fix the errors below."
  const _onboarding = _connectionOnboardingActive
    && displayErrors.length > 0
    && !(document.getElementById('USE_PLEX')?.checked || document.getElementById('USE_JELLYFIN')?.checked);
  summary.textContent = displayErrors.length
    ? (_onboarding ? 'Setup needed — pick your server software below.' : 'Fix the errors below.')
    : (displayWarnings.length ? 'Ready with warnings.' : (cleanupEnabled ? 'Ready.' : 'Ready. Optional Radarr cleanup is disabled.'));

  const items = [...displayErrors, ...displayWarnings];
  list.innerHTML = '';
  if (items.length) {
    items.forEach(msg => {
      const li = document.createElement('li');
      li.textContent = msg;
      list.appendChild(li);
    });
    list.classList.remove('d-none');
  } else {
    list.classList.add('d-none');
  }

  (d.highlights || []).forEach(name => {
    // Dependent feature fields such as Protected collections should lock when
    // their saved API connection is unavailable, but the red warning state
    // belongs on the connection URL/token fields only.
    if (name === 'PROTECTED_COLLECTIONS') return;
    if (name === 'SERVER_SOFTWARE') {
      // Pseudo-field: no media server is enabled — flag the checkbox row.
      document.getElementById('server-software-row')?.classList.add('connection-highlight');
      return;
    }
    document.getElementById(`override-field-${name}`)?.classList.add('connection-highlight');
  });
  (d.mount_highlights || []).forEach(name => {
    if (!cleanupEnabled && OPTIONAL_CLEANUP_MOUNTS.has(name)) return;
    document.getElementById(`mount-card-${name}`)?.classList.add('mount-card-problem');
  });

  const overrideHighlightNames = new Set(['TAUTULLI_URL', 'TAUTULLI_API_KEY', 'PLEX_URL', 'PLEX_TOKEN', 'JELLYFIN_URL', 'JELLYFIN_API_KEY', 'RADARR_URL', 'RADARR_API_KEY']);
  if ([...(d.highlights || [])].some(name => overrideHighlightNames.has(name))) {
    const details = document.getElementById('manual-overrides');
    if (details) details.open = true;
  }

  _requiredConnectionOk = !!d.critical_ok;
  _requiredConnectionMessage = d.required_tooltip || 'Connect the selected media server first.';

  // Do not auto-disable Optional cleanup just because one optional app is not
  // mounted. The health box explains which optional cleanup pieces will be
  // skipped, while local MediaReducer cleanup remains allowed.

  _applyConfigRuntimeLocks();
  _updateRunModeAvailability();
}

async function checkConfigErrors() {
  const btn = document.getElementById('btn-config-check');
  const lockReason = _sectionLockReason('connections');
  if (lockReason) {
    _applyConfigRuntimeLocks();
    showToast(lockReason, 'warning');
    return;
  }
  if (btn) { btn.disabled = true; btn.classList.add('btn-busy'); btn.textContent = 'Checking…'; }
  try {
    const r = await fetch('/api/config/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(getFormConfig()),
      cache: 'no-store',
    });
    const d = await r.json();
    if (_configsMatch(getFormConfig(), _savedConfig)) {
      _savedConfigHealth = d;
      syncSavedVisibilitySections();
    }
    _applyConfigHealth(d);
    showToast(d.ok ? 'Connection check passed.' : 'Connection check found issues.', d.ok ? 'success' : (d.severity === 'warning' ? 'warning' : 'danger'));
  } catch (e) {
    _applyConfigHealth({ transient: true, severity: 'error', summary: 'Could not run connection check.', errors: ['Could not reach MediaReducer to run the check — try again. (' + String(e && e.message || e) + ')'], warnings: [], highlights: [], mount_highlights: [], invalid_config: (_lastConfigHealth && _lastConfigHealth.invalid_config) || [] });
  } finally {
    if (btn) { btn.disabled = !!_sectionLockReason('connections'); btn.classList.remove('btn-busy'); btn.textContent = 'Check for Errors'; }
  }
}

function _reapplyConnectionHealthHighlights() {
  // Browser tab suspension / bfcache restores can leave Bootstrap inputs repainted
  // without the connection-highlight classes visibly applied. Re-apply the last
  // known health result so PLEX_URL / PLEX_TOKEN and other problem fields stay
  // marked until the user runs a new check or fixes the config.
  if (!_lastConfigHealth) return;
  window.requestAnimationFrame(() => _applyConfigHealth(_lastConfigHealth));
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') _reapplyConnectionHealthHighlights();
});
window.addEventListener('pageshow', _reapplyConnectionHealthHighlights);

// Verify Docker volume mounts on page load and show status in Connections section
let _lastMountData = null;
function renderMountBadges() {
  // _lastMountData === null means the verify request itself failed — a
  // network blip, not a missing mount. Say "could not check" instead of the
  // red "not found — check .env", which sent users hunting a phantom .env
  // problem.
  const unchecked = _lastMountData === null;
  const d = _lastMountData || {};
  // What the mount CONTRIBUTES, not whether a file happens to be there. These
  // mounts exist only so Auto Detect can read an API key out of a service's own
  // config; nothing else reads them, so a missing one is never an error. And
  // the status is not gated on the Plex/Jellyfin checkboxes: reading a file off
  // disk doesn't depend on which server is selected, and "(Not enabled)" told a
  // user mid-setup nothing about whether their key was there to be found.
  const _show = (id, state) => {
    const el = document.getElementById('verify-' + id);
    if (!el) return;
    const set = (text, color) => { el.textContent = text; el.style.color = color; };
    if (unchecked) return set('? could not check — reload the page', 'var(--text-muted)');
    const mounted = !!state?.mounted;
    const keys = state?.keys || [];
    if (keys.length) return set(`✓ ${keys.join(' + ')} API key${keys.length > 1 ? 's' : ''} found`,
                                'var(--text-accent)');
    if (mounted) return set('✗ mounted, but no API key inside', 'var(--text-danger)');
    set('○ not mounted — enter the API key manually', 'var(--text-muted)');
  };
  _show('tautulli', d.tautulli);
  _show('radarr',   d.radarr);
  _show('sonarr',   d.sonarr);
}
(async function _verifyMounts() {
  try {
    _lastMountData = await fetch('/api/connections/verify').then(r => r.json());
  } catch (e) { _lastMountData = null; }
  renderMountBadges();
})();

// ── Protected collection / box-set pickers ────────────────────────────────────
// Selections are held in a model (not the DOM) so a failed scan never wipes the
// user's saved choices: the list is always the union of scanned names and the
// currently-selected names, with the selected-but-not-scanned ones flagged.
// (_collectionSelections / _collectionOptions are declared near the top.)
function renderCollectionList(server) {
  const listEl = document.getElementById(server + '-collections-list');
  if (!listEl) return;
  const sel = _collectionSelections[server];
  const scanned = _collectionOptions[server] || [];
  const scannedSet = new Set(scanned);
  const all = Array.from(new Set([...scanned, ...sel]))
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  const label = 'collections';
  if (all.length === 0) {
    listEl.innerHTML = '<div class="form-text collections-empty">No ' + label + ' loaded yet — click Scan above.</div>';
    return;
  }
  listEl.innerHTML = '';
  all.forEach(name => {
    const row = document.createElement('label');
    row.className = 'collection-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'form-check-input';
    cb.checked = sel.has(name);
    cb.disabled = !!_sectionLockReason('paths');
    cb.addEventListener('change', () => { if (cb.checked) sel.add(name); else sel.delete(name); });
    const span = document.createElement('span');
    span.className = 'collection-name';
    span.textContent = name;
    row.appendChild(cb);
    row.appendChild(span);
    if (!scannedSet.has(name)) {
      const hint = document.createElement('span');
      hint.className = 'collection-missing';
      hint.textContent = 'not found on server';
      row.appendChild(hint);
    }
    listEl.appendChild(row);
  });
}

async function scanCollections(server) {
  const statusEl = document.getElementById(server + '-collections-status');
  const btn = document.getElementById('btn-scan-' + server);
  const lockReason = _sectionLockReason('paths');
  if (lockReason) {
    if (statusEl) { statusEl.textContent = lockReason; statusEl.style.color = 'var(--text-muted)'; }
    return;
  }
  if (btn) { btn.disabled = true; btn.classList.add('btn-busy'); }
  if (statusEl) { statusEl.textContent = 'Scanning…'; statusEl.style.color = 'var(--text-muted)'; }
  try {
    const d = await fetch('/api/collections', { method: 'POST' }).then(r => r.json());
    const info = (d && d[server]) || {};
    if (info.ok) {
      _collectionOptions[server] = info.names || [];
      // Saved protections the server no longer lists are KEPT and flagged
      // loudly — never silently dropped (runs fail closed on them and say why).
      const missing = info.missing || [];
      if (missing.length) {
        const serverName = server === 'plex' ? 'Plex' : 'Jellyfin';
        showToast(`Protected collection${missing.length === 1 ? '' : 's'} ${missing.map(n => `'${n}'`).join(', ')} no longer exist${missing.length === 1 ? 's' : ''} on ${serverName} — runs are blocked until fixed there or unchecked here.`, 'warning');
      }
      const n = (info.names || []).length;
      if (statusEl) {
        const suffix = missing.length ? `; ${missing.length} saved selection${missing.length === 1 ? '' : 's'} missing on the server` : '';
        statusEl.textContent = n + (n === 1 ? ' found' : ' found') + suffix;
        statusEl.style.color = missing.length ? 'var(--text-warning)' : 'var(--text-muted)';
      }
    } else if (statusEl) {
      statusEl.textContent = info.error || 'Could not scan — check the connection above.';
      statusEl.style.color = 'var(--text-danger)';
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = 'Scan failed.'; statusEl.style.color = 'var(--text-danger)'; }
  } finally {
    if (btn) { btn.disabled = !!_sectionLockReason('paths'); btn.classList.remove('btn-busy'); }
    renderCollectionList(server);
  }
}

function autoScanConnectedCollections() {
  if (_savedPlexApiConnected()) scanCollections('plex');
  if (_savedJellyfinApiConnected()) scanCollections('jellyfin');
}

function syncDebugModeVisibility() {
  const enabled = !!_savedConfig?.DEBUG_MODE;
  document.querySelectorAll('.debug-mode-control').forEach(el => {
    el.classList.toggle('d-none', !enabled);
  });
}

async function createDebugReport() {
  if (_configActivityLocked()) {
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  const btn = document.getElementById('btn-create-report');
  const status = document.getElementById('debug-report-status');
  if (btn) { btn.disabled = true; btn.classList.add('btn-busy'); btn.textContent = 'Building…'; }
  if (status) { status.textContent = 'Collecting config, connection, and API data…'; status.style.color = 'var(--text-muted)'; }
  try {
    const r = await fetch('/api/debug/report', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'Report failed.');
    const name = d.filename || ('debug_report_' + prFileStamp() + '.txt');
    if (!prDownloadText(name, d.text || '')) throw new Error('Report was empty.');
    if (status) { status.textContent = 'Downloaded ' + name; status.style.color = 'var(--text-accent)'; }
    showToast('Diagnostic report downloaded', 'success');
  } catch (e) {
    if (status) { status.textContent = 'Report failed: ' + (e.message || e); status.style.color = 'var(--text-danger)'; }
    showToast('Report failed', 'danger');
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('btn-busy'); btn.textContent = 'Download report'; }
  }
}

function _plexDebugItemText(item) {
  const lines = [];
  lines.push(`      - ${item.title || '(no title)'} | type=${item.type || 'n/a'} | year=${item.year || 'n/a'} | ratingKey=${item.rating_key || 'n/a'} | key=${item.key || 'n/a'}`);
  if (item.guid) lines.push(`        guid: ${item.guid}`);
  if ((item.guids || []).length) lines.push(`        Guid[]: ${item.guids.join(', ')}`);
  const files = item.files || [];
  if (!files.length) {
    lines.push('        Files: (none returned)');
  } else {
    files.forEach((part, idx) => {
      lines.push(`        Part ${idx + 1}: id=${part.id || 'n/a'} key=${part.key || 'n/a'} size=${part.size || 'n/a'} file=${part.file || 'n/a'}`);
    });
  }
  return lines.join('\n');
}

function renderPlexDebug(data) {
  const lines = [];
  lines.push('Plex protected-collection debug');
  lines.push('Selected: ' + ((data.selected || []).join(', ') || '(none)'));
  lines.push('Section source: ' + (data.section_source || 'n/a'));
  lines.push('Sections found: ' + ((data.sections || []).map(s => `${s.title || '(no title)'} [key=${s.key}, type=${s.type || 'n/a'}]`).join(', ') || '(none)'));
  lines.push('');
  lines.push('Collection listing attempts:');
  (data.section_attempts || []).forEach(attempt => {
    lines.push(`  ${attempt.endpoint} | section=${attempt.section_title || '(no title)'} [${attempt.section_key}]`);
    lines.push(`    count=${attempt.count}${attempt.truncated ? ' (first 100 shown)' : ''}${attempt.error ? ' | ERROR: ' + attempt.error : ''}`);
    if ((attempt.names || []).length) lines.push('    names=' + attempt.names.join(', '));
  });
  lines.push('');
  lines.push('Matched selected collection(s): ' + (data.matched_count || 0));
  if (!(data.collections || []).length) {
    lines.push('  No selected collection names matched the collections Plex listed.');
  }
  (data.collections || []).forEach(coll => {
    lines.push('');
    lines.push(`Collection: ${coll.title || '(no title)'} | section=${coll.section_title || 'n/a'} [${coll.section_key || 'n/a'}] | ratingKey=${coll.rating_key || 'n/a'} | key=${coll.key || 'n/a'} | collection_key=${coll.collection_key || 'n/a'} | type=${coll.type || 'n/a'}`);
    (coll.child_attempts || []).forEach(attempt => {
      lines.push(`  Endpoint: ${attempt.endpoint}`);
      lines.push(`    count=${attempt.count}${attempt.truncated ? ' (first 100 shown)' : ''}${attempt.error ? ' | ERROR: ' + attempt.error : ''}`);
      if (!(attempt.items || []).length) {
        lines.push('      (no items returned)');
      } else {
        (attempt.items || []).forEach(item => lines.push(_plexDebugItemText(item)));
      }
    });
  });
  prShowDebugBox('Plex protected collections', lines.join('\n'));
}

async function debugPlexCollections() {
  const btn = document.getElementById('btn-debug-plex');
  const statusEl = document.getElementById('plex-collections-status');
  const names = Array.from(_collectionSelections.plex || []);
  if (!names.length) {
    showToast('Tick at least one Plex collection first.', 'warning');
    return;
  }
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.textContent = 'Debugging selected collection...'; statusEl.style.color = 'var(--text-muted)'; }
  const startedAt = performance.now();
  prShowDebugBox('Plex protected collections', 'Asking Plex what is inside: ' + names.join(', ') + '…',
                  { pending: true });
  try {
    const r = await fetch('/api/collections/plex/debug', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'Debug request failed.');
    await prDebugMinVisible(startedAt);
    renderPlexDebug(d);
    if (statusEl) { statusEl.textContent = 'Debug loaded'; statusEl.style.color = 'var(--text-muted)'; }
  } catch (e) {
    await prDebugMinVisible(startedAt);
    prShowDebugBox('Plex protected collections', 'Plex debug failed: ' + (e.message || e));
    if (statusEl) { statusEl.textContent = 'Debug failed.'; statusEl.style.color = 'var(--text-danger)'; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _jellyfinDebugItemText(item) {
  const lines = [];
  const ids = [];
  if (item.id) ids.push('Id=' + item.id);
  if (item.item_id) ids.push('ItemId=' + item.item_id);
  if (item.movie_id) ids.push('MovieId=' + item.movie_id);
  lines.push(`      - ${item.name || '(no name)'} | type=${item.type || 'n/a'} | ${ids.join(' | ') || 'no ids'}`);
  if (item.path) lines.push(`        Path: ${item.path}`);
  if (item.parent_id) lines.push(`        ParentId: ${item.parent_id}`);
  if (item.collection_type) lines.push(`        CollectionType: ${item.collection_type}`);
  const providerIds = item.provider_ids || {};
  if (Object.keys(providerIds).length) lines.push(`        ProviderIds: ${JSON.stringify(providerIds)}`);
  const mediaSources = item.media_sources || [];
  mediaSources.forEach((ms, idx) => {
    lines.push(`        MediaSource ${idx + 1}: id=${ms.id || 'n/a'} size=${ms.size || 'n/a'} path=${ms.path || 'n/a'}`);
  });
  return lines.join('\n');
}

function renderJellyfinDebug(data) {
  const lines = [];
  lines.push('Jellyfin protected-collection debug');
  lines.push('Selected: ' + ((data.selected || []).join(', ') || '(none)'));
  lines.push('Users found: ' + ((data.users || []).map(u => `${u.name || '(no name)'} [${u.id}]`).join(', ') || '(none)'));
  if (data.users_error) lines.push('Users error: ' + data.users_error);
  lines.push('Using user id: ' + (data.using_user_id || '(none)'));
  lines.push('');
  lines.push('BoxSet listing attempts:');
  (data.boxset_attempts || []).forEach(attempt => {
    lines.push(`  ${attempt.endpoint} ${JSON.stringify(attempt.params || {})}`);
    lines.push(`    count=${attempt.count}${attempt.truncated ? ' (first 100 shown)' : ''}${attempt.error ? ' | ERROR: ' + attempt.error : ''}`);
    if ((attempt.names || []).length) lines.push('    names=' + attempt.names.join(', '));
  });
  lines.push('');
  lines.push('Matched selected collection(s): ' + (data.matched_count || 0));
  if (!(data.collections || []).length) {
    lines.push('  No selected collection names matched the BoxSets Jellyfin listed.');
  }
  (data.collections || []).forEach(coll => {
    lines.push('');
    lines.push(`Collection: ${coll.name || '(no name)'} | id=${coll.id || 'n/a'} | type=${coll.type || 'n/a'}`);
    (coll.child_attempts || []).forEach(attempt => {
      const tag = (attempt.engine === undefined) ? '' : (attempt.engine ? ' [engine]' : ' [debug-only]');
      lines.push(`  Endpoint: ${attempt.endpoint} ${JSON.stringify(attempt.params || {})}${tag}`);
      lines.push(`    count=${attempt.count}${attempt.truncated ? ' (first 100 shown)' : ''}${attempt.error ? ' | ERROR: ' + attempt.error : ''}`);
      if (!(attempt.items || []).length) {
        lines.push('      (no items returned)');
      } else {
        (attempt.items || []).forEach(item => lines.push(_jellyfinDebugItemText(item)));
      }
    });
  });
  prShowDebugBox('Jellyfin protected collections', lines.join('\n'));
}

async function debugJellyfinCollections() {
  const btn = document.getElementById('btn-debug-jellyfin');
  const statusEl = document.getElementById('jellyfin-collections-status');
  const names = Array.from(_collectionSelections.jellyfin || []);
  if (!names.length) {
    showToast('Tick at least one Jellyfin collection first.', 'warning');
    return;
  }
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.textContent = 'Debugging selected collection...'; statusEl.style.color = 'var(--text-muted)'; }
  const startedAt = performance.now();
  prShowDebugBox('Jellyfin protected collections', 'Asking Jellyfin what is inside: ' + names.join(', ') + '…',
                  { pending: true });
  try {
    const r = await fetch('/api/collections/jellyfin/debug', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'Debug request failed.');
    await prDebugMinVisible(startedAt);
    renderJellyfinDebug(d);
    if (statusEl) { statusEl.textContent = 'Debug loaded'; statusEl.style.color = 'var(--text-muted)'; }
  } catch (e) {
    await prDebugMinVisible(startedAt);
    prShowDebugBox('Jellyfin protected collections', 'Jellyfin debug failed: ' + (e.message || e));
    if (statusEl) { statusEl.textContent = 'Debug failed.'; statusEl.style.color = 'var(--text-danger)'; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initCollections(cfg) {
  _collectionSelections.plex     = new Set(cfg.PROTECTED_COLLECTIONS || []);
  _collectionSelections.jellyfin = new Set(cfg.JELLYFIN_PROTECTED_COLLECTIONS || []);
  renderCollectionList('plex');
  renderCollectionList('jellyfin');
}

// ── Reset to first-time setup (two-click confirm, then redirect) ──────────────
let _resetArmed = false;
function _disarmReset() {
  _resetArmed = false;
  const btn = document.getElementById('btn-reset-all');
  if (btn) btn.classList.remove('btn-busy');
  const status = document.getElementById('reset-status');
  if (btn) {
    _morphButtonText(btn, 'Reset to first-time setup');
  }
  if (status) { status.textContent = ''; status.style.color = 'var(--text-muted)'; }
}
function _isResetConfirmTarget(target) {
  return !!target?.closest?.('#btn-reset-all');
}
document.addEventListener('click', (ev) => {
  if (!_resetArmed) return;
  if (_isResetConfirmTarget(ev.target)) return;
  _disarmReset();
  ev.preventDefault();
  ev.stopPropagation();
}, true);
// Only useful while a run is active — the rest of the time it says so rather
// than sitting there as a live-looking button with nothing to stop.
function _applyForceStopAvailability() {
  const btn = document.getElementById('btn-force-stop');
  if (!btn || btn.classList.contains('btn-busy')) return;
  const active = _configActivityLocked();
  btn.disabled = !active;
  btn.title = active ? 'Kill the running engine immediately'
                     : 'No run is active';
  if (!active) _setForceStopStatus('');
}

function _setForceStopStatus(text, tone) {
  const el = document.getElementById('force-stop-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = tone === 'bad' ? 'var(--text-danger)'
                 : tone === 'good' ? 'var(--text-success)' : '';
}

async function onForceStopClick() {
  if (!_configActivityLocked()) return;
  const answer = await prConfirm({
    title: 'Force stop the run?',
    body: [
      { text: 'This kills the engine immediately rather than asking it to stop.', warn: true },
      'The run\'s log is not archived, and a deletion caught mid-record can be '
      + 'missing from the history. Files already deleted stay deleted.',
    ],
    confirmText: 'Force stop',
  });
  if (answer !== 'confirm') return;
  const btn = document.getElementById('btn-force-stop');
  if (btn) { btn.disabled = true; btn.classList.add('btn-busy'); }
  _setForceStopStatus('Stopping…');
  try {
    const r = await fetch('/api/run/force-stop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, cache: 'no-store',
    });
    const d = await r.json().catch(() => ({}));
    // A kill that didn't take is the case worth reading carefully — leave it on
    // screen rather than flashing it past in a toast.
    _setForceStopStatus(d.message || (d.ok ? 'Stopped.' : 'Could not stop the run.'),
                        d.ok ? 'good' : 'bad');
    if (!d.ok) showToast(d.message || 'Could not stop the run.', 'warning');
  } catch (e) {
    _setForceStopStatus('Could not reach the server.', 'bad');
  } finally {
    if (btn) btn.classList.remove('btn-busy');
    _applyForceStopAvailability();
  }
}

async function onResetClick() {
  const btn = document.getElementById('btn-reset-all');
  const status = document.getElementById('reset-status');
  if (!_resetArmed) {
    _resetArmed = true;
    if (btn) {
      _morphButtonText(btn, 'Are you sure?', { noShrink: true });
    }
    if (status) {
      status.textContent = _configActivityLocked()
        ? 'This will cancel the active run and reset.'
        : 'Ready to reset.';
      status.style.color = 'var(--text-muted)';
    }
    return;
  }
  _resetArmed = false;
  if (btn) { btn.disabled = true; btn.classList.add('btn-busy'); _morphButtonText(btn, 'Resetting…'); }
  if (status) { status.textContent = ''; status.style.color = 'var(--text-muted)'; }
  try {
    const d = await fetch('/api/config/reset', { method: 'POST' }).then(r => r.json());
    if (d && d.ok) {
      if (status) { status.textContent = 'Done — returning to setup…'; }
      window.location.href = '/';
    } else {
      if (btn) { btn.disabled = false; }
      _disarmReset();
      if (status) { status.textContent = (d && d.message) || 'Reset failed.'; status.style.color = 'var(--text-danger)'; }
    }
  } catch (e) {
    if (btn) { btn.disabled = false; }
    _disarmReset();
    if (status) { status.textContent = 'Reset failed.'; status.style.color = 'var(--text-danger)'; }
  }
}
// ── Clear archived run logs ─────────────────────────────────────────────────
async function onClearLogsClick() {
  const btn = document.getElementById('btn-clear-logs');
  if (!btn || btn.disabled) return;
  if (_configActivityLocked()) {
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (!_filesystemReady()) {
    showToast(_filesystemBlockingText(), 'warning');
    return;
  }
  btn.disabled = true;
  btn.classList.add('btn-busy');
  btn.textContent = 'Clearing…';
  try {
    const r = await fetch(`/api/logs/archived/clear?${prTimeQuery()}`, { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error((d && d.message) || 'Clear failed.');
    showToast((d && d.message) || 'Logs cleared', 'success');
  } catch (e) {
    showToast('Clear logs failed: ' + e.message, 'danger');
  } finally {
    btn.disabled = false;
    btn.classList.remove('btn-busy');
    btn.textContent = 'Clear logs';
    await refreshArchivedLogsStatus();
  }
}
async function refreshArchivedLogsStatus() {
  const statusEl = document.getElementById('archived-logs-status');
  const btn = document.getElementById('btn-clear-logs');
  try {
    const r = await fetch(`/api/logs/archived/status?${prTimeQuery()}`, { cache: 'no-store' });
    const d = await r.json();
    if (statusEl) {
      statusEl.textContent = _configActivityLocked()
        ? 'A run is active. Archived logs cannot be cleared right now.'
        : (!_filesystemReady() ? _filesystemBlockingText() : (d.label || 'Archived log directory is empty.'));
    }
    if (btn) {
      btn.disabled = !!d.empty || _configActivityLocked() || !_filesystemReady();
    }
  } catch (e) {
    // A failed status fetch is not knowledge — don't state a confident wrong
    // fact ("empty") that also implies the Clear button has nothing to do.
    if (statusEl) statusEl.textContent = 'Could not check the archived logs — reload the page to retry.';
  }
}

function _setRadarrSectionDetectionIdle(message = 'The Plex section ID is detected automatically after Radarr and Plex connect.') {
  const el = document.getElementById('section-detected');
  if (!el) return;
  el.textContent = message;
  el.style.color = 'var(--text-muted)';
  delete el.dataset.sectionId;
  delete el.dataset.sectionName;
  delete el.dataset.sectionMethod;
  delete el.dataset.sectionMethodLabel;
}

function _radarrSectionMethodLabel(method, explicitLabel = '') {
  const label = String(explicitLabel || '').trim();
  if (label) return label;
  const key = String(method || '').trim();
  const labels = {
    'path-prefix': 'path prefix',
    'root-prefix': 'root folder path',
    'folder-name': 'library folder name',
  };
  return labels[key] || key.replace(/-/g, ' ');
}

function _applyRadarrSectionDetection(d) {
  const el = document.getElementById('section-detected');
  if (!el) return;
  if (d && d.ok && d.section_id) {
    const sectionId = String(d.section_id).trim();
    const sectionName = String(d.section_name || '').trim();
    const method = String(d.method || '').trim();
    const methodLabel = _radarrSectionMethodLabel(method, d.method_label);
    const nameText = sectionName ? ` (${sectionName})` : '';
    const methodText = methodLabel ? ` via ${methodLabel}` : '';
    el.textContent = `Detected section: ${sectionId}${nameText}${methodText}`;
    el.style.color = 'var(--text-accent)';
    el.dataset.sectionId = sectionId;
    if (sectionName) el.dataset.sectionName = sectionName; else delete el.dataset.sectionName;
    if (method) el.dataset.sectionMethod = method; else delete el.dataset.sectionMethod;
    if (methodLabel) el.dataset.sectionMethodLabel = methodLabel; else delete el.dataset.sectionMethodLabel;
    return;
  }
  delete el.dataset.sectionId;
  delete el.dataset.sectionName;
  delete el.dataset.sectionMethod;
  delete el.dataset.sectionMethodLabel;
  if (d) {
    el.textContent = `Detected section: unavailable — ${d.message || 'recheck after Radarr and Plex connect'}`;
    el.style.color = 'var(--text-muted)';
  } else {
    _setRadarrSectionDetectionIdle();
  }
}

function _syncSavedRadarrSectionDetection(cfg) {
  const cached = String(cfg?._RADARR_DETECTED_SECTION_ID || '').trim();
  if (cached) {
    _applyRadarrSectionDetection({
      ok: true,
      section_id: cached,
      section_name: cfg?._RADARR_DETECTED_SECTION_NAME,
      method: cfg?._RADARR_DETECTED_SECTION_METHOD,
      method_label: cfg?._RADARR_DETECTED_SECTION_METHOD_LABEL,
    });
  } else {
    _setRadarrSectionDetectionIdle();
  }
}

function _radarrDetectedSectionValue() {
  return document.getElementById('section-detected')?.dataset?.sectionId
    || _savedConfig?._RADARR_DETECTED_SECTION_ID
    || null;
}
function _radarrDetectedSectionName() {
  return document.getElementById('section-detected')?.dataset?.sectionName
    || _savedConfig?._RADARR_DETECTED_SECTION_NAME
    || null;
}
function _radarrDetectedSectionMethod() {
  return document.getElementById('section-detected')?.dataset?.sectionMethod
    || _savedConfig?._RADARR_DETECTED_SECTION_METHOD
    || null;
}
function _radarrDetectedSectionMethodLabel() {
  return document.getElementById('section-detected')?.dataset?.sectionMethodLabel
    || _savedConfig?._RADARR_DETECTED_SECTION_METHOD_LABEL
    || null;
}

document.getElementById('MAX_LIBRARY_GB')?.addEventListener('input', () => { validateLibraryCap({ show: true }); _updatePills(); });
// Typing in the delay field LOCKS the reset button live (a reset applies the
// SAVED delay, so an unsaved edit must settle first). Deliberately narrow:
// _applySpaceThresholdLock is not a pure recompute (it force-resets threshold
// fields in some states) and must not run per keystroke — unlocking happens
// through the regular lock recompute on Save/Revert (and the status poll).
document.getElementById('DELETE_DELAY_DAYS')?.addEventListener('input', () => {
  if (_delayValueEdited()) _setDisabled('btn-reset-delays', true);
});
['RADARR_URL', 'RADARR_API_KEY', 'PLEX_URL', 'PLEX_TOKEN', 'JELLYFIN_URL', 'JELLYFIN_API_KEY'].forEach(name => {
  document.getElementById(name)?.addEventListener('input', () => {
    if (_lastConfigHealth) _applyConfigHealth(_lastConfigHealth);
  });
});

// prWireBlurValidation comes from base.html (shared with Filtering & Scoring).
prWireBlurValidation('MAX_LIBRARY_GB', validateLibraryCap);
prWireBlurValidation('DELETE_DELAY_DAYS', validateDeleteDelay);
prWireBlurValidation('MAX_HEADROOM_PCT', validateMaxHeadroomPct);
prWireBlurValidation('REDLINE_GB', validateRedline);
prWireBlurValidation('HEADROOM_GB', validateHeadroom);

document.getElementById('cap-enabled')?.addEventListener('change', toggleLibraryCap);
document.getElementById('redline-enabled')?.addEventListener('change', toggleRedline);
document.getElementById('headroom-enabled')?.addEventListener('change', toggleHeadroom);
document.getElementById('section-enabled')?.addEventListener('change', toggleSectionId);

function _wireRunModeCardClicks() {
  document.querySelectorAll('.run-mode-card').forEach(card => {
    card.addEventListener('click', (event) => {
      // Let native radio clicks work normally, but make the rest of the card select it too.
      if (event.target?.matches?.('input[name="RUN_MODE"]')) return;
      // ...except the card's own settings. Ticking "Set to Monitor Only at
      // startup" must not also arm Automatic Cleanup — that is a deletion mode
      // turned on by a click aimed at something else entirely.
      if (event.target?.closest?.('.run-mode-option')) return;

      const radio = card.querySelector('input[name="RUN_MODE"]');
      if (!radio || radio.disabled || card.getAttribute('aria-disabled') === 'true') return;

      if (!radio.checked) {
        radio.checked = true;
        radio.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  });
}
_wireRunModeCardClicks();

_applySpaceThresholdLock();
syncSavedVisibilitySections();
// Initialize the protected-collection pickers from the saved config on load.
// (populateForm only runs on save/revert/reset, so without this the pickers
// start empty and a later save would wipe PROTECTED_COLLECTIONS.)
initCollections(_savedConfig);
function _captureMarkedClockedCount(d) {
  if (Number.isFinite(Number(d?.marked_clocked_count))) _markedClockedCount = Number(d.marked_clocked_count);
}
// The measured library size, which the Library Size Cap meter reads and the
// cap's safety floor scales with. A zero or missing figure is not a
// measurement, so it never overwrites a real one.
function trackLibrarySize(gb) {
  const measured = Number(gb);
  if (!(Number.isFinite(measured) && measured > 0) || measured === _lastKnownLibraryGb) return;
  _lastKnownLibraryGb = measured;
  if (typeof _updateRunModeAvailability === 'function') _updateRunModeAvailability();
  if (typeof _updateThresholdStatus === 'function') _updateThresholdStatus();
}

// One shared apply for every /api/status payload this page sees — the base
// layout's 4-second poll (via the prOnStatusPoll hook), the page-load seed,
// and the post-action burst poll all feed the same state + UI sequence.
// Scheduler Mode can change without this page: a tick that finds Space
// Thresholds unsafe pauses Automatic Cleanup by itself, and a second tab or the
// CLI can set it too. Left frozen at render time, the page posts whatever it was
// showing on the next unrelated save — silently re-arming automatic deletion the
// app had paused for safety — and the two-click arming confirm is skipped,
// because it compares against that same stale value. So follow the live mode,
// and move the radio with it unless this session has touched it: an unedited
// page must not carry a mode the server has left behind.
let _runModeTouched = false;
document.addEventListener('change', (e) => {
  if (e.target?.matches?.('input[name="RUN_MODE"]')) _runModeTouched = true;
}, true);

function _syncRunModeFromStatus(mode) {
  if (!mode || mode === _savedConfig.RUN_MODE) return;
  _savedConfig.RUN_MODE = mode;      // always: the arming confirm keys off this
  if (_runModeTouched) return;       // ...but never overrule a deliberate choice
  const el = document.querySelector(`input[name="RUN_MODE"][value="${mode}"]`);
  if (el) el.checked = true;
}

function _applyStatusPayload(d) {
  const wasLocked = _configActivityLocked();
  _syncRunModeFromStatus(d.run_mode);
  _configRunActive = !!d.run_active;
  _configSummaryActive = !!d.summary_active;
  _simulateRequired = !!(d.cleanup_state?.space_thresholds?.simulate_required);
  _simulateRequiredMsg = d.cleanup_state?.space_thresholds?.simulate_required_message || '';
  _captureMarkedClockedCount(d);
  // The threshold panel reads free/total off this, so keep it current rather
  // than frozen at whatever the page was rendered with.
  if (d.disk && Number.isFinite(Number(d.disk.total_gb))) _diskStats = d.disk;
  // Adding the first monitored directory is what turns the panel's figures on;
  // this poll is how the page finds out, so no reload is needed.
  if (d.thresholds_configured !== undefined) _configMonitoringActive = !!d.thresholds_configured;
  trackLibrarySize(d.library_gb);
  // Unconditionally, not just when the library size moved: free space is the
  // figure two of the three meters are measured against, and the scanning
  // wording on the third follows _configSummaryActive.
  _updateThresholdStatus();
  _applyConfigRuntimeLocks();
  _updateRunModeAvailability();
  _applyImdbDownloadButtonState();
  _applyClearCacheButtonState();
  // A summary/run just finished: it may have archived a log.
  if (wasLocked && !_configActivityLocked()) refreshArchivedLogsStatus();
}

// The base layout polls /api/status every 4s for the header badge and hands
// the payload to the page here — no separate interval needed.
window.prOnStatusPoll = _applyStatusPayload;

async function _fetchStatusOnce() {
  const r = await fetch('/api/status?_=' + Date.now(), { cache: 'no-store' });
  return r.ok ? await r.json() : null;
}

async function refreshLibrarySize() {
  try {
    const d = await _fetchStatusOnce();
    if (d) _applyStatusPayload(d);
  } catch (_) { /* the panel keeps saying "not measured yet" */ }
}

// After an action that kicks a background Summary (cache clear, IMDb refresh):
// poll at 1s until it goes idle, so the meters and locks track the refresh
// faster than the 4-second base poll. Awaitable — callers chain on completion.
let _librarySizePollActive = false;
async function pollLibrarySizeUntilIdle() {
  if (_librarySizePollActive) return;
  _librarySizePollActive = true;
  try {
    for (let tries = 0; tries < 60; tries++) {
      await new Promise(resolve => setTimeout(resolve, tries === 0 ? 500 : 1000));
      try {
        const d = await _fetchStatusOnce();
        if (!d) continue;
        _applyStatusPayload(d);
        if (!d.summary_active) break;
      } catch (_) { /* transient; keep polling for the active refresh */ }
    }
  } finally {
    _librarySizePollActive = false;
  }
}

// Live server clock under the Time zone dropdown, always in the zone the app
// is ACTUALLY running on — a dropdown selection only takes effect on Save
// (the save handler updates PR_SERVER_TIME_ZONE, so the clock jumps then).
// Also flags a wrong host clock: epoch comparison is timezone-independent,
// so a real skew means the server clock itself is off.
// ── Live 12h/24h preview ────────────────────────────────────────────────────
// Time format is pure display, so the page re-renders every visible clock the
// moment the dropdown changes — no save or reload needed. (The operating time
// ZONE is deliberately not previewed: it decides when runs actually fire, so
// what's shown stays the saved zone until the change is saved.)

function _relabelDailyRunTimeOptions() {
  const sel = document.getElementById('DAILY_RUN_TIME');
  if (!sel) return;
  // Values are always canonical HH:MM — only the labels change, so the
  // selection (including a saved off-grid time) survives untouched.
  // prTimeLabel reads the format set just above.
  [...sel.options].forEach(opt => { opt.textContent = prTimeLabel(opt.value); });
}

function _applyDisplayTimeFormat() {
  const fmt = document.getElementById('DISPLAY_TIME_FORMAT')?.value === '24h' ? '24h' : '12h';
  window.PR_DISPLAY_TIME_FORMAT = fmt;
  _relabelDailyRunTimeOptions();
  updateServerClockLine();
  // These lines are formatted server-side (they carry a time_format query), so
  // they need a re-fetch rather than a re-render.
  refreshImdbDownloadStatus();
  refreshCacheClearStatus();
  refreshArchivedLogsStatus();
}

function updateServerClockLine() {
  const el = document.getElementById('server-clock-line');
  if (!el) return;
  const zone = prResolvedTimeZone();
  let now;
  try {
    now = new Intl.DateTimeFormat(undefined, {
      timeZone: zone,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: 'numeric', minute: '2-digit', second: '2-digit',
      hour12: prTimeFormat() !== '24h',
    }).format(new Date(prServerEpochNow() * 1000));
  } catch (_) { el.textContent = ''; return; }
  const skewMin = Math.round(Math.abs(prClockSkewSeconds()) / 60);
  el.innerHTML = `Server time: ${prHtmlEsc(now)}` + (Math.abs(prClockSkewSeconds()) > 120
    ? ` <span class="text-danger">— ~${skewMin} min off from this device. Check the host clock.</span>`
    : '');
}
updateServerClockLine();
setInterval(updateServerClockLine, 1000);
document.getElementById('DISPLAY_TIME_FORMAT')?.addEventListener('change', _applyDisplayTimeFormat);

// Show the current monitored library size next to the Library Size Cap so the
// cap can be set relative to it (same source the Dashboard storage card uses).
// Ongoing updates ride the base layout's 4-second poll via prOnStatusPoll.
refreshLibrarySize();
// Pre-populate the lists by scanning any connected server on load; the Scan
// buttons then just refresh. Saved selections stay checked either way.
autoScanConnectedCollections();
_updatePills();
refreshImdbDownloadStatus();
refreshCacheClearStatus();
refreshArchivedLogsStatus();
_syncDirtyState();



// Settings this page can change that make the server rebuild the marked queue
// after the save — it unschedules the running clocks when the new limits are
// already met, then reconciles the plan from the snapshot. Mirrors
// _threshold_snapshot and _RECONCILE_*_KEYS in app.py, minus the scoring keys
// (those live on Filtering & Scoring and never reach this form). While one of
// these differs, the marked set standing BEFORE the save is not the one left
// after it, so nothing may quote a count as if it survives.
const _PLAN_REBUILD_KEYS = ['HEADROOM_GB', 'REDLINE_GB', 'REDLINE_ONLY_MODE',
                            'MAX_LIBRARY_GB', 'PROTECTED_COLLECTIONS',
                            'JELLYFIN_PROTECTED_COLLECTIONS'];
function _saveRebuildsPlan(cfg) {
  // Through _canonicalConfig, so this compares the same normalized values the
  // dirty check does — a saved "5" and a typed 5 are one threshold, not two.
  const now = _canonicalConfig(cfg), saved = _canonicalConfig(_savedConfig);
  return _PLAN_REBUILD_KEYS.some(
    k => _stableStringify(now[k]) !== _stableStringify(saved[k]));
}

// _markedClockedCount rides the 4-second status poll, so it can be seconds stale
// by the time a dialog quotes it. Ask for the current one instead.
async function _refreshMarkedClockedCount() {
  try {
    const d = await _fetchStatusOnce();
    if (d) _captureMarkedClockedCount(d);   // narrow on purpose: no relocking mid-save
  } catch (_) { /* keep the last polled count */ }
  return _markedClockedCount;
}

// Wait out the background work a save kicked off (the queue reconcile, a Summary
// refresh). Both own the queue, and the re-date endpoint refuses while either
// runs. Gives up after ~30s and lets the caller take the refusal.
async function _waitForQueueIdle() {
  for (let tries = 0; tries < 40; tries++) {
    await new Promise(resolve => setTimeout(resolve, tries === 0 ? 400 : 750));
    try {
      const d = await _fetchStatusOnce();
      if (!d) continue;
      _captureMarkedClockedCount(d);
      if (!d.summary_active && !d.run_active) return true;
    } catch (_) { /* transient; keep waiting on the active job */ }
  }
  return false;
}

// A cap the current library already exceeds means the very next cleanup pass
// prunes down to it — surface that at save time, in the confirm dialog.
function _immediatePruneWarning(cfg, nowCleanup) {
  const warnings = [];
  // Cap-driven prune: the cap is being armed/changed below the library size.
  const capWarning = (() => {
    if (cfg.MAX_LIBRARY_GB === null || cfg.MAX_LIBRARY_GB === undefined) return '';
    const capGb = Number(cfg.MAX_LIBRARY_GB);
    if (!Number.isFinite(capGb) || capGb <= 0) return '';
    if (!Number.isFinite(_lastKnownLibraryGb) || _lastKnownLibraryGb <= capGb) return '';
    // Only warn when the cap is the thing being armed or changed — an untouched,
    // already-saved cap should not nag on every unrelated save.
    const savedCap = Number(_savedConfig?.MAX_LIBRARY_GB);
    const capChanged = !(Number.isFinite(savedCap) && savedCap === capGb);
    const armingCleanup = nowCleanup && !_savedConfig.RUN_MODE?.startsWith('headroom');
    if (!capChanged && !armingCleanup) return '';
    const size = Math.round(_lastKnownLibraryGb).toLocaleString();
    const cap = prCommaNum(capGb);
    return nowCleanup
      ? `Library Size Cap (${cap} GB) is below the current library (~${size} GB) — Automatic Cleanup prunes it at the next daily run.`
      : `Library Size Cap (${cap} GB) is below the current library (~${size} GB) — the next Cleanup will prune down to it.`;
  })();
  if (capWarning) warnings.push(capWarning);
  // No lowered-delay warning: an existing mark keeps the delay it was
  // marked under, so changing the setting never makes existing marks ripe
  // sooner — only the delay-reset button (or a re-mark) applies the new value.
  // Headroom/Redline-driven prune: a free-space threshold the disk is ALREADY
  // past means the next Cleanup prunes to meet it. Like the cap, warn whenever
  // the threshold is being ARMED or CHANGED into a breached state — in a
  // non-deleting mode too, not only when flipping to Automatic Cleanup — and
  // also when arming Automatic Cleanup over an
  // already-breached (unchanged) threshold. Headroom's prune frees back to its
  // target (which also clears Redline), so prefer its message when both apply.
  const armingCleanup = nowCleanup && !_savedConfig.RUN_MODE?.startsWith('headroom');
  const total = Number(_diskStats?.total_gb);
  const used = Number(_diskStats?.used_gb);
  const free = Number(_diskStats?.free_gb);

  const head = Number(cfg.HEADROOM_GB);
  const savedHead = Number(_savedConfig?.HEADROOM_GB);
  const headChanged = !(Number.isFinite(savedHead) && savedHead === head);
  let headDeficit = 0;
  if (Number.isFinite(total) && total > 0 && Number.isFinite(used)
      && Number.isFinite(head) && head > 0) {
    headDeficit = Math.max(0, used - (total - head));
  }

  // Redline frees only back to its own floor — and in redline-only mode
  // HEADROOM_GB is always 0, so the redline warning must not hide behind a
  // headroom guard.
  const red = Number(cfg.REDLINE_GB);
  const savedRed = Number(_savedConfig?.REDLINE_GB);
  const redChanged = !(Number.isFinite(savedRed) && savedRed === red);
  let redDeficit = 0;
  if (Number.isFinite(red) && red > 0 && Number.isFinite(free) && free <= red) {
    redDeficit = Math.max(0, red - free);
  }

  if (headDeficit > 0 && (headChanged || armingCleanup)) {
    warnings.push(armingCleanup
      ? `Free space is already ${prGbAmount(headDeficit)} GB past the Headroom target — Automatic Cleanup prunes it at the next daily run.`
      : `Free space is already ${prGbAmount(headDeficit)} GB past the Headroom target — the next Cleanup will prune ~that much.`);
  } else if (redDeficit > 0 && (redChanged || armingCleanup)) {
    warnings.push(armingCleanup
      ? `Free space is already ${prGbAmount(redDeficit)} GB below the Redline floor — Automatic Cleanup frees ~that much within ~15 minutes.`
      : `Free space is already ${prGbAmount(redDeficit)} GB below the Redline floor — the next Cleanup will free ~that much.`);
  }
  return warnings.join(' ');
}

// ── Save button state ───────────────────────────────────────────────────────

function _setSaveText(text) {
  const top = document.getElementById('btn-save');
  _morphButtonText(top, text === 'Save Configuration' ? 'Save' : text);
}

function _setSavePending(on) {
  _savePending = !!on;
  const btn = document.getElementById('btn-save');
  if (btn) btn.classList.toggle('btn-pending-ellipsis', _savePending);
  _setSaveText(_savePending ? 'Saving' : 'Save Configuration');
  _syncDirtyState();
}

window.addEventListener('beforeunload', (event) => {
  // Guard unsaved edits too, not just an in-flight save: clicking Dashboard
  // in the header silently threw away a dirty form.
  if (!_savePending && !_dirty) return;
  event.preventDefault();
  event.returnValue = '';
});

async function saveConfig() {
  if (_savePending) return;
  if (_configActivityLocked()) {
    _applyConfigRuntimeLocks();
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }

  if (!_hasUnsavedChanges()) {
    clearDirty();
    return;
  }

  _setSavePending(true);

  const headroomOk = validateHeadroom({ show: true, focus: false });
  const maxPctOk = validateMaxHeadroomPct({ show: true, focus: false });
  const redlineOk = validateRedline({ show: true, focus: false });
  const capOk = validateLibraryCap({ show: true, focus: false });
  const delayOk = validateDeleteDelay({ show: true, focus: false });
  const monitorOk = await validateMonitorDirsRemote({ show: true, focus: false });
  if (!headroomOk || !maxPctOk || !redlineOk || !capOk || !delayOk || !monitorOk) {
    if (!headroomOk) validateHeadroom({ show: true, focus: true });
    else if (!maxPctOk) validateMaxHeadroomPct({ show: true, focus: true });
    else if (!redlineOk) validateRedline({ show: true, focus: true });
    else if (!capOk) validateLibraryCap({ show: true, focus: true });
    else if (!delayOk) validateDeleteDelay({ show: true, focus: true });
    else await validateMonitorDirsRemote({ show: true, focus: true });
    _syncDirtyState();
    _setSavePending(false);
    return;
  }

  _updateRunModeAvailability();
  const cfg = getFormConfig();

  const liveBlock = cfg.RUN_MODE?.startsWith('headroom')
    ? (_currentConnectionBlockingText() || _spaceThresholdBlockingText())
    : '';
  if (liveBlock) {
    showToast(liveBlock, 'danger');
    _selectNonDeletingMode();
    _setSavePending(false);
    return;
  }

  // Confirm any save the user might not realise deletes: switching to Automatic
  // Cleanup, saving a threshold the disk is already past (which prunes as soon
  // as Cleanups run), or changing the delay while marks are already waiting.
  // The dialog is built from the current form, so it can only ever describe the
  // values about to be written.
  const wasLive = _savedConfig.RUN_MODE?.startsWith('headroom');
  const nowCleanup = cfg.RUN_MODE?.startsWith('headroom');
  const pruneWarning = _immediatePruneWarning(cfg, nowCleanup);
  const armingCleanup = !wasLive && nowCleanup;
  // A delay change only applies to marks made from now on — the ones already
  // waiting keep the delay they were marked under. That surprises people, so
  // when it matters (there ARE marks) the dialog says so and offers to re-date
  // them, which is the same thing the Reset button beside the field does.
  // The count it quotes comes off the status poll, so refresh it first.
  if (_delayValueEdited()) await _refreshMarkedClockedCount();
  const delayChanged = _delayValueEdited() && _markedClockedCount > 0;
  const newDelay = prBlankNumber('DELETE_DELAY_DAYS', 1) ?? 1;
  // Changing a space threshold in the SAME save rebuilds the plan server-side,
  // so the marked set about to be quoted is the one being replaced. Say the
  // number is moving instead of naming a total that won't survive the save.
  const rebuildsPlan = delayChanged && _saveRebuildsPlan(cfg);

  let resetDelaysAfterSave = false;
  if (armingCleanup || pruneWarning || delayChanged) {
    // _setSavePending above put the button into its working look — ghosted,
    // animated ellipsis, cursor: progress — so the form is locked while the
    // save runs. Nothing is running until this dialog is answered, so drop the
    // look behind it. The lock (_savePending) stays, and the line after this
    // block puts the look back for the save itself.
    document.getElementById('btn-save')?.classList.remove('btn-pending-ellipsis');
    _setSaveText('Save Configuration');
    const lines = [];
    if (armingCleanup) {
      lines.push({ text: 'Automatic Cleanup deletes media files on its own, on the daily '
                       + 'schedule and whenever free space hits the Redline floor.', warn: true });
    }
    if (pruneWarning) lines.push({ text: pruneWarning, warn: true });
    if (delayChanged) {
      const n = _markedClockedCount;
      lines.push(rebuildsPlan
        ? 'This save also changes a space threshold, so the deletion plan is rebuilt '
          + 'and the marked list changes with it. Marks that remain keep the delay they '
          + `were made under — the new ${newDelay}-day delay applies to new marks.`
        : `${n} ${n === 1 ? 'movie is' : 'movies are'} already marked for `
          + 'deletion. They keep the delay they were marked under — the new '
          + `${newDelay}-day delay applies to future marks only.`);
    }
    const answer = await prConfirm({
      title: delayChanged && !armingCleanup && !pruneWarning
        ? 'Change the deletion delay?' : 'Save these settings?',
      body: lines,
      confirmText: 'Save',
      extraText: delayChanged
        ? (rebuildsPlan
            ? 'Save and re-date the remaining marks'
            : `Save and re-date ${_markedClockedCount === 1 ? 'the mark' : `all ${_markedClockedCount} marks`}`)
        : '',
      danger: !!(armingCleanup || pruneWarning),
    });
    if (!answer) { _setSavePending(false); return; }
    resetDelaysAfterSave = answer === 'extra';
    // The dialog can sit open for as long as the user takes to read it, so
    // re-run the gates it was shown in front of. The two-click confirm this
    // replaced got that for free — its second click re-entered saveConfig from
    // the top — and a run that started, or a connection that dropped, in the
    // meantime still has to block the save it blocked a moment ago.
    if (_configActivityLocked()) {
      _applyConfigRuntimeLocks();
      showToast('A run is active. Try again when it finishes.', 'warning');
      _setSavePending(false);
      return;
    }
    const liveBlockNow = nowCleanup
      ? (_currentConnectionBlockingText() || _spaceThresholdBlockingText()) : '';
    if (liveBlockNow) {
      showToast(liveBlockNow, 'danger');
      _selectNonDeletingMode();
      _setSavePending(false);
      return;
    }
  }
  _setSavePending(true);   // (re-)assert the working look for the save itself

  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    const d = await r.json();
    if (d.ok) {
      const savedConfig = d.config || getFormConfig();
      _savedConfig = savedConfig;  // Revert now reflects the newly saved state, including backend safety changes.
      // The mode choice is now the server's; a later autopause may follow the
      // live one again without this page holding a superseded pick.
      _runModeTouched = false;
      if (d.connection_health) _savedConfigHealth = d.connection_health;
      // The save round-trips connection probes and can take many seconds, and
      // the fields stay editable meanwhile. A wholesale populateForm would
      // silently discard anything typed during that window — keep the user's
      // newer edits and repopulate only the untouched fields with the
      // server-normalized values.
      const nowForm = getFormConfig();
      const cNow = _canonicalConfig(nowForm);
      const cSubmitted = _canonicalConfig(cfg);
      const editedKeys = Object.keys(cNow).filter(
        k => _stableStringify(cNow[k]) !== _stableStringify(cSubmitted[k]));
      if (editedKeys.length) {
        const merged = { ...savedConfig };
        editedKeys.forEach(k => { merged[k] = nowForm[k]; });
        populateForm(merged);
      } else {
        populateForm(savedConfig);
      }
      if (d.connection_health) _applyConfigHealth(d.connection_health);
      commitAppearanceSettings();
      _applyConfigRuntimeLocks();
      const _savedTz = _savedConfig.TIME_ZONE || 'auto';
      window.PR_SERVER_TIME_ZONE = _savedTz === 'auto' ? window.PR_HOST_TIME_ZONE : _savedTz;
      window.PR_DISPLAY_TIME_FORMAT = _savedConfig.DISPLAY_TIME_FORMAT || '12h';
      updateServerClockLine();
      if (editedKeys.length) markDirty(); else clearDirty();
      if (Array.isArray(d.server_software_auto_disabled) && d.server_software_auto_disabled.length) {
        // If Jellyfin is being dropped, its protections stop applying while
        // the same files may stay deletable through Plex — that lapse is the
        // dangerous part, not the deselection itself.
        const jfDropped = d.server_software_auto_disabled.some(n => /jellyfin/i.test(String(n)));
        showToast(d.server_software_auto_disabled.join(' and ')
          + ' was deselected: the API connection failed or the key is blank. '
          + 'Fix the connection, then re-enable it under Server software.'
          + (jfDropped ? ' Jellyfin favorite/collection protections are inactive until then.' : ''),
          'warning');
      }
      autoScanConnectedCollections();
      refreshImdbDownloadStatus();
      refreshCacheClearStatus();
      if (d.radarr_section_detection) {
        _applyRadarrSectionDetection(d.radarr_section_detection);
      } else {
        _syncSavedRadarrSectionDetection(_savedConfig);
      }
      if (d.summary_refresh_started) {
        pollLibrarySizeUntilIdle();
      }
      const health = d.connection_health || null;
      if (d.automatic_run_mode_paused) {
        // A forced pause normally lands on Monitor Only; when the same save
        // also removed a structural piece (last server deselected, last space
        // trigger disarmed), the lifecycle rests it all the way at Paused —
        // say where it actually landed.
        showToast('Saved. Automatic Cleanup switched to '
          + (d.run_mode_forced_off ? 'Paused' : 'Monitor Only') + ': '
          + (d.automatic_run_mode_paused_reason || 're-enable it once the connections check out.'), 'warning');
      } else if (d.run_mode_auto_enabled) {
        showToast('Saved. Setup complete — the scheduler is now in Monitor Only.', 'success');
      } else if (d.run_mode_forced_off) {
        showToast('Saved. The scheduler is paused — Monitor Only needs a connected server, '
          + 'a monitored path, and an up-to-date library database. Run Simulate to restore it.', 'warning');
      } else if (d.radarr_cleanup_forced_disabled) {
        showToast('Saved. Radarr cleanup was disabled.', 'warning');
      } else if (d.api_config_changed && health && !health.critical_ok) {
        showToast('Saved. Connection issues found.', 'danger');
      } else if (d.api_config_changed && health && health.severity === 'warning') {
        showToast('Saved with connection warnings.', 'warning');
      } else if (d.pending_unscheduled > 0) {
        showToast(`Saved. Space limits are satisfied — unscheduled ${d.pending_unscheduled} marked `
          + `deletion${d.pending_unscheduled === 1 ? '' : 's'}; the eligible queue remains.`, 'success');
      } else {
        showToast('Configuration saved', 'success');
      }
      // Chosen from the save dialog: apply the new delay to the marks that
      // already exist. Runs AFTER the save so the endpoint re-dates them
      // against the value that was just persisted, not the one being replaced.
      // A save that changed a threshold hands the queue to a background
      // reconcile on its way out, and the re-date endpoint refuses (409) while
      // that holds it — wait for the rebuilt queue, then re-date what it left.
      // It reports its own outcome; catching here keeps a failure in it from
      // reaching the enclosing catch, which would report the SAVE as failed.
      if (resetDelaysAfterSave) {
        if (d.reconcile === 'started' || d.summary_refresh_started) await _waitForQueueIdle();
        await resetMarkDelays().catch(() => {});
      }
    } else {
      if (d.connection_health) _applyConfigHealth(d.connection_health);
      showToast('Save failed: ' + d.error, 'danger');
    }
  } catch (e) {
    showToast('Save failed: ' + e.message, 'danger');
  } finally {
    _setSavePending(false);
  }
}


function _imdbUrlDirty() {
  const current = document.getElementById('IMDB_RATINGS_URL')?.value?.trim() || '';
  const saved = String(_savedConfig?.IMDB_RATINGS_URL || '').trim();
  return current !== saved;
}

function _formatImdbDownloadWait(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  if (total <= 0) return 'now';
  const hours = Math.floor(total / 3600);
  const minutes = Math.ceil((total % 3600) / 60);
  if (hours > 0 && minutes > 0) return `${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h`;
  return `${Math.max(1, minutes)}m`;
}

function _applyImdbDownloadButtonState() {
  const btn = document.getElementById('btn-imdb-download');
  const statusEl = document.getElementById('imdb-download-status');
  if (!btn) return;

  const status = _imdbDownloadStatus || {};
  const urlDirty = _imdbUrlDirty();
  const runtimeLocked = _configActivityLocked() || !_filesystemReady();
  const canDownload = !!status.can_download && !urlDirty && !runtimeLocked;
  const locked = !!status.download_locked;
  btn.disabled = !canDownload;
  btn.classList.remove('btn-busy');
  btn.classList.toggle('disabled', !canDownload);
  btn.setAttribute('aria-disabled', canDownload ? 'false' : 'true');

  if (!status.exists) btn.textContent = 'Download IMDb Ratings';
  else if (locked) btn.textContent = `Available in ${_formatImdbDownloadWait(status.seconds_until_download)}`;
  else btn.textContent = 'Download Latest IMDb Ratings';

  if (statusEl) {
    if (_configActivityLocked()) {
      statusEl.textContent = 'A run is active. IMDb downloads are disabled until it finishes.';
    } else if (!_filesystemReady()) {
      statusEl.textContent = _filesystemBlockingText();
    } else if (urlDirty) {
      statusEl.textContent = 'Save the IMDb dataset URL before downloading from it.';
    } else if (status.exists) {
      const size = status.size_mb ? `${status.size_mb} MB` : 'size unknown';
      const mtime = status.mtime_ts ? prFormatEpoch(status.mtime_ts) : (status.mtime || 'unknown date');
      if (locked) {
        const next = status.next_download_ts ? prFormatEpoch(status.next_download_ts) : (status.next_download || '24 hours after the last download');
        statusEl.textContent = `Local file: ${size}, last updated ${mtime}. Next manual download available ${next} (${_formatImdbDownloadWait(status.seconds_until_download)}).`;
      } else {
        statusEl.textContent = `Local file: ${size}, last updated ${mtime}. Download is available.`;
      }
    } else {
      statusEl.textContent = 'No local IMDb ratings file found. Download is available.';
    }
  }
}

async function refreshImdbDownloadStatus() {
  try {
    const r = await fetch(`/api/imdb/status?${prTimeQuery()}`, { cache: 'no-store' });
    _imdbDownloadStatus = await r.json();
  } catch (e) {
    _imdbDownloadStatus = { can_download: false, exists: false, reason: e.message || 'Status check failed.' };
  }
  _applyImdbDownloadButtonState();
}

async function forceDownloadImdbRatings() {
  const btn = document.getElementById('btn-imdb-download');
  if (!btn || btn.disabled) return;
  if (!_filesystemReady()) {
    showToast(_filesystemBlockingText(), 'warning');
    return;
  }

  btn.disabled = true;
  btn.classList.add('btn-busy');
  btn.textContent = 'Downloading…';

  try {
    const r = await fetch(`/api/imdb/download?${prTimeQuery()}`, { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    if (d.status) _imdbDownloadStatus = d.status;
    if (!r.ok || !d.ok) throw new Error(d.message || 'IMDb download failed.');
    showToast(d.message || 'IMDb ratings downloaded', 'success');
  } catch (e) {
    showToast('IMDb download failed: ' + e.message, 'danger');
  } finally {
    await refreshImdbDownloadStatus();
  }
}

function _applyClearCacheButtonState() {
  const btn = document.getElementById('btn-clear-cache');
  const statusEl = document.getElementById('cache-clear-status');
  if (!btn) return;

  const status = _cacheClearStatus || {};
  const runtimeLocked = _configActivityLocked() || !_filesystemReady();
  const canClear = !!status.can_clear && !runtimeLocked;
  btn.disabled = !canClear;
  btn.classList.remove('btn-busy');
  btn.classList.toggle('disabled', !canClear);
  btn.setAttribute('aria-disabled', canClear ? 'false' : 'true');
  btn.textContent = status.exists ? 'Clear cache' : 'No cache file';

  if (statusEl) {
    if (status.exists) {
      const size = status.size_kb ? `${status.size_kb} KB` : 'size unknown';
      const mtime = status.mtime_ts ? prFormatEpoch(status.mtime_ts) : (status.mtime || 'unknown date');
      const suffix = _configActivityLocked()
        ? ' A run is active.'
        : (!_filesystemReady() ? ` ${_filesystemBlockingText()}` : (status.can_clear ? ' Cache can be cleared.' : ` ${status.reason || 'Cache cannot be cleared right now.'}`));
      statusEl.textContent = `Local cache file: ${size}, last updated ${mtime}.${suffix}`;
    } else {
      statusEl.textContent = _configActivityLocked() ? 'A run is active. Cache is locked.' : (!_filesystemReady() ? _filesystemBlockingText() : (status.reason || 'No cache file found.'));
    }
  }
}

async function refreshCacheClearStatus() {
  try {
    const r = await fetch(`/api/cache/status?${prTimeQuery()}`, { cache: 'no-store' });
    _cacheClearStatus = await r.json();
  } catch (e) {
    _cacheClearStatus = { exists: false, can_clear: false, reason: e.message || 'Status check failed.' };
  }
  _applyClearCacheButtonState();
}

async function clearCache() {
  const btn = document.getElementById('btn-clear-cache');
  if (!btn || btn.disabled) return;
  if (!_filesystemReady()) {
    showToast(_filesystemBlockingText(), 'warning');
    return;
  }

  btn.disabled = true;
  btn.classList.add('btn-busy');
  btn.textContent = 'Clearing…';

  try {
    const r = await fetch(`/api/cache/clear?${prTimeQuery()}`, { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    if (d.status) _cacheClearStatus = d.status;
    if (!r.ok || !d.ok) throw new Error(d.message || 'Cache clear failed.');
    showToast(d.message || 'Cache cleared', 'success');
    if (d.summary_refresh_started) {
      pollLibrarySizeUntilIdle().then(refreshCacheClearStatus);
    }
  } catch (e) {
    showToast('Cache clear failed: ' + e.message, 'danger');
  } finally {
    await refreshCacheClearStatus();
  }
}

// ── Revert / Section Reset to Default ─────────────────────────────────────────

function revertForm() {
  populateForm(_savedConfig);
  revertAppearanceSettings();
  clearDirty();
  _applyConfigRuntimeLocks();
}

// The delay field holds a value different from the SAVED one — the reset
// button locks on this, since a reset applies the saved setting, not the
// on-screen edit.
function _delayValueEdited() {
  const saved = Number(_savedConfig?.DELETE_DELAY_DAYS ?? 1) || 1;
  return (prBlankNumber('DELETE_DELAY_DAYS', 1) ?? 1) !== saved;
}

// Restart the deletion-delay clock on every currently marked movie and season: each mark's
// delete date becomes today + the saved delay. The server refuses while a
// run/reconcile owns the queue.
async function resetMarkDelays() {
  if (_configActivityLocked()) {
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (_delayValueEdited()) {
    showToast('The delay has an unsaved change — Save or Revert it first.', 'warning');
    return;
  }
  const btn = document.getElementById('btn-reset-delays');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/queue/reset-delays', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      showToast(d.message || 'Delay clocks reset.', d.reset ? 'success' : 'info');
    } else {
      showToast(d.message || 'Could not reset the delay clocks.', 'warning');
    }
  } catch (e) {
    showToast('Could not reach the server.', 'danger');
  } finally {
    if (btn) btn.disabled = false;
    _applyConfigRuntimeLocks();   // restore the real disabled state
  }
}

// Dim the notification sub-fields when the master switch is off — a visual cue
// only. Fields stay editable and the test button stays live so a user can set
// up and verify destinations before flipping the switch on.
function syncNotifyEnabled() {
  const on = !!document.getElementById('NOTIFY_ENABLED')?.checked;
  document.getElementById('notify-fields')?.classList.toggle('notify-off', !on);
}

// Deliver a test alert to whatever is currently entered (saved or not).
async function testNotify() {
  if (_configActivityLocked()) {
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  const btn = document.getElementById('btn-notify-test');
  const out = document.getElementById('notify-test-result');
  if (!btn || !out) return;
  const cfg = getFormConfig();
  const payload = Object.fromEntries(_NOTIFY_KEYS.map(key => [key, cfg[key]]));
  btn.disabled = true;
  out.className = 'form-text';
  out.textContent = 'Sending…';
  try {
    const r = await fetch('/api/notify/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) {
      out.className = 'form-text ok';
      out.textContent = '✓ ' + (d.message || 'Sent.');
    } else if (d.rate_limited) {
      // The wiring may be perfectly good — this is a wait, not a failure.
      out.className = 'form-text wait';
      out.textContent = (d.error || 'Try again shortly.');
    } else {
      out.className = 'form-text err';
      out.textContent = '✗ ' + (d.error || 'Test failed.');
    }
  } catch (e) {
    out.className = 'form-text err';
    out.textContent = '✗ Could not reach the server.';
  } finally {
    btn.disabled = _configActivityLocked();
  }
}

function resetSectionToDefault(section) {
  if (section === 'notifications' && _configActivityLocked()) {
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (section === 'space' && _spaceThresholdsLocked()) {
    _applySpaceThresholdLock();
    showToast(_sectionLockReason('space') || 'A run is active. Try again when it finishes.', 'warning');
    return;
  }
  if (section === 'space' && !_spaceThresholdsHaveUsableLibrary()) {
    _applySpaceThresholdLock();
    showToast(_spaceThresholdUnavailableText('configuring Space Thresholds'), 'warning');
    return;
  }
  const fields = SECTION_DEFAULT_FIELDS[section];
  if (!fields) return;
  const next = getFormConfig();
  fields.forEach(key => {
    const value = _defaultConfig[key];
    next[key] = Array.isArray(value) ? [...value] : value;
  });
  populateForm(next);
  markDirty();
  const names = { space: 'Space Thresholds', advanced: 'Advanced', notifications: 'Notifications' };
  showToast(`${names[section] || 'Section'} reset to defaults. Save to keep the change.`, 'warning');
}

// Initialize section reset buttons from the current form values.
_syncSectionResetState();
// Apply the notifications master-switch ghost state on first paint.
syncNotifyEnabled();


const _cfgFormForIssueObserver = document.getElementById('cfg-form');
if (_cfgFormForIssueObserver) {
  new MutationObserver(() => _scheduleSectionIssueUpdate()).observe(_cfgFormForIssueObserver, {
    subtree: true,
    attributes: true,
    attributeFilter: ['class', 'style', 'hidden', 'aria-invalid']
  });
}
_scheduleSectionIssueUpdate();

// Apply the backend health result once on load so saved connection state,
// dependent-field gating, and any visible connection warnings are in sync.
if (_lastConfigHealth) _applyConfigHealth(_lastConfigHealth);

// Debug mode ⇔ NOT Automatic Cleanup (a no-delete diagnostic state can't run live). Debug
// mode is only tickable while the scheduler isn't deleting, and it can't be on while Automatic Cleanup
// is selected. The server validator enforces this too; this is the live UX.
(function _debugModeRunLock() {
  // Debug mode ⇔ NOT Automatic Cleanup. The Automatic Cleanup radio's ghosted look and its reason text are
  // owned by _updateRunModeAvailability (which reads DEBUG_MODE), so the ghost and
  // the "why" appear on page load, not just after a click. This IIFE handles only
  // the two-way coupling: dropping an Automatic Cleanup selection when Debug turns on,
  // and locking the Debug checkbox out while Automatic Cleanup is the selected run mode.
  const dbg  = document.getElementById('DEBUG_MODE');
  const live = document.getElementById('mode-headroom');
  const paused = document.getElementById('mode-paused');
  if (!dbg || !live) return;
  const sync = () => {
    if (dbg.checked && live.checked) {
      live.checked = false;
      if (paused) paused.checked = true;
    }
    dbg.disabled = live.checked;
    dbg.title = live.checked ? 'Set Scheduler Mode to Monitor Only to enable Debug mode.' : '';
    if (typeof _updateRunModeAvailability === 'function') _updateRunModeAvailability();
  };
  dbg.addEventListener('change', sync);
  document.querySelectorAll('input[name="RUN_MODE"]').forEach(r => r.addEventListener('change', sync));
  sync();
})();
