// The countdown must NAME the event it is counting to, in every mode.
//
// Monitor Only already distinguished the daily slot from a routine 15-minute
// refresh, but Automatic Cleanup said a flat "Next run in" for both — so the
// mode that actually deletes was the one that didn't say what was about to
// happen. The server already sends which fire is next (next_tick_is_daily);
// the label just ignored it.
//
// "and cleanup" is keyed to the SAME signal that turns the countdown red, so
// the wording and the color can't disagree: a daily scan on a day that is
// within limits deletes nothing and must not claim otherwise.
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
    await p.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof _updateCountdown === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

const R = await p.evaluate(() => {
  const out = {};
  const soon = new Date(Date.now() + 9 * 60000 + 50000).toISOString();
  const saved = { paused: _paused, off: _schedOff, run: _nextRunTime };
  _active = false; _monitoringActive = true; _cleanupOk = true;
  _nextRunTime = soon; _nextTickTime = soon; _restingReason = ''; _autopauseReason = '';
  _headroomWindowUsedToday = false; _deleteDelayDays = 0; _markedRipeCount = 25;
  _dailyRunTime = '00:00'; _storageHeadroomGb = null; _storageRedlineGb = null;
  _lastStorageSnapshot = { disk: { total_gb: 53994, used_gb: 36392, free_gb: 17602 },
                           libraryGb: 12724 };
  const read = () => { _updateCountdown();
                       return document.getElementById('next-run-sub').textContent; };
  try {
    _paused = false; _schedOff = false;
    _nextTickIsDaily = true;  _storageLibraryCapGb = 12700; out.cleanupDailyOver = read();
    _nextTickIsDaily = true;  _storageLibraryCapGb = 99999; out.cleanupDailyWithin = read();
    _nextTickIsDaily = false; _storageLibraryCapGb = 12700; out.cleanupRoutine = read();
    _paused = true;
    _nextTickIsDaily = true;  out.monitorDaily = read();
    _nextTickIsDaily = false; out.monitorRoutine = read();
  } finally {
    _paused = saved.paused; _schedOff = saved.off; _nextRunTime = saved.run;
  }
  return out;
});

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };
check('Automatic Cleanup names the daily scan AND the cleanup when it will delete',
  /^Daily scan and cleanup in \d+:\d\d$/.test(R.cleanupDailyOver));
check('...but only the scan on a day that is within limits',
  /^Daily scan in \d+:\d\d$/.test(R.cleanupDailyWithin));
check('a routine 15-minute tick is a refresh, not a "run"',
  /^Next refresh in \d+:\d\d$/.test(R.cleanupRoutine));
check('no mode still shows the bare "Next run in"',
  !Object.values(R).some(v => /^Next run in/.test(v)));
check('Monitor Only wording is unchanged',
  /^Daily scan in \d+:\d\d$/.test(R.monitorDaily)
  && /^Next refresh in \d+:\d\d$/.test(R.monitorRoutine));
check('no JS errors', errs.length === 0);
console.log('R:', JSON.stringify(R));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
