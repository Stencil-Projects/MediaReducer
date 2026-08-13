// The stepper marks a stage done only when the run's MODE actually performs it.
//
// Every run ends at phase "done" (step index 4) and the terminal fill ran to the
// high-water mark, so all five dots were ticked in every mode. That is right for
// Simulate and a real Cleanup, which do walk all five. It is wrong for the two
// modes that work from the standing queue: Debug Cleanup ("no library scan, no
// deletions" in its own log header) and Summary ("info mode — no scan
// performed"). Both drew "Reading library ✓" and "Scoring ✓" next to their own
// "Scanned 0" — the panel claiming work the same panel denied.
//
// Frames go straight to renderProgress(), so this exercises the real rendering
// path at the pixel the user reads: the dot's class and glyph.
const BASE = process.env.MR_BASE_URL || 'http://127.0.0.1:7474';
const PW = process.env.PLAYWRIGHT_MODULE || 'playwright';
const { chromium } = await import(PW);
const b = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
const p = await b.newPage();
const errs = [];
p.on('pageerror', e => errs.push(e.message));

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : '   ' + JSON.stringify(extra ?? '')));
  ok = ok && cond;
}

let navErr;
for (let i = 0; i < 5; i++) {
  try {
    await p.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    await p.waitForFunction(() => typeof renderProgress === 'function', { timeout: 15000 });
    navErr = null; break;
  } catch (e) { navErr = e; await p.waitForTimeout(1000); }
}
if (navErr) { console.log('FAIL could not load /:', navErr.message); console.log('RESULT: FAIL'); await b.close(); process.exit(1); }

// This test owns the panel: frames go straight to renderProgress(). The page's
// own polls also call it with the server's real progress.json, and one landing
// between a synthetic render and its sample stomps the frame under test —
// a timing-dependent flake. Starve the pollers instead of racing them.
await p.evaluate(() => { window._fetchJson = () => new Promise(() => {}); });

// One finished run in the given mode, reported exactly as the engine reports a
// completed one: status done, phase done.
async function steps(mode, extra = {}) {
  return p.evaluate(async ([mode, extra]) => {
    renderProgress({
      schema: 1, status: 'done', phase: 'done', mode,
      started_at: 1000 + Math.random(), scanned: 0, eligible: 2213,
      deleted: 2213, bytes_freed: 8801_600_000_000, ...extra,
    });
    // Connector overlays fade in over .3s (opacity transition); sample after
    // it settles or every line reads mid-fade as unpainted.
    await new Promise(r => setTimeout(r, 450));
    return {
      dots: [...document.querySelectorAll('#rp-steps .rp-step')].map(el => {
        // The ::after overlay on a step IS the connector arriving from its
        // left neighbor — the painted line the user reads for continuity.
        const line = getComputedStyle(el, '::after');
        return {
          label: el.querySelector('.rp-dot-label').textContent.trim(),
          glyph: el.querySelector('.rp-dot').textContent.trim(),
          skipped: el.classList.contains('is-skipped'),
          done: el.classList.contains('is-done'),
          linePainted: Number(line.opacity) === 1,
          lineBlend: (line.backgroundImage || '').includes('linear-gradient'),
        };
      }),
      scanned: document.getElementById('rp-stat-scanned').textContent.trim(),
    };
  }, [mode, extra]);
}

// ── Debug Cleanup: reads no library, scores nothing ─────────────────────────
let s = await steps('debug_cleanup');
check('Debug Cleanup greys "Reading library" instead of ticking it',
      s.dots[1].skipped && !s.dots[1].done && s.dots[1].glyph !== '✓', s.dots[1]);
check('...and "Scoring" likewise',
      s.dots[2].skipped && !s.dots[2].done && s.dots[2].glyph !== '✓', s.dots[2]);
check('...while the stages it DOES run stay ticked',
      s.dots[0].done && s.dots[3].done && !s.dots[0].skipped && !s.dots[3].skipped,
      [s.dots[0], s.dots[3]]);
check('...and all five positions are still shown, greyed in place',
      s.dots.length === 5 && s.dots.map(d => d.label).join('|') ===
        'Checking|Reading library|Scoring|Debugging|Done', s.dots.map(d => d.label));
// The path is ONE continuous line: Checking's green fades to grey entering the
// span, grey runs between the skipped stops, and grey fades back to green into
// Debugging — not a dark gap with a green segment appearing from nowhere.
check('...with every connector along the row painted',
      s.dots.slice(1).every(d => d.linePainted), s.dots.slice(1));
check('...blending green→grey entering the skipped span', s.dots[1].lineBlend, s.dots[1]);
check('...and grey→green leaving it into Debugging', s.dots[3].lineBlend, s.dots[3]);
check('...while the connector between two skipped stops stays plain grey',
      s.dots[2].linePainted && !s.dots[2].lineBlend, s.dots[2]);
check('...and the Scanned tile reads not-applicable, not zero',
      s.scanned === '—', s.scanned);

// ── Summary: "info mode — no scan performed" ────────────────────────────────
s = await steps('debug_info');
check('Summary greys the same two stages',
      s.dots[1].skipped && s.dots[2].skipped && !s.dots[1].done && !s.dots[2].done, s.dots);
check('...and its Scanned tile reads not-applicable too', s.scanned === '—', s.scanned);

// ── Simulate and a full-scan Cleanup walk all five, so nothing is greyed ────
for (const [mode, label] of [['debug_sim', 'Simulate'], ['headroom', 'a full-scan Cleanup']]) {
  const r = await steps(mode, { scanned: 2888 });
  check(`${label} still ticks every stage — it really runs them`,
        r.dots.every(d => d.done && !d.skipped), r.dots);
  check(`...and still reports a real scanned count`, r.scanned === '2,888', r.scanned);
}

// ── The queue fast path: mode "headroom", but no scan happened ──────────────
// A manual Cleanup with a covering plan and a Redline emergency both delete
// from the marked queue with no rescan. The mode alone can't say so — the
// same "headroom" run full-scans when the queue can't cover the target — so
// the engine declares the committed fast path's unperformed steps as
// skipped_stages in progress.
s = await steps('headroom', { skipped_stages: [1, 2] });
check('a fast-path Cleanup greys the scan stages like Debug Cleanup',
      s.dots[1].skipped && s.dots[2].skipped && !s.dots[1].done && !s.dots[2].done, s.dots);
check('...keeps its real stages ticked, with the path continuous',
      s.dots[0].done && s.dots[3].done && s.dots.slice(1).every(d => d.linePainted), s.dots);
check('...and reads not-applicable for Scanned', s.scanned === '—', s.scanned);

// ── A run that concluded at Checking: nothing past it happened ──────────────
// "Nothing to do", "Already ran today", "Waiting for today's run time", and a
// direct invocation in a non-executable mode all end the run at the first
// stage. Ticking a library read, a scoring pass, AND a deletion pass for those
// drew a five-tick completed run out of an invocation that refused to run.
s = await steps('headroom', { skipped_stages: [1, 2, 3],
                              message: 'Nothing to do — space limits are satisfied.' });
check('a nothing-to-do finish greys everything between Checking and Done',
      s.dots[1].skipped && s.dots[2].skipped && s.dots[3].skipped
      && !s.dots.slice(1, 4).some(d => d.done), s.dots);
check('...keeps Checking and Done ticked — it checked, and it concluded',
      s.dots[0].done && s.dots[4].done && !s.dots[0].skipped && !s.dots[4].skipped, s.dots);
check('...with the path continuous into Done',
      s.dots.slice(1).every(d => d.linePainted) && s.dots[4].lineBlend, s.dots);
check('...and Scanned reads not-applicable', s.scanned === '—', s.scanned);

// ── While the run is still AT Checking, nothing paints ahead of it ──────────
// The dashed circles say what will be skipped; the LINES wait until the run
// has actually moved past. Unconditional continuity painted a gradient out of
// the active first dot into stages nothing had reached — the first stage
// connects to nothing, in every mode.
s = await steps('debug_cleanup', { status: 'running', phase: 'checking', scanned: 0 });
check('a running Debug Cleanup at Checking paints no line ahead',
      s.dots.slice(1).every(d => !d.linePainted), s.dots);
check('...while the skipped circles still show as dashed previews',
      s.dots[1].skipped && s.dots[2].skipped, s.dots);

// Once the run jumps to Debugging, the span behind it bridges in.
s = await steps('debug_cleanup', { status: 'running', phase: 'deleting', scanned: 0 });
check('...and the span paints only once the run has moved past it',
      s.dots[1].linePainted && s.dots[2].linePainted && s.dots[1].lineBlend, s.dots);
check('...with nothing painted beyond the active stage',
      !s.dots[4].linePainted, s.dots[4]);

// ── A stopped run still marks the mode's skipped stages as skipped ──────────
// Skipped is a property of the mode, not of how far the run got, so an
// interrupted Debug Cleanup must not suddenly claim the two stages back.
s = await steps('debug_cleanup', { status: 'stopped', phase: 'deleting' });
check('a stopped Debug Cleanup keeps its skipped stages greyed',
      s.dots[1].skipped && s.dots[2].skipped && !s.dots[1].done, s.dots);

check('no page errors', errs.length === 0, errs);
console.log('RESULT:', ok ? 'PASS' : 'FAIL');
await b.close();
process.exit(ok ? 0 : 1);
