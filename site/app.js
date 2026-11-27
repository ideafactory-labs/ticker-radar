/* Ticker Radar — static page over data/*.json built by radar/build.py.
 * Your watchlist, watched IPOs and chosen indicators live in this browser (localStorage);
 * the page only downloads public data files and never sends anything about your list. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const DAY = 86400000;
  // ?today=YYYY-MM-DD pins the date (tests only); everyone else gets the real date.
  const pinned = /^\d{4}-\d{2}-\d{2}$/.test(new URLSearchParams(location.search).get('today') || '') ? new URLSearchParams(location.search).get('today') : null;
  const today = pinned ? new Date(pinned + 'T00:00:00') : new Date(); today.setHours(0, 0, 0, 0);
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const parseDay = (s) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  const fmtDate = (s) => parseDay(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const fmtDay = (s) => parseDay(s).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safe = (t) => t.toUpperCase().replace(/[^A-Z0-9-]/g, '-');
  const daysOld = (s) => Math.round((today - parseDay(s)) / DAY);

  async function getJSON(path) {
    try { const r = await fetch(path, { cache: 'no-cache' }); if (!r.ok) return null; return await r.json(); } catch (e) { return null; }
  }

  // ---------- state on this device ----------
  const KEY = 'ticker-radar.v1';
  const DEFAULT = { watch: [{ tk: 'NVDA', alert: 5 }, { tk: 'AAPL', alert: 5 }, { tk: 'SPY', alert: 3 }, { tk: 'SGOV', alert: 2 }], ipos: [], inds: null, lastVisit: null, seen: {} };
  function load() { try { const s = JSON.parse(localStorage.getItem(KEY)); return s && Array.isArray(s.watch) ? Object.assign({}, DEFAULT, s) : null; } catch (e) { return null; } }
  function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* storage blocked: page still works for this visit */ } }
  const state = load() || JSON.parse(JSON.stringify(DEFAULT));
  const firstVisit = !state.lastVisit;
  const prevVisit = state.lastVisit || iso(new Date(today - 3 * DAY));

  // ---------- data ----------
  const D = {};
  // Every priced ticker comes in one file, so the server never learns which ones you watch.
  let pricesP = null;
  function allPrices() { if (!pricesP) pricesP = getJSON('data/prices.json'); return pricesP; }
  async function loadPrice(tk) { const doc = await allPrices(); return doc && doc.data[tk] ? doc.data[tk] : null; }

  function srcLabel(doc) {
    if (!doc) return 'unavailable';
    const badge = doc.official ? '' : '<span class="badge unofficial">unofficial</span>';
    return `${esc(doc.source)}${badge} · as of ${esc(fmtDate(doc.as_of))}`;
  }
  function staleNote(el, doc, maxDays) {
    if (!el) return;
    if (!doc) { el.hidden = false; el.textContent = 'This source could not be loaded. It will retry on the next scheduled refresh.'; return; }
    const old = daysOld(doc.as_of);
    if (doc.stale || old > maxDays) {
      el.hidden = false;
      el.textContent = `Stale: the latest data is from ${fmtDay(doc.as_of)}${doc.stale ? ' (the source failed on the last refresh, so this is the previous copy)' : ''}.`;
    } else el.hidden = true;
  }

  // ---------- watchlist ----------
  function spark(vals) {
    if (!vals || vals.length < 2) return '';
    const w = 200, h = 40, min = Math.min(...vals), max = Math.max(...vals), span = Math.max(max - min, Math.abs(max) * 0.02, 1e-9);
    const pts = vals.map((v, i) => [(i / (vals.length - 1)) * (w - 4) + 2, h - 4 - ((v - min) / span) * (h - 8)]);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const color = vals[vals.length - 1] >= vals[0] ? 'var(--up)' : 'var(--down)';
    const last = pts[pts.length - 1];
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><path d="${d} L ${w - 2} ${h} L 2 ${h} Z" fill="${color}" opacity="0.1"/><path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke"/><circle cx="${last[0]}" cy="${last[1]}" r="2.4" fill="${color}"/></svg>`;
  }
  const money = (v) => (v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v.toFixed(2));
  function nameOf(tk) { const r = (D.searchIndex || []).find((x) => x[0] === tk); return r ? r[1] : ''; }

  // ---------- signals ----------
  const sign = (v, d = 1) => (v > 0 ? '+' : '') + v.toFixed(d) + '%';
  const pctSpan = (v, d = 1) => (v == null ? '<span class="muted">n/a</span>' : `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : 'muted'}">${sign(v, d)}</span>`);
  // The one thing most worth knowing about this ticker right now, in priority order.
  function headline(p) {
    if (p.cross === 'death') return { cls: 'bad', text: 'Death cross', tip: 'The 50-day average fell below the 200-day average in the last two weeks. Often read as a weakening trend.' };
    if (p.cross === 'golden') return { cls: 'good', text: 'Golden cross', tip: 'The 50-day average rose above the 200-day average in the last two weeks. Often read as a strengthening trend.' };
    if (p.week_z != null && Math.abs(p.week_z) >= 2) return { cls: p.week_z > 0 ? 'good' : 'bad', text: `Unusual week (${sign(p.chg_1w)})`, tip: 'This week’s move is more than 2 standard deviations from this stock’s normal weekly move over the past year.' };
    if (p.from_high != null && p.from_high <= -20) return { cls: 'bad', text: `${Math.abs(p.from_high).toFixed(0)}% below 52-week high`, tip: 'A fall of 20% or more from the high is the usual definition of a bear market for a stock.' };
    if (p.from_high != null && p.from_high >= -2) return { cls: 'good', text: 'Near 52-week high', tip: 'Within 2% of its highest close in the past year.' };
    if (p.from_low != null && p.from_low <= 3) return { cls: 'bad', text: 'Near 52-week low', tip: 'Within 3% of its lowest close in the past year.' };
    if (p.vs_ma200 != null) return p.vs_ma200 >= 0
      ? { cls: 'good', text: 'Uptrend', tip: 'Trading above its 200-day average, a common long-term trend line.' }
      : { cls: 'bad', text: 'Downtrend', tip: 'Trading below its 200-day average, a common long-term trend line.' };
    return null;
  }
  function signalsBlock(p) {
    if (p.chg_1w == null && p.from_high == null) return '';
    const h = headline(p);
    const rows = [
      ['3 months', pctSpan(p.chg_3m)], ['vs S&P 500, 3 months', p.rel_3m == null ? '<span class="muted">n/a</span>' : `<span class="${p.rel_3m >= 0 ? 'up' : 'down'}">${p.rel_3m > 0 ? '+' : ''}${p.rel_3m.toFixed(1)} pts</span>`],
      ['From 52-week high', pctSpan(p.from_high)], ['From 52-week low', pctSpan(p.from_low)],
      ['vs 50-day average', pctSpan(p.vs_ma50)], ['vs 200-day average', pctSpan(p.vs_ma200)],
    ].map((r) => `<div class="row"><span>${r[0]}</span><span class="num">${r[1]}</span></div>`).join('');
    return `<div class="periods num"><span>1w ${pctSpan(p.chg_1w)}</span><span>1m ${pctSpan(p.chg_1m)}</span><span>YTD ${pctSpan(p.chg_ytd)}</span></div>
      ${h ? `<div class="signal ${h.cls}" title="${esc(h.tip)}">${esc(h.text)}</div>` : ''}
      <details class="more"><summary>More signals</summary>${h ? `<p class="tip">${esc(h.tip)}</p>` : ''}${rows}</details>`;
  }

  function renderGlance() {
    const doc = D.glance; const box = $('glance-panel');
    if (!doc || !doc.data.length) { box.hidden = true; return; }
    box.hidden = false;
    $('src-glance').innerHTML = srcLabel(doc);
    $('glance').innerHTML = `<thead><tr><th>Market</th><th>Day</th><th>1m</th><th>YTD</th><th>Trend</th></tr></thead><tbody>${doc.data.map((g) => {
      const trend = g.vs_ma200 == null ? '<span class="muted">n/a</span>' : (g.vs_ma200 >= 0 ? '<span class="up" aria-label="uptrend">▲</span>' : '<span class="down" aria-label="downtrend">▼</span>');
      return `<tr><td><strong>${esc(g.label)}</strong><small>${esc(g.note ? g.note + ' · ' : '')}${esc(g.ticker)}</small></td><td>${pctSpan(g.change_pct)}</td><td>${pctSpan(g.chg_1m)}</td><td>${pctSpan(g.chg_ytd)}</td><td style="font-family:var(--body)" title="Above or below its 200-day average">${trend}</td></tr>`;
    }).join('')}</tbody>`;
  }

  async function renderCards() {
    const el = $('cards');
    if (!state.watch.length) { el.innerHTML = '<div class="empty">Your watchlist is empty. Search above to add a stock or ETF.</div>'; return; }
    const docs = await Promise.all(state.watch.map((w) => loadPrice(w.tk)));
    el.textContent = '';
    state.watch.forEach((w, idx) => {
      const p = docs[idx];
      const card = document.createElement('article'); card.className = 'card'; card.dataset.tk = w.tk;
      const x = `<button class="x" data-rm="${idx}" aria-label="Remove ${esc(w.tk)}">✕</button>`;
      const nm = nameOf(w.tk);
      if (!p) {
        card.innerHTML = `${x}<div class="row"><span class="tk">${esc(w.tk)}</span></div><div class="name">${esc(nm)}</div><div class="nodata">No price data yet. This ticker is outside the priced universe, or its source had no data today.</div>`;
      } else {
        const chg = p.change_pct; const stale = daysOld(p.as_of) > 4;
        if (Math.abs(chg) >= w.alert) card.classList.add('moved');
        if (stale) card.classList.add('stale');
        const pos = ((p.close - p.low_52w) / ((p.high_52w - p.low_52w) || 1)) * 100;
        const opts = [2, 3, 5, 8, 10].map((v) => `<option value="${v}"${v === w.alert ? ' selected' : ''}>±${v}%</option>`).join('');
        card.innerHTML = `${x}<div class="row"><span class="tk">${esc(w.tk)}</span><span class="chg ${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span></div>
          <div class="name" title="${esc(nm)}">${esc(nm)}</div>
          <div class="px">$${money(p.close)}</div>
          ${spark(p.spark)}
          ${signalsBlock(p)}
          <div class="range"><div class="bar"><i style="left:calc(${Math.max(0, Math.min(100, pos)).toFixed(1)}% - 1px)"></i></div><div class="row"><span class="num">52w $${money(p.low_52w)}</span><span class="num">$${money(p.high_52w)}</span></div></div>
          <div class="row"><label class="thresh">Alert at <select id="al-${esc(safe(w.tk))}" data-al="${idx}" aria-label="Alert threshold for ${esc(w.tk)}">${opts}</select></label><span class="asof">${stale ? 'Stale · ' : ''}${esc(fmtDate(p.as_of))} · ${esc(p.source)}</span></div>`;
      }
      el.append(card);
    });
  }
  $('cards').addEventListener('click', (e) => { const b = e.target.closest('[data-rm]'); if (!b) return; state.watch.splice(+b.dataset.rm, 1); save(); renderCards(); renderAlerts(); renderCode(); });
  $('cards').addEventListener('change', (e) => { const s = e.target.closest('[data-al]'); if (!s) return; state.watch[+s.dataset.al].alert = +s.value; save(); renderCards(); renderAlerts(); renderCode(); });

  // ---------- search ----------
  const q = $('q'), res = $('results');
  let active = 0, matches = [];
  async function ensureIndex() {
    if (D.searchIndex) return D.searchIndex;
    const doc = await getJSON('data/search.json');
    D.searchIndex = doc ? doc.data : [];
    const pr = await allPrices();
    D.priced = new Set(pr ? Object.keys(pr.data) : []);
    return D.searchIndex;
  }
  function search(text) {
    const t = text.trim().toLowerCase(); if (!t) return [];
    const out = [];
    for (const r of D.searchIndex || []) {
      const tk = r[0].toLowerCase(), nm = r[1].toLowerCase();
      const score = tk === t ? 0 : tk.startsWith(t) ? 1 : nm.startsWith(t) ? 2 : (' ' + nm).includes(' ' + t) ? 3 : nm.includes(t) ? 4 : 9;
      if (score < 9) out.push([score + (D.priced && D.priced.has(r[0]) ? 0 : 0.5), r]);
    }
    return out.sort((a, b) => a[0] - b[0] || a[1][0].length - b[1][0].length).slice(0, 8).map((m) => m[1]);
  }
  async function showResults() {
    await ensureIndex();
    matches = search(q.value); res.textContent = '';
    if (!matches.length) { res.hidden = !q.value.trim(); if (q.value.trim()) res.innerHTML = '<div style="padding:10px 12px" class="muted">No US-listed ticker or ETF matches.</div>'; return; }
    matches.forEach((m, i) => {
      const b = document.createElement('button'); b.type = 'button'; b.setAttribute('role', 'option'); b.className = i === active ? 'active' : '';
      const have = state.watch.some((w) => w.tk === m[0]);
      const tag = have ? 'On your list' : (D.priced && !D.priced.has(m[0]) ? 'no price data' : m[2]);
      b.innerHTML = `<span class="tk">${esc(m[0])}</span><span>${esc(m[1])}</span><span class="tag">${esc(tag)}</span>`;
      b.addEventListener('click', () => add(m[0])); res.append(b);
    });
    res.hidden = false;
  }
  function add(tk) {
    if (!state.watch.some((w) => w.tk === tk)) state.watch.unshift({ tk, alert: 5 });
    save(); q.value = ''; res.hidden = true; active = 0; renderCards(); renderAlerts(); renderCode(); q.focus();
  }
  q.addEventListener('focus', ensureIndex, { once: true });
  q.addEventListener('input', () => { active = 0; showResults(); });
  q.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { active = Math.min(active + 1, matches.length - 1); showResults(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); showResults(); e.preventDefault(); }
    else if (e.key === 'Enter' && matches[active]) { add(matches[active][0]); e.preventDefault(); }
    else if (e.key === 'Escape') { res.hidden = true; }
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.search')) res.hidden = true; });

  // ---------- since your last visit ----------
  async function renderAlerts() {
    const el = $('alerts'); el.textContent = '';
    const items = [];
    const docs = await Promise.all(state.watch.map((w) => loadPrice(w.tk)));
    state.watch.forEach((w, i) => {
      const p = docs[i];
      if (p && p.as_of > prevVisit.slice(0, 10) && Math.abs(p.change_pct) >= w.alert) {
        items.push({ hot: true, cls: p.change_pct > 0 ? 'up' : 'down', text: `${w.tk} ${p.change_pct > 0 ? '+' : ''}${p.change_pct.toFixed(1)}% on ${fmtDate(p.as_of)} (your alert: ±${w.alert}%)` });
      }
    });
    state.watch.forEach((w, i) => {
      const p = docs[i]; if (!p || p.as_of <= prevVisit.slice(0, 10)) return;
      if (p.cross) items.push({ hot: true, cls: p.cross === 'golden' ? 'up' : 'down', text: `${w.tk}: ${p.cross} cross (50-day vs 200-day average)` });
      else if (p.week_z != null && Math.abs(p.week_z) >= 2.5 && Math.abs(p.change_pct) < w.alert) items.push({ hot: true, cls: p.week_z > 0 ? 'up' : 'down', text: `${w.tk}: unusual week, ${sign(p.chg_1w)}` });
    });
    for (const ipo of (D.ipos && D.ipos.data) || []) {
      if (!state.ipos.includes(ipoKey(ipo))) continue;
      for (const s of ipo.steps || []) {
        if (s.date > prevVisit && s.date <= iso(today)) items.push({ hot: true, text: `${ipo.name}: ${s.label.toLowerCase()} ${fmtDate(s.date)}` });
        const d = (parseDay(s.date) - today) / DAY;
        if (s.key === 'lockup' && d >= 0 && d <= 7) items.push({ hot: true, text: `${ipo.name}: lock-up ends in ${Math.round(d)} days (est.)` });
      }
    }
    if (D.bills && D.bills.data) {
      const b = D.bills.data.find((r) => r.label === '13-week bill');
      if (b && b.w1 != null && Math.abs(b.yield - b.w1) >= 0.15) items.push({ hot: true, text: `13-week bill ${b.yield > b.w1 ? '+' : ''}${Math.round((b.yield - b.w1) * 100)} bp in a week` });
    }
    if (!items.length) items.push({ text: firstVisit ? 'Welcome. Moves past your alert levels and IPO milestones you watch will show up here.' : 'Nothing crossed your alert levels.' });
    items.forEach((it) => { const c = document.createElement('span'); c.className = 'chip' + (it.hot ? ' hot' : ''); const d = document.createElement('span'); d.className = 'dot ' + (it.cls || ''); c.append(d, document.createTextNode(it.text)); el.append(c); });
    $('lastvisit').textContent = firstVisit ? 'First visit on this device' : 'Last visit ' + fmtDay(prevVisit);
  }

  // ---------- rates ----------
  function bp(a, b) { if (a == null || b == null) return '<span class="muted">—</span>'; const v = Math.round((a - b) * 100); return `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : 'muted'}">${v > 0 ? '+' : ''}${v} bp</span>`; }
  function renderRates() {
    const bills = D.bills, curve = D.curve, auc = D.auctions;
    $('src-rates').innerHTML = srcLabel(bills);
    staleNote($('stale-rates'), bills, 4);
    if (bills) {
      const extra = curve ? curve.data.filter((c) => c.tenor === '2 Yr' || c.tenor === '10 Yr').map((c) => ({ label: c.tenor.replace(' Yr', '-year note'), yield: c.now, w1: null, m1: c.m1 })) : [];
      const rows = bills.data.concat(extra).map((r) => `<tr><td>${esc(r.label)}</td><td>${r.yield.toFixed(2)}%</td><td>${bp(r.yield, r.w1)}</td><td>${bp(r.yield, r.m1)}</td></tr>`).join('');
      $('bills').innerHTML = `<thead><tr><th>Maturity</th><th>Yield</th><th>1 week</th><th>1 month</th></tr></thead><tbody>${rows}</tbody>`;
    } else $('bills').innerHTML = '<tbody><tr><td>Bill rates unavailable right now.</td></tr></tbody>';
    if (curve && curve.data.length > 1) {
      const pts = curve.data.filter((p) => p.now != null);
      const vals = pts.flatMap((p) => [p.now, p.m1]).filter((v) => v != null);
      const ymin = Math.floor((Math.min(...vals) - 0.05) * 10) / 10, ymax = Math.ceil((Math.max(...vals) + 0.05) * 10) / 10;
      const W = 320, H = 170, L = 34, R = 10, T = 12, B = 26;
      const X = (i) => L + (i / (pts.length - 1)) * (W - L - R), Y = (v) => T + (1 - (v - ymin) / (ymax - ymin || 1)) * (H - T - B);
      let g = '';
      const step = (ymax - ymin) > 1.2 ? 0.5 : 0.2;
      for (let v = Math.ceil(ymin / step) * step; v <= ymax + 1e-9; v += step) g += `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end" font-size="10" fill="var(--muted)" font-family="var(--mono)">${v.toFixed(1)}</text>`;
      pts.forEach((p, i) => { g += `<text x="${X(i)}" y="${H - 8}" text-anchor="middle" font-size="9.5" fill="var(--muted)" font-family="var(--mono)">${esc(p.tenor.replace(' Mo', 'm').replace(' Yr', 'y'))}</text>`; });
      const line = (k) => pts.map((p, i) => (p[k] == null ? '' : (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(p[k]).toFixed(1))).join(' ');
      if (pts.every((p) => p.m1 != null)) g += `<path d="${line('m1')}" fill="none" stroke="var(--muted)" stroke-width="1.4" stroke-dasharray="4 3"/>`;
      g += `<path d="${line('now')}" fill="none" stroke="var(--accent)" stroke-width="2"/>`;
      pts.forEach((p, i) => { g += `<circle cx="${X(i)}" cy="${Y(p.now)}" r="2.6" fill="var(--accent)"/>`; });
      g += `<text x="${W - R}" y="${T + 4}" text-anchor="end" font-size="10" fill="var(--accent)">${esc(fmtDate(curve.as_of))}</text><text x="${W - R}" y="${T + 16}" text-anchor="end" font-size="10" fill="var(--muted)">a month earlier</text>`;
      $('curve').innerHTML = g;
    }
    $('src-auctions').innerHTML = auc ? '· ' + srcLabel(auc) : '';
    if (auc && auc.data.length) {
      $('auctions').innerHTML = `<thead><tr><th>Security</th><th>Auction</th><th>Status</th><th>High rate</th></tr></thead><tbody>${auc.data.map((a) => `<tr><td>${esc(a.security)}</td><td>${esc(fmtDate(a.auction_date))}</td><td style="font-family:var(--body)">${esc(a.status)}</td><td>${a.high_rate != null ? a.high_rate.toFixed(3) + '%' : '—'}</td></tr>`).join('')}</tbody>`;
    } else $('auctions').innerHTML = '<tbody><tr><td>Auction data unavailable right now.</td></tr></tbody>';
  }

  // ---------- IPOs ----------
  const ipoKey = (p) => (p.ticker || p.name).toUpperCase();
  function renderIpos() {
    const doc = D.ipos; const el = $('ipos'); el.textContent = '';
    $('src-ipos').innerHTML = srcLabel(doc) + ' · dates in italics are estimated';
    staleNote($('stale-ipos'), doc, 3);
    if (!doc || !doc.data.length) { el.innerHTML = '<div class="empty">No IPO data right now.</div>'; }
    else {
      const t = iso(today);
      const relevant = doc.data.filter((p) => p.status !== 'priced' || state.ipos.includes(ipoKey(p)) || (p.trade_date && daysOld(p.trade_date) <= 45) || (p.steps || []).some((s) => s.key === 'lockup' && s.date >= t && daysOld(s.date) >= -30));
      relevant.sort((a, b) => (state.ipos.includes(ipoKey(b)) - state.ipos.includes(ipoKey(a))) || ((b.trade_date || b.filed_date || '') > (a.trade_date || a.filed_date || '') ? 1 : -1));
      relevant.slice(0, 15).forEach((p) => {
        const w = state.ipos.includes(ipoKey(p)); const box = document.createElement('article'); box.className = 'ipo' + (w ? ' watched' : '');
        let nextMarked = false;
        const steps = (p.steps || []).map((s) => { const done = s.date <= t; let cls = done ? 'done' : ''; if (!done && !nextMarked) { cls = 'next'; nextMarked = true; }
          return `<div class="step ${cls}"><b>${esc(s.label)}</b><span class="${s.est ? 'est' : ''}">${esc(fmtDate(s.date))}${s.est ? ' est.' : ''}</span></div>`; }).join('');
        const meta = [p.exchange, p.price ? '$' + p.price : null, p.deal_size, p.status].filter(Boolean).map(esc).join(' · ');
        box.innerHTML = `<div class="ipo-head">${p.ticker ? `<span class="tk">${esc(p.ticker)}</span>` : ''}<strong>${esc(p.name)}</strong><span class="meta">${meta}</span><button class="star" data-ipo="${esc(ipoKey(p))}" aria-pressed="${w}">${w ? '★ Watching' : '☆ Watch'}</button></div>${steps ? `<div class="timeline">${steps}</div>` : ''}`;
        el.append(box);
      });
    }
    const f = D.filings;
    if (f && f.data.length) $('filings').innerHTML = `<thead><tr><th>Company</th><th>Form</th><th>Filed</th></tr></thead><tbody>${f.data.slice(0, 25).map((x) => `<tr><td><a href="${esc(x.url)}" rel="noopener">${esc(x.company)}</a></td><td>${esc(x.form)}</td><td>${esc(fmtDate(x.filed))}</td></tr>`).join('')}</tbody>`;
    else $('filings-box').hidden = true;
  }
  $('ipos').addEventListener('click', (e) => { const b = e.target.closest('[data-ipo]'); if (!b) return; const k = b.dataset.ipo; state.ipos = state.ipos.includes(k) ? state.ipos.filter((x) => x !== k) : state.ipos.concat(k); save(); renderIpos(); renderAlerts(); renderCode(); });

  // ---------- indicators ----------
  function fmtInd(v, unit) {
    if (unit === '%') return v.toFixed(1) + '%';
    if (unit === 'pp') return (v > 0 ? '+' : '') + v.toFixed(2);
    if (unit === 'k') return (v > 0 && v < 5000 ? '' : '') + Math.round(v).toLocaleString() + 'k';
    return v.toFixed(1);
  }
  function renderInds() {
    const doc = D.indicators; const el = $('inds'); el.textContent = '';
    $('src-ind').innerHTML = srcLabel(doc);
    staleNote($('stale-ind'), doc, 45);
    const cat = doc ? doc.data : {};
    const chosen = state.inds || Object.keys(cat).filter((k) => cat[k].default);
    chosen.filter((k) => cat[k]).forEach((k) => {
      const c = cat[k]; const d = document.createElement('div'); d.className = 'ind';
      const vals = c.history.map((h) => h[1]);
      d.innerHTML = `<span class="label">${esc(c.label)}</span><span class="val">${esc(fmtInd(c.latest, c.unit))}</span><span class="prev">prev ${esc(fmtInd(c.previous, c.unit))} · ${esc(fmtDate(c.latest_date))}</span>${spark(vals)}`;
      el.append(d);
    });
    if (!el.children.length) el.innerHTML = doc ? '<div class="empty">No indicators chosen. Open "Choose indicators" below.</div>' : '<div class="empty">Indicator data unavailable right now.</div>';
    const cl = $('catlist'); cl.textContent = '';
    Object.keys(cat).forEach((k) => {
      const l = document.createElement('label'); const cb = document.createElement('input'); cb.type = 'checkbox'; cb.id = 'ind-' + k; cb.checked = chosen.includes(k);
      cb.addEventListener('change', () => { const cur = state.inds || chosen.slice(); state.inds = cb.checked ? cur.concat(k) : cur.filter((x) => x !== k); save(); renderInds(); renderCode(); });
      l.append(cb, document.createTextNode(cat[k].label)); cl.append(l);
    });
  }

  // ---------- calendar ----------
  function renderCal() {
    const doc = D.calendar; const el = $('cal');
    const t = iso(today), end = iso(new Date(today.getTime() + 30 * DAY));
    const ev = doc ? doc.data.filter((e) => e.date >= t && e.date <= end) : [];
    el.innerHTML = ev.length ? ev.map((e) => `<div class="cal-row"><span class="cal-date">${esc(fmtDay(e.date))}</span><span>${esc(e.title)}</span><span class="kind ${esc(e.kind)}">${e.kind === 'fed' ? 'Fed' : e.kind === 'ipo' ? 'IPO' : e.kind === 'auction' ? 'Auction' : 'Data'}</span></div>`).join('')
      : '<div class="empty">No key dates found for the next 30 days.</div>';
    if (doc && doc.notes) $('src-cal').textContent = 'Fed dates: ' + doc.notes.fomc + ' · Release dates: ' + doc.notes.bls;
  }

  // ---------- move the list between devices ----------
  function renderCode() {
    const payload = { w: state.watch.map((w) => [w.tk, w.alert]), i: state.ipos, n: state.inds };
    $('code').value = 'TR1.' + btoa(unescape(encodeURIComponent(JSON.stringify(payload))));
  }
  $('copy').addEventListener('click', () => {
    const v = $('code').value;
    const done = () => { $('move-msg').textContent = 'Copied. Paste it on your other device.'; };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(v).then(done, () => { $('code').select(); $('move-msg').textContent = 'Select and copy the code above.'; });
    else { $('code').select(); $('move-msg').textContent = 'Select and copy the code above.'; }
  });
  $('import').addEventListener('click', () => {
    const raw = $('paste').value.trim();
    try {
      if (!raw.startsWith('TR1.')) throw new Error('prefix');
      const p = JSON.parse(decodeURIComponent(escape(atob(raw.slice(4)))));
      if (!Array.isArray(p.w)) throw new Error('shape');
      state.watch = p.w.filter((x) => typeof x[0] === 'string').map((x) => ({ tk: x[0].toUpperCase().slice(0, 12), alert: [2, 3, 5, 8, 10].includes(x[1]) ? x[1] : 5 }));
      state.ipos = Array.isArray(p.i) ? p.i.map(String) : [];
      state.inds = Array.isArray(p.n) ? p.n.map(String) : null;
      save(); $('paste').value = ''; $('move-msg').textContent = `Loaded ${state.watch.length} tickers.`;
      renderCards(); renderAlerts(); renderIpos(); renderInds(); renderCode();
    } catch (e) { $('move-msg').textContent = 'That code was not recognised. Copy it again from the other device; it starts with TR1.'; }
  });

  // ---------- boot ----------
  async function boot() {
    const names = ['bills', 'curve', 'auctions', 'indicators', 'ipos', 'filings', 'calendar', 'health', 'glance'];
    const docs = await Promise.all(names.map((n) => getJSON('data/' + n + '.json')));
    names.forEach((n, i) => { D[n] = docs[i]; });
    ensureIndex().then(renderCards);
    allPrices().then((doc) => { $('src-prices').innerHTML = 'Saved in this browser only · ' + srcLabel(doc); });
    const health = D.health;
    if (health) {
      const s = health.sources || {}; const bad = Object.keys(s).filter((k) => s[k] && s[k].ok === false);
      $('asof').textContent = 'Updated ' + new Date(health.built_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      $('health').textContent = bad.length ? 'Sources down on the last refresh: ' + bad.join(', ') : 'All sources refreshed normally';
      if (daysOld(health.built_at.slice(0, 10)) > 3) { $('banner').hidden = false; $('banner').innerHTML = '<b>Stale.</b><span>This page has not refreshed for a few days. The numbers below may be out of date.</span>'; }
    } else {
      $('asof').textContent = 'No data yet';
      $('banner').hidden = false; $('banner').innerHTML = '<b>No data yet.</b><span>The data refresh has not run. Numbers will appear after the next scheduled refresh.</span>';
    }
    renderGlance(); renderCards(); renderRates(); renderIpos(); renderInds(); renderCal(); renderCode();
    await renderAlerts();
    state.lastVisit = iso(today); save();
    document.body.dataset.ready = '1';
  }
  boot();
})();
