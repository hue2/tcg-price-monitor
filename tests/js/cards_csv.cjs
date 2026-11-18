// Headless Cards CSV export test: escaping, injection prefixes, filter/sort parity (usage: node cards_csv.cjs <repo>)
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] + '/src/tcgwatch/web/static/app.js', 'utf8');
const ok = (j) => ({ok: true, status: 200, json: async () => j});
const turn = () => new Promise((r) => setImmediate(r));
const row = (o) => ({product_id: 1, name: 'N', number: '1', rarity: 'R', printing: 'Normal', set: 'S', base_date: '2026-01-01',
  base: 1, cur: 2, pct: 100, flagged: false, in_scope: true, field: 'market', url: '', ...o});
const ROWS = [
  row({product_id: 1, name: 'Alpha, "Big" One', pct: 50, flagged: true, url: 'https://x/a'}),
  row({product_id: 2, name: '=HYPERLINK("http://evil")', pct: -5.5, rarity: 'M'}),
  row({product_id: 3, name: '+1', number: '-2'}), row({product_id: 4, name: '@SUM(A1)', pct: 10}),
  row({product_id: 5, name: '\tTab'}), row({product_id: 6, name: '\rCR'}),
  row({product_id: 7, name: 'Multi\nLine', pct: 20}),
  row({product_id: 8, name: 'Out', in_scope: false, pct: 99}),
  row({product_id: 9, name: 'Other set', set: 'Z', pct: 30}),
];
const els = new Map(), blobs = [], clicks = [];
const el = (s) => { if (!els.has(s)) els.set(s, {innerHTML:'', textContent:'', hidden:true, value:'', options:[], dataset:{}, classList:{toggle(){}}, addEventListener(){}}); return els.get(s); };
el('#cardControls [name=game]').value = 'mtg'; el('#show').value = 'scope'; el('#wlDays').value = '30';
const ctx = vm.createContext({
  document: {querySelector: el, querySelectorAll: () => [], addEventListener(){},
    createElement: () => ({click() { clicks.push(this); }})},
  fetch: (p) => Promise.resolve(p === '/api/status' ? ok({games: [], discord: false, job: null, auth_enabled: false})
    : p.startsWith('/api/cards/') ? ok({rows: ROWS, rule: 'r', current_date: 'd', window_days: 7, discord_threshold_pct: 10}) : ok([])),
  setTimeout(){}, FormData: class{*[Symbol.iterator](){}}, URLSearchParams, alert(){},
  Blob: class { constructor(parts, opts) { this.text = parts.join(''); this.type = opts.type; blobs.push(this); } },
  URL: {createObjectURL: () => 'blob:x', revokeObjectURL(){}},
});
vm.runInContext(source, ctx);
const run = (c) => vm.runInContext(c, ctx);
const parse = (csv) => { // minimal RFC 4180 parser
  const out = []; let r = [], f = '', q = false;
  for (let i = 0; i < csv.length; i++) { const c = csv[i];
    if (q) { if (c === '"') { if (csv[i+1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
    else if (c === '"') q = true; else if (c === ',') { r.push(f); f = ''; }
    else if (c === '\r' && csv[i+1] === '\n') { r.push(f); out.push(r); r = []; f = ''; i++; } else f += c; }
  assert.equal(f, ''); assert.equal(r.length, 0); return out;
};
(async () => {
  await turn(); await turn(); await turn();
  ctx.__R = ROWS; run('rows = __R');
  // ---- header + escaping + injection ----
  const all = parse(run('cardsCsv(rows)'));
  assert.deepEqual(all[0], ['name','number','rarity','printing','set','base_date','base','cur','pct','flagged','url']);
  assert.equal(all.length, ROWS.length + 1);
  assert.deepEqual(all[1], ['Alpha, "Big" One','1','R','Normal','S','2026-01-01','1','2','50','true','https://x/a']);
  assert.equal(all[2][0], `'=HYPERLINK("http://evil")`);
  assert.equal(all[2][8], '-5.5');             // numeric column stays a number
  assert.equal(all[3][0], "'+1"); assert.equal(all[3][1], "'-2");
  assert.equal(all[4][0], "'@SUM(A1)");
  assert.equal(all[5][0], "'\tTab"); assert.equal(all[6][0], "'\rCR");
  assert.equal(all[7][0], 'Multi\nLine');
  assert.ok(run('cardsCsv(rows)').includes('"Multi\nLine"'));
  assert.ok(run('cardsCsv(rows)').endsWith('\r\n'));
  assert.equal(run('csvCell(null)'), ''); assert.equal(run('csvCell(undefined)'), ''); assert.equal(run('csvCell(NaN)'), '');
  assert.equal(run('csvCell("safe")'), 'safe');
  // ---- filter/sort parity: the CSV is exactly visibleRows() ----
  const names = (csv) => parse(csv).slice(1).map((r) => r[0].replace(/^'(?=[=+\-@\t\r])/, ''));
  const parity = () => assert.deepEqual(names(run('cardsCsv(visibleRows())')), run('visibleRows()').map((r) => r.name));
  parity();
  assert.ok(!names(run('cardsCsv(visibleRows())')).includes('Out'));   // scope hides filtered-out
  el('#show').value = 'all'; parity(); assert.ok(names(run('cardsCsv(visibleRows())')).includes('Out'));
  el('#show').value = 'flagged'; parity(); assert.deepEqual(names(run('cardsCsv(visibleRows())')), ['Alpha, "Big" One']);
  el('#show').value = 'scope'; el('#setFilter').value = 'Z'; parity(); assert.deepEqual(names(run('cardsCsv(visibleRows())')), ['Other set']);
  el('#setFilter').value = ''; el('#rarityFilter').value = 'M'; parity(); assert.equal(names(run('cardsCsv(visibleRows())')).length, 1);
  el('#rarityFilter').value = ''; el('#search').value = 'alpha'; parity(); assert.equal(names(run('cardsCsv(visibleRows())')).length, 1);
  el('#search').value = '';
  run('sortKey = "pct"; sortDir = -1'); parity();
  assert.equal(names(run('cardsCsv(visibleRows())'))[0], 'Out'.length && run('visibleRows()')[0].name);
  run('sortKey = "name"; sortDir = 1'); parity();
  // beyond the page: all matching rows exported, not just `shown`
  run('shown = 2'); assert.equal(parse(run('cardsCsv(visibleRows())')).length - 1, run('visibleRows()').length);
  // ---- button: Blob download of exactly that text ----
  run('shown = 200');
  el('#csvBtn').onclick();
  assert.equal(blobs.length, 1); assert.equal(clicks.length, 1);
  assert.equal(clicks[0].download, 'cards.csv'); assert.equal(clicks[0].href, 'blob:x');
  assert.match(blobs[0].type, /^text\/csv/);
  assert.equal(blobs[0].text, run('cardsCsv(visibleRows())'));
  console.log('cards_csv ok');
})().catch((e) => { console.error(e); process.exit(1); });
