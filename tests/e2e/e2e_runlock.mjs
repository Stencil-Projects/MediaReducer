const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

// Proxy /api/status so we can flip run_active without a real run.
let fakeRunActive = false;
await p.route('**/api/status**', async route => {
  const resp = await route.fetch();
  const d = await resp.json();
  d.run_active = fakeRunActive;
  await route.fulfill({ response: resp, json: d });
});

await p.goto('' + BASE + '/explorer', { waitUntil: 'networkidle' });
await p.waitForTimeout(800);

const snap = () => p.evaluate(() => ({
  // Settle any running animation first. #main carries a page-entry animation
  // whose from-state is opacity 0, and it is an ancestor of the note, so the
  // dimmed-walk below reports a decorative fade as "the run ghost dimmed it".
  // Measured: #main reads 0, then 0.19, then 0.92 while that animation runs.
  // Infinite ones (the header badge pulse) cannot be finished and are skipped.
  _settled: document.getAnimations().forEach(a => { try { a.finish(); } catch {} }),
  ghost: document.getElementById('filter-score-card')?.classList.contains('section-run-ghost'),
  noteHidden: document.getElementById('exp-run-lock-note')?.hidden,
  // The notice sits OUTSIDE the card it explains: inside, the run ghost dims
  // the sentence along with the settings it is telling you about.
  noteInCard: !!document.getElementById('filter-score-card')
                  ?.contains(document.getElementById('exp-run-lock-note')),
  noteDimmed: (() => {
    let n = document.getElementById('exp-run-lock-note');
    while (n && n !== document.body) {
      if (parseFloat(getComputedStyle(n).opacity) < 1) return true;
      n = n.parentElement;
    }
    return false;
  })(),
  balDisabled: document.getElementById('c-bal')?.disabled,
  graceDisabled: document.getElementById('c-grace')?.disabled,
  cutoffDisabled: document.getElementById('c-cutoff')?.disabled,
  cutoffOn: document.getElementById('c-cutoff-on')?.checked,
  saveDisabled: document.getElementById('btn-cfg-save')?.disabled,
}));

const before = await snap();

// Flip to run-active and wait for the 4s status poll to deliver it.
fakeRunActive = true;
await p.waitForFunction(() => document.getElementById('filter-score-card')?.classList.contains('section-run-ghost'), { timeout: 12000 });
const locked = await snap();

// Make the form dirty attempt: move dial while locked (disabled input ignores events)
const balBefore = await p.$eval('#c-bal', el => el.value);

// Flip back and wait for unlock.
fakeRunActive = false;
await p.waitForFunction(() => !document.getElementById('filter-score-card')?.classList.contains('section-run-ghost'), { timeout: 12000 });
const after = await snap();

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };
// Save is disabled on a CLEAN form by design — only inputs/ghost/note matter here.
check('starts unlocked (no ghost, note hidden, inputs enabled)',
  !before.ghost && before.noteHidden && !before.balDisabled && !before.graceDisabled);
check('locks on run: ghost + note shown', locked.ghost && !locked.noteHidden);
check('the run notice sits above the ghosted card, and stays readable',
  !locked.noteInCard && !locked.noteDimmed);
check('locks inputs (bal, grace)', locked.balDisabled && locked.graceDisabled);
check('locks Save', locked.saveDisabled);
check('unlocks after run: ghost off, note hidden', !after.ghost && after.noteHidden);
check('inputs re-enabled', !after.balDisabled && !after.graceDisabled);
check('cutoff input follows its toggle after unlock', after.cutoffDisabled === !after.cutoffOn);

// ── Configuration: EVERY section, and the state survives the round trip ─────
// The lock used to be a hand-picked set of controls, so whole groups in
// Advanced stayed editable mid-run. It is a sweep now, which raises the
// opposite risk: releasing it must not blanket-enable controls that were off
// for their own reason (a disconnected server's fields, a dependent input
// whose toggle is unticked). So the assertion is the round trip — every
// control's disabled state must come back exactly as it started.
const c = await b.newPage();
c.on('pageerror', e => errs.push('config: ' + e.message));
await c.route('**/api/status**', async route => {
  const resp = await route.fetch();
  const d = await resp.json();
  d.run_active = fakeRunActive;
  await route.fulfill({ response: resp, json: d });
});
await c.goto('' + BASE + '/config', { waitUntil: 'domcontentloaded' });
await c.waitForFunction(() => typeof _applyConfigRuntimeLocks === 'function', { timeout: 20000 });
await c.waitForTimeout(1500);
await c.evaluate(() => [...document.querySelectorAll('.modal.show')]
  .forEach(m => bootstrap.Modal.getOrCreateInstance(m).hide()));

// id -> disabled, for every control in every section.
const cfgSnap = () => c.evaluate(() => {
  const out = {};
  document.querySelectorAll('#cfg-accordion .accordion-collapse')
    .forEach((sec, si) => [...sec.querySelectorAll('input, select, textarea, button')]
      .forEach((el, i) => { out[el.id || `${sec.id}#${si}.${i}`] = el.disabled; }));
  return out;
});

const cfgIdle = await cfgSnap();

// A deselected server's URL/API fields DIM but stay editable, so a key can be
// pasted in before the server is enabled and is there waiting. They used to be
// pointer-events:none with tabindex -1, which made them unreachable by mouse
// AND keyboard — the value was saved but there was no way to enter one.
await c.evaluate(() => { document.getElementById('manual-overrides').open = true;
                         const jf = document.getElementById('USE_JELLYFIN');
                         if (jf.checked) jf.click(); });
await c.waitForTimeout(400);
const dimmed = await c.evaluate(() => {
  const w = document.getElementById('override-field-JELLYFIN_API_KEY');
  const i = document.getElementById('JELLYFIN_API_KEY');
  return { ghosted: w.classList.contains('conn-field-ghost'),
           disabled: i.disabled,
           reachable: getComputedStyle(w).pointerEvents !== 'none' && i.tabIndex >= 0,
           dim: parseFloat(getComputedStyle(w).opacity) < 1 };
});

fakeRunActive = true;
await c.waitForFunction(() => _configActivityLocked() === true, null, { timeout: 20000 });
await c.waitForTimeout(600);
const cfgLocked = await cfgSnap();
// Both of the following are properties of the LOCKED state, so they have to be
// read here rather than after the unlock below.
//
// One notice, not one per category: five copies of "A run is active" buried the
// section-specific reasons that DO differ from each other.
const runNotices = await c.evaluate(() => [...document.querySelectorAll('.config-inline-warning')]
  .filter(e => !e.hidden && /run is active/i.test(e.textContent))
  .map(e => e.id || '(inline)'));
// Force stop has to stay READABLE, not just clickable. It sits in the sidebar
// under Reset MediaReducer — four wrappers deep inside the section the run
// ghosts — and CSS opacity multiplies down and can't be undone from inside. So
// the assertion walks its whole ancestor chain, and pins that its immediate
// SIBLING is dimmed: that pair is what proves the ghost is skipping the
// passthrough chain rather than just not being applied at all.
const exempt = await c.evaluate(() => {
  const box = document.querySelector('[data-run-lock-exempt]');
  if (!box) return null;
  let node = box, dimmed = 0;
  while (node && node !== document.body) {
    if (parseFloat(getComputedStyle(node).opacity) < 1) dimmed++;
    node = node.parentElement;
  }
  const resetCard = document.getElementById('btn-reset-all').closest('.config-card');
  return { dimmed, siblingOpacity: getComputedStyle(resetCard).opacity,
           sameParentAsReset: resetCard.parentElement === box.parentElement,
           isLastInSidebar: box.parentElement.lastElementChild === box,
           afterReset: !!(document.getElementById('btn-reset-all')
             .compareDocumentPosition(document.getElementById('btn-force-stop'))
             & Node.DOCUMENT_POSITION_FOLLOWING) };
});
const dimmedDuringRun = await c.evaluate(
  () => document.getElementById('JELLYFIN_API_KEY').disabled);
const cfgGhosted = await c.evaluate(() => [...document.querySelectorAll('#cfg-accordion .accordion-collapse')]
  .every(s => s.classList.contains('section-run-ghost')));
const stillLive = Object.entries(cfgLocked).filter(([, off]) => !off).map(([id]) => id);

fakeRunActive = false;
await c.waitForFunction(() => _configActivityLocked() === false, null, { timeout: 20000 });
await c.waitForTimeout(600);
const cfgAfter = await cfgSnap();
const changed = Object.keys(cfgIdle).filter(id => cfgIdle[id] !== cfgAfter[id]);

check('a run is reported ONCE on the page, not in every category',
  runNotices.length === 1 && runNotices[0] === 'config-run-lock-note');
if (runNotices.length !== 1) console.log('   notices:', runNotices.join(', ') || '(none)');
check('the force-stop box is not dimmed by the run ghost, ancestors included',
  exempt && exempt.dimmed === 0);
check('...while its own sibling card IS dimmed, so the ghost is still applied',
  exempt && exempt.siblingOpacity === '0.55');
check('...and sits directly below Reset MediaReducer in the sidebar',
  exempt && exempt.afterReset && exempt.sameParentAsReset && exempt.isLastInSidebar);
check('a deselected server dims its URL/API fields but leaves them editable',
  dimmed && dimmed.ghosted && dimmed.dim && !dimmed.disabled && dimmed.reachable);
check('...and a run still makes them inert, dimmed or not', dimmedDuringRun === true);
check('every Configuration section ghosts during a run', cfgGhosted);
check('nothing in any section stays editable except the force-stop button',
  stillLive.length === 1 && stillLive[0] === 'btn-force-stop');
check('force stop is live ONLY during a run',
  cfgIdle['btn-force-stop'] === true && cfgAfter['btn-force-stop'] === true);
check('unlocking restores every control exactly as it was, enabled or not',
  changed.length === 0);
if (stillLive.length > 1) console.log('   still editable:', stillLive.join(', '));
if (changed.length) console.log('   changed by the round trip:', changed.join(', '));

check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0,3)));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
