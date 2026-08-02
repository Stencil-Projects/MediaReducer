// Two panels that quote space figures, and the arithmetic behind each.
//
// 1. The Dashboard's breach note. Being ARMED is not the same as being able to
//    run: when a media server stops answering, the mode stays on Automatic
//    Cleanup while the server sends next_run_time=null and the countdown beside
//    the note reads "No run scheduled". The note branched only on the mode, so
//    the same card said no run was scheduled AND that hundreds of GB of films
//    would be deleted within ~15 minutes. Only one of those can be true.
//
// 2. Configuration's Space Thresholds rows. Headroom fires on USED space
//    (used >= total - target) and Redline on FREE space (free <= floor). Any
//    filesystem that reserves blocks — ext4 keeps 5% for root — has
//    free != total - used, so measuring Headroom on the free axis reported a
//    breach that could never fire, contradicting the used-axis percentage
//    printed on the row's own next line.
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

// ── The breach note ─────────────────────────────────────────────────────────
await p.goto(BASE + '/', { waitUntil: 'networkidle', timeout: 20000 });

// Armed and over limits, with 12 ripe marks. `canRun` is the difference between
// a scheduler that will act and one the server has told us cannot.
const note = (canRun) => p.evaluate((able) => {
  _monitoringActive = true;
  _paused = false; _schedOff = false;
  _safetyBlocked = false; _simulateRequired = false;   // _debugMode is a const, and false here
  _cleanupOk = able;
  _nextRunTime = able ? new Date(Date.now() + 9e5).toISOString() : null;
  _markedEvent = { on: null, count: 12, bytes: 340e9 };
  _headroomWindowUsedToday = false;
  _currentDeficits = () => ({ max: 340, headroom: 340, redline: 0, cap: 0 });
  _updateBreachNote();
  const el = document.getElementById('breach-note');
  return { hidden: !!el.hidden, text: el.textContent };
}, canRun);

let r = await note(false);
check('a blocked scheduler does not promise an imminent deletion',
      !/delete[s]? (at the next check|today|tomorrow)/.test(r.text), r.text);
check('...and still says what a run WOULD free, so the number is not lost',
      /would delete/.test(r.text), r.text);

r = await note(true);
check('a scheduler that can actually run still says when', /delete/.test(r.text)
      && !/would delete/.test(r.text), r.text);

// ── The Space Thresholds rows ───────────────────────────────────────────────
await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });

// ext4 with the default 5% root reserve: free is 200 GB short of total - used.
// Headroom 300 is NOT breached (used 3600 < 3700); Redline 300 IS (free 200).
const rows = await p.evaluate(() => {
  _configMonitoringActive = true;
  _diskStats = { total_gb: 4000, used_gb: 3600, free_gb: 200, pct_used: 90 };
  const out = {};
  for (const row of _thresholdRows({ HEADROOM_GB: 300, REDLINE_GB: 300, MAX_LIBRARY_GB: null })) {
    out[row.id] = { over: !!row.over, state: row.state, pct: row.pct, note: row.note };
  }
  return out;
});

check('Headroom is judged on used space, like the engine',
      rows['ts-headroom'].over === false, JSON.stringify(rows['ts-headroom']));
check('...so the row agrees with its own percentage',
      rows['ts-headroom'].pct < 100, String(rows['ts-headroom'].pct));
check('...and its remaining figure is on the used axis too',
      /^100\.0 GB to go$/.test(rows['ts-headroom'].state), rows['ts-headroom'].state);
check('Redline is still judged on free space, like the engine',
      rows['ts-redline'].over === true, JSON.stringify(rows['ts-redline']));

check('no JS errors', errs.length === 0, errs.join(' | '));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
