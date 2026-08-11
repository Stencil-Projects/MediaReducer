// Lint the browser code, which used to be unlintable.
//
// Until these files existed the app's JavaScript lived inside Jinja templates,
// where no parser could read it: a template is not JavaScript, and the `{{ }}`
// in it is not an expression. A typo in an 8,000-line <script> was found by
// clicking the page it broke.
//
// The awkward part of the move is that these are classic scripts, not modules.
// Every top-level `let`, `const` and `function` in any of them — and in the
// small server-rendered preamble each template still carries — lands in ONE
// shared global scope, which is exactly how a page assembled from five files
// works without a bundler. ESLint sees five separate files and would call
// every cross-file name undefined.
//
// So the globals are DERIVED, below, from what the files and the templates
// actually declare, rather than listed by hand. A hand-kept list decays into a
// permission slip: the name that was deleted stays declared, and the next
// misspelling of it passes. Derived, `no-undef` means what it says — the name
// is not declared anywhere on the page — which is the one check this split
// makes possible and this codebase most needs, because a preamble variable
// renamed in the template and not in the .js is now a two-file edit.
//
// Derived PER PAGE, because that is the real scope. Three pages, each built
// from base.html plus its own template, and nothing else is loaded with it. A
// single pooled set of globals would let the Dashboard call a function that
// only exists on the Configuration page and call it defined.
//
//   npx eslint .          # or let tests/unit/test_js_lint.py run it
import { readFileSync, readdirSync } from "node:fs";

// ── What the browser provides ───────────────────────────────────────────────
// Written out rather than pulled from the `globals` package: one npm install
// in CI instead of two, and the list is short because this app uses plain DOM
// APIs. `bootstrap` is the vendored bundle, loaded before any of this.
const BROWSER = [
  "AbortController", "Blob", "CSS", "CustomEvent", "Date", "Event", "FormData",
  "Headers", "HTMLElement", "Intl", "IntersectionObserver", "MutationObserver",
  "Option", "Request", "Response", "URL", "URLSearchParams", "WebSocket",
  "alert", "bootstrap", "cancelAnimationFrame", "clearInterval", "clearTimeout",
  "confirm", "console", "document", "fetch", "getComputedStyle", "history",
  "localStorage", "location", "matchMedia", "navigator", "performance", "prompt",
  "queueMicrotask", "requestAnimationFrame", "screen", "sessionStorage",
  "setInterval", "setTimeout", "structuredClone", "window",
];

// ── What the page declares ──────────────────────────────────────────────────
// Top-level declarations only: a name indented by even one space is inside
// something and is not a global. That holds because this code is written that
// way, and `no-undef` itself keeps it holding — a global assigned into
// existence rather than declared is not found here, so every file that touches
// it fails the lint until it is declared somewhere.
const DECL = /^(?:async\s+)?(let|const|var|function|class)\b/gm;
const NAME = /^[A-Za-z_$][\w$]*/;
// The shared API between files is written `window.prThing = function …` where
// a page-local helper is written `function prThing()`. Both put the name in
// global scope, so both have to be found, or every caller of one reads as
// undefined.
const WINDOW_DECL = /^window\.([A-Za-z_$][\w$]*)\s*=/gm;

/** Every identifier the top-level declarations of `source` bind.
 *
 *  Only the KEYWORD is matched by regex, anchored to the start of a line, so
 *  nothing here can lose its place in a file this size. The names after it are
 *  read by hand, because `let raw=[],sc='order',sd=1` binds five of them and
 *  an initialiser is routinely an array or object with commas of its own —
 *  splitting on commas would take `sd` and miss the rest. */
function declaredIn(source) {
  const names = new Set();
  for (const m of source.matchAll(DECL)) {
    let i = m.index + m[0].length;
    if (m[1] === "function" || m[1] === "class") {
      const id = source.slice(i).match(/^\s*\*?\s*([A-Za-z_$][\w$]*)/);
      if (id) names.add(id[1]);
      continue;
    }
    // Read declarators until the statement ends. Strings and comments are
    // stepped over rather than read, so a semicolon inside one does not end it.
    let depth = 0, want = true;
    for (; i < source.length; i++) {
      const c = source[i], next = source[i + 1];
      if (c === "/" && next === "/") { i = source.indexOf("\n", i); if (i < 0) break; continue; }
      if (c === "/" && next === "*") { i = source.indexOf("*/", i + 2) + 1; if (i < 1) break; continue; }
      if (c === "'" || c === '"' || c === "`") {
        for (i++; i < source.length && source[i] !== c; i++) if (source[i] === "\\") i++;
        want = false;
        continue;
      }
      if ("([{".includes(c)) { depth++; want = false; continue; }
      if (")]}".includes(c)) { depth--; want = false; continue; }
      if (depth > 0 || /\s/.test(c)) continue;
      if (c === ";") break;
      if (c === ",") { want = true; continue; }
      if (want) {
        const id = source.slice(i).match(NAME);
        if (id) { names.add(id[0]); i += id[0].length - 1; }
      }
      want = false;
    }
  }
  return names;
}

/** The inline <script> blocks a template still carries: the server-rendered
 *  preamble whose values the deferred files read. */
function inlineScripts(html) {
  return [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1]).join("\n");
}

/** Everything `source` puts in global scope, declared or assigned onto window. */
export function globalsOf(source) {
  const names = declaredIn(source);
  for (const m of source.matchAll(WINDOW_DECL)) names.add(m[1]);
  return names;
}

/** What a .js file puts in scope. */
const fromJs = (f) => globalsOf(readFileSync(`static/js/${f}`, "utf8"));
/** What a template's inline preamble puts in scope. Its Jinja is not
 *  JavaScript, but a `{{ … }}` is only ever on the right of a declaration, so
 *  the NAMES read cleanly even though the file as a whole would not parse. */
const fromTemplate = (f) =>
  globalsOf(inlineScripts(readFileSync(`templates/${f}`, "utf8")));

// Loaded on every page, before the page's own file.
export const CHROME = ["base.html", "base.js", "form-fields.js"];
const chromeNames = [...fromTemplate("base.html"), ...fromJs("base.js"),
                     ...fromJs("form-fields.js")];

// Each entry is one file and everything that is in scope when it runs.
// readdirSync is deliberately not used here: a new file is a new page or a new
// shared script, and which one it is has to be stated, not guessed.
export const PAGES = [
  ["base.js", []],
  ["form-fields.js", []],
  ["config.js", ["config.html"]],
  ["dashboard.js", ["dashboard.html"]],
  ["explorer.js", ["deletion_score_explorer.html"]],
];

const RULES = {
  // The reason this config exists.
  "no-undef": "error",
  // builtinGlobals off: a file's own declarations are also configured as
  // globals for it, so leaving it on would report every one of them. Two
  // FILES claiming one name is the case that matters, and ESLint cannot see
  // it from inside one file — test_js_lint.py does.
  "no-redeclare": ["error", { builtinGlobals: false }],

  // Mistakes that read as working code.
  "getter-return": "error",
  "no-async-promise-executor": "error",
  "no-cond-assign": ["error", "always"],
  "no-compare-neg-zero": "error",
  "no-const-assign": "error",
  "no-constant-condition": ["error", { checkLoops: false }],
  "no-dupe-args": "error",
  "no-dupe-class-members": "error",
  "no-dupe-else-if": "error",
  "no-dupe-keys": "error",
  "no-duplicate-case": "error",
  "no-empty-pattern": "error",
  "no-fallthrough": "error",
  "no-func-assign": "error",
  "no-import-assign": "error",
  "no-invalid-regexp": "error",
  "no-obj-calls": "error",
  "no-self-assign": "error",
  "no-self-compare": "error",
  "no-sparse-arrays": "error",
  "no-unreachable": "error",
  "no-unsafe-negation": "error",
  "no-unsafe-optional-chaining": "error",
  "no-unused-labels": "error",
  "use-isnan": "error",
  "valid-typeof": ["error", { requireStringLiterals: true }],

  // Locals only. A top-level function with no caller in these files is
  // usually reached from an onclick= in the markup, or from another file,
  // and neither is visible from here.
  "no-unused-vars": ["error", { vars: "local", args: "none", caughtErrors: "none" }],
};

export default PAGES.map(([file, templates]) => ({
  files: [`static/js/${file}`],
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: "script",
    globals: Object.fromEntries(
      [...new Set([...BROWSER, ...chromeNames, ...fromJs(file),
                   ...templates.flatMap((t) => [...fromTemplate(t)])])]
        .map((n) => [n, "writable"]),
    ),
  },
  linterOptions: { reportUnusedDisableDirectives: "error" },
  rules: RULES,
}));
