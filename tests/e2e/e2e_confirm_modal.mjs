// The confirmation dialog in front of the two destructive actions: the
// Dashboard's manual Cleanup and a Config save that arms deleting.
//
// Both used to arm on the button itself ("Are you sure?" on a second click).
// What is being locked in here is the property that pattern kept getting wrong:
// only an explicit answer acts. Canceling, or dismissing the dialog any other
// way, must leave the server untouched — a confirm that fires on a stray click
// is worse than no confirm at all, so every outcome is driven and the network
// is watched, not the button's label.
//
// Also covers the delay dialog's third button, which saves AND re-dates the
// existing marks: two requests, in that order, and only when it is pressed.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
const check = (name, cond) => { console.log((cond ? 'PASS ' : 'FAIL ') + name); ok = ok && cond; };
const errs = [];

// The three actions a dialog can authorise, answered locally: the test is
// about WHICH of them happen, and really saving or really running would
// reconfigure the app under the later cases. Everything else — including the
// page's own polling and validation lookups — goes to the live server, so the
// pages behave normally.
const ACTIONS = { '/api/config': { ok: true, config: null },
                  '/api/queue/reset-delays': { ok: true, reset: 2, message: 'Delay clocks reset.' },
                  '/api/run': { ok: true, started: true } };
const POSTS = [];
async function stub(p) {
  await p.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== 'POST' || !(path in ACTIONS)) return route.continue();
    POSTS.push(path);
    return route.fulfill({ status: 200, contentType: 'application/json',
                           body: JSON.stringify(ACTIONS[path]) });
  });
}

async function load(path, readyFn) {
  const p = await b.newPage();
  p.on('pageerror', e => errs.push(e.message));
  await stub(p);
  let navErr;
  for (let i = 0; i < 5; i++) {
    try {
      await p.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await p.waitForFunction(readyFn, { timeout: 15000 });
      navErr = null; break;
    } catch (e) { navErr = e; await p.waitForTimeout(1000); }
  }
  if (navErr) { console.log(`FAIL could not load ${path}:`, navErr.message);
                console.log('RESULT: FAIL'); await b.close(); process.exit(1); }
  // On a fresh install the welcome modal opens on its own, a moment after the
  // page is interactive — it owns focus and the backdrop, and would swallow
  // every click below. Close whatever is open until nothing is, rather than
  // hiding once and hoping it had already appeared.
  for (let i = 0; i < 20; i++) {
    const open = await p.evaluate(() => {
      const m = [...document.querySelectorAll('.modal.show')];
      m.forEach(el => bootstrap.Modal.getOrCreateInstance(el).hide());
      return m.length;
    });
    await p.waitForTimeout(300);
    if (!open && i > 2) break;
  }
  return p;
}

// Wait for the dialog to be fully on screen before answering it. Bootstrap
// animates it in and only moves focus into it at the end, and Escape goes to
// whatever holds focus — pressing it too early is a keystroke the dialog never
// sees, which would read as "Escape does nothing".
const shown = (p) => p.waitForFunction(() => {
  const m = document.getElementById('pr-confirm-modal');
  return m?.classList.contains('show') && m.contains(document.activeElement);
}, null, { timeout: 10000 });
// The .show class comes off when the fade-OUT starts, so it is not the end of
// the dialog. prConfirm resolves on hidden.bs.modal — a whole transition later
// — precisely so a caller's next step isn't racing the teardown, and that
// resolution is what the caller (and this test) acts on.
const gone = (p) => p.waitForFunction(
  () => !document.getElementById('pr-confirm-modal')?.classList.contains('show')
        && !document.querySelector('.modal-backdrop'),
  null, { timeout: 10000 });

// ── prConfirm itself: each way out reports a DIFFERENT answer ────────────────
const p = await load('/config', () => typeof prConfirm === 'function');

const outcomes = {};
for (const [name, act] of [
  ['confirm', () => p.click('#pr-confirm-ok')],
  ['extra',   () => p.click('#pr-confirm-extra')],
  ['cancel',  () => p.click('#pr-confirm-modal .modal-footer .btn-outline-secondary:not(#pr-confirm-extra)')],
  ['close',   () => p.click('#pr-confirm-modal .btn-close')],
  ['escape',  () => p.keyboard.press('Escape')],
]) {
  await p.evaluate(() => {
    window.__answer = 'pending';
    prConfirm({ title: 'T', body: ['a', { text: 'b', warn: true }],
                extraText: 'E' }).then(v => { window.__answer = { v }; });
  });
  await shown(p);
  await act();
  await p.waitForFunction(() => window.__answer !== 'pending', null, { timeout: 10000 });
  await gone(p);
  outcomes[name] = (await p.evaluate(() => window.__answer)).v;
}
check('the confirm button is the only thing that answers "confirm"',
  outcomes.confirm === 'confirm'
  && ['cancel', 'close', 'escape'].every(k => outcomes[k] === null));
check('the extra button answers as itself, not as a plain confirm',
  outcomes.extra === 'extra');

// There is ONE dialog element. A second call while one is open would rewrite
// the open question's text and leave both callers listening on the same
// buttons — one click answering two actions, potentially the wrong one.
await p.evaluate(() => {
  window.__first = 'pending'; window.__second = 'pending';
  prConfirm({ title: 'FIRST', body: ['first'] }).then(v => { window.__first = { v }; });
  prConfirm({ title: 'SECOND', body: ['second'] }).then(v => { window.__second = { v }; });
});
await shown(p);
const reentry = await p.evaluate(() => ({
  title: document.getElementById('pr-confirm-title').textContent,
  second: window.__second }));
await p.click('#pr-confirm-ok');
await p.waitForFunction(() => window.__first !== 'pending', null, { timeout: 10000 });
await gone(p);
const firstAnswer = (await p.evaluate(() => window.__first)).v;
check('a second dialog is refused, not merged into the open one',
  reentry.title === 'FIRST' && reentry.second?.v === null);
check('...and the click answers the question that was actually asked',
  firstAnswer === 'confirm');

// A warning that arrives from the server must never become markup.
await p.evaluate(() => {
  window.__answer = 'pending';
  prConfirm({ body: ['<img src=x onerror="window.__xss=1">',
                     { text: 'danger line', warn: true }] }).then(v => { window.__answer = { v }; });
});
await shown(p);
const rendered = await p.evaluate(() => {
  const body = document.getElementById('pr-confirm-body');
  return { html: body.innerHTML, imgs: body.querySelectorAll('img').length,
           warns: body.querySelectorAll('.pr-confirm-warn').length, xss: !!window.__xss };
});
check('dialog text is text — server-supplied warnings never become markup',
  rendered.imgs === 0 && !rendered.xss && rendered.html.includes('&lt;img'));
check('a warn paragraph is marked as one (it is the consequence line)',
  rendered.warns === 1);
await p.keyboard.press('Escape');
await p.waitForFunction(() => window.__answer !== 'pending', null, { timeout: 10000 });
await gone(p);

// ── Config save: the answer decides whether anything is written ─────────────
// The delay dialog asks the SERVER for the mark count rather than trusting the
// last poll (the poll is up to 4s old, and the dialog names a number), and it
// waits out any background reconcile the save started before re-dating. Both
// answers arrive on /api/status, so drive them there — assigning
// _markedClockedCount in the page would just be overwritten by the refresh.
let markedClocked = 25;
let summaryActive = false;
await p.route('**/api/status*', async (route) => {
  const res = await route.fetch();
  const body = await res.json().catch(() => null);
  if (!body) return route.fulfill({ response: res });
  body.marked_clocked_count = markedClocked;
  body.summary_active = summaryActive;
  return route.fulfill({ response: res, body: JSON.stringify(body) });
});

// Arm the delay case — an edited delay with marks already waiting. The page is
// left holding a WRONG count (a poll that landed before three more marks aged
// in); the dialog must quote the server's 25, not the page's 3.
const armDelay = () => p.evaluate(() => {
  _markedClockedCount = 3;
  _savedConfig = { ...(_savedConfig || {}), RUN_MODE: 'paused', DELETE_DELAY_DAYS: 3 };
  document.getElementById('mode-paused').checked = true;
  document.getElementById('DELETE_DELAY_DAYS').value = '7';
  document.getElementById('DELETE_DELAY_DAYS').disabled = false;
  saveConfig();
});

POSTS.length = 0;
await armDelay();
await shown(p);
const delayText = await p.evaluate(() => ({
  title: document.getElementById('pr-confirm-title').textContent,
  body: document.getElementById('pr-confirm-body').textContent,
  extra: document.getElementById('pr-confirm-extra').hidden
         ? null : document.getElementById('pr-confirm-extra').textContent,
}));
await p.click('#pr-confirm-modal .modal-footer .btn-outline-secondary:not(#pr-confirm-extra)');
await gone(p);
await p.waitForTimeout(400);
check('canceling the save writes NOTHING', POSTS.length === 0);
check('the delay dialog says the existing marks keep their old delay',
  /already marked/.test(delayText.body) && /future marks only/.test(delayText.body)
  && /\b25 movies\b/.test(delayText.body) && !/\b3 movies\b/.test(delayText.body));
check('...and offers to re-date them instead',
  !!delayText.extra && /re-date/.test(delayText.extra));

POSTS.length = 0;
await armDelay();
await shown(p);
await p.click('#pr-confirm-ok');
await gone(p);
await p.waitForTimeout(700);
check('confirming saves, and leaves the existing marks alone',
  POSTS.join() === '/api/config');

POSTS.length = 0;
await armDelay();
await shown(p);
await p.click('#pr-confirm-extra');
await gone(p);
await p.waitForTimeout(900);
// Order matters: the re-date endpoint applies the SAVED delay, so a re-date
// before the save would stamp the value the user just replaced.
check('the extra button saves first, THEN re-dates the marks',
  POSTS.join() === '/api/config,/api/queue/reset-delays');

// Changing a space threshold in the SAME save rebuilds the deletion plan
// server-side (the save unschedules the running clocks, then reconciles the
// queue from the snapshot), so the marks standing NOW are not the ones left
// afterwards. Naming a total there quotes a number the save itself invalidates.
const armDelayWithThreshold = () => p.evaluate(() => {
  _savedConfig = { ...(_savedConfig || {}), RUN_MODE: 'paused',
                   DELETE_DELAY_DAYS: 3, HEADROOM_GB: 500 };
  document.getElementById('mode-paused').checked = true;
  // The fixture has no reachable media server, which locks the threshold
  // fields and makes getFormConfig fall back to the saved values.
  _simulateRequired = false;
  _spaceThresholdsHaveUsableLibrary = () => true;
  for (const id of ['DELETE_DELAY_DAYS', 'HEADROOM_GB']) {
    document.getElementById(id).disabled = false;
  }
  document.getElementById('DELETE_DELAY_DAYS').value = '7';
  document.getElementById('HEADROOM_GB').value = '800';
  saveConfig();
});

POSTS.length = 0;
await armDelayWithThreshold();
await shown(p);
const rebuildText = await p.evaluate(() => ({
  body: document.getElementById('pr-confirm-body').textContent,
  extra: document.getElementById('pr-confirm-extra').hidden
         ? null : document.getElementById('pr-confirm-extra').textContent,
}));
check('a threshold change in the same save keeps the doomed count out of the dialog',
  !/\b25\b/.test(rebuildText.body) && /deletion plan is rebuilt/.test(rebuildText.body));
check('...and out of the re-date button, which still offers the re-date',
  !!rebuildText.extra && !/\b25\b/.test(rebuildText.extra)
  && /re-date/.test(rebuildText.extra));

// ...and the re-date waits for that rebuild. The endpoint refuses (409) while a
// reconcile owns the queue, so firing it the moment the save returns loses the
// re-date the user just asked for.
summaryActive = true;
ACTIONS['/api/config'] = { ok: true, config: null, reconcile: 'started' };
POSTS.length = 0;
await armDelayWithThreshold();
await shown(p);
await p.click('#pr-confirm-extra');
await gone(p);
await p.waitForTimeout(2500);
const duringRebuild = POSTS.join();
summaryActive = false;                       // the reconcile finishes
await p.waitForTimeout(3000);
check('the re-date holds while the save\'s reconcile still owns the queue',
  duringRebuild === '/api/config');
check('...and lands once it goes idle',
  POSTS.join() === '/api/config,/api/queue/reset-delays');
ACTIONS['/api/config'] = { ok: true, config: null };

// The SAME delay edit with nothing marked has no surprise in it — there are no
// old clocks to keep running — so it must save straight through.
markedClocked = 0;
POSTS.length = 0;
await p.evaluate(() => {
  _savedConfig = { ...(_savedConfig || {}), RUN_MODE: 'paused', DELETE_DELAY_DAYS: 3 };
  document.getElementById('mode-paused').checked = true;
  document.getElementById('DELETE_DELAY_DAYS').value = '7';
  saveConfig();
});
await p.waitForTimeout(1200);
const askedAnyway = await p.evaluate(
  () => document.getElementById('pr-confirm-modal').classList.contains('show'));
check('a delay change with nothing marked asks nothing',
  !askedAnyway && POSTS.join() === '/api/config');
await p.close();

// ── Dashboard cleanup: canceling must not start a run ──────────────────────
const d = await load('/', () => typeof runCleanup === 'function');

// Whether the Cleanup button is allowed at all is server state, refreshed by
// the status poll every few seconds — including while a dialog is open, which
// is the whole point of the last case below. Poking _cleanupOk from the test
// would just race the next poll, so drive it at the source: patch the poll's
// own answer, and let the page believe it consistently.
let allowCleanup = false;
await d.route('**/api/status*', async (route) => {
  if (!allowCleanup) return route.continue();
  const res = await route.fetch();
  const body = await res.json().catch(() => null);
  if (!body?.cleanup_state) return route.fulfill({ response: res });
  body.cleanup_state.cleanup_disabled = false;
  body.cleanup_state.debug_disabled = false;
  return route.fulfill({ response: res, body: JSON.stringify(body) });
});
const armCleanup = () => d.evaluate(() => {
  // mode 'headroom' is the real Cleanup path whatever _debugMode says — the
  // debug button passes 'debug_cleanup', which deletes nothing and never asks.
  _active = false; _runStartPending = false; _cleanupOk = true;
  _deleteDelayDays = 3;
  _storageHeadroomGb = null; _storageRedlineGb = null; _storageLibraryCapGb = 12700;
  _lastStorageSnapshot = { disk: { total_gb: 53994, used_gb: 36392, free_gb: 17602 },
                           libraryGb: 12724 };
  runCleanup('headroom', 'btn-cleanup');
});

POSTS.length = 0;
await armCleanup();
await shown(d);
const cleanupText = await d.evaluate(() => ({
  body: document.getElementById('pr-confirm-body').textContent,
  ok: document.getElementById('pr-confirm-ok').textContent,
  warns: document.querySelectorAll('#pr-confirm-body .pr-confirm-warn').length,
}));
await d.keyboard.press('Escape');
await gone(d);
await d.waitForTimeout(400);
check('dismissing the cleanup dialog starts NO run', POSTS.length === 0);
check('the cleanup dialog names the amount it is about to delete',
  /about 24\.0 GB/.test(cleanupText.body) && /Delete ~24\.0 GB/.test(cleanupText.ok));
check('...says the deletion is final',
  /no recycle bin/.test(cleanupText.body) && cleanupText.warns === 1);
// A manual Cleanup ignores the delay (engine: _MANUAL_RUN), so a mark the user
// believes is still protected by its clock can go in this run.
check('...and warns that the deletion delay does not hold it back',
  /still inside its delay/.test(cleanupText.body));

POSTS.length = 0;
allowCleanup = true;
await armCleanup();
await shown(d);
await d.click('#pr-confirm-ok');
await gone(d);
await d.waitForTimeout(700);
check('confirming starts the run', POSTS.some(u => /\/api\/run/.test(u)));

// The dialog can sit open for as long as the user takes to read it. The gates
// were checked before it opened, and status polling keeps running behind it —
// so a Cleanup that became blocked while the question was on screen must not
// go through on the answer. The two-click confirm re-checked this for free by
// re-entering runCleanup on the second click.
POSTS.length = 0;
allowCleanup = true;
await armCleanup();
await shown(d);
// The gate closes while the question is on screen — the server now says the
// button is blocked, and the next poll lands behind the open dialog.
allowCleanup = false;
await d.waitForFunction(() => _cleanupOk === false, null, { timeout: 15000 });
await d.click('#pr-confirm-ok');
await gone(d);
await d.waitForTimeout(700);
check('a Cleanup blocked WHILE the dialog was open does not run on confirm',
  POSTS.length === 0);
await d.close();

check('no JS errors', errs.length === 0);
if (errs.length) console.log('errors:', JSON.stringify(errs.slice(0, 3)));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
