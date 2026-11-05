import sqlite3, json
db = sqlite3.connect('/app/data/edgelab.sqlite')
rows = db.execute("SELECT key,value FROM kv WHERE key LIKE 'analysis:%'").fetchall()
total = len(rows)
done = sum(1 for _,v in rows if json.loads(v)['n_rows'] > 0)
print(f'Total: {total}, con datos: {done}, sin datos: {total-done}')