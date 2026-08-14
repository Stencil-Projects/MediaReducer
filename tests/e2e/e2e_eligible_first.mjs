// The Filtering & Scoring table's "Eligible first" checkbox.
//
// It groups eligible/prunable rows above filtered ones while each group keeps
// sorting by the chosen column. In one view it has nothing left to do, and
// that view is the one the page opens in: sorted by # ascending, only eligible
// rows carry a deletion rank (a filtered row's # cell is an em dash), and the
// rank comparator already puts ranked rows first. Clicking the box there used
// to change nothing at all, which reads as a broken control rather than an
// inapplicable one — so it is disabled while it cannot apply.
//
// Both halves need pinning. If the disable is dropped the box goes back to
// swallowing clicks; if it over-reaches to the other sorts, a working control
// goes dead.
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

// Titles are alphabetical so a Title sort is predictable, and the two filtered
// rows sit in the middle of it — if grouping stops working they interleave.
const row = (title, extra = {}) => ({
  title, year: 2020, rating: 6.5, votes: 5000, plays: 0, users: 1,
  last_played: 0, added_at: 1_500_000_000, size_gb: 8, size_bytes: 8_000_000_000,
  protected: false, favorite: false, excluded: false, media_type: 'movie', ...extra,
});
// Protected is the one filter that applies whatever the saved config says —
// favorite only counts when the Jellyfin-favorites switch is on, and the
// rating/grace/unplayed rungs all depend on settings this test does not own.
const movies = [
  row('Alpha'),
  row('Bravo', { protected: true }),
  row('Charlie'),
  row('Delta', { protected: true }),
  row('Echo'),
];
await p.route('**/api/library-snapshot**', async route => {
  try {
    await route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, built_at: 1_700_000_000, movies,
                             imdb_dataset_on_disk: false }) });
  } catch (_) { try { await route.continue(); } catch (_) { /* page gone */ } }
});

await p.goto(BASE + '/explorer', { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForFunction(() =>
  document.querySelectorAll('#mtbody tr').length > 0 &&
  !document.getElementById('mtbody').textContent.includes('Loading'), { timeout: 20000 });

const boxState = () => p.evaluate(() => {
  const el = document.getElementById('stack-eligible');
  const lab = el?.closest('.stackctl');
  return { disabled: !!el?.disabled, checked: !!el?.checked,
           moot: !!lab?.classList.contains('is-moot'), title: lab?.title || '' };
});
// The order of the rows, with filtered ones marked, so a grouping change shows.
const order = () => p.evaluate(() => [...document.querySelectorAll('#mtbody tr')]
  .map(tr => tr.children[0].textContent.trim() === '—'
    ? tr.children[1].textContent.trim() + '*'
    : tr.children[1].textContent.trim()).join(' '));
const setBox = on => p.evaluate(v => {
  const el = document.getElementById('stack-eligible');
  if (el.disabled) return;            // a real click could not do it either
  el.checked = v; renderT();
}, on);
const sortBy = async col => { await p.evaluate(c => sb(c), col); await p.waitForTimeout(120); };

// ── The default view ────────────────────────────────────────────────────────
let s = await boxState();
check('the page opens sorted by # ascending', await p.evaluate(() => sc === 'order' && sd === 1));
check('the box is disabled there, because it has nothing to change', s.disabled, s);
check('...and stays ticked, since that IS the order shown', s.checked, s);
check('...with the label greyed to match', s.moot, s);
check('...and a tooltip that says why', /already lists eligible titles first/.test(s.title), s.title);

const defaultOrder = await order();
check('eligible rows still lead the default view',
      !/\*/.test(defaultOrder.split(' ').slice(0, 3).join(' ')), defaultOrder);
check('...and the filtered ones trail it, having no deletion order',
      /Bravo\* Delta\*$|Delta\* Bravo\*$/.test(defaultOrder), defaultOrder);

// ── Where it does apply ─────────────────────────────────────────────────────
await sortBy('title');
s = await boxState();
check('sorting by Title re-enables the box', !s.disabled, s);
check('...ungreys the label', !s.moot, s);
check('...and restores the explaining tooltip',
      /grouped above filtered rows/.test(s.title), s.title);

const titleGrouped = await order();
check('ticked, Title sorts within the eligible group first',
      titleGrouped === 'Alpha Charlie Echo Bravo* Delta*', titleGrouped);

await setBox(false);
await p.waitForTimeout(120);
const titleFlat = await order();
check('unticked, the filtered rows take their place in the Title order',
      titleFlat === 'Alpha Bravo* Charlie Delta* Echo', titleFlat);
check('...which is a real change, not a redraw', titleFlat !== titleGrouped,
      [titleGrouped, titleFlat]);

await setBox(true);
await p.waitForTimeout(120);
check('re-ticking restores the grouping', await order() === titleGrouped, titleGrouped);

// # descending is still the box's business: without it the em-dash rows lead.
// One click from Title lands on # ascending; the second toggles the direction.
await sortBy('order');
await sortBy('order');
check('a second click on # sorts descending', await p.evaluate(() => sc === 'order' && sd === -1));
s = await boxState();
check('the box is live again descending — there it decides which end', !s.disabled, s);
const descGrouped = await order();
await setBox(false);
await p.waitForTimeout(120);
const descFlat = await order();
check('unticked, the rows with no deletion order come first descending',
      /^\S+\* /.test(descFlat), descFlat);
check('ticked, the eligible rows keep the top', !/^\S+\*/.test(descGrouped), descGrouped);
check('...so the box changes the descending view', descGrouped !== descFlat, [descGrouped, descFlat]);

// Returning to the default view re-disables it rather than leaving it live.
await setBox(true);
await sortBy('order');
s = await boxState();
check('back at # ascending the box is disabled again', s.disabled, s);

check('no JS errors', errs.length === 0, errs.slice(0, 3));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
