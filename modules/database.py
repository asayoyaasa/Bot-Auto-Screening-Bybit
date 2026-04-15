import logging

import psycopg2
from psycopg2 import pool

from modules.config_loader import CONFIG
from modules.logging_setup import build_component_logger
from modules.paper_trade_utils import normalize_execution_mode

DB_POOL = None
ACTIVE_SIGNAL_STATUSES = ('Waiting Entry', 'Queued', 'Order Placed', 'Active')
logger = build_component_logger(__name__, None)


def init_db():
    global DB_POOL
    try:
        pool_size = CONFIG['system']['max_threads'] + 5
        DB_POOL = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=pool_size,
            host=CONFIG['database']['host'],
            database=CONFIG['database']['database'],
            user=CONFIG['database']['user'],
            password=CONFIG['database']['password'],
            port=CONFIG['database']['port'],
        )

        conn = DB_POOL.getconn()
        try:
            migrate_schema(conn)
        finally:
            DB_POOL.putconn(conn)

        logger.info('Database connected and schema synced.')
    except Exception as e:
        logger.error(f'DB init error: {e}')
        raise SystemExit(1)


def migrate_schema(conn):
    """
    Smart Schema Migration:
    1. If table missing -> Create with ALL columns.
    2. If table exists -> Add only missing columns.
    """
    cur = conn.cursor()

    required_columns = {
        "id": "SERIAL PRIMARY KEY",
        "symbol": "VARCHAR(100)",
        "side": "VARCHAR(10)",
        "timeframe": "VARCHAR(5)",
        "pattern": "VARCHAR(50)",
        "entry_price": "DECIMAL",
        "sl_price": "DECIMAL",
        "tp1": "DECIMAL",
        "tp2": "DECIMAL",
        "tp3": "DECIMAL",
        "rr": "DECIMAL",
        "status": "VARCHAR(50) DEFAULT 'Waiting Entry'",
        "reason": "TEXT",
        "tech_score": "INT",
        "quant_score": "INT",
        "deriv_score": "INT",
        "smc_score": "INT DEFAULT 0",
        "z_score": "DECIMAL DEFAULT 0",
        "zeta_score": "DECIMAL DEFAULT 0",
        "obi": "DECIMAL DEFAULT 0",
        "basis": "DECIMAL",
        "btc_bias": "VARCHAR(50)",
        "tech_reasons": "TEXT",
        "quant_reasons": "TEXT",
        "deriv_reasons": "TEXT",
        "smc_reasons": "TEXT",
        "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "entry_hit_at": "TIMESTAMP",
        "closed_at": "TIMESTAMP",
        "exit_price": "DECIMAL",
        "message_id": "VARCHAR(50)",
        "channel_id": "VARCHAR(50)",
        "execution_mode": "VARCHAR(20) DEFAULT 'paper'",
    }

    try:
        cur.execute("SELECT to_regclass('public.trades');")
        if cur.fetchone()[0] is None:
            logger.info("Table 'trades' not found. Creating fresh...")
            cols = [f"{k} {v}" for k, v in required_columns.items()]
            query = f"CREATE TABLE trades ({', '.join(cols)});"
            cur.execute(query)
            logger.info("Table 'trades' created successfully.")
        else:
            logger.info("Checking 'trades' schema for missing columns...")
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'trades';")
            existing_cols = {row[0] for row in cur.fetchall()}

            missing_cols = []
            for col, dtype in required_columns.items():
                if col not in existing_cols:
                    clean_type = dtype.replace("SERIAL PRIMARY KEY", "INT").replace("PRIMARY KEY", "")
                    missing_cols.append(f"ADD COLUMN IF NOT EXISTS {col} {clean_type}")

            if missing_cols:
                logger.info(
                    f"Migrating trades schema: adding {len(missing_cols)} new columns "
                    f"({', '.join([c.split()[4] for c in missing_cols])})..."
                )
                alter_query = f"ALTER TABLE trades {', '.join(missing_cols)};"
                cur.execute(alter_query)
                logger.info('Migration complete.')
            else:
                logger.info('Trades schema is up to date.')

        cur.execute("CREATE TABLE IF NOT EXISTS bot_state (key_name VARCHAR(50) PRIMARY KEY, value_text TEXT);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_timeframe ON trades(symbol, timeframe);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);")
        conn.commit()
    except Exception as e:
        logger.error(f'Migration failed: {e}')
        conn.rollback()


def get_active_signals():
    conn = get_conn()
    try:
        cur = conn.cursor()
        current_mode = normalize_execution_mode(CONFIG.get('execution', {}).get('mode', 'paper'))
        cur.execute(
            """
            SELECT symbol, timeframe
            FROM trades
            WHERE status = ANY(%s)
            AND execution_mode = %s
            """
            , (list(ACTIVE_SIGNAL_STATUSES), current_mode)
        )
        return {(r[0], r[1]) for r in cur.fetchall()}
    except Exception as e:
        logger.warning(f'Error fetching active signals: {e}')
        return set()
    finally:
        release_conn(conn)


def get_conn():
    if not DB_POOL:
        init_db()
    return DB_POOL.getconn()


def release_conn(conn):
    if DB_POOL:
        DB_POOL.putconn(conn)
