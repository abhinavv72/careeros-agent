import jobpilot
db = jobpilot.connect()
rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [r[0] for r in rows])
for tname in [r[0] for r in rows]:
    cols = db.execute(f"PRAGMA table_info({tname})").fetchall()
    print(f"\n{tname} columns:", [c[1] for c in cols])
db.close()
