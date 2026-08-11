/* The Dashboard. Server-rendered state arrives from the inline preamble in
   dashboard.html; this file is deferred and cannot carry template
   expressions. */

let _summaryBusy = false;
let _summaryPollActive = false;
let _runStartPending = false;
let _runStartButtonId = null;
let _logPollTimer = null;
let _statusPollTimer = null;
let _logFetchInFlight = false;
let _statusFetchInFlight = false;
const _ACTIVE_LOG_LINES = 900;
// The finished-run view requests the whole log ('all', server-capped); active
// runs keep a light 900-line tail since they re-poll every second.
const _FINAL_LOG_LINES = 'all';
const _LOG_POLL_MS = 750;


function _positionRunHelp() {
  const help = document.getElementById('run-help');
  const card = help?.closest('.run-controls-card');
  const header = card?.querySelector('.card-header');
  if (!help || !card || !header) return;
  const headerBottom = header.offsetTop + header.offsetHeight;
  help.style.top = `${headerBottom + 10}px`;
}

function toggleRunHelp(forceShow = null) {
  const help = document.getElementById('run-help');
  const btn = document.getElementById('btn-run-help');
  if (!help || !btn) return;
  const show = forceShow === null ? !help.classList.contains('is-visible') : !!forceShow;
  _positionRunHelp();
  help.classList.toggle('is-visible', show);
  btn.classList.toggle('is-active', show);
  btn.setAttribute('aria-expanded', show ? 'true' : 'false');
  btn.title = show ? 'Hide run-control help' : 'Show run-control help';
}

window.addEventListener('resize', _positionRunHelp);
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') toggleRunHelp(false);
});

document.addEventListener('click', (ev) => {
  const help = document.getElementById('run-help');
  const btn = document.getElementById('btn-run-help');
  if (!help || !help.classList.contains('is-visible')) return;
  if (btn && btn.contains(ev.target)) return;
  toggleRunHelp(false);
});



function _runButtonDefaultLabel(id) {
  return id === 'btn-cleanup' ? (_debugMode ? 'Debug Cleanup (Dry Run)' : 'Cleanup')
       : (id === 'btn-sim' ? 'Simulate' : '');
}

function _setRunStartPending(on, buttonId = null) {
  const previous = _runStartButtonId;
  _runStartPending = !!on;
  _runStartButtonId = _runStartPending ? buttonId : null;

  if (previous && previous !== buttonId) {
    const oldBtn = document.getElementById(previous);
    oldBtn?.classList.remove('btn-pending-ellipsis');
    const oldLabel = _runButtonDefaultLabel(previous);
    if (oldLabel) _morphButtonText(oldBtn, oldLabel);
    oldBtn?.removeAttribute('aria-busy');
  }

  if (buttonId) {
    const btn = document.getElementById(buttonId);
    if (btn) {
      btn.classList.toggle('btn-pending-ellipsis', _runStartPending);
      if (_runStartPending) btn.setAttribute('aria-busy', 'true');
      else btn.removeAttribute('aria-busy');
      _morphButtonText(btn, _runStartPending ? 'Starting' : _runButtonDefaultLabel(buttonId));
    }
  }
  _applyButtonStates();
}

window.addEventListener('beforeunload', (event) => {
  if (!_runStartPending) return;
  event.preventDefault();
  event.returnValue = '';
});

// ── Jump-to-section: stats and the status pill deep-link into the log ────────
// Section availability comes from the server with every /api/logs/last poll,
// computed over the FULL log file — so a target is clickable exactly when the
// run has genuinely written that section, regardless of how much of the log is
// loaded in the window. Clicking loads JUST that section into the window
// (long wrapped lines make scroll-position math unreliable); "Show full log"
// returns to the complete log.
let _logSections = {};            // kind -> bool, from the last /api/logs/last poll
let _rpStatCounts = { scan: 0, eligible: 0, deletions: 0 };  // last frame's box counts
let _rpErrorsClickable = false;   // pill is a jump target only in error/warn states
let _logViewMode = 'tail';        // 'tail' = full/live log, 'section' = one section
let _logViewKind = null;

const _LOG_SECTION_LABELS = {
  errors: 'Errors & warnings', scan: 'Scan', eligible: 'Eligible candidates',
  deletions: 'Deletions', summary: 'Summary',
};
// The engine writes these sections strictly in this order, so a section is
// finished — and safe to open as a stable jump target — once a later section
// has started, or once the run itself has ended. While the run is still
// appending to a section, its stat box stays inert.
const _LOG_SECTION_ORDER = ['scan', 'eligible', 'deletions', 'summary'];
function _logSectionComplete(kind) {
  if (!_logSections[kind]) return false;
  if (!_active) return true;
  const i = _LOG_SECTION_ORDER.indexOf(kind);
  if (i === -1) return true;
  return _LOG_SECTION_ORDER.slice(i + 1).some(k => _logSections[k]);
}
// A stat box jumps to its log section only when that section is complete AND the
// box has something to show: a 0 count (Scanned / Eligible / Would-delete) would
// land on an empty or just-opened banner, so it stays inert. The Summary box is
// exempt — a run always writes a summary, even one that would free nothing
// ("Would free 0" is still worth reading).
function _logBoxJumpable(kind) {
  if (!_logSectionComplete(kind)) return false;
  if (kind === 'summary') return true;
  return (_rpStatCounts[kind] || 0) > 0;
}

function _applyLogJumpAffordance() {
  const targets = {
    scan: 'rp-jump-scan', eligible: 'rp-jump-eligible',
    deletions: 'rp-jump-deletions', summary: 'rp-jump-summary',
  };
  const titles = {
    scan: 'Open the scan section of the detailed log',
    eligible: 'Open the sorted eligible candidates in the detailed log',
    deletions: 'Open the deletions section of the detailed log',
    summary: 'Open the run summary in the detailed log',
  };
  for (const [kind, id] of Object.entries(targets)) {
    const el = document.getElementById(id);
    if (!el) continue;
    const on = _logBoxJumpable(kind);
    el.classList.toggle('log-jumpable', on);
    if (on) { el.setAttribute('role', 'button'); el.setAttribute('tabindex', '0'); el.title = titles[kind]; }
    else { el.removeAttribute('role'); el.removeAttribute('tabindex'); el.removeAttribute('title'); }
  }
  const pill = document.getElementById('rp-pill');
  if (pill) {
    const on = _rpErrorsClickable && !!_logSections.errors;
    pill.classList.toggle('log-jumpable', on);
    if (on) { pill.setAttribute('role', 'button'); pill.setAttribute('tabindex', '0'); pill.title = 'Show the errors and warnings from the detailed log'; }
    else { pill.removeAttribute('role'); pill.removeAttribute('tabindex'); pill.removeAttribute('title'); }
  }
}

function _setLogViewUI() {
  const badge = document.getElementById('log-window-view');
  const btn = document.getElementById('log-window-fullbtn');
  const inSection = _logViewMode === 'section';
  if (badge) {
    badge.hidden = !inSection;
    badge.textContent = inSection ? (' — ' + (_LOG_SECTION_LABELS[_logViewKind] || 'Section')) : '';
  }
  if (btn) btn.hidden = !inSection;
}

async function jumpToLogSection(kind) {
  if (kind === 'errors' && !_rpErrorsClickable) return;
  if (kind !== 'errors' && !_logBoxJumpable(kind)) return;   // still writing, or a 0-count box — inert
  if (!_logSections[kind]) return;   // not written yet
  const win = document.getElementById('log-window');
  const term = document.getElementById('log-term');
  if (!win || !term) return;
  try {
    const d = await _fetchJson(`/api/logs/section?kind=${kind}&${prTimeQuery()}&_=${Date.now()}`, { cache: 'no-store' }, 10000);
    if (!d.found) return;
    if (win.hidden) openLogWindow({ load: false });
    _logViewMode = 'section';
    _logViewKind = kind;
    _termPinned = false;             // static view: never follow live output
    _renderTermLines(term, prNormalizeDisplayedTimestamps(d.content || ''));
    term.scrollTop = 0;
    _setLogViewUI();
    _updateLogJumpButton();
  } catch (_) { /* transient fetch failure — leave the window as it is */ }
}

function showFullLog() {
  _logViewMode = 'tail';
  _logViewKind = null;
  _setLogViewUI();
  _termPinned = true;
  refreshLastLog(_active ? _ACTIVE_LOG_LINES : 'all');
}

// Guards the async detailed-log fetch against a second click: the 3s status
// poll runs _applyButtonStates() independently, which would otherwise re-enable
// the button mid-fetch and let a double-click fire two downloads.
let _logDlInFlight = false;
async function downloadDetailedLog(btn) {
  if (_active) {
    showToast('A run is active. You can download the log when it finishes.', 'warning');
    return;
  }
  if (_logDlInFlight) return;
  _logDlInFlight = true;
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Preparing…'; }
  let msg = null;
  try {
    const d = await _fetchJson(`/api/logs/last?lines=all&${prTimeQuery()}&_=${Date.now()}`, { cache: 'no-store' }, 20000);
    const text = prNormalizeDisplayedTimestamps(d.content || '');
    if (!prDownloadText(`mediareducer-run-log-${prFileStamp()}.txt`, text)) msg = 'Log is empty';
  } catch (_) {
    msg = 'Download failed';
  } finally {
    _logDlInFlight = false;
    if (btn) btn.textContent = msg || label;
    _applyButtonStates();   // single authority for .disabled (respects _active)
    if (btn && msg) setTimeout(() => { if (btn.textContent === msg) btn.textContent = label; }, 1600);
  }
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target;
  if (el && el.classList && el.classList.contains('log-jumpable')) {
    e.preventDefault();
    el.click();
  }
});

// Sticky autoscroll for the detailed log: follow new output only while the
// user is at (or near) the bottom. Scrolling up detaches so they can read
// history during an active run; scrolling back down (or the Jump button)
// re-attaches. Programmatic scrolls also fire 'scroll', so the pinned flag
// stays true after every automatic follow.
let _termPinned = true;

function _termAtBottom(term) {
  return term.scrollHeight - term.scrollTop - term.clientHeight < 24;
}

function _updateLogJumpButton() {
  const btn = document.getElementById('log-jump');
  if (btn) btn.hidden = _termPinned;
}

function _logJumpToBottom() {
  const term = document.getElementById('log-term');
  if (!term) return;
  _termPinned = true;
  term.scrollTop = term.scrollHeight;
  _updateLogJumpButton();
}

document.getElementById('log-term')?.addEventListener('scroll', (e) => {
  _termPinned = _termAtBottom(e.target);
  _updateLogJumpButton();
});

// Render log text as one block per line (see .log-line): long lines wrap with
// a hanging indent, and a copy still comes out as the original lines. Blank
// lines become a <br> block so they keep their height and copy as a newline.
// Skips the DOM rebuild when the text hasn't changed between polls.
let _termRenderedText = null;
function _renderTermLines(term, text) {
  if (text === _termRenderedText) return;
  _termRenderedText = text;
  const lines = (text.endsWith('\n') ? text.slice(0, -1) : text).split('\n');
  const frag = document.createDocumentFragment();
  for (const line of lines) {
    const div = document.createElement('div');
    div.className = 'log-line';
    if (line) div.textContent = line;
    else div.appendChild(document.createElement('br'));
    frag.appendChild(div);
  }
  term.replaceChildren(frag);
}

function _setTerm(content) {
  const term = document.getElementById('log-term');
  if (!term) return;
  const follow = _termPinned;
  const keepTop = term.scrollTop;
  _renderTermLines(term, prNormalizeDisplayedTimestamps(content || ''));
  term.scrollTop = follow ? term.scrollHeight : keepTop;
  _updateLogJumpButton();
}

// ── Run progress panel ──────────────────────────────────────────────────────
const _RP_STEP_INDEX = { checking:0, library:1, scanning:2, simulating:3, deleting:3, done:4 };
let _rpRunKey = null;
let _rpMaxIdx = 0;
let _rpStageKey = null;
let _rpStageStart = 0;
let _rpScanDoneAt = 0;   // when the scan loop hit its total (silent scoring follows)
let _rpLibDoneAt = 0;    // when path resolution hit its total (merge/index follows)
let _rpFreeDoneAt = 0;   // when the free loop hit its byte target (silent summary follows)

// Best-effort fill for a stage with nothing to count (checking, library):
// ease toward 90% on a clock — moves immediately, slows while it waits, and
// leaves the last stretch for the real completion.
function _rpStageCreep() {
  const t = Date.now() - _rpStageStart;
  return Math.min(90, 100 * (1 - Math.exp(-t / 5000)));
}

// A denominatored stage (scanning, deleting/simulating) finishes its counted
// loop and then does silent finalize work — the scan scores/sorts/logs every
// candidate; the free loop writes the run summary and runs Radarr cleanup.
// The counted portion fills to 92%; this eases the reserved top band forward
// across that tail so the bar keeps moving instead of parking at 100%.
function _rpFinalizeEase(since) {
  const t = Date.now() - since;
  return Math.min(99, 92 + 7 * (1 - Math.exp(-t / 2500)));
}


// The engine's outcome message is a lead line plus "Label: value" rows, and the
// issues arrive as a structured list. Split them here so the head keeps its
// one-line headline and the detail lands in the panel where there is room. A
// single sentence carrying every fact a run reports is unreadable past about
// two of them.
function _renderRunOutcome(p) {
  // Only the headline. The engine's message carries "Marked: ~34.6 GB" style
  // rows after it, and every one of them restates a stat box or the Marked &
  // Eligible window, so drawing them here would say the same numbers twice.
  // The rest of the message is still produced — the notifier falls back to it
  // and the debug report echoes it — it just isn't repeated on this page.
  //
  // A run that FAILED has no result to headline — it has a reason, and a reason
  // is a thing that went wrong, so it belongs in the issue list below with
  // everything else of that kind. The pill and the bar already say "Failed".
  const failed = p.status === 'error';
  // First line of the message, trimmed. The engine wraps its sentences, and a
  // leading newline would otherwise render as an empty headline.
  const lead = String(p.message || '').trim().split('\n')[0].trim();
  document.getElementById('rp-msg').textContent = failed ? '' : lead;

  // The failure leads the list. It is deliberately not a run_issues category:
  // those fold many events of a known kind into one counted line, and this is a
  // single event whose wording is written at the call site that stopped the run.
  const entries = [];
  if (failed) {
    entries.push({
      fatal: true,
      // Same fallback sentence the failure alert uses for the same state.
      line: lead || 'The run stopped before it finished.',
      // The stage is what someone can quote in a bug report and then find
      // verbatim in the log, where the engine wrote it as a ====== banner.
      note: p.stage ? `Failed during ${p.stage} — this stage name appears in the log.` : '',
    });
  }
  for (const issue of Array.isArray(p.issues) ? p.issues : []) entries.push(issue);

  const ul = document.getElementById('rp-issues');
  ul.textContent = '';
  for (const issue of entries) {
    const li = document.createElement('li');
    li.className = 'rp-issue-' + (issue.fatal ? 'fatal'
                                : issue.severity === 'error' ? 'error' : 'warning');
    const mark = document.createElement('span');
    mark.className = 'rp-issue-mark';
    // × for a run that stopped, ! for something it got past. Same glyph as the
    // red × the stepper puts on the step that died.
    mark.textContent = issue.fatal ? '×' : '!';
    const body = document.createElement('div');
    body.className = 'rp-issue-body';
    const line = document.createElement('div');
    line.className = 'rp-issue-line';
    line.textContent = issue.line || issue.label || '';
    body.appendChild(line);
    if (issue.note) {
      const note = document.createElement('div');
      note.className = 'rp-issue-note';
      note.textContent = issue.note;
      body.appendChild(note);
    }
    li.append(mark, body);
    ul.appendChild(li);
  }
  ul.hidden = !ul.children.length;
}

function _rpTriggerLabel(t) {
  const map = { 'scheduled daily':'Scheduled daily', 'REDLINE':'Redline', 'LIBRARY CAP':'Library Size Cap' };
  return String(t || '').split(' + ').map(x => map[x] || x).join(' + ');
}

function renderProgress(p) {
  if (!p || !p.status) return;
  const status = p.status;
  const phase = p.phase || 'checking';
  const mode = p.mode || '';
  const active = (status === 'starting' || status === 'running');
  const success = status === 'done';
  const withErrors = success && !!p.completed_with_errors;
  const stopped = status === 'stopped';
  const failed = status === 'error';
  const interrupted = !active && !success;
  const isSim = mode === 'debug_sim';
  const isInfo = mode === 'debug_info';
  // Debug Cleanup: drives the cleanup (deleting) path but deletes nothing. It reads
  // "Debugging" in yellow — not the red "Cleaning" of a run that really deletes.
  const isDebugCleanup = mode === 'debug_cleanup';

  if (p.started_at && p.started_at !== _rpRunKey) { _rpRunKey = p.started_at; _rpMaxIdx = 0; _rpStageKey = null; _rpScanDoneAt = 0; _rpFreeDoneAt = 0; _rpLibDoneAt = 0; }

  if (failed && p.error_code === 'imdb_ratings_unavailable') {
    const key = p.started_at || 'imdb';
    if (!_imdbHelpAlreadyShown(key)) {
      _markImdbHelpShown(key);
      showImdbHelpModal();
    }
  }

  const pill = document.getElementById('rp-pill');
  pill.classList.remove('is-done', 'is-neutral', 'is-danger', 'is-accent', 'is-warning');
  // The tone is the color of the BUTTON doing the work — red ONLY for the run
  // that deletes ("Cleaning"), yellow for Debug Cleanup, blue for every other
  // run, which takes nothing away. Blue says "Running" however the run started;
  // the trigger line beside this pill is what names which one it was.
  const [pillLabel, pillTone] =
      active   ? window.prRunTone({ cleanup: !isSim && !isInfo && !isDebugCleanup,
                                    debugCleanup: isDebugCleanup })
    // Failed = the run ABORTED (nothing or only part completed) — it must not
    // read "Done". "Done · errors" is reserved for runs that finished but
    // skipped some files; both route to the errors report section.
    : failed     ? ['Failed', 'is-danger']
    : withErrors ? ['Done · errors', 'is-warning']
    : stopped    ? ['Stopped', 'is-danger']
    :              ['Done', 'is-done'];
  pill.textContent = pillLabel;
  pill.classList.add(pillTone);
  _rpErrorsClickable = !active && (failed || stopped || withErrors);
  // Stash this frame's raw box counts so the jump affordance can keep a 0-count
  // box (Scanned / Eligible / Would-delete) inert. Summary is exempt — see
  // _logBoxJumpable — so its count isn't tracked here.
  _rpStatCounts = { scan: Number(p.scanned) || 0, eligible: Number(p.eligible) || 0,
                    deletions: Number(p.deleted) || 0 };
  _applyLogJumpAffordance();

  document.getElementById('rp-trigger').textContent = (active && p.trigger) ? ('· ' + _rpTriggerLabel(p.trigger)) : '';
  _renderRunOutcome(p);
  document.getElementById('rp-step-cleanup-label').textContent = isInfo ? 'Summary' : (isDebugCleanup ? 'Debugging' : (isSim ? 'Simulating' : 'Deleting'));

  let activeIdx = _RP_STEP_INDEX[phase];
  if (activeIdx === undefined) activeIdx = 0;
  _rpMaxIdx = Math.max(_rpMaxIdx, activeIdx);
  const terminal = !active;

  document.querySelectorAll('#rp-steps .rp-step').forEach((el, i) => {
    el.classList.remove('is-active', 'is-done', 'is-failed', 'is-success-line', 'is-success-end', 'is-warn-line', 'is-warn-end');
    const dot = el.querySelector('.rp-dot');
    if (interrupted) {
      // When a run stops or fails: completed phases keep their green dots and
      // green connectors, and the connector INTO the interrupted phase blends
      // green→red toward its red × (each line matches the circles it joins).
      if (i < activeIdx) { el.classList.add('is-done'); dot.textContent = '✓'; }
      else if (i === activeIdx) { el.classList.add('is-failed'); dot.textContent = '×'; }
      else { dot.textContent = String(i + 1); }
    } else if (terminal) {
      if (success) {
        // Successful runs get a green completion path. With skipped/error
        // items only the LAST dot goes warning yellow, so only its incoming
        // connector blends green→yellow — the rest stay green.
        if (i > 0) el.classList.add(withErrors && i === 4 ? 'is-warn-line' : 'is-success-line');
        if (i === 4) el.classList.add(withErrors ? 'is-warn-end' : 'is-success-end');
      }
      if (i <= _rpMaxIdx || i === 4) { el.classList.add('is-done'); dot.textContent = '✓'; }
      else { dot.textContent = String(i + 1); }
    } else if (i < activeIdx) { el.classList.add('is-done'); dot.textContent = '✓'; }
    else if (i === activeIdx) { el.classList.add('is-active'); dot.textContent = String(i + 1); }
    else { dot.textContent = String(i + 1); }
  });

  // The bar fills 0→100% PER STEP (the dots above say which one), resetting
  // between steps — each stage reads at a glance instead of one blended
  // overall percentage. Stages with a real denominator progress on it;
  // the rest ease forward on a clock (_rpStageCreep). The denominator's
  // presence is part of the stage key: when a stage's total/target arrives
  // mid-stage, the bar snaps to the real fraction instead of animating
  // backward from the creep.
  const hasDenominator = (phase === 'scanning' && (Number(p.total) || 0) > 0)
    || ((phase === 'deleting' || phase === 'simulating') && (Number(p.target_bytes) || 0) > 0);
  const stageKey = active ? `${phase}:${hasDenominator ? 'n' : 'c'}` : status;
  if (stageKey !== _rpStageKey) { _rpStageKey = stageKey; _rpStageStart = Date.now(); }

  let pct = 0, label = '', pctText = '';
  if (active) {
    if (phase === 'scanning') {
      const total = Number(p.total) || 0, scanned = Number(p.scanned) || 0;
      label = 'Scoring movies';
      pctText = total ? (scanned.toLocaleString() + ' / ' + total.toLocaleString()) : '';
      if (total && scanned >= total) {
        // Counted scan done; scoring/sorting the candidates emits nothing.
        if (!_rpScanDoneAt) _rpScanDoneAt = Date.now();
        pct = _rpFinalizeEase(_rpScanDoneAt);
      } else {
        _rpScanDoneAt = 0;
        pct = total ? (scanned / total) * 92 : _rpStageCreep();
      }
    } else if (phase === 'deleting' || phase === 'simulating') {
      const tgt = Number(p.target_bytes) || 0, freed = Number(p.bytes_freed) || 0;
      label = isDebugCleanup ? 'Freeing space (dry run)' : (isSim ? 'Simulating cleanup' : 'Freeing space');
      pctText = tgt ? (_formatReclaimedBytes(freed) + ' / ' + _formatReclaimedBytes(tgt)) : _formatReclaimedBytes(freed);
      if (tgt && freed >= tgt) {
        // Target met; the run summary + Radarr cleanup emit nothing.
        if (!_rpFreeDoneAt) _rpFreeDoneAt = Date.now();
        pct = _rpFinalizeEase(_rpFreeDoneAt);
      } else {
        _rpFreeDoneAt = 0;
        pct = tgt ? Math.min(1, freed / tgt) * 92 : _rpStageCreep();
      }
    } else if (phase === 'checking') { pct = _rpStageCreep(); label = 'Checking connections'; }
    else if (phase === 'library') {
      // ONE continuous bar for the whole step (deliberately NOT a
      // denominatored stage key — that reset the bar to empty when the
      // resolve counters arrived mid-step, splitting it into two fills).
      // The bar's FIRST half belongs to the size read + source fetch: the
      // creep is capped at 50% so it can never run ahead into resolution's
      // territory. The counters then own the back half (50→92%), so
      // resolution always starts exactly midway. Monotonic across the
      // hand-off by construction (≤50 before, ≥50 after).
      const rtot = Number(p.resolve_total) || 0, rdone = Number(p.resolved) || 0;
      label = 'Reading library';
      pctText = rtot ? (rdone.toLocaleString() + ' / ' + rtot.toLocaleString()) : '';
      pct = rtot ? 50 + (rdone / rtot) * 42 : Math.min(50, _rpStageCreep());
      if (rtot && rdone >= rtot) {
        // Resolution done; the source merge/indexing that follows emits nothing.
        if (!_rpLibDoneAt) _rpLibDoneAt = Date.now();
        pct = Math.max(pct, _rpFinalizeEase(_rpLibDoneAt));
      } else {
        _rpLibDoneAt = 0;
      }
    }
    else { pct = _rpStageCreep(); label = 'Working'; }
  } else if (interrupted) {
    pct = 0;
    label = failed ? 'Run failed' : 'Run stopped';
  } else {
    pct = 100;
    label = withErrors ? 'Complete — with errors' : 'Complete';
  }
  const bar = document.getElementById('rp-bar');
  if (bar.dataset.stage !== stageKey) {
    // New step: snap to empty instantly — animating the width backward would
    // read as the run losing progress.
    bar.dataset.stage = stageKey;
    bar.style.transition = 'none';
    bar.style.width = '0%';
    void bar.offsetWidth;
    bar.style.transition = '';
  }
  const shownPct = Math.max(0, Math.min(100, Math.round(pct)));
  bar.style.width = shownPct + '%';
  bar.classList.toggle('is-running', active && !terminal);
  bar.classList.toggle('is-done', terminal && success);
  // Keep the progressbar role's value in sync for screen readers.
  const track = bar.parentElement;
  if (track) track.setAttribute('aria-valuenow', String(shownPct));
  document.getElementById('rp-bar-label').textContent = label;
  document.getElementById('rp-bar-pct').textContent = pctText;

  let current = '';
  // Nothing here for a failed run: the reason and the stage it died in are one
  // thought, and _renderRunOutcome keeps them together in the issue list rather
  // than splitting them across the panel with the stat boxes in between.
  if (active) {
    if ((phase === 'deleting' || phase === 'simulating') && p.current_title) {
      current = (isSim ? 'Would remove: ' : 'Removing: ') + p.current_title;
    } else if (phase === 'scanning') current = 'Checking ratings, watch history and protection';
    // Stage 2 does different work in a run than in a quiet Summary: a Summary
    // only measures the library's size on disk, while a run measures it, pulls
    // the movie list from the media server, and then resolves each file path.
    // resolve_total only appears once that last part starts, so it's what tells
    // the two halves apart.
    else if (phase === 'library') {
      current = isInfo ? 'Measuring library size on disk…'
              : (Number(p.resolve_total) || 0) ? 'Resolving file paths…'
              : 'Measuring the library and fetching the movie list…';
    }
    else if (phase === 'checking') current = 'Checking APIs, paths, disk and thresholds';
  }
  document.getElementById('rp-current').textContent = current;

  document.getElementById('rp-stat-scanned').textContent = (Number(p.scanned) || 0).toLocaleString();
  document.getElementById('rp-stat-eligible').textContent = (Number(p.eligible) || 0).toLocaleString();
  // The engine reports what WOULD delete right now (never the queue size), so
  // these labels hold in every mode — a redline-only preview above the floor
  // reads "Would delete 0" while the queue builds in the background.
  // Dry runs (Simulate AND Debug Cleanup) never delete — their tiles must say
  // "Would", not claim removals that didn't happen.
  document.getElementById('rp-stat-deleted-label').textContent = (isSim || isDebugCleanup) ? 'Would delete' : 'Deleted';
  document.getElementById('rp-stat-deleted').textContent = (Number(p.deleted) || 0).toLocaleString();
  document.getElementById('rp-stat-freed-label').textContent = (isSim || isDebugCleanup) ? 'Would free' : 'Freed';
  document.getElementById('rp-stat-freed').textContent = _formatReclaimedBytes(p.bytes_freed);
}

let _progressFetchSeq = 0;
let _progressKnown = false;   // set once the panel has real data — idle polls then skip the fetch
async function refreshProgress() {
  // Fired by both the 750ms log poll and the 3s status poll with a 5s
  // timeout: on a slow server several requests overlap, and without an
  // ordering guard a LATE older snapshot would render after a newer one —
  // the progress bar and scanned counters visibly jump backward mid-run.
  const seq = ++_progressFetchSeq;
  try {
    const d = await _fetchJson(`/api/run/progress?_=${Date.now()}`, { cache: 'no-store' }, 5000);
    if (seq === _progressFetchSeq) { _progressKnown = true; renderProgress(d); }
  } catch (_) { /* keep last rendered state on a transient failure */ }
}

// Live re-render of the Cleanup Targets numbers from the status poll — the
// disk-bar marks and breach note already update live, and a threshold
// changed in another tab must not leave this card showing stale values until
// a reload. The mini-labels are fixed titles; only the values change. Only
// the no-monitored-library state keeps its server-rendered "Not set" — every
// configured state ("Off", "Redline only", "Disabled", a value) re-renders,
// so switching TO the all-off state in another tab updates too.
function _renderTargetRows(d) {
  _redlineOnly = !!d.redline_only;
  if (!d.thresholds_configured) return;
  const gb = (v) => `${prCommaNum(v, 0)} <span class="unit">GB</span>`;
  const headroomVal = document.querySelector('#target-row-headroom .value');
  if (headroomVal) {
    headroomVal.innerHTML = Number(d.headroom_gb) > 0 ? gb(d.headroom_gb)
      : (d.redline_only ? 'Redline only' : 'Off');
  }
  const redlineVal = document.querySelector('#target-row-redline .value');
  if (redlineVal) redlineVal.innerHTML = Number(d.redline_gb) > 0 ? gb(d.redline_gb) : 'Disabled';
  const capVal = document.querySelector('#target-row-cap .dashboard-mini-value');
  if (capVal) capVal.innerHTML = Number(d.library_cap_gb) > 0
    ? `<span>${prCommaNum(d.library_cap_gb, 0)}</span> <span class="unit">GB</span>` : 'Disabled';
}

function _isMobileLogView() {
  return window.matchMedia('(max-width: 640px)').matches;
}

function openLogWindow({ load = true } = {}) {
  const win = document.getElementById('log-window');
  if (!win) return;
  if (win.hidden) win.style.animation = '';
  win.hidden = false;
  if (!_isMobileLogView() && win.dataset.placed !== '1') {
    // First desktop open: convert the CSS-centered position to fixed pixels so
    // dragging and resizing have a concrete starting point. Layout geometry
    // (offset*) is used instead of getBoundingClientRect() because the
    // entrance animation is mid-flight at this point and would skew the rect.
    win.style.transform = 'none';
    win.style.left = Math.max(12, Math.round((window.innerWidth - win.offsetWidth) / 2)) + 'px';
    win.style.top = Math.max(12, win.offsetTop) + 'px';
    win.dataset.placed = '1';
  }
  if (load) {
    // "Show detailed log" always shows the complete log: full file for a
    // finished run, live tail while a run is streaming.
    showFullLog();
    _logJumpToBottom();
  }
  document.getElementById('log-window-close')?.focus();
}

function closeLogWindow() {
  const win = document.getElementById('log-window');
  if (win) win.hidden = true;
  document.getElementById('btn-log-toggle')?.focus();
}

let _logWinDrag = null;
function _logDragStart(e) {
  if (_isMobileLogView()) return;
  // Header BUTTONS are click targets, not drag handles — the drag's
  // preventDefault() would otherwise swallow their clicks entirely, and
  // "Show full log" is one of them.
  if (e.target.closest('button')) return;
  const win = document.getElementById('log-window');
  if (!win) return;
  // Cancel the entrance animation so the measured rect is the settled
  // position; openLogWindow restores it for the next open.
  win.style.animation = 'none';
  const rect = win.getBoundingClientRect();
  win.style.transform = 'none';
  win.style.left = rect.left + 'px';
  win.style.top = rect.top + 'px';
  win.dataset.placed = '1';
  _logWinDrag = { sx: e.clientX, sy: e.clientY, ox: rect.left, oy: rect.top, win };
  try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) {}
  window.addEventListener('pointermove', _logDragMove);
  window.addEventListener('pointerup', _logDragEnd);
  e.preventDefault();
}
function _logDragMove(e) {
  if (!_logWinDrag) return;
  const { sx, sy, ox, oy, win } = _logWinDrag;
  const w = win.offsetWidth;
  // Keep a grabbable strip on screen. The top clamp is 8px rather than a
  // height-relative one: the drag handle IS the title bar, so a window pushed
  // off the top takes the only thing that can bring it back with it.
  let nl = Math.min(Math.max(ox + (e.clientX - sx), 60 - w), window.innerWidth - 60);
  let nt = Math.min(Math.max(oy + (e.clientY - sy), 8), window.innerHeight - 40);
  win.style.left = nl + 'px';
  win.style.top = nt + 'px';
}
function _logDragEnd() {
  _logWinDrag = null;
  window.removeEventListener('pointermove', _logDragMove);
  window.removeEventListener('pointerup', _logDragEnd);
}

function _stopLogWatch() {
  if (_logPollTimer) {
    clearInterval(_logPollTimer);
    _logPollTimer = null;
  }
}

function _fetchJson(url, options = {}, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal })
    .then(r => r.json())
    .finally(() => clearTimeout(timer));
}


// The time's colon gets its own element so it can blink while a run is going.
// Built as nodes rather than markup: the text comes from our own formatter, but
// this is the one place a timestamp reaches the page, and there is no reason for
// it to be a place where markup could. Only the FIRST colon is wrapped — the
// date half has none today, and a second one would be a separator, not a clock.
function _renderLastRunValue(el, text) {
  const s = String(text ?? '');
  const i = s.indexOf(':');
  el.replaceChildren();
  if (i < 0) { el.textContent = s; return; }
  const colon = document.createElement('span');
  colon.className = 'last-run-colon';
  colon.textContent = ':';
  el.append(s.slice(0, i), colon, s.slice(i + 1));
}

function _updateLastRunDisplay(ts, fallback) {
  const el = document.getElementById('last-run-value');
  if (!el) return;
  if (ts !== null && ts !== undefined && ts !== '') {
    el.dataset.lastRunTs = ts;
    _renderLastRunValue(el, prFormatEpoch(ts, false));
  } else {
    _renderLastRunValue(el, fallback || '—');
  }
}

function _formatInitialTimestamps() {
  const el = document.getElementById('last-run-value');
  if (el && el.dataset.lastRunTs) _renderLastRunValue(el, prFormatEpoch(el.dataset.lastRunTs, false));
}

function _formatReclaimedBytes(bytes, fallbackLabel = '') {
  if (typeof fallbackLabel === 'string' && fallbackLabel.trim()) return fallbackLabel.trim();
  const n = Number(bytes);
  const safe = Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
  const gb = safe / 1000000000;
  if (gb >= 1000) return `${(gb / 1000).toFixed(1)} TB`;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  const mb = safe / 1000000;
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  return '0.0 GB';
}

function _splitReclaimedLabel(label) {
  const cleaned = String(label || '0.0 GB').trim().toUpperCase();
  const match = cleaned.match(/^(.+?)\s+([A-Z]+)$/);
  if (match) return { number: match[1], unit: match[2] };
  return { number: cleaned || '0.0', unit: 'GB' };
}

function _updateDeletedCounter(count, reclaimedBytes = 0, reclaimedLabel = '', markedCount = null, markedImminent = null) {
  const n = Number(count);
  const safe = Number.isFinite(n) && n >= 0 ? Math.trunc(n) : 0;
  const value = document.getElementById('pruned-count-value');
  const unit = document.getElementById('pruned-count-unit');
  const space = document.getElementById('pruned-space-value');
  const spaceNumber = document.getElementById('pruned-space-number');
  const spaceUnit = document.getElementById('pruned-space-unit');
  if (value) value.textContent = prCommaNum(safe, 0);
  if (unit) unit.textContent = safe === 1 ? 'ITEM' : 'ITEMS';
  if (markedCount !== null) {
    const countEl = document.getElementById('pruned-marked-count');
    const m = Math.max(0, Math.trunc(Number(markedCount) || 0));
    const imm = Math.max(0, Math.trunc(Number(markedImminent) || 0));
    // Both halves in every mode: marked-to-delete · total eligible (the same
    // dot separator as the pruned & regained button). The marked number AND
    // the word "Marked" in the label go red the moment anything is marked.
    if (countEl) {
      countEl.innerHTML =
        `<span id="pruned-marked-hot" class="${imm > 0 ? 'marked-hot' : ''}">${prCommaNum(imm, 0)} <span class="unit">ITEMS</span></span> ` +
        `<span class="unit" aria-hidden="true">·</span> ${prCommaNum(m, 0)} <span class="unit">ITEMS</span>`;
    }
    document.getElementById('marked-label-word')?.classList.toggle('marked-hot', imm > 0);
  }
  if (space) {
    const formatted = _formatReclaimedBytes(reclaimedBytes, reclaimedLabel);
    const parts = _splitReclaimedLabel(formatted);
    space.dataset.reclaimedBytes = String(Number.isFinite(Number(reclaimedBytes)) ? Math.max(0, Math.trunc(Number(reclaimedBytes))) : 0);
    if (spaceNumber && spaceUnit) {
      spaceNumber.textContent = _groupThousands(parts.number);
      spaceUnit.textContent = parts.unit;
    } else {
      space.textContent = `${_groupThousands(parts.number)} ${parts.unit}`;
    }
  }
}

// Deleted Movie History table: marked-for-deletion rows pinned on top, then
// the deleted history newest→oldest, paginated in-memory (25/page default).
let _dhPageSize = 25;
function _dhSetPageSize(v) {
  _dhPageSize = Math.max(1, parseInt(v, 10) || 25);
  _dhPageIdx = 0;
  _dhRenderPage();
}
let _dhRows = [];       // the open view's rows, in display order
let _dhLoading = false; // a view fetch is in flight — actions stay ghosted
let _dhPageIdx = 0;
let _dhView = 'deleted';   // 'marked' | 'deleted' — set by whichever button opened the modal
let _dhDeletedCount = 0;   // deleted.log entry count — Erase stays ghosted at 0

function _dhCap(s) { s = String(s || ''); return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

// A marked row's "Deletes" cell: the date it becomes deletable ("now" once
// ripe); redline-only marks delete on the Redline trigger, not a calendar
// date, and merely-eligible rows have no schedule at all.
function _dhDeletesCell(r) {
  if (!r.marked) return '—';
  if (r.delete_on) return (r.days_remaining !== null && r.days_remaining <= 0) ? 'now' : r.delete_on;
  return 'at Redline';
}

// One pool, two kinds: the Type column says which — a season names itself
// "TV Show · S2" so the title can stay the show's plain name.
function _dhTypeCell(r) {
  return r.media === 'tv' ? 'TV Show · S' + (r.season ?? '?') : 'Movie';
}

function _dhRenderRow(r) {
  const path = prHtmlEsc(r.path || '');
  if (r._kind === 'marked') {
    const bits = [];
    if (r.score !== null && r.score !== undefined && r.score !== '') bits.push('score ' + prHtmlEsc(r.score));
    // A dated mark's "when" text would repeat the Deletes column — show the
    // marked-at timestamp instead. Undated rows (eligible, redline-only)
    // keep their explanatory "when" text.
    if (r.marked && r.delete_on) { if (r.time) bits.push('marked ' + prHtmlEsc(r.time)); }
    else if (r.when) bits.push(prHtmlEsc(_dhCap(r.when)));
    // Only genuinely MARKED rows take the danger tint + rail (the library
    // table's marked treatment); eligible rows stay plain.
    return `<tr class="${r.marked ? 'dh-marked' : ''}">`
      + (r.marked ? `<td><span class="dh-badge marked">marked</span></td>`
                  : `<td><span class="dh-badge eligible">eligible</span></td>`)
      + `<td class="dh-title">${prHtmlEsc(r.title || '—')}</td>`
      + `<td class="dh-num">${prHtmlEsc(_dhTypeCell(r))}</td>`
      + `<td class="dh-num">${prHtmlEsc(r.size || '—')}</td>`
      + `<td class="dh-num">${prHtmlEsc(_dhDeletesCell(r))}</td>`
      + `<td class="dh-details">${bits.join(' · ') || '—'}</td>`
      + `<td class="dh-path" title="${path}">${path}</td></tr>`;
  }
  // A deleted line the server couldn't parse into fields: show it whole.
  if (!r.title && r.line) {
    return `<tr><td><span class="dh-badge deleted">deleted</span></td>`
      + `<td class="dh-details" colspan="6">${prHtmlEsc(r.line)}</td></tr>`;
  }
  return `<tr>`
    + `<td><span class="dh-badge deleted">deleted</span></td>`
    + `<td class="dh-title">${prHtmlEsc(r.title || '—')}</td>`
    + `<td class="dh-num">${prHtmlEsc(_dhTypeCell(r))}</td>`
    + `<td class="dh-num">${prHtmlEsc(r.size || '—')}</td>`
    + `<td class="dh-num">${prHtmlEsc(r.time || '—')}</td>`
    + `<td class="dh-details">${prHtmlEsc(r.why || '—')}</td>`
    + `<td class="dh-path" title="${path}">${path}</td></tr>`;
}

function _dhRenderPage() {
  const tbody = document.getElementById('deleted-history-tbody');
  const wrap = document.getElementById('deleted-history-wrap');
  const empty = document.getElementById('deleted-history-empty');
  const pager = document.getElementById('deleted-history-pager');
  const total = _dhRows.length;
  const pages = Math.max(1, Math.ceil(total / _dhPageSize));
  _dhPageIdx = Math.min(Math.max(0, _dhPageIdx), pages - 1);
  if (!total) {
    if (tbody) tbody.innerHTML = '';
    if (wrap) wrap.hidden = true;
    if (empty) {
      empty.hidden = false;
      empty.textContent = _dhView === 'marked'
        ? 'Nothing is marked or eligible yet — run Simulate to build the deletion plan.'
        : 'No movies have been pruned yet.';
    }
    if (pager) pager.hidden = true;
    return;
  }
  const start = _dhPageIdx * _dhPageSize;
  if (tbody) tbody.innerHTML = _dhRows.slice(start, start + _dhPageSize).map(_dhRenderRow).join('');
  if (wrap) { wrap.hidden = false; wrap.scrollTop = 0; }
  if (empty) empty.hidden = true;
  if (pager) {
    // Keep the pager visible whenever a smaller page size could paginate —
    // otherwise picking 100/page on a 60-row list hides the size dropdown too.
    pager.hidden = total <= 10;
    document.getElementById('dh-prev').disabled = _dhPageIdx <= 0;
    document.getElementById('dh-next').disabled = _dhPageIdx >= pages - 1;
    document.getElementById('dh-page-label').textContent =
      `${(start + 1).toLocaleString()}–${Math.min(start + _dhPageSize, total).toLocaleString()} of ${total.toLocaleString()}`;
  }
}

function _dhPage(delta) { _dhPageIdx += delta; _dhRenderPage(); }

function _dhHeaderText() {
  // Mirror the on-screen table: the marked view's 4th column is the deletion
  // schedule, the deleted view's is the deletion timestamp.
  return ['Status', 'Title', 'Type', 'Size', _dhView === 'marked' ? 'Deletes' : 'Date',
          'Details', 'Path'];
}

function _dhRowText(r) {
  const dash = '—';
  if (r._kind === 'marked') {
    const bits = [];
    if (r.score !== null && r.score !== undefined && r.score !== '') bits.push('score ' + r.score);
    if (r.marked && r.delete_on) { if (r.time) bits.push('marked ' + r.time); }
    else if (r.when) bits.push(_dhCap(r.when));
    return [r.marked ? 'Marked' : 'Eligible', r.title || dash, _dhTypeCell(r), r.size || dash,
            'deletes ' + _dhDeletesCell(r), bits.join(' · ') || dash, r.path || dash];
  }
  if (!r.title && r.line) return ['Deleted', r.line, '', '', '', '', ''];
  return ['Deleted', r.title || dash, _dhTypeCell(r), r.size || dash, r.time || dash, r.why || dash, r.path || dash];
}

function downloadDeletedHistory(btn) {
  if (_active) {
    showToast('A run is active. You can download the log when it finishes.', 'warning');
    return;
  }
  const label = btn ? btn.textContent : '';
  if (!_dhRows.length) {
    if (btn) { btn.textContent = 'Nothing to save'; setTimeout(() => { btn.textContent = label; }, 1600); }
    return;
  }
  const header = _dhHeaderText();
  const lines = [header.join('\t')].concat(
    _dhRows.map(r => _dhRowText(r).map(c => String(c ?? '').replace(/[\t\r\n]+/g, ' ')).join('\t'))
  );
  const text = prNormalizeDisplayedTimestamps(lines.join('\n') + '\n');
  const stem = _dhView === 'marked' ? 'mediareducer-marked-deletions' : 'mediareducer-deleted-log';
  if (!prDownloadText(`${stem}-${prFileStamp()}.txt`, text)) {
    if (btn) { btn.textContent = 'Nothing to save'; setTimeout(() => { btn.textContent = label; }, 1600); }
  }
}

async function loadDeletedHistory() {
  const summary = document.getElementById('deleted-history-summary');
  const wrap = document.getElementById('deleted-history-wrap');
  const empty = document.getElementById('deleted-history-empty');
  const pager = document.getElementById('deleted-history-pager');
  const eraseBtn = document.getElementById('btn-clear-deleted-log');
  if (summary) summary.textContent = 'Loading…';
  if (wrap) wrap.hidden = true;
  if (empty) { empty.hidden = false; empty.textContent = 'Loading…'; }
  if (pager) pager.hidden = true;
  // Ghost the actions until THIS view's rows land — a click mid-load would
  // download the previous view's rows under this view's filename, and the
  // Erase button could linger from a prior deleted-history view. _dhLoading
  // keeps the status poll's _applyCleanupState from un-ghosting them mid-fetch.
  _dhLoading = true;
  const dlBtn = document.getElementById('btn-download-deleted-log');
  if (dlBtn) dlBtn.disabled = true;
  if (eraseBtn) { eraseBtn.hidden = _dhView === 'marked'; eraseBtn.disabled = true; }
  try {
    const d = await _fetchJson(`/api/logs/deleted?limit=all&${prTimeQuery()}&_=${Date.now()}`, { cache: 'no-store' }, 12000);
    _dhLoading = false;
    const count = Number(d.count || 0);
    const marked = Array.isArray(d.marked) ? d.marked : [];
    const deleted = Array.isArray(d.entries) ? d.entries : [];
    const imminentCount = marked.filter(r => r.marked).length;
    _updateDeletedCounter(count, d.reclaimed_bytes || 0, d.reclaimed_label || '',
                          d.marked_count !== undefined ? d.marked_count : marked.length,
                          imminentCount);
    const reclaimed = _formatReclaimedBytes(d.reclaimed_bytes || 0, d.reclaimed_label || '');
    if (summary) {
      // Said once, here. The rows carry a rank and nothing else about the
      // ordering — repeating "next in line if more space is needed" down two
      // thousand rows is the same sentence in the column a reader is scanning
      // for what's different about each movie.
      summary.textContent = _dhView === 'marked'
        ? (imminentCount
            ? (_redlineOnly
                ? `Redline is breached — ${imminentCount} marked to delete on the next Cleanup, ${marked.length - imminentCount} eligible behind them in the order they would go.`
                : `${imminentCount} marked for deletion · ${marked.length - imminentCount} more eligible, in the order they would go if more space is needed.`)
            : (_redlineOnly
                // Redline-only above the floor marks nothing, so every row's
                // Deletes cell reads "—". Without this the one window that
                // exists to say WHEN these go would not mention the Redline at
                // all, in the mode whose whole premise is that it is the trigger.
                ? `${marked.length} ${marked.length === 1 ? 'movie is' : 'movies are'} eligible, in the order they would go when free space hits the Redline floor.`
                : `${marked.length} ${marked.length === 1 ? 'movie is' : 'movies are'} eligible, in the order they would go if space is needed — none marked yet.`))
        : `${count} ${count === 1 ? 'movie has' : 'movies have'} been pruned · ${reclaimed} reclaimed.`;
    }
    // One list per view (both already come newest-first / in plan order from
    // the server): the left button shows the queue, the stats button the history.
    _dhRows = _dhView === 'marked'
      ? marked.map(e => Object.assign({ _kind: 'marked' }, e))
      : deleted.map(e => Object.assign({ _kind: 'deleted' }, e));
    _dhPageIdx = 0;
    _dhRenderPage();
    // Erase truncates deleted.log — history-view only. Download saves whichever
    // list is on screen.
    _dhDeletedCount = count;
    if (eraseBtn) {
      eraseBtn.hidden = _dhView === 'marked';
      eraseBtn.disabled = _active || count <= 0;
    }
    if (dlBtn) {
      dlBtn.disabled = _active || !_dhRows.length;
      dlBtn.title = _active ? 'Available when the run finishes' : '';
    }
  } catch (err) {
    _dhLoading = false;
    if (summary) summary.textContent = 'Could not load deleted.log.';
    _dhRows = [];
    _dhDeletedCount = 0;
    if (wrap) wrap.hidden = true;
    if (empty) { empty.hidden = false; empty.textContent = `Could not load deleted.log: ${err}`; }
    if (pager) pager.hidden = true;
    // Keep the actions ghosted — there is nothing valid to download or erase.
    if (dlBtn) dlBtn.disabled = true;
    if (eraseBtn) { eraseBtn.hidden = _dhView === 'marked'; eraseBtn.disabled = true; }
  }
}

function openDeletedHistory(view) {
  // Two views over one modal: 'marked' (the queue) from the left button,
  // 'deleted' (deleted.log history) from the stats button.
  _dhView = view === 'marked' ? 'marked' : 'deleted';
  const title = document.getElementById('deleted-history-title');
  if (title) title.textContent = _dhView === 'marked'
    ? 'Marked & Eligible Deletions'
    : 'Deletion History';
  // The date column means different things per view — label it so a marked
  // row's date is unambiguously WHEN IT DELETES (the marked-at timestamp
  // lives in Details), and a deleted row's date is when it was deleted.
  const dateTh = document.getElementById('dh-th-date');
  if (dateTh) dateTh.textContent = _dhView === 'marked' ? 'Deletes' : 'Date';
  const modalEl = document.getElementById('deleted-history-modal');
  if (modalEl && window.bootstrap) bootstrap.Modal.getOrCreateInstance(modalEl).show();
  loadDeletedHistory();
}

async function clearDeletedHistory() {
  if (_active) {
    showToast('A run is active. Try again when it finishes.', 'warning');
    return;
  }
  // One-click by design: this only truncates the deleted.log history file —
  // no media files, run logs, or config are touched.
  const eraseBtn = document.getElementById('btn-clear-deleted-log');
  if (eraseBtn) { eraseBtn.disabled = true; eraseBtn.classList.add('btn-busy'); }
  try {
    const r = await fetch('/api/logs/deleted/clear', { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    if (!r.ok || d.ok === false) throw new Error(d.message || 'Could not erase deleted.log.');
    // Erase only truncates deleted.log — the marked & eligible queue is a
    // different file and stays untouched. Erase is only reachable from the
    // deleted-history view, so this view's rows all go; the marked counts on
    // the Last Run button come from the status poll, not from these rows, so
    // leave them alone (markedCount null = don't touch).
    _dhDeletedCount = 0;
    _dhRows = [];
    _dhPageIdx = 0;
    _dhRenderPage();
    _updateDeletedCounter(0, 0, '0.0 GB');
    const summary = document.getElementById('deleted-history-summary');
    if (summary) summary.textContent = '0 movies have been pruned · 0 GB reclaimed.';
    showToast(d.message || 'Deleted history erased.', 'success');
  } catch (err) {
    showToast(String(err.message || err), 'danger');
  } finally {
    // Clear the busy state on success AND failure alike.
    if (eraseBtn) {
      eraseBtn.classList.remove('btn-busy');
      eraseBtn.disabled = _active || _dhDeletedCount <= 0;
    }
  }
}

let _dashboardTooltipEl = null;
function _dashboardTooltip() {
  if (_dashboardTooltipEl) return _dashboardTooltipEl;
  _dashboardTooltipEl = document.createElement('div');
  _dashboardTooltipEl.className = 'dashboard-run-tooltip';
  document.body.appendChild(_dashboardTooltipEl);
  return _dashboardTooltipEl;
}

function _applyCleanupState(cleanupState) {
  if (!cleanupState) return;
  _summaryOk = !cleanupState.summary_disabled;
  _simulateOk = !cleanupState.simulate_disabled;
  // The Cleanup button morphs into the yellow Debug Cleanup in Debug mode. It
  // ignores the live/safety thresholds (deletes nothing) but ghosts until a
  // Simulate has built the queue it replays — that's cleanupState.debug_disabled.
  // Bind btn-cleanup to the debug state in Debug mode, else the live state. Without
  // this the status poll would overwrite the correct server render seconds after
  // load (re-blocking on a safety target, or un-ghosting with no queue).
  const _cleanupButtonBlocked = _debugMode ? cleanupState.debug_disabled : cleanupState.cleanup_disabled;
  _cleanupOk = !_cleanupButtonBlocked;
  // Same two flags the server gates next_run_time on, with its .get() defaults:
  // connection health absent means healthy, thresholds absent means not ready.
  // See the declaration for why the button's own state will not do.
  const _health = cleanupState.connection_health || {};
  const _thresholds = cleanupState.space_thresholds || {};
  const _critOk = _health.critical_ok === undefined ? true : !!_health.critical_ok;
  _cleanupGateBlocked = !_critOk || !_thresholds.ok_for_cleanup;
  _applyCleanupButtonTitle();
  _safetyBlocked =!!(cleanupState.space_thresholds && cleanupState.space_thresholds.safety_blocked);
  _simulateRequired = !!(cleanupState.space_thresholds && cleanupState.space_thresholds.simulate_required);
  _summaryDisabledMessage = cleanupState.summary_tooltip || 'Connect the selected media server first.';
  _simulateDisabledMessage = cleanupState.simulate_tooltip || 'Fix Space Thresholds first.';
  _cleanupDisabledMessage = (_debugMode ? cleanupState.debug_tooltip : cleanupState.cleanup_tooltip)
                         || 'Fix Space Thresholds first.';

  // The hover tooltip explains why a button is blocked, so it must be empty when
  // the button is enabled. Using the *DisabledMessage fallbacks here would show a
  // stale "Fix Space Thresholds" hint over a perfectly valid, clickable button.
  // The fallbacks are still used for the click-toast on a disabled button below.
  const _simulateHoverTip = cleanupState.simulate_disabled ? _simulateDisabledMessage : '';
  const _cleanupHoverTip = _cleanupButtonBlocked ? _cleanupDisabledMessage : '';

  document.getElementById('btn-sim')?.closest('[data-hover-tip]')?.setAttribute('data-hover-tip', _simulateHoverTip);
  document.getElementById('btn-cleanup')?.closest('[data-hover-tip]')?.setAttribute('data-hover-tip', _cleanupHoverTip);
  _applyButtonStates();
}

// Single source of truth for run/summary button enablement. Summary is the
// Storage card's ↻ button. "Busy" covers both a real run and a background
// Summary so neither can start while the other runs.
function _applyButtonStates() {
  const busy = _active || _summaryBusy || _runStartPending;
  const sim = document.getElementById('btn-sim');
  const live = document.getElementById('btn-cleanup');
  const stopBtn = document.getElementById('btn-stop');
  const refresh = document.getElementById('btn-summary-refresh');
  if (sim) sim.disabled = busy || !_simulateOk;
  if (live) live.disabled = busy || !_cleanupOk;
  if (stopBtn) stopBtn.disabled = !_active || _runStartPending;
  if (refresh) {
    refresh.disabled = busy || !_summaryOk;
    refresh.classList.toggle('is-spinning', _summaryBusy);
    refresh.title = _summaryBusy
      ? 'Refreshing…'
      : (!_summaryOk ? (_summaryDisabledMessage || 'Connect the selected media server first')
                     : 'Refresh disk & library size');
  }
  // Downloading a log while a run is writing to it would capture a torn,
  // half-written file — block both log downloads until the run finishes.
  const logDl = document.getElementById('log-window-dlbtn');
  if (logDl) {
    logDl.disabled = _active || _logDlInFlight;
    logDl.title = _active ? 'Available when the run finishes' : '';
  }
  // While a view is loading, _dhRows/_dhDeletedCount still hold the PREVIOUS
  // view's data — a poll landing mid-fetch must not un-ghost the actions
  // (a click would export the old view's rows under the new view's filename).
  const delDl = document.getElementById('btn-download-deleted-log');
  if (delDl) {
    delDl.disabled = _active || _dhLoading || !_dhRows.length;
    delDl.title = _active ? 'Available when the run finishes' : '';
  }
  // Erase is also live-ghosted: the modal can be open when a run starts, and
  // the engine appends to deleted.log while it runs.
  const delErase = document.getElementById('btn-clear-deleted-log');
  if (delErase && !delErase.classList.contains('btn-busy')) {
    delErase.disabled = _active || _dhLoading || _dhDeletedCount <= 0;
    delErase.title = _active ? 'Available when the run finishes' : '';
  }
}

function _updateStorageCard(disk, libraryGb) {
  const libVal = document.getElementById('library-size-value');
  const libUnit = document.getElementById('library-size-unit');
  if (libVal) {
    // Keep the "GB" unit visible either way, so an unknown size reads "— GB".
    if (libUnit) libUnit.hidden = false;
    libVal.textContent = (libraryGb !== null && libraryGb !== undefined && libraryGb !== '')
      ? prMeasuredGb(libraryGb)
      : '—';
  }
  // The bar itself (used fill, library segment, markers, legend) belongs to
  // prRenderDiskBar via _positionStorageBar below; only the card's text is set
  // here.
  if (disk) {
    const free = document.getElementById('storage-free');
    const sub = document.getElementById('storage-sub');
    if (free && disk.free_gb !== undefined) free.textContent = prMeasuredGb(disk.free_gb);
    if (sub && disk.used_gb !== undefined) {
      sub.innerHTML = `${prMeasuredGb(disk.used_gb)} / ${prMeasuredGb(disk.total_gb)} GB &nbsp;(${disk.pct_used}% USED)`;
    }
  } else if (!_monitoringActive) {
    // Nothing monitored: dash the whole card. (A disk read that simply FAILED
    // while monitoring keeps its "Could not read… press ↻" hint, so only dash
    // when there are genuinely no monitored paths.)
    const free = document.getElementById('storage-free');
    const sub = document.getElementById('storage-sub');
    if (free) free.textContent = '—';                     // "— GB free" (unit span stays)
    if (sub) sub.innerHTML = '— / —&nbsp;(—% USED)';       // structure kept, numbers dashed
  }
  _positionStorageBar(disk, libraryGb);
}

// prCommaNum / prGbAmount are shared with Configuration — base.html.
// Thousands-separate an already-formatted numeric string, keeping its decimals
// exactly (mirrors the server's group_int filter). Non-numeric passes through.
function _groupThousands(numStr) {
  const s = String(numStr);
  const neg = s.startsWith('-');
  const body = neg ? s.slice(1) : s;
  const dot = body.indexOf('.');
  const intPart = dot === -1 ? body : body.slice(0, dot);
  if (!/^\d+$/.test(intPart)) return s;
  const frac = dot === -1 ? '' : body.slice(dot);
  return (neg ? '-' : '') + intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + frac;
}

// Lay out the storage bar's library segment and threshold markers.
//   • The library is right-aligned inside the used fill: it's the part that
//     gets trimmed, so its right edge IS the used edge that the Headroom /
//     Redline lines push back on. Its left edge is the fixed non-library used.
//   • Headroom/Redline are free-space targets, so each sits at the used
//     position total - target (the disk hits it as it fills to that point).
//   • The library cap is a library-size target: anchored to the library's
//     base, it marks where the used edge lands once the library is capped.
let _lastStorageSnapshot = null;   // last disk/library reading — feeds the Cleanup confirm estimate

// Daily-run-time helpers: the scheduler fires headroom/cap cleanups at the
// configured time of day (24h HH:MM in the operating zone).
function _runTimeLabel() {
  return prTimeLabel(_dailyRunTime || '00:00');
}
function _serverNowHHMM() {
  return prZonedHHMM(new Date(prServerEpochNow() * 1000)) || '00:00';
}
function _nextTickAfterRunTime() {
  // Whether the NEXT scheduler tick lands at/after the daily run time — the
  // red countdown must not promise a deletion a too-early tick will skip.
  if (!_nextRunTime) return false;
  const hhmm = prZonedHHMM(new Date(_nextRunTime));
  return hhmm === null ? true : hhmm >= (_dailyRunTime || '00:00');
}

function _updateBreachNote() {
  // The counterpart of the satisfied-state button ghosting: when a threshold
  // is currently BREACHED, say so at a glance — with the ~GB a run would
  // free — instead of leaving that knowledge to a run attempt. Lives at the
  // bottom of the Cleanup Targets card (it's the targets being breached),
  // and the specific crossed target's row goes red with it.
  const el = document.getElementById('breach-note');
  if (!el) return;
  if (!_monitoringActive) {
    // Nothing monitored — no thresholds to breach. Hide the note and clear any
    // red target rows left from when dirs were still configured.
    el.hidden = true;
    ['target-row-headroom', 'target-row-redline', 'target-row-cap']
      .forEach(id => document.getElementById(id)?.classList.remove('is-breached'));
    return;
  }
  const d = _currentDeficits();
  [['headroom', 'target-row-headroom'], ['redline', 'target-row-redline'], ['cap', 'target-row-cap']]
    .forEach(([key, id]) => {
      document.getElementById(id)?.classList.toggle('is-breached', !!(d && d[key] > 0));
    });
  if (d && d.max > 0) {
    // A target past the safety percentage blocks Automatic Cleanup entirely — promising
    // a freed amount would be a lie. With a deletion delay, say when the
    // marked deletions actually land instead of implying they're imminent.
    let text;
    // The daily run fires at the first check at/after the configured run
    // time on an eligible day: tomorrow once today's window is used, later
    // today if the run time hasn't arrived, else the next 15-minute check.
    // A non-deleting mode keeps conditional wording — nothing fires on its own.
    const timing = _headroomWindowUsedToday
      ? `tomorrow at ${_runTimeLabel()}`
      : (_serverNowHHMM() < (_dailyRunTime || '00:00')
          ? `today at ${_runTimeLabel()}` : 'at the next check');
    const ev = _markedEvent || {};
    const evGb = prGbAmount((ev.bytes || 0) / 1e9);
    const evN = `${ev.count} movie${ev.count === 1 ? '' : 's'}`;
    // Being armed is not the same as being able to run. A media server that
    // stops answering leaves the mode on Automatic Cleanup while the server
    // sends next_run_time=null and the countdown right beside this note reads
    // "No run scheduled" — so the conditional wording the non-deleting modes get
    // is the honest one here too. Without it the same card said no run was
    // scheduled AND that hundreds of GB of films delete within ~15 minutes.
    const willRunItself = !_paused && !_schedOff && _cleanupOk && !!_nextRunTime;
    if (_safetyBlocked) {
      // Debug Cleanup ignores the safety percentage (it deletes nothing), so
      // don't tell the user a cleanup can't run while the yellow button is live.
      text = _debugMode
        ? 'Over space limits — a target is past the safety percentage (a real Cleanup is blocked), but Debug Cleanup ignores it and previews what would be removed.'
        : 'Over space limits — but a target is past the safety percentage, so Automatic Cleanup can\'t run.';
    } else if (_simulateRequired) {
      // No current plan (e.g. the cache was wiped on a code update, or settings
      // changed since the last Simulate). The deficit is known from live disk, but
      // NOT what a run would delete — ask for a Simulate instead of claiming a
      // freed amount the app can't know yet.
      text = d.redline > 0
        ? `Redline hit — ~${prGbAmount(d.redline)} GB below the floor. Run Simulate to build the deletion plan.`
        : `Over space limits — ~${prGbAmount(d.max)} GB over. Run Simulate to build the deletion plan.`;
    } else if (d.redline > 0) {
      // Redline frees only back to its floor (never up to the headroom target),
      // ignoring the daily window and the deletion delay.
      text = willRunItself
        ? `Redline hit — the next check (within ~15 min) frees ~${prGbAmount(d.redline)} GB back above the floor.`
        : `Redline hit — a run would free ~${prGbAmount(d.redline)} GB to clear the floor.`;
    } else if (!ev.count && _eligibleCount === 0) {
      // The plan is CURRENT (simulate_required is false, handled above) and it
      // still came out empty: every title is filtered out. Asking for another
      // Simulate — what this branch used to say — sends the one user who
      // cannot be helped by one to run it again, since the filters would
      // disqualify everything a second time too. Name the actual lever.
      text = `Over space limits — but nothing is eligible to delete. Check the filters in `
           + `Filtering & Scoring: a media type may be turned off, or a rule may be `
           + `disqualifying everything.`;
    } else if (!ev.count) {
      // Nothing marked, but things ARE eligible — Simulate writes the plan, and
      // deletion (automatic OR the manual button) stays ghosted over limits
      // until it exists.
      text = `Over space limits — run Simulate to mark the ~${prGbAmount(d.max)} GB deletion plan.`;
    } else if (!ev.on) {
      // The event batch is ripe now: the next deleting run removes exactly it.
      text = willRunItself
        ? `Over space limits — ${evN} (${evGb} GB) delete${ev.count === 1 ? 's' : ''} ${timing}.`
        : `Over space limits — a run would delete ${evN}, ${evGb} GB.`;
    } else {
      // Future-dated batch: eligibility starts that calendar day; an armed
      // scheduler deletes it at the daily run time.
      text = willRunItself
        ? `Over space limits — ${evN} (${evGb} GB) delete${ev.count === 1 ? 's' : ''} ${ev.on} at ${_runTimeLabel()}.`
        : `Over space limits — next deletion ${ev.on}: ${evN}, ${evGb} GB.`;
    }
    el.textContent = text;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}
// The bar geometry itself is prRenderDiskBar (base.html), shared with
// Configuration's Space Thresholds panel. What stays here is what only the
// Dashboard means: the last reading the Cleanup estimate reuses, the breach
// note, and the fact that this card draws the SAVED thresholds.
function _positionStorageBar(disk, libraryGb) {
  if (disk) _lastStorageSnapshot = { disk, libraryGb };
  _updateBreachNote();
  // Nothing monitored: drop the last reading too, so the Cleanup estimate can't
  // quote figures from before the directories were removed.
  if (!_monitoringActive) _lastStorageSnapshot = null;
  prRenderDiskBar(document.getElementById('storage-bar-root'), {
    disk, libraryGb,
    headroomGb: _storageHeadroomGb,
    redlineGb: _storageRedlineGb,
    capGb: _storageLibraryCapGb,
    monitoring: _monitoringActive,
  });
}

// The Storage card's ↻ button: runs a background Summary (debug_info) that
// refreshes disk + media library size without touching lastrun.log or the
// progress panel. Buttons are ghosted while it runs (and vice versa for real runs).
async function refreshStorageSummary() {
  if (_active || _summaryBusy) return;
  if (!_summaryOk) {
    showToast(_summaryDisabledMessage || 'Connect the selected media server first.', 'warning');
    return;
  }
  _summaryBusy = true;
  _applyButtonStates();
  try {
    const r = await _fetchJson('/api/summary/run', { method: 'POST', cache: 'no-store' }, 6000);
    if (!r || !r.ok) {
      showToast((r && r.message) || 'Could not start refresh.', 'warning');
      _summaryBusy = false;
      _applyButtonStates();
      return;
    }
  } catch (e) {
    showToast('Could not start refresh.', 'danger');
    _summaryBusy = false;
    _applyButtonStates();
    return;
  }
  _pollSummaryUntilDone();
}

async function _pollSummaryUntilDone() {
  if (_summaryPollActive) return;
  _summaryPollActive = true;
  let tries = 0;
  while (tries < 60) {
    tries++;
    await new Promise(res => setTimeout(res, 1000));
    try {
      const d = await _fetchJson(`/api/status?${prTimeQuery()}&_=${Date.now()}`, { cache: 'no-store' }, 5000);
      _updateStorageCard(d.disk, d.library_gb);
      if (!d.summary_active) break;
    } catch (_) { /* transient — keep polling */ }
  }
  _summaryBusy = false;
  _summaryPollActive = false;
  _applyButtonStates();
}

function _initDashboardTooltips() {
  document.querySelectorAll('[data-hover-tip]').forEach(el => {
    el.addEventListener('mousemove', ev => {
      const msg = el.getAttribute('data-hover-tip') || '';
      if (!msg) return;
      const tip = _dashboardTooltip();
      tip.textContent = msg;
      tip.style.display = 'block';
      const pad = 14;
      let left = ev.clientX + pad;
      let top = ev.clientY + pad;
      const rect = tip.getBoundingClientRect();
      if (left + rect.width > window.innerWidth - 8) left = Math.max(8, ev.clientX - rect.width - pad);
      if (top + rect.height > window.innerHeight - 8) top = Math.max(8, ev.clientY - rect.height - pad);
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    });
    el.addEventListener('mouseleave', () => {
      if (_dashboardTooltipEl) _dashboardTooltipEl.style.display = 'none';
    });
  });
}

function setRunning(on, live, debugLive) {
  _active = on;
  if (window.prSetHeaderRunning) window.prSetHeaderRunning(on, live, debugLive);
  // The last-run time's colon ticks while a run is in flight — the timestamp
  // beside it is the one this run is about to replace.
  document.getElementById('last-run-value')?.classList.toggle('is-running', !!on);
  const stopBtn = document.getElementById('btn-stop');
  if (stopBtn) stopBtn.title = on ? 'Stop the active run' : 'No active run to stop';
  _applyButtonStates();
  if (!on) _stopLogWatch();
  _updateCountdown();
}

async function runCleanup(mode, btnId) {
  if (_active || _runStartPending) return;
  if (mode === 'debug_cleanup') {
    // No-delete diagnostic run: nothing to confirm (it frees nothing), and it
    // uses the Simulate gate, so the button's own disabled state is the gate —
    // not _cleanupOk (which reflects the live/safety thresholds it deliberately skips).
    triggerRun('debug_cleanup');
    return;
  }
  if (!_cleanupOk) {
    showToast(_cleanupDisabledMessage, 'warning');
    return;
  }
  // Scale the question: the user should confirm a NUMBER, not a mystery.
  // Mirrors the engine's target math — max of the headroom deficit
  // (used-based), the redline deficit (restore free to the Headroom floor),
  // and the library-cap deficit.
  const deficit = _currentDeficits()?.max ?? null;
  const sized = deficit != null && deficit > 0;
  const answer = await prConfirm({
    title: 'Run a cleanup now?',
    body: [
      { text: sized
          ? `This deletes media files now — about ${prGbAmount(deficit)} GB, `
            + 'working up from the lowest-scoring title until your space limits are met.'
          : 'This deletes media files now, working up from the lowest-scoring title '
            + 'until your space limits are met.',
        warn: true },
      'There is no recycle bin — deleted files are gone.',
      // A manual Cleanup is the one path that ignores the delay, so warn that
      // marks the user still thinks are protected by their clock can go too.
      (_deleteDelayDays || 0) > 0
        ? 'The delay and daily schedule pace automatic runs only — a movie still '
          + 'inside its delay can go in this one.'
        : 'It deletes as soon as you confirm.',
    ],
    confirmText: sized ? `Delete ~${prGbAmount(deficit)} GB` : 'Run cleanup',
  });
  if (answer !== 'confirm') return;
  // The dialog can sit open for as long as the user takes to read it, and the
  // gates above were checked before it opened. The two-click confirm this
  // replaced re-ran them on the second click because it re-entered here;
  // re-run them for the same reason. (triggerRun re-checks the run state, but
  // _cleanupOk — the live/safety thresholds — is this button's own gate.)
  // Both branches have to SAY something: the user answered a dialog, so a
  // silent return reads as a dead button.
  if (_active || _runStartPending) {
    showToast('A run started while the confirmation was open. Try again once it finishes.', 'info');
    return;
  }
  if (!_cleanupOk) {
    showToast(_cleanupDisabledMessage, 'warning');
    return;
  }
  triggerRun(mode);
}

function _currentDeficits() {
  // What a Cleanup would try to free right now, per target and combined —
  // the same max() formula the engine uses to compute its target. null =
  // unknown (no disk reading yet); max 0 = limits satisfied.
  const snap = _lastStorageSnapshot;
  const dd = snap && snap.disk;
  if (!dd) return null;
  const total = Number(dd.total_gb), used = Number(dd.used_gb), free = Number(dd.free_gb);
  if (!Number.isFinite(total) || total <= 0) return null;
  const d = { headroom: 0, redline: 0, cap: 0 };
  const head = Number(_storageHeadroomGb);
  if (Number.isFinite(head) && head > 0 && Number.isFinite(used)) {
    d.headroom = Math.max(0, used - (total - head));
  }
  // A redline emergency frees only back to the redline floor (never up to the
  // headroom target), so its deficit is measured against the floor itself.
  const red = Number(_storageRedlineGb);
  if (Number.isFinite(red) && red > 0 && Number.isFinite(free) && free <= red) {
    d.redline = Math.max(0, red - free);
  }
  const cap = Number(_storageLibraryCapGb);
  const lib = Number(snap.libraryGb);
  if (Number.isFinite(cap) && cap > 0 && Number.isFinite(lib)) {
    d.cap = Math.max(0, lib - cap);
  }
  d.max = Math.max(d.headroom, d.redline, d.cap);
  return d;
}

async function refreshLastLog(lines = 500) {
  if (_logFetchInFlight) return;
  _logFetchInFlight = true;
  try {
    const d = await _fetchJson(`/api/logs/last?lines=${lines}&${prTimeQuery()}&_=${Date.now()}`, { cache: 'no-store' }, 10000);
    if (d.sections) { _logSections = d.sections; _applyLogJumpAffordance(); }
    // In section view the window holds a static slice — keep polling for
    // section availability above, but don't overwrite the displayed text.
    if (d.content !== undefined && _logViewMode === 'tail') _setTerm(d.content);
  } catch (_) {
    // Keep the current terminal content. A transient tab/network hiccup should
    // not blank the dashboard or mark the run as finished.
  } finally {
    _logFetchInFlight = false;
  }
}

const _MODE_LABELS = { off: 'Paused', paused: 'Monitor Only', headroom: 'Automatic Cleanup' };
// In Debug mode the button keeps its own dry-run tooltip (set server-side).
function _applyCleanupButtonTitle() {
  const btn = document.getElementById('btn-cleanup');
  if (!btn || _debugMode) return;
  btn.title = (_paused || _schedOff) && _cleanupOk
    ? 'One-time Cleanup: deletes to your limits now, ignoring the delay. Does not enable Automatic Cleanup.'
    : '';
}

function _applyRunMode(mode) {
  const nowPaused = mode === 'paused', nowOff = mode === 'off';
  if (nowPaused === _paused && nowOff === _schedOff) return;
  _paused = nowPaused;
  _schedOff = nowOff;
  const pill = document.querySelector('.dashboard-mode-link');
  if (pill) {
    pill.textContent = _MODE_LABELS[mode] || mode;
    pill.classList.toggle('is-danger', mode === 'headroom');
    pill.classList.toggle('is-neutral', mode !== 'headroom');
  }
  _applyCleanupButtonTitle();   // the tooltip only applies while it can't delete on its own
}

async function syncStatus({refreshLog = true} = {}) {
  if (_statusFetchInFlight) return;
  _statusFetchInFlight = true;
  try {
    const d = await _fetchJson(`/api/status?${prTimeQuery()}&_=${Date.now()}`, { cache: 'no-store' }, 5000);
    const wasActive = _active;
    _nextRunTime = d.next_run_time || null;
    _nextTickTime = d.next_tick_time || null;
    _nextTickIsDaily = !!d.next_tick_is_daily;
    if (d.run_mode) _applyRunMode(d.run_mode);
    if (d.thresholds_configured !== undefined) _monitoringActive = !!d.thresholds_configured;
    _autopauseReason = d.run_mode_autopause_reason || '';
    // A code change can wipe the store while this page is open, and a Simulate
    // can clear the note the same way — both land here, so the note follows the
    // poll rather than only the page load. Never re-shown once dismissed in this
    // tab: the dismissal removes the element, and there is nothing to put back.
    const _resetNote = document.getElementById('store-reset-note');
    if (_resetNote) _resetNote.hidden = !d.code_reset_pending;
    _restingReason = d.run_mode_resting_reason || '';
    _applyCleanupState(d.cleanup_state);
    if (!_summaryPollActive) _summaryBusy = !!d.summary_active;
    if (d.headroom_gb !== undefined) _storageHeadroomGb = d.headroom_gb;
    if (d.redline_gb !== undefined) _storageRedlineGb = d.redline_gb;
    if (d.library_cap_gb !== undefined) _storageLibraryCapGb = d.library_cap_gb;
    if (d.headroom_window_used_today !== undefined) _headroomWindowUsedToday = d.headroom_window_used_today;
    if (d.delete_delay_days !== undefined) _deleteDelayDays = d.delete_delay_days;
    if (d.daily_run_time !== undefined) _dailyRunTime = d.daily_run_time;
    if (d.marked_ripe_count !== undefined) _markedRipeCount = d.marked_ripe_count;
    if (d.marked_count !== undefined) _eligibleCount = d.marked_count;
    if (d.marked_event_count !== undefined) {
      _markedEvent = { on: d.marked_event_on, count: d.marked_event_count, bytes: d.marked_event_bytes };
    }
    _updateStorageCard(d.disk, d.library_gb);
    _renderTargetRows(d);
    _applyButtonStates();
    // Progress only changes while a run is active — skip the second fetch on
    // idle polls. One trailing fetch on the active→idle transition still lands
    // the final done/stopped state, and the first poll always primes the panel.
    if (d.run_active || wasActive || !_progressKnown) refreshProgress();
    _updateLastRunDisplay(d.last_run_ts, d.last_run);
    if (window.prSetConfigTabError) window.prSetConfigTabError(!!d.config_attention);
    _updateDeletedCounter(d.deleted_count || 0, d.deleted_reclaimed_bytes || 0, d.deleted_reclaimed_label || '',
                          d.marked_count !== undefined ? d.marked_count : null,
                          d.marked_imminent_count !== undefined ? d.marked_imminent_count : null);
    if (d.run_active !== _active) setRunning(d.run_active, d.run_active && d.run_cleanup, d.run_active && d.run_debug_cleanup);
    // Keep the badge's Running/Cleaning/Debugging label right even when the run
    // was already active on load (no transition to carry the flags through).
    else if (window.prSetHeaderRunning) window.prSetHeaderRunning(!!d.run_active, !!(d.run_active && d.run_cleanup), !!(d.run_active && d.run_debug_cleanup));
    if (d.run_active) {
      startStream(false);
      if (refreshLog) await refreshLastLog(_ACTIVE_LOG_LINES);
    } else if (wasActive && refreshLog) {
      await refreshLastLog(_FINAL_LOG_LINES);
    }
    _updateCountdown();
  } catch (_) {
    // Do not change button state on a failed status poll. The next poll or a
    // visibility refresh will reconcile the state.
  } finally {
    _statusFetchInFlight = false;
  }
}

function startStream(clear = true) {
  // The log view polls the file tail rather than streaming: reliable when
  // browsers throttle background tabs, and it caps terminal text so a long
  // run cannot freeze the page.
  if (clear) {
    _logViewMode = 'tail'; _logViewKind = null; _setLogViewUI();
    _termPinned = true; _setTerm('');
  }
  if (!_logPollTimer) {
    _logPollTimer = setInterval(() => {
      if (_active) { refreshLastLog(_ACTIVE_LOG_LINES); refreshProgress(); }
    }, _LOG_POLL_MS);
  }
  refreshLastLog(_active ? _ACTIVE_LOG_LINES : _FINAL_LOG_LINES);
}

async function triggerRun(mode) {
  if (_active || _runStartPending) return;
  if (mode === 'debug_sim' && !_simulateOk) {
    showToast(_simulateDisabledMessage, 'warning');
    return;
  }
  // Debug Cleanup shares the Cleanup button (btn-cleanup), so it ghosts and shows
  // "Starting…" then stays ghosted for the run, exactly like a real Cleanup.
  const pendingButtonId = mode === 'debug_sim' ? 'btn-sim'
    : ((mode === 'headroom' || mode === 'debug_cleanup') ? 'btn-cleanup' : null);
  _setRunStartPending(true, pendingButtonId);
  _setTerm('Starting run…');

  try {
    const r = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(mode ? { mode } : {}),
      cache: 'no-store',
    });
    const d = await r.json();
    if (d.ok && d.started === false) {
      // Nothing was over a limit, so no run was launched — revert the optimistic
      // running state and show the reason instead of streaming an empty run.
      _setRunStartPending(false, pendingButtonId);
      showToast(d.message, 'info');
      await refreshLastLog(_FINAL_LOG_LINES);
      return;
    }
    if (!d.ok) {
      _setRunStartPending(false, pendingButtonId);
      showToast(d.message, 'danger');
      await refreshLastLog(_FINAL_LOG_LINES);
      return;
    }
    _setRunStartPending(false, pendingButtonId);
    setRunning(true, mode === 'headroom', mode === 'debug_cleanup');
    startStream(true);
    _rpRunKey = null; _rpMaxIdx = 0;
    refreshProgress();
  } catch (err) {
    _setRunStartPending(false, pendingButtonId);
    showToast(`Run request failed: ${err}`, 'danger');
    await refreshLastLog(_FINAL_LOG_LINES);
  }
}

let _stopPending = false;
async function stopRun() {
  // Guard the in-flight window: a double-click sent a second stop that
  // answered with a confusing "No active run." toast right after stopping.
  if (_stopPending) return;
  _stopPending = true;
  const stopBtn = document.getElementById('btn-stop');
  if (stopBtn) { stopBtn.disabled = true; stopBtn.classList.add('btn-busy'); }
  try {
    const r = await fetch('/api/run/stop', { method: 'POST', cache: 'no-store' });
    const d = await r.json();
    showToast(d.message, d.ok ? 'success' : 'info');
    if (d.ok) {
      setRunning(false);
      // The stop handler already wrote the terminal "stopped" frame (with the
      // interrupted phase preserved) before responding, so fetch it NOW to draw
      // the failed × immediately. Without this the stepper stays frozen on the
      // last mid-run frame: setRunning(false) clears _active, which then makes
      // syncStatus's trailing-fetch guard (run_active || wasActive) skip the
      // progress refresh — the terminal frame only landed on a later
      // visibility/pageshow refresh.
      refreshProgress();
      await refreshLastLog(_FINAL_LOG_LINES);
    }
  } catch (err) {
    showToast(`Stop request failed: ${err}`, 'danger');
  } finally {
    _stopPending = false;
    if (stopBtn) stopBtn.classList.remove('btn-busy');
    _applyButtonStates();   // owns stopBtn.disabled — recompute from _active
  }
}

_formatInitialTimestamps();
_initDashboardTooltips();
// Draw the storage bar's library segment and threshold markers from the
// server-rendered values so they show before the first status poll lands.
_positionStorageBar(_bootDisk, _bootLibraryGb);
refreshProgress();
document.getElementById('log-window-header')?.addEventListener('pointerdown', _logDragStart);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !document.getElementById('log-window')?.hidden) closeLogWindow();
});
if (_active) {
  setRunning(true, _bootRunCleanup, _bootRunDebugCleanup);
  startStream(false);
}
_applyCleanupButtonTitle();

// Countdown updater (every second)
function _updateCountdown() {
  const el = document.getElementById('next-run-sub');
  if (!el) return;
  // Red countdown: Automatic Cleanup is armed, a run is actually scheduled, and the NEXT
  // tick will genuinely DELETE — redline fires on any tick, while headroom
  // and library-cap breaches wait out the shared once-per-day window AND the
  // configured daily run time. With a deletion delay set, a daily run that
  // would only MARK stays un-red: red means files are removed, so it needs
  // ripe (aged-past-the-delay) marks.
  const d = _currentDeficits();
  const delayGate = (_deleteDelayDays || 0) <= 0 || (_markedRipeCount || 0) > 0;
  const nextTickPrunes = !!d && (d.redline > 0
    || ((d.headroom > 0 || d.cap > 0) && !_headroomWindowUsedToday && delayGate
        && _nextTickAfterRunTime()));
  const willPrune = !_active && !_paused && !_schedOff && !!_nextRunTime && nextTickPrunes;
  el.classList.toggle('will-prune', willPrune);
  if (_active) { el.textContent = 'Run in progress'; el.title = ''; return; }
  if (!_monitoringActive) {
    // Nothing monitored → the scheduler genuinely does nothing (_scheduled_tick
    // no-ops), so say so plainly rather than counting down to a tick that idles.
    el.textContent = 'Scheduler paused';
    el.title = 'No monitored library paths — add one in Configuration to start monitoring.';
    return;
  }
  if (_schedOff) {
    // Paused still refreshes storage each tick, but that timer stays
    // unsurfaced — the line just says paused. Two very different states render
    // the same way, so say WHICH: the user chose this, or the scheduler is
    // resting until a scan lands (and telling them to "run Simulate" when the
    // last one failed is how this reads as broken).
    el.textContent = _restingReason ? 'Paused — waiting for a scan' : 'Scheduler paused';
    el.title = _restingReason
      ? _restingReason + ' Storage numbers still refresh, and Dashboard runs still work.'
      : 'Paused: no scans, cleanups, or notifications. Storage numbers still refresh, and Dashboard runs still work.';
    return;
  }
  if (_paused) {
    // Monitor Only isn't idle: a ~15-minute refresh keeps the queue and disk
    // numbers current and a daily Simulate keeps the plan fresh — it just
    // never deletes. Surfaced as a neutral countdown (never red).
    el.title = _autopauseReason
      ? ('Automatic Cleanup was switched to Monitor Only: ' + _autopauseReason)
      : 'Monitor Only: a ~15-minute refresh and a daily scan keep the plan current. Never deletes on its own.';
    if (_nextTickTime) {
      const diff = Math.max(0, new Date(_nextTickTime).getTime() - Date.now());
      const m = Math.floor(diff / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      // "refresh" for the light 15-minute upkeep tick; "Daily scan" when the
      // countdown is genuinely landing on the daily slot (the run time falls
      // inside this interval) — the label flips live with the run-time config.
      const _evt = _nextTickIsDaily ? 'Daily scan' : 'Next refresh';
      el.textContent = diff === 0 ? (_nextTickIsDaily ? 'Scanning now…' : 'Refreshing now…')
        : `${_evt} in ${m}:${s.toString().padStart(2, '0')}`;
    } else {
      // No next tick, having already ruled out every standing reason for one
      // above: nothing monitored and a fully-paused scheduler both returned, so
      // the only thing left holding the clock is a run or the Summary beside it.
      // That is the same half-second Automatic Cleanup gets on a Simulate, and
      // it deserves the same answer. Describing the mode here instead would be
      // true, but reads as the final word on a countdown about to come back.
      el.textContent = 'Next run: calculating…';
    }
    return;
  }
  el.title = willPrune ? 'Over space limits — this run will prune.' : '';
  if (!_nextRunTime) {
    // Four different things null next_run_time: a run, a background Summary, a
    // down connection, unusable thresholds. Only the last two are a real state
    // to report — the first two are the clock being held for a moment and
    // resolve on their own. Keying this off the Cleanup button's state named
    // Connections and Space Thresholds for all four, so starting a Simulate in
    // Automatic Cleanup flashed "fix Connections" at a setup with nothing wrong
    // with it: the Summary that runs alongside takes the clock for about half a
    // second, and the button is disabled for that whole time.
    el.textContent = _cleanupGateBlocked
      ? 'No run scheduled — fix Connections / Space Thresholds first'
      : 'Next run: calculating…';
    return;
  }
  const diff = Math.max(0, new Date(_nextRunTime).getTime() - Date.now());
  if (diff === 0) { el.textContent = 'Starting soon…'; return; }
  const m = Math.floor(diff / 60000);
  const s = Math.floor((diff % 60000) / 1000);
  // Name the event, the way Monitor Only does — the countdown lands on either
  // the daily slot or a routine 15-minute refresh, and the server already says
  // which (next_tick_is_daily). "and cleanup" is added only when this run will
  // genuinely delete, which is the same signal that turns the countdown red, so
  // the wording and the color can never disagree: a daily scan on a day that
  // is within limits deletes nothing and shouldn't claim otherwise.
  const _evt = _nextTickIsDaily ? (willPrune ? 'Daily scan and cleanup' : 'Daily scan')
                                : 'Next refresh';
  el.textContent = `${_evt} in ${m}:${s.toString().padStart(2, '0')}`;
}
setInterval(_updateCountdown, 1000);
_updateCountdown();

// Status polling: keep UI state in sync even if a run started from the scheduler
// or the browser suspended the tab and resumed later.
_statusPollTimer = setInterval(() => syncStatus({refreshLog: _active}), 3000);
syncStatus({refreshLog: _active});

if (!_active) {
  refreshLastLog(200);
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    syncStatus({refreshLog: true});
  }
});
window.addEventListener('pageshow', () => { syncStatus({refreshLog: true}); });
