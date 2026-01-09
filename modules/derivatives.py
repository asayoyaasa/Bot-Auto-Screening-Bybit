import numpy as np
from scipy.stats import linregress

def get_slope(series):
    try: return linregress(np.arange(len(series)), np.array(series))[0]
    except: return 0

def analyze_derivatives(df, ticker, side):
    score = 1
    reasons = []
    
    funding = float(ticker.get('info', {}).get('fundingRate', 0))
    if side == "Long" and funding > 0.02: return False, 0, ["Funding Hot"]
    if abs(funding) < 0.01: score += 1; reasons.append("Cool Funding")

    mark = float(ticker.get('last', 0))
    index = float(ticker.get('info', {}).get('indexPrice', mark))
    basis = (mark - index) / index if index > 0 else 0
    
    p_slope = get_slope(df['close'].iloc[-10:])
    cvd_slope = get_slope(df['CVD'].iloc[-10:])
    
    if p_slope > 0 and cvd_slope < 0:
        if side == "Short": score += 2; reasons.append("Bear CVD Div")
        elif side == "Long": score -= 2
    elif p_slope < 0 and cvd_slope > 0:
        if side == "Long": score += 2; reasons.append("Bull CVD Div")
        elif side == "Short": score -= 2

    return True, score, reasons