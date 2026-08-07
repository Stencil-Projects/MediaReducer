// Filtering & Scoring is four collapsible categories, the same stack the
// Configuration page uses.
//
// Laid flat, the four groups ran a thousand pixels of settings you are mostly
// not changing, and each carried a heading and a subhead to read past on the
// way to the one you wanted. As a stack, one category is open and the other
// three are a line each.
//
// Collapsing settings costs something, and that cost is what most of this test
// is about: a field can now go red where you cannot see it, and the only
// symptom left is a Save button that refuses to work. So the section header
// carries a marker while it holds a flagged field — the same marker, from the
// same shared code, that the Configuration page uses, which is checked here on
// both pages for the first time.
//
// The run lock is checked too. It disables the settings, and it must NOT
// disable the headers: a run stops you changing a setting, it should not stop
// you reading what one says.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

const errs = [];
const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
p.on('pageerror', e => errs.push(e.message));

// Run-active is faked through the status poll, as in e2e_runlock.
let fakeRunActive = false;
await p.route('**/api/status**', async (route) => {
  const resp = await route.fetch();
  const d = await resp.json();
  d.run_active = fakeRunActive;
  await route.fulfill({ response: resp, json: d });
});

let loaded = false;
for (let i = 0; i < 5; i++) {
  try {
    await p.goto(BASE + '/explorer', { waitUntil: 'load', timeout: 45000 });
    loaded = true; break;
  } catch (_) { await p.waitForTimeout(1000); }
}
if (!loaded) { console.log('FAIL could not load /explorer'); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }
await p.evaluate(() => document.querySelectorAll('.modal.show')
  .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
await p.waitForTimeout(400);

const SECTIONS = ['#fs-scope', '#fs-scoring', '#fs-eligibility', '#fs-tv'];
const header = sel => `[data-bs-target="${sel}"]`;
const stackHeight = () => p.evaluate(() =>
  Math.round(document.getElementById('filter-score-card').getBoundingClientRect().height));

// Waits out the collapse animation rather than a fixed sleep: the class lands
// when the transition ENDS, and a height read mid-slide is neither number.
async function toggle(sel, open) {
  await p.click(header(sel));
  await p.waitForSelector(`${sel}${open ? '.show' : ':not(.show)'}`, { timeout: 5000 });
  await p.waitForFunction(s => !document.querySelector(s).classList.contains('collapsing'),
                          sel, { timeout: 5000 });
}

const shape = await p.evaluate((sels) => {
  const card = document.getElementById('filter-score-card');
  return {
    names: [...card.querySelectorAll('.accordion-button')]
             .map(h => h.textContent.replace('!', '').trim()),
    ids: sels.filter(s => card.querySelector(s)),
    open: sels.filter(s => card.querySelector(s).classList.contains('show')),
    // Every setting is still here — just folded away, not dropped.
    settings: card.querySelectorAll('.filter-score-card').length,
  };
}, SECTIONS);
check('the four groups are the four sections, in order',
      JSON.stringify(shape.names) === JSON.stringify(
        ['Cleanup scope', 'Scoring & ordering', 'Eligibility filters', 'TV show scoring']),
      shape.names);
check('all four are in the DOM with all 12 settings — folded, not dropped',
      shape.ids.length === 4 && shape.settings === 12, shape);
check('one starts open, the rest closed', shape.open.length === 1 && shape.open[0] === '#fs-scope',
      shape.open);

// The saving itself: one open beside three closed is a fraction of the flat
// card. Measured against this page's own all-open height, so it cannot pass by
// the card having quietly lost content.
const oneOpen = await stackHeight();
for (const s of SECTIONS.slice(1)) await toggle(s, true);
const allOpen = await stackHeight();
check('one category open is far shorter than all four', oneOpen < allOpen * 0.6,
      { oneOpen, allOpen });
for (const s of SECTIONS.slice(1)) await toggle(s, false);
check('...and closing them again returns to that height',
      Math.abs(await stackHeight() - oneOpen) <= 2, { oneOpen });

// Opening and closing is the whole interaction; it has to work on a section
// that did not start open.
await toggle('#fs-eligibility', true);
check('a closed category opens on its header',
      await p.isVisible('#c-grace'), null);
await toggle('#fs-eligibility', false);
check('...and closes again', !(await p.isVisible('#c-grace')), null);

// ── A red field in a CLOSED section still says where it is ──────────────────
await toggle('#fs-scoring', true);
await p.fill('#c-stale', '999');                   // valid range is 1–120
await p.click('.page-title');                      // blur, which flags the field
await p.waitForTimeout(300);
const flaggedOpen = await p.evaluate(() => ({
  red: !!document.getElementById('c-stale').closest('.field-invalid'),
  save: document.getElementById('btn-cfg-save').disabled,
}));
check('an out-of-range value flags the field and blocks Save',
      flaggedOpen.red && flaggedOpen.save === true, flaggedOpen);

await toggle('#fs-scoring', false);
const marker = () => p.evaluate(() => {
  const out = {};
  document.querySelectorAll('#filter-score-card .accordion-item').forEach((item) => {
    const btn = item.querySelector('.accordion-button');
    const dot = btn.querySelector('.pr-section-issue');
    out[btn.textContent.replace('!', '').trim()] = {
      flagged: btn.classList.contains('section-has-issue'),
      shown: !!dot && getComputedStyle(dot).display !== 'none',
      title: btn.title,
    };
  });
  return out;
});
const hidden = await marker();
check('the closed section holding it is marked, so you know where to look',
      hidden['Scoring & ordering'].flagged && hidden['Scoring & ordering'].shown
      && /needs attention/.test(hidden['Scoring & ordering'].title), hidden);
check('...and only that one — a clean section carries no marker',
      !hidden['Cleanup scope'].flagged && !hidden['Cleanup scope'].shown
      && !hidden['Eligibility filters'].flagged && !hidden['TV show scoring'].flagged, hidden);

// Negative control for the marker: put the value back. If the mark did not
// clear, it is painted on rather than tracking the field, and the check above
// proves nothing.
await toggle('#fs-scoring', true);
await p.fill('#c-stale', '36');
await p.click('.page-title');
await p.waitForTimeout(300);
await toggle('#fs-scoring', false);
const cleared = await marker();
check('fixing the field clears the marker (it tracks the field, not the event)',
      !cleared['Scoring & ordering'].flagged && !cleared['Scoring & ordering'].shown, cleared);

// ── A run locks the settings without locking the headers ───────────────────
fakeRunActive = true;
await p.waitForFunction(() => document.getElementById('filter-score-card')
  ?.classList.contains('section-run-ghost'), { timeout: 12000 });
const during = await p.evaluate(() => {
  const btn = document.querySelector('#filter-score-card .accordion-button');
  let dim = 1, n = btn;
  while (n && n !== document.body) { dim *= parseFloat(getComputedStyle(n).opacity); n = n.parentElement; }
  const body = document.querySelector('#fs-scope .accordion-body');
  return {
    headerDisabled: btn.disabled,
    headerOpacity: Math.round(dim * 100) / 100,
    settingDisabled: document.getElementById('c-movies-on').disabled,
    settingDim: getComputedStyle(body.firstElementChild).opacity,
  };
});
check('a run leaves the section headers live and legible',
      during.headerDisabled === false && during.headerOpacity === 1, during);
check('...while the settings inside are disabled and dimmed',
      during.settingDisabled === true && during.settingDim === '0.55', during);
// Live means live: it still opens. Guarded, because a header locked along
// with its settings does not merely read wrong here — the click never lands,
// and an uncaught timeout would take the rest of the run with it.
let opened = null;
try {
  await toggle('#fs-tv', true);
  opened = await p.isVisible('#c-tv-elig')
           && await p.evaluate(() => document.getElementById('c-tv-elig').disabled);
} catch (e) { opened = e.message; }
check('...and a locked category can still be opened and read', opened === true, opened);
fakeRunActive = false;
await p.waitForFunction(() => !document.getElementById('filter-score-card')
  ?.classList.contains('section-run-ghost'), { timeout: 12000 });

// ── The same marker, same code, on the Configuration page ──────────────────
// It moved to base.html to be shared; this is the first test of either copy.
const c = await b.newPage({ viewport: { width: 1400, height: 900 } });
c.on('pageerror', e => errs.push('config: ' + e.message));
await c.goto(BASE + '/config', { waitUntil: 'load', timeout: 45000 });
await c.waitForTimeout(1500);
await c.evaluate(() => document.querySelectorAll('.modal.show')
  .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
const cfg = await c.evaluate(() => ({
  markers: document.querySelectorAll('#cfg-accordion .pr-section-issue').length,
  sections: document.querySelectorAll('#cfg-accordion .accordion-item').length,
}));
check('Configuration gets a marker on every section from the shared code',
      cfg.markers === cfg.sections && cfg.sections >= 4, cfg);

// Whether any section is ACTUALLY faulty depends on the fixture, so the flag
// is driven directly: paint a field red and the page's own observer must mark
// the section that holds it — and only that one.
const flagName = sec => c.evaluate((s) => {
  const item = document.getElementById(s).closest('.accordion-item');
  return {
    mine: item.querySelector('.accordion-button').classList.contains('section-has-issue'),
    others: [...document.querySelectorAll('#cfg-accordion .accordion-item')]
      .filter(i => i !== item)
      .filter(i => i.querySelector('.accordion-button').classList.contains('section-has-issue'))
      .length,
  };
}, sec);
const cfgClean = await flagName('s-space');
await c.evaluate(() => document.querySelector('#s-space .accordion-body')
  .firstElementChild.classList.add('field-invalid'));
await c.waitForTimeout(300);
const cfgDirty = await flagName('s-space');
await c.evaluate(() => document.querySelector('#s-space .accordion-body')
  .firstElementChild.classList.remove('field-invalid'));
await c.waitForTimeout(300);
const cfgBack = await flagName('s-space');
check('...and a red field marks the section holding it, there too',
      cfgClean.mine === false && cfgDirty.mine === true
      && cfgDirty.others === cfgClean.others, { cfgClean, cfgDirty });
check('...clearing it unmarks the section', cfgBack.mine === false, cfgBack);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
