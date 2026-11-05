"""Base de datos SQLite (modo WAL).

SQLite basta para el volumen de este proyecto (unos cientos de miles de filas
de cuotas por temporada) y no tiene límites de almacenamiento como el plan
gratis de MongoDB Atlas. El fichero vive en data/edgelab.sqlite.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from ..config import settings

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,           -- id Statshub/Sofascore (negativo si solo existe en otra fuente)
    name TEXT NOT NULL,
    short TEXT,
    slug TEXT,
    country TEXT
);
CREATE TABLE IF NOT EXISTS team_alias (
    alias TEXT NOT NULL,
    source TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    PRIMARY KEY (alias, source)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,           -- id Statshub/Sofascore; negativo = partido histórico sin id
    comp TEXT NOT NULL,
    season TEXT,
    kickoff INTEGER NOT NULL,         -- epoch UTC
    home_id INTEGER, away_id INTEGER,
    home_name TEXT, away_name TEXT,
    status TEXT,                      -- notstarted | inprogress | finished | postponed | canceled
    hg INTEGER, ag INTEGER,           -- goles a los 90 (+ añadido), sin prórroga
    hg_ht INTEGER, ag_ht INTEGER,
    hg_et INTEGER, ag_et INTEGER,     -- tras prórroga (copas)
    referee TEXT, referee_id INTEGER,
    round TEXT,
    lineup_status TEXT,
    source TEXT,
    understat_id INTEGER,
    extra TEXT,                       -- json
    updated INTEGER
);
CREATE INDEX IF NOT EXISTS ix_matches_kick ON matches(kickoff);
CREATE INDEX IF NOT EXISTS ix_matches_comp ON matches(comp, season);
CREATE INDEX IF NOT EXISTS ix_matches_teams ON matches(home_id, away_id, kickoff);
CREATE INDEX IF NOT EXISTS ix_matches_home ON matches(home_id, kickoff);
CREATE INDEX IF NOT EXISTS ix_matches_away ON matches(away_id, kickoff);

CREATE TABLE IF NOT EXISTS match_stats (
    match_id INTEGER NOT NULL,
    side TEXT NOT NULL,               -- H | A
    stats TEXT NOT NULL,              -- json: xG, tiros, córners, tarjetas, faltas...
    source TEXT,
    PRIMARY KEY (match_id, side)
);

CREATE TABLE IF NOT EXISTS timeline (
    match_id INTEGER NOT NULL,
    minute REAL NOT NULL,
    kind TEXT NOT NULL,               -- goal | red | shot | pen_miss | sub_def | sub_att
    side TEXT NOT NULL,               -- H | A
    xg REAL,
    player TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_timeline ON timeline(match_id, minute);

CREATE TABLE IF NOT EXISTS odds (
    match_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    phase TEXT NOT NULL,              -- open | snap | close | live
    bookmaker TEXT NOT NULL,
    market TEXT NOT NULL,             -- 1X2 | AH | OU | BTTS | DNB | DC | CS | CORNERS_OU | CARDS_OU ...
    selection TEXT NOT NULL,          -- H | D | A | O | U | Y | N | 1X ...
    line REAL,                        -- AH desde el punto de vista del local; OU línea
    period TEXT DEFAULT 'FT',         -- FT | HT | 2H
    odds REAL NOT NULL,
    minute REAL,
    score_h INTEGER, score_a INTEGER,
    source TEXT
);
CREATE INDEX IF NOT EXISTS ix_odds_match ON odds(match_id, market, bookmaker, ts);

CREATE TABLE IF NOT EXISTS predictions (
    match_id INTEGER NOT NULL,
    created INTEGER NOT NULL,
    model TEXT NOT NULL,
    payload TEXT NOT NULL,            -- json: lambdas, 1X2, AH, OU...
    PRIMARY KEY (match_id, model)
);

CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT,
    origin TEXT,                      -- catalog | grid | genetic | tree | manual
    parent TEXT,
    spec TEXT NOT NULL,               -- json DSL
    status TEXT NOT NULL DEFAULT 'candidate',  -- candidate | validated | paper | live | retired | rejected
    stats TEXT,                       -- json último resumen
    created INTEGER, updated INTEGER
);

CREATE TABLE IF NOT EXISTS strategy_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    dataset TEXT,
    split TEXT,                       -- in_sample | oos | walkforward | paper
    n INTEGER, staked REAL, profit REAL, yield REAL,
    ci_lo REAL, ci_hi REAL, pvalue REAL, clv REAL, shrunk REAL,
    price_source TEXT,                -- real | simulated
    extra TEXT
);
CREATE INDEX IF NOT EXISTS ix_tests_strat ON strategy_tests(strategy_id);

CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT,
    group_id TEXT,                    -- agrupa apuesta + coberturas
    match_id INTEGER NOT NULL,
    created INTEGER NOT NULL,
    mode TEXT NOT NULL,               -- paper | real
    market TEXT NOT NULL, selection TEXT NOT NULL, line REAL, period TEXT DEFAULT 'FT',
    base_h INTEGER DEFAULT 0, base_a INTEGER DEFAULT 0,  -- marcador al apostar (AH en directo)
    odds REAL NOT NULL, odds_source TEXT,
    fair_prob REAL, edge REAL,
    stake REAL NOT NULL,
    minute REAL,
    status TEXT NOT NULL DEFAULT 'open',
    pnl REAL, settled INTEGER,
    closing_odds REAL, clv REAL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS ix_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS ix_bets_match ON bets(match_id);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    match_id INTEGER, strategy_id TEXT,
    kind TEXT, title TEXT, message TEXT, payload TEXT,
    sent INTEGER DEFAULT 0,
    dedupe TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS model_params (
    key TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    status INTEGER,
    body BLOB
);

CREATE TABLE IF NOT EXISTS jobs_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, job TEXT, status TEXT, msg TEXT
);

CREATE TABLE IF NOT EXISTS set_pieces (
    match_id INTEGER NOT NULL, team_id INTEGER, player_id INTEGER, name TEXT, situation TEXT,
    minute REAL, xg REAL, outcome TEXT, kickoff INTEGER
);
CREATE INDEX IF NOT EXISTS ix_sp_team ON set_pieces(team_id, situation, kickoff);
CREATE TABLE IF NOT EXISTS game_meta (
    match_id INTEGER PRIMARY KEY, kickoff INTEGER, home_id INTEGER, away_id INTEGER,
    home_name TEXT, away_name TEXT, tournament TEXT, hg INTEGER, ag INTEGER
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY, name TEXT, team_id INTEGER, position TEXT, updated INTEGER
);
CREATE TABLE IF NOT EXISTS player_stats (
    player_id INTEGER NOT NULL, match_id INTEGER NOT NULL, team_id INTEGER, side TEXT,
    position TEXT, starter INTEGER, minutes REAL, goals INTEGER, assists INTEGER, xg REAL, xa REAL,
    shots INTEGER, sot INTEGER, key_pass INTEGER, won_contest INTEGER, tackles INTEGER, interceptions INTEGER,
    duel_won INTEGER, duel_lost INTEGER, aerial_won INTEGER, fouls INTEGER, was_fouled INTEGER,
    yellow INTEGER, red INTEGER, crosses INTEGER, saves INTEGER, rating REAL,
    PRIMARY KEY (player_id, match_id)
);
CREATE INDEX IF NOT EXISTS ix_pstats_match ON player_stats(match_id);
CREATE TABLE IF NOT EXISTS player_extra (     -- Statshub extra-stats: cabeza, fuera del área y primera parte
    player_id INTEGER NOT NULL, match_id INTEGER NOT NULL, head_shots INTEGER, head_sot INTEGER, out_shots INTEGER,
    out_sot INTEGER, fh_shots INTEGER, fh_sot INTEGER, fh_fouls INTEGER, fh_fouled INTEGER,
    PRIMARY KEY (player_id, match_id)
);
CREATE INDEX IF NOT EXISTS ix_pextra_match ON player_extra(match_id);
CREATE TABLE IF NOT EXISTS lineups (
    match_id INTEGER NOT NULL, side TEXT NOT NULL, player_id INTEGER NOT NULL, name TEXT, position TEXT,
    jersey INTEGER, starter INTEGER, source TEXT, formation TEXT, ts INTEGER,
    PRIMARY KEY (match_id, side, player_id)
);
CREATE TABLE IF NOT EXISTS heatmaps (           -- dónde se movió cada jugador en cada partido (x: 0 portería propia → 100 rival; y: 0 banda derecha → 100 izquierda)
    match_id INTEGER NOT NULL, player_id INTEGER NOT NULL, team_id INTEGER, position TEXT, minutes REAL,
    avg_x REAL, avg_y REAL, n INTEGER, grid TEXT,
    PRIMARY KEY (match_id, player_id)
);
CREATE INDEX IF NOT EXISTS ix_heat_player ON heatmaps(player_id);
CREATE INDEX IF NOT EXISTS ix_heat_team ON heatmaps(team_id, match_id);
CREATE TABLE IF NOT EXISTS reports (
    match_id INTEGER PRIMARY KEY, ts INTEGER, payload TEXT
);

CREATE TABLE IF NOT EXISTS odds_now (           -- última cuota de cada selección y casa (lo que lee la web: rápido)
    match_id INTEGER NOT NULL, bookmaker TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL,
    line_k REAL NOT NULL, period TEXT NOT NULL, line REAL, odds REAL NOT NULL, open_odds REAL, ts INTEGER,
    PRIMARY KEY (match_id, bookmaker, market, selection, line_k, period)
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT
);
"""

MIGRATIONS = [
    "ALTER TABLE teams ADD COLUMN color1 TEXT",
    "ALTER TABLE teams ADD COLUMN color2 TEXT",
    "ALTER TABLE matches ADD COLUMN formation_h TEXT",
    "ALTER TABLE matches ADD COLUMN formation_a TEXT",
]

_local = threading.local()


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def get_con(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    key = str(path)
    cons = getattr(_local, "cons", None)
    if cons is None:
        cons = _local.cons = {}
    if key not in cons:
        con = _connect(path)
        con.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # ya aplicada
        con.commit()
        cons[key] = con
    return cons[key]


@contextmanager
def tx(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    con = get_con(path)
    try:
        con.execute("BEGIN")
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def q(sql: str, params: Iterable[Any] = (), path: Path | None = None) -> list[dict]:
    return [dict(r) for r in get_con(path).execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = (), path: Path | None = None) -> dict | None:
    r = get_con(path).execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def execute(sql: str, params: Iterable[Any] = (), path: Path | None = None) -> int:
    con = get_con(path)
    cur = con.execute(sql, tuple(params))
    con.commit()
    return cur.lastrowid


def executemany(sql: str, rows: Iterable[Iterable[Any]], path: Path | None = None) -> None:
    con = get_con(path)
    con.executemany(sql, [tuple(r) for r in rows])
    con.commit()


def kv_get(key: str, default: Any = None) -> Any:
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def _jdefault(o):
    """numpy y similares → tipos de Python (para que un bool o un float de numpy no rompa el guardado)."""
    for attr in ("item", "tolist"):
        if hasattr(o, attr):
            try:
                return getattr(o, attr)()
            except Exception:
                pass
    return str(o)


def kv_set(key: str, value: Any) -> None:
    execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, default=_jdefault)))


def save_params(key: str, payload: Any) -> None:
    execute("INSERT INTO model_params(key,ts,payload) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, payload=excluded.payload",
            (key, int(time.time()), json.dumps(payload)))


def load_params(key: str, default: Any = None) -> Any:
    r = q1("SELECT payload FROM model_params WHERE key=?", (key,))
    return json.loads(r["payload"]) if r else default


def log_job(job: str, status: str, msg: str = "") -> None:
    try:
        execute("INSERT INTO jobs_log(ts,job,status,msg) VALUES(?,?,?,?)", (int(time.time()), job, status, msg[:2000]))
    except Exception:
        pass


def find_match(home_id: int | None, away_id: int | None, kickoff: int, window_h: float = 36,
               home_name: str | None = None, away_name: str | None = None) -> dict | None:
    """Busca un partido existente por equipos y fecha aproximada (las fuentes difieren en la hora)."""
    lo, hi = kickoff - int(window_h * 3600), kickoff + int(window_h * 3600)
    if home_id is not None and away_id is not None:
        r = q1("SELECT * FROM matches WHERE home_id=? AND away_id=? AND kickoff BETWEEN ? AND ? "
               "ORDER BY ABS(kickoff-?) LIMIT 1", (home_id, away_id, lo, hi, kickoff))
        if r:
            return r
    if home_name and away_name:
        return q1("SELECT * FROM matches WHERE home_name=? AND away_name=? AND kickoff BETWEEN ? AND ? "
                  "ORDER BY ABS(kickoff-?) LIMIT 1", (home_name, away_name, lo, hi, kickoff))
    return None


_MATCH_COLS = ["id", "comp", "season", "kickoff", "home_id", "away_id", "home_name", "away_name", "status",
               "hg", "ag", "hg_ht", "ag_ht", "hg_et", "ag_et", "referee", "referee_id", "round", "lineup_status",
               "source", "understat_id", "extra", "updated"]
_ID_TABLES = ["match_stats", "timeline", "odds", "predictions", "bets", "alerts"]


def _reassign_id(old: int, new: int) -> None:
    with tx() as con:
        for t in _ID_TABLES:
            con.execute(f"UPDATE OR IGNORE {t} SET match_id=? WHERE match_id=?", (new, old))
        con.execute("UPDATE matches SET id=? WHERE id=?", (new, old))


def upsert_match(m: dict) -> int:
    """Inserta o fusiona un partido. Devuelve su id definitivo.

    - Con id externo (Statshub): si existe un partido histórico equivalente con id
      negativo, se le reasigna el id real y se fusionan los datos.
    - Sin id: se busca por equipos+fecha; si no existe, se crea con id negativo.
    """
    m = {k: v for k, v in dict(m).items() if k in _MATCH_COLS}
    if isinstance(m.get("extra"), (dict, list)):
        m["extra"] = json.dumps(m["extra"])
    m["updated"] = int(time.time())
    existing = find_match(m.get("home_id"), m.get("away_id"), int(m["kickoff"]),
                          home_name=m.get("home_name"), away_name=m.get("away_name"))
    if m.get("id") is None:
        if existing:
            m["id"] = existing["id"]
        else:
            mn = q1("SELECT MIN(id) AS v FROM matches")
            low = mn["v"] if mn and mn["v"] is not None else 0
            m["id"] = min(-1, low - 1)
    elif existing and existing["id"] != m["id"]:
        if q1("SELECT 1 FROM matches WHERE id=?", (m["id"],)) is None:
            _reassign_id(existing["id"], m["id"])
    present = [c for c in _MATCH_COLS if c in m]
    placeholders = ",".join("?" for _ in present)
    updates = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in present if c != "id")
    execute(f"INSERT INTO matches({','.join(present)}) VALUES({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}", [m[c] for c in present])
    return int(m["id"])


def upsert_odds_now(match_id: int, rows: list[tuple], ts: int) -> None:
    """rows: (bookmaker, market, selection, line, period, odds)."""
    if not rows:
        return
    executemany("INSERT INTO odds_now(match_id,bookmaker,market,selection,line_k,period,line,odds,open_odds,ts) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(match_id,bookmaker,market,selection,line_k,period) DO UPDATE SET odds=excluded.odds, ts=excluded.ts",
                [(match_id, b, m, s_, -999.0 if l is None else round(float(l), 2), p or "FT", l, o, o, ts) for b, m, s_, l, p, o in rows])


def ensure_odds_now(days_back: int = 2) -> int:
    """Primera vez: rellena odds_now con lo que ya hay en odds para los partidos recientes y futuros."""
    if kv_get("odds_now_built"):
        return 0
    since = int(time.time()) - days_back * 86400
    con = get_con()
    con.execute("INSERT OR IGNORE INTO odds_now(match_id,bookmaker,market,selection,line_k,period,line,odds,open_odds,ts) "
                "SELECT o.match_id,o.bookmaker,o.market,o.selection,COALESCE(ROUND(o.line,2),-999),COALESCE(o.period,'FT'),o.line,o.odds,o.odds,o.ts "
                "FROM odds o JOIN matches m ON m.id=o.match_id WHERE m.kickoff>? AND o.phase!='live' ORDER BY o.ts", (since,))
    con.execute("INSERT INTO odds_now(match_id,bookmaker,market,selection,line_k,period,line,odds,open_odds,ts) "
                "SELECT o.match_id,o.bookmaker,o.market,o.selection,COALESCE(ROUND(o.line,2),-999),COALESCE(o.period,'FT'),o.line,o.odds,o.odds,o.ts "
                "FROM odds o JOIN matches m ON m.id=o.match_id WHERE m.kickoff>? AND o.phase!='live' ORDER BY o.ts "
                "ON CONFLICT(match_id,bookmaker,market,selection,line_k,period) DO UPDATE SET odds=excluded.odds, ts=excluded.ts", (since,))
    con.commit()
    kv_set("odds_now_built", int(time.time()))
    return q1("SELECT COUNT(*) AS n FROM odds_now")["n"]
