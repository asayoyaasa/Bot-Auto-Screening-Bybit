import psycopg2
from psycopg2 import pool
from modules.config_loader import CONFIG

DB_POOL = None

def init_db():
    global DB_POOL
    try:
        pool_size = CONFIG['system']['max_threads'] + 5
        DB_POOL = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=pool_size,
            host=CONFIG['database']['host'], database=CONFIG['database']['database'],
            user=CONFIG['database']['user'], password=CONFIG['database']['password'],
            port=CONFIG['database']['port']
        )
        conn = DB_POOL.getconn()
        try: migrate_schema(conn)
        finally: DB_POOL.putconn(conn)
        print("✅ Database Connected & Migrated.")
    except Exception as e:
        print(f"❌ DB Init Error: {e}")
        exit(1)

def migrate_schema(conn):
    cur = conn.cursor()
    required = {
        "id": "SERIAL PRIMARY KEY", "symbol": "VARCHAR(100)", "side": "VARCHAR(10)", 
        "timeframe": "VARCHAR(5)", "pattern": "VARCHAR(50)",
        "entry_price": "DECIMAL", "sl_price": "DECIMAL", "tp1": "DECIMAL", "tp2": "DECIMAL", "tp3": "DECIMAL",
        "rr": "DECIMAL", "status": "VARCHAR(50) DEFAULT 'Waiting Entry'", "reason": "TEXT",
        "tech_score": "INT", "quant_score": "INT", "deriv_score": "INT", "smc_score": "INT DEFAULT 0",
        "z_score": "DECIMAL DEFAULT 0", "zeta_score": "DECIMAL DEFAULT 0", "obi": "DECIMAL DEFAULT 0",
        "basis": "DECIMAL", "btc_bias": "VARCHAR(50)",
        "tech_reasons": "TEXT", "quant_reasons": "TEXT", "deriv_reasons": "TEXT",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP", "entry_hit_at": "TIMESTAMP", 
        "closed_at": "TIMESTAMP", "exit_price": "DECIMAL", "message_id": "VARCHAR(50)", "channel_id": "VARCHAR(50)"
    }
    try:
        cur.execute("SELECT to_regclass('public.trades');")
        if cur.fetchone()[0] is None:
            cur.execute("CREATE TABLE trades (" + ", ".join([f"{k} {v}" for k, v in required.items()]) + ");")
        else:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'trades';")
            existing = {row[0] for row in cur.fetchall()}
            missing = [f"ADD COLUMN IF NOT EXISTS {col} {dtype.replace('SERIAL PRIMARY KEY','INT').replace('PRIMARY KEY','')}" for col, dtype in required.items() if col not in existing]
            if missing: cur.execute(f"ALTER TABLE trades {', '.join(missing)};")
        
        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key_name VARCHAR(50) PRIMARY KEY, value_text TEXT);")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Migration Error: {e}")

def get_active_signals():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT symbol, timeframe FROM trades WHERE status NOT LIKE '%Closed%' AND status NOT LIKE '%Cancelled%' AND status NOT LIKE '%Stop Loss%'")
        return {(r[0], r[1]) for r in cur.fetchall()}
    except: return set()
    finally: release_conn(conn)

def get_conn():
    if not DB_POOL: init_db()
    return DB_POOL.getconn()

def release_conn(conn):
    if DB_POOL: DB_POOL.putconn(conn)