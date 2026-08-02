// The Configuration page must not post a Scheduler Mode the server has left
// behind.
//
// Automatic Cleanup can be turned off without this page: a scheduled tick that
// finds Space Thresholds unsafe pauses it by itself ("Re-arming takes the
// two-click confirm"), and a second tab or the CLI can set the mode too. The
// page froze RUN_MODE at render, so an open tab kept showing the armed radio
// and the next unrelated save — a notification toggle, an API key — silently
// re-armed automatic file deletion. The arming confirm that exists to catch
// exactly that compares against the same frozen value, so it never fired.
//
// Driven in the real page realm: the status poll's payload goes to the page's
// own handler. _updateRunModeAvailability is held aside while the sync is
// measured — it snaps the radio off any mode this install cannot select yet,
// which would answer for the sync and hide whether it ran at all. The last case
// puts it back and pins that it still has the final say.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
let ok = true;
const errs = [];
p.on('pageerror', e => errs.push(e.message));
await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });

const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${cond ? '' : ' — ' + extra}`);
  ok = ok && cond;
};

await p.evaluate(() => {
  window.__realAvail = _updateRunModeAvailability;
  _updateRunModeAvailability = () => {};
});

// Put the page in the state an open tab is in (showing `shows`), then deliver a
// poll saying the server is on `polled`. Returns what a save would post and what
// the page now believes it last saved.
const afterPoll = (shows, polled, pick = null) => p.evaluate(([show, mode, click]) => {
  _runModeTouched = false;
  _savedConfig.RUN_MODE = show;
  document.querySelector(`input[name="RUN_MODE"][value="${show}"]`).checked = true;
  if (click) {
    // A deliberate choice, through the same event the mode cards dispatch.
    const el = document.querySelector(`input[name="RUN_MODE"][value="${click}"]`);
    el.checked = true;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  window.prOnStatusPoll({ run_mode: mode, run_active: false, summary_active: false });
  return {
    posted: document.querySelector('input[name="RUN_MODE"]:checked')?.value,
    saved: _savedConfig.RUN_MODE,
  };
}, [shows, polled, pick]);

let r = await afterPoll('headroom', 'paused');
check('a tick that paused Automatic Cleanup moves the untouched page with it',
      r.posted === 'paused', `would post ${r.posted}`);
check('...and the arming confirm now compares against the live mode',
      r.saved === 'paused', r.saved);

r = await afterPoll('paused', 'paused');
check('an unchanged mode leaves the page alone',
      r.posted === 'paused' && r.saved === 'paused', `${r.posted} / ${r.saved}`);

// A deliberate choice on this page outranks the poll — but the confirm still
// has to know what it is arming FROM, or it stays silent about the change.
r = await afterPoll('paused', 'off', 'headroom');
check('a deliberate pick is not overruled by the poll', r.posted === 'headroom', r.posted);
check('...and arming is still measured from the live mode, so the confirm fires',
      r.saved === 'off', r.saved);

// Availability still wins: a mode this install cannot select is not something a
// status poll may leave ticked.
const snapped = await p.evaluate(() => {
  _updateRunModeAvailability = window.__realAvail;
  _runModeTouched = false;
  _savedConfig.RUN_MODE = 'paused';
  window.prOnStatusPoll({ run_mode: 'headroom', run_active: false, summary_active: false });
  const el = document.getElementById('mode-headroom');
  return { blocked: !!el?.disabled, checked: !!el?.checked };
});
check('a blocked Automatic Cleanup is not left ticked by the poll',
      !snapped.blocked || !snapped.checked, JSON.stringify(snapped));

check('no JS errors', errs.length === 0, errs.join(' | '));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
