// Headless run of static/app.js: Alerts tab render, escaping, filters, failure + recovery (usage: node alerts_tab.cjs <repo>)
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] + '/src/tcgwatch/web/static/app.js', 'utf8');
const bad = `<img src=x onerror=alert(1)>`;
const els = new Map();
const el = (s) => { if (!els.has(s)) els.set(s, {innerHTML:'', textContent:'', hidden:true, value:'', options:[], dataset:{}, classList:{toggle(){}}, addEventListener(){}}); return els.get(s); };
const tab = el('alertsTab'); tab.dataset.tab = 'alerts';
let mode = 'ok'; const unhandled = [], urls = [];
process.on('unhandledRejection', (e) => unhandled.push(e));
const row = (o) => ({alerted_on: '2026-11-01', printing: 'Normal', rarity: 'R', game: 'g', window_days: 7, baseline: 1, current: 2, pct_change: 100, discord: 1, ...o});
const ctx = vm.createContext({
  document: {querySelector: el, querySelectorAll: (s) => s === '.tabs button' ? [tab] : [], addEventListener(){}},
  fetch: async (p) => {
    if (p === '/api/status') return {ok: true, json: async () => ({games: [], discord: false, job: null})};
    urls.push(p);
    if (mode === 'net') throw new Error('offline');
    if (mode === 503) return {ok: false, statusText: 'x', json: async () => ({detail: bad})};
    if (mode === 'empty') return {ok: true, json: async () => []};
    return {ok: true, json: async () => [
      row({name: bad, url: 'https://tcgplayer.com/p/1', discord: 1}),
      row({name: 'Evil', url: 'javascript:alert(1)', discord: 0, rarity: bad}),
      row({name: null, url: null, discord: 0}),
    ]};
  },
  setTimeout(){}, FormData: class{*[Symbol.iterator](){}}, URLSearchParams, alert(){ throw Error('alert'); },
});
vm.runInContext(source, ctx);
const turn = () => new Promise((r) => setImmediate(r));
(async () => {
  await turn();
  el('#alertDays').value = '30'; el('#alertFilter').value = 'false';
  tab.onclick(); await turn();
  const html = el('#alerts tbody').innerHTML;
  assert(urls.at(-1).includes('discord=false') && urls.at(-1).includes('days=30'), urls.at(-1));
  assert(!html.includes('<img'), 'name escaped');
  assert(html.includes('&lt;img'), 'name shown escaped');
  assert(html.includes('href="https://tcgplayer.com/p/1"'), 'card link');
  assert(!html.includes('javascript:'), 'unsafe link dropped');
  assert(html.includes('(card removed)'));
  assert(html.includes('alert-posted') && html.includes('alert-quiet'));
  assert.equal(el('#alertsError').hidden, true);
  el('#alertFilter').value = ''; el('#alertDays').value = '';
  el('#alertFilter').onchange(); await turn();
  assert(!urls.at(-1).includes('discord=') && !urls.at(-1).includes('days='), urls.at(-1));
  for (const m of ['net', 503]) {
    mode = m; tab.onclick(); await turn(); await turn();
    assert.equal(unhandled.length, 0, 'no unhandled rejection');
    assert(!el('#alerts tbody').innerHTML.includes('alert-posted'), 'stale rows cleared');
    assert.equal(el('#alertsError').hidden, false);
    assert.match(el('#alertsError').textContent, /Could not load alerts/);
    assert(el('#alerts tbody').innerHTML.includes('unavailable'));
    if (m === 503) assert(el('#alertsError').textContent.includes(bad));
  }
  mode = 'empty'; tab.onclick(); await turn();
  assert(el('#alerts tbody').innerHTML.includes('No alerts recorded'));
  assert.equal(el('#alertsError').hidden, true);
  mode = 'ok'; tab.onclick(); await turn();
  assert.match(el('#alerts tbody').innerHTML, /alert-posted/);
  assert.equal(el('#alertsError').textContent, '');
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
