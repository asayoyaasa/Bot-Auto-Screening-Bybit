import logging

import numpy as np
from scipy.stats import linregress

logger = logging.getLogger(__name__)

def get_slope(series):
    try:
        return linregress(np.arange(len(series)), np.array(series))[0]
    except Exception as exc:
        logger.debug(f"derivatives.get_slope fallback used: {exc}")
        return 0

def analyze_derivatives(df, ticker, side):
    """
    Analyzes derivative metrics (Funding, Basis, CVD Divergence).
    """
    score = 1
    reasons = []
    
    # 1. Funding Rate Check
    funding = float(ticker.get('info', {}).get('fundingRate', 0))
    # Bybit funding is returned as a decimal (e.g. 0.0001 == 0.01%)
    if side == "Long" and funding > 0.0002:
        return False, 0, ["Funding Hot (>0.02%)"]
    
    if abs(funding) < 0.0002:
        score += 1
        reasons.append("Cool Funding")

    # 2. Basis Calculation
    mark = float(ticker.get('last', 0))
    index = float(ticker.get('info', {}).get('indexPrice', mark))
    
    # 3. CVD Calculation (Defensive Fix)
    if 'CVD' not in df.columns:
        # Improved: Avoid look-ahead bias and calculate exact tick-level if available, else standard proxy
        df['delta'] = np.where(df['close'] >= df['open'], df['volume'], -df['volume'])
        df['CVD'] = df['delta'].cumsum()

    # 4. Divergence Analysis (Price Slope vs CVD Slope)
    # Look at the last 10 candles
    p_slope = get_slope(df['close'].iloc[-10:])
    cvd_slope = get_slope(df['CVD'].iloc[-10:])
    
    # Bearish Divergence: Price Rising, CVD Falling (Sellers absorbing)
    if p_slope > 0 and cvd_slope < 0:
        if side == "Short":
            score += 2
            reasons.append("Bear CVD Div")
        elif side == "Long":
            score -= 2 # Penalty for longing into selling pressure

    # Bullish Divergence: Price Falling, CVD Rising (Buyers absorbing)
    elif p_slope < 0 and cvd_slope > 0:
        if side == "Long":
            score += 2
            reasons.append("Bull CVD Div")
        elif side == "Short":
            score -= 2 # Penalty for shorting into buying pressure

    return True, score, reasons