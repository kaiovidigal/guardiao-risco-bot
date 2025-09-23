import sqlite3

# conecta ao banco
con = sqlite3.connect("main.sqlite")
cur = con.cursor()

# cria as tabelas se não existirem
cur.executescript("""
CREATE TABLE IF NOT EXISTS ml_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_id INTEGER,
    after INTEGER,
    base TEXT,
    pattern_key TEXT,
    conf_short REAL,
    conf_long REAL,
    gap_short REAL,
    gap_long REAL,
    samples_short INTEGER,
    chosen INTEGER,
    label INTEGER,
    stage TEXT,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_post_short TEXT,
    last_post_long TEXT,
    suggested INTEGER,
    outcome TEXT,
    seen INTEGER
);
""")

con.commit()
con.close()
print("✅ Tabelas criadas com sucesso.")
