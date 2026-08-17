// "Reset to first-time setup" while the app is busy measuring storage.
//
// /api/config/reset waits up to ten seconds for a storage refresh to finish and
// then REFUSES, rather than half-resetting: the summary's engine subprocess
// merges into the cache as it exits, so a wipe underneath it rebuilds the store
// moments later and leaves pre-reset numbers on a "first-time" dashboard. The
// refusal is correct. What was wrong is the button — enabled, thinking for ten
// seconds, then reporting failure, which reads as a broken reset rather than a
// busy one. Restarting the container and immediately hitting reset lands in
// exactly that window, because a refresh runs at startup.
//
// A RUN is deliberately NOT a reason to ghost it: reset stops an active run
// itself, so gating on that would remove a path that works. Both directions are
// pinned here, since each fails as plausibly as the other.
//
// The arming case is the subtle one. This button is a two-click confirm, so a
// refresh starting between the two clicks would leave "Are you sure?" sitting
// on a dead button and then fire on the next click — a confirmation the user
// gave to a question that has since changed.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage({ viewport: { width: 1280, height: 1000 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));
let ok = true;
const check = (name, cond, extra = '') => {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra)));
  ok = ok && cond;
};

await p.goto(BASE + '/config', { waitUntil: 'domcontentloaded', timeout: 45000 });
await p.waitForFunction(() => typeof window.prOnStatusPoll === 'function', { timeout: 20000 });
await p.waitForSelector('#btn-reset-all', { state: 'attached', timeout: 20000 });
// A fresh install opens the welcome dialog over everything.
for (let i = 0; i < 20; i++) {
  if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
  await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
}
// It lives in a collapsed accordion section. Open that section directly rather
// than driving the disclosure, which is not what this test is about.
await p.evaluate(() => {
  document.getElementById('btn-reset-all')?.closest('.accordion-collapse')?.classList.add('show');
});
await p.waitForSelector('#btn-reset-all', { timeout: 20000 });

// Deliver a real status payload with the two flags overridden — the same shape
// the 4-second poll delivers, so this exercises the real path rather than a
// hand-rolled object the page might treat differently.
const poll = (over) => p.evaluate(async (o) => {
  const d = await (await fetch('/api/status?_=' + Date.now(), { cache: 'no-store' })).json();
  window.prOnStatusPoll({ ...d, ...o });
}, over);

const state = () => p.evaluate(() => {
  const el = document.getElementById('btn-reset-all');
  const banner = document.getElementById('btn-reset-all-banner');
  const status = document.getElementById('reset-status');
  return {
    disabled: !!el?.disabled,
    aria: el?.getAttribute('aria-disabled'),
    title: el?.title || '',
    label: (el?.textContent || '').trim(),
    note: (status?.textContent || '').trim(),
    armed: typeof _resetArmed === 'undefined' ? null : _resetArmed,
    bannerPresent: !!banner,
    bannerDisabled: banner ? !!banner.disabled : null,
  };
});

// ── Idle: the button works ─────────────────────────────────────────────────
await poll({ summary_active: false, run_active: false });
let s = await state();
check('idle, the reset button is live', !s.disabled, s);
check('...with no busy note', s.note === '', s);

// ── A storage refresh ghosts it ────────────────────────────────────────────
await poll({ summary_active: true, run_active: false });
s = await state();
check('a storage refresh disables reset', s.disabled, s);
check('...and marks it disabled for assistive tech', s.aria === 'true', s);
check('...saying why in the tooltip', /storage refresh/i.test(s.title), s);
check('...and in the status line beside it', /storage refresh/i.test(s.note), s);
if (s.bannerPresent) {
  check('...the banner reset too, since it posts the same endpoint',
        s.bannerDisabled === true, s);
}

// ── It comes back on its own ───────────────────────────────────────────────
// The poll is the only thing that clears this, so a stuck ghost would be a
// worse bug than the one being fixed.
await poll({ summary_active: false, run_active: false });
s = await state();
check('when the refresh finishes the button returns', !s.disabled, s);
check('...and the busy note is cleared', s.note === '', s);
check('...with the tooltip gone', s.title === '', s);

// ── A run is already somebody else's job ───────────────────────────────────
// The page-wide run lock ghosts every section including this button, and
// e2e_runlock pins that force-stop is the ONLY control left live. So this gate
// must never re-enable on a poll: doing so punches a single live hole in a
// page that is meant to be inert, which is a worse bug than the one being
// fixed here. It adds the refresh case and clears only what it set.
await poll({ summary_active: false, run_active: true });
s = await state();
check('a run leaves the run lock in charge — the gate does not re-enable it',
      s.disabled, s);
check('...and does not claim a storage refresh is the reason',
      !/storage refresh/i.test(s.title), s);

// ── Arming, then a refresh ─────────────────────────────────────────────────
await poll({ summary_active: false, run_active: false });
await p.click('#btn-reset-all');
s = await state();
check('one click arms the confirm', s.armed === true, s);
check('...and the button asks', /are you sure/i.test(s.label), s);

await poll({ summary_active: true, run_active: false });
s = await state();
check('a refresh starting mid-confirm disarms it', s.armed === false, s);
check('...restoring the original label rather than a dead "Are you sure?"',
      /reset to first-time setup/i.test(s.label), s);
check('...and the button is ghosted', s.disabled, s);

// The disarm has to be real: a second click must ask again rather than reset.
await poll({ summary_active: false, run_active: false });
s = await state();
check('after it clears, the button is live and unarmed',
      !s.disabled && s.armed === false, s);

// ── A poll landing mid-reset must not re-enable the button ─────────────────
// Between the confirm click and the redirect the button is disabled and morphed
// to "Resetting…"; a poll that re-enabled it there would offer a second reset
// on top of the one in flight.
await p.evaluate(() => {
  const el = document.getElementById('btn-reset-all');
  el.disabled = true;
  el.classList.add('btn-busy');
});
await poll({ summary_active: false, run_active: false });
s = await state();
check('a poll during a reset in flight leaves the busy button alone', s.disabled, s);
await p.evaluate(() => document.getElementById('btn-reset-all').classList.remove('btn-busy'));

check('no JS errors', errs.length === 0, errs.slice(0, 3));
await b.close();
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
process.exit(ok ? 0 : 1);
