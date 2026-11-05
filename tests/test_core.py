import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("EDGELAB_DB", str(Path(tempfile.mkdtemp()) / "t.sqlite"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edgelab.core.markets import Outcome, Selection, apply_margin, devig, fair_odds, selection_probs, settle  # noqa: E402
from edgelab.core.hedge import (Position, dutch, green_up, hedge_equalize, hedge_to_zero, kelly_stake,  # noqa: E402
                                lay_hedge, solve_hedge)

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- liquidación
@pytest.mark.parametrize("line,score,expected", [
    (-0.5, (1, 0), (1, 0)), (-0.5, (1, 1), (0, 0)),
    (-0.25, (1, 1), (0, 0.5)), (-0.25, (2, 1), (1, 0)),
    (-0.75, (2, 1), (0.5, 0.5)), (-0.75, (3, 1), (1, 0)),
    (0.0, (1, 1), (0, 1)), (0.25, (1, 1), (0.5, 0.5)), (0.25, (0, 1), (0, 0)),
    (-1.0, (2, 1), (0, 1)), (-1.25, (2, 1), (0, 0.5)),
])
def test_asian_handicap_home(line, score, expected):
    assert settle(Selection("AH", "H", line), Outcome(*score)) == pytest.approx(expected)


def test_inplay_ah_counts_from_base():
    # AH 0.0 del local apostado con 1-1: solo cuentan los goles posteriores
    sel = Selection("AH", "H", 0.0, base_h=1, base_a=1)
    assert settle(sel, Outcome(2, 1)) == (1, 0)
    assert settle(sel, Outcome(1, 1)) == (0, 1)
    assert settle(sel, Outcome(1, 2)) == (0, 0)
    # apostado con 0-1: si acaba 1-1, el local "gana" 1-0 desde la base
    sel2 = Selection("AH", "H", 0.0, base_h=0, base_a=1)
    assert settle(sel2, Outcome(1, 1)) == (1, 0)


@pytest.mark.parametrize("line,goals,exp", [(2.5, 3, (1, 0)), (2.5, 2, (0, 0)), (2.25, 2, (0, 0.5)), (2.75, 3, (0.5, 0.5)),
                                            (3.0, 3, (0, 1)), (2.25, 3, (1, 0))])
def test_over(line, goals, exp):
    assert settle(Selection("OU", "O", line), Outcome(goals, 0)) == pytest.approx(exp)


def test_other_markets():
    o = Outcome(2, 1, 1, 1, corners_h=6, corners_a=4, cards_h=2, cards_a=3)
    assert settle(Selection("1X2", "H"), o) == (1, 0)
    assert settle(Selection("DC", "X2"), o) == (0, 0)
    assert settle(Selection("BTTS", "Y"), o) == (1, 0)
    assert settle(Selection("CS", "2-1"), o) == (1, 0)
    assert settle(Selection("1X2", "D", period="HT"), o) == (1, 0)
    assert settle(Selection("OU", "O", 0.5, period="2H"), o) == (1, 0)
    assert settle(Selection("CORNERS_OU", "O", 9.5), o) == (1, 0)
    assert settle(Selection("CORNERS_OU", "HU", 6.5), o) == (1, 0)
    assert settle(Selection("CARDS_AH", "A", -0.5), o) == (1, 0)
    assert settle(Selection("TEAM_OU", "AO", 0.5), o) == (1, 0)


def test_probs_and_fair_odds_consistency():
    from edgelab.models.poisson import score_matrix
    M = score_matrix(1.6, 1.1)
    w, p, l = selection_probs(Selection("AH", "H", -0.25), M)
    assert w + p + l == pytest.approx(1)
    fo = fair_odds(w, p, l)
    assert w * (fo - 1) - l == pytest.approx(0, abs=1e-9)


def test_devig_and_margin():
    p = devig([2.75, 3.6, 2.4], "power")
    assert sum(p) == pytest.approx(1)
    o = apply_margin([0.35, 0.28, 0.37], 0.06)
    assert sum(1 / x for x in o) == pytest.approx(1.06, abs=1e-4)
    for m in ("mult", "shin"):
        assert sum(devig([1.9, 3.8, 4.5], m)) == pytest.approx(1)


# ---------------------------------------------------------------- coberturas
def test_doc_example_D06():
    """Ejemplo del documento: 10 € al B a 21.0; con 1-1 el AH 0.0 del A paga 1.66."""
    assert hedge_to_zero(10, 1.66) == pytest.approx(15.15, abs=0.01)
    assert hedge_equalize(10, 21, 1.66) == pytest.approx(126.51, abs=0.01)
    grid = np.zeros((6, 6))
    grid[2, 1], grid[1, 1], grid[1, 2] = 0.232, 0.599, 0.169
    pos = [Position(Selection("1X2", "A"), 21, 10)]
    hed = [(Selection("AH", "H", 0.0, base_h=1, base_a=1), 1.66)]
    r = solve_hedge(pos, hed, grid, objective="balance")
    assert r.stakes[0] == pytest.approx(126.5, abs=0.2)
    assert r.branches["Gana local"] == pytest.approx(r.branches["Gana visitante"], abs=0.1)
    assert r.ev == pytest.approx(23.48, abs=0.05)
    z = solve_hedge(pos, hed, grid, objective="zero", zero_branch="Gana local")
    assert z.stakes[0] == pytest.approx(15.15, abs=0.05)
    assert z.branches["Gana local"] == pytest.approx(0, abs=0.05)


def test_arbitrage_detected():
    # back 1X2 H a 2.5 ya hecho; ahora X2 paga 2.2 → hay beneficio garantizado
    from edgelab.models.poisson import score_matrix
    M = score_matrix(1.4, 1.2)
    r = solve_hedge([Position(Selection("1X2", "H"), 2.5, 10)], [(Selection("DC", "X2"), 2.2)], M, objective="equalize")
    assert r.worst > 0
    assert any("garantizado" in n for n in r.notes)


def test_simple_formulas():
    lh = lay_hedge(10, 1.30, 3.80, 0.05)
    assert lh["if_back_wins"] == pytest.approx(lh["if_back_loses"], abs=0.01)
    d = dutch([1.62, 6.0], target_return=13)
    assert d["stakes"][0] * 1.62 == pytest.approx(13)
    assert green_up(10, 21, 5.5)["fair_cashout"] == pytest.approx(38.18, abs=0.01)
    assert kelly_stake(0.55, 2.0, 1000, 0.25, cap_pct=None) == pytest.approx(25.0)


# ---------------------------------------------------------------- parsers Statshub (respuestas reales)
def test_parse_odds_comparison_real():
    from edgelab.collectors.statshub import parse_odds_comparison
    d = json.loads((FIX / "odds_comparison.json").read_text())
    rows = parse_odds_comparison(d, "Liverpool", "Arsenal")
    one = {(r["bookmaker"], r["selection"]): r["odds"] for r in rows if r["market"] == "1X2" and r["period"] == "FT"}
    assert one[("Bet365", "H")] == pytest.approx(2.75)
    assert one[("Pinnacle", "A")] == pytest.approx(2.5)
    pin_ah = {(r["selection"], r["line"]): r["odds"] for r in rows if r["market"] == "AH" and r["bookmaker"] == "Pinnacle" and r["period"] == "FT"}
    # el signo invertido del lado local de Pinnacle se corrige
    assert pin_ah[("H", 0.25)] == pytest.approx(1.781)
    assert pin_ah[("A", -0.25)] == pytest.approx(2.16)
    kinds = {r["market"] for r in rows}
    assert {"1X2", "AH", "OU", "BTTS", "DC", "DNB", "CS", "CORNERS_OU", "CARDS_OU", "TEAM_OU"} <= kinds


def test_parse_events_real():
    from edgelab.collectors.statshub import parse_event
    d = json.loads((FIX / "by_date.json").read_text())
    parsed = [parse_event(e) for e in d["data"]]
    parsed = [p for p in parsed if p]
    assert parsed and all(p["comp"] in ("EPL", "LALIGA", "SERIEA", "BUNDES", "LIGUE1") for p in parsed)
    fin = [p for p in parsed if p["status"] == "finished"]
    assert all(p["hg"] is not None and p["hg_ht"] is not None for p in fin)
    assert any(p["referee"] for p in parsed)


def test_inplay_model_sane():
    from edgelab.models.inplay import InplayModel, LiveState
    m = InplayModel()
    P0 = m.final_distribution(1.5, 1.1, LiveState(0))
    assert P0.sum() == pytest.approx(1)
    late = m.fair_prices(1.5, 1.1, LiveState(85, 1, 0))
    early = m.fair_prices(1.5, 1.1, LiveState(20, 1, 0))
    assert late["1X2"]["H"] < early["1X2"]["H"]          # ir ganando tarde vale más
    red = m.fair_prices(1.5, 1.1, LiveState(30, 0, 0, red_h=1))
    nored = m.fair_prices(1.5, 1.1, LiveState(30, 0, 0))
    assert red["1X2"]["H"] > nored["1X2"]["H"]           # roja al local empeora su cuota
    ng = m.next_goal(1.5, 1.1, LiveState(60, 0, 0))
    assert sum(ng.values()) == pytest.approx(1, abs=1e-6)
