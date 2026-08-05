// Page-level notes: pinned ones on top, dismissible ones underneath.
//
// The two kinds say different things. One reports a condition still in effect —
// hiding it would not change the condition, so it has no X and belongs nearest
// the content it is about. The other reports something already done, and once
// read there is nothing left for it to warn about.
//
// The order is enforced by the .page-notes region rather than by where a note is
// written, which is the point: a note added in the wrong place still lands in
// the right half, and nobody has to remember the rule to keep it. So this drives
// it the hard way — inserts a pinned note BELOW a dismissible one in the markup
// and checks it still paints above.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));
let ok = true;
const check = (name, cond, extra = '') => {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra)));
  ok = ok && cond;
};

for (const [page, path] of [['Configuration', '/config'], ['Dashboard', '/'],
                            ['Filtering & Scoring', '/explorer']]) {
  await p.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 45000 });

  const R = await p.evaluate(() => {
    const region = document.querySelector('.page-notes');
    if (!region) return { missing: true };
    // Written in the WRONG order on purpose: dismissible first, pinned second.
    const dis = document.createElement('div');
    dis.className = 'config-inline-note is-dismissible mb-3';
    dis.id = 'probe-dismissible';
    dis.innerHTML = '<span>already done</span>'
      + '<button type="button" class="config-inline-dismiss" data-dismiss-note="probe">&times;</button>';
    const pinned = document.createElement('div');
    pinned.className = 'config-inline-warning mb-3';
    pinned.id = 'probe-pinned';
    pinned.textContent = 'still in effect';
    region.append(dis, pinned);
    const top = (el) => el.getBoundingClientRect().top;
    const out = {
      pinnedAbove: top(pinned) < top(dis),
      // Both visible, so the comparison above means something.
      bothShown: dis.offsetHeight > 0 && pinned.offsetHeight > 0,
      // The two kinds must not look alike — the colour is what says which is
      // still waiting on you.
      sameColour: getComputedStyle(dis).borderTopColor === getComputedStyle(pinned).borderTopColor,
      // hidden has to beat the flex display .is-dismissible sets.
      hiddenWins: (() => {
        dis.hidden = true;
        const gone = dis.offsetHeight === 0 && getComputedStyle(dis).display === 'none';
        dis.hidden = false;
        return gone;
      })(),
    };
    dis.remove(); pinned.remove();
    return out;
  });

  check(`${page}: has a page-notes region`, !R.missing);
  if (R.missing) continue;
  check(`${page}: both probe notes render`, R.bothShown === true, R);
  check(`${page}: a pinned note written LAST still paints above the dismissible one`,
        R.pinnedAbove === true, R);
  check(`${page}: the two kinds are told apart by colour`, R.sameColour === false, R);
  check(`${page}: hidden beats the dismissible layout's display`, R.hiddenWins === true, R);
}

// The region sits above the page content, not buried in it.
await p.goto(BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
const above = await p.evaluate(() => {
  const region = document.querySelector('.page-notes');
  const accordion = document.getElementById('cfg-accordion');
  return !!region && !!accordion
    && (region.compareDocumentPosition(accordion) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
});
check('Configuration: the notes region comes before the sections', above === true);

check('no JS errors', errs.length === 0, errs.slice(0, 3));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
