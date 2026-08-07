// Copy and Download act on the debug box's TEXT, so while a probe is still
// running they would hand over the "Running debug…" placeholder — a file or a
// clipboard full of nothing, with no sign anything went wrong. Both are ghosted
// until the output lands.
//
// Three properties, and the third is the one that is easy to lose: a FAILED
// probe must un-ghost too, or the box strands its own error message as the one
// text a user cannot copy into a bug report. Close stays live throughout —
// leaving is always allowed.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage({ viewport: { width: 1000, height: 760 } });
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
    await p.waitForFunction(() => typeof prRunDebug === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /config:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }
await p.evaluate(() => document.querySelectorAll('.modal.show')
  .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
await p.waitForTimeout(400);

const state = () => p.evaluate(() => {
  const g = (id) => { const e = document.getElementById(id); return e ? e.disabled : null; };
  const close = document.querySelector('.pr-debug-head button:last-child');
  return { copy: g('pr-debug-copy'), dl: g('pr-debug-download'),
           close: close ? close.disabled : null,
           text: (document.getElementById('pr-debug-text') || {}).textContent || '' };
});

// Hold the endpoint open so the pending window is observable. prRunDebug is
// fired WITHOUT awaiting it — evaluate() resolves a returned promise, which
// would skip straight past the state under test.
await p.route('**/api/debug/cache', async (r) => {
  await new Promise(x => setTimeout(x, 900));
  r.continue();
});
await p.evaluate(() => { prRunDebug('/api/debug/cache', 'Cache contents', null); });
await p.waitForTimeout(300);
const during = await state();
check('Copy and Download are ghosted while the probe runs',
      during.copy === true && during.dl === true, during);
check('...and Close stays live — leaving is always allowed',
      during.close === false, during);
check('...and the box is showing the placeholder, not output',
      /Running debug/.test(during.text), during.text.slice(0, 60));

await p.waitForTimeout(1500);
const after = await state();
check('both un-ghost when the output lands',
      after.copy === false && after.dl === false, after);
check('...with real output behind them', after.text.length > 40 && !/Running debug/.test(after.text),
      after.text.slice(0, 60));

// A failed probe still un-ghosts: its error text is exactly what gets pasted
// into a bug report.
await p.route('**/api/debug/radarr', r => r.abort());
await p.evaluate(() => { prRunDebug('/api/debug/radarr', 'Radarr API', null); });
await p.waitForTimeout(1400);
const failed = await state();
check('a FAILED probe un-ghosts too, so its error can be copied',
      failed.copy === false && failed.dl === false && /Debug failed/.test(failed.text), failed);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
