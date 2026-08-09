// Every group on the Configuration page sits at the same rhythm.
//
// A group is a rule, a heading, then its fields. That spacing had been left to
// Bootstrap utilities written per instance and had drifted: three divider
// weights (my-2, my-3, my-4) and two heading gaps — one of which depended on
// something invisible in the markup, whether the heading shared a grid column
// with its first field. Logging therefore sat twice as far from its fields as
// IMDb Ratings Data did, on the same page, for no reason a reader could see.
//
// It is now set centrally, so this checks the result rather than the rules:
// measure every heading and require one answer. A group added later with its
// own `my-4` fails here rather than shipping slightly out of step.
//
// Measuring needs two corrections, and both are why eyeballing this is hard:
// a Bootstrap .row pulls itself up by the gutter and pads it back onto its
// columns, so the row's own box top is not where anything appears; and a
// d-flex with align-items-center centres a short child, which then reads a few
// px low without being misplaced. So containers are measured, and rows are
// measured through to their first column's padding edge.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
p.on('pageerror', e => errs.push(e.message));

let loaded = false;
for (let i = 0; i < 5; i++) {
  try { await p.goto(BASE + '/config', { waitUntil: 'load', timeout: 45000 }); loaded = true; break; }
  catch (_) { await p.waitForTimeout(1000); }
}
if (!loaded) { console.log('FAIL could not load /config'); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

await p.evaluate(() => document.querySelectorAll('.modal.show')
  .forEach(m => window.bootstrap && bootstrap.Modal.getInstance(m)?.hide()));
await p.evaluate(() => {
  document.querySelectorAll('.accordion-collapse:not(.show)').forEach(c => c.classList.add('show'));
  // The debug-only group is display:none until Debug mode is on. Hidden boxes
  // measure as zeros, which would quietly pass every check below.
  document.querySelectorAll('.debug-mode-control').forEach(e => e.classList.remove('d-none'));
});
await p.waitForTimeout(500);

const groups = await p.evaluate(() => {
  const out = [];
  for (const h of document.querySelectorAll('h6.config-group-title')) {
    const hb = h.getBoundingClientRect();
    const col = h.closest('.col-12, .col-lg-6') || h.parentElement;
    const rule = col.querySelector('hr.config-divider')
      || (h.previousElementSibling?.matches?.('hr.config-divider') ? h.previousElementSibling : null);
    const box = h.nextElementSibling || col.nextElementSibling;
    let content = null;
    if (box) {
      let top = box.getBoundingClientRect().top;
      if (box.classList.contains('row')) {
        const first = box.firstElementChild;
        if (first) top = first.getBoundingClientRect().top + parseFloat(getComputedStyle(first).paddingTop);
      }
      content = Math.round(top - hb.bottom);
    }
    out.push({
      title: h.textContent.trim(),
      visible: hb.height > 0,
      rule: rule ? Math.round(hb.top - rule.getBoundingClientRect().bottom) : null,
      content,
    });
  }
  return out;
});

check('the page has group headings to check', groups.length >= 10, { found: groups.length });
check('every one of them is actually rendered', groups.every(g => g.visible),
      groups.filter(g => !g.visible).map(g => g.title));

// The numbers themselves are not the point — agreement is. Read them off the
// page rather than hard-coding, so a deliberate change to the rhythm stays a
// one-line change and does not have to be mirrored here.
const contentGaps = [...new Set(groups.map(g => g.content))];
check('every heading sits the same distance above its fields',
      contentGaps.length === 1 && contentGaps[0] > 0,
      groups.map(g => `${g.title}=${g.content}`));

const ruled = groups.filter(g => g.rule !== null);
check('several groups open with a rule', ruled.length >= 3, { ruled: ruled.length });
const ruleGaps = [...new Set(ruled.map(g => g.rule))];
check('...and every rule sits the same distance above its heading',
      ruleGaps.length === 1 && ruleGaps[0] > 0,
      ruled.map(g => `${g.title}=${g.rule}`));

// The groups that were missing a heading entirely, which is what made the
// spacing look arbitrary rather than merely uneven.
for (const title of ['Appearance', 'Troubleshooting', 'Logging', 'IMDb Ratings Data']) {
  check(`"${title}" is a named group`, groups.some(g => g.title === title),
        groups.map(g => g.title));
}

// Nothing may go back to setting its own spacing: that is how it drifted.
const strays = await p.evaluate(() => ({
  dividers: [...document.querySelectorAll('hr.config-divider')]
    .filter(el => /\bm[ytb]-\d/.test(el.className)).map(el => el.className),
  headings: [...document.querySelectorAll('#cfg-accordion h6')]
    .filter(el => !el.classList.contains('config-group-title')).map(el => el.textContent.trim()),
}));
check('no divider carries its own margin utility', strays.dividers.length === 0, strays.dividers);
check('every heading in the accordion uses the shared class',
      strays.headings.length === 0, strays.headings);

check('no JS errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
