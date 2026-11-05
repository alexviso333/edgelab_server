"""Parsers de Sofascore con respuestas reales (capturadas el 02/11/2026)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from edgelab.collectors import sofascore as sf  # noqa: E402

FX = json.loads((Path(__file__).parent / "fixtures" / "sofascore.json").read_text())


def test_frac():
    assert sf.frac_to_dec("9/1") == 10.0
    assert sf.frac_to_dec("1/4") == 1.25
    assert sf.frac_to_dec(None) is None


def test_live_odds_parse():
    ev = FX["live_event"]
    rows = sf.parse_odds(FX["live_odds"], ev["homeTeam"]["name"], ev["awayTeam"]["name"])
    live = [r for r in rows if r["live"]]
    pre = [r for r in rows if not r["live"]]
    assert {r["selection"] for r in pre if r["market"] == "1X2"} == {"H", "D", "A"}
    x = {r["selection"]: r["odds"] for r in live if r["market"] == "1X2" and r["period"] == "FT"}
    assert x["H"] == 10.0 and x["A"] == 1.25
    # OU 2.5 en directo está suspendido → no se guarda
    assert not [r for r in live if r["market"] == "OU" and r["line"] == 2.5]
    assert [r for r in live if r["market"] == "OU" and r["line"] == 3.5]
    ah = [r for r in live if r["market"] == "AH"]
    assert {(r["selection"], r["line"]) for r in ah} >= {("H", -0.5), ("A", 0.5), ("H", 0.0), ("A", 0.0)}
    ng = {r["selection"] for r in live if r["market"] == "NEXT_GOAL"}
    assert ng == {"H", "A", "NONE"}
    assert [r for r in live if r["market"] == "CARDS_OU"] and [r for r in live if r["market"] == "CORNERS_OU"]
    assert {r["period"] for r in live if r["market"] == "1X2"} == {"FT", "HT"}


def test_incidents_and_shotmap():
    inc = FX["incidents"]["incidents"]
    ev = sf.parse_incidents(inc)
    goals = [e for e in ev if e["kind"] == "goal"]
    assert len(goals) == 2 and all(e["side"] == "H" for e in goals)
    assert all(e["minute"] < 120 for e in ev)          # el 'addedTime: 999' del periodo no cuenta
    tl = sf.build_timeline(inc, FX["shotmap"]["shotmap"])
    shots = [e for e in tl if e["kind"] == "shot"]
    assert shots and all(e["xg"] >= 0 for e in shots)
    assert tl == sorted(tl, key=lambda e: (e["minute"], e["kind"] != "shot"))


def test_lineups_and_stats():
    lu = sf.parse_lineups(FX["lineups"])
    assert lu["confirmed"] and lu["formation"]["H"] == "4-4-2" and lu["H"][0]["pos"] == "G"
    st = sf.parse_live_stats(FX["live_stats"])
    assert "shots" in st["H"] and "poss" in st["A"]
