// Headless run of static/app.js: Runs tab failure + recovery (usage: node runs_failure.cjs <repo>)
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] + '/src/tcgwatch/web/static/app.js', 'utf8');
const bad = `<img src=x onerror=alert(1)>`;
const els = new Map();
const el = (s) => { if (!els.has(s)) els.set(s, {innerHTML:'', textContent:'', hidden:true, value:'', options:[], dataset:{}, classList:{toggle(){}}, addEventListener(){}}); return els.get(s); };
const tab = el('runsTab'); tab.dataset.tab = 'runs';
let mode = 'ok'; const unhandled = [];
process.on('unhandledRejection', (e) => unhandled.push(e));
const ctx = vm.createContext({
  document: {querySelector: el, querySelectorAll: (s) => s === '.tabs button' ? [tab] : [], addEventListener(){}},
  fetch: async (p) => {
    if (p === '/api/status') return {ok: true, json: async () => ({games: [], discord: false, job: null})};
    if (mode === 'net') throw new Error('offline');
    if (mode === 503) return {ok: false, statusText: 'x', json: async () => ({detail: bad})};
    return {ok: true, json: async () => [{ok: 1, kind: 'k', game: 'g', started: 's', finished: 'f', dataset_date: 'd', note: 'n'}]};
  },
  setTimeout(){}, FormData: class{*[Symbol.iterator](){}}, URLSearchParams, alert(){ throw Error('alert'); },
});
vm.runInContext(source, ctx);
const turn = () => new Promise((r) => setImmediate(r));
(async () => {
  await turn();
  tab.onclick(); await turn();
  assert.match(el('#runs tbody').innerHTML, /run-ok/);
  assert.equal(el('#runsError').hidden, true);
  for (const m of ['net', 503]) {
    mode = m; tab.onclick(); await turn(); await turn();
    assert.equal(unhandled.length, 0, 'no unhandled rejection');
    assert(!el('#runs tbody').innerHTML.includes('run-ok'), 'stale rows cleared');
    assert.equal(el('#runsError').hidden, false);
    assert.match(el('#runsError').textContent, /Could not load job runs/);
    assert(el('#runs tbody').innerHTML.includes('unavailable'));
    if (m === 503) assert(el('#runsError').textContent.includes(bad)); // textContent, never markup
  }
  mode = 'ok'; tab.onclick(); await turn();
  assert.match(el('#runs tbody').innerHTML, /run-ok/);
  assert.equal(el('#runsError').hidden, true);
  assert.equal(el('#runsError').textContent, '');
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
