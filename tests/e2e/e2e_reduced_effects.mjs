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
// Flipped through the real config API, and restored in a finally: this shares
// its app with every other browser test in the suite, and one left on would
// quietly turn e2e_nav_transition's entrance into a no-op.
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

async function setEffects(reduce) {
  // Absolute, and from a loaded page: a relative fetch on about:blank has no
  // base to resolve against, which is where this ran before the first goto.
  if (!p.url().startsWith(BASE)) {
    await p.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
  }
  const r = await p.evaluate(async ([val, base]) => {
    const cur = await fetch(base + '/api/config').then(r2 => r2.json());
    cur.REDUCE_VISUAL_EFFECTS = val;
    const res = await fetch(base + '/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cur),
    });
    return { status: res.status, body: await res.json().catch(() => null) };
  }, [reduce, BASE]);
  if (r.status !== 200 || !(r.body && r.body.ok !== false)) {
    throw new Error('could not set REDUCE_VISUAL_EFFECTS: ' + JSON.stringify(r));
  }
}

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
} finally {
  // Always, even on a thrown assertion: the next test in this suite shares
  // this app, and a stray "off" would make its animation checks vacuous.
  await setEffects(false).catch(e => { console.log('FAIL could not restore:', e.message); ok = false; });
}

const restored = await look('/');
check('the setting is back off for the rest of the suite',
      restored.attr === null && restored.entrance === 'pr-page-enter', restored);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
