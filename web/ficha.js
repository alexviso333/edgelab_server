/* edgelab — ficha de apuesta (el análisis completo de una apuesta) y análisis de partido */
const betHref = (mid, r) => `#/apuesta/${mid}?m=${encodeURIComponent(r.market)}&p=${encodeURIComponent(r.period || 'FT')}&s=${encodeURIComponent(r.selection)}&l=${r.line == null ? '' : r.line}`;
const fx = (v, d = 1) => v == null || isNaN(v) ? '–' : Number(v).toFixed(d).replace('.', ',').replace(/,0+$/, '');
const VERDICT_ICON = { good: 'M5 12l4 4 10-10', warn: 'M12 7v6M12 17h.01', bad: 'M7 7l10 10M17 7L7 17' };

async function apuesta(mid) {
  const q = new URLSearchParams(location.hash.split('?')[1] || '');
  const qs = new URLSearchParams({ market: q.get('m'), period: q.get('p') || 'FT', selection: q.get('s') || '' });
  if (q.get('l')) qs.set('line', q.get('l'));
  const f = await api(`/api/match/${mid}/bet?${qs}`);
  window.__F = f;
  const r = f.row, m = f.match;
  const stake = stakeOf(r);
  const pro = f.signals.filter(s => s.dir > 0), con = f.signals.filter(s => s.dir < 0), neu = f.signals.filter(s => !s.dir);
  const sc = Math.round(f.score * 100);
  const sim = f.sim;
  view.innerHTML = `
  <div class="row crumbs"><a class="faint small" href="#/partido/${mid}?t=apuestas">← ${esc(m.home_name)} – ${esc(m.away_name)}</a><span class="sp"></span>
    <span class="faint small">${F.day(m.kickoff)}, ${F.hm(m.kickoff)} · ${COMP[m.comp] || m.comp || ''}</span></div>
  <section class="dossier">
    <div class="dz-head">
      <div class="dz-title">
        <span class="tag">${GROUP_NAME[r.group] || r.group}</span>
        <h1>${esc(r.label)}</h1>
        <p class="muted">${f.type === 'player' ? `${esc(f.player.name || '')} · ${esc(f.player.team)}${f.player.pos ? ` · ${esc(f.player.pos)}` : ''}` : `${esc(m.home_name)} contra ${esc(m.away_name)}`}</p>
      </div>
      <div class="stamp ${f.tone}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="${VERDICT_ICON[f.tone]}"/></svg>
        <div><b>${esc(f.verdict)}</b><span>${pro.length} señales a favor, ${con.length} en contra</span></div>
      </div>
    </div>
    <div class="dz-figs">
      <div><small>Cuota bet365</small><span class="num">${F.o(r.b365)}</span>${r.b365_open && r.b365_open !== r.b365 ? `<em>abrió a ${F.o(r.b365_open)}</em>` : '<em>&nbsp;</em>'}</div>
      <div><small>Cuota justa</small><span class="num">${F.o(r.fair)}</span><em>entra si es ≥ ${F.o(r.fair ? r.fair * 1.02 : null)}</em></div>
      <div><small>Pasa</small><span class="num">${F.p0(r.p)}</span><em>bet365 cree ${F.p0(r.b365 ? 1 / r.b365 : null)}</em></div>
      <div><small>Valor</small><span class="num ${r.ev > 0 ? 'win' : 'loss'}">${F.pct(r.ev)}</span><em>${r.ev != null ? `${F.eur(r.ev * 10)} por cada 10 €` : ''}</em></div>
      <div><small>Apuesta</small><span class="num">${stake && r.ev > 0.02 ? stake + ' €' : '–'}</span><em>${stake && r.ev > 0.02 ? `${(stake / USER.bankroll * 100).toFixed(1).replace('.', ',')} % de tu banca` : 'no entrar'}</em></div>
    </div>
    <div class="meter" title="Peso de las señales a favor">
      <div class="meter-bar"><i style="width:${sc}%"></i></div>
      <div class="row small"><span>A favor ${sc} %</span><span class="sp"></span><span class="faint">${f.n_pro + f.n_con + neu.length} datos cruzados</span></div>
    </div>
  </section>

  <div class="cols c-main section">
    <div>
      <article class="panel analysis">
        <h2>El análisis</h2>
        ${f.text.map(t => `<p>${esc(t)}</p>`).join('')}
        <div class="row" style="margin-top:14px">${r.ev > 0 ? `<button class="btn go" id="madeIt">La he hecho</button>` : ''}<a class="btn" href="#/partido/${mid}?t=analisis">Análisis del partido</a></div>
      </article>
      ${histBlock(f)}
      ${simBlock(f)}
      ${f.type === 'player' ? pitchBlock(f) : ''}
    </div>
    <aside>
      <div class="panel">
        <h2>Señales</h2>
        <ul class="signals">${[...pro, ...con, ...neu].map(s => `<li class="${s.dir > 0 ? 'up' : s.dir < 0 ? 'down' : 'flat'}"><i></i><span>${esc(s.text)}</span></li>`).join('')}</ul>
        <p class="faint small" style="margin:12px 0 0">Verde empuja a favor de la apuesta, rojo en contra, gris es neutro. Pesan más el precio, la racha y el duelo directo.</p>
      </div>
      ${rivalBlock(f)}
      ${refBlock(f)}
      ${vsOppBlock(f)}
    </aside>
  </div>`;
  const b = $('#madeIt');
  if (b) b.onclick = () => { const i = REC.push({ ...r, match: m }) - 1; madeBet(i); };
  const pb = $('#loadPasses');
  if (pb) pb.onclick = async () => {
    pb.disabled = true; pb.textContent = 'Trayendo sus pases…';
    try {
      const a = await api(`/api/player/${f.player.id}/actions`, { keep: true });
      drawActions(a);
      pb.textContent = a.matches.length ? `Pases y conducciones de sus últimos ${a.matches.length} partidos` : 'No hay mapa de pases publicado';
    } catch (e) { pb.textContent = 'No se pudo traer'; }
  };
}

function histBlock(f) {
  if (f.type === 'player' && f.history && f.history.length) {
    const h = [...f.history].reverse();
    const mx = Math.max(f.line + 1, ...h.map(x => x.v));
    return `<div class="panel section"><div class="row"><h2 style="margin:0">Sus últimos ${h.length} partidos</h2><span class="sp"></span><span class="badge ${f.n_hit / f.n_hist >= .6 ? 'b-value' : 'b-neutral'}">${f.n_hit} de ${f.n_hist}</span></div>
      <p class="lead" style="margin:6px 0 12px">${esc(f.player.stat_word)} en cada partido. La raya es la línea de la apuesta (${fx(f.line)}); en verde, los partidos en que habría pasado.</p>
      ${barsLine(h.map(x => ({ v: x.v, hit: x.hit, lab: (x.home ? 'vs ' : 'en ') + x.opp, sub: `${Math.round(x.min)}'${x.starter ? '' : ' (supl.)'}` })), f.line, mx)}
    </div>`;
  }
  if (f.type === 'count' && f.home_series) {
    const side = f.side;
    const one = (ser, name) => {
      const h = [...ser].reverse().filter(x => x.for != null);
      if (!h.length) return '';
      const vals = h.map(x => side ? x.for : x.for + x.against);
      const mx = Math.max(f.line + 1, ...vals);
      return `<h3 style="margin-top:14px">${esc(name)}</h3>${barsLine(h.map((x, i) => ({ v: vals[i], hit: f.over ? vals[i] > f.line : vals[i] < f.line, lab: (x.home ? 'vs ' : 'en ') + x.opp, sub: side ? `${fx(x.against, 0)} en contra` : `${fx(x.for, 0)}+${fx(x.against, 0)}` })), f.line, mx)}`;
    };
    const m = f.match;
    return `<div class="panel section"><div class="row"><h2 style="margin:0">${esc(f.word[0].toUpperCase() + f.word.slice(1))} en sus últimos partidos</h2><span class="sp"></span>${f.n_hist ? `<span class="badge ${f.n_hit / f.n_hist >= .6 ? 'b-value' : 'b-neutral'}">${f.n_hit} de ${f.n_hist}</span>` : ''}</div>
      <p class="lead" style="margin:6px 0 0">${side ? 'Lo que hizo el equipo en cada partido' : 'Total del partido (los dos equipos)'}; la raya es la línea (${fx(f.line)}).</p>
      ${!side || side === 'H' ? one(f.home_series, m.home_name) : ''}${!side || side === 'A' ? one(f.away_series, m.away_name) : ''}</div>`;
  }
  if (f.type === 'goals' && f.home_series) {
    const m = f.match;
    const one = (ser, name) => {
      const h = [...ser].reverse();
      return `<h3 style="margin-top:14px">${esc(name)}</h3><div class="gstrip">${h.map(x => { const res = x.gf > x.ga ? 'w' : x.gf < x.ga ? 'l' : 'd';
        return `<div class="g ${res}" title="${x.home ? 'vs' : 'en'} ${esc(x.opp)}"><b class="num">${x.gf}-${x.ga}</b><small>${esc((x.opp || '').slice(0, 10))}</small><em>xG ${fx(x.xg, 1)}–${fx(x.xga, 1)}</em></div>`; }).join('')}</div>`;
    };
    return `<div class="panel section"><div class="row"><h2 style="margin:0">Últimos resultados</h2><span class="sp"></span>${f.n_hist ? `<span class="badge ${f.n_hit / f.n_hist >= .6 ? 'b-value' : 'b-neutral'}">habría pasado ${f.n_hit} de ${f.n_hist}</span>` : ''}</div>
      ${one(f.home_series, m.home_name)}${one(f.away_series, m.away_name)}</div>`;
  }
  return '';
}

function barsLine(items, line, mx) {
  const H = 120;
  return `<div class="bline" style="--n:${items.length}"><div class="bl-line" style="bottom:${(line / mx) * H + 33}px"><span>${fx(line)}</span></div>
    ${items.map(x => `<div class="bl ${x.hit ? 'hit' : ''}"><div class="bl-col"><i style="height:${Math.max(2, x.v / mx * H)}px"></i><b class="num">${fx(x.v, x.v % 1 ? 1 : 0)}</b></div><small title="${esc(x.lab)}">${esc(x.lab)}</small><em>${esc(x.sub || '')}</em></div>`).join('')}</div>`;
}

function simBlock(f) {
  const s = f.sim;
  if (!s || !s.hist) return '';
  const tot = s.hist.reduce((a, b) => a + b, 0) || 1;
  const mx = Math.max(...s.hist);
  const isGoals = f.type === 'goals';
  const L = isGoals ? null : s.line;
  const cols = s.hist.map((c, k) => {
    const hit = L == null ? null : (s.over ? k > L : k < L);
    return `<div class="sh ${hit === true ? 'hit' : hit === false ? 'miss' : ''}"><i style="height:${c / mx * 100}%"></i><small>${k === s.hist.length - 1 && !isGoals ? k + '+' : k}</small><em>${c / tot >= .005 ? Math.round(c / tot * 100) + '%' : ''}</em></div>`;
  }).join('');
  return `<div class="panel section simp"><div class="row"><h2 style="margin:0">${(s.n).toLocaleString('es-ES')} simulaciones</h2><span class="sp"></span><span class="big num">${F.p0(s.p)}</span></div>
    <p class="lead" style="margin:6px 0 12px">${isGoals ? 'Goles totales en cada partido simulado (minuto a minuto, con el efecto del marcador).' : `Cuántas veces sale cada cifra; en verde, las que hacen ganar la apuesta. Media ${fx(s.mean, 2)}.`}</p>
    <div class="shist">${cols}</div>
    ${s.scores ? `<div class="chips" style="margin-top:14px">${s.scores.map(([k, v]) => chip(k, F.p0(v))).join('')}</div>` : ''}
    ${s.home_win != null ? `<div class="tri"><div style="flex:${s.home_win}"><b>${F.p0(s.home_win)}</b> ${esc(f.match.home_name)}</div><div style="flex:${s.draw}"><b>${F.p0(s.draw)}</b> empate</div><div style="flex:${s.away_win}"><b>${F.p0(s.away_win)}</b> ${esc(f.match.away_name)}</div></div>` : ''}
  </div>`;
}

/* campo: 105 × 68, el equipo del jugador ataca hacia la derecha; y=0 de los datos es su banda derecha (abajo) */
function pitchSVG2(inner) {
  return `<svg class="pitch2" viewBox="-2 -2 109 72" preserveAspectRatio="xMidYMid meet">
    <rect x="0" y="0" width="105" height="68" class="turf"/>
    ${[...Array(7)].map((_, i) => `<rect x="${i * 15}" y="0" width="7.5" height="68" class="stripe"/>`).join('')}
    <g class="lines"><rect x="0" y="0" width="105" height="68"/><line x1="52.5" y1="0" x2="52.5" y2="68"/><circle cx="52.5" cy="34" r="9.15"/>
    <rect x="0" y="13.84" width="16.5" height="40.32"/><rect x="88.5" y="13.84" width="16.5" height="40.32"/><rect x="0" y="24.84" width="5.5" height="18.32"/><rect x="99.5" y="24.84" width="5.5" height="18.32"/></g>
    ${inner}<text x="100" y="-0.4" class="dir">ataca →</text></svg>`;
}
const PX = x => x / 100 * 105, PY = y => 68 - y / 100 * 68;
function heatCells(grid, cls) {
  if (!grid) return '';
  const GY = grid.length, GX = grid[0].length;
  const mx = Math.max(...grid.flat()) || 1;
  let s = '';
  grid.forEach((row, gy) => row.forEach((v, gx) => {
    if (v <= 0) return;
    s += `<rect class="${cls}" x="${gx * 105 / GX}" y="${68 - (gy + 1) * 68 / GY}" width="${105 / GX}" height="${68 / GY}" style="opacity:${(0.12 + 0.8 * v / mx).toFixed(2)}"/>`;
  }));
  return s;
}
function pitchBlock(f) {
  const h = f.heat;
  const rv = (f.rivals || [])[0];
  if (!h) return `<div class="panel section"><h2>En el campo</h2><p class="muted small" style="margin:0">Todavía no hay mapas de calor de sus últimos partidos: el planificador los va trayendo cada 45 minutos.</p><div class="row" style="margin-top:12px"><button class="btn sm" id="loadPasses">Ver sus pases de los últimos partidos</button></div><div id="actmap"></div></div>`;
  const rg = rv && rv.heat ? rv.heat.grid.map(r => [...r].reverse()).reverse() : null;
  const dot = (x, y, cls, lab) => x == null ? '' : `<circle cx="${PX(x)}" cy="${PY(y)}" r="2.2" class="${cls}"/><text x="${PX(x) + 3}" y="${PY(y) + 1.2}" class="lab">${esc(lab)}</text>`;
  const rx = rv && rv.heat ? 100 - rv.heat.avg_x : null, ry = rv && rv.heat ? 100 - rv.heat.avg_y : null;
  return `<div class="panel section"><div class="row"><h2 style="margin:0">En el campo</h2><span class="sp"></span><span class="faint small">${h.n_matches} partidos</span></div>
    <p class="lead" style="margin:6px 0 12px">Dónde se mueve ${esc(f.player.name || '')} (verde)${rv && rv.heat ? ` y su par, ${esc(rv.name)} (rojo), visto desde el mismo lado del campo` : ''}. ${rv && rv.overlap != null ? `Comparten el <b>${Math.round(rv.overlap * 100)} %</b> del terreno.` : ''}</p>
    <div id="pitchwrap">${pitchSVG2(heatCells(h.grid, 'hm') + (rg ? heatCells(rg, 'hr') : '') + dot(h.avg_x, h.avg_y, 'me', (f.player.name || '').split(' ').slice(-1)[0]) + (rx != null ? dot(rx, ry, 'riv', (rv.name || '').split(' ').slice(-1)[0]) : ''))}</div>
    ${h.recent_x != null && h.older_x != null ? `<p class="small muted" style="margin:10px 0 0">Posición media: ${Math.round(h.older_x)} de 100 en los partidos anteriores, ${Math.round(h.recent_x)} en los tres últimos (100 = línea de fondo rival).</p>` : ''}
    <div class="row" style="margin-top:12px"><button class="btn sm" id="loadPasses">Ver sus pases y conducciones</button></div><div id="actmap"></div></div>`;
}
function drawActions(a) {
  const el = $('#actmap'); if (!el) return;
  if (!a.matches.length) { el.innerHTML = ''; return; }
  const arrows = [];
  a.matches.forEach(mm => {
    mm.passes.forEach(p => { if (p.a && p.b) arrows.push(`<line x1="${PX(p.a.x)}" y1="${PY(p.a.y)}" x2="${PX(p.b.x)}" y2="${PY(p.b.y)}" class="${p.key ? 'pk' : p.ok === false ? 'pf' : 'pp'}" marker-end="url(#ah)"/>`); });
    mm.carries.forEach(p => { if (p.a && p.b) arrows.push(`<line x1="${PX(p.a.x)}" y1="${PY(p.a.y)}" x2="${PX(p.b.x)}" y2="${PY(p.b.y)}" class="cr"/>`); });
    mm.defensive.forEach(p => { if (p.a) arrows.push(`<circle cx="${PX(p.a.x)}" cy="${PY(p.a.y)}" r="1.1" class="df"/>`); });
  });
  const np = a.matches.reduce((s, m) => s + m.passes.length, 0), nk = a.matches.reduce((s, m) => s + m.passes.filter(p => p.key).length, 0);
  el.innerHTML = `<div style="margin-top:14px">${pitchSVG2('<defs><marker id="ah" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="3" markerHeight="3" orient="auto"><path d="M0 0L6 3L0 6z" class="ahp"/></marker></defs>' + arrows.join(''))}
    <div class="legend small"><span><i class="lg-pp"></i>pase (${np})</span><span><i class="lg-pk"></i>pase clave (${nk})</span><span><i class="lg-cr"></i>conducción</span><span><i class="lg-df"></i>acción defensiva</span></div>
    <p class="faint small" style="margin:6px 0 0">${a.matches.map(m => esc(m.label)).join(' · ')}</p></div>`;
}

function rivalBlock(f) {
  const rv = f.rivals || [];
  if (f.type !== 'player' || !rv.length) return '';
  return `<div class="panel section"><h2>Duelo directo</h2>${rv.map(x => `<div class="duel2">
      <div class="row"><b>${esc(x.name)}</b><span class="faint small">${esc(x.pos)}${x.source === 'último once' ? ' · según su último once' : ''}</span></div>
      ${x.rate ? `<div class="cmp"><span>${x.verb.replace('{}', `<b class="num">${fx(x.rate, 2)}</b>`)} por 90'</span><span class="faint">media del puesto ${fx(x.lg, 2)}</span></div>
      <div class="bar2"><i style="width:${Math.min(100, x.rate / Math.max(x.lg * 2, 0.01) * 100)}%"></i><u style="left:50%"></u></div>` : ''}
      ${x.overlap != null ? `<div class="small muted">Comparten el ${Math.round(x.overlap * 100)} % del terreno</div>` : ''}</div>`).join('')}</div>`;
}
function refBlock(f) {
  const r = f.referee;
  if (!r) return '';
  return `<div class="panel section"><h2>Árbitro</h2><div class="row"><b>${esc(r.name || 'Sin nombre')}</b><span class="sp"></span><span class="faint small">${r.games} partidos</span></div>
    <div class="cmp"><span><b class="num">${fx(r.cards)}</b> tarjetas por partido</span><span class="faint">media ${fx(r.lg_cards)}</span></div>
    ${r.fouls ? `<div class="cmp"><span><b class="num">${fx(r.fouls)}</b> faltas por partido</span><span class="faint">media ${fx(r.lg_fouls)}</span></div>` : ''}</div>`;
}
function vsOppBlock(f) {
  if (f.type !== 'player' || !(f.vs_opp || []).length) return '';
  return `<div class="panel section"><h2>Contra ${esc(f.opp_name)}</h2>${f.vs_opp.map(x => `<div class="cmp"><span>${F.dshort(x.kickoff)}</span><span><b class="num">${fx(x.v, 0)}</b> ${esc(f.player.stat_word)} en ${Math.round(x.min)}'</span></div>`).join('')}</div>`;
}

/* ---------- análisis del partido: cruces, duelos por banda y simulación ---------- */
async function loadInsights(mid) {
  const el = $('#insights'); if (!el) return;
  let d;
  try { d = await api(`/api/match/${mid}/insights`, { keep: true }); } catch (e) { el.innerHTML = ''; return; }
  if (!$('#insights')) return;
  const cr = (d.crosses || []);
  const al = cr.filter(x => x.aligned);
  const s = d.sim || {};
  const st = s.stats || [];
  el.innerHTML = `
  ${s.n ? `<div class="section"><h2>${s.n.toLocaleString('es-ES')} partidos simulados</h2><p class="lead">Minuto a minuto, con lo que cambia el juego según el marcador. Así acaba este partido si se jugara diez mil veces.</p>
    <div class="cols c2">
      <div class="panel"><h3>Resultado</h3><div class="tri big3"><div style="flex:${s.home_win}"><b>${F.p0(s.home_win)}</b> gana local</div><div style="flex:${s.draw}"><b>${F.p0(s.draw)}</b> empate</div><div style="flex:${s.away_win}"><b>${F.p0(s.away_win)}</b> gana visitante</div></div>
        <div class="chips" style="margin-top:12px">${(s.scores || []).map(([k, v]) => chip(k, F.p0(v))).join('')}</div>
        <div class="kv small" style="margin-top:12px"><span>Ambos marcan</span><b class="num">${F.p0(s.btts)}</b><span>Más de 2,5 goles</span><b class="num">${F.p0(s.over25)}</b><span>Portería a cero local</span><b class="num">${F.p0(s.home_cs)}</b><span>Portería a cero visitante</span><b class="num">${F.p0(s.away_cs)}</b></div></div>
      <div class="panel"><h3>Estadísticas esperadas</h3><table class="simtab"><tr><th></th><th class="r">Local</th><th class="r">Visitante</th><th class="r">Total</th><th class="r">8 de cada 10 entre</th></tr>
        ${st.map(x => `<tr><td>${esc(x.word[0].toUpperCase() + x.word.slice(1))}</td><td class="r num">${fx(x.home)}</td><td class="r num">${fx(x.away)}</td><td class="r num"><b>${fx(x.total)}</b></td><td class="r num faint">${Math.round(x.p10)}–${Math.round(x.p90)}</td></tr>`).join('')}</table></div>
    </div></div>` : ''}
  ${cr.length ? `<div class="section"><h2>Cruces de estadísticas</h2><p class="lead">Lo que hace cada equipo frente a lo que su rival suele conceder, comparado con la media de la liga. Cuando las dos cosas apuntan al mismo lado, es una pista fuerte.</p>
    <div class="panel crosses">${(al.length ? al : cr.slice(0, 6)).slice(0, 10).map(crossRow).join('')}
    ${al.length && cr.length > al.length ? `<details><summary class="small">Ver los ${cr.length - Math.min(al.length, 10)} cruces restantes</summary>${cr.filter(x => !x.aligned || al.indexOf(x) >= 10).map(crossRow).join('')}</details>` : ''}</div></div>` : ''}
  ${(d.duels || []).length ? `<div class="section"><h2>Duelos por banda</h2><p class="lead">Cada atacante del once previsto contra el defensor que le toca. El índice de faltas multiplica lo que recibe el atacante por lo que comete el defensor (1 = normal).</p>
    <div class="cols c2">${d.duels.slice(0, 6).map(x => `<div class="panel duel3"><div class="row small faint"><span>${esc(x.zone)}</span><span class="sp"></span><span>índice de faltas <b class="num ${x.foul_index > 1.2 ? 'win' : ''}">${fx(x.foul_index, 2)}</b></span></div>
      <div class="vs"><div><b>${esc(x.attacker.name)}</b><small>${esc(x.attacker.pos)} · recibe ${fx(x.attacker.fouled, 2)} faltas y completa ${fx(x.attacker.dribbles, 2)} regates por 90'</small></div><span>contra</span>
      <div><b>${esc(x.defender.name)}</b><small>${esc(x.defender.pos)} · comete ${fx(x.defender.fouls, 2)} faltas y hace ${fx(x.defender.tackles, 2)} entradas por 90'</small></div></div>
      ${x.overlap != null ? `<div class="small muted">Comparten el ${Math.round(x.overlap * 100)} % del terreno</div>` : ''}</div>`).join('')}</div></div>` : ''}
  ${d.referee ? `<div class="section"><h2>Árbitro</h2><div class="panel"><div class="row"><b>${esc(d.referee.name || '')}</b><span class="sp"></span><span class="faint small">${d.referee.games} partidos</span></div><div class="cmp"><span><b class="num">${fx(d.referee.cards)}</b> tarjetas por partido</span><span class="faint">media ${fx(d.referee.lg_cards)}</span></div></div></div>` : ''}`;
}
function crossRow(x) {
  const r1 = x.for / x.lg, r2 = x.against / x.lg;
  const dir = x.aligned ? (x.dir > 0 ? 'up' : 'down') : 'flat';
  return `<div class="cross ${dir}"><div class="ct"><b>${esc(x.team)}</b> · ${esc(x.name)}</div>
    <div class="cx"><span>hace <b class="num">${fx(x.for)}</b></span><span class="faint">${r1 >= 1 ? '+' : ''}${Math.round((r1 - 1) * 100)} %</span></div>
    <div class="cx"><span>sus rivales ante ${esc(x.opp)}: <b class="num">${fx(x.against)}</b></span><span class="faint">${r2 >= 1 ? '+' : ''}${Math.round((r2 - 1) * 100)} %</span></div>
    <div class="cx"><span>cruce</span><b class="num">${fx(x.proj)}</b></div>
    ${x.aligned ? `<span class="tagx ${x.dir > 0 ? 'up' : 'down'}">${x.dir > 0 ? 'los dos apuntan a más' : 'los dos apuntan a menos'}</span>` : ''}</div>`;
}

/* en las tarjetas de recomendación: veredicto y la frase más fuerte del análisis, una a una para no cargar el servidor */
async function fillTeasers() {
  const seq = navSeq;
  for (const el of $$('.teaser[data-mid]')) {
    if (seq !== navSeq) return;
    try {
      const f = await api(`/api/match/${el.dataset.mid}/bet?${el.dataset.q}`, { keep: true });
      if (seq !== navSeq) return;
      const skip = new Set(['precio', 'consenso', 'simulacion', 'racha10']);
      const want = f.tone === 'good' ? 1 : -1;
      const sig = f.signals.filter(x => !skip.has(x.key) && x.dir === want).sort((a, b) => b.w - a.w).slice(0, 2).map(x => x.text);
      const best = sig.length ? sig.join('. ') + '.' : (f.text.find(t => !t.startsWith('Veredicto') && !t.startsWith('En 10.000')) || '');
      el.innerHTML = `<span class="tv ${f.tone}">${esc(f.verdict)}</span><span>${esc(best)}</span><span class="faint">${f.n_pro} a favor, ${f.n_con} en contra</span>`;
      const card = el.closest('.rec');
      if (card && f.tone === 'bad') { card.classList.add('dim'); card.parentNode.appendChild(card); }
    } catch (e) { el.innerHTML = ''; }
  }
}
