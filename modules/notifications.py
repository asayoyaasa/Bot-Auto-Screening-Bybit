import json
import os
from datetime import datetime

import mplfinance as mpf
import numpy as np
import pandas as pd
import pytz
import requests
from psycopg2.extras import RealDictCursor
from scipy.signal import argrelextrema

from modules.config_loader import CONFIG
from modules.database import ACTIVE_SIGNAL_STATUSES, get_conn, release_conn
from modules.runtime_utils import retry_call
from modules.control import get_status_snapshot, get_last_telegram_update_id, set_last_telegram_update_id, set_paused


def get_now():
    return datetime.now(pytz.timezone(CONFIG['system']['timezone']))


def format_price(value):
    return "{:.8f}".format(float(value)).rstrip('0').rstrip('.') if float(value) < 1 else "{:.2f}".format(float(value))


def _escape_html(text):
    if not isinstance(text, str):
        text = str(text)
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _notifications_cfg():
    return CONFIG.get('notifications', {})


def execution_mode():
    return str(CONFIG.get('execution', {}).get('mode', 'paper')).strip().lower()


def mode_tag():
    return f"[{execution_mode().upper()}]"


def telegram_enabled():
    n = _notifications_cfg()
    return bool(n.get('telegram_enabled') and CONFIG['api'].get('telegram_bot_token') and CONFIG['api'].get('telegram_chat_id'))


def discord_enabled():
    n = _notifications_cfg()
    return bool(n.get('discord_enabled') and CONFIG['api'].get('discord_webhook'))


# ---------- Telegram ----------
def _tg_api(method):
    token = CONFIG['api'].get('telegram_bot_token', '')
    return f"https://api.telegram.org/bot{token}/{method}"


def _tg_chat_id():
    return CONFIG['api'].get('telegram_chat_id', '')


def _post_telegram(method, *, json=None, data=None, files=None, timeout=15):
    return retry_call(
        requests.post,
        _tg_api(method),
        json=json,
        data=data,
        files=files,
        timeout=timeout,
        retries=3,
        base_delay=1.0,
        context=f"Telegram {method}",
    )


def _tg_send_message(text, chat_id=None):
    cid = chat_id or _tg_chat_id()
    if not cid:
        return None
    try:
        r = _post_telegram(
            'sendMessage',
            json={
                'chat_id': cid,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            },
            timeout=15,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _tg_send_photo(photo_path, caption, chat_id=None):
    cid = chat_id or _tg_chat_id()
    if not cid:
        return None
    try:
        with open(photo_path, 'rb') as f:
            r = _post_telegram(
                'sendPhoto',
                data={'chat_id': cid, 'caption': caption, 'parse_mode': 'HTML'},
                files={'photo': f},
                timeout=30,
            )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _tg_edit_message(text, message_id, chat_id=None):
    cid = chat_id or _tg_chat_id()
    if not cid or not message_id:
        return None
    try:
        r = _post_telegram(
            'editMessageText',
            json={
                'chat_id': cid,
                'message_id': message_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            },
            timeout=15,
        )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _tg_get_updates(offset=None):
    payload = {'timeout': 1}
    if offset is not None:
        payload['offset'] = offset
    try:
        r = _post_telegram('getUpdates', json=payload, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# ---------- Discord ----------
def _discord_webhook():
    return CONFIG['api'].get('discord_webhook', '')


def _discord_send_json(payload):
    webhook = _discord_webhook()
    if not webhook:
        return None
    try:
        r = retry_call(
            requests.post,
            webhook,
            json=payload,
            timeout=20,
            retries=3,
            base_delay=1.0,
            context='Discord webhook json',
        )
        return r
    except Exception:
        return None


def _discord_send_with_file(payload, file_path):
    webhook = _discord_webhook()
    if not webhook:
        return None
    try:
        with open(file_path, 'rb') as f:
            r = retry_call(
                requests.post,
                webhook,
                data={'payload_json': json.dumps(payload)},
                files={'file': f},
                timeout=30,
                retries=3,
                base_delay=1.0,
                context='Discord webhook file',
            )
        return r
    except Exception:
        return None


def _discord_send_json_wait(payload):
    webhook = _discord_webhook()
    if not webhook:
        return None
    separator = '&' if '?' in webhook else '?'
    wait_url = webhook if 'wait=true' in webhook else f"{webhook}{separator}wait=true"
    try:
        return retry_call(
            requests.post,
            wait_url,
            json=payload,
            timeout=20,
            retries=3,
            base_delay=1.0,
            context='Discord webhook json wait',
        )
    except Exception:
        return None


def _discord_edit_message(message_id, payload):
    webhook = _discord_webhook()
    if not webhook or not message_id:
        return None
    try:
        return retry_call(
            requests.patch,
            f"{webhook}/messages/{message_id}",
            json=payload,
            timeout=20,
            retries=3,
            base_delay=1.0,
            context='Discord webhook edit',
        )
    except Exception:
        return None


# ---------- Shared formatting ----------
def generate_chart(df, symbol, pattern, timeframe):
    filename = f"chart_{symbol.replace('/', '')}_{timeframe}.png"
    try:
        plot_df = df.iloc[-100:].copy()
        if 'timestamp' in plot_df.columns:
            plot_df.set_index('timestamp', inplace=True)
        plot_df.index = pd.to_datetime(plot_df.index)

        n = 3
        min_idx = argrelextrema(plot_df['low'].values, np.less_equal, order=n)[0]
        max_idx = argrelextrema(plot_df['high'].values, np.greater_equal, order=n)[0]
        peak_dates, peak_vals = plot_df.index[max_idx], plot_df['high'].iloc[max_idx].values
        valley_dates, valley_vals = plot_df.index[min_idx], plot_df['low'].iloc[min_idx].values

        lines, colors = [], []

        def add_line(dates, vals, color):
            if len(dates) >= 2:
                lines.append([(str(dates[-2]), float(vals[-2])), (str(dates[-1]), float(vals[-1]))])
                colors.append(color)

        if pattern in ['ascending_triangle', 'bullish_rectangle', 'double_top', 'bear_flag', 'descending_triangle']:
            add_line(peak_dates, peak_vals, 'red')
        if pattern in ['descending_triangle', 'bullish_rectangle', 'double_bottom', 'bull_flag', 'ascending_triangle']:
            add_line(valley_dates, valley_vals, 'green')

        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc)
        apds = []
        if 'EMA_Fast' in plot_df.columns:
            apds.append(mpf.make_addplot(plot_df['EMA_Fast'], color='cyan', width=1))

        ratios, vol_panel = (3, 1), 1
        if 'MACD_h' in plot_df.columns:
            cols = ['#2ebd85' if v >= 0 else '#f6465d' for v in plot_df['MACD_h']]
            apds.append(mpf.make_addplot(plot_df['MACD_h'], type='bar', panel=1, color=cols, ylabel='MACD'))
            ratios, vol_panel = (3, 1, 1), 2

        kwargs = dict(
            type='candle', style=style, addplot=apds,
            title=f"\n{symbol} ({timeframe}) - {pattern}", figsize=(12, 8),
            panel_ratios=ratios, volume=True, volume_panel=vol_panel,
            savefig=dict(fname=filename, dpi=100, bbox_inches='tight'),
        )
        if lines:
            kwargs['alines'] = dict(alines=lines, colors=colors, linewidths=1.5, alpha=0.7)
        mpf.plot(plot_df, **kwargs)
        return filename
    except Exception:
        return None


def _build_signal_caption(data):
    esc = _escape_html
    is_long = data['Side'] == 'Long'
    emoji = '🚀' if is_long else '🔻'
    rvol = data['df']['RVOL'].iloc[-1]
    rvol_txt = '⚡ Explosive' if rvol > 3.0 else ('🔥 Strong' if rvol > 2.0 else '🌊 Normal')
    obi_val = data.get('OBI', 0.0)
    obi_icon = '🟢' if obi_val > 0 else ('🔴' if obi_val < 0 else '⚪')
    fund_rate = data['df'].get('funding', pd.Series([0])).iloc[-1]
    if isinstance(fund_rate, pd.Series):
        fund_rate = fund_rate.iloc[-1]
    fund_pct = float(fund_rate) * 100
    fund_icon = '🔴' if fund_pct > 0.01 else '🟢'
    fund_txt = 'Hot' if fund_pct > 0.01 else 'Cool'
    basis_pct = float(data.get('Basis', 0)) * 100
    smc_reasons_str = str(data.get('SMC_Reasons', ''))
    smc_txt = 'None'
    if 'Order Block' in smc_reasons_str:
        smc_txt = '🟢 Demand Zone' if 'Bullish' in smc_reasons_str else '🔴 Supply Zone'
    elif 'Structure' in smc_reasons_str:
        smc_txt = '📈 Higher Low' if 'Higher Low' in smc_reasons_str else '📉 Lower High'
    elif data['SMC_Score'] > 0:
        smc_txt = '✅ Confluence Found'

    return (
        f"{emoji} <b>{esc(mode_tag())} SIGNAL: {esc(data['Symbol'])}</b> ({esc(data['Pattern'])})\n"
        f"<b>{esc(data['Side'])}</b> | <b>{esc(data['Timeframe'])}</b>\n\n"
        f"🎯 <b>Entry:</b> <code>{esc(format_price(data['Entry']))}</code>\n"
        f"🛑 <b>Stop:</b> <code>{esc(format_price(data['SL']))}</code>\n"
        f"💰 <b>RR:</b> 1:{float(data.get('RR', 0.0)):.2f}\n\n"
        f"🏁 <b>Targets</b>\n"
        f"TP1: <code>{esc(format_price(data['TP1']))}</code>\n"
        f"TP2: <code>{esc(format_price(data['TP2']))}</code>\n"
        f"TP3: <code>{esc(format_price(data['TP3']))}</code>\n\n"
        f"📊 <b>Technicals</b>\nPattern: {esc(data['Pattern'])}\nTrend: {emoji} {esc(data['Side'])}\nSMC: {smc_txt}\n\n"
        f"🧮 <b>Quant Models</b>\nRVOL: <code>{rvol:.1f}x</code> ({rvol_txt})\n"
        f"Z-Score: <code>{float(data.get('Z_Score', 0)):.2f}σ</code>\nζ-Field: <code>{float(data.get('Zeta_Score', 0)):.1f}</code> / 100\n"
        f"OBI: <code>{obi_val:.2f}</code> {obi_icon}\n\n"
        f"⛽ <b>Derivatives</b>\nFunding: <code>{fund_pct:.4f}%</code> {fund_icon} ({fund_txt})\n"
        f"Basis: <code>{basis_pct:.4f}%</code>\nBias: {esc(data.get('Deriv_Reasons', 'Neutral'))}\n\n"
        f"🏆 <b>Scores</b>\nTech: <code>{int(data['Tech_Score'])}</code> | SMC: <code>{int(data['SMC_Score'])}</code> | Quant: <code>{int(data['Quant_Score'])}</code> | Deriv: <code>{int(data['Deriv_Score'])}</code>\n\n"
        f"📝 <b>Detailed Analysis</b>\nTech: {esc(data.get('Tech_Reasons', '-'))}\nSMC: {esc(smc_reasons_str if smc_reasons_str else '-')}\n"
        f"Quant: {esc(data.get('Quant_Reasons', '-'))}\n\n"
        f"🧠 <b>Context:</b> Bias: {esc(data['BTC_Bias'])}\n\n"
        f"<i>V8 Bot | {esc(get_now().strftime('%Y-%m-%d %H:%M:%S'))}</i>"
    )


def send_event_message(title, body_lines):
    full_title = f"{mode_tag()} {title}"
    if telegram_enabled():
        text = f"<b>{_escape_html(full_title)}</b>\n" + "\n".join(_escape_html(line) for line in body_lines if line)
        _tg_send_message(text)
    if discord_enabled():
        payload = {'content': f"**{full_title}**\n" + "\n".join(body_lines)}
        _discord_send_json(payload)
    return True


def send_alert(data):
    image_path = None
    try:
        image_path = generate_chart(data['df'], data['Symbol'], data['Pattern'], data['Timeframe'])
        caption = _build_signal_caption(data)
        sent = False

        telegram_result = None
        if telegram_enabled():
            telegram_result = _tg_send_photo(image_path, caption) if image_path else _tg_send_message(caption)
            sent = sent or bool(telegram_result and telegram_result.get('ok'))

        if discord_enabled():
            discord_payload = {'content': caption.replace('<b>', '**').replace('</b>', '**').replace('<code>', '`').replace('</code>', '`').replace('<i>', '*').replace('</i>', '*')}
            if image_path:
                r = _discord_send_with_file(discord_payload, image_path)
            else:
                r = _discord_send_json(discord_payload)
            sent = sent or bool(r and r.status_code in (200, 204))

        if sent:
            conn = get_conn()
            try:
                cur = conn.cursor()
                msg_id = ''
                channel_id = ''
                if telegram_result and telegram_result.get('ok'):
                    result = telegram_result['result']
                    msg_id = str(result.get('message_id', ''))
                    channel_id = str(result.get('chat', {}).get('id', ''))
                cur.execute(
                    """
                    INSERT INTO trades (symbol, side, timeframe, pattern, entry_price, sl_price, tp1, tp2, tp3, reason,
                    tech_score, quant_score, deriv_score, smc_score, basis, btc_bias, z_score, zeta_score, obi,
                    tech_reasons, quant_reasons, deriv_reasons, smc_reasons, message_id, channel_id, execution_mode, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Waiting Entry')
                    """,
                    (
                        data['Symbol'], data['Side'], data['Timeframe'], data['Pattern'], data['Entry'], data['SL'], data['TP1'],
                        data['TP2'], data['TP3'], data['Reason'], data['Tech_Score'], data['Quant_Score'], data['Deriv_Score'],
                        data['SMC_Score'], data['Basis'], data['BTC_Bias'], data['Z_Score'], data['Zeta_Score'], data['OBI'],
                        data.get('Tech_Reasons', ''), data.get('Quant_Reasons', ''), data.get('Deriv_Reasons', ''), data.get('SMC_Reasons', ''),
                        msg_id, channel_id, execution_mode(),
                    ),
                )
                conn.commit()
            finally:
                release_conn(conn)

        return sent
    finally:
        if image_path and os.path.exists(image_path):
            os.remove(image_path)


def update_status_dashboard():
    conn = get_conn()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT symbol, side, status, entry_hit_at, created_at FROM trades WHERE status = ANY(%s) AND execution_mode = %s ORDER BY created_at DESC",
            (list(ACTIVE_SIGNAL_STATUSES), execution_mode()),
        )
        trades = cur.fetchall()
        lines = [f"{(t['entry_hit_at'] or t['created_at']).strftime('%H:%M')} {'🟢' if t['status'] == 'Active' else '⏳'} {t['symbol']} ({t['side']}): {t['status']}" for t in trades]
        plain = f"📊 {mode_tag()} DASHBOARD\n" + ("\n".join(lines) if lines else "No active trades.")
        html = f"📊 <b>{_escape_html(mode_tag())} DASHBOARD</b>\n" + ("\n".join([f"<code>{(t['entry_hit_at'] or t['created_at']).strftime('%H:%M')}</code> {'🟢' if t['status'] == 'Active' else '⏳'} <b>{_escape_html(t['symbol'])}</b> ({_escape_html(t['side'])}): {_escape_html(t['status'])}" for t in trades]) if trades else "No active trades.")

        cur.execute("SELECT value_text FROM bot_state WHERE key_name = 'dashboard_msg_id'")
        row = cur.fetchone()
        msg_id = row[0] if row else None

        cur.execute("SELECT value_text FROM bot_state WHERE key_name = 'dashboard_discord_msg_id'")
        discord_row = cur.fetchone()
        discord_msg_id = discord_row[0] if discord_row else None

        if telegram_enabled():
            if msg_id:
                _tg_edit_message(html, msg_id)
            else:
                resp = _tg_send_message(html)
                if resp and resp.get('ok'):
                    new_id = str(resp['result']['message_id'])
                    cur.execute("INSERT INTO bot_state (key_name, value_text) VALUES ('dashboard_msg_id', %s) ON CONFLICT (key_name) DO UPDATE SET value_text = EXCLUDED.value_text", (new_id,))
                    conn.commit()

        if discord_enabled():
            discord_payload = {'content': plain}
            response = None
            if discord_msg_id:
                response = _discord_edit_message(discord_msg_id, discord_payload)
            if not response or response.status_code not in (200, 204):
                response = _discord_send_json_wait(discord_payload)
                if response and response.status_code == 200:
                    try:
                        discord_data = response.json()
                        new_discord_id = str(discord_data.get('id', ''))
                        if new_discord_id:
                            cur.execute(
                                "INSERT INTO bot_state (key_name, value_text) VALUES ('dashboard_discord_msg_id', %s) ON CONFLICT (key_name) DO UPDATE SET value_text = EXCLUDED.value_text",
                                (new_discord_id,),
                            )
                            conn.commit()
                    except Exception:
                        pass
    finally:
        release_conn(conn)


def run_fast_update():
    update_status_dashboard()
    poll_telegram_commands()


def send_scan_completion(count, duration, bias):
    send_event_message(
        'Scan Cycle Complete',
        [f'Duration: {duration:.2f}s', f'Signals: {count}', f'Bias: {bias}'],
    )


def poll_telegram_commands():
    if not telegram_enabled() or not _notifications_cfg().get('telegram_control_enabled', True):
        return
    last_id = get_last_telegram_update_id(0)
    updates = _tg_get_updates(offset=last_id + 1)
    if not updates or not updates.get('ok'):
        return

    for update in updates.get('result', []):
        update_id = update.get('update_id', 0)
        message = update.get('message', {})
        chat = message.get('chat', {})
        chat_id = str(chat.get('id', ''))
        text = (message.get('text') or '').strip()
        if chat_id != str(_tg_chat_id()) or not text.startswith('/'):
            set_last_telegram_update_id(update_id)
            continue

        if text.startswith('/pause'):
            reason = text[len('/pause'):].strip()
            set_paused(True, reason)
            _tg_send_message(f"<b>Bot paused.</b>\n{_escape_html(reason or 'No reason provided.')}")
        elif text.startswith('/resume'):
            set_paused(False, '')
            _tg_send_message("<b>Bot resumed.</b>")
        elif text.startswith('/status'):
            snap = get_status_snapshot()
            lines = [
                f"Mode: {str(snap.get('mode', 'paper')).upper()}",
                f"Paused: {snap['paused'].get('paused')}",
                f"Reason: {snap['paused'].get('reason') or '-'}",
                f"Active signals: {snap['active_signals']}",
                f"Active positions: {snap['active_positions']}",
            ]
            _tg_send_message("<b>Bot status</b>\n" + "\n".join(_escape_html(x) for x in lines))
        elif text.startswith('/help'):
            _tg_send_message("<b>Commands</b>\n/status\n/pause [reason]\n/resume\n/help")

        set_last_telegram_update_id(update_id)
