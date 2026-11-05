/* Gráficos SVG propios: sin dependencias externas. */
const SVGNS = 'http://www.w3.org/2000/svg';
const C = { chalk: '#10221b', chalk2: '#46564e', chalk3: '#7f8c85', line: '#dde3dc', value: '#127a49', win: '#127a49', loss: '#c13f2c', info: '#2c5d8f', draw: '#9a6a00', turf: '#ffffff', pitch: '#e6ebe5' };
const PITCH_TXT = '#eef2ea';

function hexOk(c) { return typeof c === 'string' && /^#[0-9a-f]{6}$/i.test(c); }
function lum(hex) { const n = parseInt(hex.slice(1), 16); const r = n >> 16 & 255, g = n >> 8 & 255, b = n & 255; return (0.299 * r + 0.587 * g + 0.114 * b) / 255; }
/* color de equipo legible sobre fondo claro (los blancos y amarillos pálidos no se ven) */
function teamColor(c, fallback) {
  if (!hexOk(c)) return fallback;
  return lum(c) > 0.78 ? fallback : c;
}

/* Escudo genérico con los colores del club e iniciales (no se usan logos reales). */
function crest(name, colors = [], size = 36) {
  const tid = window.TEAM_IDS && window.TEAM_IDS[name];
  const L = Math.round(Math.max(28, size * 1.35));   // los escudos reales llevan aire alrededor: se pintan más grandes
  if (tid) return `<img class="crest logo" src="/api/team/${tid}/logo" width="${L}" height="${L}" alt="" loading="lazy" data-n="${String(name).replace(/"/g, '&quot;')}" onerror="crestFail(this)">`;
  return crestSvg(name, colors, size);
}
function crestFail(el) { el.outerHTML = crestSvg(el.dataset.n, [], +el.getAttribute('width') || 36); }
function crestSvg(name, colors = [], size = 36) {
  colors = colors || [];
  const c1 = hexOk(colors[0]) ? colors[0] : '#2f5d50', c2 = hexOk(colors[1]) ? colors[1] : '#eef2ea';
  const ini = (name || '?').replace(/^(FC|CF|AC|SC|RC|CD|UD|SD|1\.)\s+/i, '').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();
  const txt = lum(c1) > 0.6 ? '#13241f' : '#ffffff';
  return `<svg class="crest" width="${size}" height="${size * 1.12}" viewBox="0 0 40 45" aria-hidden="true">
    <path d="M20 1 L38 6 V22 C38 33 30 40 20 44 C10 40 2 33 2 22 V6 Z" fill="${c1}" stroke="${c2}" stroke-width="2"/>
    <path d="M2 22 C2 33 10 40 20 44 C30 40 38 33 38 22 Z" fill="${c2}" opacity=".22"/>
    <text x="20" y="27" text-anchor="middle" font-family="Archivo, sans-serif" font-weight="700" font-size="${ini.length > 1 ? 15 : 18}" fill="${txt}">${ini}</text></svg>`;
}

/* Gráfico de líneas con tooltip. series: [{name, color, points:[[x,y]], area, dash}] */
function lineChart(el, series, o = {}) {
  const W = o.width || 760, H = o.height || 240, P = { l: 46, r: 14, t: 12, b: 26 };
  const pts = series.flatMap(s => s.points);
  if (!pts.length) { el.innerHTML = `<div class="empty"><b>Sin datos todavía</b>${o.empty || ''}</div>`; return; }
  let x0 = Math.min(...pts.map(p => p[0])), x1 = Math.max(...pts.map(p => p[0]));
  let y0 = Math.min(...pts.map(p => p[1]), o.zero ? 0 : Infinity), y1 = Math.max(...pts.map(p => p[1]), o.zero ? 0 : -Infinity);
  if (o.yMin != null) y0 = o.yMin; if (o.yMax != null) y1 = o.yMax;
  if (x1 === x0) x1 = x0 + 1; if (y1 === y0) { y1 += 1; y0 -= 1; }
  const pad = (y1 - y0) * 0.08; if (o.yMin == null) y0 -= pad; if (o.yMax == null) y1 += pad;
  const X = x => P.l + (x - x0) / (x1 - x0) * (W - P.l - P.r), Y = y => P.t + (1 - (y - y0) / (y1 - y0)) * (H - P.t - P.b);
  const yf = o.yfmt || (v => v.toFixed(1)), xf = o.xfmt || (v => v);
  let g = '';
  for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4; g += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${C.line}"/><text x="${P.l - 8}" y="${Y(v) + 4}" text-anchor="end" fill="${C.chalk3}" font-size="11">${yf(v)}</text>`; }
  for (let i = 0; i <= 4; i++) { const v = x0 + (x1 - x0) * i / 4; g += `<text x="${X(v)}" y="${H - 6}" text-anchor="middle" fill="${C.chalk3}" font-size="11">${xf(v)}</text>`; }
  if (o.zero && y0 < 0 && y1 > 0) g += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="${C.chalk3}" stroke-dasharray="3 4"/>`;
  if (o.shadeFrom != null) g += `<rect x="${X(o.shadeFrom)}" y="${P.t}" width="${W - P.r - X(o.shadeFrom)}" height="${H - P.t - P.b}" fill="rgba(242,207,79,.06)"/><text x="${X(o.shadeFrom) + 6}" y="${P.t + 14}" fill="${C.value}" font-size="11">${o.shadeLabel || ''}</text>`;
  for (const s of series) {
    if (!s.points.length) continue;
    const d = s.points.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
    if (s.area) g += `<path d="${d}L${X(s.points.at(-1)[0])},${Y(Math.max(y0, 0))}L${X(s.points[0][0])},${Y(Math.max(y0, 0))}Z" fill="${s.color}" opacity=".08"/>`;
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 2}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ''} stroke-linejoin="round"/>`;
    if (s.dots) g += s.points.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3" fill="${s.color}"/>`).join('');
  }
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${o.label || 'gráfico'}">${g}<line class="hx" y1="${P.t}" y2="${H - P.b}" stroke="${C.chalk2}" stroke-width="1" opacity="0"/><rect class="hit" x="${P.l}" y="${P.t}" width="${W - P.l - P.r}" height="${H - P.t - P.b}" fill="transparent"/></svg>`;
  const svg = el.querySelector('svg'), hx = svg.querySelector('.hx'), tip = document.getElementById('tip');
  svg.querySelector('.hit').addEventListener('mousemove', e => {
    const r = svg.getBoundingClientRect(); const xv = x0 + ((e.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * (x1 - x0);
    const rows = series.filter(s => s.points.length).map(s => { const p = s.points.reduce((a, b) => Math.abs(b[0] - xv) < Math.abs(a[0] - xv) ? b : a); return [s, p]; });
    const xs = rows[0][1][0]; hx.setAttribute('x1', X(xs)); hx.setAttribute('x2', X(xs)); hx.setAttribute('opacity', '.5');
    tip.innerHTML = `<div class="faint small">${(o.tipx || xf)(xs)}</div>` + rows.map(([s, p]) => `<div><span style="color:${s.color}">●</span> ${s.name || ''} <b>${yf(p[1])}</b></div>`).join('');
    tip.classList.remove('hidden'); tip.style.left = Math.min(e.clientX + 14, innerWidth - 240) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
  });
  svg.querySelector('.hit').addEventListener('mouseleave', () => { hx.setAttribute('opacity', 0); tip.classList.add('hidden'); });
}

/* Barras verticales (yield por temporada, etc.) */
function barChart(el, bars, o = {}) {
  const W = o.width || 560, H = o.height || 200, P = { l: 40, r: 10, t: 14, b: 34 };
  if (!bars.length) { el.innerHTML = '<div class="empty">Sin datos</div>'; return; }
  const vals = bars.map(b => b.v); const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals) || 1;
  const Y = v => P.t + (1 - (v - lo) / (hi - lo || 1)) * (H - P.t - P.b); const bw = (W - P.l - P.r) / bars.length;
  const yf = o.yfmt || (v => v.toFixed(2));
  let g = `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="${C.chalk3}"/>`;
  bars.forEach((b, i) => {
    const x = P.l + i * bw + bw * 0.18, w = bw * 0.64, y = Math.min(Y(0), Y(b.v)), h = Math.abs(Y(b.v) - Y(0));
    g += `<rect x="${x}" y="${y}" width="${w}" height="${Math.max(h, 1)}" rx="3" fill="${b.color || (b.v >= 0 ? C.win : C.loss)}" opacity="${b.dim ? .45 : .9}"/>`;
    g += `<text x="${x + w / 2}" y="${b.v >= 0 ? y - 5 : y + h + 13}" text-anchor="middle" fill="${C.chalk}" font-size="12" font-family="Archivo">${yf(b.v)}</text>`;
    g += `<text x="${x + w / 2}" y="${H - 12}" text-anchor="middle" fill="${C.chalk3}" font-size="11">${b.label}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${g}</svg>`;
}

/* Calibración: prob. predicha vs frecuencia real */
function calibChart(el, bins) {
  const W = 420, H = 320, P = 40, S = v => P + v * (W - 2 * P), T = v => H - P - v * (H - 2 * P);
  let g = `<rect x="${P}" y="${P}" width="${W - 2 * P}" height="${H - 2 * P}" fill="none" stroke="${C.line}"/>`;
  for (let i = 0; i <= 5; i++) { const v = i / 5; g += `<text x="${S(v)}" y="${H - P + 16}" text-anchor="middle" fill="${C.chalk3}" font-size="11">${Math.round(v * 100)}%</text><text x="${P - 8}" y="${T(v) + 4}" text-anchor="end" fill="${C.chalk3}" font-size="11">${Math.round(v * 100)}%</text>`; }
  g += `<line x1="${S(0)}" y1="${T(0)}" x2="${S(1)}" y2="${T(1)}" stroke="${C.chalk3}" stroke-dasharray="4 4"/>`;
  const mx = Math.max(...bins.map(b => b.n));
  bins.forEach(b => { g += `<circle cx="${S(b.pred)}" cy="${T(b.real)}" r="${4 + 8 * Math.sqrt(b.n / mx)}" fill="${C.value}" opacity=".8"><title>${b.bin}: predicho ${(b.pred * 100).toFixed(1)}%, real ${(b.real * 100).toFixed(1)}% (${b.n})</title></circle>`; });
  g += `<text x="${W / 2}" y="${H - 4}" text-anchor="middle" fill="${C.chalk2}" font-size="12">Probabilidad que dio el modelo</text>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}">${g}</svg>`;
}

/* ---------------- campo táctico ---------------- */
const DEPTH = { GK: .05, G: .05, CB: .19, LCB: .19, RCB: .19, LB: .23, RB: .23, LWB: .33, RWB: .33, CDM: .33, LCDM: .33, RCDM: .33,
  CM: .43, LCM: .43, RCM: .43, LM: .47, RM: .47, CAM: .56, LCAM: .56, RCAM: .56, LW: .72, RW: .72, LF: .75, RF: .75, LST: .82, RST: .82, ST: .84, CF: .84, F: .82, D: .2, M: .43 };
function lateral(pos) { if (/^L(?!C)/.test(pos)) return .13; if (/^LC/.test(pos)) return .34; if (/^R(?!C)/.test(pos)) return .87; if (/^RC/.test(pos)) return .66; return .5; }
function placeTeam(players, side) {
  const st = players.filter(p => p.starter).slice(0, 11);
  const pts = st.map(p => ({ p, d: DEPTH[p.pos] ?? .45, y: lateral(p.pos || '') }));
  // separa jugadores que caen en el mismo punto (dos "CB", "CM"…)
  const groups = {}; pts.forEach(q => { const k = q.d.toFixed(2) + '|' + q.y.toFixed(2); (groups[k] = groups[k] || []).push(q); });
  Object.values(groups).forEach(g => { if (g.length > 1) g.forEach((q, i) => { q.y = 0.5 + (i - (g.length - 1) / 2) * (0.7 / g.length); }); });
  return pts.map(q => ({ ...q, x: side === 'H' ? q.d * 0.5 : 1 - q.d * 0.5, y: side === 'H' ? q.y : 1 - q.y }));
}
function pitchSVG(lu, duels, names, colors) {
  const W = 1000, H = 640, m = 24, X = x => m + x * (W - 2 * m), Y = y => m + y * (H - 2 * m);
  const ch = 'rgba(238,242,234,.22)';
  let g = `<defs><pattern id="stripes" width="${(W - 2 * m) / 12}" height="10" patternUnits="userSpaceOnUse"><rect width="${(W - 2 * m) / 24}" height="10" fill="rgba(255,255,255,.018)"/></pattern></defs>
  <rect x="${m}" y="${m}" width="${W - 2 * m}" height="${H - 2 * m}" rx="6" fill="#143128"/><rect x="${m}" y="${m}" width="${W - 2 * m}" height="${H - 2 * m}" fill="url(#stripes)"/>
  <g fill="none" stroke="${ch}" stroke-width="2"><rect x="${m}" y="${m}" width="${W - 2 * m}" height="${H - 2 * m}" rx="6"/>
  <line x1="${W / 2}" y1="${m}" x2="${W / 2}" y2="${H - m}"/><circle cx="${W / 2}" cy="${H / 2}" r="70"/>
  <rect x="${m}" y="${Y(.21)}" width="${X(.155) - m}" height="${Y(.79) - Y(.21)}"/><rect x="${X(.845)}" y="${Y(.21)}" width="${X(.155) - m}" height="${Y(.79) - Y(.21)}"/>
  <rect x="${m}" y="${Y(.37)}" width="${X(.055) - m}" height="${Y(.63) - Y(.37)}"/><rect x="${X(.945)}" y="${Y(.37)}" width="${X(.055) - m}" height="${Y(.63) - Y(.37)}"/></g>`;
  const pos = {}; const placed = { H: placeTeam(lu.H || [], 'H'), A: placeTeam(lu.A || [], 'A') };
  placed.H.forEach(q => pos['H' + q.p.name] = q); placed.A.forEach(q => pos['A' + q.p.name] = q);
  (duels || []).slice(0, 6).forEach(d => {
    const a = pos[d.side + d.attacker.name], b = pos[(d.side === 'H' ? 'A' : 'H') + d.defender.name];
    if (!a || !b) return;
    const col = d.advantage > 0 ? C.value : C.info, w = 1.5 + Math.min(4, Math.abs(d.advantage) * 2);
    g += `<line x1="${X(a.x)}" y1="${Y(a.y)}" x2="${X(b.x)}" y2="${Y(b.y)}" stroke="${col}" stroke-width="${w}" stroke-dasharray="7 6" opacity=".85"><title>${d.attacker.name} vs ${d.defender.name}: ventaja ${d.advantage.toFixed(2)}</title></line>`;
  });
  for (const side of ['H', 'A']) {
    const col = hexOk(colors[side][0]) ? colors[side][0] : (side === 'H' ? '#e8e3d3' : '#8cbde8');
    const ring = hexOk(colors[side][1]) ? colors[side][1] : '#ffffff';
    const txt = lum(col) > 0.6 ? '#13241f' : '#fff';
    placed[side].forEach(q => {
      const f = q.p.form || {}; const imp = f.impact;
      const r = 17 + Math.max(0, Math.min(7, (imp || 0) * 3));
      const short = (q.p.name || '').split(' ').slice(-1)[0];
      g += `<g class="pl" transform="translate(${X(q.x)},${Y(q.y)})"><title>${q.p.name} (${q.p.pos})${imp != null ? `\nImpacto ${imp.toFixed(2)} · xG/90 ${(f.xg_p90 || 0).toFixed(2)} · regates/90 ${(f.won_contest_p90 || 0).toFixed(1)} · faltas/90 ${(f.fouls_p90 || 0).toFixed(1)}` : ''}</title>
        <circle r="${r}" fill="${col}" stroke="${ring}" stroke-width="2.5"/>
        <text y="5" text-anchor="middle" font-family="Archivo" font-weight="700" font-size="16" fill="${txt}">${q.p.jersey ?? ''}</text>
        <text y="${r + 16}" text-anchor="middle" font-family="Archivo" font-size="13" font-weight="600" fill="${PITCH_TXT}" paint-order="stroke" stroke="#143128" stroke-width="4">${short}</text></g>`;
    });
  }
  g += `<text x="${m + 10}" y="${m + 22}" fill="#cfe0d6" font-size="14" font-family="Archivo" font-weight="700">${names.H} →</text>
        <text x="${W - m - 10}" y="${H - m - 12}" text-anchor="end" fill="#cfe0d6" font-size="14" font-family="Archivo" font-weight="700">← ${names.A}</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Alineaciones y duelos sobre el campo">${g}</svg>`;
}

/* mini sparkline */
function spark(values, color = C.value, w = 120, h = 32) {
  if (!values || values.length < 2) return '';
  const lo = Math.min(...values), hi = Math.max(...values), d = values.map((v, i) => `${i ? 'L' : 'M'}${(i / (values.length - 1) * w).toFixed(1)},${(h - 2 - (v - lo) / ((hi - lo) || 1) * (h - 4)).toFixed(1)}`).join('');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.8"/></svg>`;
}
