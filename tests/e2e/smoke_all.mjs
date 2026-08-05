const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
let pass = true;
for (const path of ['/', '/config', '/explorer']) {
  const p = await b.newPage();
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  try {
    const resp = await p.goto(BASE + path, { waitUntil: 'networkidle', timeout: 20000 });
    await p.waitForTimeout(1200);
    const status = resp ? resp.status() : 0;
    const bodyLen = (await p.evaluate(() => document.body.innerText.length));
    const nan = await p.evaluate(() => document.body.innerText.includes('NaN'));
    const ok = status === 200 && errs.length === 0 && bodyLen > 100 && !nan;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${path} status=${status} jsErrors=${errs.length} bodyLen=${bodyLen} NaN=${nan}`);
    if (errs.length) console.log('   errors:', JSON.stringify(errs.slice(0, 3)));
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL ${path}: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── Each phase lights its own step, and a failure names its stage ──────────
// The stepper and the log stages are written from ONE call in the engine
// (log_stage), so this mapping is the contract between them. A failure lands as
// one entry in the issue list — reason on the line, stage on the note, red × for
// the marker — and says neither of them anywhere else on the panel.
{
  const p = await b.newPage({ viewport: { width: 1180, height: 1000 } });
  try {
    await p.goto(BASE + '/', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(900);
    const WANT = { checking: 0, library: 1, scanning: 2, simulating: 3, deleting: 3, done: 4 };
    const bad = [];
    for (const [phase, idx] of Object.entries(WANT)) {
      const got = await p.evaluate(({ phase }) => {
        _rpMaxIdx = 0;
        renderProgress({ schema: 1, status: 'running', phase, mode: 'debug_sim',
                         stage: phase.toUpperCase(), scanned: 1, total: 2 });
        return [...document.querySelectorAll('#rp-steps .rp-step')]
          .findIndex(s => s.classList.contains('is-active'));
      }, { phase });
      if (got !== idx) bad.push(`${phase}->${got} want ${idx}`);
    }
    const fail = await p.evaluate(() => {
      _rpMaxIdx = 0;
      renderProgress({ schema: 1, status: 'error', phase: 'library', mode: 'debug_sim',
                       stage: 'READING LIBRARY', message: 'Jellyfin timed out' });
      // Sample the SETTLED page. Once the run log has an errors section the pill
      // becomes clickable, which brings a .15s color transition with it, and a
      // computed color read in the same instant is the colour it is leaving —
      // the pill reported the accent blue it had a frame ago. finish() throws on
      // the header badge's infinite pulse, hence the catch.
      document.getAnimations().forEach(a => { try { a.finish(); } catch {} });
      const top = document.querySelector('#rp-issues li');
      const txt = el => el ? el.textContent : '';
      const color = el => el ? getComputedStyle(el).color : null;
      return { current: document.getElementById('rp-current').textContent,
               head: document.getElementById('rp-msg').textContent,
               fatal: !!top && top.classList.contains('rp-issue-fatal'),
               mark: txt(top && top.querySelector('.rp-issue-mark')),
               line: txt(top && top.querySelector('.rp-issue-line')),
               note: txt(top && top.querySelector('.rp-issue-note')),
               // The failure's line, its ×, and the "Failed" pill all read the
               // same red or the panel is telling three stories about one event.
               lineColor: color(top && top.querySelector('.rp-issue-line')),
               markColor: color(top && top.querySelector('.rp-issue-mark')),
               pillColor: color(document.getElementById('rp-pill')),
               x: [...document.querySelectorAll('#rp-steps .rp-step')]
                    .findIndex(s => s.classList.contains('is-failed')) };
    });
    if (!fail.fatal) bad.push('failure did not lead the issue list');
    if (fail.mark !== '×') bad.push(`failure marker is ${JSON.stringify(fail.mark)}, want ×`);
    if (!/Jellyfin timed out/.test(fail.line)) bad.push('failure lost its reason');
    if (!/Failed during READING LIBRARY/.test(fail.note)) bad.push('failure does not name its stage');
    // Said once. The headline and the under-bar line both used to carry a piece
    // of this, which put the reason and the stage on opposite sides of the
    // stat boxes.
    if (/Jellyfin timed out/.test(fail.head + fail.current)) bad.push('failure repeats the reason');
    if (/Failed during/.test(fail.current)) bad.push('failure repeats its stage');
    if (fail.lineColor !== fail.markColor || fail.lineColor !== fail.pillColor) {
      bad.push(`red disagrees: line ${fail.lineColor} mark ${fail.markColor} pill ${fail.pillColor}`);
    }
    if (fail.x !== 1) bad.push(`x on step ${fail.x}, want 1`);
    const ok = bad.length === 0;
    console.log(`${ok ? 'PASS' : 'FAIL'} stepper phases map to steps ${ok ? '' : JSON.stringify(bad)}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL stepper phases: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── A big run log opens without hanging the tab ────────────────────────────
// The log is one div per line, so a finished run on a large library is tens of
// thousands of boxes. Laying every one of them out cost 2,443ms for 30k lines
// (~2,065ms of it layout), and the 900-line live tail re-renders every 750ms
// during a run at 58ms a go. content-visibility skips layout and paint for the
// off-screen lines. What is asserted is the PROPERTY, not a millisecond count:
// off-screen lines must be skipped, the tail must still land at the bottom,
// and selecting the log must still copy every line — a virtualisation that
// broke copy would make the log useless for bug reports.
{
  const p = await b.newPage({ viewport: { width: 1180, height: 900 } });
  try {
    await p.goto(BASE + '/', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(800);
    await p.evaluate(() => openLogWindow({ load: false }));
    await p.waitForTimeout(400);
    const r = await p.evaluate(() => {
      const term = document.getElementById('log-term');
      const N = 8000;
      _termRenderedText = null;
      _renderTermLines(term, Array.from({ length: N },
        (_, i) => `2026-07-31 04:12:00 - line ${i} MARK_${i}`).join('\n'));
      void term.scrollHeight;
      term.scrollTop = term.scrollHeight;
      const sel = window.getSelection(); sel.removeAllRanges();
      const range = document.createRange(); range.selectNodeContents(term); sel.addRange(range);
      const copied = sel.toString();
      sel.removeAllRanges();
      const cs = getComputedStyle(term.firstElementChild);
      return {
        lines: term.childElementCount,
        skipsOffscreen: cs.contentVisibility === 'auto',
        // Every line copies, including ones never scrolled into view.
        copiedAll: ['MARK_0', 'MARK_4000', `MARK_${N - 1}`].every(m => copied.includes(m)),
        // "Jump to latest" has to reach the real bottom, not an estimated one.
        bottomErrPx: Math.round(Math.abs(term.scrollHeight - term.scrollTop - term.clientHeight)),
        chainsScroll: getComputedStyle(term).overscrollBehaviorY,
      };
    });
    const ok = r.lines === 8000 && r.skipsOffscreen && r.copiedAll
               && r.bottomErrPx <= 2 && r.chainsScroll === 'contain';
    console.log(`${ok ? 'PASS' : 'FAIL'} a big log skips off-screen layout, still copies whole ${ok ? '' : JSON.stringify(r)}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL big log: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── Every run issue starts at the same left edge ───────────────────────────
// The markers are different widths — a "!" is about a third of a "×" — so
// without a fixed-width marker column each issue's text starts at its own x and
// the list reads as ragged. A run that failed puts a "×" at the top of a list
// whose other entries carry "!", which is exactly the mix that goes ragged, so
// the fatal entry is in the sample.
{
  const p = await b.newPage({ viewport: { width: 900, height: 1000 } });
  try {
    await p.goto(BASE + '/', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(900);
    const g = await p.evaluate(() => {
      renderProgress({ schema: 1, status: 'error', phase: 'scanning', mode: 'debug_sim',
        stage: 'SCORING', message: 'Tautulli stopped answering.', issues: [
          { severity: 'error', line: 'Identity mismatch: 43 movies skipped', note: 'A note.' },
          { severity: 'warning', line: 'Cleanup blocked by settings', note: 'Another note.' },
          { severity: 'error', line: 'IMDb ratings download failed' },
        ] });
      const rows = [...document.querySelectorAll('#rp-issues li')];
      const left = el => el ? +el.getBoundingClientRect().left.toFixed(2) : null;
      return { n: rows.length,
               marks: rows.map(li => (li.querySelector('.rp-issue-mark') || {}).textContent),
               lines: rows.map(li => left(li.querySelector('.rp-issue-line'))),
               notes: rows.map(li => left(li.querySelector('.rp-issue-note'))).filter(v => v !== null),
               rules: rows.map(li => parseFloat(getComputedStyle(li).borderTopWidth) || 0) };
    });
    const edges = new Set([...g.lines, ...g.notes]);
    // A rule between issues made the list read as a table; space separates them.
    const ok = g.n === 4 && g.marks[0] === '×' && g.marks.slice(1).every(m => m === '!')
               && edges.size === 1 && g.rules.every(w => w === 0);
    console.log(`${ok ? 'PASS' : 'FAIL'} run issues share one left edge, no dividers ${ok ? '' : JSON.stringify(g)}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL run issue alignment: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── The API-error tab states its case without moving ──────────────────────
// A broken connection is a standing state, not an event, so the tab wears a
// steady red and a dot instead of a pulse — this is the guard against a loop
// coming back to the header. It also has to keep its width when the state
// flips: /api/status turns this on mid-poll, and the sliding underline is
// positioned from offsetWidth, so a wider tab would shunt the bar sideways.
{
  const p = await b.newPage({ viewport: { width: 900, height: 700 } });
  try {
    await p.goto(BASE + '/', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(900);
    const g = await p.evaluate(() => {
      const tab = [...document.querySelectorAll('.header-tabs a')]
        .find(a => a.getAttribute('href') === '/config');
      prSetConfigTabError(false);
      const clean = tab.getBoundingClientRect().width;
      prSetConfigTabError(true);
      // Same reason as the stepper check above: the tab fades into red over
      // .15s, so its colour has to be sampled settled or the dot (which has no
      // transition) is compared against a colour on its way somewhere.
      document.getAnimations().forEach(a => { try { a.finish(); } catch {} });
      const dot = getComputedStyle(tab, '::before');
      return { clean: +clean.toFixed(2), error: +tab.getBoundingClientRect().width.toFixed(2),
               // Keyframe animations only. The flip also starts the tab's
               // color/border transitions, which getAnimations() reports too and
               // which are the point — the tab should fade into red, not blink.
               anims: tab.getAnimations()
                         .filter(a => typeof a.animationName === 'string')
                         .map(a => a.animationName),
               dotSize: dot.width, dotColor: dot.backgroundColor,
               textColor: getComputedStyle(tab).color };
    });
    const bad = [];
    if (g.anims.length) bad.push(`tab animates: ${g.anims.join()}`);
    if (g.dotSize !== '5px') bad.push(`no dot (width ${g.dotSize})`);
    if (g.dotColor !== g.textColor) bad.push(`dot ${g.dotColor} vs text ${g.textColor}`);
    if (g.clean !== g.error) bad.push(`width moves ${g.clean} -> ${g.error}`);
    const ok = bad.length === 0;
    console.log(`${ok ? 'PASS' : 'FAIL'} the API-error tab is static and keeps its width ${ok ? '' : JSON.stringify(bad)}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL API-error tab: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── The corner arrow appears on exactly the boxes that do something ────────
// It is the only affordance that shows without a pointer, which is to say the
// only one a phone ever sees. That makes it a promise: a box wearing the arrow
// and doing nothing when tapped is worse than no arrow at all. A 0-count box
// (here, "Would delete") is inert because its log section has nothing to show.
{
  const p = await b.newPage({ viewport: { width: 900, height: 1000 } });
  try {
    await p.goto(BASE + '/', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(900);
    const g = await p.evaluate(() => {
      // Every log section written, so only the counts decide.
      _logSections = { scan: 1, eligible: 1, deletions: 1, summary: 1, errors: 1 };
      renderProgress({ schema: 1, status: 'done', phase: 'done', mode: 'debug_sim',
        stage: 'DRY RUN SUMMARY', scanned: 2880, eligible: 2201, deleted: 0,
        bytes_freed: 0, message: 'Dry run.' });
      const arrow = el => (getComputedStyle(el, '::after').content || '').includes('↖');
      const box = id => {
        const el = document.getElementById(id);
        return { jumpable: el.classList.contains('log-jumpable'), arrow: arrow(el) };
      };
      // Read the zero-deleted state BEFORE re-rendering: the collision case
      // below needs a non-zero count, which would make the inert box jumpable
      // and quietly turn the "no arrow on an inert box" assertion into a
      // tautology about a box that no longer is one.
      const state = {
        scan: box('rp-jump-scan'),
        eligible: box('rp-jump-eligible'),
        deletions: box('rp-jump-deletions'),   // 0 deleted — inert
        summary: box('rp-jump-summary'),       // exempt from the count rule
        history: [...document.querySelectorAll('.dashboard-history-btn')].map(arrow),
      };

      // …and it must not be painted over the very number it marks. These
      // columns are a quarter of a phone screen wide, so a five-figure count is
      // the widest thing in the box; the arrow sits in a reserved bottom strip.
      renderProgress({ schema: 1, status: 'done', phase: 'done', mode: 'headroom',
        stage: 'SUMMARY', scanned: 128800, eligible: 128800, deleted: 12880,
        bytes_freed: 1.2e12, message: 'done' });
      const collisions = [];
      for (const el of document.querySelectorAll('.rp-stat.log-jumpable')) {
        const b = el.getBoundingClientRect();
        const cs = getComputedStyle(el, '::after');
        const a = { right: b.right - parseFloat(cs.right), bottom: b.bottom - parseFloat(cs.bottom) };
        a.left = a.right - parseFloat(cs.width);
        a.top = a.bottom - parseFloat(cs.height);
        const val = el.querySelector('.rp-stat-val');
        const rng = document.createRange(); rng.selectNodeContents(val);
        const v = rng.getBoundingClientRect();
        if (a.left < v.right && a.right > v.left && a.top < v.bottom && a.bottom > v.top) {
          collisions.push(`${el.id}:${val.textContent}`);
        }
      }
      return { ...state, collisions };
    });
    const bad = [];
    for (const k of ['scan', 'eligible', 'summary']) {
      if (!g[k].jumpable || !g[k].arrow) bad.push(`${k} ${JSON.stringify(g[k])}`);
    }
    if (g.deletions.jumpable || g.deletions.arrow) bad.push(`inert box wears an arrow ${JSON.stringify(g.deletions)}`);
    if (g.history.length !== 2 || !g.history.every(Boolean)) bad.push(`history buttons ${JSON.stringify(g.history)}`);
    if (g.collisions.length) bad.push(`arrow painted over the value: ${g.collisions.join()}`);
    const ok = bad.length === 0;
    console.log(`${ok ? 'PASS' : 'FAIL'} corner arrow marks the clickable boxes only ${ok ? '' : JSON.stringify(bad)}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL corner arrow: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── A theme flip must never leave the page wearing both palettes ───────────
// Only a handful of elements carry the .15s re-skin transition; every other
// surface, border and label repaints at once. On a flip that meant ~150ms of
// mixture — a card that had already turned light sitting on a still-dark page,
// and "Paused" rendered in light text on a light card, i.e. briefly invisible.
// prSetTheme holds transitions off for the swap, so each frame is entirely
// one theme or the other.
{
  const p = await b.newPage({ viewport: { width: 448, height: 900 } });
  try {
    await p.goto(BASE + '/config', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(1000);
    for (let i = 0; i < 20; i++) {
      if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
      await p.keyboard.press('Escape');
      await p.waitForTimeout(200);
    }
    await p.evaluate(() => prSetTheme('dark'));
    await p.waitForTimeout(350);
    const sample = () => p.evaluate(() => {
      const g = (sel, prop) => { const e = document.querySelector(sel);
        return e ? getComputedStyle(e)[prop] : null; };
      return { page: g('html', 'backgroundColor'), header: g('.site-header', 'backgroundColor'),
               card: g('.run-mode-card', 'backgroundColor'),
               label: g('.run-mode-name', 'color') };
    });
    // Chrome returns rgb()/rgba() in 0-255 and color(srgb ...) in 0-1, and a
    // transparent color means "nothing painted" rather than "black" — reading
    // either wrong would invent a mismatch that isn't there.
    const lum = (c) => {
      const str = String(c);
      const m = str.match(/-?\d*\.?\d+(?:e[-+]?\d+)?/gi);
      if (!m) return null;
      const n = m.map(Number);
      const alpha = n.length > 3 ? n[3] : 1;
      if (!(alpha > 0.05)) return null;
      const scale = /^color\(/i.test(str) ? 1 : 255;
      return (0.2126 * n[0] + 0.7152 * n[1] + 0.0722 * n[2]) / scale;
    };
    await p.evaluate(() => prToggleTheme());
    const bad = [];
    for (let i = 0; i < 8; i++) {
      const f = await sample();
      const pageL = lum(f.page);
      if (pageL !== null) {
        for (const key of ['header', 'card']) {
          const L = lum(f[key]);
          if (L !== null && (L > 0.5) !== (pageL > 0.5)) bad.push(`${key} vs page @${i}`);
        }
        const tl = lum(f.label), cb = lum(f.card);
        if (tl !== null && cb !== null && Math.abs(tl - cb) < 0.12) bad.push(`label on card @${i}`);
      }
      await p.waitForTimeout(25);
    }
    const ok = bad.length === 0;
    console.log(`${ok ? 'PASS' : 'FAIL'} theme flip stays one palette ${ok ? '' : JSON.stringify(bad.slice(0, 4))}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL theme flip: ${e.message}`);
    pass = false;
  }
  await p.close();
}

// ── The sticky header and the sticky title bar must meet with no gap ───────
// The title bar sticks at whatever --pr-header-h says the header is tall. That
// used to be offsetHeight, which is rounded to whole pixels: the stacked phone
// header is 86.72px and reported 87, so the bar sat a quarter-pixel too low and
// scrolling content showed through the seam as a hairline of text. The header
// paints above the bar, so lapping UNDER it is invisible — only a gap shows.
for (const w of [390, 448]) {
  const ctx = await b.newContext({ viewport: { width: w, height: 820 }, deviceScaleFactor: 3 });
  const p = await ctx.newPage();
  try {
    await p.goto(BASE + '/config', { waitUntil: 'load', timeout: 20000 });
    await p.waitForTimeout(900);
    // A fresh install opens the welcome dialog; dismiss it properly, because
    // merely removing the node leaves body{overflow:hidden}, which silently
    // disables position:sticky and would make this test pass for the wrong
    // reason.
    for (let i = 0; i < 20; i++) {
      if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
      await p.keyboard.press('Escape');
      await p.waitForTimeout(200);
    }
    await p.evaluate(() => window.scrollBy(0, 900));
    await p.waitForTimeout(400);
    const g = await p.evaluate(() => {
      const hd = document.querySelector('.site-header');
      const bar = document.querySelector('.page-title-bar');
      if (!hd || !bar) return null;
      const hr = hd.getBoundingClientRect(), br = bar.getBoundingClientRect();
      return { headerStuck: Math.abs(hr.top) < 1, stuck: bar.classList.contains('is-stuck'),
               gap: +(br.top - hr.bottom).toFixed(3) };
    });
    // Guard the guard: if the header is not actually stuck there is nothing to
    // measure, and a "gap of 0" would mean nothing.
    const ok = g && g.headerStuck && g.stuck && g.gap <= 0;
    console.log(`${ok ? 'PASS' : 'FAIL'} header/title-bar seam@${w} ${JSON.stringify(g)}`);
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL header/title-bar seam@${w}: ${e.message}`);
    pass = false;
  }
  await p.close();
  await ctx.close();
}

// ── Phone-width sanity: no page may scroll horizontally at common smartphone
// widths, and the dashboard's Cleanup Targets word-values (e.g. "Disabled")
// must not break mid-word into a second line. 320 = smallest common (SE-class),
// 360 = most common Android, 390 = iPhone 12-15.
for (const w of [320, 360, 390]) {
  const ctx = await b.newContext({ viewport: { width: w, height: 780 } });
  for (const path of ['/', '/config', '/explorer']) {
    const p = await ctx.newPage();
    try {
      await p.goto(BASE + path, { waitUntil: 'load', timeout: 20000 });
      await p.waitForTimeout(700);
      const overflow = await p.evaluate(() =>
        Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth);
      const ok = overflow <= 1;
      console.log(`${ok ? 'PASS' : 'FAIL'} ${path}@${w} horizontal-overflow=${overflow}px`);
      pass = pass && ok;
      if (path === '/') {
        // Each Cleanup Targets value must sit on ONE line (a mid-word break
        // like "Disable d" doubles the element's height past one line-box).
        const wrapped = await p.evaluate(() => {
          const bad = [];
          for (const id of ['target-row-headroom', 'target-row-redline']) {
            const v = document.querySelector('#' + id + ' .value');
            if (!v) continue;
            const lh = parseFloat(getComputedStyle(v).lineHeight) || 24;
            if (v.clientHeight > lh * 1.6) bad.push(id + ':' + v.textContent.trim());
          }
          return bad;
        });
        const vok = wrapped.length === 0;
        console.log(`${vok ? 'PASS' : 'FAIL'} cleanup-target values single-line@${w}${vok ? '' : ' — ' + wrapped.join(', ')}`);
        pass = pass && vok;
      }
    } catch (e) {
      console.log(`FAIL ${path}@${w}: ${e.message}`);
      pass = false;
    }
    await p.close();
  }
  await ctx.close();
}
// ── A ghosted checkbox's label does not react to the pointer ────────────────
// The Explorer under the Plex-only fixture has one of each: "Don't delete
// unplayed movies" is live, and the Jellyfin favorites toggle is disabled
// because Jellyfin isn't connected. The disabled one used to recolor on hover
// anyway, which reads as clickable on a control that does nothing.
{
  const p = await b.newPage();
  try {
    await p.goto(BASE + '/explorer', { waitUntil: 'networkidle', timeout: 20000 });
    const label = id => `label.filter-score-toggle-row[for="${id}"] .form-check-label`;
    const colorOf = id => p.evaluate(s => getComputedStyle(document.querySelector(s)).color, label(id));

    // Park the pointer away from both rows so neither starts out hovered.
    await p.mouse.move(0, 0);
    await p.waitForTimeout(250);
    const restLive = await colorOf('c-unplayed');
    const restDead = await colorOf('c-jf-fav');

    await p.hover(label('c-unplayed'));
    await p.waitForTimeout(250);                 // the color transition is .15s
    const hotLive = await colorOf('c-unplayed');

    await p.hover(label('c-jf-fav'));
    await p.waitForTimeout(250);
    const hotDead = await colorOf('c-jf-fav');

    const disabled = await p.evaluate(() => document.getElementById('c-jf-fav')?.disabled);
    // The live row proves the hover is real; without it, "disabled doesn't
    // change" would also pass if hovering had quietly stopped working.
    const ok = disabled === true && hotLive !== restLive && hotDead === restDead;
    console.log(`${ok ? 'PASS' : 'FAIL'} a disabled toggle row's label ignores hover` +
                (ok ? '' : ` — disabled=${disabled} live ${restLive}->${hotLive} dead ${restDead}->${hotDead}`));
    pass = pass && ok;
  } catch (e) {
    console.log(`FAIL disabled toggle hover: ${e.message}`);
    pass = false;
  }
  await p.close();
}

await b.close();
console.log('RESULT:', pass ? 'PASS' : 'FAIL');
process.exit(pass ? 0 : 1);
