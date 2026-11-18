// Headless UX-2 regression: one banner on 401, no repeat requests, Log out button only when auth is on (usage: node auth_401.cjs <repo>)
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2] + '/src/tcgwatch/web/static/app.js', 'utf8');
// One process-wide listener; each boot() gets its own collector so nothing is swallowed silently.
let sink = null;
process.on('unhandledRejection', (e) => { if (sink) sink.push(e); else throw e; });
function boot(statusRes) {
  const els = new Map();
  const el = (s) => { if (!els.has(s)) els.set(s, {innerHTML:'', textContent:'', hidden:true, value:'', options:[], dataset:{}, classList:{toggle(){}}, addEventListener(){}}); return els.get(s); };
  const calls = [], rejections = [];
  sink = rejections;
  const ctx = vm.createContext({
    document: {querySelector: el, querySelectorAll: () => [], addEventListener(){}},
    fetch: (p) => { calls.push(p); return Promise.resolve(statusRes(p)); },
    setTimeout(){}, FormData: class{*[Symbol.iterator](){}}, URLSearchParams, alert(){},
  });
  vm.runInContext(source, ctx);
  return {el, calls, ctx, rejections};
}
const turn = () => new Promise((r) => setImmediate(r));
const ok = (j) => ({ok: true, status: 200, json: async () => j});
const r401 = (detail = 'authentication required') => ({ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({detail})});
const status = (auth) => ok({games: [], discord: false, job: null, auth_enabled: auth});
(async () => {
  // button visibility
  let t = boot(() => status(true)); await turn(); await turn();
  assert.equal(t.el('#logout').hidden, false, 'logout shown when auth on');
  assert.equal(t.el('#authBanner').hidden, true);
  assert.equal(t.rejections.length, 0, 'no rejections on healthy boot');
  t = boot(() => status(false)); await turn(); await turn();
  assert.equal(t.el('#logout').hidden, true, 'logout hidden when auth off');
  assert.equal(t.rejections.length, 0);
  // 401: one banner, later requests suppressed
  t = boot(() => r401());
  await turn(); await turn();
  // loadStatus().then(loadCards) rejects on the initial 401: expected exactly once, and only that.
  assert.equal(t.rejections.length, 1, 'exactly the expected startup rejection');
  assert.match(String(t.rejections[0] && t.rejections[0].message), /Signed out/);
  assert.equal(t.el('#authBanner').hidden, false, 'banner shown');
  const n = t.calls.length;
  assert.equal(n, 1, 'only the first request went out');
  await assert.rejects(vm.runInContext('api("/api/alerts")', t.ctx), /Signed out/);
  await assert.rejects(vm.runInContext('api("/api/job-runs")', t.ctx), /Signed out/);
  assert.equal(t.calls.length, n, 'no repeat requests after 401');
  assert.equal(t.el('#authBanner').hidden, false);
  await turn();
  assert.equal(t.rejections.length, 1, 'no unexpected rejections after the startup one');

  // malicious 401 detail is never rendered
  const evil = '<img src=x onerror=alert(1)>';
  t = boot(() => r401(evil)); await turn(); await turn();
  assert.equal(t.el('#authBanner').hidden, false);
  for (const k of ['#authBanner', '#alertsError', '#runsError', '#status', '#cards'])
    for (const f of ['innerHTML', 'textContent'])
      assert(!String(t.el(k)[f]).includes('<img') && !String(t.el(k)[f]).includes('onerror'), k + ' ' + f);
  assert.equal(t.el('#authBanner').innerHTML, '', 'banner is static markup, never filled from the response');
  assert.equal(t.rejections.length, 1);

  // a stale older success cannot erase a newer 401 banner
  const pending = [];
  t = boot((p) => ({then: (res) => pending.push({p, res})}));
  await turn();
  pending.shift().res(status(true)); await turn(); await turn();
  const a = vm.runInContext('loadAlerts()', t.ctx), b = vm.runInContext('loadAlerts()', t.ctx);
  await turn();
  const [older, newer] = pending.splice(-2);
  newer.res(r401(evil)); await b;
  assert.equal(t.el('#authBanner').hidden, false);
  assert(!t.el('#alertsError').textContent.includes('<img'), 'detail leaked into the alerts error text');
  older.res(ok([])); await a;
  assert.equal(t.el('#authBanner').hidden, false, 'stale success did not hide the banner');
  assert.equal(t.el('#alertsError').hidden, false, 'stale success did not clear the error');
  const before = t.calls.length;
  await assert.rejects(vm.runInContext('api("/api/status")', t.ctx), /Signed out/);
  assert.equal(t.calls.length, before);

  // an OLDER failure cannot overwrite a NEWER success (catch-side alertsGen guard)
  const pend2 = [];
  t = boot((p) => ({then: (res) => pend2.push({p, res})}));
  await turn();
  pend2.shift().res(status(true)); await turn(); await turn();
  const c = vm.runInContext('loadAlerts()', t.ctx), d = vm.runInContext('loadAlerts()', t.ctx);
  await turn();
  const [oldF, newOk] = pend2.splice(-2);
  newOk.res(ok([])); await d;
  assert.equal(t.el('#alertsError').hidden, true);
  assert.match(t.el('#alertsSummary').textContent, /0 alert/);
  oldF.res({ok: false, status: 500, statusText: 'Server Error', json: async () => ({detail: 'boom'})}); await c;
  assert.equal(t.el('#alertsError').hidden, true, 'stale failure re-showed the error');
  assert.equal(t.el('#alertsError').textContent, '', 'stale failure wrote error text');
  assert.match(t.el('#alertsSummary').textContent, /0 alert/, 'stale failure cleared the summary');
  assert(!t.el('#alerts tbody').innerHTML.includes('unavailable'), 'stale failure replaced the rows');
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
