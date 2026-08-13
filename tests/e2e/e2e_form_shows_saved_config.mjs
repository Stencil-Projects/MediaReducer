// The Configuration form shows the SAVED config, and keeps showing it.
//
// Reported: set a 500 GB Redline and a 1-day delay, save, refresh — and the
// page came back with Redline unticked and blank, Headroom ticked and blank,
// and the delay reading 25. The file on disk was correct throughout; only the
// fields were wrong, and they were wrong in a way that looks authoritative.
//
// Rendering the values once is not enough to prevent that. Browsers replay a
// form's previous contents on a refresh, and a phone browser does the same
// after the OS discards a background tab and restores it — either lands ON TOP
// of what the render just wrote. So the page needs both halves:
//
//   • autocomplete="off" on the form, which is what asks the browser not to;
//   • a re-assert of the saved config at init, which corrects the fields if
//     anything managed to change them anyway.
//
// The second is checked by doing the damage deliberately: put the exact
// reported state into the fields, then run the correction and require every
// one of them back.
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

await p.goto(BASE + '/config', { waitUntil: 'networkidle', timeout: 20000 });
for (let i = 0; i < 20; i++) {
  if (!await p.evaluate(() => !!document.querySelector('.modal.show'))) break;
  await p.keyboard.press('Escape');
  await p.waitForTimeout(200);
}

check('the form tells the browser not to replay its old contents',
      await p.evaluate(() => document.getElementById('cfg-form')?.getAttribute('autocomplete'))
        === 'off');

// Every threshold field, against the saved config it is supposed to be showing.
// Read from _savedConfig rather than hardcoded, so this holds on any fixture.
const agrees = () => p.evaluate(() => {
  const v = id => document.getElementById(id)?.value;
  const c = id => document.getElementById(id)?.checked;
  const cfg = _savedConfig || {};
  const num = x => (x === null || x === undefined || x === '') ? null : Number(x);
  return {
    headroom: c('headroom-enabled') === !cfg.REDLINE_ONLY_MODE,
    headroomValue: cfg.REDLINE_ONLY_MODE || num(v('HEADROOM_GB')) === num(cfg.HEADROOM_GB),
    redline: c('redline-enabled') === (cfg.REDLINE_GB !== null && cfg.REDLINE_GB !== undefined),
    redlineValue: cfg.REDLINE_GB === null || cfg.REDLINE_GB === undefined
      || num(v('REDLINE_GB')) === num(cfg.REDLINE_GB),
    delay: num(v('DELETE_DELAY_DAYS')) === num(cfg.DELETE_DELAY_DAYS ?? 1),
  };
});

let state = await agrees();
check('on load, every threshold field matches the saved config',
      Object.values(state).every(Boolean), JSON.stringify(state));

// ── The reported corruption, done deliberately, then corrected ──────────────
await p.evaluate(() => {
  document.getElementById('headroom-enabled').checked = true;   // reported: ticked
  document.getElementById('HEADROOM_GB').value = '';            // ...and blank
  document.getElementById('redline-enabled').checked = false;   // reported: unticked
  document.getElementById('REDLINE_GB').value = '';             // ...and blank
  document.getElementById('DELETE_DELAY_DAYS').value = '25';    // reported: 25
});
const wrecked = await agrees();
check('...the reported state really is a disagreement with the file',
      !Object.values(wrecked).every(Boolean), JSON.stringify(wrecked));

await p.evaluate(() => populateForm(_savedConfig));   // what page init runs
state = await agrees();
check('re-asserting the saved config puts every field back',
      Object.values(state).every(Boolean), JSON.stringify(state));

// ...and page init has to be the thing that runs it. A replay lands between
// the parse and the deferred script, which is a window this harness cannot
// open — so the call itself is checked in the shipped source rather than
// asserted through a scenario that would quietly pass without it.
const src = await p.evaluate(async () => (await fetch('/static/js/config.js')).text());
check('page init re-asserts the saved config over the rendered fields',
      /^populateForm\(_savedConfig\);$/m.test(src),
      'no top-level populateForm(_savedConfig) call in config.js');

// ...and it must not leave the page looking edited, or every load would offer
// to save changes nobody made.
check('...without marking the form dirty',
      await p.evaluate(() => !document.getElementById('btn-save')
        ?.classList.contains('btn-dirty')));

check('no page errors', errs.length === 0, errs.join(' | '));
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
