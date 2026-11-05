import sqlite3
db = sqlite3.connect('/app/data/edgelab.sqlite')
for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    try:
        n = db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
        if n > 0: print(t, n)
    except: pass
