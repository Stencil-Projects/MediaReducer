// Scrolling a slider-heavy page with a finger must not move a slider.
//
// Two things stop it. `input[type=range] { touch-action: pan-y }` hands
// vertical gestures to the page scroller, and prGuardRangeTouchScroll in
// base.html remembers the value at touchstart and puts it back when the
// gesture turns out to be vertical — some mobile browsers jump the thumb on
// the initial touch before deciding the gesture was a scroll.
//
// Both live in base.html: the rule is a bare element selector and the guard is
// delegated off document, so every slider on every page is covered by
// construction. This test is here so that stays true. It does not name the
// sliders it checks — it sweeps every page for input[type=range] and holds
// each one it finds to the same contract, so a slider added later is covered
// the day it lands rather than the day someone remembers to add it here.
//
// The browser's own value jump can't be provoked headlessly, so each gesture
// simulates it the way the browser does: set .value between touchstart and
// touchmove, then check what the guard leaves behind. A horizontal drag is
// checked in the same breath — a guard that ate deliberate slides would pass
// every vertical case and still be broken.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

const p = await b.newPage({ viewport: { width: 420, height: 780 }, hasTouch: true });
const errs = [];
p.on('pageerror', e => errs.push(e.message));

async function load(path) {
  for (let i = 0; i < 5; i++) {
    try { await p.goto(BASE + path, { waitUntil: 'load', timeout: 45000 }); return true; }
    catch (_) { await p.waitForTimeout(1000); }
  }
  return false;
}

// One touch gesture against one slider, run entirely in the page so the
// synthetic TouchEvents reach the capture-phase listeners the guard installs.
// Returns what the value did, plus whether an input event was re-fired — the
// live score preview reads the sliders through that event, so a silent restore
// would leave the page showing a number no slider is set to.
const gesture = (id, dx, dy) => p.evaluate(([id, dx, dy]) => {
  const el = document.getElementById(id);
  const before = el.value;
  // A value the browser could plausibly jump to, and never the current one:
  // step off the midpoint, then off the other way if that IS the current one.
  const min = Number(el.min || 0), max = Number(el.max || 100);
  const step = Number(el.step) || 1;
  let jumped = String(Math.round(((min + max) / 2) / step) * step);
  if (jumped === before) jumped = String(Math.min(max, Number(before) + step));
  if (jumped === before) jumped = String(Math.max(min, Number(before) - step));

  const r = el.getBoundingClientRect();
  const x0 = r.left + r.width / 2, y0 = r.top + r.height / 2;
  const touch = (x, y) => new Touch({ identifier: 1, target: el, clientX: x, clientY: y });
  const fire = (type, x, y) => {
    const t = [touch(x, y)];
    el.dispatchEvent(new TouchEvent(type, {
      bubbles: true, cancelable: true, touches: t, targetTouches: t, changedTouches: t }));
  };

  let inputs = 0;
  const count = () => { inputs++; };
  fire('touchstart', x0, y0);
  // The jump the browser applies before it has classified the gesture. Counted
  // from here so `inputs` reflects only what the GUARD dispatched.
  el.value = jumped;
  // Read it back: a fractional step snaps the assignment (3.3000000000000003 →
  // 3.3), and comparing against the unsnapped number would fail the horizontal
  // case on a slider the guard handled perfectly.
  jumped = el.value;
  el.addEventListener('input', count);
  fire('touchmove', x0 + dx, y0 + dy);
  const afterMove = el.value;
  fire('touchend', x0 + dx, y0 + dy);
  const after = el.value;
  el.removeEventListener('input', count);
  el.value = before;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return { before, jumped, afterMove, after, inputs };
}, [id, dx, dy]);

const PAGES = [['/', 'Dashboard'], ['/config', 'Configuration'], ['/explorer', 'Filtering & Scoring']];
let total = 0;

for (const [path, label] of PAGES) {
  if (!await load(path)) { check(`loaded ${path}`, false); continue; }
  await p.evaluate(() => document.querySelectorAll('.modal.show')
    .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
  // Sliders inside a collapsed accordion are still real controls a finger can
  // land on once the section is open, so open everything before sweeping.
  await p.evaluate(() => document.querySelectorAll('.accordion-collapse:not(.show)')
    .forEach(c => c.classList.add('show')));
  await p.waitForTimeout(300);

  const sliders = await p.evaluate(() => [...document.querySelectorAll('input[type="range"]')]
    .map(el => ({ id: el.id, panY: getComputedStyle(el).touchAction })));
  total += sliders.length;
  if (!sliders.length) { console.log(`(no sliders on ${label})`); continue; }

  check(`${label}: every slider has an id the guard test can address`,
        sliders.every(s => s.id), sliders);
  check(`${label}: every slider hands vertical gestures to the scroller (touch-action: pan-y)`,
        sliders.every(s => s.panY === 'pan-y'), sliders);

  for (const s of sliders) {
    const down = await gesture(s.id, 2, 40);      // a scroll: mostly vertical
    check(`${label} #${s.id}: a vertical scroll puts the value back`,
          down.afterMove === down.before && down.after === down.before, down);
    check(`${label} #${s.id}: the restore re-fires input, so the preview re-syncs`,
          down.inputs > 0, down);

    const across = await gesture(s.id, 40, 2);    // a deliberate slide
    check(`${label} #${s.id}: a horizontal drag still sets the value`,
          across.after === across.jumped, across);
  }
}

// A sweep that finds nothing passes every assertion above. The floor is the
// eight sliders Filtering & Scoring ships with today.
check('the sweep actually found the sliders (>= 8)', total >= 8, { total });
check('no page errors', errs.length === 0, errs);

console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
