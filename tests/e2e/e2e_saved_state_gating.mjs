// Nothing on the Configuration page unlocks or ungreys on an unsaved edit.
//
// Every control state here is a server verdict about the config ON DISK,
// delivered by the render, the status poll and the save response. The page
// keeps no rules of its own, so typing cannot move a lock, a ghost or a
// reason — a save can, and only a save.
//
// Two failures this pins, both of which happened:
//   • the page mirrored the server's arming rules in JavaScript, the mirror
//     went out of date, and the first status poll re-enabled a radio the save
//     would refuse. There is no mirror left to drift.
//   • the page gated on the FORM, so it described a configuration that was
//     not running — an unsaved threshold ungreyed a mode the scheduler could
//     not actually be put into.
//
// The exception, checked last: the informational panel is not a gate, and it
// DOES follow the form. Watching the numbers move as you type is the point of
// it, and it unlocks nothing.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
let ok = true;
const errs = [];
p.on('pageerror', e => errs.push(e.message));

const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${cond ? '' : ' — ' + extra}`);
  ok = ok && cond;
};

await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });
for (let i = 0; i < 20; i++) {
  if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
  await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
}

// Every gate on the page, in one snapshot.
const gates = () => p.evaluate(() => {
  const dis = id => !!document.getElementById(id)?.disabled;
  const txt = id => document.getElementById(id)?.textContent?.trim() || '';
  return {
    modeHeadroom: dis('mode-headroom'), modeHeadroomWhy: txt('desc-headroom'),
    modePaused: dis('mode-paused'), modePausedWhy: txt('desc-paused'),
    cardHeadroom: !!document.getElementById('mode-card-headroom')?.classList.contains('run-mode-disabled'),
    cardPaused: !!document.getElementById('mode-card-paused')?.classList.contains('run-mode-disabled'),
    notifyTest: dis('btn-notify-test'), notifyTestWhy: document.getElementById('btn-notify-test')?.title || '',
    notifyPreview: dis('btn-notify-preview'),
    spaceLocked: !!document.getElementById('space-thresholds-section')?.classList.contains('space-thresholds-locked'),
  };
});

const before = await gates();

// ── Edit everything a gate ever keyed off, and save none of it ──────────────
// Thresholds, both cleanup-relevant checkboxes, Debug mode, the server
// selection, and a notification destination: between them these used to drive
// the Automatic Cleanup radio, Monitor Only, the Space Thresholds section lock
// and both notification buttons.
await p.evaluate(() => {
  const set = (id, v) => { const e = document.getElementById(id); if (e) { e.value = String(v); } };
  const tick = (id, v) => { const e = document.getElementById(id); if (e) { e.checked = v; } };
  tick('headroom-enabled', true); set('HEADROOM_GB', 999999);   // over any safety cap
  tick('redline-enabled', true); set('REDLINE_GB', 0);          // invalid
  tick('cap-enabled', true); set('MAX_LIBRARY_GB', '');         // invalid
  set('MAX_HEADROOM_PCT', 0);                                   // invalid
  tick('DEBUG_MODE', true);
  tick('USE_PLEX', false); tick('USE_JELLYFIN', false);          // no server selected
  set('NOTIFY_NTFY_TOPIC', 'a-real-destination');
  // Everything the page runs when it re-evaluates itself.
  document.getElementById('cfg-form')?.dispatchEvent(new Event('input', { bubbles: true }));
  _updateRunModeAvailability();
  _applyNotifyButtonState();
  _applySpaceThresholdLock?.();
});
const afterTyping = await gates();
check('typing changes no gate on the page',
      JSON.stringify(afterTyping) === JSON.stringify(before),
      Object.keys(before).filter(k => before[k] !== afterTyping[k])
        .map(k => `${k}: ${before[k]} -> ${afterTyping[k]}`).join('; '));

// ...and a status poll landing on top of the edits does not either: the poll
// is where the page used to re-derive itself from the form.
await p.evaluate(async () => {
  const d = await (await fetch('/api/status?_=' + Date.now(), { cache: 'no-store' })).json();
  window.prOnStatusPoll(d);
});
check('...and neither does a status poll arriving on top of them',
      JSON.stringify(await gates()) === JSON.stringify(before),
      JSON.stringify(await gates()));

// ── A save's verdicts DO move them ─────────────────────────────────────────
// Or the check above would be satisfied by a page whose gates are simply
// frozen, which is a different bug with the same result. This delivers what a
// save response delivers.
const moved = await p.evaluate(() => {
  _monitorLockReason = '';
  _notifyTestReason = '';
  _notifyPreviewReason = '';
  _serverThresholds = { ok_for_cleanup: true, simulate_required: false,
                        simulate_required_message: '', cleanup_tooltip: '',
                        safety_blocked: false };
  _savedConfig = { ..._savedConfig, USE_JELLYFIN: true, DEBUG_MODE: false, RUN_MODE: 'off' };
  _requiredConnectionOk = true;
  _selectedMediaApisReady = () => true;
  _updateRunModeAvailability();
  _applyNotifyButtonState();
  return { headroom: !!document.getElementById('mode-headroom')?.disabled,
           monitor: !!document.getElementById('mode-paused')?.disabled,
           test: !!document.getElementById('btn-notify-test')?.disabled,
           preview: !!document.getElementById('btn-notify-preview')?.disabled };
});
check('server verdicts saying "all clear" ungrey every control',
      !moved.headroom && !moved.monitor && !moved.test && !moved.preview,
      JSON.stringify(moved));

// ...and back the other way, each on its own reason.
const reghosted = await p.evaluate(() => {
  _serverThresholds = { ok_for_cleanup: false, cleanup_tooltip: 'Fix the thresholds.',
                        simulate_required: false, safety_blocked: false };
  _monitorLockReason = 'Run Simulate first.';
  _notifyTestReason = 'Add a destination.';
  _notifyPreviewReason = 'Nothing to preview.';
  _updateRunModeAvailability();
  _applyNotifyButtonState();
  return { headroom: !!document.getElementById('mode-headroom')?.disabled,
           headroomWhy: document.getElementById('desc-headroom')?.textContent?.trim(),
           monitor: !!document.getElementById('mode-paused')?.disabled,
           monitorWhy: document.getElementById('desc-paused')?.textContent?.trim(),
           test: !!document.getElementById('btn-notify-test')?.disabled,
           preview: !!document.getElementById('btn-notify-preview')?.disabled };
});
check('...and verdicts saying otherwise re-ghost them',
      reghosted.headroom && reghosted.monitor && reghosted.test && reghosted.preview,
      JSON.stringify(reghosted));
check('...each wearing the server\'s own words',
      reghosted.headroomWhy === 'Fix the thresholds.'
      && reghosted.monitorWhy === 'Run Simulate first.',
      `${reghosted.headroomWhy} | ${reghosted.monitorWhy}`);

// ── The exception: the calculator still follows the form ───────────────────
// Informational, not a gate. It is what makes an unsaved threshold worth
// typing — you can see where it would put you before committing to it.
const panel = await p.evaluate(() => {
  const read = () => document.getElementById('ts-bar-root')?.innerHTML || '';
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = String(v); };
  const tick = (id, v) => { const e = document.getElementById(id); if (e) e.checked = v; };
  _configMonitoringActive = true;
  tick('headroom-enabled', true); set('HEADROOM_GB', 100);
  _updateThresholdStatus();
  const a = read();
  set('HEADROOM_GB', 400);
  _updateThresholdStatus();
  return { changed: a !== read(), nonEmpty: read().length > 0 };
});
check('the threshold calculator still redraws as you type', panel.changed && panel.nonEmpty,
      JSON.stringify(panel));

check('no page errors', errs.length === 0, errs.join(' | '));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
