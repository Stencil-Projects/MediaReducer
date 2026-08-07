// The progress bar fills 0→100 once per stage and never runs backwards inside
// one. Two writers describe a run — the web app's start stub, then the engine
// — and the dashboard keys its per-stage bar on started_at, treating a changed
// one as a whole new run. When the engine minted its own stamp instead of
// adopting the app's, the very first stage visibly filled, snapped to empty,
// and filled again in the seconds between the two frames. That is the bug this
// test reproduces at the pixel the user actually sees: the bar's width.
//
// Frames are fed straight to renderProgress(), so this exercises the real
// rendering path without needing a live run to be mid-flight.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
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
    await p.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof renderProgress === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

// Walk a stage's frames and report the bar width after each. `stamps` supplies
// the started_at per frame, which is the whole point of the exercise.
async function widths(stamps) {
  return p.evaluate(async (stampList) => {
    const out = [];
    // A stage's creep is a function of wall-clock since the stage began, so
    // let real time pass between frames — that is what makes a restart visible
    // as a DROP rather than two identical readings.
    for (const started_at of stampList) {
      renderProgress({
        schema: 1, status: 'running', phase: 'checking', mode: 'headroom',
        started_at, updated_at: started_at,
        scanned: 0, total: 0, message: 'Checking connections…',
      });
      await new Promise(r => setTimeout(r, 260));
      out.push(parseFloat(document.getElementById('rp-bar').style.width) || 0);
    }
    return out;
  }, stamps);
}

// The shipped behavior: one stamp for the whole run, so Checking climbs.
const RUN = 1700000000.5;
const good = await widths([RUN, RUN, RUN, RUN, RUN, RUN]);
const goodDrops = good.filter((w, i) => i > 0 && w < good[i - 1]);
check('the Checking bar only moves forward within the stage', goodDrops.length === 0, good);
check('...and it actually moves (a frozen bar would pass vacuously)',
      good[good.length - 1] > good[0], good);

// The negative control, and the exact shape of the reported bug: the app's
// stub stamp followed by a DIFFERENT engine stamp for the same run. If the
// dashboard ever stops treating that as a new run this probe goes blind, so
// it is asserted to still drop.
const bad = await widths([RUN, RUN, RUN, RUN + 3.25, RUN + 3.25]);
const badDrops = bad.filter((w, i) => i > 0 && w < bad[i - 1]);
check('a second stamp for one run still visibly restarts the bar '
      + '(the probe can see the bug it guards)', badDrops.length > 0, bad);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
