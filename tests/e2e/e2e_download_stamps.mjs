// Downloads are named for their CONTENT's time, not the click. The server
// declares each artifact's stamp (a run log: its first timestamp; the cache
// dump: the store's write time) and the page threads it into the filename —
// the click-time stamp had a run from 07:48 downloading as "…_085924".
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
    await p.goto(BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof prShowDebugBox === 'function', null, { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /config:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

// The endpoint half: the fixture app has a real store and a real lastrun.log.
const logStamp = await p.evaluate(async () =>
  (await (await fetch('/api/logs/last?lines=all')).json()).stamp || '');
check('the run-log endpoint declares the run\'s own stamp',
      /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$/.test(logStamp), logStamp);
const logHead = await p.evaluate(async () =>
  (await (await fetch('/api/logs/last?lines=all')).json()).content.split('\n').find(l => /\d{4}-\d{2}-\d{2}/.test(l)) || '');
check('...matching the log\'s own first date', logHead.includes(logStamp.slice(0, 10)), { logStamp, logHead });

// The popup half: run the real Cache contents debug, then capture what the
// Save button would name the file — through the real handler, download stubbed.
const named = await p.evaluate(async () => {
  const r = await (await fetch('/api/debug/cache', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
  prShowDebugBox('Cache contents', r.text, { stamp: r.stamp || '' });
  window.prDownloadText = (fn) => { window.__fn = fn; return true; };
  prDownloadDebugBox(null);
  return { fn: window.__fn, stamp: r.stamp || '' };
});
check('the cache popup names its download after the store\'s write time',
      named.stamp && named.fn === `mediareducer-cache-contents_${named.stamp}.txt`, named);

// A popup WITHOUT a declared stamp falls back to click time — and must not
// inherit the cache popup's stamp from the previous open.
const fallback = await p.evaluate(() => {
  prShowDebugBox('Sonarr debug', 'some output');
  window.prDownloadText = (fn) => { window.__fn2 = fn; return true; };
  prDownloadDebugBox(null);
  return window.__fn2;
});
check('an on-demand popup falls back to click time, not the previous stamp',
      /^mediareducer-sonarr-debug_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt$/.test(fallback)
      && !fallback.includes(named.stamp), fallback);

check('no page errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
