// The Config page's save-time "this will prune" warning — the text the confirm
// dialog shows before a save that starts deleting.
// A Library Size Cap below the current library warns even when the scheduler
// can't delete. This locks in that Headroom and Redline behave the SAME: setting
// either to a value the disk is already past raises the warning in a
// non-deleting mode too, not only when arming Automatic Cleanup — while an
// unchanged breached threshold on an unrelated save stays quiet.
// It also pins WHEN each warning says the prune will happen, which is the part
// that was wrong: Headroom and the Library Size Cap only run in the once-a-day
// window, so promising "immediately" or "within ~15 minutes" sent the user
// watching for something that wasn't coming. Only Redline fires on any tick.
// Drives the pure _immediatePruneWarning in the real page realm (real
// _diskStats), so it's deterministic.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

let navErr;
for (let i = 0; i < 5; i++) {
  try {
    await p.goto('' + BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof _immediatePruneWarning === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /config:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

const R = await p.evaluate(() => {
  const total = Number(_diskStats?.total_gb) || 1000;
  const free = Number(_diskStats?.free_gb) || 500;
  const big = total + free + 1000;   // guaranteed to breach either free-space threshold
  const base = { RUN_MODE: 'paused', HEADROOM_GB: 0, REDLINE_GB: null,
                 MAX_LIBRARY_GB: null, DELETE_DELAY_DAYS: 0 };
  const savedRun = _savedConfig, savedLib = _lastKnownLibraryGb;
  const call = (saved, cfg, nowCleanup) => { _savedConfig = saved; return _immediatePruneWarning(cfg, !!nowCleanup); };
  try {
    return {
      // Headroom CHANGED into a breach, still Paused → warns like the cap does.
      headPaused: call({ ...base }, { ...base, HEADROOM_GB: big }, false),
      // Redline CHANGED into a breach, still Paused → warns.
      redPaused:  call({ ...base }, { ...base, REDLINE_GB: big }, false),
      // Unchanged breached Redline on an unrelated Paused save → must NOT nag.
      redUnchanged: call({ ...base, REDLINE_GB: big }, { ...base, REDLINE_GB: big }, false),
      // Arming Automatic Cleanup over an already-breached (unchanged) threshold
      // warns — and each one has to name the schedule IT actually runs on.
      headArmCleanup: (() => { _lastKnownLibraryGb = null;
        return call({ ...base, HEADROOM_GB: big, RUN_MODE: 'paused' },
                    { ...base, HEADROOM_GB: big, RUN_MODE: 'headroom' }, true); })(),
      redArmCleanup: (() => { _lastKnownLibraryGb = null;
        return call({ ...base, REDLINE_GB: big, RUN_MODE: 'paused' },
                    { ...base, REDLINE_GB: big, RUN_MODE: 'headroom' }, true); })(),
      capArmCleanup: (() => { _lastKnownLibraryGb = 5000;
        return call({ ...base, MAX_LIBRARY_GB: 100, RUN_MODE: 'paused' },
                    { ...base, MAX_LIBRARY_GB: 100, RUN_MODE: 'headroom' }, true); })(),
    };
  } finally {
    _savedConfig = savedRun; _lastKnownLibraryGb = savedLib;
  }
});

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };

check('Headroom changed into a breach warns in Paused mode (next Cleanup will prune)',
  /Headroom target/.test(R.headPaused) && /next Cleanup will prune/.test(R.headPaused));
check('Redline changed into a breach warns in Paused mode (next Cleanup will free)',
  /Redline floor/.test(R.redPaused) && /next Cleanup will free/.test(R.redPaused));
check('an unchanged breached threshold on an unrelated save does NOT nag',
  R.redUnchanged === '');
// A warning that promises action sooner than the scheduler can deliver it is
// worse than no warning: the user watches for a prune that isn't coming and
// concludes the app is broken. Headroom and the Library Size Cap are gated by
// the once-a-day window, so both said something faster than they meant — one
// "within ~15 minutes", one "immediately". Only Redline fires on any tick.
check('arming Automatic Cleanup over an already-breached Headroom warns, on the daily schedule',
  /Headroom target/.test(R.headArmCleanup) && /next daily run/.test(R.headArmCleanup));
check('...and the Library Size Cap says the same, since it runs in the same window',
  /Library Size Cap/.test(R.capArmCleanup) && /next daily run/.test(R.capArmCleanup));
check('...while Redline, which fires on any 15-minute check, still says so',
  /Redline floor/.test(R.redArmCleanup) && /within ~15 minutes/.test(R.redArmCleanup));
check('no warning promises a prune the daily window will not deliver',
  [R.headArmCleanup, R.capArmCleanup].every(m => m && !/immediate|within ~15 minutes/i.test(m)));
check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0, 3)));
console.log('R:', JSON.stringify(R));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
