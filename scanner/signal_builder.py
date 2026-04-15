import logging
import random

import pandas as pd
import pandas_ta as ta

from modules.config_loader import CONFIG
from modules.derivatives import analyze_derivatives
from modules.logging_setup import build_component_logger
from modules.patterns import find_pattern
from modules.quant import calculate_metrics, check_fakeout
from modules.runtime_utils import retry_call
from modules.smc import analyze_smc
from modules.technicals import detect_divergence, get_technicals

logger = build_component_logger('Scanner', 'scanner.log')


def calculate_rr(entry, sl, tp3):
    if entry <= 0 or sl <= 0 or tp3 <= 0:
        return 0.0
    risk = abs(entry - sl)
    return round(abs(tp3 - entry) / risk, 2) if risk > 0 else 0.0


def get_btc_bias(fetch_ohlcv, *, context='fetch BTC bias candles'):
    try:
        bars = fetch_ohlcv('BTC/USDT', '1d', limit=100, context=context)
        if not bars:
            return 'Sideways'
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        df['ema13'] = ta.ema(df['c'], length=13)
        df['ema21'] = ta.ema(df['c'], length=21)
        curr = df.iloc[-1]
        return 'Bullish' if curr['ema13'] > curr['ema21'] else 'Bearish'
    except Exception as e:
        logger.warning(f'Failed to compute BTC bias: {e}')
        return 'Sideways'


def analyze_ticker(exchange_call, symbol, timeframe, btc_bias, active_signals):
    if (symbol, timeframe) in active_signals:
        return None

    try:
        ticker_info = exchange_call('fetch_ticker', symbol, context=f'fetch ticker {symbol}')
        if "ST" in ticker_info.get('info', {}).get('symbol', ''):
            return None

        min_candles = CONFIG['system'].get('min_candles_analysis', 150)
        bars = exchange_call('fetch_ohlcv', symbol, timeframe, limit=min_candles + 50, context=f'fetch candles {symbol} {timeframe}')
        if not bars or len(bars) < min_candles:
            return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = get_technicals(df)
        pattern = find_pattern(df)
        if not pattern:
            return None

        side = CONFIG['pattern_signals'].get(pattern)
        if not side:
            logger.warning(f'Pattern {pattern} has no configured side')
            return None

        valid_smc, smc_score, smc_reasons = analyze_smc(df, side)
        if smc_score < CONFIG['strategy'].get('min_smc_score', 0):
            return None
        if not valid_smc and CONFIG['strategy'].get('require_valid_smc', False):
            return None

        df, basis, z_score, zeta_score, obi, quant_score, quant_reasons = calculate_metrics(df, ticker_info)
        valid_deriv, deriv_score, deriv_reasons = analyze_derivatives(df, ticker_info, side)
        if not valid_deriv:
            return None
        if deriv_score < CONFIG['strategy'].get('min_deriv_score', 0):
            return None

        div_score, div_msg = detect_divergence(df)
        tech_score = 3 + div_score
        tech_reasons = [f'Pattern: {pattern}', div_msg] + smc_reasons

        if 'Bearish' in btc_bias and side == 'Long':
            return None
        if 'Bullish' in btc_bias and side == 'Short':
            return None

        valid_fo, _ = check_fakeout(df, CONFIG['indicators']['min_rvol'])
        if not valid_fo:
            return None
        if tech_score < CONFIG['strategy']['min_tech_score']:
            return None

        s = CONFIG['setup']
        swing_high = df['high'].iloc[-50:].max()
        swing_low = df['low'].iloc[-50:].min()
        rng = swing_high - swing_low
        if rng <= 0:
            return None

        if side == 'Long':
            entry = (swing_high - (rng * s['fib_entry_start']) + swing_high - (rng * s['fib_entry_end'])) / 2
            sl = swing_low - (rng * s['fib_sl'])
            tp1, tp2, tp3 = swing_low + rng, swing_low + (rng * 1.618), swing_low + (rng * 2.618)
        else:
            entry = (swing_low + (rng * s['fib_entry_start']) + swing_low + (rng * s['fib_entry_end'])) / 2
            sl = swing_high + (rng * s['fib_sl'])
            tp1, tp2, tp3 = swing_high - rng, swing_high - (rng * 1.618), swing_high - (rng * 2.618)

        rr = calculate_rr(entry, sl, tp3)
        if rr < CONFIG['strategy'].get('risk_reward_min', 2.0):
            return None

        df['funding'] = float(ticker_info.get('info', {}).get('fundingRate', 0))
        return {
            'Symbol': symbol,
            'Side': side,
            'Timeframe': timeframe,
            'Pattern': pattern,
            'Entry': float(entry),
            'SL': float(sl),
            'TP1': float(tp1),
            'TP2': float(tp2),
            'TP3': float(tp3),
            'RR': float(rr),
            'Tech_Score': int(tech_score),
            'Quant_Score': int(quant_score),
            'Deriv_Score': int(deriv_score),
            'SMC_Score': int(smc_score),
            'Basis': float(basis),
            'Z_Score': float(z_score),
            'Zeta_Score': float(zeta_score),
            'OBI': float(obi),
            'BTC_Bias': btc_bias,
            'Reason': pattern,
            'Tech_Reasons': ', '.join([r for r in tech_reasons if r]),
            'Quant_Reasons': ', '.join(quant_reasons),
            'SMC_Reasons': ', '.join([r for r in smc_reasons if r]),
            'Deriv_Reasons': ', '.join(deriv_reasons),
            'df': df,
        }
    except Exception as e:
        logger.warning(f'Analyze ticker failed for {symbol} {timeframe}: {e}')
        return None
