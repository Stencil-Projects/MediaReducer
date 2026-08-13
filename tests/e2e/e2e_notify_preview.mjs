// The Notifications section's Debug button pops the notification preview.
//
// The server side — content, section applicability, and above all that a
// preview never consumes a pending alert's trigger — is pinned by
// test_notify_preview.py. This drives the one part only a browser exercises:
// the button next to Send Test Notification runs the real fetch and lands the
// text in the standard debug box, with Copy/Download live. Both buttons act on
// the SAVED config and ghost from the server's verdicts about it, so nothing
// here posts a form overlay.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
let ok = true;
const errs = [];
p.on('pageerror', e => errs.push(e.message));

const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${cond ? '' : ' — ' + extra}`);
  ok = ok && cond;
};

await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });

const layout = await p.evaluate(() => {
  const test = document.getElementById('btn-notify-test');
  const dbg = document.getElementById('btn-notify-preview');
  return { both: !!(test && dbg),
           siblings: !!(test && dbg && test.parentElement === dbg.parentElement) };
});
check('the Debug button sits beside Send Test Notification',
      layout.both && layout.siblings, JSON.stringify(layout));

// A fresh install opens the welcome dialog over everything.
for (let i = 0; i < 20; i++) {
  if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
  await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
}
// The Notifications section ships collapsed; open it the way a user does.
await p.click('button[data-bs-target="#s-notify"]');

// ── It is a debug tool, so it lives and dies with Debug mode ────────────────
// Same rule as every other debug control (.debug-mode-control), and keyed to
// the SAVED value like the rest of this page. This fixture has Debug mode off,
// which is where the button has to be absent.
check('with Debug mode off the button is hidden, like the other debug controls',
      await p.evaluate(() => document.getElementById('btn-notify-preview')
        ?.classList.contains('d-none')) === true);
check('...and Send test notification is NOT hidden with it',
      await p.evaluate(() => document.getElementById('btn-notify-test')
        ?.classList.contains('d-none')) === false);

// Turn Debug mode on the way a save does, and it appears.
await p.evaluate(() => {
  _savedConfig = Object.assign({}, _savedConfig, { DEBUG_MODE: true });
  syncDebugModeVisibility();
});
check('turning Debug mode on reveals it',
      await p.evaluate(() => document.getElementById('btn-notify-preview')
        ?.classList.contains('d-none')) === false);

await p.waitForSelector('#btn-notify-preview', { state: 'visible', timeout: 10000 });
await p.click('#btn-notify-preview');
let box = null;
for (let waited = 0; waited < 8000; waited += 150) {
  box = await p.evaluate(() => ({
    shown: !!document.getElementById('pr-debug-overlay')?.classList.contains('show'),
    title: document.getElementById('pr-debug-title')?.textContent || '',
    text: document.getElementById('pr-debug-text')?.textContent || '',
    copyLive: !document.getElementById('pr-debug-copy')?.disabled,
  }));
  if (box.shown && box.text && !/Running debug/.test(box.text)) break;
  await p.waitForTimeout(150);
}
check('clicking it opens the debug box with the preview',
      box?.shown && /RUN SUMMARY/.test(box.text), (box?.text || '').slice(0, 120));
check('...titled as the notification preview',
      /Notification preview/i.test(box?.title || ''), box?.title);
check('...with Copy/Download live once loaded', box?.copyLive === true);
// The fixture has no completed run or a real one — either way the summary
// section explains itself rather than erroring.
check('the summary section is present in some form',
      /no completed run report|rebuilt from the last completed run/.test(box?.text || ''),
      (box?.text || '').slice(0, 160));

// ── Style: it is a debug button, so it wears the debug button's look ────────
const style = await p.evaluate(() => {
  const mine = document.getElementById('btn-notify-preview');
  const other = document.getElementById('btn-debug-tautulli-movies');
  const cls = c => [...c.classList].filter(x => x !== 'debug-mode-control' && x !== 'd-none').sort().join(' ');
  return { mine: cls(mine), other: other ? cls(other) : null,
           color: getComputedStyle(mine).color,
           otherColor: other ? getComputedStyle(other).color : null };
});
check('the Debug button carries the debug-button classes',
      /btn-outline-warning/.test(style.mine) && /pr-debug-btn/.test(style.mine), style.mine);
if (style.other) {
  check('...the same set the API debug buttons use', style.mine === style.other,
        `${style.mine} vs ${style.other}`);
}

// ── The master switch dims the settings, not the two buttons ────────────────
// Turning notifications off greys the fields because they describe alerts that
// aren't going out. Both buttons still work then — verifying destinations
// before switching on, and reading a message the switch is why you never got —
// so neither may be dimmed with them.
const dim = await p.evaluate(() => {
  const sw = document.getElementById('NOTIFY_ENABLED');
  sw.checked = false;
  syncNotifyEnabled();
  // ANCESTOR opacity only. A button that is itself disabled carries Bootstrap's
  // own greying, and that is a different statement ("you can't press this")
  // from the one under test ("this section is off") — measuring the element's
  // own opacity too would conflate them and pass on the wrong reason.
  const opacityOf = el => {
    let o = 1;
    for (let n = el.parentElement; n && n !== document.documentElement; n = n.parentElement) {
      o *= parseFloat(getComputedStyle(n).opacity);
    }
    return o;
  };
  const fieldOpacity = opacityOf(document.getElementById('NOTIFY_PUSHOVER_USER_KEY'));
  const r = { fieldOpacity,
              test: opacityOf(document.getElementById('btn-notify-test')),
              preview: opacityOf(document.getElementById('btn-notify-preview')) };
  sw.checked = true;
  syncNotifyEnabled();
  return r;
});
check('notifications off dims the settings', dim.fieldOpacity < 0.9, JSON.stringify(dim));
check('...but not Send test notification', dim.test > 0.99, JSON.stringify(dim));
check('...and not Debug', dim.preview > 0.99, JSON.stringify(dim));

// ── Each button ghosts on its own emptiness, from the server's verdict ──────
const ghosts = await p.evaluate(() => {
  _notifyPreviewReason = 'Nothing to preview yet — run Simulate.';
  _notifyTestReason = 'Add a notification service.';
  _applyNotifyButtonState();
  const test = document.getElementById('btn-notify-test');
  const prev = document.getElementById('btn-notify-preview');
  const out = { testOff: test.disabled, testTip: test.title,
                prevOff: prev.disabled, prevTip: prev.title };
  _notifyPreviewReason = '';
  _notifyTestReason = '';
  _applyNotifyButtonState();
  out.testOn = !test.disabled;
  out.prevOn = !prev.disabled;
  out.prevTipBack = prev.title;
  return out;
});
check('no destinations ghosts Send test notification', ghosts.testOff, JSON.stringify(ghosts));
check('...saying what to add', /Add a notification service/.test(ghosts.testTip), ghosts.testTip);
check('nothing to preview ghosts Debug', ghosts.prevOff, JSON.stringify(ghosts));
check('...naming the Simulate to run', /Simulate/.test(ghosts.prevTip), ghosts.prevTip);
check('both come back when the server says there is something',
      ghosts.testOn && ghosts.prevOn, JSON.stringify(ghosts));
check('...and Debug gets its descriptive tooltip back',
      /never sent/.test(ghosts.prevTipBack), ghosts.prevTipBack);

check('no page errors', errs.length === 0, errs.join(' | '));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
