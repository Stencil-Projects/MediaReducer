// A SAVED Debug mode ghosts the Scheduler Mode → Automatic Cleanup option,
// with a visible reason — the same .run-mode-disabled card styling and
// desc-headroom text the other gates use, not a raw `disabled` toggle that
// leaves the card unstyled and gives no reason.
//
// Saved, not ticked: no control on this page moves on an unsaved edit, so the
// checkbox states an intention and the save is what makes it real (and the
// save refuses Debug mode together with Automatic Cleanup outright). The two
// halves are checked separately below — the checkbox alone must do nothing,
// and the saved value must ghost.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

await p.goto('' + BASE + '/config', { waitUntil: 'networkidle' });
await p.waitForTimeout(600);

const snap = () => p.evaluate(() => {
  const card = document.getElementById('mode-card-headroom');
  const live = document.getElementById('mode-headroom');
  const desc = document.getElementById('desc-headroom');
  return {
    ghosted: !!card?.classList.contains('run-mode-disabled'),
    cleanupDisabled: !!live?.disabled,
    reason: (desc?.textContent || '').trim(),
    enabledText: desc?.dataset.enabledText || '',
  };
});

const before = await snap();

// Ticking the box without saving must change nothing at all.
await p.evaluate(() => {
  const dbg = document.getElementById('DEBUG_MODE');
  dbg.checked = true;
  dbg.dispatchEvent(new Event('change', { bubbles: true }));
});
await p.waitForTimeout(150);
const ticked = await snap();

// A SAVED Debug mode — the same state a page load finds in config.json.
await p.evaluate(() => {
  _savedConfig = Object.assign({}, _savedConfig, { DEBUG_MODE: true });
  _updateRunModeAvailability();
});
await p.waitForTimeout(150);
const on = await snap();

// Back off — Automatic Cleanup returns to its normal (server-gated) availability.
await p.evaluate(() => {
  const dbg = document.getElementById('DEBUG_MODE');
  dbg.checked = false;
  dbg.dispatchEvent(new Event('change', { bubbles: true }));
  _savedConfig = Object.assign({}, _savedConfig, { DEBUG_MODE: false });
  _updateRunModeAvailability();
});
await p.waitForTimeout(150);
const off = await snap();

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };

check('ticking Debug mode without saving changes nothing',
  JSON.stringify(ticked) === JSON.stringify(before));
check('with Debug mode SAVED, Automatic Cleanup is ghosted (card styled + input disabled)',
  on.ghosted && on.cleanupDisabled);
check('the ghost shows a reason naming Debug mode',
  /debug mode/i.test(on.reason) && on.reason !== on.enabledText);
// Turning Debug off must restore the exact pre-debug state — whatever it was.
// (This fixture already ghosts Automatic Cleanup for a real headroom-safety reason, so we
// compare to `before` rather than assume it becomes enabled.)
check('turning Debug mode off restores the pre-debug Automatic Cleanup state (no debug reason left over)',
  off.ghosted === before.ghosted && off.cleanupDisabled === before.cleanupDisabled
  && off.reason === before.reason && !/debug mode/i.test(off.reason));
check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0, 3)));
console.log('before:', JSON.stringify(before));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
