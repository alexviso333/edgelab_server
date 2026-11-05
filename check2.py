import sqlite3
db = sqlite3.connect('/app/data/edgelab.sqlite')
print("=== jobs_log ===")
for r in db.execute('SELECT * FROM jobs_log ORDER BY rowid DESC LIMIT 20').fetchall():
    print(r)
print("=== kv ===")
for r in db.execute('SELECT * FROM kv').fetchall():
    print(r)
