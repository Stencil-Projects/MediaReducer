// Dashboard <-> shared.py deficit parity: the GB figure the delete confirmation
// puts on its own button.
//
// "Run a cleanup now?" says "Delete ~N GB", and N comes from _currentDeficits()
// in dashboard.js — a third implementation of arithmetic shared.pool_deficit_gb
// already owns for the engine and the app. It is the number a user weighs
// before authorising an irreversible deletion, so it has to be the same number,
// and nothing was checking that.
//
// The function is lifted out of dashboard.js and eval'd rather than the file
// being required: it is a classic browser script, so loading the whole of it
// here would want a DOM. Lifting it is what makes this a check on the shipped
// code instead of on a copy of it.
//
// Redline is deliberately NOT compared: shared.pool_deficit_gb documents it as
// an emergency that it never sizes, while the dashboard does size it (an
// emergency frees back to the floor, and the confirmation still owes the user a
// number). The redline arm is checked here against that rule instead.
//
// Usage: node deficit_parity.cjs <py_deficits.json>
const { readFileSync } = require('fs');
const path = require('path');

const repoRoot = path.join(__dirname, '..', '..');
const js = readFileSync(path.join(repoRoot, 'static', 'js', 'dashboard.js'), 'utf8');
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));

// State _currentDeficits reads, redeclared here; the eval'd function closes
// over these.
let _lastStorageSnapshot = null;
let _storageHeadroomGb = null, _storageRedlineGb = null, _storageLibraryCapGb = null;

const grab = (re, name) => {
  const m = js.match(re);
  if (!m) throw new Error('missing ' + name + ' — has dashboard.js been restructured?');
  return m[0];
};
eval(grab(/function _currentDeficits\(\)\s*\{[\s\S]*?\n\}/, '_currentDeficits'));

let fails = 0, worst = 0, checked = 0;
for (const c of cases) {
  _lastStorageSnapshot = { disk: c.disk, libraryGb: c.library_gb };
  _storageHeadroomGb = c.headroom_gb;
  _storageRedlineGb = c.redline_gb;
  _storageLibraryCapGb = c.cap_gb;
  const got = _currentDeficits();
  if (got === null) {
    if (c.py_deficit !== null) {
      console.log(`FAIL ${c.name}: page says "unknown", shared.py says ${c.py_deficit}`);
      fails++;
    }
    checked++;
    continue;
  }
  // The non-emergency figure — headroom vs cap — is exactly what shared.py sizes.
  const mine = Math.max(got.headroom, got.cap);
  const drift = Math.abs(mine - c.py_deficit);
  if (drift > 0.01) {
    console.log(`FAIL ${c.name}: dashboard ${mine.toFixed(3)} vs shared.py `
                + `${c.py_deficit.toFixed(3)} (drift ${drift.toFixed(3)} GB)`);
    fails++;
  }
  worst = Math.max(worst, drift);
  // Redline: sized only while free space is at or under the floor, and only
  // ever back UP to the floor — never to the headroom target.
  const free = Number(c.disk.free_gb), red = Number(c.redline_gb);
  const wantRed = (Number.isFinite(red) && red > 0 && Number.isFinite(free) && free <= red)
    ? Math.max(0, red - free) : 0;
  if (Math.abs(got.redline - wantRed) > 0.01) {
    console.log(`FAIL ${c.name}: redline arm ${got.redline} vs expected ${wantRed}`);
    fails++;
  }
  checked++;
}

console.log(`${checked} cases, worst drift ${worst.toFixed(4)} GB`);
console.log('RESULT:', fails ? 'FAIL' : 'PASS');
process.exit(fails ? 1 : 0);
