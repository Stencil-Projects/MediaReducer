// The Filtering & Scoring library's type filter (All / Movies / TV shows).
//
// The filter narrows what is SCORED and RANKED, not just what is drawn: a
// Movies-only view must show the deletion order a movie run would use, with no
// gaps where TV rows sat. And a snapshot with no TV must say so in words when
// the TV view is chosen, not present an empty table that reads as broken.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage({ viewport: { width: 1180, height: 900 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));
let ok = true;
const check = (name, cond, extra = '') => {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra)));
  ok = ok && cond;
};

const row = (title, media_type, plays) => ({
  title, year: 2020, rating: 6.5, votes: 5000, plays, users: 1,
  last_played: 0, added_at: 1_500_000_000, size_gb: 8, size_bytes: 8_000_000_000,
  protected: false, favorite: false, excluded: false, media_type,
});
// S1 is the most recently ADDED season (the "newest") even though S2 is the
// latest — the season-eligibility rule tells them apart. Its date sits well
// past the saved grace period so only the season rules decide.
const nowSec = Math.floor(Date.now() / 1000);
const gammaShow = () => ({ ...row('Gamma Show', 'tv', 1), tv_status: 'continuing',
  tv_in_scope: 1,   // the refresh always stamps scope before a row is stored
  tv_episodes: 20, tv_episodes_watched: 5,
  tv_seasons: [{ n: 1, eps: 10, eps_watched: 5, plays: 5, users: 1,
                 size_bytes: 8_000_000_000, last_played: 0, added_at: nowSec - 90 * 86400 },
               { n: 2, eps: 10, eps_watched: 0, plays: 0, users: 0,
                 size_bytes: 8_000_000_000, last_played: 0, added_at: nowSec - 700 * 86400 }] });
let movies = [row('Alpha Movie', 'movie', 0), row('Beta Movie', 'movie', 3), gammaShow()];
await p.route('**/api/library-snapshot**', async route => {
  try {
    await route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: true, built_at: 1_700_000_000, movies,
                             imdb_dataset_on_disk: false }) });
  } catch (_) { try { await route.continue(); } catch (_) { /* page gone */ } }
});

await p.goto(BASE + '/explorer', { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForFunction(() =>
  document.querySelectorAll('#mtbody tr').length > 0 &&
  !document.getElementById('mtbody').textContent.includes('Loading'), { timeout: 20000 });

const state = () => p.evaluate(() => ({
  stat: document.getElementById('tstat')?.textContent || '',
  body: document.getElementById('mtbody')?.textContent || '',
  rows: document.querySelectorAll('#mtbody tr').length,
}));
const setFilter = async v => {
  await p.selectOption('#type-filter', v);
  await p.waitForTimeout(150);
};

let s = await state();
check('All types shows every row — the TV series as its SEASONS, typed',
      s.body.includes('Alpha Movie') && s.body.includes('Beta Movie')
      && s.body.includes('Gamma Show') && s.body.includes('TV Show · S1')
      && s.body.includes('TV Show · S2') && s.body.includes('Movie'), s.stat);
check('the mixed view counts titles', /4 titles/.test(s.stat), s.stat);

await setFilter('movie');
s = await state();
check('Movies hides the TV row', !s.body.includes('Gamma Show'), s.body.slice(0, 120));
check('...and keeps both movies', s.body.includes('Alpha Movie') && s.body.includes('Beta Movie'), s.stat);
check('the movie view counts movies', /2 movies/.test(s.stat), s.stat);

await setFilter('tv');
s = await state();
check('TV shows only the TV rows', s.body.includes('Gamma Show')
      && !s.body.includes('Alpha Movie'), s.stat);
check('the TV view counts seasons', /2 seasons/.test(s.stat), s.stat);
// Seasons are IN the pool: with TV cleanup on, an in-scope season that no
// eligibility rule shields is ELIGIBLE and ranks in the deletion order. The
// latest season of a continuing show is shielded — the household may be
// keeping up with it.
check('an unshielded season is eligible, not inventory', /1 eligible/.test(s.stat), s.stat);
check('the continuing show\'s LATEST season is the filtered one',
      /filtered — latest season/.test(s.body) && !/inventory/.test(s.body),
      s.body.slice(0, 200));
// The season-eligibility dropdown, live: the default is oldest-only (S1 IS
// the oldest, so it stays eligible); "any except the newest" holds back the
// most recently ADDED season — S1, even though it is not the latest.
check('the dropdown defaults to oldest-only', await p.evaluate(() =>
      document.getElementById('c-tv-elig')?.value) === 'oldest');
await p.evaluate(() => { document.getElementById('c-tv-elig').value = 'except_newest'; onExpFilterInput(); });
await p.waitForTimeout(300);
s = await state();
check('except-newest filters the most recently ADDED season — not the latest',
      /filtered — newest season/.test(s.body) && /\b0 eligible/.test(s.stat), s.stat);
await p.evaluate(() => { document.getElementById('c-tv-elig').value = 'oldest'; onExpFilterInput(); });
await p.waitForTimeout(300);
s = await state();
check('back on oldest-only the oldest season is eligible again',
      /\b1 eligible/.test(s.stat), s.stat);
// The score column carries the season's unified retention score, and the
// series-watch-bump knob moves it live (Gamma is half watched, so the bump
// contributes knob x 0.5 on the history side).
const seasonScoreOf = () => p.evaluate(() => {
  const tr = [...document.querySelectorAll('#mtbody tr')]
    .find(r => r.textContent.includes('Gamma Show') && r.textContent.includes('TV Show · S1'));
  return parseFloat(tr?.querySelector('.sctxt')?.textContent || 'NaN');
});
const scoreAt10 = await seasonScoreOf();
check('the season row shows a real unified score', Number.isFinite(scoreAt10) && scoreAt10 > 0, scoreAt10);
await p.evaluate(() => { const el = document.getElementById('c-tv-bump'); el.value = '0'; onExpTvKnobInput(); });
await p.waitForTimeout(300);
const scoreAt0 = await seasonScoreOf();
check('turning the all-season watch boost to 0 lowers a watched show\'s season score',
      Number.isFinite(scoreAt0) && scoreAt0 < scoreAt10, [scoreAt0, scoreAt10]);
await p.evaluate(() => { const el = document.getElementById('c-tv-bump'); el.value = '10'; onExpTvKnobInput(); });
await p.waitForTimeout(200);

// The TV watch weight slider, live: Gamma is half-played (5 plays / 10 eps =
// half a movie watch). Doubling the weight makes it a full watch, so the
// season's score must RISE — and the badge must say what the weight means.
const weightBase = await seasonScoreOf();
await p.evaluate(() => { const el = document.getElementById('c-tv-weight'); el.value = '200'; onExpTvKnobInput(); });
await p.waitForTimeout(300);
const weightUp = await seasonScoreOf();
check('raising the TV watch weight raises a played season\'s score live',
      Number.isFinite(weightUp) && weightUp > weightBase, [weightBase, weightUp]);
const weightBadge = await p.evaluate(() => document.getElementById('v-tv-weight')?.textContent || '');
check('the weight badge translates the slider into watches', weightBadge === '2 watches', weightBadge);
await p.evaluate(() => { const el = document.getElementById('c-tv-weight'); el.value = '100'; onExpTvKnobInput(); });
await p.waitForTimeout(200);

// The four setting groups, in the order the page promises them.
const headings = await p.evaluate(() =>
  [...document.querySelectorAll('#filter-score-card .filter-score-heading')].map(h => h.textContent.trim()));
check('the card is organized into the four setting groups',
      JSON.stringify(headings) === JSON.stringify(
        ['Cleanup scope', 'Scoring & ordering', 'Eligibility filters', 'TV show settings curve']),
      headings);
check('both TV knobs are sliders', await p.evaluate(() =>
      document.getElementById('c-tv-weight')?.type === 'range'
      && document.getElementById('c-tv-bump')?.type === 'range'));

// A library with no TV at all: the TV view answers in words.
movies = [row('Alpha Movie', 'movie', 0)];
await p.reload({ waitUntil: 'domcontentloaded' });
await p.waitForFunction(() =>
  document.getElementById('mtbody')?.textContent.includes('Alpha Movie'), { timeout: 20000 });
await setFilter('tv');
s = await state();
check('an all-movie library says there are no TV seasons',
      s.body.includes('No TV seasons in the library database'), s.body.slice(0, 120));
await setFilter('all');
s = await state();
check('switching back restores the table', s.body.includes('Alpha Movie'), s.stat);

// Rows that never state a type are movies — the store's default, and what
// every snapshot row written before the column looks like.
movies = [ (() => { const r = row('Legacy Movie', 'movie', 0); delete r.media_type; return r; })() ];
await p.reload({ waitUntil: 'domcontentloaded' });
await p.waitForFunction(() =>
  document.getElementById('mtbody')?.textContent.includes('Legacy Movie'), { timeout: 20000 });
await setFilter('movie');
s = await state();
check('a row with no media_type key counts as a movie', s.body.includes('Legacy Movie'), s.stat);

// ── The Cleanup scope toggles ───────────────────────────────────────────────
// Both toggles preview live, each zeroing ITS type's eligibility: unticking
// Movies leaves only the eligible season standing; unticking TV too reads
// zero, with each row naming its own switch. Ticking back restores the count
// exactly. (Save persists via /api/score-config; the engine side — an empty
// plan both scan and reconcile — is pinned by unit tests.)
// Back on the full mixed fixture — the sections above swapped in smaller ones.
movies = [row('Alpha Movie', 'movie', 0), row('Beta Movie', 'movie', 3), gammaShow()];
await p.reload({ waitUntil: 'domcontentloaded' });
await p.waitForFunction(() =>
  document.getElementById('mtbody')?.textContent.includes('Gamma Show'), { timeout: 20000 });
await setFilter('all');
s = await state();
const beforeElig = Number((s.stat.match(/(\d+) eligible/) || [])[1] ?? NaN);
check('the scope toggles render checked by default', await p.evaluate(() =>
  document.getElementById('c-movies-on')?.checked && document.getElementById('c-tv-on')?.checked));
await p.evaluate(() => { document.getElementById('c-movies-on').checked = false; onExpFilterInput(); });
await p.waitForTimeout(300);
s = await state();
check('unticking Movies leaves only the eligible SEASON', /\b1 eligible/.test(s.stat), s.stat);
check('...and the movie row names the switch', /movie cleanup off/.test(s.body), s.body.slice(0, 200));
await p.evaluate(() => { document.getElementById('c-tv-on').checked = false; onExpFilterInput(); });
await p.waitForTimeout(300);
s = await state();
check('unticking TV as well zeroes eligibility', /\b0 eligible/.test(s.stat), s.stat);
check('...and the season row names ITS switch', /TV cleanup off/.test(s.body), s.body.slice(0, 260));
await p.evaluate(() => { document.getElementById('c-movies-on').checked = true;
                         document.getElementById('c-tv-on').checked = true; onExpFilterInput(); });
await p.waitForTimeout(300);
s = await state();
check('ticking both back restores the eligible count',
      Number((s.stat.match(/(\d+) eligible/) || [])[1] ?? NaN) === beforeElig, s.stat);

check('no JS errors', errs.length === 0, errs.slice(0, 3));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
