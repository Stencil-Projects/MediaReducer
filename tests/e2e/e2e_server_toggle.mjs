// Which server selections leave Movie Library Paths and Space Thresholds
// editable.
//
// The gate mixes two things that are easy to conflate: the LIVE Server software
// checkboxes, and the health of the SAVED config. Health is only probed for the
// servers the saved config had selected, so a deselected server's flag is false
// because nothing asked it, not because it failed. Reading that as "unreachable"
// meant ticking a server back on locked both sections behind "Connect or uncheck
// the selected media server first" — advice that could not help, because Check
// for Errors only adopts a probe while the form still matches what was saved.
// Only a save cleared it, and nothing on screen said so.
//
// The rules this pins, in both directions:
//   * a server the SAVED config did not select is unknown, not broken
//   * a server the saved config DID select is still held to its health
//   * something has to be proven good, so a fresh install cannot unlock by
//     ticking a box it has never connected to
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
// found. ticked: what the checkboxes say right now.
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
  ['fresh install, nothing ticked',
   { plex: false, jf: false }, NONE_UP, { plex: false, jf: false }, false],
  ['fresh install, Plex ticked but never connected',
   { plex: false, jf: false }, NONE_UP, { plex: true, jf: false }, false],
  ['fresh install, both ticked but neither connected',
   { plex: false, jf: false }, NONE_UP, { plex: true, jf: true }, false],

  ['saved Plex-only and healthy',
   { plex: true, jf: false }, JF_DOWN, { plex: true, jf: false }, true],
  // The reported bug: re-ticking a server the saved config had off.
  ['saved Plex-only and healthy, Jellyfin ticked back on',
   { plex: true, jf: false }, JF_DOWN, { plex: true, jf: true }, true],
  // ...but swapping ONTO the unproven server alone leaves nothing proven.
  ['saved Plex-only and healthy, swapped to Jellyfin alone',
   { plex: true, jf: false }, JF_DOWN, { plex: false, jf: true }, false],

  // A server the save DID select is still judged on its health.
  ['saved both, Jellyfin down',
   { plex: true, jf: true }, JF_DOWN, { plex: true, jf: true }, false],
  ['saved both, Jellyfin down, unticked to escape it',
   { plex: true, jf: true }, JF_DOWN, { plex: true, jf: false }, true],
  ['saved both and both healthy',
   { plex: true, jf: true }, BOTH_UP, { plex: true, jf: true }, true],
  ['saved both and both healthy, Jellyfin unticked',
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

check('no page errors', errs.length === 0, JSON.stringify(errs.slice(0, 3)));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
