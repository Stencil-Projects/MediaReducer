// Advanced -> Reduce visual effects: the decorative layer comes off, and
// nothing that carries information comes off with it.
//
// Off by default, so the check runs both ways round the same page: what is
// there normally has to be gone, and what is gone has to still be there.
//
// The trap this guards is not "did the animation stop". It is what a stopped
// animation LEAVES BEHIND. Several of these effects are elements drawn over
// something, so killing only their animation freezes them mid-effect: the tab
// wave becomes a permanent gradient, the sliding underline strands itself
// under whichever tab the pointer last touched — and, because that bar
// suppresses the per-tab underline while it exists, the current tab would be
// left with no mark at all. Those are removed outright, and the marks they
// replaced have to come back.
//
// Flipped by cookie, since that is where appearance lives — per browser, not
// per install, so no other test's app state is touched by any of this. Still
// restored in a finally: the later checks in this file read a page rendered
// under the setting the earlier ones left behind.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));

// Cookies, not config: appearance is per browser, so the server reads it off
// the request rather than out of config.json. Set on the CONTEXT, which is how
// a real browser would carry them into the next navigation — the stamp on
// <html> has to be right on the first paint, so it cannot be applied after the
// page arrives.
const url = new URL(BASE);
async function setDisplay(patch) {
  await p.context().addCookies(Object.entries(patch).map(([name, off]) => ({
    name, value: off ? 'off' : 'on', domain: url.hostname, path: '/',
  })));
}
// Both switches, always stated together, so no case inherits the last one.
const setEffects = (reduce) => setDisplay({ mr_effects: reduce, mr_glass: false });
const setGlassOff = (off) => setDisplay({ mr_effects: false, mr_glass: off });

// Everything that should differ, read off one loaded page.
async function look(path) {
  for (let i = 0; i < 5; i++) {
    try { await p.goto(BASE + path, { waitUntil: 'load', timeout: 45000 }); break; }
    catch (_) { await p.waitForTimeout(1000); }
  }
  await p.evaluate(() => document.querySelectorAll('.modal.show')
    .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
  await p.waitForTimeout(400);
  return p.evaluate(() => {
    const active = document.querySelector('.header-tabs a.active');
    const anyTab = document.querySelector('.header-tabs a');
    const main = document.getElementById('main');
    const opaque = c => c && c !== 'transparent' && !/rgba\(0, 0, 0, 0\)/.test(c);
    // The wave overlay is a ::after that only exists mid-navigation, so it is
    // asked about directly rather than waited for.
    const waveHidden = (() => {
      anyTab.classList.add('is-navigating');
      const d = getComputedStyle(anyTab, '::after').display;
      anyTab.classList.remove('is-navigating');
      return d === 'none';
    })();
    return {
      attr: document.documentElement.getAttribute('data-effects'),
      entrance: getComputedStyle(main).animationName,
      tabEases: getComputedStyle(anyTab).transitionProperty !== 'none',
      slider: !!document.querySelector('.header-tab-indicator'),
      waveHidden,
      // Information, not decoration: the current tab is still marked.
      activeMarked: opaque(getComputedStyle(active).borderBottomColor),
      activeName: active.textContent.trim(),
      // The run pill breathes while a run is live. The pill still appears and
      // still says Running; it just holds still.
      pillPulse: (() => {
        const pill = document.querySelector('.header-run-badge');
        return pill ? getComputedStyle(pill).animationName : 'missing';
      })(),
    };
  });
}

// Every backdrop-filter the page would ask the compositor for, pseudo-elements
// included, plus how many background layers the two stuck bars paint — the
// compensation that keeps them opaque once the blur is gone stacks a second
// one under the tint.
async function glass(path) {
  await p.goto(BASE + path, { waitUntil: 'load' });
  await p.evaluate(() => document.querySelectorAll('.modal.show')
    .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
  await p.waitForTimeout(300);
  return p.evaluate(() => {
    const blur = (el, pe) => {
      const s = getComputedStyle(el, pe);
      const v = s.backdropFilter || s.webkitBackdropFilter;
      return v && v !== 'none';
    };
    let blurred = 0;
    for (const el of document.querySelectorAll('*')) {
      for (const pe of [null, '::before', '::after']) if (blur(el, pe)) blurred++;
    }
    const layers = (sel, pe) => {
      const el = document.querySelector(sel);
      if (!el) return 0;
      const img = getComputedStyle(el, pe).backgroundImage;
      return !img || img === 'none' ? 0 : img.split(/,(?![^(]*\))/).length;
    };
    return { blurred,
             headerLayers: layers('.site-header', null),
             titleLayers: layers('.page-title-bar', '::before') };
  });
}

// Light/dark. The palette lands in one frame either way — that is what stops
// the page wearing half of each theme — but the CROSS-FADE over it is a
// snapshot of the whole page animating over another, the largest animation the
// app runs. Asked of the API rather than watched, since the transition owns the
// pixels while it runs and there is nothing stable to sample.
async function themeSwap() {
  await p.goto(BASE + '/', { waitUntil: 'load' });
  await p.waitForTimeout(300);
  return p.evaluate(async () => {
    let used = 0;
    const real = document.startViewTransition;
    if (typeof real !== 'function') return { supported: false, used: 0 };
    document.startViewTransition = function (...a) { used++; return real.apply(this, a); };
    try {
      const was = document.documentElement.getAttribute('data-theme');
      window.prSetTheme(was === 'light' ? 'dark' : 'light', { animate: true });
      await new Promise(r => setTimeout(r, 500));
      window.prSetTheme(was === 'light' ? 'light' : 'dark', { animate: false });
    } finally { document.startViewTransition = real; }
    return { supported: true, used };
  });
}

// The dashboard's progress fill: stripes are a background-image, the sweep is
// an ::after. Both are what "still working" looks like while the percentage
// holds still, and both are decoration — the percentage says it in numbers.
async function bar() {
  await p.goto(BASE + '/', { waitUntil: 'load' });
  await p.evaluate(() => document.querySelectorAll('.modal.show')
    .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
  await p.waitForTimeout(300);
  return p.evaluate(() => {
    const el = document.querySelector('.rp-bar');
    if (!el) return null;
    // Exactly the running state, nothing else. A dashboard showing a finished
    // run leaves .is-done on the bar, and its `background` shorthand resets
    // background-image — so merely ADDING .is-running reads as "no stripes"
    // whatever the setting says.
    const had = el.className;
    el.className = 'rp-bar is-running';
    const cs = getComputedStyle(el), after = getComputedStyle(el, '::after');
    const out = { stripes: cs.backgroundImage !== 'none',
                  sweep: after.display !== 'none',
                  striping: cs.animationName !== 'none' };
    el.className = had;
    return out;
  });
}

try {
  // ── Default: everything on ────────────────────────────────────────────────
  await setEffects(false);
  const on = await look('/config');
  check('off by default — nothing is stamped on the document',
        on.attr === null, on);
  check('...the page entrance animates', on.entrance === 'pr-page-enter', on);
  check('...the tabs ease', on.tabEases === true, on);
  check('...the sliding underline is built', on.slider === true, on);
  check('...the run pill pulses', on.pillPulse === 'pr-pulse', on);
  const barOn = await bar();
  check('...and a running progress bar carries stripes and a sweep',
        barOn && barOn.stripes && barOn.sweep && barOn.striping, barOn);
  // The baseline the glass check below is worth anything against: a page with
  // no blur to begin with would pass "no blur when reduced" on its own.
  const glassOn = await glass('/config');
  check('...and the glass is on: Config asks for a blurred backdrop many times over',
        glassOn.blurred > 10, glassOn);
  const swapOn = await themeSwap();
  check('...and light/dark cross-fades',
        !swapOn.supported || swapOn.used === 1, swapOn);

  // ── Reduced ───────────────────────────────────────────────────────────────
  await setEffects(true);
  for (const path of ['/', '/config', '/explorer']) {
    const off = await look(path);
    check(`${path}: the document is stamped, so the FIRST paint is plain`,
          off.attr === 'off', off);
    check(`${path}: no page entrance`, off.entrance === 'none', off);
    check(`${path}: nothing eases`, off.tabEases === false, off);
    check(`${path}: no sliding underline is built`, off.slider === false, off);
    check(`${path}: no click wave`, off.waveHidden === true, off);
    // The one that would be easy to lose: the bar that slides is also the bar
    // that marks the current tab.
    check(`${path}: the current tab is still underlined (${off.activeName})`,
          off.activeMarked === true, off);
    check(`${path}: the run pill holds still`, off.pillPulse === 'none', off);
  }
  const barOff = await bar();
  check('a running progress bar drops its stripes and sweep, not its fill',
        barOff && !barOff.stripes && !barOff.sweep, barOff);

  // Hover still says "clickable"; it just lands at once, and the press lands
  // on the same color rather than a third, darker one.
  await p.goto(BASE + '/explorer', { waitUntil: 'load' });
  await p.evaluate(() => document.querySelectorAll('.modal.show')
    .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
  await p.evaluate(() => { document.getElementById('btn-cfg-revert').disabled = false; });
  await p.waitForTimeout(300);
  const bg = () => p.evaluate(() =>
    getComputedStyle(document.getElementById('btn-cfg-revert')).backgroundColor);
  const rest = await bg();
  await p.hover('#btn-cfg-revert');
  await p.waitForTimeout(200);
  const hover = await bg();
  await p.mouse.down();
  await p.waitForTimeout(200);
  const press = await bg();
  await p.mouse.up();
  check('hover still changes the button', hover !== rest, { rest, hover });
  check('...and pressing it lands on that same color', press === hover, { hover, press });

  // The glass. Unlike the rest of this list it is not only decoration — a
  // backdrop-filter makes the compositor re-read and re-blur what is behind an
  // element every frame it moves, and Config carries dozens at once, which is
  // what makes a long page feel heavy to scroll on a phone. Counted rather
  // than spot-checked, because the rule has to reach the ones inside pseudo-
  // elements too (both stuck bars paint their backdrop that way).
  const swapOff = await themeSwap();
  check('reduced effects makes light/dark instant, with no cross-fade',
        swapOff.used === 0, swapOff);

  const glassOff = await glass('/config');
  check('reduced effects leaves no blurred layer anywhere on Config',
        glassOff.blurred === 0, glassOff);
  // Dropping the blur alone would leave translucent tints with nothing behind
  // them, so the surfaces have to stay solid-looking, not go see-through.
  check('...and the stuck bars keep an opaque backdrop rather than going clear',
        glassOff.headerLayers >= 2 && glassOff.titleLayers >= 2, glassOff);
  // ── The glass on its own ─────────────────────────────────────────────────
  // Its own switch, because it is the only one of these that costs frames
  // rather than taste. Turning it off must take the blur and NOTHING else: the
  // animations are a separate decision, and someone who wants a page that
  // scrolls well should not have to give them up to get it.
  await setGlassOff(true);
  const soloGlass = await glass('/config');
  check('the standalone switch drops every blurred layer',
        soloGlass.blurred === 0, soloGlass);
  check('...and keeps the stuck bars opaque, same as the broader setting',
        soloGlass.headerLayers >= 2 && soloGlass.titleLayers >= 2, soloGlass);
  const soloLook = await look('/config');
  check('...without stamping the reduced-effects mode on the document',
        soloLook.attr === null, soloLook);
  check('...so the page entrance still animates', soloLook.entrance === 'pr-page-enter', soloLook);
  check('...and the sliding underline is still built', soloLook.slider === true, soloLook);
  const soloSwap = await themeSwap();
  check('...and light/dark still cross-fades',
        !soloSwap.supported || soloSwap.used === 1, soloSwap);

  // Reduce visual effects already covers the blur, so the standalone control
  // has nothing left to do — it is ghosted rather than left live and inert.
  const ghost = async () => {
    await p.goto(BASE + '/config', { waitUntil: 'load' });
    await p.waitForTimeout(400);
    return p.evaluate(() => {
      const cb = document.getElementById('DISABLE_GLASS_BLUR');
      return { disabled: !!cb?.disabled, checked: !!cb?.checked,
               help: document.getElementById('glass-blur-help')?.textContent || '' };
    });
  };
  await setGlassOff(false);
  check('the standalone switch is usable on its own', (await ghost()).disabled === false);

  // ── Ticked, then saved ───────────────────────────────────────────────────
  // Appearance is a cookie, so it COULD apply on the tick. It deliberately
  // does not: every other control on this page waits for Save, and one pair
  // that jumps ahead makes the button look like it missed them. So a tick only
  // arms Save, and the change lands with it.
  const dialogs = [];
  const onDialog = d => { dialogs.push(d.type()); d.accept().catch(() => {}); };
  p.on('dialog', onDialog);
  let reloads = 0;
  const onLoad = () => { reloads++; };
  p.on('load', onLoad);
  await setGlassOff(false);
  await p.goto(BASE + '/config', { waitUntil: 'load' });
  await p.waitForTimeout(400);
  reloads = 0;
  const tick = (id) => p.evaluate((id) => {
    const el = document.getElementById(id);
    el.checked = !el.checked;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, id);
  const state = () => p.evaluate(() => ({
    glass: document.documentElement.getAttribute('data-glass'),
    effects: document.documentElement.getAttribute('data-effects'),
    saveArmed: !document.getElementById('btn-save')?.disabled,
    cookie: /(^|;\s*)mr_glass=off/.test(document.cookie),
  }));

  await tick('DISABLE_GLASS_BLUR');
  await p.waitForTimeout(300);
  const pending = await state();
  check('ticking the blur off does not change the page yet',
        pending.glass === null, pending);
  check('...and does not write the cookie yet', pending.cookie === false, pending);
  check('...but arms Save, so the page says there is something to apply',
        pending.saveArmed === true, pending);

  // Revert is the other half of "waits for Save": a tick you change your mind
  // about has to be undoable without ticking it back.
  await p.evaluate(() => window.revertForm());
  await p.waitForTimeout(300);
  const reverted = await p.evaluate(() => ({
    checked: document.getElementById('DISABLE_GLASS_BLUR').checked,
    saveArmed: !document.getElementById('btn-save')?.disabled,
  }));
  check('Revert puts the tick back and disarms Save',
        reverted.checked === false && reverted.saveArmed === false, reverted);

  await tick('DISABLE_GLASS_BLUR');
  await p.evaluate(() => window.saveConfig());
  await p.waitForTimeout(1500);
  const saved = await state();
  check('saving applies it to the page already on screen',
        saved.glass === 'off', saved);
  check('...and writes the cookie, so the next load is stamped the same way',
        saved.cookie === true, saved);
  check('...without reloading', reloads === 0, { reloads });
  check('...and without a prompt about unsaved data', dialogs.length === 0, dialogs);

  // Turning effects back ON is the direction with something to rebuild: the
  // sliding underline exists by being BUILT, and a page LOADED with effects
  // off never built it. So this has to start from such a page — toggling on a
  // page that loaded with effects on proves nothing, because the underline is
  // already there and stays there whatever the attribute says.
  await setEffects(true);
  await p.goto(BASE + '/config', { waitUntil: 'load' });
  await p.waitForTimeout(400);
  const loadedOff = await p.evaluate(() => ({
    attr: document.documentElement.getAttribute('data-effects'),
    slider: !!document.querySelector('.header-tab-indicator'),
  }));
  check('a page loaded with reduced effects never builds the sliding underline',
        loadedOff.attr === 'off' && loadedOff.slider === false, loadedOff);
  await tick('REDUCE_VISUAL_EFFECTS');
  await p.evaluate(() => window.saveConfig());
  await p.waitForTimeout(1500);
  const cameBack = await p.evaluate(() => ({
    attr: document.documentElement.getAttribute('data-effects'),
    slider: !!document.querySelector('.header-tab-indicator'),
  }));
  check('...and saving it off builds the underline then and there, no reload',
        cameBack.attr === null && cameBack.slider === true, cameBack);
  p.off('dialog', onDialog);
  p.off('load', onLoad);
  await setGlassOff(false);
  await setEffects(true);
  const ghosted = await ghost();
  check('...and ghosted once Reduce visual effects covers it',
        ghosted.disabled === true, ghosted);
  check('...saying so, rather than looking simply broken',
        /already off/i.test(ghosted.help), ghosted);
  check('...without rewriting the preference it would go back to',
        ghosted.checked === false, ghosted);
} finally {
  // Always, even on a thrown assertion: the next test in this suite shares
  // this app, and a stray "off" would make its animation checks vacuous.
  await setDisplay({ mr_effects: false, mr_glass: false })
    .catch(e => { console.log('FAIL could not restore:', e.message); ok = false; });
}

const restored = await look('/');
check('the setting is back off for the rest of the suite',
      restored.attr === null && restored.entrance === 'pr-page-enter', restored);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
