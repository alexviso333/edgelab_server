/* edgelab — interfaz */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const view = $('#view');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const F = {
  pct: (v, d = 1) => v == null || isNaN(v) ? '–' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(d)} %`,
  p0: v => v == null || isNaN(v) ? '–' : `${Math.round(v * 100)} %`,
  eur: v => v == null || isNaN(v) ? '–' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)} €`,
  n: (v, d = 2) => v == null || isNaN(v) ? '–' : Number(v).toFixed(d),
  o: v => v == null || isNaN(v) || v > 999 ? '–' : Number(v).toFixed(2),
  cls: v => v == null || isNaN(v) ? '' : v > 0 ? 'win' : v < 0 ? 'loss' : '',
  day: t => new Date(t * 1000).toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' }),
  dshort: t => new Date(t * 1000).toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: '2-digit' }),
  hm: t => new Date(t * 1000).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' }),
  ago: t => { const s = Date.now() / 1000 - t; return s < 90 ? 'ahora' : s < 3600 ? `hace ${Math.round(s / 60)} min` : s < 86400 ? `hace ${Math.round(s / 3600)} h` : `hace ${Math.round(s / 86400)} días`; },
};
const COMP = { EPL: 'Premier League', LALIGA: 'LaLiga', SERIEA: 'Serie A', BUNDES: 'Bundesliga', LIGUE1: 'Ligue 1', UCL: 'Champions', UEL: 'Europa League', UECL: 'Conference', CDR: 'Copa del Rey' };
const STATUS = { live: 'Dinero real', paper: 'En papel', validated: 'Validada', candidate: 'Candidata', rejected: 'Descartada', retired: 'Retirada', watch: 'Vigilada' };
const MK = { '1X2': 'Resultado', AH: 'Hándicap asiático', OU: 'Más/menos goles', BTTS: 'Ambos marcan', DC: 'Doble oportunidad', DNB: 'Empate no válido', TEAM_OU: 'Goles de un equipo', CORNERS_OU: 'Córners', CARDS_OU: 'Tarjetas', CS: 'Resultado exacto', SHOTS_OU: 'Tiros', SOT_OU: 'Tiros a puerta' };
const TIPS = {
  clv: 'CLV: cuánto mejor fue tu cuota que la de cierre de Pinnacle (la casa más afinada). Si es positivo de forma sostenida, estás ganando al mercado aunque la suerte tarde en notarse.',
  shrunk: 'Yield contraído: el yield corregido por el tamaño de la muestra. 30 apuestas con +40 % pesan menos que 800 con +4 %. Es la cifra en la que fiarse.',
  padj: 'p ajustado: probabilidad de que el resultado sea suerte, corregida porque se prueban miles de variantes. Por debajo de 0,10 se considera señal.',
  oos: 'Fuera de muestra: temporadas que el buscador NO vio al diseñar la estrategia. Es la prueba de verdad.',
  minodds: 'Cuota mínima: por debajo de esta cuota la apuesta no tiene ventaja. Si bet365 paga menos, no se entra.',
  fair: 'Cuota justa: la que corresponde a la probabilidad del modelo, sin margen de la casa.',
  impact: 'Impacto del jugador: (xG + xA)/90, acciones defensivas/90 y nota media de sus últimos 10 partidos.',
  prob: 'Probabilidad real estimada: mezcla del modelo (goles esperados, córners, tarjetas) y del consenso de todas las casas sin su margen. Pinnacle y Betfair pesan el doble porque son las más afinadas.',
  ev: 'Valor esperado a la cuota de bet365: lo que ganas de media por cada euro apostado si la probabilidad es correcta. +5 % = 5 céntimos por euro a la larga.',
  stake: 'Stake recomendado: un cuarto de Kelly sobre tu banca, con tope del 3 %. Sube cuanto más valor y más probabilidad tiene la apuesta.',
  watch: 'Las estrategias vigiladas se siguen en silencio en todos los partidos para acumular historial real, sin avisarte.',
};
let CUR_MID = null;
let timers = [], meta = {}, navSeq = 0, USER = { bankroll: 1000, risk: 1, min_conf: 'media', show_players: true };
const stakeOf = v => { const k = v.kf != null ? v.kf : (v.stake || 0) / 1000; const st = k * USER.bankroll * USER.risk; return st >= 1 ? Math.round(st * 2) / 2 : 0; };
const okConf = v => USER.min_conf === 'alta' ? v.conf === 'alta' : v.conf === 'alta' || v.conf === 'media';
const visibleValues = vs => vs.filter(v => okConf(v) && (USER.show_players || v.group !== 'jugadores'));
// portada: como mucho 3 de jugador (sin límite por partido) y primero las de confianza alta
// misma regla que el bot (plans.official_picks): como mucho 3 por partido, nada repetido de la misma familia (ni dos
// del mismo jugador), 3 de jugador al día y, por día, hasta que lo recomendado sume el 15 % de la banca (máx. 10)
function famOf(v) {
  if (v.group === 'jugadores' || v.player) return 'pl:' + String(v.player || v.label || '').toLowerCase();
  const s = String(v.selection || ''); const d = s.startsWith('O') ? 'O' : s.startsWith('U') ? 'U' : s;
  return `${v.market}|${v.period || 'FT'}|${d}`;
}
function topPicks(vs, n) {
  const out = [], perMatch = {}, fams = new Set(), perDay = {};
  const tone = v => (v.dossier || {}).tone;
  const ok = vs.filter(v => tone(v) !== 'bad');
  const rank = v => (tone(v) === 'good' ? 0 : tone(v) === 'warn' ? 2 : 1) + (v.conf === 'alta' ? 0 : 0.5);
  const ordered = [...ok].sort((a, b) => rank(a) - rank(b));
  const bank = (USER.bankroll || 0) * (USER.risk || 1);
  for (const v of ordered) {
    const mid = v.match.id, fam = mid + ':' + famOf(v);
    if ((perMatch[mid] || 0) >= 3 || fams.has(fam)) continue;
    const day = new Date(v.match.kickoff * 1000).toDateString();
    const d = perDay[day] || (perDay[day] = { n: 0, pl: 0, eur: 0 });
    const st = stakeOf(v) || 1;
    if (d.n >= 10 || (v.group === 'jugadores' && d.pl >= 3) || (bank > 0 && d.n && d.eur + st > 0.15 * bank)) continue;
    out.push(v); fams.add(fam); perMatch[mid] = (perMatch[mid] || 0) + 1;
    d.n++; if (v.group === 'jugadores') d.pl++; d.eur += st;
    if (out.length >= n) break;
  }
  return out;
}
const ICON = {
  hoy: '<path d="M3 11l9-7 9 7v9a1 1 0 01-1 1h-5v-6H9v6H4a1 1 0 01-1-1z"/>',
  directo: '<circle cx="12" cy="12" r="3"/><path d="M5.6 5.6a9 9 0 000 12.8M18.4 5.6a9 9 0 010 12.8M8.5 8.5a5 5 0 000 7M15.5 8.5a5 5 0 010 7"/>',
  partidos: '<rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  calculadora: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 7h8M8 12h2M14 12h2M8 16h2M14 16h2"/>',
  estrategias: '<path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/>',
  laboratorio: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 002 3h10a2 2 0 002-3l-5-9V3"/>',
  apuestas: '<path d="M4 6h16v4a2 2 0 000 4v4H4v-4a2 2 0 000-4z"/>',
  aprendizaje: '<path d="M3 17l6-6 4 4 8-8M15 7h6v6"/>',
  guia: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 015 .5c0 1.5-2.5 2-2.5 3.5M12 17h.01"/>',
  ajustes: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.6 1.6 0 00-2.7 1.1V21a2 2 0 11-4 0v-.1a1.6 1.6 0 00-2.6-1.1l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.6 1.6 0 00-1.1-2.7H3a2 2 0 110-4h.1a1.6 1.6 0 001.1-2.6l-.1-.1a2 2 0 112.8-2.8l.1.1a1.6 1.6 0 002.7-1.1V3a2 2 0 114 0v.1a1.6 1.6 0 002.6 1.1l.1-.1a2 2 0 112.8 2.8l-.1.1a1.6 1.6 0 001.1 2.7H21a2 2 0 110 4h-.1a1.6 1.6 0 00-1.5 1z"/>',
  historial: '<path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/>',
  oportunidades: '<path d="M12 3l2.6 5.6 6.1.7-4.5 4.2 1.2 6L12 16.6 6.6 19.5l1.2-6L3.3 9.3l6.1-.7z"/>',
};
const NAV = [['', 'Hoy', 'hoy'], ['oportunidades', 'Oportunidades', 'oportunidades'], ['partidos', 'Partidos', 'partidos'], ['directo', 'Directo', 'directo'], ['historial', 'Historial', 'historial'], ['-'],
  ['calculadora', 'Calculadora', 'calculadora'], ['apuestas', 'Mis apuestas', 'apuestas'], ['-'],
  ['estrategias', 'Estrategias', 'estrategias'], ['laboratorio', 'Laboratorio', 'laboratorio'], ['aprendizaje', 'Aprendizaje', 'aprendizaje'], ['guia', 'Cómo funciona', 'guia'], ['ajustes', 'Ajustes', 'ajustes']];
$('#nav').innerHTML = NAV.map(([r, t, i]) => r === '-' ? '<div class="sep"></div>' : `<a href="#/${r}" data-r="${r}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON[i]}</svg>${t}${r === 'directo' ? '<span class="live-dot hidden" id="liveDot"></span>' : ''}</a>`).join('');

// caché en el navegador: volver a una página ya vista es instantáneo (y se actualiza por detrás si tiene más de 60 s)
const _AC = new Map();
const _AC_SKIP = /^\/api\/(live|status|health|login|jobs|debug|alerts)/;
async function api(path, opts = {}) {
  const seq = navSeq;
  const isGet = !opts.method || opts.method === 'GET';
  if (!isGet) _AC.clear();
  if (isGet && !_AC_SKIP.test(path)) {
    const c = _AC.get(path);
    if (c && Date.now() - c.t < 600000) {
      if (Date.now() - c.t > 60000 && !c.busy) { c.busy = true; _apiRaw(path, opts).then(j => _AC.set(path, { t: Date.now(), j })).catch(() => { c.busy = false; }); }
      return structuredClone(c.j);
    }
    const j = await _apiRaw(path, opts);
    _AC.set(path, { t: Date.now(), j });
    if (seq !== navSeq && !opts.keep) throw new Error('stale');
    return structuredClone(j);
  }
  const j = await _apiRaw(path, opts);
  if (seq !== navSeq && !opts.keep) throw new Error('stale');
  return j;
}
async function _apiRaw(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', ...opts, body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body });
  if (r.status === 401) { $('#login').classList.remove('hidden'); throw new Error('login'); }
  if (!r.ok) { let t = await r.text(); try { t = JSON.parse(t).detail || t; } catch (e) {} throw new Error(t || r.statusText); }
  return r.json();
}
function toast(m) { const t = $('#toast'); t.textContent = m; t.classList.remove('hidden'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add('hidden'), 3200); }
$('#loginForm').addEventListener('submit', async e => { e.preventDefault(); try { await api('/api/login', { method: 'POST', body: { password: $('#pw').value } }); $('#login').classList.add('hidden'); route(); } catch (err) { $('#loginErr').textContent = 'Contraseña incorrecta'; } });
const help = k => `<span class="help" data-tip="${esc(TIPS[k])}">?</span>`;
document.addEventListener('mouseover', e => { const el = e.target.closest('[data-tip]'); const tip = $('#tip'); if (!el) { if (!e.target.closest('svg')) tip.classList.add('hidden'); return; } const r = el.getBoundingClientRect(); tip.innerHTML = el.dataset.tip; tip.classList.remove('hidden'); tip.style.left = Math.max(8, Math.min(r.left, innerWidth - 320)) + 'px'; tip.style.top = (r.bottom + 8) + 'px'; });
const pill = st => `<span class="pill st-${st}"><i></i>${STATUS[st] || st}</span>`;
const formDots = s => `<span class="form">${(s || '').split('').map(c => `<i class="${c}">${{ W: 'V', D: 'E', L: 'D' }[c]}</i>`).join('')}</span>`;
const ph = (title, sub, right = '') => `<div class="ph"><div><h1>${title}</h1>${sub ? `<p>${sub}</p>` : ''}</div><div class="sp"></div>${right}</div>`;
const empty = (t, s, cta = '') => `<div class="empty"><b>${t}</b>${s}${cta ? `<div style="margin-top:14px">${cta}</div>` : ''}</div>`;
const tc = (cols, side) => { const a = teamColor(cols && cols[0], null), b = teamColor(cols && cols[1], null); return a || b || (side === 'H' ? '#2b3d5c' : '#c0562f'); };
const chip = (k, v) => `<span class="chip"><span>${k}</span><b class="num">${v}</b></span>`;
function segWire(id, fn) { $('#' + id).onclick = e => { const b = e.target.closest('button'); if (!b) return; $$(`#${id} button`).forEach(x => x.classList.toggle('on', x === b)); fn(b); }; }

const routes = { '': hoy, apuesta, oportunidades, ajustes, historial, directo, partidos, partido, calculadora, estrategias, estrategia, laboratorio, apuestas, aprendizaje, guia };
async function route() {
  navSeq++;
  timers.forEach(clearInterval); timers = []; $('#tip').classList.add('hidden'); CUR_MID = null;
  const [r, arg] = location.hash.replace(/^#\/?/, '').split('?')[0].split('/');
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.r === ({ partido: 'partidos', estrategia: 'estrategias' }[r] ?? r)));
  view.innerHTML = '<div class="skeleton"></div>';
  window.scrollTo(0, 0);
  try { await (routes[r] || hoy)(arg); } catch (e) { if (e.message !== 'login' && e.message !== 'stale') view.innerHTML = `<div class="note">No se pudo cargar: ${esc(e.message)}</div>`; }
}
addEventListener('hashchange', route);

/* ============================================================ HOY */
async function hoy() {
  const [d, ms, stt, opp, trk] = await Promise.all([api('/api/dashboard'), api('/api/matches?days=2'), api('/api/status').catch(() => null), api('/api/opportunities?hours=36').catch(() => null), api('/api/track?days=90').catch(() => null)]);
  const p = d.paper || {}, r = d.real || {}, st = d.strategies || {};
  const next = ms.filter(m => m.status !== 'finished').slice(0, 8);
  const picks = ms.flatMap(m => m.picks.map(k => ({ ...k, m })));
  const title = new Date().toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' });
  view.innerHTML = ph(title.charAt(0).toUpperCase() + title.slice(1), 'Qué apostar ahora, en qué partidos y cuánto. Abajo, cómo vas y el estado de los datos.', `<a class="btn" href="#/partidos">Ver todos los partidos</a>`) + `
  <div class="cols c-main section">
    <div>
      <div class="row" style="margin-bottom:8px"><h2 style="margin:0">Qué apostar ahora</h2><span class="sp"></span><a class="btn sm" href="#/oportunidades">Ver todas (${opp ? opp.values.length : 0})</a></div>
      <p class="lead" style="margin:0 0 12px">Las apuestas de las próximas 36 horas en las que bet365 paga más de lo que debería, ordenadas por lo que ganas de media. Revisadas ${opp ? (opp.n_rows || 0).toLocaleString('es-ES') : 0} cuotas de ${opp ? opp.n_matches : 0} partidos${opp && opp.pending ? ` (calculando ${opp.pending} más en segundo plano: recarga en unos minutos)` : ''}.</p>
      ${trk && trk.all ? `<a class="trk" href="#/historial"><b>Historial de recomendaciones</b><span>${trk.all.n} liquidadas · ${F.eur(trk.all.profit)} con 10 € cada una · yield ${F.pct(trk.all.yield)} · ${trk.open} pendientes</span><span class="sp"></span><span class="faint">Ver →</span></a>` : ''}
      ${howTo()}
      ${opp && visibleValues(opp.values).length ? `<div id="recs">${topPicks(visibleValues(opp.values), 8).map(recCard).join('')}</div>`
        : opp && opp.pending && !opp.n_matches ? `<div class="panel">${empty(`Analizando ${opp.pending} partidos…`, 'El planificador está revisando todas las cuotas de bet365. Las recomendaciones aparecen aquí en unos minutos; la página se refresca sola.')}</div>`
        : `<div class="panel">${empty('Nada con valor claro en las próximas 36 horas', 'Las cuotas se actualizan solas; en Oportunidades puedes ampliar a 72 horas.')}</div>`}
      <h2 class="section">Picks de estrategias validadas</h2>
      ${picks.length ? picks.map(k => `<a class="fixture" href="#/partido/${k.m.id}" style="grid-template-columns:62px minmax(0,1fr) 150px">
        <div class="ko">${F.hm(k.m.kickoff)}<small>${COMP[k.m.comp] || ''}</small></div>
        <div><div class="team">${crest(k.m.home_name, k.m.colors.H, 22)}<b>${esc(k.m.home_name)}</b><span class="faint">contra</span><b>${esc(k.m.away_name)}</b></div>
        <div class="small muted" style="margin-top:4px">${esc(k.strategy)}</div></div>
        <div class="pickbox r"><b>${esc(k.selection)}</b>a ${F.o(k.odds)}, mínimo ${F.o(k.min_odds)}<br><span class="faint">${k.stake} € sugeridos</span></div></a>`).join('')
        : `<div class="panel">${empty('Sin picks de estrategias validadas', 'Los picks de esta sección solo salen de estrategias que han pasado el laboratorio con datos fuera de muestra. Mientras tanto, guíate por «Lo mejor de hoy».')}</div>`}
      <h2 class="section">Próximos partidos</h2>
      ${next.length ? '<div class="narrow">' + next.map(fixtureRow).join('') + '</div>' : `<div class="panel">${empty('Sin partidos en las próximas 48 horas', 'El calendario se actualiza solo cada 6 horas.')}</div>`}
    </div>
    <div>
      <h2>Curva de beneficio</h2>
      <div class="panel chart" id="curve"></div>
      <h2 class="section">Últimos avisos</h2>
      <div class="panel">${alerts(d.alerts)}</div>
      <h2 class="section">Estado de los datos</h2>
      ${statusPanel(stt)}
      <div class="panel" style="margin-top:12px">
        <div class="chips">${chip('partidos', d.data.matches)}${chip('con cronología', d.data.with_timeline)}${chip('cuotas guardadas', d.data.odds_rows)}${chip('apuestas abiertas', d.open_bets)}</div>
        <table style="margin-top:12px">${d.data.by_comp.map(c => `<tr><td>${COMP[c.comp] || c.comp}</td><td class="r num" style="font-size:17px">${c.n}</td><td class="r faint small">${c.first || ''} a ${c.last || ''}</td></tr>`).join('')}</table>
        ${d.lab && d.lab.trials ? `<div class="note" style="margin-bottom:0">Último laboratorio ${F.ago(d.lab.started)}: ${d.lab.trials} variantes probadas, ${d.lab.validated} validadas. <a class="val" href="#/laboratorio">Ver informe</a></div>` : ''}
      </div>
    </div>
  </div>
  <h2 class="section">Cómo vas</h2>
  <div class="board">
    <div><div class="k">Beneficio en papel</div><div class="v ${F.cls(p.profit)}">${F.eur(p.profit)}</div><div class="s">${p.n || 0} apuestas, yield ${F.pct(p.yield)}</div></div>
    <div><div class="k">Beneficio real</div><div class="v ${F.cls(r.profit)}">${F.eur(r.profit)}</div><div class="s">${r.n || 0} apuestas que marcaste como hechas</div></div>
    <div><div class="k">CLV medio ${help('clv')}</div><div class="v ${F.cls(p.clv)}">${F.pct(p.clv)}</div><div class="s">${p.clv_n || 0} apuestas comparadas con el cierre</div></div>
    <div><div class="k">Estrategias activas</div><div class="v">${(st.live || 0) + (st.paper || 0)}</div><div class="s">${st.live || 0} con dinero, ${st.paper || 0} en papel, ${st.candidate || 0} candidatas</div></div>
  </div>
`;
  if ($('#syncNow')) $('#syncNow').onclick = async () => { $('#syncNow').disabled = true; $('#syncNow').textContent = 'Sincronizando…';
    try { const r = await api('/api/jobs/sync', { method: 'POST', keep: true }); toast(r.via === 'worker' ? 'El planificador lo está trayendo; recarga en un par de minutos.' : 'Sincronizando en segundo plano; tarda unos minutos.'); } catch (e) { toast(e.message); } };
  const pts = (d.paper_curve || []).map(x => [x.t, x.pnl]), rpts = (d.real_curve || []).map(x => [x.t, x.pnl]);
  lineChart($('#curve'), [{ name: 'Papel', color: C.info, points: pts, area: true }, { name: 'Real', color: C.win, points: rpts, area: true }],
    { zero: true, height: 220, xfmt: t => F.dshort(t), yfmt: v => `${v.toFixed(0)} €`, empty: '<br>Aparecerá en cuanto se liquiden las primeras apuestas.' });
  fillTeasers();
  if (opp && opp.pending) timers.push(setInterval(() => { if (!location.hash || location.hash.startsWith('#/hoy') || location.hash === '#/') route(); }, 60000));
}
function statusPanel(s) {
  if (!s) return '';
  const w = s.worker || {}, u = s.upcoming || {};
  const bar = (n, of, label) => `<div style="margin:8px 0"><div class="row small"><span>${label}</span><span class="sp"></span><span class="num">${n}/${of}</span></div>
    <div class="pbar" style="height:6px"><i style="width:${of ? n / of * 100 : 0}%;background:var(--win)"></i><i style="width:${of ? 100 - n / of * 100 : 100}%;background:var(--line)"></i></div></div>`;
  const doing = w.doing && w.doing.job ? ` Ahora: ${esc({ sync: 'sincronizando', odds: 'cuotas', odds_sofa: 'cuotas Sofascore', lineups: 'alineaciones', results: 'resultados', timelines: 'cronologías', daily: 'tarea diaria', weekly: 'laboratorio semanal', schedule: 'calendario' }[w.doing.job] || w.doing.job)}.` : '';
  return `<div class="panel">
    <div class="row"><b class="${w.alive ? 'win' : 'loss'}">${w.alive ? 'Planificador en marcha' : 'Planificador parado'}</b><span class="sp"></span><button class="btn sm" id="syncNow">Sincronizar ahora</button></div>
    <div class="small muted" style="margin:4px 0 6px">${w.alive ? `Última sincronización ${w.last_sync ? F.ago(w.last_sync) : 'en curso'}.${doing}` : 'Sin él no llegan cuotas, alineaciones ni el directo. Cierra la ventana negra y vuelve a abrir <b>abrir_en_windows.bat</b>: ahora lo arranca solo.'}</div>
    ${bar(u.with_odds || 0, u.n || 0, 'Próximas 72 h con cuotas')}${bar(u.with_b365 || 0, u.n || 0, 'con bet365')}${bar(u.with_lineups || 0, u.n || 0, 'con once previsto')}${bar(u.with_pred || 0, u.n || 0, 'con predicción')}
    <div class="small faint" style="margin-top:6px">${s.timelines} partidos con cronología de ${s.finished} jugados${s.live_now ? `, ${s.live_now} en juego ahora` : ''}.</div>
    ${(s.errors || []).length ? `<details style="margin-top:8px"><summary class="small loss">${s.errors.length} errores recientes</summary>${s.errors.map(e => `<div class="small faint" style="margin-top:4px">${F.ago(e.ts)} · ${esc(e.job)}: ${esc(e.msg)}</div>`).join('')}</details>` : ''}
  </div>`;
}
function alerts(a) {
  if (!a || !a.length) return empty('Sin avisos', 'Aquí verás los picks, las señales en directo y las coberturas.');
  return a.map(x => `<div style="padding:10px 0;border-bottom:1px solid var(--line)"><div class="row"><b>${esc(x.title)}</b><span class="sp"></span><span class="faint small">${F.ago(x.ts)}</span></div><div class="small muted" style="white-space:pre-line">${esc(x.message)}</div></div>`).join('');
}
function fixtureRow(m) {
  const p = m.model || {}, o = m.b365 || {};
  const edge = ['H', 'D', 'A'].map(s => p['p' + s] && o[s] ? p['p' + s] * o[s] - 1 : null);
  const ch = tc(m.colors && m.colors.H, 'H'), ca = tc(m.colors && m.colors.A, 'A');
  return `<a class="fixture" href="#/partido/${m.id}">
    <div class="ko">${m.status === 'inprogress' ? '<span class="live-tag">EN JUEGO</span>' : m.status === 'finished' ? `${m.hg}-${m.ag}` : F.hm(m.kickoff)}<small>${COMP[m.comp] || m.comp}</small></div>
    <div class="teams">
      <div class="team"><div>${crest(m.home_name, m.colors && m.colors.H, 22)}<b>${esc(m.home_name)}</b></div>${formDots(m.form && m.form.H)}</div>
      <div class="team"><div>${crest(m.away_name, m.colors && m.colors.A, 22)}<b>${esc(m.away_name)}</b></div>${formDots(m.form && m.form.A)}</div>
    </div>
    <div class="probcol">${p.pH ? `<div class="pbar"><i style="width:${p.pH * 100}%;background:${ch}"></i><i style="width:${p.pD * 100}%;background:#4b6159"></i><i style="width:${p.pA * 100}%;background:${ca}"></i></div>
      <div class="pbar-l"><span>${F.p0(p.pH)}</span><span>${F.p0(p.pD)}</span><span>${F.p0(p.pA)}</span></div>` : '<span class="faint small">Sin modelo aún</span>'}</div>
    <div class="oddscol">${m.odds_book && m.odds_book !== 'Bet365' ? `<div class="faint small" style="text-align:center;margin-bottom:2px">${m.odds_book === 'Sofascore' ? 'cuota Sofascore' : 'Pinnacle'}</div>` : ''}<div class="odds3">${['H', 'D', 'A'].map((s, i) => `<div class="${edge[i] > 0.02 ? 'edge' : ''}" ${edge[i] != null ? `data-tip="Valor según el modelo: ${F.pct(edge[i])}"` : ''}><small>${'1X2'[i]}</small><span class="num">${F.o(o[s])}</span></div>`).join('')}</div></div>
    <div class="pickbox">${m.picks.length ? m.picks.map(k => `<b>${esc(k.selection)}</b><span class="faint">mín. ${F.o(k.min_odds)}, ${k.stake} €</span>`).join('') : m.n_value ? `<b>${m.n_value} apuestas con valor</b><span class="faint">mejor: ${esc(m.best_value || '')}</span>` : '<span class="faint">Ver todas las apuestas</span>'}</div>
  </a>`;
}

/* ---------- tarjeta de recomendación: qué apostar, dónde, cuánto y por qué ---------- */
const GROUP_NAME = { resultado: 'Resultado', handicap: 'Hándicap', goles: 'Goles', exacto: 'Resultado exacto', mitades: 'Por mitades', corners: 'Córners', tarjetas: 'Tarjetas', tiros: 'Tiros', estadisticas: 'Estadísticas', jugadores: 'Jugador', especiales: 'Especial' };
const REC = [];
function recCard(v) {
  const i = REC.push(v) - 1;
  const minO = v.fair ? v.fair * 1.02 : null;
  const gain10 = v.ev * 10;
  const src = v.p_market != null && v.p_model != null ? `el modelo dice ${F.p0(v.p_model)} y las demás casas, sin su margen, ${F.p0(v.p_market)}`
    : v.p_market != null ? `las demás casas, quitándoles su margen, dan ${F.p0(v.p_market)}` : `sale del modelo (no hay otras casas con las que comparar)`;
  return `<div class="rec ${v.conf === 'alta' ? 'hi' : ''}">
    <div class="rh"><a href="#/partido/${v.match.id}?t=apuestas"><b>${esc(v.match.home_name)} – ${esc(v.match.away_name)}</b></a><span class="faint">${F.day(v.match.kickoff).split(' ')[0]} ${F.hm(v.match.kickoff)} · ${COMP[v.match.comp] || v.match.comp}</span><span class="sp"></span><span class="tag">${GROUP_NAME[v.group] || v.group}</span></div>
    <div class="what">${esc(v.label)}</div>
    <div class="how">
      <div><small>Cuota en bet365</small><span class="num">${F.o(v.b365)}</span><em>entra si es ≥ ${F.o(minO)}</em></div>
      <div><small>Apuesta</small><span class="num">${stakeOf(v) ? stakeOf(v) + ' €' : 'mínima'}</span><em>${(stakeOf(v) / USER.bankroll * 100).toFixed(1).replace('.', ',')} % de tu banca</em></div>
      <div><small>Pasa</small><span class="num">${F.p0(v.p)}</span><em>de las veces</em></div>
      <div><small>Ganas de media</small><span class="num val">${F.eur(gain10)}</span><em>por cada 10 €</em></div>
    </div>
    <div class="teaser" data-mid="${v.match.id}" data-q="${esc(new URLSearchParams({ market: v.market, period: v.period || 'FT', selection: v.selection, ...(v.line != null ? { line: v.line } : {}) }).toString())}"></div>
    <div class="why">Por qué: ${src}. bet365 paga ${F.o(v.b365)}, como si pasara el ${F.p0(1 / v.b365)}: te paga de más. ${v.explain ? esc(v.explain) + '. ' : ''}${confBadge(v.conf)}</div>
    <div class="row" style="margin-top:10px"><a class="btn sm go" href="${betHref(v.match.id, v)}">Ver el análisis</a><button class="btn sm" onclick="madeBet(${i})">La he hecho</button><a class="btn sm ghost" href="#/partido/${v.match.id}?t=apuestas">Partido</a></div>
  </div>`;
}
window.madeBet = async i => {
  const v = REC[i]; if (!v) return;
  const o = prompt(`¿A qué cuota te la ha dado bet365?\n${v.label}`, v.b365); if (!o) return;
  const st = prompt('¿Cuánto has apostado (€)?', stakeOf(v) || 10); if (!st) return;
  await api('/api/bets/manual', { method: 'POST', keep: true, body: { match_id: v.match.id, market: v.market, selection: v.selection, line: v.line, period: v.period, odds: parseFloat(String(o).replace(',', '.')), stake: parseFloat(String(st).replace(',', '.')), label: v.label, fair_prob: v.p } });
  toast('Guardada en «Mis apuestas». Se liquida sola si es un mercado de goles; si no, márcala tú allí.');
};
const howTo = () => `<details class="howto"><summary>Cómo leer esto (30 segundos)</summary><div>
  <p><b>Pasa el X %</b>: la probabilidad real de que salga, calculada con el modelo y con lo que dicen todas las casas quitándoles su margen.</p>
  <p><b>Cuota justa</b>: la cuota que le tocaría sin margen (1 ÷ probabilidad). Si bet365 paga <b>más</b> que la justa, la apuesta tiene valor.</p>
  <p><b>Ganas de media</b>: lo que ganarías a la larga por cada 10 € si repites esa apuesta muchas veces. Una sola puede perder: lo que cuenta es hacer muchas así.</p>
  <p><b>Fiabilidad</b>: alta si muchas casas y el modelo coinciden; media si son pocas casas o un mercado secundario; baja si solo sale del modelo. Las de fiabilidad baja no se recomiendan.</p>
  <p><b>Entra si es ≥</b>: si cuando vayas a apostar la cuota ha bajado por debajo de esa cifra, ya no merece la pena.</p></div></details>`;

/* ============================================================ OPORTUNIDADES */
async function oportunidades() {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  let hours = +(q.get('h') || 72);
  const o = await api('/api/opportunities?hours=' + hours);
  const groups = { '': 'Todos los mercados', resultado: 'Resultado', handicap: 'Hándicap', goles: 'Goles', exacto: 'Exacto', mitades: 'Mitades', corners: 'Córners', tarjetas: 'Tarjetas', tiros: 'Tiros', estadisticas: 'Estadísticas', jugadores: 'Jugadores' };
  let g = '', conf = '';
  view.innerHTML = ph('Oportunidades', `${(o.n_rows || 0).toLocaleString('es-ES')} cuotas revisadas de todos los mercados de bet365, también los de jugador. Lo mejor de los ${o.n_matches} partidos con cuotas de las próximas ${hours} horas: cada apuesta suelta con valor en bet365, los planes de varias apuestas que salen rentables en la simulación, las surebets entre casas y una combinada con las mejores.`,
    `<div class="seg" id="hz">${[24, 48, 72].map(h => `<button data-h="${h}" class="${h === hours ? 'on' : ''}">${h} h</button>`).join('')}</div>`) +
    (o.accumulator ? `<h2>Combinada del día</h2><p class="lead">Las mejores apuestas con valor de partidos distintos, en mercados principales y cuotas entre 1,45 y 3,60. Al multiplicar, el valor se multiplica… y el riesgo también.</p>
      <div class="acc">${o.accumulator.legs.map(l => `<a class="leg" href="#/partido/${l.match.id}?t=apuestas"><small>${esc(l.match.home_name)} – ${esc(l.match.away_name)} · ${F.hm(l.match.kickoff)}</small><b>${esc(l.label)}</b><div class="row small" style="margin-top:4px"><span class="num" style="font-size:18px">${F.o(l.b365)}</span><span class="val">${F.pct(l.ev)}</span></div></a>`).join('')}
        <div class="tot"><small class="faint">Cuota total</small><span class="num">${F.o(o.accumulator.odds)}</span><small class="muted">Salen las ${o.accumulator.legs.length} el ${F.p0(o.accumulator.prob)} de las veces · valor ${F.pct(o.accumulator.ev)}</small></div></div>` : '') +
    `<div class="section"><div class="row" style="margin-bottom:12px"><h2 style="margin:0">Apuestas sueltas con valor</h2><span class="sp"></span>
      <div class="seg" id="og">${Object.entries(groups).map(([k, t]) => `<button data-g="${k}" class="${k ? '' : 'on'}">${t}</button>`).join('')}</div>
      <div class="seg" id="oc"><button class="on" data-c="">Cualquier fiabilidad</button><button data-c="alta">Solo alta</button></div>
      <select id="ocomp" style="width:auto"><option value="">Todas las competiciones</option>${[...new Set(o.values.map(v => v.match.comp))].map(c => `<option value="${c}">${COMP[c] || c}</option>`).join('')}</select>
      <select id="oodds" style="width:auto"><option value="">Cualquier cuota</option><option value="1-2">Hasta 2,00</option><option value="2-4">2,00 a 4,00</option><option value="4-999">Más de 4,00</option></select></div><div id="olist"></div></div>
    <div class="section"><h2>Planes con valor</h2><p class="lead">Coberturas, empates, entradas en directo y dutching que, simulando cada partido 6.000 veces, ganan dinero de media a las cuotas actuales.</p>
      ${o.plans.length ? `<div class="plans">${o.plans.slice(0, 12).map(planCard).join('')}</div>` : `<div class="panel">${empty('Todavía no hay planes calculados', 'El planificador los calcula cada 15 minutos para todos los partidos con cuotas. Si acabas de arrancar, vuelve en unos minutos.')}</div>`}</div>
    ${o.surebets.length ? `<div class="section"><h2>Surebets</h2><p class="lead">Repartiendo el dinero entre varias casas ganas pase lo que pase. Duran poco: comprueba las cuotas antes.</p>${surebetList(o.surebets)}</div>` : ''}`;
  const draw = () => {
    const cp = $('#ocomp').value, od = $('#oodds').value.split('-').map(Number);
    const vs = visibleValues(o.values).filter(v => (!g || v.group === g) && (!conf || v.conf === conf) && (!cp || v.match.comp === cp)
      && (!$('#oodds').value || (v.b365 >= od[0] && v.b365 < od[1])));
    $('#olist').innerHTML = vs.length ? vs.slice(0, 6).map(recCard).join('') + (vs.length > 6 ? `<h3 style="margin-top:22px">El resto (${vs.length - 6})</h3>` : '') + `<div class="mtable"><div class="opp head"><div>Partido</div><div>Apuesta</div><div class="hide-m">Prob.</div><div class="r hide-t hide-m">Justa</div><div class="r">bet365</div><div class="r">Valor</div><div class="r hide-m">Stake</div></div>` +
      vs.slice(6, 200).map(v => `<a class="opp" href="#/partido/${v.match.id}?t=apuestas"><div class="mt"><b>${esc(v.match.home_name)} – ${esc(v.match.away_name)}</b>${F.day(v.match.kickoff).split(' ').slice(0, 2).join(' ')} ${F.hm(v.match.kickoff)} · ${COMP[v.match.comp] || v.match.comp}</div>
        <div class="lb"><b>${esc(v.label)}</b><small>${confBadge(v.conf)}${v.explain ? ' ' + esc(v.explain) : ''}</small></div>
        <div class="hide-m"><div class="pr" style="display:flex;gap:8px;align-items:center"><div style="flex:1;height:6px;background:var(--pitch);border-radius:3px;overflow:hidden"><i style="display:block;height:100%;width:${v.p * 100}%;background:var(--chalk2)"></i></div><span class="num">${F.p0(v.p)}</span></div></div>
        <div class="r num faint hide-t hide-m">${F.o(v.fair)}</div><div class="r num" style="font-size:17px">${F.o(v.b365)}</div><div class="r">${evBadge(v.ev)}</div><div class="r num hide-m">${stakeOf(v) || '–'} €</div></a>`).join('') + '</div>'
      : `<div class="panel">${empty('Nada con estos filtros', o.n_matches ? 'Ninguna cuota de bet365 supera la justa con margen en este mercado. Prueba con otro.' : (o.pending ? `Analizando ${o.pending} partidos en segundo plano; en unos minutos aparecen aquí.` : 'Aún no hay cuotas de los próximos partidos. Si acabas de arrancar, espera a que el planificador termine la primera sincronización.'))}</div>`;
  };
  segWire('og', b => { g = b.dataset.g; draw(); });
  segWire('oc', b => { conf = b.dataset.c; draw(); });
  $('#ocomp').onchange = draw; $('#oodds').onchange = draw;
  segWire('hz', b => { location.hash = '#/oportunidades?h=' + b.dataset.h; });
  draw();
}

/* ============================================================ DIRECTO */
async function directo() {
  const draw = async () => {
    const d = await api('/api/live');
    const snaps = d.snapshot || [];
    $('#liveDot') && $('#liveDot').classList.toggle('hidden', !snaps.length);
    view.innerHTML = ph('En directo', 'Partidos en juego ahora. Cada minuto proyectamos lo que va a pasar con las estadísticas en vivo. Cada aviso dice a partir de qué cuota merece la pena: si bet365 paga menos, no entres.') +
      srcLine(d) +
      (d.sofa_paused ? `<div class="note warn">Sofascore está en pausa unos ${Math.ceil(d.sofa_paused / 60)} minutos más. No pasa nada: estadísticas, xG y jugadores siguen con FotMob, y el marcador con ESPN. Las cuotas de bet365 en directo salen de Oddspedia.</div>` : '') +
      (snaps.length ? `<div class="live-grid">${snaps.map(liveCard).join('')}</div>`
        : `<div class="panel">${empty('No hay partidos en juego', 'Cuando empiece un partido de las competiciones seguidas aparecerá aquí con su marcador, las cuotas justas del modelo y las señales.')}</div>`) +
      `<h2 class="section">Avisos en directo</h2><div class="panel">${alerts(d.alerts)}</div>`;
  };
  await draw(); timers.push(setInterval(draw, 20000));
}
function srcLine(d) {
  const s = d.sources || {}; if (!s.n) return '';
  const now = Date.now() / 1000, ok = t => t && now - t < 180;
  const it = (nm, t, what) => `<span title="${what}">${ok(t) ? '🟢' : '⚪'} ${nm}${ok(t) ? ` <small class="faint">hace ${Math.max(1, Math.round(now - t))} s</small>` : ''}</span>`;
  return `<div class="faint small" style="display:flex;gap:14px;flex-wrap:wrap;margin:6px 0">Fuentes en directo: ${it('FotMob', s.fotmob, 'Estadísticas, xG de Opta, jugadores, mapa de tiros')}${it('ESPN', s.espn, 'Marcador, minuto y rojas')}${it('Oddspedia', s.oddspedia, 'Cuotas de bet365 y otras casas en directo')}${it('365Scores', s.s365, 'Tiempo neto jugado')}${d.sofa_paused ? '<span>⏸️ Sofascore (en pausa)</span>' : it('Sofascore', s.sofa, 'Respaldo: once, incidencias y cuotas si Oddspedia falla')}</div>`;
}
function liveCard(s) {
  if (!s.fair) return `<div class="lcard"><b>${esc(s.match || s.match_id)}</b><div class="muted small">${esc(s.note || '')}</div></div>`;
  const f = s.fair, o = f['1X2'] || {}, [hn, an] = (s.match || ' - ').split(' - ');
  const ah = Object.entries(f.AH || {}).filter(([k]) => /^[HA][+-]?0(\.25)?$|^[HA][+-]0\.5$/.test(k)).slice(0, 6);
  const ou = Object.entries(f.OU || {}).slice(0, 4);
  return `<div class="lcard">
    <div class="top"><span class="tag">${COMP[s.comp] || s.comp}</span><a class="tag" href="#/partido/${s.match_id}">Detalle</a><span class="clock">${Math.floor(s.minute)}'</span></div>
    <div class="sb"><div class="nm">${esc(hn)}</div><div class="sc">${s.score.replace('-', ' – ')}</div><div class="nm" style="text-align:right">${esc(an)}</div></div>
    <div class="row small muted" style="margin-bottom:10px"><span>Rojas ${s.reds.join('–')}</span><span>xG ${s.xg[0] == null ? 'no disponible' : `${F.n(s.xg[0])} – ${F.n(s.xg[1])}`}</span></div>
    <div class="small faint" style="margin-bottom:6px">Cuotas justas del modelo ${help('fair')}</div>
    <div class="chips">${[['1', o.H], ['X', o.D], ['2', o.A]].map(([k, v]) => chip(k, F.o(v))).join('')}
      ${Object.entries(f.NEXT_GOAL || {}).map(([k, v]) => chip(k === 'NONE' ? 'Sin más goles' : 'Próximo gol ' + (k === 'H' ? esc(hn) : esc(an)), F.o(v))).join('')}</div>
    <div class="chips" style="margin-top:6px">${ah.map(([k, v]) => chip(`AH ${k[0] === 'H' ? 'local' : 'visit.'} ${k.slice(1)}`, F.o(v))).join('')}${ou.map(([k, v]) => chip(`${k[0] === 'O' ? 'Más de' : 'Menos de'} ${k.slice(1)}`, F.o(v))).join('')}</div>
    ${(s.signals || []).map(sig => sig.stake_table ? `<div class="signal hedge"><div class="h">Cubrir ahora: ${esc(sig.selection)}</div>
        <div class="gate"><span class="num">${F.o(sig.fair_odds)}</span><span class="muted">cuota justa de la cobertura</span></div>
        ${sig.live_odds ? `<div class="small" style="margin-bottom:4px">Ahora se paga ${F.o(sig.live_odds)}; busca esa cuota en la tabla</div>` : ''}
        <div class="small muted">Stake de cobertura según la cuota que veas en bet365:</div>
        <div class="stake-table">${Object.entries(sig.stake_table).map(([k, v]) => `<div><small>a ${k}</small><span class="num">${v} €</span></div>`).join('')}</div></div>`
      : `<div class="signal"><div class="h">${esc(sig.selection)}<span class="sp"></span>${pill(sig.status || 'watch')}</div>
        <div class="small muted">${esc(sig.strategy)}</div>
        <div class="gate"><span class="num">${F.o(sig.min_odds)}</span><span class="muted">cuota mínima para entrar ${help('minodds')}</span></div>
        ${sig.live_odds ? `<div class="small ${sig.enter ? 'win' : 'loss'}" style="margin-bottom:4px">Ahora se paga ${F.o(sig.live_odds)}: ${sig.enter ? 'hay valor, se puede entrar' : 'por debajo del mínimo, esperar'}</div>` : ''}
        <div class="small muted">Justa ${F.o(sig.fair_odds)}, stake ${sig.stake} €. <a class="val" href="#/calculadora?m=${s.match_id}&min=${Math.floor(s.minute)}&sc=${s.score}">Preparar cobertura</a></div></div>`).join('')}
  </div>`;
}

/* ============================================================ PARTIDOS */
async function partidos() {
  const ms = await api('/api/matches?days=7&past=1');
  const comps = [...new Set(ms.map(m => m.comp))];
  let comp = '', onlyValue = false;
  view.innerHTML = ph('Partidos', 'Probabilidad del modelo frente a la cuota de bet365. Las cuotas con borde amarillo tienen valor según el modelo; los picks salen solo de estrategias validadas.') +
    `<div class="row"><div class="seg" id="cseg"><button class="on" data-c="">Todas</button>${comps.map(c => `<button data-c="${c}">${COMP[c] || c}</button>`).join('')}</div>
     <span class="sp"></span><div class="seg" id="vseg"><button class="on" data-v="0">Todos</button><button data-v="1">Con valor o pick</button></div></div><div id="mlist"></div>`;
  const draw = () => {
    const by = {};
    ms.filter(m => (!comp || m.comp === comp) && (!onlyValue || m.picks.length || ['H', 'D', 'A'].some(s => m.model && m.model['p' + s] && m.b365[s] && m.model['p' + s] * m.b365[s] > 1.02)))
      .forEach(m => { const k = F.day(m.kickoff); (by[k] = by[k] || []).push(m); });
    $('#mlist').innerHTML = Object.keys(by).length ? Object.entries(by).map(([d, l]) => `<div class="day"><h2>${d.charAt(0).toUpperCase() + d.slice(1)} <small>${l.length} partidos</small></h2>${l.map(fixtureRow).join('')}</div>`).join('')
      : `<div class="panel section">${empty('Nada con estos filtros', 'Prueba con otra competición o quita el filtro de valor.')}</div>`;
  };
  segWire('cseg', b => { comp = b.dataset.c; draw(); });
  segWire('vseg', b => { onlyValue = b.dataset.v === '1'; draw(); });
  draw();
}

/* ============================================================ PARTIDO */
async function partido(id) {
  CUR_MID = id;
  const tabQ = new URLSearchParams(location.hash.split('?')[1] || '').get('t');
  const [d, mk] = await Promise.all([api('/api/match/' + id), api(`/api/match/${id}/markets`).catch(() => null)]);
  const rep = null;   // el informe (más lento) se carga después, sin bloquear la página
  const m = d.match, cols = m.colors || { H: [], A: [] };
  const ch = tc(cols.H, 'H'), ca = tc(cols.A, 'A');
  const rows = (mk && mk.rows) || [], plans = (mk && mk.plans) || [], combos = (mk && mk.combos) || [], lam = (mk && mk.lambdas) || {};
  const r1 = s => rows.find(r => r.market === '1X2' && r.period === 'FT' && r.selection === s);
  const p = d.prediction || (r1('H') ? { pH: r1('H').p, pD: r1('D').p, pA: r1('A').p, lh: lam.lh, la: lam.la } : {});
  const lsTxt = { confirmed: 'confirmada', predicted: 'prevista' };
  const fr = d.freshness || {};
  const vals = rows.filter(isRec);
  const vplans = plans.filter(x => x.verdict === 'value');
  const TABS = [...(m.status === 'inprogress' ? [['directo', 'En directo', '']] : []), ['resumen', 'Resumen', ''], ['apuestas', 'Todas las apuestas', rows.length ? `<span class="n ${vals.length ? 'v' : ''}">${vals.length ? vals.length + ' con valor' : rows.length}</span>` : ''],
    ['planes', 'Planes y coberturas', plans.length ? `<span class="n ${vplans.length ? 'v' : ''}">${vplans.length ? vplans.length + ' con valor' : plans.length}</span>` : ''], ['combinadas', 'Combinadas', ''],
    ['analisis', 'Análisis', ''], ['cuotas', 'Cuotas', '']];
  let tab = TABS.some(t => t[0] === tabQ) ? tabQ : TABS[0][0];
  view.innerHTML = `<div class="row"><a class="faint small" href="#/partidos">← Partidos</a><span class="sp"></span>
    <span class="faint small" id="freshTxt">${freshText(fr, m)}</span><button class="btn sm" id="syncBtn">Actualizar datos</button></div>
  <div id="syncbar"></div>
  <div class="score-hero" style="margin-top:12px">
    <div class="side">${crest(m.home_name, cols.H, 60)}<div class="nm">${esc(m.home_name)}</div><span id="formH"></span></div>
    <div class="mid">${m.hg != null && m.status !== 'notstarted' ? `<div class="big">${m.hg} – ${m.ag}</div>` : `<div class="big">${F.hm(m.kickoff)}</div>`}
      <div class="meta">${COMP[m.comp] || m.comp} · ${F.day(m.kickoff)}</div>
      <div class="meta">${m.referee ? `Árbitro: ${esc(m.referee)}. ` : ''}${m.lineup_status && lsTxt[m.lineup_status] ? `Alineación ${lsTxt[m.lineup_status]}.` : ''}</div></div>
    <div class="side">${crest(m.away_name, cols.A, 60)}<div class="nm">${esc(m.away_name)}</div><span id="formA"></span></div>
    ${p.pH ? `<div class="prob-strip"><div class="pbar"><i style="width:${p.pH * 100}%;background:${ch}"></i><i style="width:${p.pD * 100}%;background:#b9c2ce"></i><i style="width:${p.pA * 100}%;background:${ca}"></i></div>
      <div class="labels"><div><span class="num">${F.p0(p.pH)}</span>gana ${esc(m.home_name)}</div><div><span class="num">${F.p0(p.pD)}</span>empate</div><div><span class="num">${F.p0(p.pA)}</span>gana ${esc(m.away_name)}</div></div>
      <div class="small faint" style="margin-top:8px;text-align:center">Goles esperados ${F.n(p.lh)} – ${F.n(p.la)}${lam.label ? ` · Fuente: ${esc(lam.label)}` : ''}</div></div>` : ''}
  </div>
  <div class="tabs" id="mtabs">${TABS.map(([k, t, n]) => `<button data-t="${k}" class="${k === tab ? 'on' : ''}">${t}${n}</button>`).join('')}</div>
  <div id="mtab"></div>`;
  const ctx = { d, rep, mk, m, ch, ca, rows, plans, combos, vals, vplans, id };
  const show = t => {
    tab = t; $$('#mtabs button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
    history.replaceState(null, '', `#/partido/${id}?t=${t}`);
    $('#mtab').innerHTML = MT[t](ctx);
    if (MT_WIRE[t]) MT_WIRE[t](ctx);
  };
  $('#mtabs').onclick = e => { const b = e.target.closest('button'); if (b) show(b.dataset.t); };
  show(tab);
  api(`/api/match/${id}/report`).then(r => {
    if (!r || !$('#mtabs')) return;
    ctx.rep = r;
    $('#formH').innerHTML = formDots((r.form.H || []).map(x => x.res).reverse().join(''));
    $('#formA').innerHTML = formDots((r.form.A || []).map(x => x.res).reverse().join(''));
    if (tab === 'analisis' || tab === 'resumen') show(tab);
  }).catch(() => {});
  const doSync = async auto => {
    const bar = $('#syncbar'), btn = $('#syncBtn'); if (!bar) return;
    btn.disabled = true; btn.textContent = 'Buscando…';
    bar.innerHTML = `<div class="note">${auto ? 'Faltan datos de este partido: ' : ''}buscando cuotas, alineaciones${m.status !== 'notstarted' ? ' y cronología' : ''} en Statshub y Sofascore. Tarda unos segundos.</div>`;
    const seq = navSeq;
    try {
      const r = await api(`/api/match/${id}/refresh`, { method: 'POST', keep: true });
      if (seq !== navSeq) return;
      SYNCED.add(String(id));
      const got = (r.odds || 0) + (r.odds_sofa || 0) + (r.lineups || 0) + (r.timeline || 0);
      if (got) { toast(`Actualizado: ${r.odds + r.odds_sofa} cuotas, ${r.lineups} jugadores, ${r.timeline} eventos`); route(); }
      else bar.innerHTML = `<div class="note">Todavía no hay nada publicado para este partido${r.errors && r.errors.length ? ` (${esc(r.errors.join(' · '))})` : ''}. Lo vuelvo a mirar solo cada pocos minutos.</div>`;
    } catch (e) { if (e.message !== 'stale' && $('#syncbar')) $('#syncbar').innerHTML = `<div class="note">No se pudo actualizar: ${esc(e.message)}</div>`; }
    if ($('#syncBtn')) { $('#syncBtn').disabled = false; $('#syncBtn').textContent = 'Actualizar datos'; }
  };
  $('#syncBtn').onclick = () => doSync(false);
  if (fr.needs_sync && !SYNCED.has(String(id))) doSync(true);
  if (m.status === 'inprogress') timers.push(setInterval(() => { if (location.hash.includes('partido/' + id) && tab === 'directo') route(); }, 30000));
}

/* ---------- pestañas de la ficha del partido ---------- */
const GROUP_ORDER = ['resultado', 'handicap', 'goles', 'exacto', 'mitades', 'corners', 'tarjetas', 'tiros', 'estadisticas', 'jugadores', 'especiales'];
const isRec = r => r.rec != null ? r.rec : (r.ev != null && r.ev > (r.group === 'jugadores' ? 0.10 : 0.02) && r.conf !== 'baja' && !r.warn && r.b365);
const evBadge = (ev, rec = true) => ev == null ? '<span class="faint">–</span>' : `<span class="badge ${ev > 0.02 && rec ? 'b-value' : ev > -0.03 ? 'b-neutral' : 'b-novalue'}">${F.pct(ev)}</span>`;
const confBadge = c => c === 'sin modelo' ? '<span class="badge b-baja">sin modelo</span>' : `<span class="badge b-${c}" data-tip="${esc({ alta: 'Muchas casas y el modelo coinciden: el precio justo es fiable.', media: 'Pocas casas o mercado secundario: fíate con cautela.', baja: 'Pocas cuotas o el modelo y el mercado discrepan: no apuestes solo por esto.' }[c])}">fiabilidad ${c}</span>`;
const MT = {
  directo: c => liveBlock(c.d, c.m),
  resumen(c) {
    const { m, rows, vals, vplans, mk, rep } = c;
    if (!rows.length) return `<div class="panel">${empty('Todavía no hay cuotas de este partido', 'Las casas suelen publicarlas 3-5 días antes. Pulsa «Actualizar datos» arriba o vuelve más tarde: en cuanto lleguen verás aquí todas las apuestas y los planes.')}</div>`;
    const score = v => (v.ev / Math.max((v.b365 || 2) - 1, 0.2)) * (v.conf === 'alta' ? 1 : 0.75) * (v.group === 'jugadores' ? 0.8 : 1);
    const best = [...vals].sort((a, b) => score(b) - score(a))[0];
    const bp = c.vplans[0];
    const P = s => (rows.find(r => r.market === 'OU' && r.period === 'FT' && r.selection === s && r.line === 2.5) || {}).p;
    const bt = (rows.find(r => r.market === 'BTTS' && r.period === 'FT' && r.selection === 'Y') || {}).p;
    const cs = rows.filter(r => r.market === 'CS' && r.period === 'FT').sort((a, b) => b.p - a.p).slice(0, 5);
    const card1 = best ? `<div class="vcard good"><div class="k">Mejor apuesta suelta</div><div class="t">${esc(best.label)}</div>
        <div class="figs"><div><small>bet365</small><span class="num">${F.o(best.b365 || best.best)}</span></div><div><small>justa</small><span class="num">${F.o(best.fair)}</span></div><div><small>valor</small><span class="num val">${F.pct(best.ev)}</span></div><div><small>stake</small><span class="num">${stakeOf(best) || "–"} €</span></div></div>
        <p>${best.explain ? esc(best.explain) + '. ' : ''}${confBadge(best.conf)}</p></div>`
      : `<div class="vcard"><div class="k">Mejor apuesta suelta</div><div class="t">Nada con valor ahora mismo</div><p>Ninguna cuota de bet365 supera la justa con margen suficiente. No apostar también es una decisión; vuelve a mirar cuando se muevan las cuotas.</p></div>`;
    const card2 = bp ? `<div class="vcard good"><div class="k">Mejor plan</div><div class="t">${esc(bp.title)}</div>
        <div class="figs"><div><small>valor esperado</small><span class="num val">${F.pct(bp.ev_pct)}</span></div><div><small>acabas ganando</small><span class="num">${F.p0(bp.p_profit)}</span></div><div><small>peor caso</small><span class="num loss">${F.eur(bp.worst)}</span></div></div>
        <p>${esc(bp.summary)} <a class="val" href="#/partido/${m.id}?t=planes">Ver los pasos</a></p></div>`
      : `<div class="vcard"><div class="k">Mejor plan</div><div class="t">Ningún plan con valor</div><p>He probado ${c.plans.length} planes (coberturas, empates, directo, dutching). Todos cuestan dinero a estas cuotas. <a class="val" href="#/partido/${m.id}?t=planes">Verlos igualmente</a></p></div>`;
    const sm = mk.summary || {};
    const cw = { CORNERS: 'córners', CARDS: 'tarjetas', SHOTS: 'tiros', SOT: 'a puerta' };
    const card3 = `<div class="vcard"><div class="k">Qué espera el modelo</div><div class="t">${sm.scores ? `Lo más probable: ${esc(sm.scores[0][0])}` : 'Sin modelo de goles'}</div>
        <div class="figs"><div><small>más de 2,5</small><span class="num">${F.p0(sm.p_o25)}</span></div><div><small>marcan los dos</small><span class="num">${F.p0(sm.p_btts)}</span></div><div><small>goles esperados</small><span class="num">${F.n(sm.xg, 1)}</span></div></div>
        <p>${(sm.scores || []).map(x => `${x[0]} (${F.p0(x[1])})`).join(' · ')}${Object.keys(sm.counts || {}).length ? '<br>' + Object.entries(sm.counts).map(([k, v]) => `${F.n(v[0] + v[1], 1)} ${cw[k] || k}`).join(' · ') + ' esperados' : ''}</p></div>`;
    const top = [...vals].sort((a, b) => b.ev - a.ev).slice(0, 8);
    return `<div class="verdict">${card1}${card2}${card3}</div>
      ${(mk.surebets || []).length ? `<div class="section"><h2>Surebets: ganas pase lo que pase</h2><p class="lead">Combinando la mejor cuota de cada casa. Dura minutos: comprueba las cuotas antes de apostar y que tengas cuenta en esas casas.</p>${surebetList(mk.surebets)}</div>` : ''}
      <div class="section"><div class="row" style="margin-bottom:10px"><h2 style="margin:0">Apuestas con valor en este partido</h2><span class="sp"></span><a class="btn sm" href="#/partido/${m.id}?t=apuestas">Ver los ${rows.length} mercados</a></div>
        ${top.length ? marketTable(top, true) : `<div class="panel">${empty('Ninguna con valor', `He calculado ${rows.length} selecciones de este partido y ninguna paga por encima de su cuota justa en bet365.`)}</div>`}</div>
      ${rep ? `<div class="section"><h2>Lectura del partido</h2><div class="panel"><p class="narr">${esc(rep.narrative)}</p><div class="narr-src">Factores completos, alineaciones, duelos y cara a cara en la pestaña Análisis.</div></div></div>` : ''}`;
  },
  apuestas(c) {
    const { rows, mk } = c;
    if (!rows.length) return `<div class="panel">${empty('Sin cuotas todavía', 'Pulsa «Actualizar datos» arriba para buscarlas ahora.')}</div>`;
    const groups = (mk.groups || []).filter(g => rows.some(r => r.group === g.key));
    return `<p class="lead" style="margin-top:0">Cada fila es una apuesta que ofrece al menos una casa. <b>Prob.</b> es la probabilidad real estimada (modelo + consenso de casas sin margen), <b>Justa</b> la cuota que le corresponde, y <b>Valor</b> lo que ganas de media por cada euro a la cuota de bet365. Pulsa una fila para ver todas las casas.</p>
      <div class="row" style="margin-bottom:16px"><div class="seg" id="fv"><button class="on" data-v="0">Todas</button><button data-v="1">Solo con valor</button></div>
      <div class="seg" id="fg"><button class="on" data-g="">Todos los mercados</button>${groups.map(g => `<button data-g="${g.key}">${g.name}</button>`).join('')}</div></div>
      <div id="mlist2"></div>`;
  },
  planes(c) {
    if (!c.plans.length) return `<div class="panel">${empty('Sin planes todavía', 'Los planes se calculan cuando hay cuotas y goles esperados del partido.')}</div>`;
    const fams = [...new Set(c.plans.map(p => p.family))];
    return `<p class="lead" style="margin-top:0">Estrategias de varias apuestas (entrar, cubrirte, salir) probadas sobre 6.000 simulaciones de este partido con el mismo modelo del directo. Las cuotas en vivo se estiman como la justa menos el margen típico de la casa; por eso cada paso en directo trae la cuota mínima a la que merece la pena. Stake base: 1 % de la banca.</p>
      <div class="row" style="margin-bottom:16px"><div class="seg" id="pf"><button class="on" data-f="">Todos (${c.plans.length})</button>${fams.map(f => `<button data-f="${f}">${f}</button>`).join('')}</div>
      <div class="seg" id="pv"><button class="on" data-v="0">Todos</button><button data-v="1">Solo con valor</button></div></div><div class="plans" id="plist"></div>`;
  },
  combinadas(c) {
    if (!c.combos.length) return `<div class="panel">${empty('Sin datos', 'Hace falta el modelo de goles del partido.')}</div>`;
    return `<p class="lead" style="margin-top:0">Apuestas combinadas del mismo partido («Crear apuesta» en bet365). bet365 no publica estas cuotas por adelantado, así que aquí tienes la cuota justa calculada con la matriz de marcadores: si en la casa ves una cuota igual o mayor que la <b>mínima</b>, tiene valor. Las marcadas como «van juntas» pasan más a menudo a la vez de lo que parecería multiplicando por separado.</p>
      <div class="mtable">${`<div class="mrow head" style="grid-template-columns:minmax(0,1fr) 130px 80px 90px 120px"><div>Combinada</div><div>Prob.</div><div class="r">Justa</div><div class="r">Mínima</div><div class="r">Relación</div></div>` +
        c.combos.map(x => `<div class="mrow" style="grid-template-columns:minmax(0,1fr) 130px 80px 90px 120px"><div class="lb"><b>${esc(x.label)}</b></div>
          <div class="pr"><div class="bar"><i style="width:${x.prob * 100}%"></i></div><span class="num">${F.p0(x.prob)}</span></div><div class="o"><span class="num">${F.o(x.fair)}</span></div><div class="o"><span class="num val">${F.o(x.min_odds)}</span></div>
          <div class="r small">${x.corr > 0.05 ? '<span class="badge b-value">van juntas</span>' : x.corr < -0.05 ? '<span class="badge b-novalue">se estorban</span>' : '<span class="faint">independientes</span>'}</div></div>`).join('')}</div>`;
  },
  analisis(c) { return `<div id="insights"><div class="skeleton"></div><p class="faint small">Simulando el partido 10.000 veces y cruzando estadísticas…</p></div>` + (c.rep ? `<div class="section">${reportBlock(c.rep, c.m, c.ch, c.ca)}</div>` : `<div class="skeleton"></div><p class="faint small">Preparando el análisis (forma, alineaciones, duelos, cara a cara)…</p>`); },
  cuotas(c) {
    const { d, m } = c;
    return `<div class="cols c-main"><div class="panel"><h3>Movimiento del 1X2</h3><div class="chart" id="omove"></div><div class="legend" id="olegend"></div></div>
      <div class="panel"><h3>Qué significa que se mueva</h3><p class="small muted" style="margin:0">Si la cuota baja en Pinnacle (la casa más afinada), el dinero informado está entrando en esa selección. Apostar antes de que baje es lo que mide el CLV: si de forma sostenida consigues mejores cuotas que la de cierre, estás batiendo al mercado.</p></div></div>
      <div class="section">${c.rows.length ? `<h2>Comparador de casas</h2><p class="lead">Las cuotas de cada casa en los mercados principales. En verde, la mejor cuota disponible.</p>${compareTable(c.rows.filter(r => ['resultado', 'goles', 'handicap'].includes(r.group) && (r.period || 'FT') === 'FT').slice(0, 60), m)}` : ''}</div>
      ${d.timeline.length ? `<div class="section"><h2>Cronología</h2><div class="panel chips">${d.timeline.map(e => chip(`${e.kind === 'goal' ? 'Gol' : e.kind === 'red' ? 'Roja' : e.kind} de ${e.side === 'H' ? esc(m.home_name) : esc(m.away_name)}`, Math.floor(e.minute) + "'")).join('')}</div></div>` : ''}
      ${d.bets.length ? `<div class="section"><h2>Apuestas en este partido</h2><div class="panel scroll">${betsTable(d.bets)}</div></div>` : ''}`;
  },
};
const MT_WIRE = {
  apuestas(c) {
    let onlyV = false, g = '';
    const draw = () => {
      const rs = c.rows.filter(r => (!onlyV || isRec(r)) && (!g || r.group === g));
      const gs = GROUP_ORDER.filter(k => rs.some(r => r.group === k));
      $('#mlist2').innerHTML = gs.length ? gs.map(k => { const gi = (c.mk.groups || []).find(x => x.key === k) || { name: k, help: '' };
        const gr = rs.filter(r => r.group === k);
        const body = k === 'jugadores' ? playersBlock(gr) : k === 'especiales' && !g ? `<details class="panel"><summary class="small">Ver ${gr.length} cuotas de mercados especiales</summary>${marketTable(gr)}</details>` : marketTable(gr);
        return `<div class="mgroup"><div class="gh"><h2>${gi.name}</h2><span>${gi.help}</span></div>${body}</div>`; }).join('')
        : `<div class="panel">${empty('Nada con estos filtros', 'Quita el filtro de valor o elige otro mercado.')}</div>`;
      wirePlayers(rs.filter(r => r.group === 'jugadores'));
    };
    segWire('fv', b => { onlyV = b.dataset.v === '1'; draw(); });
    segWire('fg', b => { g = b.dataset.g; draw(); });
    draw();
  },
  planes(c) {
    let fam = '', onlyV = false;
    const draw = () => { const ps = c.plans.filter(p => (!fam || p.family === fam) && (!onlyV || p.verdict === 'value'));
      $('#plist').innerHTML = ps.length ? ps.map(planCard).join('') : `<div class="panel">${empty('Ningún plan con valor en esta familia', 'Prueba con «Todos».')}</div>`; };
    segWire('pf', b => { fam = b.dataset.f; draw(); });
    segWire('pv', b => { onlyV = b.dataset.v === '1'; draw(); });
    draw();
  },
  analisis(c) { if (c.rep) wirePitch(c.rep, c.m); loadInsights(c.id); },
  async cuotas(c) {
    const { m, ch, ca, id } = c;
    const hist = await api(`/api/match/${id}/odds_history?market=1X2`, { keep: true });
    if (!$('#omove')) return;
    const colors = { H: ch, D: '#8792a2', A: ca };
    const series = Object.entries(hist).filter(([k]) => k.startsWith('Bet365') || k.startsWith('Pinnacle')).map(([k, v]) => { const [bk, s] = k.split('|'); return { name: `${bk} ${s === 'H' ? m.home_name : s === 'A' ? m.away_name : 'empate'}`, color: colors[s], dash: bk === 'Pinnacle' ? '5 4' : null, points: v, dots: v.length < 12 }; });
    lineChart($('#omove'), series, { height: 240, xfmt: t => F.dshort(t), tipx: t => new Date(t * 1000).toLocaleString('es-ES'), yfmt: v => v.toFixed(2), empty: '<br>Se grabarán a medida que se acerque el partido.' });
    $('#olegend').innerHTML = series.length ? '<span><i style="background:#8792a2"></i>bet365 (continua)</span><span><i style="background:repeating-linear-gradient(90deg,#8792a2 0 4px,transparent 4px 7px)"></i>Pinnacle (discontinua)</span>' : '';
  },
};
function playersBlock(rows) {
  const by = {};
  rows.forEach(r => { const k = r.player || 'Otros'; (by[k] = by[k] || []).push(r); });
  const ps = Object.entries(by).map(([n, rs]) => ({ n, rs, team: (rs.find(x => x.team) || {}).team, v: rs.filter(isRec).length,
    mins: (rs.find(x => x.exp_min) || {}).exp_min, modeled: rs.some(x => x.p != null) }))
    .sort((a, b) => b.v - a.v || (b.modeled - a.modeled) || b.rs.length - a.rs.length);
  return `<div class="row" style="margin-bottom:10px"><input id="psearch" placeholder="Buscar jugador…" style="max-width:280px"><span class="faint small">${ps.length} jugadores · ${rows.length} cuotas. Pulsa un jugador para ver sus mercados.</span></div>
    <div class="plist" id="plist2">${ps.map((p, i) => `<details class="pcard" data-i="${i}" data-n="${esc(p.n.toLowerCase())}"><summary><b>${esc(p.n)}</b><span class="faint small">${p.team === 'H' ? 'local' : p.team === 'A' ? 'visitante' : ''}${p.mins ? ` · juega ≈${p.mins}'` : ''}${p.modeled ? '' : ' · sin datos del jugador'}</span><span class="sp"></span>${p.v ? `<span class="badge b-value">${p.v} con valor</span>` : ''}<span class="faint small">${p.rs.length} cuotas</span></summary><div class="pbody"></div></details>`).join('')}</div>`;
}
function wirePlayers(rows) {
  const el = $('#plist2'); if (!el) return;
  const by = {};
  rows.forEach(r => { const k = (r.player || 'Otros').toLowerCase(); (by[k] = by[k] || []).push(r); });
  el.querySelectorAll('details').forEach(d => d.addEventListener('toggle', () => { const b = d.querySelector('.pbody'); if (d.open && !b.innerHTML) b.innerHTML = marketTable(by[d.dataset.n] || []); }));
  const s = $('#psearch'); if (s) s.oninput = () => { const q = s.value.trim().toLowerCase(); el.querySelectorAll('details').forEach(d => d.style.display = !q || d.dataset.n.includes(q) ? '' : 'none'); };
}
function compareTable(rs, m) {
  const books = [...new Set(rs.flatMap(r => Object.keys(r.books || {})))].filter(b => b !== 'Sofascore').slice(0, 7);
  const order = ['Bet365', 'Pinnacle', 'Betfair Exchange'];
  books.sort((a, b) => (order.indexOf(a) + 1 || 9) - (order.indexOf(b) + 1 || 9));
  return `<div class="panel scroll"><table><tr><th>Apuesta</th>${books.map(b => `<th class="r">${esc(b.replace(' Exchange', ''))}</th>`).join('')}<th class="r">Justa</th></tr>
    ${rs.map(r => { const bs = r.books || {}; const mx = Math.max(...books.map(b => bs[b] || 0));
      return `<tr><td>${esc(r.label)}</td>${books.map(b => `<td class="r num ${bs[b] && bs[b] === mx ? 'val' : ''}">${bs[b] ? F.o(bs[b]) : '<span class="faint">–</span>'}</td>`).join('')}<td class="r num faint">${F.o(r.fair)}</td></tr>`; }).join('')}</table></div>`;
}
function marketTable(rs, withMatchGroup) {
  return `<div class="mtable"><div class="mrow head"><div>Apuesta</div><div>Prob. ${help('prob')}</div><div class="r fa">Justa ${help('fair')}</div><div class="r b3">bet365</div><div class="r bk">Mejor cuota</div><div class="r">Valor ${help('ev')}</div><div class="r stk">Stake ${help('stake')}</div></div>` +
    rs.map(r => `<div class="mrow ${isRec(r) ? 'vrow' : ''}">
      <div class="lb"><b>${(r.match ? r.match.id : CUR_MID) && r.b365 && r.p != null ? `<a class="alink" href="${betHref(r.match ? r.match.id : CUR_MID, r)}">${esc(r.label)}</a>` : esc(r.label)}</b>${r.explain ? `<small>${esc(r.explain)}</small>` : ''}${r.warn ? `<small class="loss">${esc(r.warn)}</small>` : ''}${r.why_not && r.ev > 0.02 && !r.warn ? `<small class="draw-c">Paga algo más que la justa, pero no la recomendamos: ${esc(r.why_not)}</small>` : ''}
      <details><summary>${Object.keys(r.books || {}).length} casas · detalle</summary>
        <div class="books">${Object.entries(r.books || {}).map(([b, o]) => `<span class="${b === r.best_book ? 'best' : ''}">${esc(b)} <b>${F.o(o)}</b></span>`).join('')}</div>
        <div style="margin-top:6px">Modelo: ${F.p0(r.p_model)}${r.meta && r.meta.p_sub ? ` (con Sustituto+: ${F.p0(r.meta.p_sub)})` : ''} · Consenso de casas sin margen: ${F.p0(r.p_market)}${r.push ? ` · Devolución: ${F.p0(r.push)}` : ''} · ${r.conf === 'sin modelo' ? 'No se puede calcular: solo se muestran las cuotas.' : confBadge(r.conf)}</div></details></div>
      <div class="pr">${r.p != null ? `<div class="bar"><i style="width:${Math.min(100, r.p * 100)}%"></i></div><span class="num">${F.p0(r.p)}</span>` : '<span class="faint small">sin modelo</span>'}</div>
      <div class="o fa"><span class="num faint">${F.o(r.fair)}</span></div>
      <div class="o b3">${r.b365 ? `<span class="num">${F.o(r.b365)}</span>${r.b365_open && r.b365_open !== r.b365 ? `<small>abrió ${F.o(r.b365_open)}</small>` : ''}` : '<span class="faint">–</span>'}</div>
      <div class="o bk">${r.best ? `<span class="num">${F.o(r.best)}</span><small>${esc(r.best_book || '')}</small>` : '<span class="faint">–</span>'}</div>
      <div class="ev">${evBadge(r.ev, isRec(r))}</div>
      <div class="o stk">${stakeOf(r) > 0 && isRec(r) ? `<span class="num">${stakeOf(r)} €</span>` : '<span class="faint">–</span>'}</div>
      <div class="mobile-figs">Prob. ${F.p0(r.p)} · justa ${F.o(r.fair)} · bet365 ${F.o(r.b365)}${stakeOf(r) > 0 && isRec(r) ? ` · ${stakeOf(r)} €` : ''}</div>

    </div>`).join('') + '</div>';
}
function planCard(p) {
  const mx = Math.max(...p.scenarios.map(s => Math.abs(s.pnl)), 1);
  const fam = { 'Tu estrategia': 'b-media', Cobertura: 'b-neutral', Empate: 'b-neutral', Directo: 'b-neutral', Goles: 'b-neutral', Dutching: 'b-neutral', Precio: 'b-neutral' }[p.family] || 'b-neutral';
  const vb = { value: '<span class="badge b-value">con valor</span>', neutral: '<span class="badge b-neutral">neutro</span>', novalue: '<span class="badge b-novalue">sin valor</span>' }[p.verdict];
  return `<div class="plan ${p.verdict === 'value' ? 'value' : ''}">
    <div class="top"><span class="badge ${fam}">${esc(p.family)}</span>${vb}${p.match ? `<a class="tag" href="#/partido/${p.match.id}?t=planes">${esc(p.match.home_name)} – ${esc(p.match.away_name)}</a>` : ''}<span class="sp"></span>${p.p_trigger < 0.999 ? `<span class="faint small">se activa en el ${F.p0(p.p_trigger)} de los partidos</span>` : ''}</div>
    <div class="ttl">${esc(p.title)}</div><p class="sum">${esc(p.summary)}</p>
    <div class="steps">${p.steps.map((s, i) => `<div data-n="${i + 1}"><b>${esc(s.when)}</b>${esc(s.do)}</div>`).join('')}</div>
    <div class="pfigs"><div><small>Valor esperado</small><span class="num ${F.cls(p.ev)}">${F.eur(p.ev)}</span></div><div><small>Rentabilidad</small><span class="num ${F.cls(p.ev_pct)}">${F.pct(p.ev_pct)}</span></div>
      <div><small>Acabas ganando</small><span class="num">${F.p0(p.p_profit)}</span></div><div><small>Peor caso</small><span class="num loss">${F.eur(p.worst)}</span></div></div>
    <div class="scen">${p.scenarios.map(s => `<div class="s"><span class="nm" title="${esc(s.name)}">${esc(s.name)}</span><span class="pp">${F.p0(s.prob)}</span>
      <div class="bar"><i style="${s.pnl >= 0 ? 'left:50%' : 'right:50%'};width:${Math.abs(s.pnl) / mx * 50}%;background:${s.pnl >= 0 ? C.win : C.loss}"></i></div>
      <span class="eur ${F.cls(s.pnl)}">${F.eur(s.pnl)}</span></div>`).join('')}</div>
  </div>`;
}
function surebetList(sb) {
  return `<div class="cols c2">${sb.slice(0, 6).map(s => `<div class="panel"><div class="row"><b>${esc(MK[s.market] || s.market)}${s.period !== 'FT' ? ' · ' + s.period : ''}</b><span class="sp"></span><span class="badge b-value">+${(s.profit_pct * 100).toFixed(1)} % seguro</span></div>
    ${s.match ? `<div class="small faint">${esc(s.match.home_name)} – ${esc(s.match.away_name)}</div>` : ''}
    <table style="margin-top:8px">${s.legs.map(l => `<tr><td>${esc(l.label)}</td><td class="small muted">${esc(l.book)}</td><td class="r num">${F.o(l.odds)}</td><td class="r num">${l.stake.toFixed(2)} €</td></tr>`).join('')}</table>
    <div class="small faint" style="margin-top:6px">Con 100 € repartidos así cobras ${(100 * (1 + s.profit_pct)).toFixed(2)} € salga lo que salga.</div></div>`).join('')}</div>`;
}

const SYNCED = new Set();
function freshText(fr, m) {
  const bits = [];
  if (fr.odds_ts) bits.push(`cuotas ${F.ago(fr.odds_ts)}`); else if (m.status === 'notstarted') bits.push('sin cuotas aún');
  const lu = (fr.lineups || []).find(x => x.source === 'confirmed') || (fr.lineups || []).find(x => x.source === 'predicted');
  if (m.status === 'notstarted') bits.push(lu ? `once ${lu.source === 'confirmed' ? 'confirmado' : 'previsto'} ${F.ago(lu.ts)}` : 'sin once aún');
  if (fr.odds_ts && !fr.has_b365) bits.push('bet365 no publicado');
  return bits.join(' · ');
}
function liveBlock(d, m) {
  const lo = d.live_odds || [];
  const pick = (mk, sel, line) => (lo.find(r => r.market === mk && r.selection === sel && r.period === 'FT' && (line == null || r.line === line)) || {}).odds;
  const fair = (d.live && d.live.fair) || {};
  const rows = [['1', '1X2', 'H'], ['X', '1X2', 'D'], ['2', '1X2', 'A']].map(([k, mk, s]) => [k, pick(mk, s), (fair['1X2'] || {})[s]]);
  const ou = [...new Set(lo.filter(r => r.market === 'OU').map(r => r.line))].slice(0, 3);
  const ouRows = ou.flatMap(L => [[`Más de ${L}`, pick('OU', 'O', L), (fair.OU || {})['O' + L]], [`Menos de ${L}`, pick('OU', 'U', L), (fair.OU || {})['U' + L]]]);
  const all = rows.concat(ouRows);
  return `<div class="section"><h2>En directo</h2><div class="cols c-main">
    <div>${d.live ? liveCard(d.live) : '<div class="panel">' + empty('Esperando al rastreador', 'El planificador actualiza el directo cada 30 segundos.') + '</div>'}</div>
    <div class="panel"><h3>Cuota en vivo frente a la justa</h3>${lo.length ? `<table><tr><th>Selección</th><th class="r">Ahora</th><th class="r">Justa</th><th class="r">Valor</th></tr>
      ${all.map(([k, o, f]) => `<tr><td>${k}</td><td class="r num" style="font-size:17px">${F.o(o)}</td><td class="r num faint" style="font-size:17px">${F.o(f)}</td><td class="r num ${o && f && o / f - 1 > 0.03 ? 'val' : F.cls(o && f ? o / f - 1 : null)}">${o && f ? F.pct(o / f - 1) : '–'}</td></tr>`).join('')}</table>
      <div class="faint small" style="margin-top:8px">Cuotas en vivo de Sofascore (minuto ${Math.floor(lo[0].minute || 0)}). bet365 suele estar muy cerca; compruébalo antes de entrar.</div>`
      : empty('Sin cuotas en vivo', 'Aparecen cuando el planificador está en marcha y el partido tiene mercados abiertos.')}</div></div></div>`;
}
function reportBlock(r, m, ch, ca) {
  const fx = r.factors || [], lean = r.lean || {}, lu = r.lineups || {}, duels = r.duels || [], h2h = r.h2h || [];
  const fxRows = fx.slice(0, 12).map(f => {
    const w = Math.max(4, f.strength * 50);
    const color = f.side === 'H' ? ch : f.side === 'A' ? ca : f.side === 'O' || f.side === 'U' ? C.info : C.value;
    const bar = f.side === 'H' ? `<i style="right:50%;width:${w}%;background:${color}"></i>` : f.side === 'A' ? `<i style="left:50%;width:${w}%;background:${color}"></i>` : `<i style="left:${50 - w / 2}%;width:${w}%;background:${color};opacity:.75"></i>`;
    return `<div class="factor"><div class="t"><b>${esc(f.label)}</b>${esc(f.group)}</div><div class="bar">${bar}</div><div class="t right">${esc(f.text)}</div></div>`;
  }).join('');
  const hwin = g => g.home_id === m.home_id ? g.hg > g.ag : g.ag > g.hg, awin = g => g.home_id === m.home_id ? g.ag > g.hg : g.hg > g.ag;
  const withCards = h2h.filter(g => g.cards != null);
  return `
  <div class="cols c-main section">
    <div class="panel"><h2>Lectura del partido</h2><p class="narr">${esc(r.narrative)}</p>
      <div class="narr-src">${r.narrative_source === 'ia' ? 'Redactado por IA a partir de los factores calculados, sin datos añadidos.' : 'Redactado automáticamente a partir de los factores calculados.'}</div></div>
    <div class="panel"><h2>Hacia dónde empujan los factores</h2>
      ${barRow(m.home_name, lean.H, ch)}${barRow(m.away_name, lean.A, ca)}${barRow('Empate', lean.D, '#a7bcb1')}${barRow('Más goles', lean.O, C.info)}${barRow('Menos goles', lean.U, C.info)}
      <div class="small faint" style="margin-top:8px">Suma de la fuerza de los factores que favorecen cada lado.</div></div>
  </div>
  <div class="section"><h2>Factores</h2><p class="lead">Cada barra sale hacia el equipo al que favorece el factor; cuanto más larga, más pesa. Todos se calculan solo con datos anteriores al partido.</p>
    <div class="panel">${fx.length ? `<div class="factor-axis"><div></div><div><span>${esc(m.home_name)}</span><span>${esc(m.away_name)}</span></div><div></div></div>${fxRows}` : empty('Sin factores', 'Faltan datos de este partido.')}</div></div>
  <div class="section"><h2>Alineaciones y duelos</h2><p class="lead">Once ${lu.source === 'confirmed' ? 'confirmado' : lu.source === 'predicted' ? 'previsto por Statshub' : lu.source === 'played' ? 'que jugó' : 'no disponible todavía'}. Las líneas discontinuas son los duelos clave: amarillas si gana el atacante, azules si gana el defensor. El tamaño de cada jugador es su impacto ${help('impact')}.</p>
    <div class="cols c-main"><div class="pitch-wrap" id="pitch">${(lu.H || []).length ? '' : empty('Sin alineación', 'Statshub publica la prevista 1-2 días antes y la confirmada una hora antes del partido.')}</div>
      <div class="panel"><h3>Duelos clave</h3>${duels.length ? duels.slice(0, 6).map(d => duelRow(d, ch, ca)).join('') : '<div class="muted small">Aparecerán con la alineación.</div>'}
        ${['H', 'A'].map(s => (lu['missing_' + s] || []).length ? `<h3 style="margin-top:18px">Fuera del once de ${esc(s === 'H' ? m.home_name : m.away_name)}</h3><div class="chips">${lu['missing_' + s].map(x => chip(esc(x.name), `${x.starts}/8`)).join('')}</div><div class="faint small" style="margin-top:4px">Titulares en los últimos 8 partidos que hoy no salen.</div>` : '').join('')}
        ${lu.strength_H != null ? `<h3 style="margin-top:18px">Fuerza del once frente al habitual</h3>${barRow(m.home_name, lu.strength_H / Math.max(lu.usual_H || 1, .1) * 1.5, ch, `${F.n(lu.strength_H, 1)} / ${F.n(lu.usual_H, 1)}`)}${barRow(m.away_name, lu.strength_A / Math.max(lu.usual_A || 1, .1) * 1.5, ca, `${F.n(lu.strength_A, 1)} / ${F.n(lu.usual_A, 1)}`)}` : ''}
      </div></div></div>
  <div class="cols c2 section">
    <div><h2>Forma reciente</h2><div class="panel">${['H', 'A'].map(s => formTable(r.form[s] || [], s === 'H' ? m.home_name : m.away_name)).join('<div style="height:18px"></div>')}</div></div>
    <div><h2>Cara a cara</h2><div class="panel">${h2h.length ? `<div class="h2h" style="--hc:${ch};--ac:${ca}">${h2h.map(g => `<div class="g ${hwin(g) ? 'hw' : awin(g) ? 'aw' : ''}"><small>${F.dshort(g.kickoff)}</small><span class="num">${g.hg}–${g.ag}</span><small>en casa de ${esc((g.home_name || '').split(' ')[0])}</small></div>`).join('')}</div>
      <div class="chips" style="margin-top:14px">${chip('Gana ' + esc(m.home_name), h2h.filter(hwin).length)}${chip('Empates', h2h.filter(g => g.hg === g.ag).length)}${chip('Gana ' + esc(m.away_name), h2h.filter(awin).length)}${chip('Goles por partido', F.n(h2h.reduce((a, g) => a + g.hg + g.ag, 0) / h2h.length, 1))}${withCards.length ? chip('Tarjetas por partido', F.n(withCards.reduce((a, g) => a + g.cards, 0) / withCards.length, 1)) : ''}</div>
      <div class="faint small" style="margin-top:8px">El borde inferior marca quién ganó cada duelo.</div>`
      : empty('Sin enfrentamientos previos', 'No hay partidos entre estos dos equipos en la base de datos.')}</div></div>
  </div>`;
}
const barRow = (k, v, c, lbl) => `<div style="display:grid;grid-template-columns:130px 1fr 70px;gap:10px;align-items:center;margin:8px 0"><span class="small" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(k)}</span><div style="height:10px;background:var(--pitch);border-radius:5px"><div style="height:100%;width:${Math.min(100, Math.max(0, (v || 0) * 33))}%;background:${c};border-radius:5px"></div></div><span class="num r" style="font-size:17px">${lbl || F.n(v, 1)}</span></div>`;
function duelRow(d, ch, ca) {
  const atkCol = d.side === 'H' ? ch : ca, defCol = d.side === 'H' ? ca : ch;
  const w = Math.min(50, Math.abs(d.advantage) * 22);
  const bar = d.advantage > 0 ? `<i style="${d.side === 'H' ? 'right' : 'left'}:50%;width:${w}%;background:${atkCol}"></i>` : `<i style="${d.side === 'H' ? 'left' : 'right'}:50%;width:${w}%;background:${defCol}"></i>`;
  const L = d.side === 'H' ? d.attacker : d.defender, R = d.side === 'H' ? d.defender : d.attacker;
  const desc = x => x.dribbles != null ? `${F.n(x.dribbles, 1)} regates y ${F.n(x.xg, 2)} xG por 90'` : `${F.n(x.tackles, 1)} entradas por 90', ${F.p0(x.duels_pct)} duelos ganados`;
  return `<div class="duel"><div class="p"><b>${esc(L.name)}</b><small>${L.pos}, ${desc(L)}</small></div>
    <div><div class="meter">${bar}</div><div class="lbl">${esc(d.zone)}${d.card_risk >= 0.25 ? `<br>tarjeta ${F.p0(d.card_risk)}` : ''}</div></div>
    <div class="p r"><b>${esc(R.name)}</b><small>${R.pos}, ${desc(R)}</small></div></div>`;
}
function formTable(rows, name) {
  if (!rows.length) return `<div class="muted small">Sin partidos previos de ${esc(name)}.</div>`;
  return `<h3>${esc(name)}</h3><table>${rows.map(g => `<tr><td style="width:28px">${formDots(g.res)}</td><td class="small">${g.home ? 'contra' : 'en casa de'} ${esc(g.opp)}<div class="faint" style="font-size:12px">${F.dshort(g.kickoff)}, ${COMP[g.comp] || g.comp}</div></td>
    <td class="r num" style="font-size:19px">${g.gf}–${g.ga}</td><td class="r" style="width:120px">${g.xgf != null ? `<div class="small faint">xG ${F.n(g.xgf, 1)} – ${F.n(g.xga, 1)}</div><div style="display:flex;height:5px;gap:2px;margin-top:3px"><i style="flex:${g.xgf};background:var(--win);border-radius:2px"></i><i style="flex:${g.xga};background:var(--loss);border-radius:2px"></i></div>` : ''}</td></tr>`).join('')}</table>`;
}
function wirePitch(r, m) {
  const el = $('#pitch'); const lu = r.lineups || {};
  if (!el || !(lu.H || []).length) return;
  el.innerHTML = pitchSVG(lu, r.duels, { H: m.home_name, A: m.away_name }, m.colors || { H: [], A: [] });
}
function oddsTables(d, m) {
  const groups = {};
  d.odds.forEach(o => { if (!MK[o.market]) return; const k = o.market + (o.period !== 'FT' ? ' ' + o.period : ''); (groups[k] = groups[k] || []).push(o); });
  const keys = Object.keys(groups).sort((a, b) => Object.keys(MK).indexOf(a.split(' ')[0]) - Object.keys(MK).indexOf(b.split(' ')[0]));
  if (!keys.length) return empty('Sin cuotas todavía', 'Las casas suelen publicar las cuotas unos días antes. Pulsa «Actualizar datos» arriba para buscarlas ahora.');
  const lbl = o => { const n = { H: m.home_name, A: m.away_name, D: 'Empate', O: 'Más de', U: 'Menos de', Y: 'Sí', N: 'No', '1X': m.home_name + ' o empate', X2: 'Empate o ' + m.away_name, '12': 'Cualquiera gana', HO: m.home_name + ', más de', HU: m.home_name + ', menos de', AO: m.away_name + ', más de', AU: m.away_name + ', menos de' }[o.selection] || o.selection;
    return `${esc(n)}${o.line != null ? ' ' + (o.market === 'AH' && o.line > 0 ? '+' : '') + o.line : ''}`; };
  return keys.slice(0, 8).map(k => `<h3 style="margin-top:6px">${MK[k.split(' ')[0]]}${k.includes(' ') ? ' (' + k.split(' ')[1] + ')' : ''}</h3><table style="margin-bottom:18px">
    <tr><th>Selección</th><th class="r">bet365</th><th class="r">Pinnacle</th><th class="r">Betfair</th><th class="r">Sofascore</th><th class="r">Justa ${help('fair')}</th><th class="r">Valor</th></tr>
    ${groups[k].slice(0, 14).map(o => { const b = o.books.Bet365, pi = o.books.Pinnacle, bf = o.books['Betfair Exchange'], so = o.books.Sofascore;
      const cell = x => x ? `<span class="num" style="font-size:17px">${F.o(x.now)}</span>${x.open !== x.now ? `<span class="faint small"> (${F.o(x.open)})</span>` : ''}` : '<span class="faint">–</span>';
      return `<tr><td>${lbl(o)}</td><td class="r">${cell(b)}</td><td class="r">${cell(pi)}</td><td class="r">${cell(bf)}</td><td class="r">${cell(so)}</td><td class="r num faint" style="font-size:17px">${F.o(o.fair)}</td><td class="r num ${o.edge_b365 > 0.02 ? 'val' : F.cls(o.edge_b365)}" style="font-size:17px">${F.pct(o.edge_b365)}</td></tr>`; }).join('')}</table>`).join('');
}

/* ============================================================ CALCULADORA */
async function calculadora() {
  const q = new URLSearchParams(location.hash.split('?')[1] || ''); const [sh, sa] = (q.get('sc') || '1-1').split('-').map(Number);
  view.innerHTML = ph('Calculadora', 'Introduce tus apuestas abiertas y las cuotas que ves en bet365. Se liquida cada marcador final como lo hace la casa, con cuartos de línea y hándicap en directo desde el marcador actual.') +
    `<div class="seg" id="tabs"><button class="on" data-t="gen">Cobertura</button><button data-t="quick">Fórmulas rápidas</button><button data-t="fair">Cuota justa en directo</button></div><div id="tab" style="margin-top:20px"></div>`;
  const tabs = {
    gen() {
      $('#tab').innerHTML = `<div class="cols c2"><div class="panel">
        <h2>Situación del partido</h2>
        <div class="fgrid"><label class="field">Minuto<input id="c_min" type="number" value="${q.get('min') || 82}"></label><label class="field">Goles local<input id="c_hg" type="number" value="${sh}"></label><label class="field">Goles visitante<input id="c_ag" type="number" value="${sa}"></label><label class="field">Rojas local/visitante<input id="c_red" value="0/0"></label></div>
        <label class="field" style="margin-top:12px">Cuotas 1X2 en este momento (opcional, mejora la precisión)<input id="c_1x2" placeholder="Ejemplo: 4.00 1.55 5.50" value="${q.get('m') ? '' : '4.00 1.55 5.50'}"></label>
        <input id="c_mid" type="hidden" value="${q.get('m') || ''}">
        <h2 style="margin-top:22px">Lo que ya tienes apostado</h2><div id="legsP"></div><button class="btn sm ghost" id="addP">Añadir apuesta</button>
        <h2 style="margin-top:22px">Con qué te puedes cubrir</h2><div id="legsH"></div><button class="btn sm ghost" id="addH">Añadir cobertura</button>
        <h2 style="margin-top:22px">Qué quieres conseguir</h2>
        <div class="seg" id="obj">${[['balance', 'Igualar local y visitante'], ['zero', 'Dejar una rama a cero'], ['equalize', 'Máximo beneficio asegurado'], ['kelly', 'Hacer crecer la banca'], ['minvar', 'Mínimo riesgo']].map(([k, t], i) => `<button data-o="${k}" class="${i ? '' : 'on'}">${t}</button>`).join('')}</div>
        <div class="fgrid" style="margin-top:12px"><label class="field">Rama a dejar en cero<select id="c_zero"><option>Gana local</option><option>Gana visitante</option><option>Empate</option></select></label><label class="field">Banca (€)<input id="c_bank" type="number" value="${meta.bankroll || 1000}"></label></div>
        <div class="row" style="margin-top:18px"><button class="btn go" id="solve">Calcular cobertura</button><button class="btn ghost" id="ex">Cargar tu ejemplo del 75'</button></div>
      </div><div class="panel" id="res">${empty('El resultado sale aquí', 'Verás cuánto meter en cada cobertura, cómo queda cada posible final y cuánto te cuesta cubrirte.')}</div></div>`;
      const legRow = (l = {}) => `<div class="fgrid leg" style="grid-template-columns:1.4fr 1fr .7fr .8fr .8fr auto;align-items:end;margin-bottom:8px">
        <label class="field">Mercado<select class="mk">${['1X2', 'AH', 'DNB', 'OU', 'DC', 'BTTS', 'CS', 'TEAM_OU'].map(x => `<option value="${x}" ${l.market === x ? 'selected' : ''}>${MK[x] || x}</option>`).join('')}</select></label>
        <label class="field">Selección<input class="sel" value="${l.selection || 'H'}" data-tip="H local, D empate, A visitante, O más, U menos, 1X, X2, 12, Y sí, N no, o un resultado tipo 2-1"></label>
        <label class="field">Línea<input class="line" value="${l.line ?? ''}" placeholder="0"></label><label class="field">Cuota<input class="odds" value="${l.odds || ''}"></label>
        <label class="field">Stake<input class="stake" value="${l.stake ?? ''}" placeholder="—"></label><button class="btn sm ghost rm" aria-label="Quitar">✕</button></div>`;
      const add = (id, l) => { const d = document.createElement('div'); d.innerHTML = legRow(l); d.querySelector('.rm').onclick = () => d.remove(); $('#' + id).appendChild(d); };
      $('#addP').onclick = () => add('legsP'); $('#addH').onclick = () => add('legsH');
      add('legsP', { market: '1X2', selection: 'A', odds: 21, stake: 10 }); add('legsH', { market: 'AH', selection: 'H', line: 0, odds: 1.66 });
      let obj = 'balance'; segWire('obj', b => { obj = b.dataset.o; });
      $('#ex').onclick = () => { $('#c_min').value = 82; $('#c_hg').value = 1; $('#c_ag').value = 1; $('#c_1x2').value = '4.00 1.55 5.50'; $('#legsP').innerHTML = ''; $('#legsH').innerHTML = ''; add('legsP', { market: '1X2', selection: 'A', odds: 21, stake: 10 }); add('legsH', { market: 'AH', selection: 'H', line: 0, odds: 1.66 }); toast("Ejemplo cargado: 10 € al B a 21 al 75', empate 1-1 al 82'"); };
      const read = (id, hg, ag) => $$(`#${id} .leg`).map(r => { let mk = r.querySelector('.mk').value; let line = r.querySelector('.line').value === '' ? null : parseFloat(r.querySelector('.line').value); if (mk === 'DNB') { mk = 'AH'; line = 0; }
        const inplay = id === 'legsH' && mk === 'AH'; return { market: mk, selection: r.querySelector('.sel').value.trim().toUpperCase(), line, odds: parseFloat(r.querySelector('.odds').value), stake: parseFloat(r.querySelector('.stake').value || 0), base_h: inplay ? hg : 0, base_a: inplay ? ag : 0 }; });
      $('#solve').onclick = async () => {
        const hg = +$('#c_hg').value, ag = +$('#c_ag').value, [rh, ra] = $('#c_red').value.split('/').map(Number), o = $('#c_1x2').value.trim().split(/[\s,;]+/).map(Number).filter(x => x > 1);
        const body = { positions: read('legsP', 0, 0), hedges: read('legsH', hg, ag), objective: obj, zero_branch: $('#c_zero').value, bankroll: +$('#c_bank').value, minute: +$('#c_min').value, hg, ag, red_h: rh || 0, red_a: ra || 0, odds_1x2: o.length === 3 ? o : null, match_id: $('#c_mid').value ? +$('#c_mid').value : null };
        $('#solve').disabled = true;
        try {
          const r = await api('/api/hedge', { method: 'POST', body });
          const mx = Math.max(...Object.values(r.branches).map(Math.abs), 1);
          $('#res').innerHTML = `<h2>Resultado</h2>${r.stakes.map((s, i) => { const h = body.hedges[i], fair = r.hedge_fair_odds[i];
            return `<div style="padding-bottom:14px;border-bottom:1px solid var(--line);margin-bottom:14px"><div class="muted small">Mete en ${MK[h.market] || h.market} ${h.selection} ${h.line ?? ''} a ${h.odds}</div><div class="num val" style="font-size:54px;line-height:1.05">${s.toFixed(2)} €</div>
            <div class="small ${fair > h.odds ? 'loss' : 'win'}">${fair > h.odds ? `La cuota justa es ${F.o(fair)}: cubrirte aquí te cuesta algo de valor` : `La cuota justa es ${F.o(fair)}: esta cobertura además tiene valor`}</div></div>`; }).join('')}
            <h3>Cómo queda cada final</h3>${Object.entries(r.branches).map(([k, v]) => `<div style="display:grid;grid-template-columns:120px 1fr 90px;gap:10px;align-items:center;margin:8px 0"><span class="small">${k}</span>
              <div style="position:relative;height:14px;background:var(--pitch);border-radius:4px"><div style="position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--line2)"></div><div style="position:absolute;top:0;bottom:0;${v >= 0 ? 'left:50%' : 'right:50%'};width:${Math.abs(v) / mx * 50}%;background:${v >= 0 ? C.win : C.loss};border-radius:4px"></div></div>
              <span class="num r ${F.cls(v)}" style="font-size:19px">${F.eur(v)}</span></div>`).join('')}
            <div class="chips" style="margin-top:14px">${chip('Valor esperado cubriéndote', F.eur(r.ev))}${chip('Sin cubrirte', F.eur(r.ev_without))}${chip('Coste de cubrirte', F.eur(r.hedge_cost_ev))}${chip('Riesgo', `${F.n(r.std, 1)} (antes ${F.n(r.std_without, 1)})`)}${chip('Peor caso', F.eur(r.worst))}</div>
            ${(r.notes || []).map(n => `<div class="note">${esc(n)}</div>`).join('')}
            <p class="small muted" style="margin-top:14px">Cubrirse no crea valor: reduce el riesgo y casi siempre cuesta el margen de la segunda apuesta. El beneficio esperado nace de haber entrado por encima de la cuota justa.</p>`;
        } catch (e) { $('#res').innerHTML = `<div class="note">${esc(e.message)}</div>`; }
        $('#solve').disabled = false;
      };
    },
    quick() {
      const forms = [['zero', 'Cubrir dejando una rama a cero', 'Cuánto meter para no perder nada si gana tu cobertura.', [['stake', 'Stake original', 10], ['hedge_odds', 'Cuota de la cobertura', 1.66]]],
        ['equalize', 'Igualar las dos ramas ganadoras', 'Ganas lo mismo gane tu apuesta o tu cobertura.', [['stake', 'Stake original', 10], ['odds', 'Cuota original', 21], ['hedge_odds', 'Cuota de la cobertura', 1.66]]],
        ['greenup', 'Cash-out justo', 'Lo que vale hoy tu apuesta a las cuotas actuales.', [['stake', 'Stake', 10], ['odds', 'Cuota tomada', 21], ['odds_now', 'Cuota actual', 5.5]]],
        ['lay', 'Lay en Betfair', 'Igualar resultado con un lay en exchange.', [['stake', 'Stake back', 10], ['odds', 'Cuota back', 1.3], ['lay_odds', 'Cuota lay', 3.8], ['commission', 'Comisión', 0.05]]],
        ['kelly', 'Stake según Kelly', 'Cuánto apostar según tu ventaja (fracción de Kelly).', [['prob', 'Probabilidad (0-1)', 0.55], ['odds', 'Cuota', 2.0], ['bankroll', 'Banca', meta.bankroll || 1000], ['fraction', 'Fracción', 0.25]]],
        ['dutch', 'Dutching', 'Repartir entre varias selecciones para cobrar lo mismo.', [['odds', 'Cuotas separadas por espacio', '1.62 6.0'], ['target_return', 'Cobro objetivo', 13]]]];
      $('#tab').innerHTML = `<div class="cols c3">${forms.map(([k, t, s, fs]) => `<div class="panel"><h3>${t}</h3><p class="small muted" style="margin-top:0">${s}</p><div class="fgrid" data-k="${k}">${fs.map(([n, l, v]) => `<label class="field">${l}<input name="${n}" value="${v}"></label>`).join('')}</div>
        <button class="btn sm go" style="margin-top:12px" data-go="${k}">Calcular</button><div id="q_${k}" style="margin-top:12px"></div></div>`).join('')}</div>`;
      const LBL = { hedge_stake: 'Stake de cobertura', lay_stake: 'Stake del lay', liability: 'Responsabilidad', if_back_wins: 'Si gana el back', if_back_loses: 'Si pierde el back', fair_cashout: 'Cash-out justo', profit_locked: 'Beneficio asegurado', hedge_stake_back_opposite_equiv: 'Equivale a apostar en contra', stake: 'Stake', stakes: 'Stakes', total: 'Total', return: 'Cobro', profit: 'Beneficio', book: 'Suma de inversas' };
      $$('[data-go]').forEach(b => b.onclick = async () => { const k = b.dataset.go, params = {}; $$(`[data-k="${k}"] input`).forEach(i => params[i.name] = i.name === 'odds' && k === 'dutch' ? i.value.split(/\s+/).map(Number) : parseFloat(i.value));
        const r = await api('/api/calc', { method: 'POST', body: { kind: k, params } });
        $('#q_' + k).innerHTML = Object.entries(r).map(([a, v]) => `<div class="row" style="justify-content:space-between;border-bottom:1px solid var(--line);padding:6px 0"><span class="small muted">${LBL[a] || a}</span><b class="num" style="font-size:20px">${Array.isArray(v) ? v.map(x => F.n(x)).join(' / ') : F.n(v)}</b></div>`).join(''); });
    },
    fair() {
      $('#tab').innerHTML = `<div class="cols c2"><div class="panel"><h2>Estado</h2><div class="fgrid">
        <label class="field">Cuotas 1X2 antes del partido<input id="f_1x2" value="2.10 3.40 3.60"></label><label class="field">Prob. de más de 2,5 (opcional)<input id="f_o" placeholder="0.52"></label>
        <label class="field">Minuto<input id="f_min" value="75"></label><label class="field">Goles local<input id="f_hg" value="1"></label><label class="field">Goles visitante<input id="f_ag" value="0"></label>
        <label class="field">Rojas local<input id="f_rh" value="0"></label><label class="field">Rojas visitante<input id="f_ra" value="0"></label><label class="field">xG local (opcional)<input id="f_xh"></label><label class="field">xG visitante (opcional)<input id="f_xa"></label><label class="field">Añadido anunciado<input id="f_add"></label></div>
        <button class="btn go" style="margin-top:14px" id="f_go">Calcular cuotas justas</button></div><div class="panel" id="f_res">${empty('Cuotas justas', 'El modelo en directo calcula la probabilidad de cada mercado en este minuto y marcador.')}</div></div>`;
      $('#f_go').onclick = async () => { const n = id => $(id).value === '' ? null : parseFloat($(id).value);
        const r = await api('/api/calc', { method: 'POST', body: { kind: 'fair', params: { odds_1x2: $('#f_1x2').value.split(/\s+/).map(Number), p_over25: n('#f_o'), minute: n('#f_min'), hg: n('#f_hg'), ag: n('#f_ag'), red_h: n('#f_rh'), red_a: n('#f_ra'), xg_h: n('#f_xh'), xg_a: n('#f_xa'), added: n('#f_add') } } });
        const block = (t, o, ren = k => k) => `<h3 style="margin-top:14px">${t}</h3><div class="chips">${Object.entries(o).filter(([k]) => k !== 'p').map(([k, v]) => chip(ren(k), F.o(v))).join('')}</div>`;
        $('#f_res').innerHTML = `<h2>Cuotas justas, sin margen</h2>${block('Resultado', r['1X2'], k => ({ H: 'Local', D: 'Empate', A: 'Visitante' }[k]))}${block('Próximo gol', r.NEXT_GOAL, k => ({ H: 'Local', A: 'Visitante', NONE: 'Ninguno' }[k]))}${block('Hándicap asiático (cuenta desde el marcador actual)', r.AH, k => `${k[0] === 'H' ? 'Local' : 'Visit.'} ${k.slice(1)}`)}${block('Más/menos goles (total del partido)', r.OU, k => `${k[0] === 'O' ? 'Más de' : 'Menos de'} ${k.slice(1)}`)}${block('Ambos marcan', r.BTTS, k => k === 'Y' ? 'Sí' : 'No')}${block('Resultados exactos más probables', r.CS)}
          <p class="small muted" style="margin-top:14px">Para tener ventaja necesitas una cuota al menos un 3-5 % por encima de la justa.</p>`; };
    },
  };
  segWire('tabs', b => tabs[b.dataset.t]());
  tabs.gen();
}

/* ============================================================ ESTRATEGIAS */
function stratRow(r, o, extra) {
  return `<a class="s" href="#/estrategia/${r.id}">
    <div class="st">${pill(r.status)}</div><div class="n"><b>${esc(r.name)}</b><span>${esc(extra)}</span></div>
    <div class="m"><span class="num">${o.n ?? '–'}</span><small>apuestas</small></div>
    <div class="m"><span class="num ${F.cls(o.yield)}">${F.pct(o.yield)}</span><small>yield</small></div>
    <div class="m hide-m"><span class="num ${F.cls(o.shrunk)}">${F.pct(o.shrunk)}</span><small>${o.p_profitable != null ? 'P(rentable) ' + F.p0(o.p_profitable) : 'contraído'}</small></div>
    <div class="m hide-t hide-m"><span class="num">${o.p_adj != null ? F.n(o.p_adj, 3) : '–'}</span><small>${o.clv != null ? 'CLV ' + F.pct(o.clv) : 'p ajustado'}</small></div></a>`;
}
const slistHead = () => `<div class="s head"><div class="st">Estado</div><div>Estrategia</div><div class="m">Muestra</div><div class="m">Yield ${help('oos')}</div><div class="m hide-m">Contraído ${help('shrunk')}</div><div class="m hide-t hide-m">p ajust. ${help('padj')}</div></div>`;
async function estrategias() {
  view.innerHTML = ph('Estrategias', 'Todas las hipótesis probadas. Solo cuenta lo que pasa fuera de muestra, en temporadas que el buscador no vio.') +
    `<div class="row"><div class="seg" id="sst"><button class="on" data-s="">Todas</button>${['live', 'paper', 'validated', 'candidate', 'rejected', 'retired'].map(s => `<button data-s="${s}">${STATUS[s]}</button>`).join('')}</div><span class="sp"></span>
    <div class="seg" id="sph"><button class="on" data-p="">Pre y directo</button><button data-p="pre">Pre-partido</button><button data-p="live">Directo</button></div></div><div class="panel section slist" id="slist" style="padding:6px 0"></div>`;
  let s = '', p = '';
  const draw = async () => {
    const rows = await api(`/api/strategies?limit=400${s ? '&status=' + s : ''}${p ? '&phase=' + p : ''}`);
    $('#slist').innerHTML = rows.length ? slistHead() + rows.map(r => stratRow(r, { ...(r.oos || {}), p_adj: r.p_adj }, `${r.phase === 'live' ? 'Directo' : 'Pre-partido'}${(r.oos || {}).price_source === 'simulated' ? ' con precios simulados' : ''}. ${r.description || ''}`)).join('')
      : empty('Todavía no hay estrategias aquí', 'El laboratorio las genera y las valida.', '<a class="btn go" href="#/laboratorio">Ir al laboratorio</a>');
  };
  segWire('sst', b => { s = b.dataset.s; draw(); });
  segWire('sph', b => { p = b.dataset.p; draw(); });
  draw();
}
function recipe(spec) {
  const c = spec.context || {}, t = spec.trigger || {}, m = spec.market || {}, pr = spec.price || {}, e = spec.exit || {}, st = spec.staking || {}, mf = spec.model_filter || {};
  const ctx = [c.comps && c.comps.map(x => COMP[x] || x).join(', '), c.fav_prob && `favorito con ${F.p0(c.fav_prob[0])} a ${F.p0(c.fav_prob[1])}`, ...(c.filters || []).map(f => `${f.f} ${f.op} ${f.v}`)].filter(Boolean).join('; ') || 'Cualquier partido';
  const leaderTxt = { fav: 'el favorito', dog: 'el débil', none: 'nadie', H: 'el local', A: 'el visitante' };
  const trg = spec.phase === 'live' ? [t.minute && `minuto ${t.minute[0]}${t.minute[1] !== t.minute[0] ? ' a ' + t.minute[1] : ''}`, t.leader && `gana ${leaderTxt[t.leader] || t.leader}`, t.margin && `por ${t.margin[0]}${t.margin[1] !== t.margin[0] ? '-' + t.margin[1] : ''} gol(es)`, t.total_goals && `${t.total_goals[0]} a ${t.total_goals[1]} goles en total`, t.red && `roja: ${t.red}`, t.xg_gap_min && `diferencia de xG ≥ ${t.xg_gap_min}`, t.goal_recent && `gol hace ≤ ${t.goal_recent}'`].filter(Boolean).join(', ') : 'Antes del partido';
  const sel = { fav: 'el favorito', dog: 'el débil', trailer: 'el que pierde', leader: 'el que gana', original_leader: 'el que iba ganando', H: 'local', A: 'visitante', D: 'empate', O: 'más', U: 'menos', any: 'la que tenga valor', fav_or_draw: 'favorito o empate', dog_or_draw: 'débil o empate' }[m.selection] || m.selection;
  const ext = e.type === 'hedge' ? `Cubrir con ${MK[e.hedge.market] || e.hedge.market} ${e.hedge.line ?? ''} de ${({ original_leader: 'el que iba ganando', fav: 'el favorito' })[e.hedge.selection] || e.hedge.selection} si ${({ equalizer: 'empatan', goal: 'hay gol', goal_for: 'marca tu equipo' })[e.on] || e.on} (${({ zero: 'a cero', balance: 'igualando', half: 'mitad' })[e.size] || e.size})` : e.type === 'greenup' ? 'Cerrar con beneficio tras gol a favor' : 'Mantener hasta el final';
  const blocks = [['Contexto', ctx], ['Disparador', trg], ['Mercado', `${MK[m.market] || m.market}: ${sel}${m.line != null && m.line !== 'main' ? ' ' + m.line : m.line === 'main' ? ' (línea principal)' : ''}${m.line_offset ? `, +${m.line_offset} sobre los goles actuales` : ''}`],
    ['Precio', `${({ b365_open: 'bet365 apertura', b365_close: 'bet365 cierre', live: 'bet365 en directo', pin_close: 'Pinnacle cierre', max_open: 'mejor cuota' })[pr.source] || pr.source || '–'}${pr.min_odds ? `, cuota ≥ ${pr.min_odds}` : ''}${pr.max_odds ? `, ≤ ${pr.max_odds}` : ''}`],
    ['Salida', ext], ['Stake', st.type === 'kelly' ? `Kelly ${st.fraction}, tope ${F.p0(st.cap)}` : 'Fijo, 1 unidad'], ['Filtro', mf.min_edge != null ? `Ventaja ≥ ${F.p0(mf.min_edge)} frente a ${({ ens: 'el ensemble', pin_open: 'Pinnacle', secondary: 'el modelo de córners y tarjetas' })[mf.prob] || mf.prob || 'el modelo'}` : 'Sin filtro de modelo']];
  return `<div class="recipe">${blocks.map(([k, v]) => `<div><b>${k}</b><span>${esc(v)}</span></div>`).join('')}</div>`;
}
async function estrategia(id) {
  const s = await api('/api/strategies/' + id);
  const st = s.stats || {}, o = st.oos || {}, i = st.in_sample || {};
  const checks = [[(o.n || 0) >= 100, `${o.n || 0} apuestas fuera de muestra (mínimo 100)`], [(o.shrunk ?? -1) > 0, `Yield contraído ${F.pct(o.shrunk)}`], [!!st.bh_ok, `Supera la corrección por pruebas múltiples (p ajustado ${st.p_adj != null ? F.n(st.p_adj, 3) : '–'})`],
    [o.clv == null || o.clv > 0, o.clv == null ? 'CLV: no medible con estos datos' : `CLV ${F.pct(o.clv)}`], [!(o.top1pct_share > 1), 'No depende de unas pocas cuotas altas'], [!o.seasons || (o.seasons_positive || 0) >= Math.max(1, Math.floor(o.seasons / 2)), `Positiva en ${o.seasons_positive ?? '–'} de ${o.seasons ?? '–'} temporadas`]];
  view.innerHTML = `<a class="faint small" href="#/estrategias">Volver a estrategias</a>` + ph(esc(s.name), esc(s.spec.hypothesis || ''), pill(s.status)) + `
  <div class="board">
    <div><div class="k">Yield fuera de muestra ${help('oos')}</div><div class="v ${F.cls(o.yield)}">${F.pct(o.yield)}</div><div class="s">${o.n || 0} apuestas, entre ${F.pct(o.ci_lo)} y ${F.pct(o.ci_hi)}</div></div>
    <div><div class="k">Yield contraído ${help('shrunk')}</div><div class="v ${F.cls(o.shrunk)}">${F.pct(o.shrunk)}</div><div class="s">Probabilidad de ser rentable ${F.p0(o.p_profitable)}</div></div>
    <div><div class="k">En entrenamiento</div><div class="v ${F.cls(i.yield)}">${F.pct(i.yield)}</div><div class="s">${i.n || 0} apuestas, cuota media ${F.o(i.avg_odds)}</div></div>
    <div><div class="k">CLV ${help('clv')}</div><div class="v ${F.cls(o.clv)}">${F.pct(o.clv)}</div><div class="s">${o.price_source === 'simulated' ? 'Precios de directo simulados' : 'Contra el cierre de Pinnacle'}</div></div>
  </div>
  <div class="section"><h2>La receta</h2>${recipe(s.spec)}</div>
  <div class="cols c-main section"><div class="panel"><h2>Beneficio acumulado</h2><div class="chart" id="eq"></div><div class="legend"><span><i style="background:${C.value}"></i>En unidades apostadas. La zona sombreada son las temporadas fuera de muestra.</span></div></div>
    <div class="panel"><h2>¿Pasa los filtros?</h2><div class="checks">${checks.map(([ok, t]) => `<div><span class="${ok ? 'pass' : 'fail'}">${ok ? '✓' : '✕'}</span><span>${t}</span></div>`).join('')}</div>
      <h3 style="margin-top:18px">Cambiar estado</h3><div class="row">${['paper', 'live', 'retired'].map(x => `<button class="btn sm ${x === 'live' ? 'go' : ''}" data-s="${x}">${x === 'paper' ? 'Seguir en papel' : x === 'live' ? 'Pasar a dinero real' : 'Retirar'}</button>`).join('')}</div></div></div>
  <div class="cols c2 section"><div class="panel"><h2>Por temporada</h2><div class="chart" id="seasons"></div><div class="small faint">Con asterisco, temporadas fuera de muestra.</div></div>
    <div class="panel"><h2>Historial de pruebas</h2><div class="scroll"><table><tr><th>Cuándo</th><th>Prueba</th><th class="r">Apuestas</th><th class="r">Yield</th><th class="r">Contraído</th></tr>
    ${s.tests.slice(0, 12).map(t => `<tr><td class="small faint">${F.ago(t.ts)}</td><td>${({ in_sample: 'Entrenamiento', oos: 'Fuera de muestra', paper: 'Papel', manual: 'Manual' })[t.split] || t.split}</td><td class="r num" style="font-size:17px">${t.n}</td><td class="r num ${F.cls(t.yield)}" style="font-size:17px">${F.pct(t.yield)}</td><td class="r num" style="font-size:17px">${F.pct(t.shrunk)}</td></tr>`).join('')}</table></div></div></div>
  ${s.bets.length ? `<div class="section"><h2>Apuestas en papel y reales</h2><div class="panel scroll">${betsTable(s.bets)}</div></div>` : ''}
  <details class="section"><summary class="muted" style="cursor:pointer">Ver la definición técnica (JSON)</summary><textarea readonly style="margin-top:10px">${esc(JSON.stringify(s.spec, null, 2))}</textarea></details>`;
  $$('[data-s]').forEach(b => b.onclick = async () => { await api(`/api/strategies/${id}/status`, { method: 'POST', body: { status: b.dataset.s } }); toast('Estado cambiado a ' + STATUS[b.dataset.s]); route(); });
  $('#eq').innerHTML = '<div class="skeleton" style="height:200px"></div>';
  const cv = await api(`/api/strategies/${id}/curve`);
  if (cv.light) { $('#eq').innerHTML = '<div class="note">La curva de esta estrategia se calcula con años de partidos y no cabe en el servidor gratuito. Ábrela una vez en la versión del PC y quedará guardada también aquí.</div>'; $('#seasons').innerHTML = ''; return; }
  const firstOos = (cv.points.find(p => p[2]) || [])[0];
  lineChart($('#eq'), [{ name: 'Beneficio', color: C.value, points: cv.points.map(p => [p[0], p[1]]), area: true }], { zero: true, height: 250, xfmt: t => F.dshort(t), yfmt: v => v.toFixed(1) + ' u', shadeFrom: firstOos, shadeLabel: 'fuera de muestra', empty: '<br>Esta estrategia no generó apuestas con los datos actuales.' });
  barChart($('#seasons'), Object.entries(cv.seasons).map(([k, v]) => ({ label: `${k.slice(0, 2)}/${k.slice(2)}${v.oos ? '*' : ''}`, v: v.yield, dim: !v.oos })), { yfmt: v => `${(v * 100).toFixed(0)}%` });
}

/* ============================================================ LABORATORIO */
async function laboratorio() {
  const [l, j, fm] = await Promise.all([api('/api/learning'), api('/api/jobs'), api('/api/factors')]);
  const rep = l.lab || {}, t = rep.table || [];
  view.innerHTML = ph('Laboratorio', 'Aquí se inventan y se ponen a prueba las estrategias. Se ejecuta solo cada lunes con los partidos nuevos; también puedes lanzarlo ahora.', `<button class="btn go" id="runlab">Ejecutar el laboratorio</button>`) + `
  <div class="pipeline">
    <div><b>Catálogo</b>Semillas conocidas e inventadas</div><div><b>Rejilla</b>Variantes de minutos, cuotas y ligas</div><div><b>Genético</b>Cruza y muta las mejores</div><div><b>Árboles</b>Reglas que nadie escribió</div>
    <div><b>Cruce de factores</b>Todas las parejas por cada mercado</div><div><b>Fuera de muestra</b><span class="num">${rep.oos_tested ?? '–'}</span>probadas en temporadas no vistas</div><div><b>Validadas</b><span class="num">${rep.validated ?? '–'}</span>de ${rep.trials ?? '–'} variantes</div>
  </div>
  <div class="panel section" id="jobbox" style="${(j.jobs.lab || {}).log ? '' : 'display:none'}"><h3>Progreso</h3><pre class="log" id="lablog">${esc(((j.jobs.lab || {}).log || []).join('\n'))}</pre></div>
  <div class="section"><h2>Mapa de factores cruzados</h2><p class="lead">Cada pareja de factores se parte en tercios y se mide el yield real a cuota de apertura de bet365 en las 9 casillas. Las casillas fuertes se convierten en estrategias candidatas y se validan como cualquier otra.</p>
    ${fm.grid.length ? `<div class="cols c-main"><div class="panel" id="heat"></div><div class="panel pairs" id="pairs" style="max-height:500px;overflow:auto"></div></div>` : `<div class="panel">${empty('Sin mapa todavía', 'Aparece tras ejecutar el laboratorio con datos de cuotas.')}</div>`}</div>
  <div class="section"><h2>Resultados del último laboratorio</h2><p class="lead">${rep.started ? `Ejecutado ${F.ago(rep.started)}. Temporadas fuera de muestra: pre-partido ${(rep.oos_pre || []).join(', ') || '–'}, directo ${(rep.oos_live || []).join(', ') || '–'}.` : 'Aún no se ha ejecutado.'}</p>
    <div class="panel slist" style="padding:6px 0">${t.length ? slistHead() + t.slice().sort((a, b) => (b.oos_shrunk ?? -9) - (a.oos_shrunk ?? -9)).slice(0, 40).map(r => stratRow({ id: r.id, name: r.name, status: r.verdict }, { n: r.oos_n, yield: r.oos_yield, shrunk: r.oos_shrunk, p_adj: r.p_adj, clv: r.clv }, `${r.phase === 'live' ? 'Directo' : 'Pre-partido'}${r.price_source === 'simulated' ? ', precios simulados' : ''}`)).join('') : empty('Sin resultados', 'Pulsa "Ejecutar el laboratorio".')}</div></div>
  <div class="section"><h2>Probar una idea tuya</h2><p class="lead">Rellena la receta y el sistema la prueba con todo el histórico. Queda guardada como candidata y el laboratorio la validará fuera de muestra.</p>
    <div class="panel">${builderForm()}<div class="row" style="margin-top:16px"><button class="btn go" id="bt_go">Probar estrategia</button><span class="sp"></span><button class="btn ghost sm" id="bt_json">Editar en JSON</button></div>
    <textarea id="bt_spec" class="hidden" style="margin-top:12px"></textarea><div id="bt_res" style="margin-top:16px"></div></div></div>`;
  $('#runlab').onclick = async () => { try { await api('/api/jobs/lab', { method: 'POST' }); toast('Laboratorio en marcha, tarda unos minutos'); $('#jobbox').style.display = ''; poll(); } catch (e) { toast(e.message); } };
  const poll = () => timers.push(setInterval(async () => { const j2 = await api('/api/jobs'); const lab = j2.jobs.lab || {}; $('#lablog').textContent = (lab.log || []).join('\n'); if (!lab.running) { timers.forEach(clearInterval); toast('Laboratorio terminado'); route(); } }, 4000));
  if ((j.jobs.lab || {}).running) poll();
  if (fm.grid.length) {
    const names = fm.names;
    const selTxt = { H: 'gana el local', D: 'empate', A: 'gana el visitante', O: 'más de 2,5', U: 'menos de 2,5' };
    const drawHeat = k => { const g = fm.grid[k];
      const color = v => v == null ? 'var(--pitch)' : v > 0 ? `rgba(242,207,79,${Math.min(.85, .12 + v * 3)})` : `rgba(239,100,97,${Math.min(.7, .1 - v * 2.5)})`;
      $('#heat').innerHTML = `<h3>${MK[g.market]}: ${selTxt[g.selection] || g.selection}</h3><p class="small muted" style="margin-top:0">Filas: ${names[g.fa]}. Columnas: ${names[g.fb]}. Cada casilla muestra el yield real y el número de apuestas.</p>
        <div class="heat"><div></div>${[0, 1, 2].map(c => `<div class="ax">${['bajo', 'medio', 'alto'][c]}</div>`).join('')}
        ${[0, 1, 2].map(r => `<div class="ax">${names[g.fa]}<br>${['bajo', 'medio', 'alto'][r]}</div>` + [0, 1, 2].map(c => { const x = g.cells[r * 3 + c]; return `<div class="cell" style="background:${color(x ? x.yield : null)}" ${x ? `data-tip="${names[g.fa]}: ${F.n(x.a[0])} a ${F.n(x.a[1])}<br>${names[g.fb]}: ${F.n(x.b[0])} a ${F.n(x.b[1])}<br>Señal z = ${F.n(x.z, 1)}"` : ''}>${x ? `<span class="num">${F.pct(x.yield, 0)}</span><small>${x.n} apuestas</small>` : '<small>poca muestra</small>'}</div>`; }).join('')).join('')}</div>
        <p class="small faint" style="margin-top:10px">Esto usa todo el histórico y no corrige por pruebas múltiples. Solo las casillas que aguantan fuera de muestra pasan a estrategia.</p>`;
      $$('#pairs .p').forEach((p, i) => p.classList.toggle('on', i === k)); };
    $('#pairs').innerHTML = `<div class="small faint" style="padding:4px 12px 8px">Parejas con la casilla más fuerte</div>` + fm.grid.slice(0, 40).map((g, k) => `<div class="p" data-k="${k}"><span>${names[g.fa]} × ${names[g.fb]}<br><span class="faint small">${MK[g.market]}, ${selTxt[g.selection] || g.selection}</span></span><span class="num ${F.cls(g.best.yield)}">${F.pct(g.best.yield, 0)}</span></div>`).join('');
    $('#pairs').onclick = e => { const p = e.target.closest('.p'); if (p) drawHeat(+p.dataset.k); };
    drawHeat(0);
  }
  wireBuilder();
}
function builderForm() {
  return `<div class="builder">
    <div class="blk"><b>Cuándo</b><label class="field">Fase<select id="b_phase"><option value="live">En directo</option><option value="pre">Antes del partido</option></select></label>
      <label class="field">Minuto (desde-hasta)<input id="b_min" value="75-75"></label></div>
    <div class="blk"><b>Situación</b><label class="field">Va ganando<select id="b_leader"><option value="">Da igual</option><option value="dog">El débil</option><option value="fav">El favorito</option><option value="none">Nadie (empate)</option></select></label>
      <label class="field">Por cuántos goles<input id="b_margin" value="1-1"></label></div>
    <div class="blk"><b>Contexto</b><label class="field">Prob. del favorito (de-a)<input id="b_fav" placeholder="0.5-0.95"></label><label class="field">Competición<select id="b_comp"><option value="">Todas</option>${Object.entries(COMP).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></label></div>
    <div class="blk"><b>Apuesta</b><label class="field">Mercado<select id="b_mk"><option value="1X2">Resultado</option><option value="AH">Hándicap asiático</option><option value="OU">Más/menos goles</option><option value="DC">Doble oportunidad</option></select></label>
      <label class="field">A quién<select id="b_sel"><option value="trailer">El que pierde</option><option value="leader">El que gana</option><option value="fav">El favorito</option><option value="dog">El débil</option><option value="D">Empate</option><option value="O">Más goles</option><option value="U">Menos goles</option><option value="H">Local</option><option value="A">Visitante</option></select></label></div>
    <div class="blk"><b>Precio</b><label class="field">Cuota mínima<input id="b_mino" placeholder="sin mínimo"></label><label class="field">Línea (AH) o goles extra (más/menos)<input id="b_line" placeholder="0 o 0.5"></label></div>
    <div class="blk"><b>Salida</b><label class="field">Cubrirse<select id="b_exit"><option value="">No, mantener</option><option value="balance">Si empatan: AH 0.0 igualando</option><option value="zero">Si empatan: AH 0.0 a cero</option><option value="greenup">Cerrar con beneficio tras gol a favor</option></select></label></div>
  </div>`;
}
function wireBuilder() {
  const spec = () => { const ph_ = $('#b_phase').value, rng = v => v ? v.split('-').map(Number) : null;
    const s = { name: 'Mi estrategia', phase: ph_, context: {}, trigger: {}, market: { market: $('#b_mk').value, selection: $('#b_sel').value }, price: { source: ph_ === 'live' ? 'live' : 'b365_open' }, exit: { type: 'hold' }, staking: { type: 'flat', units: 1 }, model_filter: {} };
    if (ph_ === 'live') { s.trigger.minute = rng($('#b_min').value) || [60, 85]; if ($('#b_leader').value) s.trigger.leader = $('#b_leader').value; if ($('#b_margin').value) s.trigger.margin = rng($('#b_margin').value); }
    if ($('#b_fav').value) s.context.fav_prob = rng($('#b_fav').value); if ($('#b_comp').value) s.context.comps = [$('#b_comp').value];
    if ($('#b_mino').value) s.price.min_odds = parseFloat($('#b_mino').value);
    const ln = $('#b_line').value;
    if (ln !== '') { if (s.market.market === 'OU' && ph_ === 'live') s.market.line_offset = parseFloat(ln); else s.market.line = parseFloat(ln); }
    else if (s.market.market === 'AH') s.market.line = ph_ === 'live' ? 0 : 'main';
    else if (s.market.market === 'OU') { if (ph_ === 'live') s.market.line_offset = 0.5; else s.market.line = 2.5; }
    const ex = $('#b_exit').value; if (ex === 'balance' || ex === 'zero') s.exit = { type: 'hedge', on: 'equalizer', hedge: { market: 'AH', selection: 'original_leader', line: 0 }, size: ex }; else if (ex === 'greenup') s.exit = { type: 'greenup', on: 'goal_for', size: 'balance' };
    return s; };
  $('#bt_json').onclick = () => { const t = $('#bt_spec'); t.classList.toggle('hidden'); if (!t.classList.contains('hidden')) t.value = JSON.stringify(spec(), null, 2); };
  $('#bt_go').onclick = async () => {
    let sp; try { sp = $('#bt_spec').classList.contains('hidden') ? spec() : JSON.parse($('#bt_spec').value); } catch (e) { toast('El JSON no es válido'); return; }
    $('#bt_res').innerHTML = '<div class="skeleton" style="height:90px"></div>';
    try { const r = await api('/api/backtest', { method: 'POST', body: { spec: sp } }); const s = r.summary;
      $('#bt_res').innerHTML = `<div class="board"><div><div class="k">Apuestas</div><div class="v">${s.n}</div><div class="s">cuota media ${F.o(s.avg_odds)}</div></div><div><div class="k">Yield</div><div class="v ${F.cls(s.yield)}">${F.pct(s.yield)}</div><div class="s">de ${F.pct(s.ci_lo)} a ${F.pct(s.ci_hi)}</div></div><div><div class="k">Contraído</div><div class="v ${F.cls(s.shrunk)}">${F.pct(s.shrunk)}</div><div class="s">P(rentable) ${F.p0(s.p_profitable)}</div></div><div><div class="k">Precio</div><div class="v" style="font-size:28px">${s.price_source === 'simulated' ? 'Simulado' : 'Real'}</div><div class="s">${esc(r.description)}</div></div></div>
        <div class="note">Resultado con todo el histórico, dentro de muestra. Queda guardada como candidata: <a class="val" href="#/estrategia/${r.id}">ver ficha</a>. El próximo laboratorio dirá si aguanta fuera de muestra.</div>`;
    } catch (e) { $('#bt_res').innerHTML = `<div class="note">${esc(e.message)}</div>`; }
  };
}

/* ============================================================ APUESTAS */
function betsTable(bs) {
  const stCls = { won: 'win', halfwon: 'win', lost: 'loss', halflost: 'loss' };
  const stTxt = { open: 'Abierta', won: 'Ganada', lost: 'Perdida', push: 'Devuelta', halfwon: 'Media ganada', halflost: 'Media perdida', void: 'Anulada' };
  return `<table><tr><th>Partido</th><th>Apuesta</th><th class="r">Cuota</th><th class="r">Stake</th><th>Estado</th><th class="r">Resultado</th><th class="r">CLV</th><th></th></tr>
  ${bs.map(b => `<tr><td>${esc((b.home_name || '') + (b.away_name ? ' – ' + b.away_name : ''))}<div class="faint" style="font-size:12px">${esc(b.strategy || '')}${b.minute != null ? `, minuto ${Math.floor(b.minute)}` : ''}</div></td>
  <td>${betLabel(b)}${b.base_h || b.base_a ? ` <span class="faint">(desde ${b.base_h}-${b.base_a})</span>` : ''}<div><span class="tag">${b.mode === 'real' ? 'real' : b.mode === 'watch' ? 'vigilada' : 'papel'}</span></div></td>
  <td class="r num" style="font-size:17px">${F.o(b.odds)}</td><td class="r num" style="font-size:17px">${F.n(b.stake)}</td><td class="${stCls[b.status] || ''}">${stTxt[b.status] || b.status}</td>
  <td class="r num ${F.cls(b.pnl)}" style="font-size:17px">${b.pnl != null ? F.eur(b.pnl) : ''}</td><td class="r num ${F.cls(b.clv)}">${F.pct(b.clv)}</td>
  <td>${b.mode !== 'real' && b.status === 'open' ? `<button class="btn sm" onclick="takeBet(${b.id},${b.odds},${b.stake})">La hice</button>` : ''}
    ${b.mode === 'real' && b.status === 'open' ? `<div class="row" style="gap:4px"><button class="btn sm" onclick="setRes(${b.id},'won')">Ganada</button><button class="btn sm" onclick="setRes(${b.id},'lost')">Perdida</button><button class="btn sm" onclick="setRes(${b.id},'void')">Nula</button></div>` : ''}</td></tr>`).join('')}</table>`;
}
function betLabel(b) {
  try { const n = JSON.parse(b.notes || '{}'); if (n && n.label) return esc(n.label); } catch (e) {}
  const mk = String(b.market || '');
  return `${MK[mk] || esc(mk.replace('RAW:', ''))} ${esc(b.selection)}${b.line != null ? ' ' + b.line : ''}`;
}
window.setRes = async (id, r) => { await api(`/api/bets/${id}/result`, { method: 'POST', body: { result: r } }); toast('Apuesta liquidada'); route(); };
window.takeBet = async (id, odds, stake) => { const o = prompt('¿A qué cuota te la dio bet365?', odds); if (!o) return; const s = prompt('¿Cuánto apostaste?', stake); if (!s) return;
  await api(`/api/bets/${id}/taken`, { method: 'POST', body: { odds: parseFloat(o), stake: parseFloat(s) } }); toast('Guardada como apuesta real'); route(); };
async function apuestas() {
  view.innerHTML = ph('Mis apuestas', 'Las que marcas con «La he hecho» quedan aquí como reales: las de goles se liquidan solas al acabar el partido; las de jugadores y especiales también, cuando hay datos; si alguna no se liquida sola la marcas tú (Ganada, Perdida o Nula). Las de papel y vigiladas las crea el sistema para medir sus estrategias.') +
    `<div class="seg" id="bt"><button data-m="">Todas</button><button class="on" data-m="real">Mías (reales)</button><button data-m="paper">Papel</button><button data-m="watch">Vigiladas</button><button data-m="import">Importar CSV</button></div><div id="b_body" style="margin-top:18px"></div>`;
  const draw = async m => {
    if (m === 'import') { $('#b_body').innerHTML = `<div class="panel"><p class="muted" style="margin-top:0">Columnas: fecha (dd/mm/aaaa), local, visitante, mercado (1X2, AH, OU, BTTS…), seleccion (H, D, A, O, U…), linea, cuota, stake.</p>
      <textarea id="csv">fecha,local,visitante,mercado,seleccion,linea,cuota,stake\n01/11/2026,Liverpool,Arsenal,1X2,A,,2.40,10</textarea><button class="btn go" style="margin-top:12px" id="imp">Importar apuestas</button><div id="impres" style="margin-top:12px"></div></div>`;
      $('#imp').onclick = async () => { const r = await api('/api/bets/import', { method: 'POST', body: { csv: $('#csv').value } }); $('#impres').innerHTML = `<div class="note">Importadas ${r.imported}. ${r.rejected.length ? `No encontradas: ${r.rejected.map(x => esc(x.local + '-' + x.visitante + ' (' + x.error + ')')).join(', ')}` : ''}</div>`; }; return; }
    const bs = await api('/api/bets?limit=400' + (m ? '&mode=' + m : ''));
    $('#b_body').innerHTML = `<div class="panel scroll">${bs.length ? betsTable(bs) : empty('Sin apuestas todavía', 'Aparecerán cuando las estrategias encuentren valor en partidos reales.')}</div>`;
  };
  segWire('bt', b => draw(b.dataset.m));
  draw('real');
}

/* ============================================================ APRENDIZAJE */
async function aprendizaje() {
  const l = await api('/api/learning');
  const cal = l.calibration || {}, ip = l.inplay_params || {};
  const P = [['trail1', 'Quien pierde por 1 aprieta', 1.12], ['trail2', 'Quien pierde por 2 o más', 1.05], ['lead1', 'Quien gana por 1 se cierra', .92], ['lead2', 'Quien gana por 2 o más', .90], ['red_self', 'Jugar con 10', .68], ['red_opp', 'Rival con 10', 1.28], ['ht_bump', 'Añadido de la 1ª parte', 1.9], ['ft_bump', 'Añadido final', 1.35], ['slope', 'Más goles al final', .35]];
  view.innerHTML = ph('Aprendizaje', 'Cómo de bien acierta el sistema y qué ha aprendido de los partidos jugados. Se reentrena solo: a diario los modelos pre-partido y cada lunes el modelo en directo y la búsqueda de estrategias.') + `
  <div class="cols c2"><div class="panel"><h2>¿Las probabilidades son fiables?</h2><p class="lead">Si el modelo dice 30 %, eso debería pasar 3 de cada 10 veces. Cuanto más pegados a la diagonal, mejor.</p><div class="chart" id="cal"></div>
    <div class="small faint">${cal.n ? `${cal.n} probabilidades, Brier ${cal.brier}` : ''}</div></div>
    <div class="panel"><h2>Lo que ha aprendido del directo</h2><p class="lead">Multiplicadores de la intensidad de gol que el modelo calibra con las cronologías reales. 1,00 sería "no influye".</p>
      ${P.map(([k, t, def]) => { const v = ip[k] ?? def, w = Math.min(100, v / 2 * 100); return `<div style="display:grid;grid-template-columns:180px 1fr 54px;gap:10px;align-items:center;margin:9px 0"><span class="small">${t}</span><div style="position:relative;height:10px;background:var(--pitch);border-radius:5px"><div style="position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--line2)"></div><div style="height:100%;width:${w}%;background:${v >= 1 ? C.value : C.info};border-radius:5px"></div></div><span class="num r" style="font-size:18px">${F.n(v)}</span></div>`; }).join('')}
      <div class="small faint">${l.inplay_info ? `Calibrado con ${l.inplay_info.n} partidos.` : 'Aún con valores por defecto: se calibra en el primer laboratorio.'}</div></div></div>
  <div class="cols c2 section"><div class="panel"><h2>Registro de pruebas</h2><div class="chips">${chip('Pruebas registradas', l.tests_total)}${chip('Último laboratorio', l.lab && l.lab.started ? F.ago(l.lab.started) : 'nunca')}${chip('Variantes', (l.lab && l.lab.trials) || 0)}</div>
    <p class="small muted">Se guardan todas, también las descartadas: es lo que permite corregir los falsos positivos de la búsqueda masiva.</p></div>
    <div class="panel"><h2>Informe semanal</h2>${l.weekly ? `<pre class="log">${esc(l.weekly.text)}</pre>` : '<p class="muted">Llega cada lunes, también por Telegram.</p>'}</div></div>`;
  if (cal.n) calibChart($('#cal'), cal.bins); else $('#cal').innerHTML = empty('Sin datos', 'Aparece cuando haya predicciones con resultado.');
}

/* ============================================================ GUÍA */
async function guia() {
  const items = [['Cuota justa', TIPS.fair], ['Cuota mínima', TIPS.minodds], ['Valor', 'La diferencia entre la cuota que paga bet365 y la justa. +5 % significa que, a la larga, cada 100 € apostados deberían dejar 5 €.'], ['Yield', 'Beneficio dividido entre lo apostado. Es la medida de rentabilidad.'],
    ['Yield contraído', TIPS.shrunk], ['Fuera de muestra', TIPS.oos], ['p ajustado', TIPS.padj], ['CLV', TIPS.clv], ['Papel', 'Apuestas que el sistema registra con partidos reales sin dinero. Una estrategia necesita unas 150 en papel antes de proponerte dinero real.'],
    ['Vigiladas', TIPS.watch], ['Impacto del jugador', TIPS.impact], ['Duelos', 'Se empareja el extremo con el lateral que le toca, el delantero con los centrales y el mediapunta con los pivotes, y se compara lo que hace cada uno por 90 minutos.'],
    ['Precio simulado', 'Para el directo no existe un histórico gratis de cuotas de bet365. Se simula con un modelo estándar más el margen típico; por eso nada de directo pasa a dinero real sin validarse antes en papel.'], ['Cobertura', 'Una segunda apuesta para reducir el riesgo. No crea valor: el valor está en haber entrado a buena cuota.']];
  view.innerHTML = ph('Cómo funciona', 'Qué significa cada cifra y cómo usar edgelab en el día a día.') + `
  <div class="cols c3"><div class="panel"><h2>Antes del partido</h2><p class="muted">Mira <a class="val" href="#/partidos">Partidos</a>. Abre el partido para ver la lectura, los factores, las alineaciones y duelos, el cara a cara y las cuotas. Si hay pick, apuesta solo si bet365 paga la cuota mínima o más.</p></div>
    <div class="panel"><h2>Durante el partido</h2><p class="muted">Ten <a class="val" href="#/directo">Directo</a> abierto o activa Telegram. Cada señal dice a qué apostar y la cuota mínima. Si luego toca cubrirse, llega un aviso con el stake según la cuota que veas.</p></div>
    <div class="panel"><h2>Después</h2><p class="muted">No tienes que hacer nada: se liquida solo. Si apostaste de verdad, pulsa "La hice" en <a class="val" href="#/apuestas">Apuestas</a> para que mida tu rendimiento real.</p></div></div>
  <div class="note section">Objetivo realista: en mercados principales, ganar al cierre de Pinnacle de forma sostenida ya es un éxito. Un yield del +30 % mantenido no es realista en mercados líquidos; donde más margen hay es en mercados de jugador, tarjetas y córners, y ahí bet365 limita antes.</div>
  <div class="section"><h2>Glosario</h2><div class="gloss">${items.map(([k, v]) => `<div><b>${k}</b><p>${v}</p></div>`).join('')}</div></div>`;
}

function railFoot() { $('#railFoot').innerHTML = `<a href="#/ajustes">Tu banca<span class="num">${USER.bankroll} €</span>Perfil ${USER.risk < 0.8 ? 'prudente' : USER.risk > 1.2 ? 'agresivo' : 'normal'} · cambiar</a>`; }

/* ============================================================ HISTORIAL */
async function historial() {
  const t = await api('/api/track?days=180');
  const a = t.all || {};
  const row = (k, x) => `<tr><td>${esc(k)}</td><td class="r num">${x.n}</td><td class="r num">${x.n ? F.p0(x.won / x.n) : '–'}</td><td class="r num">${F.o(x.avg_odds)}</td><td class="r num ${F.cls(x.profit)}">${F.eur(x.profit)}</td><td class="r num ${F.cls(x.yield)}">${F.pct(x.yield)}</td><td class="r num ${F.cls(x.clv)}">${F.pct(x.clv)}</td></tr>`;
  const tbl = (title, obj) => Object.keys(obj).length ? `<div class="panel"><h3>${title}</h3><table><tr><th></th><th class="r">Apuestas</th><th class="r">Acierto</th><th class="r">Cuota media</th><th class="r">Beneficio</th><th class="r">Yield</th><th class="r">CLV ${help('clv')}</th></tr>${Object.entries(obj).sort((x, y) => y[1].n - x[1].n).map(([k, v]) => row(GROUP_NAME[k] || k, v)).join('')}</table></div>` : '';
  const stTxt = { open: 'Pendiente', won: 'Ganada', lost: 'Perdida', push: 'Devuelta', halfwon: 'Media ganada', halflost: 'Media perdida', void: 'Anulada' };
  view.innerHTML = ph('Historial de recomendaciones', 'Cada apuesta que la web recomienda queda apuntada sola, a la cuota de bet365 de ese momento y con 10 € fijos, y se liquida sola al acabar el partido. Es la prueba honesta de si esto gana dinero.') + `
  <div class="board"><div><div class="k">Recomendaciones liquidadas</div><div class="v">${a.n || 0}</div><div class="s">${t.open} pendientes de jugarse</div></div>
    <div><div class="k">Beneficio (10 € cada una)</div><div class="v ${F.cls(a.profit)}">${F.eur(a.profit)}</div><div class="s">acierto ${a.n ? F.p0(a.won / a.n) : '–'}, cuota media ${F.o(a.avg_odds)}</div></div>
    <div><div class="k">Yield</div><div class="v ${F.cls(a.yield)}">${F.pct(a.yield)}</div><div class="s">beneficio ÷ dinero apostado</div></div>
    <div><div class="k">Mejor que el cierre ${help('clv')}</div><div class="v">${a.beat_close != null ? F.p0(a.beat_close) : '–'}</div><div class="s">CLV medio ${F.pct(a.clv)}</div></div></div>
  ${(a.n || 0) < 100 ? `<div class="note warn">Con menos de 100 apuestas liquidadas el resultado es sobre todo suerte. El CLV (si cogemos mejor cuota que la de cierre) es lo primero que indica si hay ventaja de verdad.</div>` : ''}
  <div class="section panel chart" id="tcurve"></div>
  <div class="cols c2 section">${tbl('Por tipo de mercado', t.by_group)}${tbl('Por cuota', t.by_band)}</div>
  <div class="section">${tbl('Por fiabilidad', t.by_conf)}</div>
  <h2 class="section">Últimas recomendaciones</h2>
  <div class="panel scroll"><table><tr><th>Partido</th><th>Apuesta</th><th class="r">Cuota</th><th class="r">Valor</th><th>Estado</th><th class="r">Resultado</th><th class="r">CLV</th></tr>
  ${t.recent.map(r => `<tr><td><a href="#/partido/${r.match_id}">${esc(r.match)}</a><div class="faint small">${F.dshort(r.kickoff)}</div></td><td>${esc(r.label || '')}</td><td class="r num">${F.o(r.odds)}</td><td class="r">${evBadge(r.ev)}</td>
    <td class="${r.status === 'won' || r.status === 'halfwon' ? 'win' : r.status === 'lost' || r.status === 'halflost' ? 'loss' : 'faint'}">${stTxt[r.status] || r.status}</td><td class="r num ${F.cls(r.pnl)}">${r.pnl != null ? F.eur(r.pnl) : ''}</td><td class="r num ${F.cls(r.clv)}">${F.pct(r.clv)}</td></tr>`).join('') || `<tr><td colspan="7">${empty('Todavía no hay recomendaciones apuntadas', 'Se apuntan solas cada 15 minutos con las que salen en Oportunidades.')}</td></tr>`}</table></div>`;
  lineChart($('#tcurve'), [{ name: 'Beneficio acumulado', color: C.win, points: t.curve, area: true }], { zero: true, height: 220, xfmt: x => F.dshort(x), yfmt: v => `${v.toFixed(0)} €`, empty: '<br>La curva aparece cuando se liquiden las primeras recomendaciones.' });
}

/* ============================================================ AJUSTES */
async function ajustes() {
  USER = { ...USER, ...(await api('/api/settings')) };
  view.innerHTML = ph('Ajustes', 'Tu banca y cuánto riesgo quieres asumir. Todos los stakes de la web se recalculan con esto.') + `
  <div class="cols c2"><div class="panel">
    <label class="field">Banca (el dinero que dedicas a apostar, en €)<input id="s_bank" type="number" min="10" step="10" value="${USER.bankroll}"></label>
    <h3 style="margin-top:18px">Perfil de riesgo</h3>
    <div class="seg" id="s_risk">${[[0.5, 'Prudente'], [1, 'Normal'], [1.5, 'Agresivo']].map(([v, t]) => `<button data-v="${v}" class="${Math.abs(USER.risk - v) < 0.01 ? 'on' : ''}">${t}</button>`).join('')}</div>
    <p class="small muted">Normal = ¼ de Kelly (máximo 3 % de la banca por apuesta; 1 % en jugadores). Prudente apuesta la mitad; agresivo, 1,5 veces.</p>
    <h3 style="margin-top:18px">Qué recomendaciones ver</h3>
    <div class="seg" id="s_conf"><button data-v="media" class="${USER.min_conf !== 'alta' ? 'on' : ''}">Fiabilidad media y alta</button><button data-v="alta" class="${USER.min_conf === 'alta' ? 'on' : ''}">Solo alta</button></div>
    <div class="seg" id="s_pl" style="margin-top:8px"><button data-v="1" class="${USER.show_players ? 'on' : ''}">Con apuestas de jugador</button><button data-v="0" class="${!USER.show_players ? 'on' : ''}">Sin jugadores</button></div>
    <div class="row" style="margin-top:18px"><button class="btn go" id="s_save">Guardar</button></div>
  </div><div class="panel"><h2>Cómo se calcula el stake</h2><p class="muted">Kelly dice qué parte de la banca apostar según la ventaja y la cuota: más ventaja y menos cuota, más stake. Usar una fracción (¼) reduce mucho las rachas malas a cambio de crecer algo más despacio. Con banca de ${USER.bankroll} € y perfil normal, una apuesta con un 5 % de valor a cuota 2,00 serían unos ${Math.round(USER.bankroll * 0.25 * 0.05 / 1 * 2) / 2} €.</p>
  <p class="muted">Consejo: no metas en la banca dinero que necesites. Apuesta siempre la misma proporción y mide el resultado en Mis apuestas tras 100 o más apuestas, no tras 5.</p></div></div>`;
  let risk = USER.risk, conf = USER.min_conf, pl = USER.show_players;
  segWire('s_risk', b => { risk = +b.dataset.v; }); segWire('s_conf', b => { conf = b.dataset.v; }); segWire('s_pl', b => { pl = b.dataset.v === '1'; });
  $('#s_save').onclick = async () => { USER = await api('/api/settings', { method: 'POST', body: { bankroll: +$('#s_bank').value, risk, min_conf: conf, show_players: pl } }); railFoot(); toast('Guardado: los stakes ya usan tu banca'); };
}

/* arranque */
(async () => {
  try { meta = await api('/api/meta'); USER = { ...USER, ...(meta.user || {}) }; window.TEAM_IDS = meta.teams || {}; railFoot(); } catch (e) {}
  route();
})();
