// Which server selections leave Media Library Paths and Space Thresholds
// editable.
//
// Both are keyed to the SAVED selection and the health of that selection —
// never to the checkboxes. Ticking a server states an intention; until it is
// saved and probed, unlocking a section on it would offer settings for a
// server the app has never spoken to, and unlocking on UNticking would hand
// over sections on the strength of a change that is not in effect.
//
// The rules this pins:
//   * a server the SAVED config did not select is unknown, not broken — its
//     health flag is false because nothing probed it
//   * a server the saved config DID select is still held to its health
//   * something has to be proven good, so a fresh install cannot unlock by
//     ticking a box it has never connected to
//   * the checkboxes do not enter into it at all (the last check)
//
// Driven in the real page realm rather than through saves, so every combination
// is reachable without booting an app per case. The page's own functions decide;
// the test only supplies the state they read.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
let ok = true;
const errs = [];
p.on('pageerror', e => errs.push(e.message));
await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });

const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${cond ? '' : ' — ' + extra}`);
  ok = ok && cond;
};

// saved: what the last save recorded. health: what probing that save's selection
// found. ticked: what the checkboxes say right now — which must not matter.
const gate = (saved, health, ticked) => p.evaluate(([sv, h, tk]) => {
  _savedConfig = Object.assign({}, _savedConfig, { USE_PLEX: sv.plex, USE_JELLYFIN: sv.jf });
  _savedConfigHealth = Object.assign({}, _savedConfigHealth, {
    tautulli_connected: h.taut,
    jellyfin_connected: h.jf,
    media_path_blocker: !!h.pathBlock,
    filesystem_blocker: !!h.fsBlock,
  });
  document.getElementById('USE_PLEX').checked = tk.plex;
  document.getElementById('USE_JELLYFIN').checked = tk.jf;
  onServerSoftwareChange();
  return { paths: _sectionLockReason('paths'), space: _sectionLockReason('space') };
}, [saved, health, ticked]);

const BOTH_UP = { taut: true, jf: true };
const JF_DOWN = { taut: true, jf: false };
const NONE_UP = { taut: false, jf: false };

const CASES = [
  // saved selection        health    checkboxes now                  editable?
  ['fresh install, nothing saved',
   { plex: false, jf: false }, NONE_UP, { plex: false, jf: false }, false],
  ['fresh install, Plex ticked but not saved',
   { plex: false, jf: false }, NONE_UP, { plex: true, jf: false }, false],
  ['fresh install, both ticked but not saved',
   { plex: false, jf: false }, NONE_UP, { plex: true, jf: true }, false],

  ['saved Plex-only and healthy',
   { plex: true, jf: false }, JF_DOWN, { plex: true, jf: false }, true],
  // Ticking Jellyfin on changes nothing until it is saved and probed — and
  // the healthy SAVED Plex is what keeps the sections open meanwhile.
  ['saved Plex-only and healthy, Jellyfin ticked back on',
   { plex: true, jf: false }, JF_DOWN, { plex: true, jf: true }, true],
  ['saved Plex-only and healthy, unticked to Jellyfin alone (unsaved)',
   { plex: true, jf: false }, JF_DOWN, { plex: false, jf: true }, true],

  // A server the save DID select is still judged on its health...
  ['saved both, Jellyfin down',
   { plex: true, jf: true }, JF_DOWN, { plex: true, jf: true }, false],
  // ...and unticking it does not lift that until the untick is SAVED. The
  // Connections section stays editable throughout, so the save is reachable.
  ['saved both, Jellyfin down, unticked but not yet saved',
   { plex: true, jf: true }, JF_DOWN, { plex: true, jf: false }, false],
  ['saved both and both healthy',
   { plex: true, jf: true }, BOTH_UP, { plex: true, jf: true }, true],
  ['saved both and both healthy, Jellyfin unticked (unsaved)',
   { plex: true, jf: true }, BOTH_UP, { plex: true, jf: false }, true],

  // Blockers that have nothing to do with which server is selected.
  ['healthy servers but media paths do not line up',
   { plex: true, jf: false }, { taut: true, jf: false, pathBlock: true }, { plex: true, jf: true }, false],
  ['healthy servers but /config is unwritable',
   { plex: true, jf: false }, { taut: true, jf: false, fsBlock: true }, { plex: true, jf: true }, false],
];

for (const [name, saved, health, ticked, editable] of CASES) {
  const r = await gate(saved, health, ticked);
  const got = !r.paths && !r.space;
  check(`${editable ? 'editable' : 'locked  '}: ${name}`, got === editable,
        `paths=${JSON.stringify(r.paths)} space=${JSON.stringify(r.space)}`);
}

// ── The checkboxes do not enter into it ────────────────────────────────────
// Stated directly, rather than left implied by the table: for a fixed saved
// state, every checkbox combination must give the same answer.
for (const [label, saved, health] of [
  ['saved Plex-only, healthy', { plex: true, jf: false }, JF_DOWN],
  ['saved both, Jellyfin down', { plex: true, jf: true }, JF_DOWN],
  ['nothing saved', { plex: false, jf: false }, NONE_UP],
]) {
  const seen = [];
  for (const tk of [{ plex: false, jf: false }, { plex: true, jf: false },
                    { plex: false, jf: true }, { plex: true, jf: true }]) {
    seen.push(JSON.stringify(await gate(saved, health, tk)));
  }
  check(`ticking boxes changes nothing (${label})`, new Set(seen).size === 1,
        seen.join(' | '));
}

check('no page errors', errs.length === 0, JSON.stringify(errs.slice(0, 3)));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
