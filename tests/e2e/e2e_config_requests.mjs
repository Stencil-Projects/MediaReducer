// Configuration asks the server for each thing once when it loads.
//
// Measured on the dev instance, a Configuration load sent /api/status twice
// (the base poll fired before the page's hook existed, so the page fetched its
// own copy), the IMDb, cache and archived-log status lines twice each (the form
// fill re-fetched them in the same time format), and two POST /api/collections
// in the same millisecond, each scanning BOTH media servers. Every Save sent
// the collections pair again.
//
// Counted inside one base poll interval (4 s), so a second tick cannot be
// mistaken for a duplicate.
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

const seen = {};
const t0 = Date.now();
p.on('request', r => {
  if (Date.now() - t0 > 3500) return;
  const u = new URL(r.url());
  if (!u.pathname.startsWith('/api/')) return;
  const k = `${r.method()} ${u.pathname}`;
  seen[k] = (seen[k] || 0) + 1;
});
await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });
await p.waitForTimeout(Math.max(0, 3500 - (Date.now() - t0)));
const n = k => seen[k] || 0;

check('the status is fetched once, and it seeds the page',
      n('GET /api/status') === 1, JSON.stringify(seen));
for (const line of ['/api/imdb/status', '/api/cache/status', '/api/logs/archived/status']) {
  check(`${line} is fetched once`, n('GET ' + line) === 1, JSON.stringify(seen));
}
check('the collections scan is one request for both servers, if any',
      n('POST /api/collections') <= 1, JSON.stringify(seen));
check('the page is seeded: the library size line is filled in by the first poll',
      await p.evaluate(() => typeof _configRunActive !== 'undefined'));
check('no page errors', errs.length === 0, errs.join(' | '));

await b.close();
console.log(`RESULT: ${ok ? 'PASS' : 'FAIL'}`);
process.exit(ok ? 0 : 1);
