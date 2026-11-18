// Headless race regressions for Alerts + Runs: a stale (older) completion must never write the DOM (usage: node stale_requests.cjs <repo>)
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] + '/src/tcgwatch/web/static/app.js', 'utf8');
const els = new Map();
const el = (s) => { if (!els.has(s)) els.set(s, {innerHTML:'', textContent:'', hidden:true, value:'', options:[], dataset:{}, classList:{toggle(){}}, addEventListener(){}}); return els.get(s); };
const aTab = el('aTab'); aTab.dataset.tab = 'alerts';
const rTab = el('rTab'); rTab.dataset.tab = 'runs';
const pending = []; // deferred fetches, in call order
const unhandled = [];
process.on('unhandledRejection', (e) => unhandled.push(e));
const ctx = vm.createContext({
  document: {querySelector: el, querySelectorAll: (s) => s === '.tabs button' ? [aTab, rTab] : [], addEventListener(){}},
  fetch: (p) => {
    if (p === '/api/status') return Promise.resolve({ok: true, json: async () => ({games: [], discord: false, job: null})});
    return new Promise((resolve, reject) => pending.push({p, resolve, reject}));
  },
  setTimeout(){}, FormData: class{*[Symbol.iterator](){}}, URLSearchParams, alert(){ throw Error('alert'); },
});
vm.runInContext(source, ctx);
const turn = () => new Promise((r) => setImmediate(r));
const okRes = (rows) => ({ok: true, json: async () => rows});
const alertRow = (name) => ({alerted_on: 'd', printing: 'N', rarity: 'R', game: 'g', window_days: 7, baseline: 1, current: 2, pct_change: 1, discord: 1, name, url: null});
const runRow = (kind) => ({ok: 1, kind, game: 'g', started: 's', finished: 'f', dataset_date: 'd', note: 'n'});
const cases = {
  alerts: {tab: aTab, body: '#alerts tbody', err: '#alertsError', row: alertRow, trigger: () => aTab.onclick()},
  runs:   {tab: rTab, body: '#runs tbody', err: '#runsError', row: runRow, trigger: () => rTab.onclick()},
};
(async () => {
  await turn();
  for (const [name, c] of Object.entries(cases)) {
    const start = async () => { pending.length = 0; c.trigger(); await turn(); c.trigger(); await turn(); assert.equal(pending.length, 2); return pending.slice(); };
    // 1. older success finishing AFTER newer success must not overwrite
    let [a, b] = await start();
    b.resolve(okRes([c.row('NEW')])); await turn(); await turn();
    a.resolve(okRes([c.row('OLD')])); await turn(); await turn();
    assert(el(c.body).innerHTML.includes('NEW') && !el(c.body).innerHTML.includes('OLD'), name + ': stale success overwrote');
    // 2. older failure finishing AFTER newer success must not wipe it or show an error
    [a, b] = await start();
    b.resolve(okRes([c.row('GOOD')])); await turn(); await turn();
    a.reject(new Error('late boom')); await turn(); await turn();
    assert(el(c.body).innerHTML.includes('GOOD'), name + ': stale failure wiped newer rows');
    assert.equal(el(c.err).hidden, true, name + ': stale failure showed error');
    assert(!el(c.err).textContent.includes('late boom'));
    // 3. older success arriving after newer FAILURE must not hide the error / restore stale rows
    [a, b] = await start();
    b.reject(new Error('newer fail')); await turn(); await turn();
    a.resolve(okRes([c.row('STALE')])); await turn(); await turn();
    assert.equal(el(c.err).hidden, false, name + ': stale success hid newer error');
    assert(!el(c.body).innerHTML.includes('STALE'));
    // 4. in-order completion still works (older first, then newer)
    [a, b] = await start();
    a.resolve(okRes([c.row('A1')])); await turn(); await turn();
    assert(!el(c.body).innerHTML.includes('A1'), name + ': superseded older rendered');
    b.resolve(okRes([c.row('B1')])); await turn(); await turn();
    assert(el(c.body).innerHTML.includes('B1') && el(c.err).hidden === true);
  }
  assert.equal(unhandled.length, 0, 'no unhandled rejection');
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
