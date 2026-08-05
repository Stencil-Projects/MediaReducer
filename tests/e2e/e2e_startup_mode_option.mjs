// "Set to Monitor Only at startup" — a setting that lives inside a mode card.
//
// The card is click-to-select: clicking anywhere in it picks that mode. This
// checkbox sits at the bottom of the Automatic Cleanup card, so without an
// exemption, ticking it would ALSO arm the mode that deletes files — a deletion
// mode switched on by a click aimed at something else. That is the case worth a
// browser test; the rest is form plumbing.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage({ viewport: { width: 1180, height: 900 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));
let ok = true;
const check = (name, cond, extra = '') => {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra)));
  ok = ok && cond;
};

await p.goto(BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForFunction(() => typeof getFormConfig === 'function', { timeout: 15000 });

const R = await p.evaluate(() => {
  const box = document.getElementById('PAUSE_CLEANUP_ON_STARTUP');
  const radio = document.getElementById('mode-headroom');
  const card = document.getElementById('mode-card-headroom');
  if (!box || !radio || !card) return { missing: true };
  const out = { inCard: card.contains(box), shipsOn: box.checked };

  // Not inside the mode's <label>: a click there toggles the radio natively,
  // before any handler gets a say.
  out.outsideLabel = !box.closest('label[for="mode-headroom"]');

  // The real hazard: the card's click-to-select must skip this control.
  // Availability is held aside for these two clicks — this install's Automatic
  // Cleanup is blocked on its thresholds, and every change event re-runs the
  // check and snaps the selection back off it. That is correct behaviour and
  // covered elsewhere; here it would answer for the exemption being tested.
  const realAvail = _updateRunModeAvailability;
  _updateRunModeAvailability = () => {};
  radio.checked = false;
  card.classList.remove('run-mode-disabled');
  card.setAttribute('aria-disabled', 'false');
  radio.disabled = false;
  box.disabled = false;
  box.click();
  out.tickedIt = box.checked === false;         // it was on; the click turned it off
  out.didNotArm = radio.checked === false;      // ...and did NOT select the deleting mode

  // Clicking the card ANYWHERE else still selects the mode, so the exemption is
  // narrow rather than having broken the card.
  document.getElementById('desc-headroom').click();
  out.cardStillSelects = radio.checked === true;
  _updateRunModeAvailability = realAvail;

  // What a save would post follows the box, both ways.
  box.checked = false;
  out.postsOff = getFormConfig().PAUSE_CLEANUP_ON_STARTUP === false;
  box.checked = true;
  out.postsOn = getFormConfig().PAUSE_CLEANUP_ON_STARTUP === true;

  // A mode nobody can pick has no settings to offer either.
  _setRunModeDisabled('headroom', true);
  out.disabledWithMode = box.disabled === true;
  _setRunModeDisabled('headroom', false);
  out.enabledWithMode = box.disabled === false;
  return out;
});

check('the option renders inside the Automatic Cleanup card', R.missing !== true && R.inCard === true, R);
if (!R.missing) {
  check('it ships checked', R.shipsOn === true, R);
  check('it sits outside the mode label, so a click cannot toggle the radio natively',
        R.outsideLabel === true, R);
  check('clicking it toggles the box', R.tickedIt === true, R);
  check('...without arming Automatic Cleanup', R.didNotArm === true, R);
  check('the rest of the card still selects the mode', R.cardStillSelects === true, R);
  check('a save posts what the box says', R.postsOff === true && R.postsOn === true, R);
  check('a mode that cannot be picked disables its own setting', R.disabledWithMode === true, R);
  check('...and re-enables with it', R.enabledWithMode === true, R);
}
check('no JS errors', errs.length === 0, errs.slice(0, 3));

await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
