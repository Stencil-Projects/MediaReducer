// A real end-to-end run: fire a Simulate through the API against the booted
// app + mock Tautulli, wait for the engine subprocess to finish, and assert the
// full cache pipeline landed — the library snapshot, the marked & eligible
// queue, and (on a second run) that the metadata cache is reused. Plain fetch,
// no browser: this exercises the run/cache path the page-load smoke tests don't.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const H = { 'Content-Type': 'application/json', 'X-MediaReducer': '1' };

let ok = true;
// `extra` carries the app's own refusal text on a failure. Without it a refused
// run reads as "first Simulate starts FAIL" and the reason only exists in the
// app log, which is a long way to go for a sentence the response already had.
const check = (name, cond, extra = '') => {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond || !extra ? '' : ` — ${extra}`));
  ok = ok && cond;
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
// "The app is busy with its own work", as distinct from a real answer about
// this run. Kept beside the scenarios harness's BUSY_RE, which says the same.
const BUSY = /try again in a moment|already (?:in progress|active)/i;

async function status() {
  const r = await fetch(`${BASE}/api/status`, { cache: 'no-store' });
  return r.json();
}

async function runSimulate() {
  // "Try again in a moment" is the app's own startup storage refresh still
  // running, not a verdict about this run — so it is retried until it clears
  // rather than reported. Only the FIRST full-run test after boot ever meets
  // it, which is what made this look like a bug in the Plex profile: on a
  // machine slow enough to lose the race, e2e_fullrun_plex failed and the
  // jellyfin and both profiles behind it passed, because by then the refresh
  // was long finished.
  let d = {};
  for (let attempt = 0; attempt < 20; attempt++) {
    const r = await fetch(`${BASE}/api/run`, {
      method: 'POST', headers: H, body: JSON.stringify({ mode: 'debug_sim' }),
    });
    d = await r.json();
    // A refusal that is not about being busy is a real answer; keep it.
    // Matched on the message, because this one arrives as a 200 with
    // started:false. Exact rather than a bare /already/, which would also
    // catch "Space limits are already satisfied" — a true no-op, and one this
    // would then retry twenty times before reporting.
    if (d.started || !BUSY.test(d.message || '')) break;
    await sleep(1500);
  }
  if (!d.started) return { started: false, message: d.message || '' };
  // Wait out the engine subprocess (bounded).
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    const s = await status();
    if (!s.run_active) return { started: true, status: s };
  }
  return { started: true, timeout: true };
}

// Health must be green first (the fixture points Tautulli at the mock).
const s0 = await status();
check('media server connected before the run',
  (s0.cleanup_state?.connection_health?.critical_ok) === true);

// First Simulate: builds the plan and snapshot from scratch.
const r1 = await runSimulate();
check('first Simulate starts', r1.started === true, r1.message);
check('first Simulate finishes (no timeout)', r1.started && !r1.timeout);

const snapResp = await fetch(`${BASE}/api/library-snapshot`, { cache: 'no-store' });
const snap = await snapResp.json();
check('run wrote a non-empty library snapshot',
  Array.isArray(snap.movies) && snap.movies.length > 0);
check('snapshot carries a build time',
  Number(snap.built_at) > 0);
// Everything below reads the snapshot. Substituting an empty list keeps a
// missing one reporting as four more FAIL lines instead of a TypeError that
// kills the script and hides them — the run that found this printed a stack
// trace where the remaining checks should have been.
const movies = Array.isArray(snap.movies) ? snap.movies : [];

// Distinct viewers, end to end through the real Tautulli history endpoint. The
// media-info rows the catalog is built from carry a play count and nothing
// about who watched, so before the history sweep every played movie read as
// exactly one viewer — the figure the retention score treats as "nobody but one
// person cares about this". The fixture spreads plays over 1-4 users.
const viewerCounts = new Set(movies.map(m => Number(m.users) || 0));
const played = movies.filter(m => Number(m.plays) > 0);
check('the snapshot counts real distinct viewers, not one per played movie',
  [...viewerCounts].some(n => n > 1), [...viewerCounts].sort((a, b) => a - b).join(','));
check('every played movie has at least one viewer',
  played.length > 0 && played.every(m => Number(m.users) >= 1));
// "Never watched" means no plays AND no last-played date: a play count of zero
// beside a real date is still someone having watched it.
check('a movie nobody has watched has no viewers',
  movies.filter(m => !Number(m.plays) && !Number(m.last_played))
             .every(m => Number(m.users) === 0));

const s1 = r1.status || await status();
// The fixture library sits well under the 100 GB headroom, so nothing is marked
// for deletion — but the ENTIRE eligible library still enters the queue.
check('the eligible queue was built (every movie, in deletion order)',
  Number(s1.marked_count) > 0);

// Second Simulate: exercises the metadata-cache-hit path end to end. It must
// complete just the same (the cache makes it cheaper, never breaks it). Skipped
// with MR_E2E_SECOND_RUN=0 — the metadata cache is Plex-keyed, so a Jellyfin/
// both re-run just repeats the first pass and adds runtime for no new coverage.
if (process.env.MR_E2E_SECOND_RUN !== '0') {
  const r2 = await runSimulate();
  check('second Simulate (cache path) also finishes', r2.started === true && !r2.timeout, r2.message);
  const s2 = r2.status || await status();
  check('the queue is stable across a re-run',
    Number(s2.marked_count) === Number(s1.marked_count));
}

console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
