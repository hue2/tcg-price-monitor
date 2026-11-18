// Headless watchlist UI test: add/remove/list against stubbed fetch, failed PUT, 401 path (usage: node watchlist.cjs <repo>)
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] + '/src/tcgwatch/web/static/app.js', 'utf8');
let sink = null;
process.on('unhandledRejection', (e) => { if (sink) sink.push(e); else throw e; });
const ok = (j) => ({ok: true, status: 200, json: async () => j});
const err = (status, detail) => ({ok: false, status, statusText: 'Err' + status, json: async () => ({detail})});
const turn = () => new Promise((r) => setImmediate(r));
const status = ok({games: [], discord: false, job: null, auth_enabled: true});

function boot(route) {
  const els = new Map(), listeners = {};
  const el = (s) => { if (!els.has(s)) els.set(s, {innerHTML:'', textContent:'', hidden:true, value:'', options:[], dataset:{}, classList:{toggle(){}}, addEventListener(){}}); return els.get(s); };
  const tabs = ['cards', 'watchlist'].map((n) => ({dataset: {tab: n}, classList: {toggle(){}}}));
  const calls = [], alerts = [], rejections = [];
  sink = rejections;
  el('#cardControls [name=game]').value = 'mtg';
  el('#wlDays').value = '30';
  el('#show').value = 'all';
  const ctx = vm.createContext({
    document: {querySelector: el, querySelectorAll: (q) => (q === '.tabs button' ? tabs : []), addEventListener: (t, f) => { listeners[t] = f; }},
    fetch: (p, o = {}) => {
      calls.push({path: p, method: o.method || 'GET', body: o.body === undefined ? undefined : JSON.parse(o.body)});
      return Promise.resolve(p === '/api/status' ? status : route(p, o));
    },
    setTimeout(){}, FormData: class{*[Symbol.iterator](){}}, URLSearchParams,
    alert: (m) => alerts.push(m),
  });
  vm.runInContext(source, ctx);
  // synthetic star button; click(star) drives the document-level click handler
  const star = (pid, printing, pressed, inWl) => {
    const s = {dataset: {star: String(pid), printing}, getAttribute: () => String(pressed),
      closest: (sel) => (sel === '#watchlist' && inWl ? {} : null)};
    return {closest: (sel) => (sel === '[data-star]' ? s : null)};
  };
  const click = async (target) => { await listeners.click({target}); await turn(); await turn(); };
  return {el, tabs, calls, alerts, rejections, ctx, click, star};
}
const item = (pid, printing, name) => ({product_id: pid, printing, name, number: '1', rarity: 'R', set: 'S', cur: 2, pct: 1.5, field: 'market', points: [], url: ''});
const rowsOf = (t) => (t.el('#watchlist tbody').innerHTML.match(/<tr>/g) || []).length;

(async () => {
  // ---- list ----
  let list = [item(1, 'Normal', 'Alpha'), item(2, 'Foil', 'Beta')];
  let t = boot((p) => ok(list));
  await turn(); await turn();
  t.calls.length = 0;
  await vm.runInContext('loadWatchlist()', t.ctx);
  assert.deepEqual(t.calls, [{path: '/api/watchlist/mtg?days=30', method: 'GET', body: undefined}]);
  assert.equal(rowsOf(t), 2);
  assert.match(t.el('#wlSummary').textContent, /2 watched card/);
  assert.match(t.el('#watchlist tbody').innerHTML, /Alpha/);
  assert.match(t.el('#watchlist tbody').innerHTML, /data-star="2" data-printing="Foil"/);
  list = [];
  await vm.runInContext('loadWatchlist()', t.ctx);
  assert.equal(rowsOf(t), 0);
  assert.match(t.el('#wlSummary').textContent, /Nothing watched yet/);

  // ---- add (star on Cards tab, unpressed -> PUT, row flagged watched) ----
  t = boot(() => ok({}));
  await turn(); await turn();
  vm.runInContext(`rows = [{product_id: 7, printing: 'Foil & Co', name: 'Gamma', number: '3', rarity: 'R', set: 'S', pct: 0, base: 1, cur: 1, base_date: 'd', field: 'market', in_scope: true, flagged: false, watched: false}]`, t.ctx);
  t.calls.length = 0;
  await t.click(t.star(7, 'Foil & Co', false, false));
  assert.deepEqual(t.calls, [{path: '/api/watchlist', method: 'PUT', body: {product_id: 7, printing: 'Foil & Co'}}]);
  assert.equal(vm.runInContext('rows[0].watched', t.ctx), true);
  assert.match(t.el('#cards tbody').innerHTML, /star on/);
  assert.equal(t.alerts.length, 0);

  // ---- remove from watchlist tab (pressed -> DELETE with encoded printing, then list reloads) ----
  list = [item(2, 'Foil', 'Beta')];
  t = boot((p, o) => (o.method === 'DELETE' ? ok({}) : ok(list)));
  await turn(); await turn();
  t.calls.length = 0;
  list = [];
  await t.click(t.star(5, 'Foil & Co', true, true));
  assert.deepEqual(t.calls[0], {path: '/api/watchlist/5?printing=Foil%20%26%20Co', method: 'DELETE', body: undefined});
  assert.deepEqual(t.calls[1], {path: '/api/watchlist/mtg?days=30', method: 'GET', body: undefined}, 'list reloaded after delete');
  assert.equal(t.calls.length, 2);
  assert.match(t.el('#wlSummary').textContent, /Nothing watched yet/);
  assert.equal(t.alerts.length, 0);

  // ---- failed PUT: error shown, list/rows unchanged, no reload ----
  list = [item(1, 'Normal', 'Alpha')];
  t = boot((p, o) => (o.method === 'PUT' ? err(500, 'db locked') : ok(list)));
  await turn(); await turn();
  await vm.runInContext('loadWatchlist()', t.ctx);
  vm.runInContext(`rows = [{product_id: 7, printing: 'Normal', name: 'Gamma', number: '3', rarity: 'R', set: 'S', pct: 0, base: 1, cur: 1, base_date: 'd', field: 'market', in_scope: true, flagged: false, watched: false}]`, t.ctx);
  const htmlBefore = t.el('#watchlist tbody').innerHTML, cardsBefore = t.el('#cards tbody').innerHTML;
  t.calls.length = 0;
  await t.click(t.star(7, 'Normal', false, false));
  assert.deepEqual(t.alerts, ['db locked']);
  assert.equal(t.calls.length, 1, 'only the PUT went out, no reload');
  assert.equal(vm.runInContext('rows[0].watched', t.ctx), false, 'row not marked watched');
  assert.equal(t.el('#watchlist tbody').innerHTML, htmlBefore, 'watchlist unchanged');
  assert.equal(t.el('#cards tbody').innerHTML, cardsBefore, 'cards unchanged');
  assert.equal(t.rejections.length, 0);
  // failed DELETE leaves the list too
  list = [item(1, 'Normal', 'Alpha')];
  t = boot((p, o) => (o.method === 'DELETE' ? err(404, 'not watched') : ok(list)));
  await turn(); await turn();
  await vm.runInContext('loadWatchlist()', t.ctx);
  const before = t.el('#watchlist tbody').innerHTML; t.calls.length = 0;
  await t.click(t.star(1, 'Normal', true, true));
  assert.deepEqual(t.alerts, ['not watched']);
  assert.equal(t.calls.length, 1);
  assert.equal(t.el('#watchlist tbody').innerHTML, before);

  // ---- unstar from the Cards tab: DELETE then renderCards, no list reload ----
  t = boot(() => ok({}));
  await turn(); await turn();
  vm.runInContext(`rows = [{product_id: 7, printing: 'Foil & Co', name: 'Gamma', number: '3', rarity: 'R', set: 'S', pct: 0, base: 1, cur: 1, base_date: 'd', field: 'market', in_scope: true, flagged: false, watched: true}]`, t.ctx);
  vm.runInContext('renderCards()', t.ctx);
  assert.match(t.el('#cards tbody').innerHTML, /star on/);
  t.calls.length = 0;
  await t.click(t.star(7, 'Foil & Co', true, false));
  assert.deepEqual(t.calls, [{path: '/api/watchlist/7?printing=Foil%20%26%20Co', method: 'DELETE', body: undefined}]);
  assert.equal(vm.runInContext('rows[0].watched', t.ctx), false);
  assert.doesNotMatch(t.el('#cards tbody').innerHTML, /star on/);
  assert.equal(t.alerts.length, 0);
  assert.equal(t.rejections.length, 0);

  // ---- DELETE ok but the follow-up list reload fails: no unhandled rejection, error shown ----
  t = boot((p, o) => (o.method === 'DELETE' ? ok({}) : p.startsWith('/api/watchlist/') ? err(500, 'boom') : ok({})));
  await turn(); await turn();
  t.rejections.length = 0;
  await t.click(t.star(5, 'Normal', true, true));
  assert.equal(t.rejections.length, 0, 'reload failure must not be an unhandled rejection');
  assert.equal(t.alerts.length, 0);
  assert.match(t.el('#wlSummary').textContent, /boom/);

  // ---- 401 during a watchlist action: Signed-out path, one banner, no more requests ----
  t = boot((p, o) => err(401, '<img src=x onerror=alert(1)>'));
  await turn(); await turn();
  // startup status is stubbed OK, so authLost is not yet set
  assert.equal(t.el('#authBanner').hidden, true);
  assert.equal(t.rejections.length, 0);
  vm.runInContext(`rows = [{product_id: 7, printing: 'Normal', name: 'G', number: '3', rarity: 'R', set: 'S', pct: 0, base: 1, cur: 1, base_date: 'd', field: 'market', in_scope: true, flagged: false, watched: false}]`, t.ctx);
  t.calls.length = 0;
  await t.click(t.star(7, 'Normal', false, false));
  assert.equal(t.calls.length, 1);
  assert.equal(t.el('#authBanner').hidden, false, 'banner shown');
  assert.equal(t.alerts.length, 1);
  assert.match(t.alerts[0], /Signed out/);
  assert(!t.alerts[0].includes('<img'), 'server detail not surfaced');
  assert.equal(vm.runInContext('rows[0].watched', t.ctx), false);
  await t.click(t.star(7, 'Normal', false, false));
  assert.equal(t.calls.length, 1, 'no repeat request after 401');
  assert.equal(t.alerts.length, 2, 'second click alerts again (Signed out), one per click');
  assert.match(t.alerts[1], /Signed out/);
  assert.equal(t.rejections.length, 0, 'click handler swallowed the error itself');
  // tab-driven loadWatchlist after sign-out rejects exactly once, with Signed out
  await assert.rejects(vm.runInContext('loadWatchlist()', t.ctx), /Signed out/);
  assert.equal(t.calls.length, 1);
  // a 401 on the list load itself: exactly one rejection from the unawaited call
  t = boot((p) => (p.startsWith('/api/watchlist') ? err(401, 'x') : ok({})));
  await turn(); await turn();
  vm.runInContext('loadWatchlist()', t.ctx);
  await turn(); await turn();
  assert.equal(t.rejections.length, 1, 'exactly one rejection');
  assert.match(String(t.rejections[0].message), /Signed out/);
  assert.equal(t.el('#authBanner').hidden, false);
  // ---- tab switch / days change with a failing list: message in #wlSummary, no unhandled rejection ----
  for (const how of ['tab', 'days']) {
    let fail = false;
    t = boot((p) => (fail && p.startsWith('/api/watchlist/') ? err(500, 'list down ' + how) : ok([])));
    await turn(); await turn();
    fail = true; t.rejections.length = 0; t.calls.length = 0;
    if (how === 'tab') t.tabs[1].onclick(); else t.el('#wlDays').onchange();
    await turn(); await turn();
    assert.equal(t.calls.length, 1);
    assert.equal(t.rejections.length, 0, how + ': no unhandled rejection');
    assert.equal(t.el('#wlSummary').textContent, 'list down ' + how);
  }
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
