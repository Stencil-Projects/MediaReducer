// Space Thresholds: the "Where you stand" panel, and the promise that every
// trigger may be off.
//
// The panel exists to remove arithmetic — it states the figure free space (or
// the library) has to reach for each trigger to fire. Two things make that a
// lie rather than merely stale, and both have already happened once:
//
//   • it quoted the INPUT while the section was locked, where a save discards
//     the inputs and writes the saved values instead — a trigger the run would
//     never use;
//   • it read free space once at render and never again, so the figure it is
//     measured against could move with the panel frozen.
//
// The last case is the guard that outlived its rule: unticking the last armed
// threshold used to snap the checkbox back, long after the server started
// accepting an all-off save and resting the scheduler at Paused.
//
// The panel also carries the disk bar — the same one the Dashboard's Storage
// card draws, through the shared prRenderDiskBar. Its figures are deliberately
// NOT gated on anything being armed: they are what tells you a monitored
// directory took effect, so a panel that blanks itself the moment every trigger
// is off is the regression this file guards against.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
const check = (name, cond, extra) => {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + (extra ?? '')));
  ok = ok && cond;
};

const p = await b.newPage({ viewport: { width: 1180, height: 1000 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));
await p.goto(BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForFunction(() => typeof toggleHeadroom === 'function', null, { timeout: 20000 });
// A fresh install opens the welcome dialog over everything.
for (let i = 0; i < 20; i++) {
  if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
  await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
}
await p.waitForTimeout(1000);

// The section is normally gated on a connected server and a saved path; this
// test is about the thresholds themselves, so open it and arm all three.
const arm = () => p.evaluate(() => {
  _simulateRequired = false;
  _spaceThresholdsHaveUsableLibrary = () => true;
  _spaceThresholdsLocked = () => false;
  _configMonitoringActive = true;
  document.querySelector('#s-space')?.classList.add('show');
  for (const [box, fld, val] of [['headroom-enabled', 'HEADROOM_GB', '500'],
                                 ['redline-enabled', 'REDLINE_GB', '200'],
                                 ['cap-enabled', 'MAX_LIBRARY_GB', '9000']]) {
    document.getElementById(box).disabled = false;
    document.getElementById(box).checked = true;
    document.getElementById(fld).disabled = false;
    document.getElementById(fld).value = val;
  }
  _diskStats = { total_gb: 1000, used_gb: 300, free_gb: 700, pct_used: 30 };
  _lastKnownLibraryGb = 8000;
  _updatePills();
});
const rows = () => p.evaluate(() => Object.fromEntries(
  [...document.querySelectorAll('#threshold-status .ts-row')].map(r => [r.id, {
    state: r.querySelector('.ts-state').textContent,
    note: r.querySelector('.ts-note').textContent,
    off: r.classList.contains('is-off'),
  }])));
// The shared bar's drawn state, read straight off the DOM the renderer wrote.
const bar = () => p.evaluate(() => {
  const root = document.getElementById('ts-bar-root');
  const g = s => { const e = root.querySelector(s); return e ? { left: e.style.left, width: e.style.width, hidden: e.hidden } : null; };
  return {
    fill: g('.disk-bar-fill'), lib: g('.disk-bar-library'),
    headroom: g('.disk-bar-mark--headroom'), redline: g('.disk-bar-mark--redline'),
    cap: g('.disk-bar-mark--cap'),
    legend: [...root.querySelectorAll('[data-dbl]')].filter(e => !e.hidden)
      .map(e => e.dataset.dbl).sort(),
    free: document.getElementById('ts-free-value').textContent,
    sub: document.getElementById('ts-disk-sub').textContent,
    library: document.getElementById('ts-library-value').textContent,
  };
});

// ── The trigger figures are computed, not guessed ───────────────────────────
await arm();
let r = await rows();
check('an armed threshold states the figure free space must reach',
      /falls to 500 GB/.test(r['ts-headroom'].note), r['ts-headroom'].note);
check('...and how far away that is',
      r['ts-headroom'].state === '200.0 GB to go', r['ts-headroom'].state);
check('the cap is measured against the library, not the disk',
      /grows past 9,000 GB/.test(r['ts-cap'].note) && r['ts-cap'].state === '1,000.0 GB to go',
      r['ts-cap'].note + ' | ' + r['ts-cap'].state);

// ── The bar states where the disk actually is ───────────────────────────────
// One track for the whole disk, with each armed trigger marked on it: headroom
// at the used edge where 500 GB would still be free (50%), redline at 800 GB
// used. The library is anchored flush to the used edge, so a library larger
// than used space cannot overhang it.
let bs = await bar();
check('the bar fills to the disk\'s used share', bs.fill.width === '30%', bs.fill.width);
check('the headroom marker sits where that trigger fires',
      !bs.headroom.hidden && bs.headroom.left === '50%', JSON.stringify(bs.headroom));
check('the redline marker sits where ITS trigger fires',
      !bs.redline.hidden && bs.redline.left === '80%', JSON.stringify(bs.redline));
check('the library segment never overhangs the used edge',
      parseFloat(bs.lib.left) + parseFloat(bs.lib.width) <= 30.001, JSON.stringify(bs.lib));
// 8,000 GB of library cannot fit under a 9,000 GB cap on a 1,000 GB disk, so
// the marker would land off the end of the bar: it is dropped, not clamped to
// the edge where it would read as a reachable limit.
check('an unreachable cap draws no marker at all', bs.cap.hidden, JSON.stringify(bs.cap));

// ── It follows the disk, not just the library ───────────────────────────────
// A poll that moves ONLY free space used to leave the panel frozen: the
// re-render hung off the library size having changed.
await p.evaluate(() => _applyStatusPayload({
  run_active: false, summary_active: false, cleanup_state: {},
  disk: { total_gb: 1000, used_gb: 100, free_gb: 900, pct_used: 10 }, library_gb: 8000 }));
await p.waitForTimeout(200);
r = await rows();
bs = await bar();
check('a status poll that moves free space redraws the panel',
      r['ts-headroom'].state === '400.0 GB to go', r['ts-headroom'].state);
check('...and the bar with it', bs.fill.width === '10%', bs.fill.width);
check('...and the figures above it', bs.free === '900.0' && /100\.0 \/ 1,000\.0 GB/.test(bs.sub),
      bs.free + ' | ' + bs.sub);

// ── It never quotes a value the save would discard ──────────────────────────
// While the section is locked, getFormConfig keeps the SAVED thresholds and
// ignores the inputs; the panel must describe what would actually be written.
await arm();
const locked = await p.evaluate(() => {
  _spaceThresholdsHaveUsableLibrary = () => false;
  document.getElementById('HEADROOM_GB').value = '77777';
  _updatePills();
  const out = { wouldSave: getFormConfig().HEADROOM_GB,
                note: document.querySelector('#ts-headroom .ts-note').textContent };
  _spaceThresholdsHaveUsableLibrary = () => true;
  return out;
});
check('the panel quotes the trigger the save would write, not the typed one',
      !locked.note.includes('77,777') && !locked.note.includes('77777'),
      `save=${locked.wouldSave} note=${locked.note}`);

// ── Every trigger may be off ────────────────────────────────────────────────
// Each threshold gets its own pass in which it is unticked LAST, because the
// guard only ever bit the last armed one — unticking them in a single fixed
// order leaves two of the three guards untouched and the test green over them.
const TOGGLES = [['Headroom', 'headroom-enabled', 'toggleHeadroom'],
                 ['Redline', 'redline-enabled', 'toggleRedline'],
                 ['Library Size Cap', 'cap-enabled', 'toggleLibraryCap']];
for (const last of TOGGLES) {
  await arm();
  await p.evaluate(() => [...document.querySelectorAll('.pr-toast')].forEach(t => t.remove()));
  for (const [, box, fn] of TOGGLES) {
    if (box === last[1]) continue;                       // this one goes last
    await p.evaluate(({ box, fn }) => {
      document.getElementById(box).checked = false;
      window[fn]();
    }, { box, fn });
    await p.waitForTimeout(150);
  }
  await p.evaluate(({ box, fn }) => {
    document.getElementById(box).checked = false;
    window[fn]();
  }, { box: last[1], fn: last[2] });
  await p.waitForTimeout(250);
  const stuck = await p.evaluate(x => document.getElementById(x).checked, last[1]);
  const bounced = await p.evaluate(() => [...document.querySelectorAll('.pr-toast')]
    .map(t => t.textContent).filter(t => /must drive cleanup|Turn on a Redline/.test(t)));
  check(`${last[0]} may be the LAST trigger unticked`, !stuck && bounced.length === 0,
        `checked=${stuck} toast=${bounced.join(' | ')}`);
}

const endState = await p.evaluate(() => ({
  cfg: (({ HEADROOM_GB, REDLINE_GB, MAX_LIBRARY_GB }) =>
        ({ HEADROOM_GB, REDLINE_GB, MAX_LIBRARY_GB }))(getFormConfig()),
  note: !document.getElementById('no-trigger-note')?.hidden,
  monitor: document.getElementById('mode-paused')?.disabled,
  cleanup: document.getElementById('mode-headroom')?.disabled,
  panel: [...document.querySelectorAll('#threshold-status .ts-state')].map(s => s.textContent),
  specs: [...document.querySelectorAll('.mode-spec-v')].map(s => s.textContent),
}));
check('the form would save every threshold off',
      Number(endState.cfg.HEADROOM_GB) === 0 && endState.cfg.REDLINE_GB === null
      && endState.cfg.MAX_LIBRARY_GB === null, JSON.stringify(endState.cfg));
check('the panel and the mode specs both read Off',
      endState.panel.every(s => s === 'Off') && endState.specs.every(s => s === 'Off'),
      endState.panel.join() + ' | ' + endState.specs.join());

// ── Every trigger off does NOT mean the panel goes blank ────────────────────
// The figures and the bar answer "how much space is there, and how much of it
// is the library" — the question the thresholds are set AGAINST. They used to
// vanish the moment nothing was armed, which is exactly when a new install
// needs them to confirm a monitored directory took effect.
//
// Asserting the figures are merely PRESENT here proves nothing: they were
// painted while the thresholds were still armed, and stale text looks identical
// to live text. So move the disk first, with everything off, and require the
// panel to follow — that is the only version of this check that fails when the
// figures stop being updated.
await p.evaluate(() => _applyStatusPayload({
  run_active: false, summary_active: false, cleanup_state: {},
  disk: { total_gb: 2000, used_gb: 1500, free_gb: 500, pct_used: 75 },
  library_gb: 1200, thresholds_configured: true }));
await p.waitForTimeout(200);
const bareBar = await bar();
check('free space still TRACKS the disk with every trigger off',
      bareBar.free === '500.0', bareBar.free);
check('used / total still tracks with every trigger off',
      /1,500\.0 \/ 2,000\.0 GB/.test(bareBar.sub), bareBar.sub);
check('the library size still tracks with every trigger off',
      bareBar.library === '1,200.0', bareBar.library);
check('the bar still redraws with every trigger off', bareBar.fill.width === '75%',
      bareBar.fill.width);
check('the panel does not hide itself once nothing is armed',
      !(await p.evaluate(() => document.getElementById('threshold-status').hidden)));
// The markers are the part that SHOULD go: nothing armed, nothing to mark.
check('no trigger markers remain in the legend',
      JSON.stringify(bareBar.legend) === JSON.stringify(['library']), bareBar.legend.join());

// ── Nothing monitored is a different state from nothing armed ───────────────
// Neither _diskStats nor the library size is cleared when the paths go away
// (the poll simply stops carrying them), so the panel has to dash on the
// monitoring flag or it will keep quoting a disk it can no longer see.
const unmonitored = await p.evaluate(() => {
  _configMonitoringActive = false;
  _updateThresholdStatus();
  const root = document.getElementById('ts-bar-root');
  return {
    free: document.getElementById('ts-free-value').textContent,
    library: document.getElementById('ts-library-value').textContent,
    sub: document.getElementById('ts-disk-sub').textContent,
    fill: root.querySelector('.disk-bar-fill').style.width,
    legend: [...root.querySelectorAll('[data-dbl]')].filter(e => !e.hidden).length,
  };
});
check('nothing monitored dashes the figures rather than quoting a stale disk',
      unmonitored.free === '—' && unmonitored.library === '—'
      && unmonitored.sub.includes('— / —'), JSON.stringify(unmonitored));
check('...and empties the bar and its legend',
      unmonitored.fill === '0%' && unmonitored.legend === 0, JSON.stringify(unmonitored));
await p.evaluate(() => { _configMonitoringActive = true; _updateThresholdStatus(); });
// Nothing armed is allowed, but it is not silent: the consequence is stated,
// and the modes that need a trigger ghost rather than the save being refused.
check('the "nothing armed" note explains the consequence', endState.note);
// The two radios deliberately key off different things, and the difference is
// worth holding: Automatic Cleanup gates on the LIVE form, because arming
// deletion against an unsaved threshold is the dangerous case; Monitor Only
// gates on the SAVED config, so typing in a field never flickers the radio
// under the pointer. So mid-edit only Automatic Cleanup ghosts.
check('Automatic Cleanup ghosts as soon as the form has nothing armed',
      endState.cleanup, `cleanup=${endState.cleanup}`);
const savedOff = await p.evaluate(() => {
  _savedConfig = { ..._savedConfig, HEADROOM_GB: 0, REDLINE_GB: null, MAX_LIBRARY_GB: null };
  _updateRunModeAvailability();
  return { monitor: document.getElementById('mode-paused')?.disabled,
           reason: document.getElementById('desc-paused')?.textContent };
});
check('Monitor Only ghosts once nothing armed is the SAVED state',
      savedOff.monitor, `monitor=${savedOff.monitor}`);
check('...naming the fix rather than just going gray',
      /Arm a space threshold/.test(savedOff.reason), savedOff.reason);

check('no page errors', errs.length === 0, errs.join('; '));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
