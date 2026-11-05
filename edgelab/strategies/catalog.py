"""Catálogo semilla de estrategias.

Incluye todas las del documento "Catálogo de estrategias de apuestas" (P01-P15,
I-P01-I-P10, D01-D10, I-D01-I-D10, S01-S07) más las que añado (marcadas como
nuevas: P16-P19, I-P11-I-P16, I-D11-I-D18, S08-S09). Cada una es solo la
SEMILLA: el buscador genera miles de variantes de cada una (minutos, cuotas,
filtros, coberturas) y se queda con las que sobreviven a la validación.

Roles de selección: fav / dog (según cuota pre-partido), leader / trailer
(según marcador en directo), H / A / D, O / U, Y / N.
"""
from __future__ import annotations

from .dsl import Strategy

FLAT = {"type": "flat", "units": 1.0}
KELLY = {"type": "kelly", "fraction": 0.25, "cap": 0.03}
B365_OPEN = {"source": "b365_open"}
B365_CLOSE = {"source": "b365_close"}
LIVE_PRICE = {"source": "live"}
BIG5 = ["EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1"]


def pre(sid: str, name: str, hyp: str, market: dict, context: dict | None = None, price: dict | None = None,
        model_filter: dict | None = None, data: str = "G", control: bool = False) -> Strategy:
    return Strategy(name=f"{sid} · {name}", phase="pre", family=sid, hypothesis=hyp, market=market,
                    context=context or {}, price=price or dict(B365_OPEN), model_filter=model_filter or {},
                    staking=dict(FLAT), data_needs=data, control=control)


def live(sid: str, name: str, hyp: str, trigger: dict, market: dict, context: dict | None = None,
         exit: dict | None = None, price: dict | None = None, data: str = "R", control: bool = False,
         model_filter: dict | None = None) -> Strategy:
    return Strategy(name=f"{sid} · {name}", phase="live", family=sid, hypothesis=hyp, trigger=trigger, market=market,
                    context=context or {}, exit=exit or {"type": "hold"}, price=price or dict(LIVE_PRICE),
                    staking=dict(FLAT), data_needs=data, control=control, model_filter=model_filter or {})


def catalog() -> list[Strategy]:
    S: list[Strategy] = []
    # ======================= PRE-PARTIDO: CONOCIDAS =======================
    for sel in ("H", "D", "A"):
        S.append(pre("P01", f"Value por modelo 1X2 {sel}", "El ensemble estima mejor que la cuota de apertura de bet365",
                     {"market": "1X2", "selection": sel}, model_filter={"min_edge": 0.04, "prob": "ens"}))
    for sel in ("O", "U"):
        S.append(pre("P01", f"Value por modelo O/U 2.5 {sel}", "Idem en goles",
                     {"market": "OU", "selection": sel, "line": 2.5}, model_filter={"min_edge": 0.04, "prob": "ens"}))
    S.append(pre("P02", "Entrar pronto y batir el cierre", "La apertura tiene más error que el cierre",
                 {"market": "1X2", "selection": "any"}, model_filter={"min_edge": 0.03, "prob": "ens"}))
    S.append(pre("P03", "Seguir a Pinnacle (movimiento apertura→cierre)", "bet365 tarda en copiar a la casa sharp",
                 {"market": "1X2", "selection": "any"}, context={"filters": [{"f": "pin_move", "op": ">=", "v": 0.03}]},
                 price=dict(B365_OPEN), data="R"))
    S.append(pre("P04", "Favoritos fuertes (contra favorito-longshot)", "Los outsiders llevan más margen",
                 {"market": "1X2", "selection": "fav"}, context={"fav_prob": [0.6, 0.9]}))
    S.append(pre("P05", "Local infravalorado", "La ventaja de campo se subestima en algunas ligas",
                 {"market": "AH", "selection": "H", "line": "main"}, context={"fav_side": "away", "fav_prob": [0.4, 0.55]}))
    S.append(pre("P06", "Contra equipos populares", "El dinero del público infla a los grandes",
                 {"market": "AH", "selection": "dog", "line": "main"},
                 context={"filters": [{"f": "fav_popular", "op": "==", "v": 1}]}))
    S.append(pre("P07", "Regresión a xG", "Quien marca muy por encima de su xG está sobrevalorado",
                 {"market": "AH", "selection": "opp_of_overperformer", "line": "main"},
                 context={"filters": [{"f": "overperf_gap", "op": ">=", "v": 0.4}]}))
    S.append(pre("P08", "Fatiga por calendario", "Pocos días de descanso tras jornada europea",
                 {"market": "AH", "selection": "rested", "line": "main"},
                 context={"filters": [{"f": "rest_gap_abs", "op": ">=", "v": 2.0}, {"f": "tired_prev_euro", "op": "==", "v": 1}]}))
    S.append(pre("P09", "Efecto nuevo entrenador", "El mercado reacciona tarde o de más",
                 {"market": "1X2", "selection": "new_coach"}, context={"filters": [{"f": "new_coach_any", "op": "==", "v": 1}]},
                 data="A"))
    S.append(pre("P10", "Partidos sin nada en juego", "Motivación asimétrica a final de temporada",
                 {"market": "AH", "selection": "motivated", "line": "main"},
                 context={"comps": BIG5, "filters": [{"f": "gp_h", "op": ">=", "v": 30}, {"f": "motivation_gap", "op": ">=", "v": 1}]}))
    S.append(pre("P11", "Recién ascendidos en las primeras jornadas", "Ratings arrastrados de una liga inferior",
                 {"market": "AH", "selection": "promoted", "line": "main"},
                 context={"filters": [{"f": "gp_h", "op": "<=", "v": 8}, {"f": "any_promoted", "op": "==", "v": 1}]}))
    S.append(pre("P12", "Under en derbis", "La tensión baja el ritmo",
                 {"market": "OU", "selection": "U", "line": 2.5}, context={"filters": [{"f": "derby", "op": "==", "v": 1}]}))
    S.append(pre("P13", "Alineación confirmada", "Una baja clave anunciada 60' antes no se ajusta del todo",
                 {"market": "1X2", "selection": "weakened_opp"}, context={"filters": [{"f": "key_absences_gap", "op": ">=", "v": 0.8}]},
                 price=dict(B365_CLOSE), data="G"))
    S.append(pre("P14", "Incoherencias entre mercados de la misma casa", "AH y 1X2 implican probabilidades distintas",
                 {"market": "AH", "selection": "coherence", "line": "main"},
                 context={"filters": [{"f": "ah_1x2_gap", "op": ">=", "v": 0.03}]}, data="R"))
    S.append(pre("P15", "Empate en partidos igualados y de xG bajo", "El empate está infravalorado con pocos goles esperados",
                 {"market": "1X2", "selection": "D"},
                 context={"fav_prob": [0.3, 0.45], "filters": [{"f": "exp_goals", "op": "<=", "v": 2.4}]}))
    # nuevas (mías)
    S.append(pre("P16", "Valor contra Pinnacle (apertura bet365 vs consenso sharp)",
                 "La forma más probada de ganar: apostar solo cuando bet365 paga más que la probabilidad sin margen de Pinnacle",
                 {"market": "1X2", "selection": "any"}, model_filter={"min_edge": 0.03, "prob": "pin_open"}))
    S.append(pre("P17", "Valor contra Pinnacle en O/U 2.5", "Idem en goles",
                 {"market": "OU", "selection": "any", "line": 2.5}, model_filter={"min_edge": 0.03, "prob": "pin_open"}))
    S.append(pre("P18", "Valor contra Pinnacle en AH", "Idem en hándicap asiático (línea principal)",
                 {"market": "AH", "selection": "any", "line": "main"}, model_filter={"min_edge": 0.025, "prob": "pin_open"}))
    S.append(pre("P19", "Empate en Serie A/Ligue 1 a final de temporada entre equipos de mitad de tabla",
                 "Pactos implícitos y poca motivación", {"market": "1X2", "selection": "D"},
                 context={"comps": ["SERIEA", "LIGUE1"], "filters": [{"f": "gp_h", "op": ">=", "v": 30}, {"f": "motivation_gap", "op": "<=", "v": 0}]}))
    # control negativo
    S.append(pre("C01", "Control: apostar siempre al visitante", "No debería ganar (pierde el margen)",
                 {"market": "1X2", "selection": "A"}, control=True))

    # ======================= PRE-PARTIDO: INVENTADAS =======================
    S.append(pre("I-P01", "Cansancio asimétrico", "Descanso × viaje europeo pesa más que cada factor por separado",
                 {"market": "AH", "selection": "rested", "line": "main"},
                 context={"filters": [{"f": "rest_gap_abs", "op": ">=", "v": 2.5}, {"f": "travel_km_tired", "op": ">=", "v": 1200}]}))
    S.append(pre("I-P02", "Resaca europea", "Tras perder en Europa entre semana rinde por debajo de su rating",
                 {"market": "AH", "selection": "opp_of_hungover", "line": "main"},
                 context={"comps": BIG5, "filters": [{"f": "hangover_any", "op": "==", "v": 1}]}))
    S.append(pre("I-P03", "Partido sándwich", "El favorito rota entre dos partidos grandes",
                 {"market": "AH", "selection": "dog", "line": "main"},
                 context={"comps": BIG5, "filters": [{"f": "fav_next_big", "op": "==", "v": 1}, {"f": "fav_prev_euro", "op": "==", "v": 1}]}))
    S.append(pre("I-P04", "Desajuste xG-tabla", "Alto en la tabla por suerte frente a uno que juega mejor que sus resultados",
                 {"market": "AH", "selection": "xg_better", "line": "main"},
                 context={"filters": [{"f": "xg_table_mismatch", "op": ">=", "v": 0.5}]}))
    S.append(pre("I-P05", "Copa del Rey en campo de equipo inferior", "Rotaciones y campo pequeño: el hándicap del favorito está sobrevalorado",
                 {"market": "AH", "selection": "dog", "line": "main"}, context={"comps": ["CDR"], "fav_side": "away"}, data="A"))
    S.append(pre("I-P06", "Climatología", "Lluvia y viento fuertes bajan los goles",
                 {"market": "OU", "selection": "U", "line": 2.5}, context={"filters": [{"f": "bad_weather", "op": "==", "v": 1}]}))
    S.append(pre("I-P07", "Movimiento sin noticia", "Cuota que se mueve sin baja ni alineación: seguir o ir en contra",
                 {"market": "1X2", "selection": "steamed"}, context={"filters": [{"f": "pin_move_abs", "op": ">=", "v": 0.04}]}, data="R"))
    S.append(pre("I-P08", "Consenso de modelos", "Solo cuando Elo, DC, xG y la sharp coinciden",
                 {"market": "1X2", "selection": "consensus"}, model_filter={"min_edge": 0.02, "prob": "ens", "consensus": True}))
    S.append(pre("I-P09", "Vuelta de eliminatoria", "Quien necesita 2 goles arriesga: over y goles tardíos",
                 {"market": "OU", "selection": "O", "line": 2.5}, context={"comps": ["UCL", "UEL", "UECL", "CDR"],
                                                                              "filters": [{"f": "second_leg_deficit", "op": ">=", "v": 2}]}, data="A"))
    S.append(pre("I-P10", "Árbitro × estilo de juego", "Árbitro de muchas faltas contra equipos de presión: under",
                 {"market": "OU", "selection": "U", "line": 2.5}, context={"filters": [{"f": "ref_strict", "op": "==", "v": 1}]}))
    S.append(pre("I-P11", "Racha caliente sobrevalorada", "5+ victorias seguidas inflan la cuota del rival",
                 {"market": "AH", "selection": "opp_of_streak", "line": "main"}, context={"filters": [{"f": "win_streak_max", "op": ">=", "v": 5}]}))
    S.append(pre("I-P12", "Rotación en alineación prevista", "La alineación prevista de Statshub anticipa rotaciones antes que el mercado",
                 {"market": "AH", "selection": "opp_of_rotated", "line": "main"},
                 context={"filters": [{"f": "rotation_gap", "op": ">=", "v": 3}]}, price=dict(B365_CLOSE)))
    S.append(pre("I-P13", "Lucha por no descender vs equipo salvado", "Motivación extrema contra relajación",
                 {"market": "AH", "selection": "relegation_side", "line": "main"},
                 context={"comps": BIG5, "filters": [{"f": "gp_h", "op": ">=", "v": 28}, {"f": "relegation_fight", "op": "==", "v": 1}]}))
    S.append(pre("I-P14", "Favorito tras goleada recibida", "El mercado sobrecastiga una derrota abultada",
                 {"market": "AH", "selection": "fav", "line": "main"}, context={"filters": [{"f": "fav_last_heavy_loss", "op": "==", "v": 1}]}))
    S.append(pre("I-P15", "Over cuando ambos tienen xG alto y defensas pobres", "El mercado ancla el total en la media de liga",
                 {"market": "OU", "selection": "O", "line": 2.5}, context={"filters": [{"f": "xg_total_form", "op": ">=", "v": 3.1}]}))
    S.append(pre("I-P16", "Visitante descansado contra local que viene de Europa", "Combina P05, P08 e I-P02",
                 {"market": "AH", "selection": "A", "line": "main"},
                 context={"comps": BIG5, "filters": [{"f": "prev_euro_h", "op": "==", "v": 1}, {"f": "prev_euro_a", "op": "==", "v": 0}]}))

    # ---- enfrentamientos directos, alineación y duelos (nuevas) ----
    S.append(pre("I-P17", "Dominio histórico en el cara a cara", "Algunos emparejamientos se repiten (estilos que se anulan) y el mercado los ignora",
                 {"market": "AH", "selection": "h2h_dominant", "line": "main"}, context={"filters": [{"f": "h2h_n", "op": ">=", "v": 4}]}))
    S.append(pre("I-P18", "Cara a cara de muchos goles", "Over cuando los últimos duelos directos promedian ≥3,5 goles",
                 {"market": "OU", "selection": "O", "line": 2.5}, context={"filters": [{"f": "h2h_n", "op": ">=", "v": 4}, {"f": "h2h_goals", "op": ">=", "v": 3.5}]}))
    S.append(pre("I-P19", "Empate como costumbre en el cara a cara", "Pares con ≥50 % de empates recientes",
                 {"market": "1X2", "selection": "D"}, context={"filters": [{"f": "h2h_n", "op": ">=", "v": 4}, {"f": "h2h_draws", "op": ">=", "v": 0.5}]}))
    S.append(pre("I-P20", "Ventaja en los duelos individuales", "Los extremos de un equipo superan claramente a los laterales del otro",
                 {"market": "AH", "selection": "duel_favoured", "line": "main"}, context={"filters": [{"f": "duel_edge_abs", "op": ">=", "v": 1.0}]},
                 price=dict(B365_CLOSE)))
    S.append(pre("I-P21", "Once debilitado + rival al completo", "Bajas de titulares importantes que el cierre no recoge del todo",
                 {"market": "AH", "selection": "weakened_opp", "line": "main"},
                 context={"filters": [{"f": "key_absences_gap", "op": ">=", "v": 0.6}, {"f": "rotation_gap", "op": ">=", "v": 2}]}, price=dict(B365_CLOSE)))
    S.append(pre("I-P22", "Cara a cara caliente × árbitro tarjetero", "Duelos directos con muchas tarjetas: over de tarjetas",
                 {"market": "CARDS_OU", "selection": "O", "line": "main"}, context={"filters": [{"f": "h2h_cards", "op": ">=", "v": 5}]},
                 model_filter={"min_edge": 0.04, "prob": "secondary"}, data="R"))

    # ======================= DIRECTO: CONOCIDAS =======================
    S.append(live("D01", "Lay the draw (back al 'no empate' vía DC)", "El empate inicial está sobrevalorado con goles esperados",
                  {"minute": [1, 15], "leader": "none", "total_goals": [0, 0]}, {"market": "DC", "selection": "12"},
                  context={"filters": [{"f": "exp_goals", "op": ">=", "v": 2.8}]},
                  exit={"type": "greenup", "on": "goal", "size": "balance"}))
    S.append(live("D02", "Back al empate tardío", "El tiempo comprime la cuota del empate más lento de lo que debería",
                  {"minute": [70, 80], "leader": "none"}, {"market": "1X2", "selection": "D"}))
    S.append(live("D03", "Over tardío (+0.5 desde el 70')", "Los goles tardíos son más frecuentes",
                  {"minute": [70, 75]}, {"market": "OU", "selection": "O", "line_offset": 0.5}))
    S.append(live("D04", "Favorito pre-partido perdiendo", "El mercado sobrerreacciona al gol del débil",
                  {"minute": [20, 70], "leader": "dog", "margin": [1, 1]}, {"market": "1X2", "selection": "fav"},
                  exit={"type": "hedge", "on": "equalizer", "hedge": {"market": "AH", "selection": "fav", "line": 0.0}, "size": "zero"}))
    S.append(live("D05", "Lay al líder sorpresa (back DC del favorito)", "El underdog que gana 1-0 está sobrevalorado",
                  {"minute": [30, 75], "leader": "dog", "margin": [1, 1]}, {"market": "DC", "selection": "fav_or_draw"}))
    S.append(live("D06", "Tu estrategia: B perdiendo 0-1 al 75' + cobertura AH 0.0 del A",
                  "Si la cuota del que pierde al 75' está por encima de su probabilidad real; la cobertura solo da forma al riesgo",
                  {"minute": [75, 75], "margin": [1, 1]}, {"market": "1X2", "selection": "trailer"},
                  exit={"type": "hedge", "on": "equalizer", "hedge": {"market": "AH", "selection": "original_leader", "line": 0.0}, "size": "balance"}))
    S.append(live("D06", "Tu estrategia sin cobertura", "Control de D06: misma entrada, sin cubrirse",
                  {"minute": [75, 75], "margin": [1, 1]}, {"market": "1X2", "selection": "trailer"}))
    S.append(live("D07", "Under tras 0-0 al descanso con xG bajo", "El partido muerto sigue muerto",
                  {"minute": [46, 50], "total_goals": [0, 0], "xg_total_max": 0.8}, {"market": "OU", "selection": "U", "line_offset": 1.5}))
    S.append(live("D08", "Tras tarjeta roja", "El ajuste de precio es incompleto",
                  {"minute": [10, 75], "red": "any", "red_recent": 5}, {"market": "AH", "selection": "opp_of_red", "line": 0.0}))
    S.append(live("D09", "Gol justo antes del descanso (control)", "Se sabe que el precio se ajusta bien",
                  {"minute": [46, 48], "goal_window": [40, 46]}, {"market": "1X2", "selection": "last_scorer"}, control=True))
    S.append(live("D10", "Salida a valor (green-up)", "Salir cuando la cuota actual está por debajo de la justa",
                  {"minute": [55, 65], "leader": "fav", "margin": [1, 1]}, {"market": "1X2", "selection": "fav"},
                  exit={"type": "greenup", "on": "goal_for", "size": "balance"}))
    # ======================= DIRECTO: INVENTADAS =======================
    S.append(live("I-D01", "Presión sin premio", "xG en vivo muy superior a sus goles al 60': infravalorado para el siguiente gol",
                  {"minute": [55, 65], "xg_gap_min": 0.9, "xg_team": "trailer_or_level"}, {"market": "AH", "selection": "xg_dominant", "line": 0.0}))
    S.append(live("I-D02", "Sorpresa del underdog (seguirla)", "Tras un gol tardío del débil, se infravalora que acabe ganando",
                  {"minute": [65, 85], "leader": "dog", "margin": [1, 1], "goal_recent": 5}, {"market": "1X2", "selection": "dog"}))
    S.append(live("I-D03", "Sobrerreacción al goleador sorpresa (ir en contra)", "Dirección contraria a I-D02",
                  {"minute": [65, 85], "leader": "dog", "margin": [1, 1], "goal_recent": 5}, {"market": "DC", "selection": "fav_or_draw"}))
    S.append(live("I-D04", "Cambio defensivo del que gana", "Al meter un defensa, el líder concede presión: over",
                  {"minute": [60, 85], "margin": [1, 1], "sub_def_leader_recent": 5}, {"market": "OU", "selection": "O", "line_offset": 0.5}, data="R"))
    S.append(live("I-D05", "Tiempo añadido largo", "Con ≥6' anunciados, el over restante tarda en ajustarse",
                  {"minute": [90, 90], "added_min": 6}, {"market": "OU", "selection": "O", "line_offset": 0.5}, data="R"))
    S.append(live("I-D06", "Decaimiento lento del under", "La cuota del under cae más despacio que el reloj",
                  {"minute": [60, 70], "total_goals": [0, 1]}, {"market": "OU", "selection": "U", "line_offset": 0.5}))
    S.append(live("I-D07", "Partido roto", "Tras 2 goles en 10', el ritmo sigue alto",
                  {"minute": [20, 75], "goals_last10_min": 2}, {"market": "OU", "selection": "O", "line_offset": 0.5}))
    S.append(live("I-D08", "Favorito con 10 que empata", "Combinación roja + marcador",
                  {"minute": [30, 80], "leader": "none", "red": "fav"}, {"market": "DC", "selection": "dog_or_draw"}))
    S.append(live("I-D09", "Favorito perdiendo 0-1 al 75'", "Tu estrategia filtrada a cuando el que pierde era favorito",
                  {"minute": [75, 75], "leader": "dog", "margin": [1, 1]}, {"market": "1X2", "selection": "trailer"},
                  exit={"type": "hedge", "on": "equalizer", "hedge": {"market": "AH", "selection": "original_leader", "line": 0.0}, "size": "zero"}))
    S.append(live("I-D10", "Entrada escalonada", "Mitad al 70' y mitad al 80' si no cambia el marcador",
                  {"minute": [70, 70], "margin": [1, 1], "scale_in": 80}, {"market": "1X2", "selection": "trailer"}))
    S.append(live("I-D11", "Festival temprano", "2+ goles antes del 25': el over siguiente sigue barato",
                  {"minute": [20, 30], "total_goals": [2, 3]}, {"market": "OU", "selection": "O", "line_offset": 1.5}))
    S.append(live("I-D12", "0-0 al descanso con xG alto", "Contracara de D07: goles 'debidos'",
                  {"minute": [46, 50], "total_goals": [0, 0], "xg_total_min": 1.6}, {"market": "OU", "selection": "O", "line_offset": 0.5}))
    S.append(live("I-D13", "Favorito fuerte 0-0 al 60'", "La presión del favorito acaba rindiendo",
                  {"minute": [58, 62], "total_goals": [0, 0]}, {"market": "AH", "selection": "fav", "line": 0.0},
                  context={"fav_prob": [0.6, 0.95]}))
    S.append(live("I-D14", "Ventaja de 2 recortada a 1 tras el 70'", "Momentum del que recorta",
                  {"minute": [70, 85], "margin": [1, 1], "comeback_from2": True, "goal_recent": 5},
                  {"market": "AH", "selection": "trailer", "line": 0.5}))
    S.append(live("I-D15", "Roja temprana al underdog", "El favorito con superioridad durante 60' está barato",
                  {"minute": [10, 35], "red": "dog", "red_recent": 5}, {"market": "AH", "selection": "fav", "line": -1.0}))
    S.append(live("I-D16", "Dutching de exactos al 80' con 0-0", "0-0/1-0/0-1 cubren casi todo el resto",
                  {"minute": [80, 80], "total_goals": [0, 0]}, {"market": "OU", "selection": "U", "line_offset": 1.5}))
    S.append(live("I-D17", "Empate 1-1 tardío tras remontada del débil", "Variación de D06: entrar en el empate, no antes",
                  {"minute": [78, 86], "leader": "none", "total_goals": [2, 2], "goal_recent": 4}, {"market": "AH", "selection": "fav", "line": 0.0}))
    S.append(live("I-D18", "Under con favorito ganando 2-0 al 70'", "El favorito sale del partido",
                  {"minute": [68, 72], "leader": "fav", "margin": [2, 2]}, {"market": "OU", "selection": "U", "line_offset": 1.5}))

    # ======================= MERCADOS SECUNDARIOS =======================
    S.append(pre("S01", "Tarjetas: árbitro × faltas × derbi", "Modelo de tarjetas contra la línea de bet365",
                 {"market": "CARDS_OU", "selection": "O", "line": "main"}, model_filter={"min_edge": 0.05, "prob": "secondary"}, data="R"))
    S.append(live("S02", "Tarjetas en directo", "Quien pierde comete más faltas desde el 70'",
                  {"minute": [70, 75], "margin": [1, 2]}, {"market": "CARDS_OU", "selection": "O", "line_offset": 0.5}, data="R"))
    S.append(pre("S03", "Córners", "Estilo (centros vs juego interior) y posesión esperada",
                 {"market": "CORNERS_OU", "selection": "any", "line": "main"}, model_filter={"min_edge": 0.05, "prob": "secondary"}, data="R"))
    S.append(live("S04", "Córners en directo", "Quien pierde y domina genera córners tardíos",
                  {"minute": [70, 75], "leader": "dog", "margin": [1, 1]}, {"market": "CORNERS_OU", "selection": "O", "line_offset": 2.5}, data="R"))
    S.append(pre("S05", "Goleador", "xG/90 + lanzador de penaltis frente a la cuota", {"market": "PLAYER_GOAL", "selection": "model"}, data="R"))
    S.append(pre("S06", "Copas: rotación del favorito", "Hándicap del modesto en rondas tempranas",
                 {"market": "AH", "selection": "dog", "line": "main"}, context={"comps": ["CDR"]}, data="A"))
    S.append(live("S07", "Copas: gol en la prórroga", "Empate a 90' con un inferior agotado", {"minute": [90, 90], "leader": "none"},
                  {"market": "OU", "selection": "O", "line_offset": 0.5}, context={"comps": ["CDR", "UCL", "UEL", "UECL"]}, data="R"))
    S.append(pre("S08", "Tiros: equipo que domina vs bloque bajo", "Las líneas de tiros se anclan en medias",
                 {"market": "SHOTS_OU", "selection": "O", "line": "main"}, model_filter={"min_edge": 0.05, "prob": "secondary"}, data="R"))
    S.append(pre("S09", "Valor contra Pinnacle en props (córners/tarjetas)", "Comparar bet365 con la media de 15 casas de Statshub",
                 {"market": "CORNERS_OU", "selection": "any", "line": "main"}, model_filter={"min_edge": 0.04, "prob": "consensus_books"}, data="R"))
    return S
