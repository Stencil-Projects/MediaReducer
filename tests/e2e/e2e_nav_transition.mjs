// Navigation must not run through a view transition.
//
// One was tried, to hold the header still while pages changed, and it could not
// sit still. A view transition renders from SNAPSHOTS, and this header is glass
// — 88% surface with a blur behind it — so its snapshot composites over the
// FADING page instead of over live content: it visibly dims and comes back.
// Sampling the header strip through a real tab click measured that swing at ~11
// of 255 with the transition and 0 without it. The body had the matching
// problem — two different pages drawn at once is a double exposure, every line
// of the old page showing through the new.
//
// The mechanism is what has to stay gone, so the mechanism is what is asserted:
// no cross-document transition runs, and nothing carries a view-transition-name,
// which is what would opt an element back into snapshot compositing. Pixel
// sampling was how the fault was found, but it makes a poor guard here — page
// load timing moves the blank-frame boundary around, so it fails for reasons
// unrelated to the fault.
//
// Same-document view transitions are untouched: the theme flip still
// cross-fades, and has its own test.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const ctx = await b.newContext({ viewport: { width: 1400, height: 900 } });
await ctx.addInitScript(() => {
  window.__vtSeen = false;
  window.addEventListener('pagereveal', (e) => { window.__vtSeen = !!e.viewTransition; });
});

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

const p = await ctx.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));
async function open(path) {
  for (let i = 0; i < 5; i++) {
    try {
      await p.goto(BASE + path, { waitUntil: 'load', timeout: 45000 });
      await p.evaluate(() => document.querySelectorAll('.modal.show')
        .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
      await p.waitForTimeout(400);
      return true;
    } catch (_) { await p.waitForTimeout(1000); }
  }
  return false;
}
if (!await open('/config')) { console.log('FAIL could not load /config'); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

// Nothing may carry a view-transition-name. One named element is enough to opt
// that element into snapshot compositing, which is exactly how the header came
// to dim — it was named so it would hold still, and holding still is the one
// thing it then could not do.
for (const path of ['/', '/config', '/explorer']) {
  if (!await open(path)) { check(`could not load ${path}`, false); continue; }
  const named = await p.evaluate(() => {
    const out = [];
    for (const el of document.querySelectorAll('*')) {
      // <html> is always 'root' — that is the user agent's own name for the
      // page snapshot and cannot be removed. Every OTHER name is ours.
      if (el === document.documentElement) continue;
      const n = getComputedStyle(el).viewTransitionName;
      if (n && n !== 'none') out.push((el.tagName + '.' + el.className).slice(0, 60) + ' = ' + n);
    }
    return out;
  });
  check(`${path}: nothing is registered as a view-transition group`,
        named.length === 0, named);
}

// And no navigation actually runs one. Every hop leaves a SCROLLED page, the
// case that looked worst, since the chrome then sits higher than where the
// arriving page draws its own.
for (const [from, link] of [['/config', 'Filtering & Scoring'],
                            ['/config', 'Dashboard'],
                            ['/explorer', 'Dashboard'],
                            ['/', 'Configuration']]) {
  if (!await open(from)) { check(`could not load ${from}`, false); continue; }
  await p.evaluate(() => window.scrollTo(0, 800));
  await p.waitForTimeout(250);
  await p.getByRole('link', { name: link, exact: true }).first().click();
  await p.waitForLoadState('load');
  await p.waitForTimeout(500);
  const r = await p.evaluate(() => ({
    vt: window.__vtSeen,
    // The arriving page still eases its content in — that is the whole of the
    // navigation's animation now, and it must not have been dropped along
    // with the transition.
    entrance: (() => {
      const m = document.getElementById('main');
      return m ? getComputedStyle(m).animationName : null;
    })(),
    path: location.pathname,
  }));
  check(`${from} -> ${link}: no cross-document view transition`, r.vt === false, r);
  check(`${from} -> ${link}: the arriving page still eases in`,
        r.entrance === 'pr-page-enter', r);
}

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
