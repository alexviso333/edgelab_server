"""Prueba de punta a punta con un mundo sintético pequeño (sin red)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def world():
    tmp = Path(tempfile.mkdtemp())
    db_path = str(tmp / "w.sqlite")
    from edgelab.config import settings
    old = (settings.db_path, settings.data_dir)
    settings.db_path = Path(db_path)
    settings.data_dir = tmp
    from scripts.synthetic import main as synth
    synth(seasons=4, db_path=db_path, teams_per=10, seed=3, leagues=["EPL", "LALIGA"])
    yield tmp
    settings.db_path, settings.data_dir = old


def test_lab_end_to_end(world):
    from edgelab.lab import LabConfig, run_lab
    from edgelab.core import db
    rep = run_lab(LabConfig(grid_per_seed=3, live_grid_per_seed=2, genetic_generations=1, genetic_size=6, top_k_oos=10,
                            min_n_train=20, min_n_oos=30, use_mlp=False), log=lambda s: None)
    assert rep["trials"] > 50
    assert rep["n_bet_rows"] > 500 and rep["n_timelines"] > 300
    names = {r["name"]: r for r in rep["table"]}
    ctrl = [r for n, r in names.items() if n.startswith("C01")]
    assert ctrl and ctrl[0]["verdict"] != "validated"          # el control negativo no pasa
    assert db.q1("SELECT COUNT(*) AS n FROM strategy_tests")["n"] >= rep["trials"]
    assert db.load_params("inplay") is not None


def test_api_endpoints(world):
    from fastapi.testclient import TestClient
    from edgelab.api.app import app
    c = TestClient(app)
    assert c.get("/api/health").json()["ok"]
    d = c.get("/api/dashboard").json()
    assert d["data"]["matches"] > 0
    assert isinstance(c.get("/api/strategies").json(), list)
    ms = c.get("/api/matches?days=10").json()
    assert ms, "debería haber partidos futuros en el mundo sintético"
    det = c.get(f"/api/match/{ms[0]['id']}").json()
    assert det["prediction"] and det["fair"]["1X2"]["H"] > 1
    body = {"positions": [{"market": "1X2", "selection": "A", "odds": 21, "stake": 10}],
            "hedges": [{"market": "AH", "selection": "H", "line": 0, "odds": 1.66, "base_h": 1, "base_a": 1}],
            "objective": "balance", "minute": 82, "hg": 1, "ag": 1, "odds_1x2": [4.0, 1.55, 5.5]}
    r = c.post("/api/hedge", json=body).json()
    assert r["stakes"][0] > 50
    f = c.post("/api/calc", json={"kind": "fair", "params": {"odds_1x2": [2.1, 3.4, 3.6], "minute": 75, "hg": 1, "ag": 0}}).json()
    assert f["1X2"]["H"] < 1.6
    bt = c.post("/api/backtest", json={"spec": {"name": "test", "phase": "pre", "market": {"market": "1X2", "selection": "D"},
                                               "price": {"source": "b365_open"}}}).json()
    assert bt["summary"]["n"] > 100


def test_live_engine_with_simulated_match(world):
    """El rastreador en vivo genera señal + cobertura con un partido 'en juego' simulado."""
    import json
    import time
    from edgelab.core import db
    from edgelab.live import engine
    m = db.q1("SELECT * FROM matches WHERE status='notstarted' ORDER BY kickoff LIMIT 1")
    now = int(time.time())
    extra = {"rawStatus": "2nd half", "periodStart": now - 30 * 60}
    db.execute("UPDATE matches SET status='inprogress', kickoff=?, hg=0, ag=1, extra=? WHERE id=?",
               (now - 80 * 60, json.dumps(extra), m["id"]))
    tr = engine.LiveTracker()
    row = db.q1("SELECT * FROM matches WHERE id=?", (m["id"],))
    st = {"score": (0, 1), "events": [{"minute": 50, "kind": "goal", "side": "A"}], "xg": (None, None), "reds": (0, 0)}
    snap = tr.evaluate(row, 75.0, st)
    assert snap["fair"]["1X2"]["A"] < snap["fair"]["1X2"]["H"]
    assert any("D06" in s["strategy"] for s in snap["signals"]), snap["signals"]
    # empate al 82' → aviso de cobertura con tabla de stakes
    st2 = {"score": (1, 1), "events": st["events"] + [{"minute": 81, "kind": "goal", "side": "H"}], "xg": (None, None), "reds": (0, 0)}
    snap2 = tr.evaluate(row, 82.0, st2)
    assert any(s.get("stake_table") for s in snap2["signals"])


def test_report_lineups_duels_h2h(world):
    from edgelab.core import db
    from edgelab import analysis
    from edgelab.models.players import lineup_features
    LU = lineup_features()
    assert not LU.empty and LU["duel_edge"].notna().sum() > 100
    m = db.q1("SELECT id FROM matches WHERE status='finished' ORDER BY kickoff DESC LIMIT 1")
    r = analysis.report(m["id"], use_cache=False)
    assert r["narrative"] and r["factors"]
    assert r["lineups"]["H"] and r["duels"]
    assert len(r["h2h"]) >= 1 and len(r["form"]["H"]) >= 1
    assert db.kv_get("factor_grid")  # el laboratorio guardó el mapa de factores


def test_players_parser_real():
    import json
    from pathlib import Path
    from edgelab.collectors.players import parse_last_games
    d = json.loads((Path(__file__).parent / "fixtures" / "last_games.json").read_text())
    games = parse_last_games(d["data"])
    assert games and all(sum(p["starter"] for p in g["players"] if p["side"] == "H") == 11 for g in games)
    assert any(p["xg"] is not None for g in games for p in g["players"])


def test_sync_match_and_live_price(world, monkeypatch):
    """sync_match guarda cuotas y once de Sofascore; live_price las encuentra."""
    import json
    import time
    from pathlib import Path
    from edgelab.core import db
    from edgelab.core.markets import Selection
    from edgelab.live import engine
    fx = json.loads((Path(__file__).parent / "fixtures" / "sofascore.json").read_text())
    m = db.q1("SELECT * FROM matches WHERE status='notstarted' ORDER BY kickoff LIMIT 1")

    class FakeSh:
        def odds_comparison(self, *a): raise RuntimeError("sin red")
        def predicted_lineup(self, *a): raise RuntimeError("sin red")
        def formations(self, *a): return {}

    class FakeSofa:
        ok = True
        def odds(self, mid): return fx["live_odds"]
        def lineups(self, mid, ttl=None): return fx["lineups"]
        def incidents(self, mid, ttl=None): return fx["incidents"]["incidents"]
        def shotmap(self, mid, ttl=None): return fx["shotmap"]["shotmap"]

    monkeypatch.setattr(engine, "shub", lambda: FakeSh())
    monkeypatch.setattr(engine, "sofa", lambda: FakeSofa())
    r = engine.sync_match(m["id"])
    assert r["odds_sofa"] > 3 and r["lineups"] > 0 and len(r["errors"]) == 2
    assert db.q1("SELECT COUNT(*) AS n FROM odds WHERE match_id=? AND bookmaker='Sofascore' AND phase='open'", (m["id"],))["n"] > 0
    from edgelab.collectors import sofascore as sf
    rows = [r for r in sf.parse_odds(fx["live_odds"], "Real Oviedo", "UD Las Palmas") if r["live"]]
    sf.store_odds(m["id"], rows, minute=40, score=(0, 0))
    lo = engine.live_price(m["id"], Selection("1X2", "H"))
    assert lo == 10.0
    assert engine.live_price(m["id"], Selection("AH", "A", 0.5, base_h=1, base_a=0)) is None  # marcador distinto
    db.execute("UPDATE odds SET score_h=0, score_a=2 WHERE match_id=? AND phase='live'", (m["id"],))
    assert engine.live_price(m["id"], Selection("AH", "A", 0.5, base_h=0, base_a=2)) == round(1 + 39 / 40, 4)
    # cronologías
    fin = db.q1("SELECT id FROM matches WHERE status='finished' AND id>0 ORDER BY kickoff DESC LIMIT 1")
    if fin:
        assert engine.fetch_timeline(fin["id"]) > 0


def test_status_and_refresh_endpoints(world):
    from fastapi.testclient import TestClient
    from edgelab.api.app import app
    c = TestClient(app)
    s = c.get("/api/status").json()
    assert "worker" in s and "upcoming" in s
    ms = c.get("/api/matches?days=10").json()
    d = c.get(f"/api/match/{ms[0]['id']}").json()
    assert "freshness" in d and "needs_sync" in d["freshness"]


def test_pricing_and_plans(world):
    from fastapi.testclient import TestClient
    from edgelab.api.app import app
    c = TestClient(app)
    ms = c.get("/api/matches?days=10").json()
    mid = next(m["id"] for m in ms if m["status"] == "notstarted")
    r = c.get(f"/api/match/{mid}/markets").json()
    assert r["rows"] and r["lambdas"]["lh"] > 0 and r["summary"]["scores"]
    one = {x["selection"]: x["p"] for x in r["rows"] if x["market"] == "1X2" and x["period"] == "FT"}
    assert abs(sum(one.values()) - 1) < 0.02
    assert len(r["plans"]) >= 8 and all(p["scenarios"] for p in r["plans"])
    assert r["combos"]
    o = c.get("/api/opportunities?hours=720").json()
    assert "values" in o and o["n_matches"] > 0
