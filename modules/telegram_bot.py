import requests, json, os, pytz, pandas as pd, numpy as np
import mplfinance as mpf
from scipy.signal import argrelextrema
from datetime import datetime
from psycopg2.extras import RealDictCursor
from modules.config_loader import CONFIG
from modules.database import get_conn, release_conn

# Telegram Bot API base
def _tg_api(method):
    token = CONFIG['api'].get('telegram_bot_token', '')
    return f"https://api.telegram.org/bot{token}/{method}"

CHAT_ID = None  # resolved lazily

def _get_chat_id():
    global CHAT_ID
    if CHAT_ID:
        return CHAT_ID
    CHAT_ID = CONFIG['api'].get('telegram_chat_id', '')
    return CHAT_ID

def get_now(): return datetime.now(pytz.timezone(CONFIG['system']['timezone']))
def format_price(value): return "{:.8f}".format(float(value)).rstrip('0').rstrip('.') if float(value) < 1 else "{:.2f}".format(float(value))

def _escape_html(text):
    """Escape special chars for Telegram HTML parse mode."""
    if not isinstance(text, str):
        text = str(text)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def _tg_send_message(text, chat_id=None):
    """Send a text message via Telegram Bot API (HTML parse mode)."""
    cid = chat_id or _get_chat_id()
    if not cid:
        print("❌ No telegram_chat_id configured")
        return None
    try:
        r = requests.post(_tg_api("sendMessage"), json={
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        if r.status_code != 200:
            print(f"❌ Telegram send_message error: {r.text}")
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"❌ Telegram send_message exception: {e}")
        return None

def _tg_send_photo(photo_path, caption, chat_id=None):
    """Send a photo with caption via Telegram Bot API."""
    cid = chat_id or _get_chat_id()
    if not cid:
        print("❌ No telegram_chat_id configured")
        return None
    try:
        with open(photo_path, 'rb') as f:
            r = requests.post(_tg_api("sendPhoto"), data={
                "chat_id": cid,
                "caption": caption,
                "parse_mode": "HTML"
            }, files={"photo": f}, timeout=30)
        if r.status_code != 200:
            print(f"❌ Telegram send_photo error: {r.text}")
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"❌ Telegram send_photo exception: {e}")
        return None

def _tg_edit_message(text, message_id, chat_id=None):
    """Edit an existing message (for dashboard updates)."""
    cid = chat_id or _get_chat_id()
    if not cid or not message_id:
        return None
    try:
        r = requests.post(_tg_api("editMessageText"), json={
            "chat_id": cid,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"❌ Telegram edit_message exception: {e}")
        return None

def generate_chart(df, symbol, pattern, timeframe):
    filename = f"chart_{symbol.replace('/','')}_{timeframe}.png"
    try:
        plot_df = df.iloc[-100:].copy()
        if 'timestamp' in plot_df.columns: plot_df.set_index('timestamp', inplace=True)
        plot_df.index = pd.to_datetime(plot_df.index)

        n=3
        min_idx = argrelextrema(plot_df['low'].values, np.less_equal, order=n)[0]
        max_idx = argrelextrema(plot_df['high'].values, np.greater_equal, order=n)[0]
        
        peak_dates, peak_vals = plot_df.index[max_idx], plot_df['high'].iloc[max_idx].values
        valley_dates, valley_vals = plot_df.index[min_idx], plot_df['low'].iloc[min_idx].values
        
        lines, colors = [], []
        def add_line(dates, vals, color):
            if len(dates) >= 2: lines.append([(str(dates[-2]), float(vals[-2])), (str(dates[-1]), float(vals[-1]))]); colors.append(color)

        if pattern in ['ascending_triangle', 'bullish_rectangle', 'double_top', 'bear_flag', 'descending_triangle']: add_line(peak_dates, peak_vals, 'red')
        if pattern in ['descending_triangle', 'bullish_rectangle', 'double_bottom', 'bull_flag', 'ascending_triangle']: add_line(valley_dates, valley_vals, 'green')

        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc)
        apds = []
        if 'EMA_Fast' in plot_df.columns: apds.append(mpf.make_addplot(plot_df['EMA_Fast'], color='cyan', width=1))
        
        ratios, vol_panel = (3, 1), 1
        if 'MACD_h' in plot_df.columns:
            cols = ['#2ebd85' if v >= 0 else '#f6465d' for v in plot_df['MACD_h']]
            apds.append(mpf.make_addplot(plot_df['MACD_h'], type='bar', panel=1, color=cols, ylabel='MACD'))
            ratios, vol_panel = (3, 1, 1), 2

        kwargs = dict(type='candle', style=s, addplot=apds, title=f"\n{symbol} ({timeframe}) - {pattern}", figsize=(12, 8), panel_ratios=ratios, volume=True, volume_panel=vol_panel, savefig=dict(fname=filename, dpi=100, bbox_inches='tight'))
        if lines: kwargs['alines'] = dict(alines=lines, colors=colors, linewidths=1.5, alpha=0.7)
        mpf.plot(plot_df, **kwargs)
        return filename
    except Exception as e: print(f"Chart Error: {e}"); return None


def send_alert(data):
    token = CONFIG['api'].get('telegram_bot_token', '')
    if not token: return False
    
    symbol = data['Symbol']
    esc = _escape_html
    
    # 1. Generate Chart
    image_path = None
    try:
        image_path = generate_chart(data['df'], symbol, data['Pattern'], data['Timeframe'])
    except Exception as e:
        print(f"❌ Chart Error: {e}")

    # 2. Prepare Data
    try:
        is_long = data['Side'] == 'Long'
        emoji = "🚀" if is_long else "🔻"
        
        # --- QUANT BLOCK ---
        rvol = data['df']['RVOL'].iloc[-1]
        rvol_txt = "⚡ Explosive" if rvol > 3.0 else ("🔥 Strong" if rvol > 2.0 else "🌊 Normal")
        obi_val = data.get('OBI', 0.0)
        obi_icon = "🟢" if obi_val > 0 else ("🔴" if obi_val < 0 else "⚪")
        
        # --- DERIVATIVE BLOCK ---
        fund_rate = data['df'].get('funding', pd.Series([0])).iloc[-1]
        if isinstance(fund_rate, pd.Series): fund_rate = fund_rate.iloc[-1]
        
        fund_pct = fund_rate * 100
        fund_icon = "🔴" if fund_pct > 0.01 else "🟢"
        fund_txt = "Hot" if fund_pct > 0.01 else "Cool"
        
        basis_pct = data.get('Basis', 0) * 100

        # --- SMC TEXT ---
        smc_reasons_str = str(data.get('SMC_Reasons', ''))
        smc_txt = "None"
        if "Order Block" in smc_reasons_str:
            smc_txt = "🟢 Demand Zone" if "Bullish" in smc_reasons_str else "🔴 Supply Zone"
        elif "Structure" in smc_reasons_str:
            smc_txt = "📈 Higher Low" if "Higher Low" in smc_reasons_str else "📉 Lower High"
        elif data['SMC_Score'] > 0:
            smc_txt = "✅ Confluence Found"

        # 3. Build Telegram HTML message
        caption = (
            f"{emoji} <b>SIGNAL: {esc(symbol)}</b> ({esc(data['Pattern'])})\n"
            f"<b>{esc(data['Side'])}</b> | <b>{esc(data['Timeframe'])}</b>\n"
            f"\n"
            f"🎯 <b>Entry:</b> <code>{esc(format_price(data['Entry']))}</code>\n"
            f"🛑 <b>Stop:</b> <code>{esc(format_price(data['SL']))}</code>\n"
            f"💰 <b>RR:</b> 1:{esc(data.get('RR', 0.0))}\n"
            f"\n"
            f"🏁 <b>Targets</b>\n"
            f"  TP1: <code>{esc(format_price(data['TP1']))}</code>\n"
            f"  TP2: <code>{esc(format_price(data['TP2']))}</code>\n"
            f"  TP3: <code>{esc(format_price(data['TP3']))}</code>\n"
            f"\n"
            f"📊 <b>Technicals</b>\n"
            f"  Pattern: {esc(data['Pattern'])}\n"
            f"  Trend: {emoji} {esc(data['Side'])}\n"
            f"  SMC: {smc_txt}\n"
            f"\n"
            f"🧮 <b>Quant Models</b>\n"
            f"  RVOL: <code>{rvol:.1f}x</code> ({rvol_txt})\n"
            f"  Z-Score: <code>{esc(data.get('Z_Score', 0)):.2f}σ</code>\n"
            f"  ζ-Field: <code>{esc(data.get('Zeta_Score', 0)):.1f}</code> / 100\n"
            f"  OBI: <code>{obi_val:.2f}</code> {obi_icon}\n"
            f"\n"
            f"⛽ <b>Derivatives</b>\n"
            f"  Funding: <code>{fund_pct:.4f}%</code> {fund_icon} ({fund_txt})\n"
            f"  Basis: <code>{basis_pct:.4f}%</code>\n"
            f"  Bias: {esc(data.get('Deriv_Reasons', 'Neutral'))}\n"
            f"\n"
            f"🏆 <b>Scores</b>\n"
            f"  Tech: <code>{data['Tech_Score']}</code> | SMC: <code>{data['SMC_Score']}</code> | Quant: <code>{data['Quant_Score']}</code> | Deriv: <code>{data['Deriv_Score']}</code>\n"
            f"\n"
            f"📝 <b>Detailed Analysis</b>\n"
            f"  Tech: {esc(data.get('Tech_Reasons', '-'))}\n"
            f"  SMC: {esc(smc_reasons_str if smc_reasons_str else '-')}\n"
            f"  Quant: {esc(data.get('Quant_Reasons', '-'))}\n"
            f"\n"
            f"🧠 <b>Context:</b> Bias: {esc(data['BTC_Bias'])}\n"
            f"\n"
            f"<i>V8 Bot | {esc(get_now().strftime('%Y-%m-%d %H:%M:%S'))}</i>"
        )

        # 4. Send via Telegram
        tg_resp = None
        if image_path:
            tg_resp = _tg_send_photo(image_path, caption)
        else:
            tg_resp = _tg_send_message(caption)
            
        # 5. Save to DB
        if tg_resp and tg_resp.get('ok'):
            result = tg_resp['result']
            msg_id = str(result.get('message_id', ''))
            chat_id_str = str(result.get('chat', {}).get('id', ''))
            
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades (symbol, side, timeframe, pattern, entry_price, sl_price, tp1, tp2, tp3, reason, 
                tech_score, quant_score, deriv_score, smc_score, basis, btc_bias, z_score, zeta_score, obi, 
                tech_reasons, quant_reasons, deriv_reasons, smc_reasons, message_id, channel_id, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Waiting Entry')
            """, (symbol, data['Side'], data['Timeframe'], data['Pattern'], data['Entry'], data['SL'], data['TP1'], 
                  data['TP2'], data['TP3'], data['Reason'], data['Tech_Score'], data['Quant_Score'], data['Deriv_Score'],
                  data['SMC_Score'], data['Basis'], data['BTC_Bias'], data['Z_Score'], data['Zeta_Score'], data['OBI'],
                  data.get('Tech_Reasons',''), data.get('Quant_Reasons',''), data.get('Deriv_Reasons',''), smc_reasons_str,
                  msg_id, chat_id_str))
            conn.commit()
            release_conn(conn)
            return True
            
    except Exception as e:
        print(f"Alert Error: {e}")
        return False
    finally:
        if image_path and os.path.exists(image_path): os.remove(image_path)
    return False

def update_status_dashboard():
    token = CONFIG['api'].get('telegram_bot_token', '')
    if not token: return
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT symbol, side, status, entry_hit_at, created_at FROM trades WHERE status NOT LIKE '%Closed%' ORDER BY created_at DESC")
        trades = cur.fetchall()
        lines = [f"<code>{(t['entry_hit_at'] or t['created_at']).strftime('%H:%M')}</code> {'🟢' if 'Active' in t['status'] else '⏳'} <b>{t['symbol']}</b> ({t['side']}): {t['status']}" for t in trades]
        content = "📊 <b>LIVE DASHBOARD</b>\n" + ("\n".join(lines) if lines else "No active trades.")
        
        cur.execute("SELECT value_text FROM bot_state WHERE key_name = 'dashboard_msg_id'")
        row = cur.fetchone()
        msg_id = row[0] if row else None
        
        if msg_id:
            _tg_edit_message(content, msg_id)
        else:
            resp = _tg_send_message(content)
            if resp and resp.get('ok'):
                new_id = str(resp['result']['message_id'])
                cur.execute("INSERT INTO bot_state (key_name, value_text) VALUES ('dashboard_msg_id', %s) ON CONFLICT (key_name) DO UPDATE SET value_text = EXCLUDED.value_text", (new_id,))
                conn.commit()
    except Exception as e:
        print(f"Dashboard error: {e}")
    finally: release_conn(conn)

def run_fast_update(): update_status_dashboard()

def send_scan_completion(count, duration, bias):
    token = CONFIG['api'].get('telegram_bot_token', '')
    if not token: return
    esc = _escape_html
    text = (
        f"🔭 <b>Scan Cycle Complete</b>\n"
        f"⏱️ Duration: <code>{duration:.2f}s</code>\n"
        f"📶 Signals: <code>{count}</code>\n"
        f"📊 Bias: <b>{esc(bias)}</b>"
    )
    try: _tg_send_message(text)
    except: pass
