// Switching themes cross-fades the whole page, and no frame wears both palettes.
//
// The original transition eased a LIST of elements — html, body, .site-header,
// .card, .form-control — over 150ms while every other surface, border and label
// repainted instantly. For those 150ms the page was visibly half-converted: a
// light hairline over a still-dark header, hints and labels already in the new
// theme against panels still in the old. Those are the seams, and the first fix
// removed the transition entirely rather than solving them.
//
// The transition is back, animated differently: the DOM's colors still change
// in ONE frame (nothing can be caught between palettes), and the ANIMATION is a
// view-transition crossfade of the whole page — which also covers the surfaces
// CSS can never ease between, like gradient card headers and the body washes.
//
// Sampling must straddle the old transition list — eased surfaces AND un-listed
// ones — or it cannot see the seam it is guarding against. The legacy path is
// re-run at the end as a negative control and asserted to STILL seam, so this
// test can never pass by having gone blind.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage({ viewport: { width: 1100, height: 800 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

let navErr;
for (let i = 0; i < 5; i++) {
  try {
    await p.goto(BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof prToggleTheme === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /config:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }
// The welcome modal traps focus and holds a backdrop over the page.
await p.evaluate(() => document.querySelectorAll('.modal.show')
  .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
await p.waitForTimeout(500);

async function flip(legacy) {
  return p.evaluate(async (isLegacy) => {
    const SURFACES = {
      // In the old transition list — these eased. html is the one that eases
      // LAST and alone when suppression lifts early, so it must be sampled.
      page:      ['html', 'backgroundColor'],
      header:    ['.site-header', 'backgroundColor'],
      input:     ['.form-control', 'backgroundColor'],
      bodyText:  ['body', 'color'],
      // NOT in it — these snapped, which is what produced the seam.
      btn:       ['.btn-outline-secondary', 'backgroundColor'],
      btnBorder: ['.btn-outline-secondary', 'borderTopColor'],
      hint:      ['.form-text', 'color'],
    };
    const pick = () => {
      const out = {};
      for (const [k, [sel, prop]] of Object.entries(SURFACES)) {
        const el = document.querySelector(sel);
        out[k] = el ? getComputedStyle(el)[prop] : '';
      }
      return out;
    };
    const before = pick();
    const seen = { vt: false };
    const realVT = document.startViewTransition;
    if (typeof realVT === 'function') {
      document.startViewTransition = function (cb) { seen.vt = true; return realVT.call(this, cb); };
    }
    if (isLegacy) {
      // The old path, verbatim: a bare attribute flip with no suppression
      // class and no view transition.
      const root = document.documentElement;
      const t = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', t);
      root.setAttribute('data-bs-theme', t);
    } else {
      prToggleTheme();
    }
    const frames = [];
    for (let i = 0; i < 40; i++) {
      frames.push(pick());
      await new Promise(r => requestAnimationFrame(r));
    }
    if (typeof realVT === 'function') document.startViewTransition = realVT;
    const after = pick();
    const keys = Object.keys(before).filter(k => before[k] !== after[k]);
    // Seamed: at one instant some surfaces have ALREADY landed on the new
    // palette while others have not.
    const seamed = frames.filter((f) => {
      const landed = keys.filter(k => f[k] === after[k]).length;
      return landed > 0 && landed < keys.length;
    });
    return { usedViewTransition: seen.vt, changed: keys, seamed: seamed.length };
  }, legacy);
}

const live = await flip(false);
check('the flip repaints surfaces on both sides of the old transition list',
      live.changed.length >= 4, live.changed);
check('no frame wears both palettes', live.seamed === 0, live);
check('...and the flip is animated, not just instant',
      live.usedViewTransition === true, live);

// A second flip landing while the first is still settling must not lift the
// suppression early: an earlier call's backstop timer firing mid-crossfade
// un-suppresses transitions while the new palette is still resolving, and html
// eases alone against surfaces that already snapped. Real users hit this by
// toggling twice; the test hits it deterministically.
const rapid = await p.evaluate(async () => {
  const root = document.documentElement;
  prSetTheme(root.getAttribute('data-theme') === 'light' ? 'dark' : 'light');
  // That call's backstop timer fires 400ms later. Wait 350 so it lands ~50ms
  // into the NEXT flip — mid-crossfade, exactly where it did damage.
  await new Promise(r => setTimeout(r, 350));
  const before = getComputedStyle(root).backgroundColor;
  prToggleTheme();
  const seen = [];
  for (let i = 0; i < 40; i++) {
    seen.push(getComputedStyle(root).backgroundColor);
    await new Promise(r => requestAnimationFrame(r));
  }
  const after = getComputedStyle(root).backgroundColor;
  // Every sample is one palette or the other; an eased value is neither.
  return { before, after, tweened: seen.filter(v => v !== before && v !== after).length };
});
check('a flip during an earlier one still lands the page color in one frame',
      rapid.before !== rapid.after && rapid.tweened === 0, rapid);

// Nothing may animate as its own group during a theme flip: one element on a
// shorter group than the page crossfade snaps ahead of it, which is the seam
// rebuilt out of view transitions — and invisible to any computed-style check,
// because the DOM still flips atomically. The header carried such a name once,
// for navigation; navigation no longer uses view transitions at all (see
// e2e_nav_transition), so the whole page dissolves as one snapshot. Assert on
// the animations themselves.
const anims = await p.evaluate(async () => {
  const out = { during: [] };
  const real = document.startViewTransition.bind(document);
  document.startViewTransition = (cb) => {
    const vt = real(cb);
    vt.ready.then(() => {
      out.during = document.getAnimations()
        .map(a => ({ pseudo: String((a.effect && a.effect.pseudoElement) || ''),
                     dur: a.effect && a.effect.getTiming().duration }))
        .filter(x => x.pseudo.includes('view-transition'));
    });
    return vt;
  };
  prToggleTheme();
  await new Promise(r => setTimeout(r, 400));
  document.startViewTransition = real;
  return out;
});
check('the header does not animate as its own group during a theme flip',
      !anims.during.some(a => a.pseudo.includes('site-header')), anims.during);
check('...the page crossfade is the whole animation, at full duration',
      anims.during.some(a => a.pseudo.includes('view-transition-new(root)') && a.dur >= 200),
      anims.during);

// The flip must not move anything: a layout shift would make the crossfade
// slide instead of dissolve. Scroll is pinned first — the page restores its
// own scroll position, and that drift is not the theme's doing.
await p.evaluate(() => window.scrollTo(0, 0));
await p.waitForTimeout(600);
const geom = () => p.evaluate(() => [document.documentElement.scrollHeight,
                                     Math.round(document.querySelector('.site-header').getBoundingClientRect().height)]);
const gBefore = await geom();
await p.evaluate(() => prToggleTheme());
await p.waitForTimeout(700);
const gAfter = await geom();
check('the flip moves no layout', JSON.stringify(gBefore) === JSON.stringify(gAfter), [gBefore, gAfter]);

// Negative control: the legacy path must STILL seam, or the probe above proves
// nothing about the fix.
const legacy = await flip(true);
check('the legacy element-by-element flip still seams (the probe can see it)',
      legacy.seamed > 0, legacy);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
