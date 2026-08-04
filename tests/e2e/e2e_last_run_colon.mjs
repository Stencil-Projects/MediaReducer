// The last-run time's colon ticks while a run is going.
//
// It is the clock-face cue that something is live, sitting next to the very
// timestamp the run is about to replace. Three things have to hold: the colon is
// its own element (a text node cannot be animated), it only ticks while a run is
// active, and wrapping it does not change the text — the time is read aloud by
// screen readers and copied by people, and splitting it into elements is exactly
// how a stray space gets introduced.
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

await p.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForFunction(() => typeof setRunning === 'function'
                          && typeof _updateLastRunDisplay === 'function', { timeout: 15000 });

// A fixed epoch so the rendered time is the same on every run: 2026-08-03
// 14:37 UTC. Whatever zone and 12/24h format the page is in, it contains a
// colon between hours and minutes, which is the one being wrapped.
const R = await p.evaluate(() => {
  const el = document.getElementById('last-run-value');
  const read = () => ({
    text: el.textContent,
    colons: el.querySelectorAll('.last-run-colon').length,
    running: el.classList.contains('is-running'),
    anim: getComputedStyle(el.querySelector('.last-run-colon') || el).animationName,
  });
  setRunning(false);
  _updateLastRunDisplay(1785767820, null);
  const idle = read();
  setRunning(true, false, false);
  const running = read();
  // A repaint mid-run must not lose the wrapper or the class.
  _updateLastRunDisplay(1785767820, null);
  const rerendered = read();
  setRunning(false);
  const after = read();
  // Nothing to blink in the em-dash placeholder, and it must not throw.
  _updateLastRunDisplay(null, '—');
  const empty = { text: el.textContent, colons: el.querySelectorAll('.last-run-colon').length };
  _updateLastRunDisplay(1785767820, null);
  return { idle, running, rerendered, after, empty };
});

check('the colon is its own element so it can be animated', R.idle.colons === 1, R.idle);
check('...exactly one of them, not every colon in the line', R.running.colons === 1, R.running);
check('the wrapper does not change the text', R.idle.text === R.running.text, [R.idle.text, R.running.text]);
check('...and the text still reads as a time', /\d:\d\d/.test(R.idle.text), R.idle.text);
check('a run makes it blink', R.running.running === true && R.running.anim === 'pr-last-run-colon',
      R.running);
check('an idle dashboard leaves it still', R.idle.anim === 'none' && !R.idle.running, R.idle);
check('a repaint mid-run keeps it blinking',
      R.rerendered.running === true && R.rerendered.anim === 'pr-last-run-colon', R.rerendered);
check('the run ending stops it', R.after.anim === 'none' && !R.after.running, R.after);
check('a dashboard that has never run has nothing to blink',
      R.empty.colons === 0 && R.empty.text === '—', R.empty);
check('no JS errors', errs.length === 0, errs.slice(0, 3));

await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
