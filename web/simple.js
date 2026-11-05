/* edgelab — capa "para todos": boletos claros, pasos para bet365 y menú sencillo.
   Todo lo técnico sigue existiendo en "Para expertos". */

/* ---------- menú ---------- */
(() => {
  const NAV2 = [['', 'Qué apostar hoy', 'hoy'], ['partidos', 'Partidos', 'partidos'], ['directo', 'En directo', 'directo'],
    ['historial', '¿Acierta la web?', 'historial'], ['apuestas', 'Mis apuestas', 'apuestas'], ['creditos', 'Créditos de apuesta', 'apuestas'],
    ['-', 'Para expertos'], ['oportunidades', 'Todas las apuestas con valor', 'oportunidades'], ['calculadora', 'Calculadora de coberturas', 'calculadora'],
    ['estrategias', 'Estrategias', 'estrategias'], ['laboratorio', 'Laboratorio', 'laboratorio'], ['aprendizaje', 'Cómo aprende', 'aprendizaje'],
    ['guia', 'Cómo funciona', 'guia'], ['ajustes', 'Mi banca', 'ajustes']];
  $('#nav').innerHTML = NAV2.map(([r, t, i]) => r === '-' ? `<div class="sep"></div><div class="navlab">${t}</div>`
    : `<a href="#/${r}" data-r="${r}" class="${['', 'partidos', 'directo', 'historial', 'apuestas', 'creditos'].includes(r) ? 'prime' : ''}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${ICON[i]}</svg>${t}${r === 'directo' ? '<span class="live-dot hidden" id="liveDot"></span>' : ''}</a>`).join('');
})();

/* ---------- lenguaje llano ---------- */
const RAW_TAB = {
  'Player Shots': 'Jugador → Tiros', 'Player Shots On Target': 'Jugador → Tiros a puerta', 'Player Tackles': 'Jugador → Entradas',
  'Player Fouls Committed': 'Jugador → Faltas cometidas', 'Player Fouls': 'Jugador → Faltas cometidas', 'Player To Be Fouled': 'Jugador → Faltas recibidas',
  'Player Fouls Drawn': 'Jugador → Faltas recibidas', 'Anytime Goalscorer': 'Goleadores → Marcará en cualquier momento', 'First Goalscorer': 'Goleadores → Primer goleador',
  'Player Assists': 'Jugador → Asistencias', 'Player To Be Booked': 'Jugador → Tarjetas', 'Goalkeeper Saves': 'Jugador → Paradas del portero',
};
const GROUP_TAB = { resultado: 'Resultado final / Doble oportunidad', handicap: 'Hándicap asiático', goles: 'Goles (más/menos)', exacto: 'Resultado correcto',
  mitades: 'Mitades', corners: 'Córners', tarjetas: 'Tarjetas', tiros: 'Estadísticas → Tiros', estadisticas: 'Estadísticas', jugadores: 'Jugador', especiales: 'Especiales' };
const sing = w => w.replace(/^(\S+?)(e?s)(\b.*)$/, (_, a, b, c) => a + (b === 'es' && /[lnr]$/.test(a) ? '' : '') + c.replace(/(\S+?)s\b/g, '$1'));
function plainLabel(label) {
  return String(label || '')
    .replace(/([Mm])ás de 0,5 (.+)$/, (_, M, w) => `al menos 1 ${sing(w)}`)
    .replace(/([Mm])ás de (\d+),5/g, (_, M, n) => `${+n + 1} o más`)
    .replace(/([Mm])enos de (\d+),5/g, (_, M, n) => `${n} o menos`)
    .replace(/^al menos/, 'Al menos');
}
function winText(v) {
  const L = plainLabel(v.label);
  let m = L.match(/^(.+?): (\d+) o (más|menos) (.+)$/);
  if (m) {
    const [, who, n, dir, what] = m, more = dir === 'más';
    const verb = /tarjeta/.test(what) ? `a ${who} le sacan` : /recibida/.test(what) ? `a ${who} le hacen` : /cometida/.test(what) ? `${who} comete` : /córner/.test(what) ? `${who} saca` : /gol/.test(what) ? `${who} marca` : `${who} hace`;
    const w = what.replace(/ (cometidas|recibidas)$/, '');
    return more ? `Ganas si ${verb} ${n} ${w} o más en el partido.` : `Ganas si ${verb} ${n} ${w} o menos en el partido.`;
  }
  m = L.match(/^(.+?): al menos 1 (.+)$/);
  if (m) { const w = m[2]; const verb = /tarjeta/.test(w) ? `a ${m[1]} le sacan` : /recibida/.test(w) ? `a ${m[1]} le hacen` : /cometida/.test(w) ? `${m[1]} comete` : /córner/.test(w) ? `${m[1]} saca` : /gol/.test(w) ? `${m[1]} marca` : `${m[1]} hace`;
    return `Ganas si ${verb} al menos 1 ${w.replace(/ (cometida|recibida)$/, '')} en el partido.`; }
  if (/^Al menos 1 /.test(L)) return `Ganas si hay ${L.toLowerCase()} en el partido.`;
  m = L.match(/^(\d+) o (más|menos) (.+)$/);
  if (m) return `Ganas si entre los dos equipos hay ${m[1]} ${m[3]} o ${m[2]}.`;
  if (/empate devuelve/i.test(L)) return `Ganas si gana ${L.split(' · ')[0]}; si empatan te devuelven el dinero.`;
  if (/^Gana /.test(L)) return `Ganas si ${L.replace(/^Gana /, '')} gana el partido.`;
  m = L.match(/^(.+?) o empate$/) || L.match(/^Empate o (.+?)$/);
  if (m) return `Ganas si ${m[1]} gana o empata.`;
  if (/^Ambos marcan$|marcan los dos/i.test(L) && !/^No /.test(L)) return 'Ganas si marcan los dos equipos.';
  if (/^No marcan los dos/i.test(L)) return 'Ganas si al menos uno de los dos equipos se queda sin marcar.';
  return v.explain ? v.explain.charAt(0).toUpperCase() + v.explain.slice(1) + '.' : '';
}
function riskOf(p) {
  if (p >= 0.62) return { lvl: 'low', txt: 'Riesgo bajo', sub: 'Sale la mayoría de las veces' };
  if (p >= 0.45) return { lvl: 'mid', txt: 'Riesgo medio', sub: 'Sale más o menos una de cada dos' };
  return { lvl: 'high', txt: 'Riesgo alto', sub: 'Sale pocas veces, pero paga mucho' };
}
const tenDots = p => { const k = Math.round((p || 0) * 10); return `<span class="dots10" aria-label="${k} de cada 10">${Array.from({ length: 10 }, (_, i) => `<i class="${i < k ? 'on' : ''}"></i>`).join('')}</span>`; };
const fe = v => v == null || isNaN(v) ? '–' : Number(v).toFixed(2).replace('.', ',');
const qOf = v => new URLSearchParams({ market: v.market, period: v.period || 'FT', selection: v.selection, ...(v.line != null ? { line: v.line } : {}) }).toString();

/* ---------- el boleto ---------- */
function ticket(v, o = {}) {
  const i = REC.push(v) - 1;
  const st = stakeOf(v) || 1;
  const minO = v.fair ? v.fair * 1.02 : null;
  const rk = riskOf(v.p);
  const col = o.colors || {};
  const m = v.match;
  return `<article class="tk ${o.hero ? 'hero' : ''}" data-i="${i}">
    ${o.hero ? '<div class="tk-flag">La apuesta del día</div>' : ''}
    <header class="tk-match">
      <span class="tk-teams">${crest(m.home_name, col.H, o.hero ? 36 : 28)}<b>${esc(m.home_name)}</b><span class="faint">vs</span><b>${esc(m.away_name)}</b>${crest(m.away_name, col.A, o.hero ? 36 : 28)}</span>
      <span class="tk-when">${F.day(m.kickoff).split(' ')[0]} ${F.hm(m.kickoff)} · ${COMP[m.comp] || m.comp}</span>
    </header>
    <div class="tk-body">
      <div class="tk-k">Apuesta</div>
      <h3 class="tk-bet">${esc(plainLabel(v.label))}</h3>
      <p class="tk-win">${esc(winText(v))}</p>
    </div>
    <div class="tk-perf" aria-hidden="true"></div>
    <div class="tk-nums">
      <div><small>Pon</small><b class="num">${String(st).replace('.', ',')} €</b></div>
      <div><small>Cuota en bet365</small><b class="num">${fe(v.b365)}</b><em>vale si es ${fe(minO)} o más</em>${v.b365_src && v.b365_src.startsWith('bet365 (hace') ? `<em class="draw-c">vista ${esc(v.b365_src.slice(7, -1))}: mírala en bet365</em>` : v.b365_src === 'mercado' ? '<em class="draw-c">precio medio de las casas: mira el de bet365</em>' : ''}</div>
      <div class="tk-pay"><small>Si aciertas cobras</small><b class="num">${fe(st * v.b365)} €</b><em>ganas ${fe(st * (v.b365 - 1))} €</em></div>
    </div>
    <div class="tk-risk ${rk.lvl}">
      <div><b>${rk.txt}</b><span>Sale ${Math.round(v.p * 10)} de cada 10 veces${v.meta && v.meta.p_sub && v.meta.p_sub > v.p + 0.02 ? ` · ${Math.round(v.meta.p_sub * 10)} de 10 con Sustituto+ (si bet365 te lo ofrece, actívalo)` : ''}</span></div>${tenDots(v.p)}
    </div>
    ${v.dossier ? `<div class="tk-verdict ${v.dossier.tone}">${esc(v.dossier.verdict === 'Apostar' ? 'El análisis completo la respalda' : v.dossier.verdict === 'Con cautela' ? 'Con cautela: hay datos a favor y en contra' : v.dossier.verdict)} · ${v.dossier.n_pro} a favor, ${v.dossier.n_con} en contra</div>
      <ul class="tk-why">${v.dossier.pro.map(t => `<li class="up">${esc(t)}</li>`).join('')}${v.dossier.con.slice(0, 1).map(t => `<li class="down">${esc(t)}</li>`).join('')}</ul>`
      : `<ul class="tk-why" data-mid="${m.id}" data-q="${esc(qOf(v))}"><li class="ld">Cruzando los datos de este partido…</li></ul>`}
    <p class="tk-edge">${v.push > 0.02 ? `Gana ${Math.round(v.p * 100)} de cada 100 veces y otras ${Math.round(v.push * 100)} te devuelven el dinero.` : `bet365 la paga como si saliera ${Math.round(100 / v.b365)} de cada 100 veces; creemos que sale ${Math.round(v.p * 100)}.`} Por eso merece la pena: a la larga ganas <b>${fe(v.ev * 10)} € por cada 10 €</b>.</p>
    <footer class="tk-foot"><a class="btn go" href="${betHref(m.id, v)}">Ver por qué y cómo hacerla</a><button class="btn" onclick="madeBet(${i})">Ya la he hecho</button></footer>
  </article>`;
}

/* razones: lo que empuja a favor (y la principal en contra), en frases cortas */
async function fillReasons(root = document) {
  const seq = navSeq;
  const els = $$('.tk-why[data-mid]', root);
  await Promise.all(els.map(async el => {
    try {
      const f = await api(`/api/match/${el.dataset.mid}/bet?${el.dataset.q}`, { keep: true });
      if (seq !== navSeq) return;
      el.innerHTML = reasonsHtml(f, 3);
      const tk = el.closest('.tk');
      if (tk && f.tone === 'bad') {
        tk.classList.add('dim');
        tk.querySelector('.tk-body').insertAdjacentHTML('afterbegin', '<div class="tk-warn">Al cruzar todos los datos, no la vemos clara: mejor no</div>');
      } else if (tk && f.verdict === 'Espera al once') {
        tk.querySelector('.tk-body').insertAdjacentHTML('afterbegin', '<div class="tk-hold">Espera a que salga la alineación oficial (1 h antes)</div>');
      }
    } catch (e) { el.innerHTML = ''; }
  }));
  const box = $('#tkOthers');
  if (box) $$('.tk.dim', box).forEach(t => box.appendChild(t));   // las dudosas, al final
}
const SKIP = new Set(['precio', 'consenso', 'simulacion', 'racha10']);
function reasonsHtml(f, n) {
  const pro = f.signals.filter(s => s.dir > 0 && !SKIP.has(s.key)).sort((a, b) => b.w - a.w).slice(0, n);
  const con = f.signals.filter(s => s.dir < 0).sort((a, b) => b.w - a.w).slice(0, 1);
  if (!pro.length && !con.length) return '';
  return pro.map(s => `<li class="up">${esc(s.text)}</li>`).join('') + con.map(s => `<li class="down">${esc(s.text)}</li>`).join('');
}

/* ---------- QUÉ APOSTAR HOY ---------- */
// días a elegir (selección múltiple): hoy, mañana y los 5 siguientes con su fecha
function dayChips() {
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(d0.getTime() + i * 86400000);
    const wd = d.toLocaleDateString('es-ES', { weekday: 'long' });
    const name = i === 0 ? 'Hoy' : i === 1 ? 'Mañana' : wd.charAt(0).toUpperCase() + wd.slice(1);
    return { i, t0: d.getTime() / 1000, t1: d.getTime() / 1000 + 86400, label: `${name} ${d.getDate()}` };
  });
}
async function hoySimple() {
  const days = dayChips();
  let sel = [0];
  try { const v = JSON.parse(localStorage.getItem('el_days') || '[0]'); if (Array.isArray(v) && v.length) sel = v.filter(i => i >= 0 && i < 7); } catch (e) {}
  if (!sel.length) sel = [0];
  const last = Math.max(...sel);
  const now = Date.now() / 1000;
  const hrs = Math.min(168, Math.ceil((days[last].t1 - now) / 3600) + 1);
  const [ms, opp, trk] = await Promise.all([api(`/api/matches?days=${last + 1}`), api(`/api/opportunities?hours=${hrs}`).catch(() => null), api('/api/track?days=90').catch(() => null)]);
  const colors = {}; ms.forEach(m => { colors[m.id] = m.colors; });
  const inSel = t => sel.some(i => t >= days[i].t0 && t < days[i].t1 && t >= now - 3 * 3600);
  if (opp) opp.values = opp.values.filter(v => inSel(v.match.kickoff));
  const nShow = 10 * sel.length;          // el límite real es por día (10 y el 15 % de la banca), como el bot
  const vs = opp ? topPicks(visibleValues(opp.values), nShow) : [];
  const RW = sel.length === 1 ? (sel[0] === 0 ? 'Hoy' : sel[0] === 1 ? 'Para mañana' : 'Para el ' + days[sel[0]].label.toLowerCase()) : `En los ${sel.length} días elegidos`;
  // la del día: la mejor de riesgo bajo/medio y fiabilidad alta si la hay
  // la del día: la más sólida, no la más vistosa (mercado principal, fiabilidad alta, sale más de la mitad de las veces)
  // la del día: la más favorable mirando TODO (valor por euro, probabilidad, fiabilidad y el análisis completo)
  const pickScore = v => { const d = v.dossier || {}; if (d.tone === 'bad') return -1;
    const kelly = Math.max(0, v.ev) / Math.max((v.b365 || 2) - 1, 0.2);
    return kelly * (0.4 + (d.score ?? 0.6)) * (v.conf === 'alta' ? 1 : 0.85) * (d.tone === 'good' ? 1 : d.tone === 'warn' ? 0.7 : 0.85) * (0.6 + Math.min(v.p, 0.8) / 2); };
  vs.sort((a, b) => pickScore(b) - pickScore(a));
  let hero = vs[0];
  const rest = vs.filter(v => v !== hero);
  const today = new Date().toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' });
  const a = (trk && trk.all) || {};
  const first = (() => { try { return !localStorage.getItem('el_seen_steps'); } catch (e) { return true; } })();
  view.innerHTML = `
  <section class="intro">
    <p class="intro-date">${today.charAt(0).toUpperCase() + today.slice(1)}</p>
    <div class="rng" role="group" aria-label="Días">${days.map(d => `<button class="${sel.includes(d.i) ? 'on' : ''}" data-day="${d.i}" aria-pressed="${sel.includes(d.i)}">${d.label}</button>`).join('')}<button class="all" data-day="all">Toda la semana</button></div>
    <h1>${vs.length ? `${RW} te recomendamos ${vs.length === 1 ? '1 apuesta' : vs.length + ' apuestas'}` : `${RW} no hay ninguna apuesta clara`}</h1>
    <p class="intro-sub">${opp ? `Hemos comparado ${(opp.n_rows || 0).toLocaleString('es-ES')} cuotas de ${opp.n_matches} partidos con lo que de verdad creemos que va a pasar. Solo te enseñamos las que bet365 paga de más.` : ''}${vs.length ? ` Si las haces todas pones <b>${fe(vs.reduce((a, v) => a + (stakeOf(v) || 1), 0))} €</b> y, de media, ganas <b>${fe(vs.reduce((a, v) => a + (stakeOf(v) || 1) * v.ev, 0))} €</b>.` : ''}</p>
    <a class="bank" href="#/ajustes">Tu banca: <b>${USER.bankroll} €</b><span>Las cantidades salen de aquí · cambiar</span></a>
  </section>
  ${first ? `<section class="steps3" id="steps3">
    <div><span>1</span><b>Elige un boleto</b><p>Cada uno dice qué apostar, cuánto poner y cuánto cobras.</p></div>
    <div><span>2</span><b>Búscalo en bet365</b><p>Mismo partido, mismo mercado. Comprueba que la cuota no ha bajado del mínimo.</p></div>
    <div><span>3</span><b>Pon lo que te decimos</b><p>Ni más ni menos: así una mala racha no te hunde. Luego pulsa «Ya la he hecho».</p></div>
    <button class="x" id="stepsOk" aria-label="Entendido">Entendido</button></section>` : ''}
  ${opp && opp.pending && !vs.length ? `<div class="panel">${empty(`Analizando ${opp.pending} partidos…`, 'Estamos revisando todas las cuotas. En unos minutos aparecen aquí; la página se refresca sola.')}</div>` : ''}
  ${hero ? ticket(hero, { hero: true, colors: colors[hero.match.id] }) : (!opp || !opp.pending ? `<div class="panel">${empty('Ninguna apuesta paga lo suficiente ahora mismo', 'No apostar también es ganar dinero. Las cuotas cambian: vuelve en unas horas.')}</div>` : '')}
  ${rest.length ? `<h2 class="sec-t">También recomendamos</h2><div class="tk-grid" id="tkOthers">${rest.map(v => ticket(v, { colors: colors[v.match.id] })).join('')}</div>` : ''}
  <div id="mkBox"></div>
  <div id="ladderBox"></div>
  <section class="trust">
    <div class="trust-t"><h2>¿Acierta la web?</h2><p>Cada recomendación se apunta sola, a la cuota de ese momento y con 10 € fijos, y se cobra o se pierde sola al acabar el partido. Con esos resultados la web corrige sus cálculos: si un tipo de apuesta falla más de lo previsto, deja de recomendarlo tanto.</p></div>
    <div class="trust-n">
      <div><b class="num">${a.n || 0}</b><span>resueltas</span></div>
      <div><b class="num">${a.n ? Math.round(a.won / a.n * 100) + ' %' : '–'}</b><span>acertadas</span></div>
      <div><b class="num ${F.cls(a.profit)}">${a.n ? F.eur(a.profit) : '–'}</b><span>con 10 € cada una</span></div>
      <div><b class="num">${trk ? trk.open : 0}</b><span>por jugarse</span></div>
    </div>
    <a class="btn" href="#/historial">Ver todas</a>
  </section>
  <h2 class="sec-t">Partidos</h2>
  <div class="mini-fx">${ms.filter(m => m.status === 'inprogress' || (m.status === 'notstarted' && inSel(m.kickoff))).slice(0, 16).map(miniFixture).join('') || empty('Sin partidos', 'El calendario se actualiza solo.')}</div>`;
  $$('[data-day]').forEach(b => b.onclick = () => {
    let nx;
    if (b.dataset.day === 'all') nx = sel.length === 7 ? [0] : [0, 1, 2, 3, 4, 5, 6];
    else { const i = +b.dataset.day; nx = sel.includes(i) ? sel.filter(x => x !== i) : [...sel, i].sort((a, c) => a - c); if (!nx.length) nx = [i]; }
    try { localStorage.setItem('el_days', JSON.stringify(nx)); } catch (e) {}
    route();
  });
  api('/api/alerts?kind=mercado&hours=24', { keep: true }).then(al => {
    if (!$('#mkBox')) return;
    const can = 'Notification' in window;
    const on = can && Notification.permission === 'granted';
    $('#mkBox').innerHTML = `<section class="panel mk-new"><div class="row"><h2 style="margin:0">Mercados recién abiertos con valor</h2><span class="sp"></span>
      ${can && !on ? '<button class="btn sm" id="mkNotif">Avisarme en este dispositivo</button>' : on ? '<span class="faint small">Avisos activados aquí</span>' : ''}</div>
      <p class="muted">Cuando bet365 abre un mercado nuevo (un partido, los jugadores, los córners…) sus cuotas suelen estar desajustadas un rato. Si al abrir ya alguna paga más de lo que debería, sale aquí y te avisa${on ? '' : ' (también por Telegram si lo tienes en Ajustes)'}.</p>
      ${(al || []).length ? al.map(x => `<a class="mk-it" href="#/partido/${x.match_id}"><b>${esc(x.title.replace('Mercado nuevo con valor: ', ''))}</b><span>${esc(x.message.split('\n').filter(l => l.startsWith('•')).slice(0, 3).join('  ').replace(/<[^>]+>/g, ''))}</span><small>${F.ago(x.ts)}</small></a>`).join('') : '<p class="faint">En las últimas 24 horas no se ha abierto ningún mercado con valor.</p>'}</section>`;
    const nb = $('#mkNotif');
    if (nb) nb.onclick = async () => { const r = await Notification.requestPermission(); toast(r === 'granted' ? 'Te avisaremos mientras tengas la web abierta' : 'Sin permiso para avisar'); route(); };
  }).catch(() => {});
  const ok = $('#stepsOk');
  if (ok) ok.onclick = () => { try { localStorage.setItem('el_seen_steps', '1'); } catch (e) {} $('#steps3').remove(); };
  fillReasons();
  api('/api/ladders', { keep: true }).then(L => { const it = (L.items || [])[0]; if (it && $('#ladderBox')) $('#ladderBox').innerHTML = `<h2 class="sec-t">La escalera del día</h2>${ladderCard(it, true)}`; }).catch(() => {});
  if (opp && opp.pending) timers.push(setInterval(() => { if (!location.hash || location.hash === '#/' || location.hash.startsWith('#/?')) route(); }, 60000));
}
function miniFixture(m) {
  const p = m.model || {};
  const fav = p.pH ? (p.pH > p.pA ? [m.home_name, p.pH] : [m.away_name, p.pA]) : null;
  return `<a class="mfx" href="#/partido/${m.id}">
    <span class="mfx-t">${m.status === 'inprogress' ? '<span class="live-tag">EN JUEGO</span>' : F.hm(m.kickoff)}<small>${COMP[m.comp] || m.comp}</small></span>
    <span class="mfx-n">${crest(m.home_name, m.colors && m.colors.H, 26)}${esc(m.home_name)}<br>${crest(m.away_name, m.colors && m.colors.A, 26)}${esc(m.away_name)}</span>
    ${p.pH ? `<span class="mfx-p"><span class="pbar"><i style="width:${p.pH * 100}%;background:${tc(m.colors && m.colors.H, 'H')}"></i><i style="width:${p.pD * 100}%;background:#c9d1dc"></i><i style="width:${p.pA * 100}%;background:${tc(m.colors && m.colors.A, 'A')}"></i></span><small>${fav ? `Favorito: ${esc(fav[0])} (${Math.round(fav[1] * 100)} %)` : ''}</small></span>` : '<span></span>'}
    <span class="mfx-v">${m.n_value ? `<b>${m.n_value}</b> con valor` : '<span class="faint">ver</span>'}</span></a>`;
}
routes[''] = hoySimple; routes.hoy = hoySimple;

/* ---------- FICHA DE APUESTA: arriba, el boleto y cómo hacerla; debajo, todos los datos ---------- */
const _apuesta = apuesta;
routes.apuesta = async mid => {
  await _apuesta(mid);
  const f = window.__F;
  if (!f) return;
  const r = { ...f.row, match: f.match };
  const st = stakeOf(r) || 1, minO = r.fair ? r.fair * 1.02 : null, rk = riskOf(r.p);
  const raw = (r.market || '').startsWith('RAW:') ? r.market.slice(4) : null;
  const tab = (raw && RAW_TAB[raw]) || GROUP_TAB[r.group] || 'el mercado del partido';
  const pro = f.signals.filter(s => s.dir > 0 && !SKIP.has(s.key)).sort((a, b) => b.w - a.w);
  const con = f.signals.filter(s => s.dir < 0).sort((a, b) => b.w - a.w);
  const good = f.tone === 'good', bad = f.tone === 'bad' || !(r.ev > 0.02);
  const head = `
  <section class="bet-hero ${bad ? 'bad' : good ? 'good' : 'warn'}">
    <div class="bh-l">
      <div class="bh-verdict">${bad ? 'Mejor no' : good ? 'Sí, apuéstala' : esc(f.verdict)}</div>
      <h1>${esc(plainLabel(r.label))}</h1>
      <p class="bh-win">${esc(winText(r))}</p>
      <div class="tk-risk ${rk.lvl}"><div><b>${rk.txt}</b><span>Sale ${Math.round(r.p * 10)} de cada 10 veces</span></div>${tenDots(r.p)}</div>
    </div>
    ${bad ? `<div class="bh-r"><p>Ahora mismo la cuota no compensa o los datos van en contra. Si quieres apostarla igualmente, que sea a ${fe(minO)} o más.</p></div>` : `
    <ol class="bh-steps">
      <li><b>Abre bet365</b> y busca <b>${esc(f.match.home_name)} – ${esc(f.match.away_name)}</b> (${F.day(f.match.kickoff).split(' ')[0]} ${F.hm(f.match.kickoff)}).</li>
      <li>Entra en <b>${esc(tab)}</b> y elige <b>${esc(plainLabel(r.label))}</b>.</li>
      <li>Mira la cuota: si es <b>${fe(minO)} o más</b>, adelante. Ahora está a ${fe(r.b365)}.</li>
      <li>Pon <b>${st} €</b>. Si aciertas cobras <b>${fe(st * r.b365)} €</b>.</li>
      <li>Vuelve aquí y pulsa <button class="btn sm go" id="madeIt2">Ya la he hecho</button> para llevar la cuenta.</li>
    </ol>`}
  </section>
  <section class="why-big">
    <h2>Por qué</h2>
    <div class="why-cols">
      <div><h3 class="up">A favor (${pro.length})</h3><ul>${pro.map(s => `<li>${esc(s.text)}</li>`).join('') || '<li class="faint">Nada destacable</li>'}</ul></div>
      <div><h3 class="down">En contra (${con.length})</h3><ul>${con.map(s => `<li>${esc(s.text)}</li>`).join('') || '<li class="faint">Nada en contra</li>'}</ul></div>
    </div>
    <p class="tk-edge">bet365 la paga como si saliera ${Math.round(100 / r.b365)} de cada 100 veces; nosotros creemos que sale ${Math.round(r.p * 100)}${r.p_market != null ? ` (y las demás casas, quitándoles su margen, ${Math.round(r.p_market * 100)})` : ''}. ${r.ev > 0 ? `A la larga, apostando siempre así, ganas ${fe(r.ev * 10)} € por cada 10 €.` : 'No paga lo suficiente.'}</p>
  </section>
  <h2 class="sec-t">Todos los datos, uno a uno</h2>`;
  const dz = $('.dossier');
  if (dz) dz.outerHTML = head;
  const b = $('#madeIt2');
  if (b) b.onclick = () => { const i = REC.push(r) - 1; madeBet(i); };
};

/* ---------- PARTIDO: arriba, qué apostar en este partido y cómo creemos que irá ---------- */
const _partido = partido;
routes.partido = async id => {
  await _partido(id);
  const tabs = $('#mtabs');
  if (!tabs) return;
  tabs.insertAdjacentHTML('beforebegin', '<section id="mrec" class="mrec"></section>');
  const seq = navSeq;
  const [opp, ins] = await Promise.all([api('/api/opportunities?hours=168', { keep: true }).catch(() => null), api(`/api/match/${id}/insights`, { keep: true }).catch(() => null)]);
  if (seq !== navSeq || !$('#mrec')) return;
  const mine = opp ? topPicksOne(visibleValues(opp.values).filter(v => String(v.match.id) === String(id)), 2) : [];
  const sim = ins && ins.sim;
  $('#mrec').innerHTML = `
    <div class="mrec-l"><h2>Qué apostar en este partido</h2>${mine.length ? mine.map(v => ticket(v, {})).join('')
      : `<div class="panel">${empty('Nada que merezca la pena', 'Ninguna cuota de bet365 de este partido paga más de lo que debería. Si cambian las cuotas, aparecerá aquí.')}</div>`}</div>
    ${sim ? `<div class="mrec-r"><h2>Cómo creemos que irá</h2><div class="panel forecast">${forecastHtml(sim, ins)}</div></div>` : ''}`;
  fillReasons($('#mrec'));
  $('#mrec').insertAdjacentHTML('afterend', '<section id="mpost"></section><section id="mlad"></section><section id="mnews" class="mnews"></section>');
  api(`/api/match/${id}/postmatch`, { keep: true }).then(pm => { if (seq === navSeq && $('#mpost') && pm && pm.ts) $('#mpost').innerHTML = postmatchHtml(pm); }).catch(() => {});
  api(`/api/match/${id}/ladder`, { keep: true }).then(L => { if (seq === navSeq && $('#mlad') && L && L.steps) $('#mlad').innerHTML = `<h2 class="sec-t">Escalera para este partido</h2>${ladderCard(L, false)}`; }).catch(() => {});
  api(`/api/match/${id}/context`, { keep: true }).then(cx => {
    if (seq !== navSeq || !$('#mnews') || !cx || !cx.H) return;
    const mt = $$('.score-hero .side .nm').map(e => e.textContent);
    const one = (sd, nm) => { const c = cx[sd]; return `<div><h3>${esc(nm || '')}</h3><ul class="plain">
      <li>Descanso: <b>${c.rest_days != null ? fx(c.rest_days, 0) + ' días' : '–'}</b></li><li>Partidos en 14 días: <b>${c.games_14d}</b></li>
      ${c.next_days != null ? `<li>Próximo partido: en <b>${fx(c.next_days, 0)} días</b>${c.next_comp ? ` (${esc(COMP[c.next_comp] || c.next_comp)})` : ''}</li>` : ''}
      ${c.stakes ? `<li>${esc(COMP[c.stakes.comp] || 'Liga')}: <b>${c.stakes.pos}.º</b>, ${c.stakes.pts} pts${c.stakes.fight ? ` · <b>${esc(c.stakes.fight)}</b>` : ''}</li>` : ''}
      ${c.notes.length ? `<li class="${c.tired || c.rotation ? 'draw-c' : ''}">${esc(c.notes.join(' · '))}</li>` : ''}</ul></div>`; };
    $('#mnews').insertAdjacentHTML('afterbegin', `<div class="panel ctxp"><h2>Cansancio y lo que se juegan</h2><div class="cols c2">${one('H', mt[0])}${one('A', mt[1])}</div>${cx.weather ? `<p class="muted" style="margin:10px 0 0">Tiempo previsto${cx.weather.city ? ' en ' + esc(cx.weather.city) : ''}: <b>${cx.weather.temp} °C</b>, ${cx.weather.rain > 0 ? `<b>${String(cx.weather.rain).replace('.', ',')} mm</b> de lluvia` : 'sin lluvia'}, viento de <b>${cx.weather.wind} km/h</b>${cx.weather.rain >= 3 || cx.weather.wind >= 35 ? ' · puede frenar el juego y los goles' : ''}.</p>` : ''}</div>`);
  }).catch(() => {});
  api(`/api/match/${id}/news`, { keep: true }).then(nw => {
    if (seq !== navSeq || !$('#mnews')) return;
    const mm = nw.missing || [];
    const side = sd => mm.filter(p => p.side === sd).map(p => `<li><b>${esc(p.name)}</b> <span class="${p.status === 'baja' ? 'loss' : 'draw-c'}">${p.status === 'baja' ? 'no juega' : 'duda'}</span> <span class="faint">${esc(p.reason)}</span></li>`).join('') || '<li class="faint">Sin bajas publicadas</li>';
    const mt = $('.score-hero .side .nm') ? $$('.score-hero .side .nm').map(e => e.textContent) : ['Local', 'Visitante'];
    $('#mnews').insertAdjacentHTML('beforeend', `<div class="panel"><h2>Bajas</h2><div class="cols c2"><div><h3>${esc(mt[0] || '')}</h3><ul class="plain">${side('H')}</ul></div><div><h3>${esc(mt[1] || '')}</h3><ul class="plain">${side('A')}</ul></div></div></div>
      <div class="panel"><h2>Últimas noticias</h2>${(nw.flags || []).length ? `<div class="flags">${nw.flags.map(f => `<a class="flag ${f.kind}" href="${esc(f.link)}" target="_blank" rel="noopener"><b>${f.kind === 'baja' ? (f.level === 'fuera' ? `Baja según la prensa: ${esc(f.player)}` : `En duda: ${esc(f.player)}`) : f.kind === 'vuelve' ? `Vuelve / disponible: ${esc(f.player)}` : f.kind === 'entrenador' ? `Entrenador: ${esc(mt[f.side === 'H' ? 0 : 1] || '')}` : `Rotaciones: ${esc(mt[f.side === 'H' ? 0 : 1] || '')}`}</b><span>${esc(f.title)}</span></a>`).join('')}</div>` : ''}${(nw.items || []).length ? `<ul class="news">${nw.items.slice(0, 8).map(n => `<li><a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a><small>${esc(n.source || '')}${n.ts ? ' · ' + F.ago(n.ts) : ''}</small></li>`).join('')}</ul>` : '<p class="faint">Sin noticias recientes.</p>'}</div>`);
  }).catch(() => {});
};
function topPicksOne(vs, n) {
  const a = [...vs.filter(v => v.conf === 'alta'), ...vs.filter(v => v.conf !== 'alta')];
  const out = []; let np = 0;
  for (const v of a) { if (v.group === 'jugadores' && np >= 1) continue; out.push(v); if (v.group === 'jugadores') np++; if (out.length >= n) break; }
  return out;
}
function forecastHtml(sim, ins) {
  const sc = (sim.scores || []).slice(0, 3);
  const rowsK = [['CORNERS', 'Córners'], ['CARDS', 'Tarjetas'], ['SHOTS', 'Tiros'], ['SOT', 'Tiros a puerta'], ['FOULS', 'Faltas']];
  const st = {}; (sim.stats || []).forEach(x => { st[x.code] = { total: x.total, lo: Math.round(x.p10), hi: Math.round(x.p90) }; });
  return `<p class="faint small" style="margin:0 0 10px">Jugando el partido 10.000 veces en el ordenador, con todo lo que sabemos de los dos equipos.</p>
    ${sc.length ? `<div class="fc-sc"><small>Marcadores más probables</small><div>${sc.map(([k, p]) => `<span><b class="num">${k}</b>${Math.round(p * 100)} %</span>`).join('')}</div></div>` : ''}
    <div class="fc-g">
      ${sim.btts != null ? `<div><b class="num">${Math.round(sim.btts * 100)} %</b><span>marcan los dos</span></div>` : ''}
      ${sim.over25 != null ? `<div><b class="num">${Math.round(sim.over25 * 100)} %</b><span>3 goles o más</span></div>` : ''}
    </div>
    <table class="fc-t">${rowsK.filter(([k]) => st[k]).map(([k, t]) => `<tr><td>${t}</td><td class="r"><b class="num">${fx(st[k].total, 1)}</b></td><td class="faint small r">normalmente entre ${st[k].lo} y ${st[k].hi}</td></tr>`).join('')}</table>
    <button class="btn sm" onclick="document.querySelector('#mtabs [data-t=analisis]').click();document.querySelector('#mtabs').scrollIntoView({behavior:'smooth'})">Ver el análisis completo del partido</button>`;
}

/* ---------- ¿ACIERTA LA WEB?: lo que ha aprendido, arriba ---------- */
const _historial = historial;
routes.historial = async () => {
  await _historial();
  const seq = navSeq;
  const t = await api('/api/track?days=180', { keep: true }).catch(() => null);
  if (!t || seq !== navSeq) return;
  const L = (t.learning && t.learning.groups) || {};
  const ks = Object.keys(L).sort((a, b) => L[b].n - L[a].n);
  const box = `<section class="learn panel"><h2>Lo que ha aprendido la web</h2>
    <p class="muted">Cada recomendación resuelta se compara con lo que esperábamos. Si un tipo de apuesta acierta menos de lo previsto, la web rebaja sus probabilidades y lo recomienda menos; si acierta más, las sube un poco. Con pocas apuestas apenas corrige, para no fiarse de la suerte.</p>
    ${ks.length ? `<table><tr><th>Tipo de apuesta</th><th class="r">Resueltas</th><th class="r">Esperábamos acertar</th><th class="r">Acertadas</th><th class="r">Corrección</th></tr>${ks.map(k => { const g = L[k]; const d = Math.round((g.factor - 1) * 100);
      return `<tr><td>${esc(k.includes('@') ? (GROUP_NAME[k.split('@')[0]] || k) + (k.endsWith('corta') ? ' · cuotas hasta 4' : ' · cuotas de 4 o más') : (GROUP_NAME[k] || k))}</td><td class="r num">${g.n}</td><td class="r num">${fx(g.expected, 1)}</td><td class="r num">${fx(g.won, 1)}</td><td class="r ${d < 0 ? 'loss' : d > 0 ? 'win' : 'faint'}">${d === 0 ? 'ninguna' : (d > 0 ? '+' : '') + d + ' %'}</td></tr>`; }).join('')}</table>`
      : `<div class="note">Todavía no se ha resuelto ninguna recomendación. En cuanto terminen los partidos de hoy, aparecerá aquí lo que va aprendiendo.</div>`}</section>`;
  const sc = t.scan && t.scan.all, a = t.all || {};
  const sumBox = (h, x, txt) => `<div class="hs-c"><h3>${h}</h3><p class="faint small">${txt}</p>${x && x.n ? `<div class="hs-n"><span><b class="num">${x.won}/${x.n}</b> acertadas</span><span><b class="num ${x.profit >= 0 ? 'win' : 'loss'}">${x.profit >= 0 ? '+' : ''}${fx(x.profit, 2)} €</b> con 10 € por apuesta</span><span>Rentabilidad <b class="num">${x.yield != null ? Math.round(x.yield * 100) + ' %' : '–'}</b></span><span>Cuota media <b class="num">${fx(x.avg_odds, 2)}</b></span>${x.beat_close != null ? `<span>Bate la cuota de cierre <b class="num ${x.beat_close >= 0.5 ? 'win' : 'loss'}">${Math.round(x.beat_close * 100)} %</b> de las veces</span>` : ''}</div>` : '<p class="faint">Aún ninguna resuelta.</p>'}</div>`;
  const ST = { won: ['Acertada', 'win'], lost: ['Fallada', 'loss'], halfwon: ['Media ganada', 'win'], halflost: ['Media perdida', 'loss'], push: ['Devuelta', 'faint'], void: ['Anulada', 'faint'] };
  const head = `<section class="panel hsum"><div class="row"><h2 style="margin:0">Apuestas resueltas</h2><span class="sp"></span><button class="btn sm" id="resRun">Resolver ya los partidos acabados</button></div>
    <p class="muted">Se resuelve <b>todo</b>: las recomendaciones oficiales (las que te enseñamos para apostar, máximo unas pocas al día), todas las apuestas con valor que detecta el escáner aunque no te las recomendemos, y las que tú marques como hechas. Se cuentan por separado para que el historial oficial sea honesto.</p>
    <div class="cols c3">${sumBox('Recomendaciones oficiales', a, 'Las que la web te dice que hagas.')}${sumBox('Recién abiertos', t.opening && t.opening.all, 'Las que tenían valor nada más abrir su mercado, a la cuota de ese momento. ¿Compensa entrar en las aperturas?')}${sumBox('Todo lo que detecta el escáner', sc, 'Cualquier cuota con valor, también las que descartamos. Sirve para aprender.')}</div>
    <div id="resList"></div></section>`;
  const ph_ = $('.ph');
  if (ph_) { ph_.insertAdjacentHTML('afterend', box); ph_.insertAdjacentHTML('afterend', head); }
  const rb = $('#resRun');
  if (rb) rb.onclick = async () => { rb.disabled = true; rb.textContent = 'Resolviendo…'; await api('/api/results/run', { method: 'POST', keep: true });
    toast('Bajando las estadísticas finales y resolviendo. Se actualiza solo en cuanto acabe.');
    for (let k = 0; k < 30; k++) { await new Promise(r => setTimeout(r, 10000)); if (seq !== navSeq) return; const n = await api('/api/bets?resolved=1&limit=1500', { keep: true }).catch(() => []); if (n.filter(x => x.status !== 'open').length !== nResolved) { routes.historial(); return; } } rb.disabled = false; rb.textContent = 'Resolver ya los partidos acabados'; };
  const allB = await api('/api/bets?resolved=1&limit=1500', { keep: true }).catch(() => []);
  if (seq !== navSeq) return;
  const done = allB.filter(x => x.status !== 'open');
  const nResolved = done.length;
  const MODE = { tip: 'Oficial', opening: 'Recién abierto', scan: 'Escáner', real: 'Tuya', watch: 'Vigilancia en directo', paper: 'Estrategia en prueba' };
  const pendPast = 0;
  const drawList = f => { const rows = done.filter(x => f === 'all' || x.mode === f).sort((a, b) => (b.kickoff - a.kickoff) || (b.settled - a.settled));
    $('#resList').innerHTML = `<div class="chips">${[['all', 'Todas'], ['tip', 'Oficiales'], ['opening', 'Recién abiertos'], ['scan', 'Escáner'], ['real', 'Tuyas'], ['watch', 'Vigilancia en directo'], ['paper', 'Estrategias en prueba']].map(([k, n]) => `<button class="chip ${k === f ? 'on' : ''}" data-f="${k}">${n} <small>${k === 'all' ? done.length : done.filter(x => x.mode === k).length}</small></button>`).join('')}</div>
      ${pendPast ? `<p class="faint small">${pendPast} apuestas de partidos ya acabados esperan a la estadística final (tiros, córners, jugadores). Se resuelven solas en cuanto llega.</p>` : ''}
      ${rows.length ? `<div class="tw"><table class="rtab"><tr><th>Partido</th><th>Apuesta</th><th>Tipo</th><th class="r">Cuota</th><th class="r">Prob.</th><th>Resultado</th><th class="r">Con 10 €</th></tr>${rows.slice(0, 400).map(r => { const s_ = ST[r.status] || [r.status, 'faint']; let n = {}; try { n = JSON.parse(r.notes || '{}'); } catch (e) {}
        const pnl10 = r.stake ? (r.pnl || 0) / r.stake * 10 : null;
        return `<tr><td><a href="#/partido/${r.match_id}">${esc(r.home_name)} – ${esc(r.away_name)}</a><br><small class="faint">${F.day(r.kickoff)}</small></td><td>${esc(plainLabel(n.label || (r.selection + (r.line != null ? ' ' + r.line : ''))))}${(n.why || []).length ? `<ul class="why">${n.why.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}</td><td class="faint small">${MODE[r.mode] || r.mode}</td><td class="r num">${fe(r.odds)}</td><td class="r num faint">${r.fair_prob ? Math.round(r.fair_prob * 100) + ' %' : '–'}</td><td class="${s_[1]}">${s_[0]}</td><td class="r num ${(pnl10 || 0) >= 0 ? 'win' : 'loss'}">${pnl10 != null ? ((pnl10 >= 0 ? '+' : '') + fx(pnl10, 2)) : '–'}</td></tr>`; }).join('')}</table></div>` : '<p class="faint">Ninguna resuelta todavía en este grupo.</p>'}`;
    $$('#resList .chip').forEach(c => c.onclick = () => drawList(c.dataset.f)); };
  drawList('all');
  api('/api/bias', { keep: true }).then(B => { const R = (B.rows || {}).groups || {}, K = Object.keys(R).sort((a, b) => R[b].n - R[a].n), bu = (B.rows || {}).buckets || {};
    if (seq !== navSeq || !$('.hsum')) return;
    $('.hsum').insertAdjacentHTML('afterend', `<section class="panel learn"><h2>Todo lo que enseñaba la web frente a lo que pasó</h2>
      <p class="muted">Antes de cada partido se guarda cada mercado, cada línea y cada apuesta que salía en la página (tenga valor o no) con su probabilidad. Al acabar se mira cuál salió. Así se ve si la web acierta sus porcentajes y en qué tipo de apuesta se pasa o se queda corta, y se corrigen solas. También se comparan las estadísticas de cada jugador, sus minutos, los cambios, el mapa de calor y las del equipo (en la página de cada partido acabado).</p>
      ${K.length ? `<div class="cols c2"><div><h3>Por tipo de apuesta</h3><table><tr><th>Tipo</th><th class="r">Apuestas</th><th class="r">Esperábamos</th><th class="r">Salieron</th><th class="r">Las de valor</th></tr>${K.map(k => { const g = R[k]; return `<tr><td>${esc(GROUP_NAME[k] || k)}</td><td class="r num">${g.n}</td><td class="r num">${fx(g.exp, 0)}</td><td class="r num ${g.won < g.exp * 0.93 ? 'loss' : g.won > g.exp * 1.07 ? 'win' : ''}">${fx(g.won, 0)}</td><td class="r num ${g.val_pnl >= 0 ? 'win' : 'loss'}">${g.val_n ? `${g.val_n} · ${g.val_pnl >= 0 ? '+' : ''}${fx(g.val_pnl * 10, 0)} €` : '–'}</td></tr>`; }).join('')}</table><p class="faint small">«Las de valor»: lo que habrías ganado con 10 € en cada una que la web marcaba con valor.</p></div>
        <div><h3>¿Acierta sus porcentajes?</h3><table><tr><th>Cuando decía</th><th class="r">Apuestas</th><th class="r">Salió de verdad</th></tr>${Object.keys(bu).sort().map(k => { const b_ = bu[k]; return `<tr><td>${k * 10}–${k * 10 + 10} %</td><td class="r num">${b_.n}</td><td class="r num">${Math.round(b_.won / b_.n * 100)} % <small class="faint">(esperado ${Math.round(b_.exp / b_.n * 100)} %)</small></td></tr>`; }).join('')}</table></div></div><p class="faint small">${B.rows_n} partidos comparados mercado a mercado.</p>`
        : '<div class="note">Empieza con los partidos que se jueguen a partir de ahora: hace falta guardar lo que decía la web antes del saque. Los de esta noche ya acabados solo se comparan en estadísticas de equipo y jugadores.</div>'}</section>`); }).catch(() => {});
  const rv = await api('/api/review', { keep: true }).catch(() => ({}));
  if (seq !== navSeq || !$('.learn')) return;
  const SN = { arbitro: 'El árbitro', baja: 'Bajas oficiales', campo: 'Casa / fuera', competicion: 'Rendimiento en esa competición', consenso: 'Modelo y casas coinciden',
    cruce: 'Cruces de estadísticas', duelo: 'Duelo con su par', equipo: 'Lo que hace el equipo', faltas_directas: 'Lanzador de faltas', fichaje: 'Fichaje reciente',
    forma: 'Forma del equipo', minutos: 'Minutos esperados', modelo: 'Goles esperados', movimiento: 'Movimiento de la cuota', noticia: 'Noticias de bajas',
    once: 'Alineación', penaltis: 'Lanzador de penaltis', precio: 'Precio frente a lo justo', racha: 'Racha reciente', racha10: 'Racha (con suplencias)',
    rival: 'Lo que concede el rival', rol: 'Cambio de rol (mapas de calor)', rotacion: 'Riesgo de rotación', serie: 'Serie larga', simulacion: 'Simulaciones',
    suplencia: 'Titularidad dudosa', tendencia: 'Tendencia últimos 5', xg: 'xG de los equipos',
    afiladas: 'Casas afiladas (Pinnacle/Betfair)', aviso_cuota: 'Cuota de bet365 disparada', tendencia_area: 'Toques en el área (tendencia)',
    jugarse: 'Lo que se juega cada uno', area: 'Presencia en el área (mapas de calor)', bajas: 'Bajas del equipo', clima: 'El tiempo' };
  const ws = Object.entries(rv.weights || {}).sort((a, b) => b[1].n - a[1].n);
  const rs = Object.entries(rv.reasons || {}).sort((a, b) => b[1] - a[1]);
  $('.learn').insertAdjacentHTML('afterend', `<section class="learn panel"><div class="row"><h2 style="margin:0">Revisión apuesta por apuesta</h2><span class="sp"></span><button class="btn sm" id="rvRun">Revisar ahora</button></div>
    <p class="muted">Cada día la web vuelve a mirar todas las recomendaciones resueltas de los últimos 10 días: rehace su análisis con lo que se sabía antes del partido, mira qué pasó de verdad (minutos del jugador, rojas, goleadas, si se quedó a una) y ajusta cuánto pesa cada tipo de dato en los análisis siguientes.</p>
    ${rv.reviewed ? `<p><b>${rv.reviewed}</b> apuestas revisadas ${F.ago(rv.ts)}.</p>
      <div class="cols c2"><div><h3>Por qué salieron o no</h3><table>${rs.map(([k, n]) => `<tr><td>${esc(k)}</td><td class="r num">${n}</td></tr>`).join('')}</table></div>
      <div><h3>Qué datos están funcionando</h3><table><tr><th>Dato</th><th class="r">Veces</th><th class="r">Ahora pesa</th></tr>${ws.map(([k, v]) => `<tr><td>${esc(SN[k] || k)}</td><td class="r num">${v.n}</td><td class="r ${v.w > 1.05 ? 'win' : v.w < 0.95 ? 'loss' : 'faint'}">${v.w > 1.05 ? 'más' : v.w < 0.95 ? 'menos' : 'igual'} (×${fx(v.w, 2)})</td></tr>`).join('')}</table></div></div>`
      : '<div class="note">Todavía no hay apuestas resueltas que revisar. La primera revisión llega en cuanto terminen los partidos.</div>'}</section>`);
  const b = $('#rvRun');
  if (b) b.onclick = async () => { b.disabled = true; b.textContent = 'Revisando…'; const t0 = rv.ts || 0; await api('/api/review/run', { method: 'POST', keep: true });
    for (let k = 0; k < 40; k++) { await new Promise(r => setTimeout(r, 6000)); if (seq !== navSeq) return; const r2 = await api('/api/review', { keep: true }).catch(() => ({})); if ((r2.ts || 0) > t0) { toast(`Revisadas ${r2.reviewed} apuestas resueltas.`); routes.historial(); return; } }
    b.disabled = false; b.textContent = 'Revisar ahora'; };
};


/* ---------- «Ya la he hecho»: una hoja sencilla en vez de ventanas del navegador ---------- */
window.madeBet = i => {
  const v = REC[i]; if (!v) return;
  const st = stakeOf(v) || 1;
  const d = document.createElement('div');
  d.className = 'sheet-bg';
  d.innerHTML = `<form class="sheet" role="dialog" aria-label="Apuntar apuesta">
    <h2>Apuntar en «Mis apuestas»</h2><p class="muted">${esc(plainLabel(v.label))} · ${esc(v.match.home_name)} – ${esc(v.match.away_name)}</p>
    <label class="field">¿A qué cuota te la dio bet365?<input id="mbO" inputmode="decimal" value="${fe(v.b365)}"></label>
    <label class="field">¿Cuánto apostaste (€)?<input id="mbS" inputmode="decimal" value="${String(st).replace('.', ',')}"></label>
    <p class="small muted">Se cobra o se pierde sola al acabar el partido y la verás en «Mis apuestas».</p>
    <div class="row"><button class="btn go" type="submit">Guardar</button><button class="btn" type="button" id="mbX">Cancelar</button></div></form>`;
  document.body.appendChild(d);
  const close = () => d.remove();
  d.onclick = e => { if (e.target === d) close(); };
  $('#mbX', d).onclick = close;
  $('#mbO', d).focus();
  $('form', d).onsubmit = async e => {
    e.preventDefault();
    const num = s => parseFloat(String(s).replace(',', '.'));
    try {
      await api('/api/bets/manual', { method: 'POST', keep: true, body: { match_id: v.match.id, market: v.market, selection: v.selection, line: v.line, period: v.period,
        odds: num($('#mbO', d).value), stake: num($('#mbS', d).value), label: v.label, fair_prob: v.p } });
      close(); toast('Apuntada. Se resuelve sola al acabar el partido.');
    } catch (err) { toast('No se pudo guardar: ' + err.message); }
  };
};

/* ---------- EN DIRECTO: avisos con base estadística y proyección de equipos y jugadores ---------- */
const _liveCard = liveCard;
liveCard = s => {
  const base = _liveCard(s);
  const sig = (s.props && s.props.signals) || [];
  const extra = `<div class="lp">${sig.length ? `<div class="lp-t">Lo que vemos venir (con base en lo que está pasando)</div>${sig.slice(0, 5).map(liveSig).join('')}` : '<div class="faint small">Calculando proyecciones… (se actualizan cada minuto)</div>'}
    <a class="btn sm go" href="#/envivo/${s.match_id}">Ver todo lo que proyectamos</a></div>`;
  return base.replace(/<\/div>\s*$/, extra + '</div>');
};
function liveSig(x) {
  return `<div class="lsig ${x.odds ? 'has' : ''}"><b>${esc(x.title)}</b><span>${esc(x.why)}</span>
    <div class="lsig-n"><em>Sale ${Math.round(x.p * 100)} %</em>${x.odds ? `<em>${x.books && x.books.includes('bet365') ? `bet365 ahora ${fe(x.odds)}` : `Cuota típica ahora ${fe(x.odds)}${x.books && x.books.length ? ` (${esc(x.books.slice(0, 3).join(', '))})` : ''}`}</em><strong>Entra si bet365 te da ${fe(x.min_odds)} o más</strong>` : `<strong>Entra si bet365 paga ${fe(x.min_odds)} o más</strong>`}</div></div>`;
}
function liveExtras(d, m) {
  const parts = [];
  if (d.net_time) parts.push(`<p class="faint small">Tiempo neto jugado (balón en juego): <b>${fx(d.net_time.net, 1)}</b> de ${fx(d.net_time.total, 1)} min (${Math.round(d.net_time.ratio * 100)} %). Un partido muy cortado deja menos tiempo real para marcar y suele traer más añadido. Fuente: 365Scores.</p>`);
  if ((d.shots || []).length) {
    const W = 420, H = 272, X = v => v / 100 * W, Y = v => v / 100 * H;
    const dots = d.shots.map(s => { const x = s.side === 'H' ? X(s.x) : W - X(s.x), y = s.side === 'H' ? Y(s.y) : H - Y(s.y);
      const r = 3 + 16 * Math.sqrt(Math.max(0, s.xg || 0)), on = s.res === 'on' || s.res === 'goal';
      const c = s.side === 'H' ? 'var(--accent, #2f6fde)' : '#d9822b';
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r.toFixed(1)}" fill="${on ? c : 'none'}" fill-opacity="${s.res === 'goal' ? 0.95 : 0.55}" stroke="${c}" stroke-width="${s.res === 'goal' ? 3 : 1.5}"><title>${esc(s.player || '')} ${Math.round(s.minute)}' · xG ${fx(s.xg, 2)} · ${s.body === 'head' ? 'cabeza' : 'pie'} · ${s.zone === 'out' ? 'fuera del área' : 'dentro del área'} · ${({ goal: 'gol', on: 'a puerta', off: 'fuera', blocked: 'bloqueado' })[s.res] || ''}</title></circle>`; }).join('');
    const xg = side => d.shots.filter(s => s.side === side).reduce((a, s) => a + (s.xg || 0), 0);
    const pitch = `<rect x="1" y="1" width="${W - 2}" height="${H - 2}" fill="none" stroke="currentColor" stroke-opacity=".25"/><line x1="${W / 2}" y1="1" x2="${W / 2}" y2="${H - 1}" stroke="currentColor" stroke-opacity=".25"/><circle cx="${W / 2}" cy="${H / 2}" r="36" fill="none" stroke="currentColor" stroke-opacity=".25"/><rect x="1" y="${Y(20.4)}" width="${X(15.7)}" height="${Y(59.2)}" fill="none" stroke="currentColor" stroke-opacity=".25"/><rect x="${W - 1 - X(15.7)}" y="${Y(20.4)}" width="${X(15.7)}" height="${Y(59.2)}" fill="none" stroke="currentColor" stroke-opacity=".25"/>`;
    parts.push(`<h2 class="sec-t">Mapa de tiros</h2><p class="faint small">${esc(m.home_name)} ataca hacia la derecha (azul, xG ${fx(xg('H'), 2)}) y ${esc(m.away_name)} hacia la izquierda (naranja, xG ${fx(xg('A'), 2)}). Cuanto más grande, más xG; relleno = a puerta; borde grueso = gol. Fuente: ${esc(d.shots_src || '')}.</p><div class="panel"><svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:640px;display:block;margin:auto">${pitch}${dots}</svg></div>`);
  }
  if (d.zones && d.zones.H && d.zones.A) {
    const z = (t, nm) => `<div class="lb-row"><b class="num">${t.left} %</b><div><small>${esc(nm)}: por dónde ataca (izquierda · centro · derecha)</small><div class="lb-bar" style="display:flex"><i style="width:${t.left}%;opacity:.9"></i><i style="width:${t.center}%;opacity:.55"></i><i style="width:${t.right}%;opacity:.3"></i></div></div><b class="num">${t.right} %</b></div>`;
    parts.push(`<div class="livebar">${z(d.zones.H, m.home_name)}${z(d.zones.A, m.away_name)}</div>`);
  }
  if ((d.momentum || []).length > 5) {
    const mo = d.momentum, W = 420, H = 80, n = Math.max(...mo.map(v => v[0] || 0), 90);
    const bars = mo.map(([mi, v]) => { const h = Math.abs(v || 0) / 100 * (H / 2); return `<rect x="${(mi / n * W).toFixed(1)}" y="${v > 0 ? H / 2 - h : H / 2}" width="${(W / n * 0.8).toFixed(1)}" height="${h.toFixed(1)}" fill="${v > 0 ? 'var(--accent, #2f6fde)' : '#d9822b'}"/>`; }).join('');
    parts.push(`<h2 class="sec-t">Presión minuto a minuto</h2><p class="faint small">Arriba aprieta ${esc(m.home_name)}, abajo ${esc(m.away_name)}. Fuente: FotMob.</p><div class="panel"><svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:640px;display:block;margin:auto"><line x1="0" y1="${H / 2}" x2="${W}" y2="${H / 2}" stroke="currentColor" stroke-opacity=".3"/>${bars}</svg></div>`);
  }
  return parts.join('');
}
routes.envivo = async mid => {
  const draw = async () => {
    const d = await api(`/api/live/${mid}/props`);
    const m = await api(`/api/match/${mid}`).then(x => x.match).catch(() => ({ home_name: 'Local', away_name: 'Visitante' }));
    if (!d || !d.teams) { view.innerHTML = `<div class="panel">${empty('Todavía no hay proyecciones', 'Aparecen en cuanto empieza el partido y se actualizan cada minuto.')}</div>`; return; }
    const tl = (ls) => ls.map(l => `<span class="lchip"><b>+${String(l.line).replace('.', ',')}</b> ${Math.round(l.p * 100)} % · mín. ${fe(l.min_odds)}</span>`).join('');
    const teamRows = d.teams.map(t => `<tr><td><b>${esc(t.word[0].toUpperCase() + t.word.slice(1))}</b></td>
      <td class="r num">${t.H.now} → ${fx(t.H.exp_final, 1)}</td><td class="r num">${t.A.now} → ${fx(t.A.exp_final, 1)}</td><td class="r num"><b>${t.T.now} → ${fx(t.T.exp_final, 1)}</b></td><td>${tl(t.lines)}</td></tr>`).join('');
    const pl = [...d.players].sort((a, b) => ((b.stats.shots || {}).exp_rem || 0) - ((a.stats.shots || {}).exp_rem || 0));
    const pRows = pl.map(p => { const S = k => p.stats[k] ? `${p.stats[k].now}${p.stats[k].lines[0] ? `<small>${p.stats[k].lines[0].need + p.stats[k].now}+: ${Math.round(p.stats[k].lines[0].p * 100)} %</small>` : ''}` : '–';
      return `<tr class="prow" data-pid="${p.id}" data-name="${esc(p.name)}"><td><b>${esc(p.name)}</b><small class="faint"> ${esc(p.side === 'H' ? m.home_name : m.away_name)} · ${Math.round(p.on)}'</small></td><td class="r">${S('shots')}</td><td class="r">${S('sot')}</td><td class="r">${S('fouls')}</td><td class="r">${S('was_fouled')}</td><td class="r">${p.stats.sot_head || p.stats.sot_out ? `${S('sot_head')} / ${S('sot_out')}` : '–'}</td>
        <td class="r">${p.card ? Math.round(p.card.p * 100) + ' %' : '–'}</td><td class="r">${p.goal ? Math.round(p.goal.p * 100) + ' %' : '–'}</td></tr>`; }).join('');
    view.innerHTML = `<div class="row crumbs"><a class="faint small" href="#/directo">← En directo</a></div>
      <section class="intro"><p class="intro-date">Minuto ${Math.round(d.minute)} · quedan unos ${Math.round(d.rem)}'</p><h1>${esc(m.home_name)} ${d.score[0]} – ${d.score[1]} ${esc(m.away_name)}</h1>
      <p class="intro-sub">Proyección de lo que queda: mezcla lo que esperábamos antes del partido con lo que está pasando hoy, corregido por el marcador y las rojas. Se actualiza cada minuto.</p></section>
      ${(d.signals || []).length ? `<h2 class="sec-t">Avisos</h2><div class="lsig-grid">${d.signals.map(liveSig).join('')}</div>` : ''}
      ${d.live ? `<div class="livebar">${[['poss', 'Posesión', v => Math.round(v) + ' %'], ['xg', 'Goles esperados (xG)', v => fx(v, 2)], ['box_touches', 'Toques en el área rival', v => Math.round(v)], ['big_chances', 'Ocasiones claras', v => Math.round(v)], ['saves', 'Paradas del portero', v => Math.round(v)], ['offsides', 'Fueras de juego', v => Math.round(v)], ['crosses', 'Centros', v => Math.round(v)], ['tackles', 'Entradas', v => Math.round(v)]]
        .concat(d.mom && d.mom.share_h != null ? [['_mom', "Presión últimos 10' (xG + córners)", v => fx(v, 2)]] : [])
        .map(r => { if (r[0] === '_mom') { d.live.H._mom = d.mom.H; d.live.A._mom = d.mom.A; } return r; })
        .filter(([k]) => d.live.H[k] != null && d.live.A[k] != null).map(([k, t, f]) => { const h = +d.live.H[k], a = +d.live.A[k], sh = h + a ? h / (h + a) * 100 : 50;
          return `<div class="lb-row"><b class="num">${f(h)}</b><div><small>${t}</small><div class="lb-bar"><i style="width:${sh}%"></i></div></div><b class="num">${f(a)}</b></div>`; }).join('')}</div>` : ''}
      ${liveExtras(d, m)}
      <h2 class="sec-t">Mapas de calor en directo</h2>
      <p class="faint small">Dónde está jugando cada equipo ahora mismo (los dos atacan hacia la derecha). Pulsa un jugador de la tabla de abajo para ver el suyo.</p>
      <div class="hm2"><div><h3>${esc(m.home_name)}</h3><div id="hmH" class="hmbox">Cargando…</div></div><div><h3>${esc(m.away_name)}</h3><div id="hmA" class="hmbox">Cargando…</div></div></div>
      <div id="hmP"></div>
      <h2 class="sec-t">Equipos: ahora → final esperado</h2>
      <div class="panel scroll"><table><tr><th></th><th class="r">${esc(m.home_name)}</th><th class="r">${esc(m.away_name)}</th><th class="r">Total</th><th>Líneas del total (prob. y cuota mínima)</th></tr>${teamRows}</table></div>
      <h2 class="sec-t">Jugadores en el campo</h2>
      <p class="faint small">Lo que lleva cada uno y la probabilidad de llegar a la siguiente cifra. Tarjeta y gol: probabilidad de que le pase en lo que queda.</p>
      <div class="panel scroll"><table class="ptab"><tr><th>Jugador</th><th class="r">Tiros</th><th class="r">A puerta</th><th class="r">Faltas</th><th class="r">Le hacen faltas</th><th class="r" title="Tiros a puerta de cabeza / desde fuera del área">A puerta: cabeza / fuera</th><th class="r">Tarjeta</th><th class="r">Gol</th></tr>${pRows}</table></div>`;
  };
  const heat = async (sel, q) => {
    const el = $(sel); if (!el) return;
    try { const h = await api(`/api/live/${mid}/heatmap?${q}`, { keep: true });
      el.innerHTML = h.grid ? pitchSVG2(heatCells(h.grid, 'hm')) : '<span class="faint small">Sofascore aún no lo ha publicado</span>'; }
    catch (e) { el.innerHTML = '<span class="faint small">No disponible</span>'; }
  };
  const wire = () => {
    heat('#hmH', 'side=H'); heat('#hmA', 'side=A');
    $$('.prow').forEach(r => r.onclick = () => { $('#hmP').innerHTML = `<div class="panel"><h3>${r.dataset.name}: su mapa de calor hoy</h3><div id="hmPP" class="hmbox">Cargando…</div></div>`; heat('#hmPP', 'pid=' + r.dataset.pid); $('#hmP').scrollIntoView({ behavior: 'smooth' }); });
  };
  await draw(); wire(); timers.push(setInterval(async () => { await draw(); wire(); }, 60000));
};

/* ---------- MI BANCA: también los avisos al móvil ---------- */
const _ajustes = ajustes;
routes.ajustes = async () => {
  await _ajustes();
  const st = await api('/api/telegram', { keep: true }).catch(() => ({}));
  view.insertAdjacentHTML('beforeend', `<section class="panel section tgp"><h2>Avisos al móvil (Telegram, gratis)</h2>
    <p class="muted">${st.token && st.chat ? '<b class="win">Conectado.</b> Cada mañana te llegan las apuestas del día y, durante los partidos, los avisos en directo.' : 'Cada mañana te llegan las apuestas del día y, durante los partidos, los avisos en directo.'}</p>
    <ol class="tg-steps"><li>En Telegram, abre <b>@BotFather</b>, escribe <b>/newbot</b> y ponle un nombre. Te dará un <b>token</b> (algo como 123456:ABC…).</li>
      <li>Pega el token aquí y pulsa Conectar.</li><li>Abre tu bot nuevo, escríbele «hola» y vuelve a pulsar Conectar.</li></ol>
    <div class="row"><input id="tgTok" placeholder="Token del bot" style="max-width:420px" autocomplete="off"><button class="btn go" id="tgGo">Conectar</button></div>
    <p class="small" id="tgMsg"></p>
    <h3>¿En un grupo?</h3>
    <p class="muted small">${st.group ? `Ahora los avisos van al grupo <b>«${esc(st.group)}»</b>. ` : ''}Crea el grupo en Telegram, añade tu bot como miembro, escribe en el grupo <b>/start@nombre_de_tu_bot</b> y pulsa «Usar grupo». Todos los del grupo recibirán los avisos. Para volver a tu chat privado, pulsa Conectar.</p>
    <button class="btn" id="tgGroup">Usar grupo</button></section>`);
  $('#tgGo').onclick = async () => {
    $('#tgMsg').textContent = 'Conectando…';
    try { const r = await api('/api/telegram', { method: 'POST', keep: true, body: { token: $('#tgTok').value || null } }); $('#tgMsg').textContent = r.msg; }
    catch (e) { $('#tgMsg').textContent = e.message; }
  };
  $('#tgGroup').onclick = async () => {
    $('#tgMsg').textContent = 'Buscando el grupo…';
    try { const r = await api('/api/telegram', { method: 'POST', keep: true, body: { token: $('#tgTok').value || null, group: true } }); $('#tgMsg').textContent = r.msg; }
    catch (e) { $('#tgMsg').textContent = e.message; }
  };
};

/* ---------- PARTIDOS: primero lo que viene, agrupado por día, y los resultados aparte ---------- */
routes.partidos = async () => {
  const ms = await api('/api/matches?days=7&past=2');
  const comps = [...new Set(ms.map(m => m.comp))];
  let comp = '', mode = 'next';
  view.innerHTML = `<section class="intro"><h1>Partidos</h1><p class="intro-sub">Pulsa un partido para ver cómo creemos que irá, qué apostar y por qué. El número verde son las apuestas que recomendamos en ese partido.</p></section>
    <div class="row" style="flex-wrap:wrap;gap:8px"><div class="seg" id="pmode"><button class="on" data-m="next">Próximos</button><button data-m="live">En juego</button><button data-m="done">Resultados</button></div>
    <select id="pcomp" style="width:auto"><option value="">Todas las competiciones</option>${comps.map(c => `<option value="${c}">${esc(COMP[c] || c)}</option>`).join('')}</select></div>
    <div id="plist" class="section"></div>`;
  const draw = () => {
    const pick = ms.filter(m => (!comp || m.comp === comp) && (mode === 'next' ? m.status === 'notstarted' && m.kickoff > Date.now() / 1000 - 3 * 3600 : mode === 'live' ? m.status === 'inprogress' : m.status === 'finished'));
    const by = {};
    (mode === 'done' ? [...pick].reverse() : pick).forEach(m => { const k = F.day(m.kickoff); (by[k] = by[k] || []).push(m); });
    $('#plist').innerHTML = Object.keys(by).length ? Object.entries(by).map(([d, l]) => `<h2 class="sec-t">${d.charAt(0).toUpperCase() + d.slice(1)} <small class="faint">${l.length}</small></h2><div class="mini-fx">${l.map(m => mode === 'done' ? resultRow(m) : miniFixture(m)).join('')}</div>`).join('')
      : `<div class="panel">${empty(mode === 'live' ? 'Ahora no hay partidos en juego' : 'Nada por aquí', mode === 'live' ? 'Cuando empiece uno lo verás aquí y en «En directo».' : 'Prueba con otra competición.')}</div>`;
  };
  segWire('pmode', b => { mode = b.dataset.m; draw(); });
  $('#pcomp').onchange = e => { comp = e.target.value; draw(); };
  draw();
};
function resultRow(m) {
  return `<a class="mfx" href="#/partido/${m.id}"><span class="mfx-t">${m.hg ?? '–'}-${m.ag ?? '–'}<small>${esc(COMP[m.comp] || m.comp)}</small></span>
    <span class="mfx-n">${crest(m.home_name, m.colors && m.colors.H, 26)}${esc(m.home_name)}<br>${crest(m.away_name, m.colors && m.colors.A, 26)}${esc(m.away_name)}</span><span></span><span class="mfx-v faint">ver</span></a>`;
}


/* ---------- ESCALERA: tres combinadas del mismo partido, cada vez más difíciles y con menos dinero ---------- */
function ladderCard(L, withMatch) {
  const unit = Math.max(1, Math.round(USER.bankroll * 0.01 * USER.risk * 2) / 2);
  const m = L.match;
  return `<article class="ladder">
    ${withMatch ? `<header class="tk-match"><span class="tk-teams">${crest(m.home_name, null, 28)}<b>${esc(m.home_name)}</b><span class="faint">vs</span><b>${esc(m.away_name)}</b>${crest(m.away_name, null, 28)}</span><span class="tk-when">${F.day(m.kickoff).split(' ')[0]} ${F.hm(m.kickoff)} · ${esc(COMP[m.comp] || m.comp)}</span></header>` : ''}
    <p class="lad-intro">Como se hace en Statshub: jugadores con racha (superan la línea de +1,5 o +2,5 en el 75 % o más de sus últimos partidos) y que en esa línea pagan 1,75 o más. Peldaño 1: una línea más abajo (paga 1,20 o más y sale casi siempre). Peldaño 2: la línea de la racha. Peldaño 3: una más arriba. El dinero baja a la mitad en cada peldaño.</p>${L.level === 'suave' ? '<p class="lad-intro draw-c">En este partido no hay ninguna racha del 75 % que pague 1,75; esta usa un criterio algo más suave (racha del 70 % y cuota de 1,50 o más).</p>' : ''}${(L.chains || []).length ? `<ul class="lad-ch">${L.chains.map(c => `<li><b>${esc(c.player || '')}</b>: más de ${fx(c.line, 1)} ${({ shots: 'tiros', sot: 'tiros a puerta', fouls: 'faltas', fouled: 'faltas recibidas', tackles: 'entradas' })[c.kind] || c.kind} en <b>${Math.round(c.streak * c.n)} de sus últimos ${c.n}</b>${c.streak_low != null ? `, más de ${fx(c.line - 1, 1)} en ${Math.round(c.streak_low * c.n)} de ${c.n}` : ''}${c.mu != null ? ` · esperamos ${fx(c.mu, 1)}` : ''}</li>`).join('')}</ul>` : ''}
    <div class="lad-steps">${L.steps.map((st, i) => { const eur = Math.max(0.5, Math.round(unit * st.stake_u * 2) / 2);
      return `<div class="lad-step s${i + 1}"><div class="lad-h"><b>Peldaño ${i + 1}</b><span>Pon ${String(eur).replace('.', ',')} €</span></div>
        <ul>${st.legs.map(lg => `<li>${esc(plainLabel(lg.label))} <small>(${Math.round(lg.p * 100)} %)</small></li>`).join('')}</ul>
        <div class="lad-n"><span>Sale <b>${Math.round(st.p * 100)} %</b> de las veces${st.p_sub && st.p_sub > st.p + 0.01 ? ` · <b>${Math.round(st.p_sub * 100)} %</b> con Sustituto+` : ''}</span><span>Cuota estimada <b>${fe(st.odds_est)}</b></span><span>Hazla si bet365 te da <b>${fe(st.min_odds)}</b> o más</span></div></div>`; }).join('')}</div>
    <p class="faint small">Las probabilidades salen del modelo de cada jugador (sus partidos, minutos, rival y duelo). bet365 rebaja la cuota de las combinadas del mismo partido; por eso te damos la cuota mínima de cada peldaño.</p>
  </article>`;
}

/* ---------- después del partido: lo que esperábamos frente a lo que pasó ---------- */
function postmatchHtml(pm) {
  const g = pm.goals || {};
  const pl = (pm.players || []).filter(p => p.min > 0);
  const K = [['shots', 'Tiros'], ['sot', 'A puerta'], ['fouls', 'Faltas'], ['was_fouled', 'Faltas recibidas'], ['tackles', 'Entradas']];
  const diff = (e, a) => { if (e == null || a == null) return ''; const d = a - e; return Math.abs(d) < Math.max(0.6, e * 0.25) ? 'faint' : d > 0 ? 'win' : 'loss'; };
  // los jugadores donde más fallamos (con los minutos que jugaron de verdad)
  const err = pl.map(p => ({ p, e: K.reduce((s, [k]) => s + Math.abs((p.act[k] ?? 0) - (p.exp_at_min[k] ?? 0)), 0) })).sort((a, b) => b.e - a.e);
  const subTxt = p => p.sub ? (p.sub.in != null ? `entró ${p.sub.in}′` : `salió ${p.sub.out}′`) : '';
  const heatTxt = p => { const h = p.heat; if (!h) return ''; const dx = h.x - h.px; return Math.abs(dx) < 6 ? 'donde siempre' : dx > 0 ? `${Math.round(dx)} m más adelantado` : `${Math.round(-dx)} m más atrás`; };
  const mk = pm.markets || [], MS = pm.mk_summary || {};
  const mkGroups = [...new Set(mk.map(r => r.g))];
  return `<div class="panel pm"><h2>Lo que esperábamos y lo que pasó</h2>
    <p class="muted">Antes del partido guardamos lo que creía el modelo. Al acabar descargamos todas las estadísticas y las comparamos. Separamos dos errores: que un jugador juegue menos minutos de lo previsto (no es culpa de su ritmo) y que con esos minutos haga más o menos de lo esperado. Con esto el modelo se corrige solo, poco a poco.</p>
    ${g.exp && g.exp[0] != null ? `<p>Goles esperados <b class="num">${fx(g.exp[0], 2)} – ${fx(g.exp[1], 2)}</b> · Resultado <b class="num">${g.act[0]} – ${g.act[1]}</b>${g.p1x2 ? ` <span class="faint">(dábamos 1 ${Math.round(g.p1x2[0] * 100)} %, X ${Math.round(g.p1x2[1] * 100)} %, 2 ${Math.round(g.p1x2[2] * 100)} %)</span>` : ''}</p>` : ''}
    ${(pm.teams || []).length ? `<table><tr><th>Estadística</th><th class="r">Esperábamos</th><th class="r">Pasó</th></tr>${pm.teams.map(t => `<tr><td>${esc(t.word)}</td><td class="r num">${fx(t.exp[0], 1)} – ${fx(t.exp[1], 1)}</td><td class="r num ${diff(t.exp[0] + t.exp[1], t.act[0] + t.act[1])}">${t.act[0]} – ${t.act[1]}</td></tr>`).join('')}</table>` : ''}
    ${err.length ? `<h3>Cada jugador (primero, donde más se desvió)</h3><div class="tw"><table class="pmt"><tr><th>Jugador</th><th class="r">Minutos</th>${K.map(([, n]) => `<th class="r">${n}</th>`).join('')}<th>Cambio · mapa de calor</th></tr>${err.map(({ p }) => `<tr><td>${esc(p.name)}</td><td class="r num ${p.min < p.exp_min - 15 ? 'loss' : 'faint'}">${p.min}′ <small>(${Math.round(p.exp_min)}′)</small></td>${K.map(([k]) => `<td class="r num ${diff(p.exp_at_min[k], p.act[k])}">${p.act[k] ?? '–'} <small class="faint">/${fx(p.exp_at_min[k], 1)}</small></td>`).join('')}<td class="faint small">${[subTxt(p), heatTxt(p)].filter(Boolean).join(' · ')}</td></tr>`).join('')}</table></div><p class="faint small">Cifra real / lo que esperábamos con los minutos que jugó. Entre paréntesis, los minutos que le dábamos. El mapa de calor se compara con su posición media de los últimos meses.</p>` : ''}
    ${mk.length ? `<h3>Cada mercado que enseñaba la web: ¿salió?</h3><p class="faint small">${mk.length} apuestas de este partido con la probabilidad que les dábamos antes del saque.</p>
      ${mkGroups.map(gk => { const rs = mk.filter(r => r.g === gk).sort((a, b) => b.p - a.p), S = MS[gk] || {};
        return `<details><summary>${esc(GROUP_NAME[gk] || gk)} · esperábamos ${fx(S.exp, 1)} de ${S.n}, salieron ${fx(S.won, 0)}${S.val_n ? ` · las de valor: ${S.val_pnl >= 0 ? '+' : ''}${fx(S.val_pnl * 10, 0)} € con 10 €` : ''}</summary>
          <table class="mk"><tr><th>Apuesta</th><th class="r">Le dábamos</th><th class="r">Cuota</th><th>¿Salió?</th></tr>${rs.map(r => `<tr><td>${esc(plainLabel(r.lb))}${r.ev > 0.02 ? ' <small class="win">valor</small>' : ''}</td><td class="r num">${Math.round(r.p * 100)} %</td><td class="r num">${fe(r.o)}</td><td class="${r.pv >= 0.99 ? 'faint' : r.w >= 0.99 ? 'w' : r.w > 0 ? 'w' : 'l'}">${r.pv >= 0.99 ? 'Devuelta' : r.w >= 0.99 ? 'Sí' : r.w > 0 ? 'Media' : 'No'}</td></tr>`).join('')}</table></details>`; }).join('')}` : ''}
  </div>`;
}


/* ---------- CRÉDITOS DE APUESTA: dónde meterlos ---------- */
routes.creditos = async () => {
  let amt = 10;
  try { amt = +(localStorage.getItem('credAmt') || 10); } catch (e) {}
  view.innerHTML = ph('Créditos de apuesta', 'Con créditos de bet365, si aciertas cobras solo la ganancia: el crédito no vuelve. Por eso no se usan igual que el dinero: lo que cuenta es la probabilidad por la ganancia (cuota − 1), y suelen convenir cuotas algo más altas, entre 2 y 6, siempre que la probabilidad sea fiable.') +
    `<section class="panel cred-in"><label>¿Cuántos créditos tienes? <span class="cred-box"><input id="credAmt" type="number" min="1" step="0.5" value="${amt}"> €</span></label>
     <button class="btn go" id="credGo">Dime dónde meterlos</button></section><div id="credOut"></div>`;
  const run = async () => {
    const a = Math.max(1, +$('#credAmt').value || 10);
    try { localStorage.setItem('credAmt', a); } catch (e) {}
    $('#credOut').innerHTML = '<div class="panel faint">Buscando entre todas las apuestas de las próximas 48 horas…</div>';
    const r = await api(`/api/credits?amount=${a}`).catch(() => null);
    if (!r || !r.single) { $('#credOut').innerHTML = `<div class="panel">${empty('Ahora mismo no hay una buena opción', 'Vuelve en un rato: cuando se publiquen las cuotas de los próximos partidos aparecerán aquí.')}</div>`; return; }
    const card = (b) => `<div class="cred-bet"><div class="cred-m">${crest(b.match.home_name, null, 22)}<span>${esc(b.match.home_name)} – ${esc(b.match.away_name)}</span><small>${F.day(b.match.kickoff).split(' ')[0]} ${F.hm(b.match.kickoff)}</small></div>
      <b class="cred-l">${esc(plainLabel(b.label))}</b>
      <div class="cred-n"><span>Pon <b>${fe(b.stake)} €</b> de crédito</span><span>Cuota bet365 <b>${fe(b.b365)}</b></span><span>Sale <b>${Math.round((b.p_eff || b.p) * 100)} %</b></span><span>Si sale cobras <b>${fe(b.stake * (b.b365 - 1))} €</b></span></div>
      <details class="cred-why" open><summary>Por qué esta</summary><ul>${(b.why || []).map(w => `<li>${esc(w)}</li>`).join('')}${b.dossier ? b.dossier.pro.map(t => `<li class="up">${esc(t)}</li>`).join('') + b.dossier.con.slice(0, 1).map(t => `<li class="down">${esc(t)}</li>`).join('') : ''}</ul>
        ${b.dossier ? '' : `<ul class="tk-why" data-mid="${b.match.id}" data-q="${esc(qOf(b))}"><li class="ld">Cruzando los datos del partido…</li></ul>`}
        <a class="btn sm" href="${betHref(b.match.id, b)}">Ver el análisis completo</a></details></div>`;
    const opt = (o, title, sub, on) => o ? `<article class="panel cred-opt ${on ? 'rec' : ''}">${on ? '<div class="tk-flag">Lo que te recomiendo</div>' : ''}<h2>${title}</h2><p class="muted">${sub}</p>
      ${o.bets.map(card).join('')}
      <div class="cred-sum"><span>A la larga te vale <b>${fe(o.exp)} €</b> en dinero de verdad</span><span>Cobras algo el <b>${Math.round(o.p_any * 100)} %</b> de las veces</span></div></article>` : '';
    const s1 = r.rec === 'single';
    $('#credOut').innerHTML = `<div class="cols c2">
      ${opt(r.single, 'Todo a una apuesta', 'La que más vale por cada euro de crédito.', s1)}
      ${opt(r.split, `Repartido en ${r.split ? r.split.bets.length : 0} apuestas`, 'Partidos distintos: casi el mismo valor y más veces cobras algo.', !s1)}</div>
      <p class="faint small">${s1 ? (r.split ? 'Repartir apenas sube las veces que cobras y pierde valor: mejor todo a una.' : 'No hay otra apuesta casi igual de buena en otro partido: mejor todo a una.') : 'Repartir cuesta muy poco valor y hace que cobres algo muchas más veces: mejor repartido.'}
      Comprueba la cuota en bet365 antes; si ha bajado mucho, vuelve a pedir la recomendación. Si tu promoción exige una cuota mínima, elige una que la cumpla.</p>
      ${r.cands.length > 3 ? `<details class="panel"><summary>Otras buenas opciones</summary>${r.cands.slice(1, 10).map(c => `<div class="cred-alt"><span>${esc(c.match.home_name)} – ${esc(c.match.away_name)}: <b>${esc(plainLabel(c.label))}</b></span><span>cuota ${fe(c.b365)} · sale ${Math.round(c.p * 100)} % · vale ${fe(c.conv * 10)} € por cada 10 €</span></div>`).join('')}</details>` : ''}`;
  };
  const run0 = run;
  const runAndReasons = async () => { await run0(); fillReasons($('#credOut')); };
  $('#credGo').onclick = runAndReasons;
  $('#credAmt').onkeydown = e => { if (e.key === 'Enter') runAndReasons(); };
  runAndReasons();
};


/* ---------- aviso del sistema cuando abre un mercado con valor (mientras la web esté abierta) ---------- */
(() => {
  let last = 0;
  try { last = +(localStorage.getItem('el_mk_last') || 0); } catch (e) {}
  if (!last) last = Math.floor(Date.now() / 1000);
  const tick = async () => {
    try {
      const r = await fetch(`/api/alerts?kind=mercado&since=${last}`, { credentials: 'same-origin' });
      if (!r.ok) return;
      const al = await r.json();
      for (const x of al.reverse()) {
        last = Math.max(last, x.ts);
        const body = x.message.split('\n').filter(l => l.startsWith('•')).slice(0, 3).join('\n').replace(/<[^>]+>/g, '');
        if ('Notification' in window && Notification.permission === 'granted') {
          const n = new Notification(x.title, { body, tag: 'mk' + x.id });
          n.onclick = () => { window.focus(); location.hash = '#/partido/' + x.match_id; };
        } else if (typeof toast === 'function') toast(x.title);
      }
      try { localStorage.setItem('el_mk_last', last); } catch (e) {}
    } catch (e) {}
  };
  setInterval(tick, 120000);
  setTimeout(tick, 15000);
})();
