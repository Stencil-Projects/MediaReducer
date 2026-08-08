// Over space limits with nothing eligible says WHY, instead of asking for a
// Simulate that cannot help.
//
// The breach note had one branch for "nothing is marked", and it read "run
// Simulate to mark the deletion plan". That is right when no plan has been
// built yet. It is wrong for the case underneath it: the plan IS current and
// came out empty because the filters disqualify everything — a media type
// switched off, a rating cutoff below every title, a grace period longer than
// the library. Running Simulate again disqualifies them all a second time.
//
// The two states are told apart by the eligible count the Marked & Eligible
// button already publishes, so this needs no new plumbing — only the note
// reading a number that was already on the page.
//
// Deliberately NOT a block. An empty eligible set is the ordinary state of a
// healthy library and changes on its own as files arrive and watch history
// ages; refusing to arm Automatic Cleanup on it would refuse exactly when
// everything is fine, and leave it unarmed when the library grows. The hard
// block stays where nothing CAN ever be eligible (both types off), which is
// config, not data — see test_cleanup_optin.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

const p = await b.newPage({ viewport: { width: 1400, height: 1000 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));
for (let i = 0; i < 5; i++) {
  try { await p.goto(BASE + '/', { waitUntil: 'load', timeout: 45000 }); break; }
  catch (_) { await p.waitForTimeout(1000); }
}
await p.evaluate(() => document.querySelectorAll('.modal.show')
  .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
await p.waitForTimeout(600);

// Drive the note's own inputs rather than a whole run: the branch under test is
// a rendering decision over (deficit, marked, eligible, plan-current), and
// staging a library where every title is filtered would test the filters, not
// the note.
const note = (marked, eligible) => p.evaluate(([m, e]) => {
  _monitoringActive = true;
  _safetyBlocked = false;
  _simulateRequired = false;
  _markedEvent = { on: null, count: m, bytes: m * 1e9 };
  _eligibleCount = e;
  _currentDeficits = () => ({ headroom: 40, redline: 0, cap: 0, max: 40 });
  _updateBreachNote();
  const el = document.getElementById('breach-note');
  return { hidden: el.hidden, text: el.textContent };
}, [marked, eligible]);

const empty = await note(0, 0);
check('a current plan with nothing eligible names the filters',
      !empty.hidden && /nothing is eligible/i.test(empty.text)
      && /Filtering & Scoring/.test(empty.text), empty);
check('...and does NOT ask for another Simulate',
      !/run Simulate/i.test(empty.text), empty);

const unplanned = await note(0, 12);
check('nothing marked but things ARE eligible still asks for a Simulate',
      !unplanned.hidden && /run Simulate/i.test(unplanned.text), unplanned);
check('...and does not claim nothing is eligible',
      !/nothing is eligible/i.test(unplanned.text), unplanned);

const planned = await note(3, 12);
check('a marked plan is unaffected — it reports what deletes',
      !/nothing is eligible/i.test(planned.text)
      && !/run Simulate/i.test(planned.text), planned);

// Within limits there is no deficit, so there is nothing to warn about — an
// empty eligible set is the healthy state and must stay silent.
const satisfied = await p.evaluate(() => {
  _currentDeficits = () => ({ headroom: 0, redline: 0, cap: 0, max: 0 });
  _eligibleCount = 0;
  _markedEvent = { on: null, count: 0, bytes: 0 };
  _updateBreachNote();
  return document.getElementById('breach-note').hidden;
});
check('within limits, an empty eligible set says nothing at all', satisfied === true);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
