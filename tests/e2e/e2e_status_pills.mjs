// The three status pills — the header badge, the dashboard's run pill, and Last
// Run's mode pill — all answer "what is happening, or what is armed", and each
// one is supposed to wear the color of the BUTTON that does that thing: red is
// the Cleanup button, blue is the primary action button, neutral is Simulate.
//
// They were written independently and drifted: the header badge was blue for a
// simulation and a dimmer red for a Cleanup than the Cleanup button, the run
// pill used a third red again, and Last Run showed the mode that DELETES in
// green. This asserts the pill and its button resolve to the same border, text
// and fill, so a token changed on one side cannot quietly leave the other
// behind.
//
// Colors are compared as numbers, not strings: the same token serializes as
// rgba() on one element and color(srgb …) or oklab() on another, so an equal
// pair fails a string compare. A canvas 2D context resolves any CSS color to
// RGBA, which is the one comparison that means what it looks like.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage({ viewport: { width: 1180, height: 900 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));

let ok = true;
const check = (name, cond, extra = '') => {
  if (!cond) ok = false;
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra)));
};

try {
  await p.goto(BASE + '/', { waitUntil: 'load', timeout: 25000 });
  await p.waitForFunction(() => typeof renderProgress === 'function' && !!window.prRunTone,
                          { timeout: 15000 });
  await p.waitForTimeout(700);

  for (const theme of ['dark', 'light']) {
    await p.evaluate(t => prSetTheme(t), theme);
    // Past the .15s re-skin transition AND the pills' own border transition:
    // a color read mid-transition is neither the old one nor the new one.
    await p.waitForTimeout(450);

    const r = await p.evaluate(async () => {
      const cv = document.createElement('canvas');
      cv.width = cv.height = 1;
      const ctx = cv.getContext('2d', { willReadFrequently: true });
      // Any CSS color in, RGBA out. Painted over a known backdrop so a
      // translucent fill still compares as what the eye receives.
      const rgba = (css) => {
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = '#808080';
        ctx.fillRect(0, 0, 1, 1);
        ctx.fillStyle = css;
        ctx.fillRect(0, 0, 1, 1);
        return [...ctx.getImageData(0, 0, 1, 1).data];
      };
      const skin = (el) => {
        const c = getComputedStyle(el);
        return { bd: rgba(c.borderTopColor), fg: rgba(c.color), bg: rgba(c.backgroundColor),
                 flat: c.boxShadow === 'none' };
      };
      const settle = () => new Promise(res =>
        requestAnimationFrame(() => setTimeout(res, 220)));
      // Both pills are also written by the page's own status poll, which fires
      // every 4s and would overwrite a state set here mid-settle. Reading a
      // color needs the transition to finish, so re-apply and re-read until
      // the tone we asked for is still the one on the element.
      const stable = async (apply, el, tone) => {
        for (let i = 0; i < 6; i++) {
          apply();
          await settle();
          if (el.classList.contains(tone)) return skin(el);
        }
        throw new Error('status poll kept overwriting ' + tone);
      };

      // The reference buttons, read in their LIVE look — a fresh install ships
      // them disabled, and .btn-outline-secondary:disabled recolours the label.
      for (const id of ['btn-cleanup', 'btn-sim']) {
        const e = document.getElementById(id); if (e) e.disabled = false;
      }
      await settle();
      const buttons = {
        cleanup: skin(document.getElementById('btn-cleanup')),
        simulate: skin(document.getElementById('btn-sim')),
        primary: skin(document.querySelector('.modal-footer .btn-outline-primary')),
      };

      const badge = document.querySelector('.header-run-badge.run-badge-desktop');
      const pill = document.getElementById('rp-pill');
      const link = document.querySelector('.dashboard-mode-link');
      const out = { buttons, header: {}, run: {}, mode: {} };

      const CASES = [
        ['cleanup', { cleanup: true }, 'headroom'],
        ['debugCleanup', { debugCleanup: true }, 'debug_cleanup'],
        ['manualSim', { manual: true }, 'debug_sim'],
        ['scheduled', {}, 'debug_sim'],
      ];
      const TONE = { cleanup: 'is-danger', debugCleanup: 'is-warning',
                     manualSim: 'is-accent', scheduled: 'is-accent' };
      for (const [key, s, mode] of CASES) {
        const setHeader = () => prSetHeaderRunning(true, s.cleanup, s.debugCleanup);
        // The label is written synchronously, so read it in the same tick the
        // call returns — no window for the poll to slip in.
        setHeader();
        const headLabel = badge.querySelector('.run-badge-label').textContent;
        out.header[key] = { ...(await stable(setHeader, badge, TONE[key])),
                            label: headLabel, tone: TONE[key] };
        const setRun = () => {
          _rpMaxIdx = 0;
          renderProgress({ schema: 1, status: 'running', phase: 'scanning', mode,
                           manual: !!s.manual, stage: 'SCORING' });
        };
        setRun();
        const runLabel = pill.textContent;
        out.run[key] = { ...(await stable(setRun, pill, TONE[key])), label: runLabel };
      }
      // Idle is the server-rendered resting state, not one renderProgress paints.
      const setIdle = () => { pill.className = 'pr-pill pr-pill--state rp-pill is-neutral'; };
      out.run.idle = await stable(setIdle, pill, 'is-neutral');

      for (const [key, mode] of [['auto', 'headroom'], ['monitor', 'paused'], ['paused', 'off']]) {
        const tone = mode === 'headroom' ? 'is-danger' : 'is-neutral';
        out.mode[key] = await stable(() => {
          link.classList.remove('is-danger', 'is-neutral');
          link.classList.add(tone);
        }, link, tone);
      }
      return out;
    });

    // Two channels within 2/255 is "the same color" — enough slack for a
    // rounding difference between color spaces, far tighter than any of the
    // mismatches this replaces (the old reds were 40+ apart).
    const same = (a, c) => a && c && a.every((v, i) => Math.abs(v - c[i]) <= 2);
    const sameSkin = (a, c) => same(a.bd, c.bd) && same(a.fg, c.fg);
    const wears = (label, pillSkin, btn) => {
      check(`${theme}: ${label} wears its button's border`, same(pillSkin.bd, btn.bd), [pillSkin.bd, btn.bd]);
      check(`${theme}: ${label} wears its button's text color`, same(pillSkin.fg, btn.fg), [pillSkin.fg, btn.fg]);
      check(`${theme}: ${label} wears its button's fill`, same(pillSkin.bg, btn.bg), [pillSkin.bg, btn.bg]);
    };

    wears('a running Cleanup in the header', r.header.cleanup, r.buttons.cleanup);
    wears('a running Cleanup in the run pill', r.run.cleanup, r.buttons.cleanup);
    wears('Automatic Cleanup in Last Run', r.mode.auto, r.buttons.cleanup);

    wears('a manual Simulate in the header', r.header.manualSim, r.buttons.primary);
    wears('a manual Simulate in the run pill', r.run.manualSim, r.buttons.primary);
    wears('a scheduled run in the header', r.header.scheduled, r.buttons.primary);
    wears('a scheduled run in the run pill', r.run.scheduled, r.buttons.primary);

    wears('Idle in the run pill', r.run.idle, r.buttons.simulate);
    wears('Monitor Only in Last Run', r.mode.monitor, r.buttons.simulate);
    wears('Paused in Last Run', r.mode.paused, r.buttons.simulate);

    // The three pills report the same run, so they must not word it differently.
    for (const key of ['cleanup', 'debugCleanup', 'manualSim', 'scheduled']) {
      check(`${theme}: the header and the run pill agree on "${key}"`,
            r.header[key].label === r.run[key].label, [r.header[key].label, r.run[key].label]);
    }
    // Blue says "Running" however the run started — what this badge is for is
    // whether something is happening and whether it deletes, and who pressed the
    // button changes neither. The run-progress card names the trigger itself.
    check(`${theme}: a manual Simulate says "Running"`,
          r.run.manualSim.label === 'Running', r.run.manualSim.label);
    check(`${theme}: and so does a scheduled run`,
          r.run.scheduled.label === 'Running', r.run.scheduled.label);
    // Red is reserved for the run that deletes, and it has to SAY that. "Running"
    // in red is the alarm without the information — it describes every run.
    check(`${theme}: a run that deletes says "Cleaning", not "Running"`,
          r.run.cleanup.label === 'Cleaning', r.run.cleanup.label);
    check(`${theme}: nothing that deletes nothing wears the Cleanup red`,
          ['manualSim', 'scheduled', 'debugCleanup']
            .every(k => r.run[k].label !== 'Cleaning'
                     && !sameSkin(r.run[k], r.buttons.cleanup)),
          ['manualSim', 'scheduled', 'debugCleanup'].map(k => r.run[k].label));
    check(`${theme}: a Debug Cleanup says "Debugging"`,
          r.run.debugCleanup.label === 'Debugging', r.run.debugCleanup.label);

    // Flat, not glassy: the inset highlight belongs to things you press.
    const glassy = [['header', r.header.cleanup], ['run pill', r.run.cleanup],
                    ['Last Run', r.mode.auto]].filter(([, s]) => !s.flat).map(([n]) => n);
    check(`${theme}: the status pills carry no button sheen`, glassy.length === 0, glassy);
  }

  // ── A page loaded MID-RUN must open on the right badge ───────────────────
  // The dashboard was rendered with run_active and nothing else, so the boot
  // path called setRunning(true) with no run kind: the badge opened as a blue
  // "Running" whatever was happening and corrected itself on the first status
  // poll ~3s later. Moving between tabs during a run replayed that every time,
  // and during a real Cleanup the red "files are going away" cue was missing
  // for those seconds. The kind is server-rendered now.
  const boot = await p.evaluate(async () => {
    const d = await (await fetch('/api/status?_=' + Date.now(), { cache: 'no-store' })).json();
    const out = {
      types: [typeof _bootRunCleanup, typeof _bootRunDebugCleanup],
      rendered: [_bootRunCleanup, _bootRunDebugCleanup],
      api: [!!(d.run_active && d.run_cleanup), !!(d.run_active && d.run_debug_cleanup)],
      byKind: {},
    };
    // setRunning is what the boot path calls; drive it the way each kind of run
    // would and read what the header ends up saying.
    for (const [n, c, g] of [['cleanup', true, false],
                             ['debugCleanup', false, true],
                             ['other', false, false]]) {
      setRunning(true, c, g);
      const el = document.querySelector('.run-badge-desktop');
      out.byKind[n] = el.querySelector('.run-badge-label').textContent.trim()
        + ':' + [...el.classList].find(x => x.startsWith('is-'));
    }
    setRunning(false);
    return out;
  });
  check('the dashboard is rendered with the run KIND, not just run_active',
        boot.types.every(t => t === 'boolean'), boot.types);
  check('the boot path paints a Cleanup red and named',
        boot.byKind.cleanup === 'Cleaning:is-danger', boot.byKind.cleanup);
  check('...a Debug Cleanup yellow', boot.byKind.debugCleanup === 'Debugging:is-warning',
        boot.byKind.debugCleanup);
  check('...and every run that deletes nothing blue', boot.byKind.other === 'Running:is-accent',
        boot.byKind.other);

  // The above only proves setRunning honours what it is GIVEN. The bug was the
  // boot call not giving it anything, so drive the real thing: serve the page
  // as it renders mid-manual-Simulate and watch the badge from the first paint.
  // No engine is started — the served HTML is rewritten, which is exactly the
  // input the boot path reads — so this costs nothing and cannot race a run.
  for (const [name, patch, want] of [
    ['a Simulate', h => h, 'Running:is-accent'],
    ['a Cleanup', h => h.replace(/const _bootRunCleanup\s*=\s*false;/, 'const _bootRunCleanup = true;'),
     'Cleaning:is-danger'],
  ]) {
    const q = await b.newPage({ viewport: { width: 1180, height: 900 } });
    // ONE handler: Playwright runs the most recently registered matching route
    // first, so a second catch-all would swallow /api/status before a dedicated
    // status route ever saw it.
    await q.route('**/*', async (route) => {
      // The status poll rewrites the badge within ~150ms of load, so leave it
      // hanging: whatever the badge says afterwards can ONLY have come from the
      // boot path, which is the thing under test.
      if (/\/api\/status/.test(route.request().url())) return;   // never answers
      if (route.request().resourceType() !== 'document') return route.continue();
      const res = await route.fetch();
      // Fulfil with fresh headers: inheriting the original response carries its
      // Content-Encoding, and the browser then parses the ORIGINAL body — the
      // rewrite goes out on the wire and changes nothing.
      const html = patch((await res.text()).replace(/let _active\s*=\s*false;/, 'let _active = true;'));
      await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: html });
    });
    await q.goto(BASE + '/', { waitUntil: 'commit', timeout: 25000 });
    await q.waitForTimeout(700);
    const got = await q.evaluate(() => {
      const h = document.querySelector('.site-header');
      const el = document.querySelector('.run-badge-desktop');
      if (!h || !el) return 'no badge';
      if (!h.classList.contains('is-running')) return 'hidden';
      return el.querySelector('.run-badge-label').textContent.trim()
        + ':' + [...el.classList].find(x => x.startsWith('is-'));
    }).catch(e => 'threw: ' + e.message);
    check(`a page loaded mid-run opens on the right badge — ${name}`, got === want, { got, want });
    await q.close();
  }

  // ── The header badge holds still under the pointer ─────────────────────────
  // It is a link only so the run is one click away — it reports what the app is
  // doing rather than offering to do something, and it lives in the chrome
  // rather than beside the thing it describes. The mode pill beside it is the
  // control that still reacts, so a badge that stopped moving proves the rule
  // was aimed, not that hover went missing from every pill at once.
  await p.evaluate(() => prSetHeaderRunning(true, false, false));
  const borderOf = (sel) => p.evaluate((q) => {
    // The badge pulses forever; .finish() throws on an infinite animation, and
    // a border read mid-transition is neither the old color nor the new one.
    document.getAnimations().forEach(a => { try { a.finish(); } catch (_) {} });
    return getComputedStyle(document.querySelector(q)).borderTopColor;
  }, sel);
  // force: the pulse never settles, so the default actionability wait would
  // sit here until it timed out.
  const hoverBorders = async (sel) => {
    await p.mouse.move(0, 0);
    const rest = await borderOf(sel);
    await p.hover(sel, { force: true });
    await p.waitForTimeout(250);
    const hovered = await borderOf(sel);
    await p.mouse.move(0, 0);
    return [rest, hovered];
  };
  const [badgeRest, badgeHover] = await hoverBorders('.run-badge-desktop');
  const [modeRest, modeHover] = await hoverBorders('.dashboard-mode-link');
  check('the header run badge does not react to hover',
        badgeRest === badgeHover, [badgeRest, badgeHover]);
  check('...while a pill that IS a control still does',
        modeRest !== modeHover, [modeRest, modeHover]);

  check('no JS errors', errs.length === 0, errs.slice(0, 3));
} catch (e) {
  console.log('FAIL status pills:', e.message);
  ok = false;
}

await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
