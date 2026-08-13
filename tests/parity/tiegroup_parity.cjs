// Engine <-> Score Explorer parity for the boundary tie-group pick: which movie
// the preview names as going next, against which one the run actually takes.
//
// engine.py flags this mirror in as many words, above NEAR_TIE_PTS: "the Score
// Explorer's popNextDeletion() mirrors the logic below; change one side only
// and the preview diverges from the run." It had diverged. The engine picks the
// SMALLEST member that covers the remaining target — deliberately, because
// everything in the group is near-tied on score, and deciding by a fraction of
// a point sent a 42 GB file to cover a 2 GB need. The mirror still picked by
// score, so the preview named the 42 GB movie and the run deleted a 4 GB one.
//
// pickFromTieGroup and keyLess are lifted out of explorer.js and eval'd rather
// than the file being required: it is a classic browser script, so loading the
// whole of it here would want a DOM. Lifting them is what makes this a check on
// the shipped code instead of on a copy of it.
//
// Usage: node tiegroup_parity.cjs <py_tiegroup.json>
const { readFileSync } = require('fs');
const path = require('path');

const repoRoot = path.join(__dirname, '..', '..');
const js = readFileSync(path.join(repoRoot, 'static', 'js', 'explorer.js'), 'utf8');
const cases = JSON.parse(readFileSync(process.argv[2], 'utf8'));

const grab = (re, name) => {
  const m = js.match(re);
  if (!m) throw new Error('missing ' + name + ' — has explorer.js been restructured?');
  return m[0];
};
eval(grab(/function keyLess\(a,b\)\{[\s\S]*?\n\}/, 'keyLess'));
eval(grab(/function pickFromTieGroup\(group,remainingGb\)\{[\s\S]*?\n\}/, 'pickFromTieGroup'));

let fails = 0;
for (const c of cases) {
  // pickFromTieGroup splices its choice out, so hand it a fresh copy.
  const group = c.group.map(m => ({ ...m }));
  const got = pickFromTieGroup(group, c.remainingGb);
  const name = got && got.title;
  if (name !== c.picked) {
    console.log(`FAIL ${c.name}: preview picks ${JSON.stringify(name)}, `
                + `the run picks ${JSON.stringify(c.picked)}`);
    fails++;
  }
  // ...and it must remove exactly the one it named, or the replay walks a
  // different queue from the second pick onward.
  if (group.length !== c.group.length - 1
      || group.some(m => m.title === name && !c.group.filter(x => x.title === name)[1])) {
    console.log(`FAIL ${c.name}: picked movie was not removed from the group`);
    fails++;
  }
}

console.log(`${cases.length} tie-group cases`);
console.log('RESULT:', fails ? 'FAIL' : 'PASS');
process.exit(fails ? 1 : 0);
