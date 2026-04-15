from modules.config_loader import CONFIG
from modules.database import get_conn, release_conn
from modules.logging_setup import build_component_logger
from modules.notifications import send_event_message

from execution.paper_engine import compute_paper_equity
from modules.paper_trade_utils import normalize_execution_mode

logger = build_component_logger('Reporting', 'auto_trades.log', json_format=True, pii_mask=True)

EXECUTION_MODE = normalize_execution_mode(CONFIG.get('execution', {}).get('mode', 'paper'))
IS_PAPER = EXECUTION_MODE == 'paper'


def generate_daily_report():
    logger.info("📊 Generating Daily Report...")
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END),
                   SUM(pnl), MAX(pnl), MIN(pnl)
            FROM active_trades
            WHERE status = 'CLOSED' AND updated_at >= NOW() - INTERVAL '24 hours' AND execution_mode = %s
        """,
            (EXECUTION_MODE,),
        )
        row = cur.fetchone()
        if row and row[0] > 0:
            total, wins, losses, pnl, best, worst = row
            pnl = pnl if pnl else 0
            win_rate = (wins / total) * 100
            cur.execute("SELECT symbol FROM active_trades WHERE pnl = %s AND execution_mode = %s LIMIT 1", (best, EXECUTION_MODE))
            b_sym = cur.fetchone()
            best_sym = b_sym[0] if b_sym else '-'
            cur.execute("SELECT symbol FROM active_trades WHERE pnl = %s AND execution_mode = %s LIMIT 1", (worst, EXECUTION_MODE))
            w_sym = cur.fetchone()
            worst_sym = w_sym[0] if w_sym else '-'
            cur.execute(
                """
                INSERT INTO daily_reports (report_date, total_pnl, win_rate, total_wins, total_losses, total_trades, best_trade_symbol, best_trade_pnl, worst_trade_symbol, worst_trade_pnl, execution_mode)
                VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (report_date, execution_mode) DO UPDATE SET
                    total_pnl = EXCLUDED.total_pnl,
                    win_rate = EXCLUDED.win_rate,
                    total_wins = EXCLUDED.total_wins,
                    total_losses = EXCLUDED.total_losses,
                    total_trades = EXCLUDED.total_trades,
                    best_trade_symbol = EXCLUDED.best_trade_symbol,
                    best_trade_pnl = EXCLUDED.best_trade_pnl,
                    worst_trade_symbol = EXCLUDED.worst_trade_symbol,
                    worst_trade_pnl = EXCLUDED.worst_trade_pnl,
                    generated_at = CURRENT_TIMESTAMP
                """,
                (pnl, win_rate, wins, losses, total, best_sym, best, worst_sym, worst, EXECUTION_MODE),
            )
            conn.commit()
            logger.info(f"✅ Report Generated: ${pnl:.2f} ({wins}W/{losses}L)")
            report_lines = [f'Mode: {EXECUTION_MODE.upper()}', f'Trades: {total}', f'Wins: {wins}', f'Losses: {losses}', f'Win rate: {win_rate:.2f}%', f'PnL: {pnl}']
            if IS_PAPER:
                report_lines.append(f'Paper equity: {compute_paper_equity(cur):.2f}')
            send_event_message('Daily Trading Report', report_lines)
    except Exception as e:
        logger.error(f"Report Error: {e}")
    finally:
        release_conn(conn)
