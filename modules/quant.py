import numpy as np
import pandas_ta as ta

def calculate_metrics(df, ticker):
    mark = float(ticker.get('last', 0))
    index = float(ticker.get('info', {}).get('indexPrice', mark))
    basis = (mark - index) / index if index > 0 else 0
    
    df['Vol_SMA'] = ta.sma(df['volume'], length=20)
    df['RVOL'] = df['volume'] / df['Vol_SMA']
    
    df['delta'] = np.where(df['close'] > df['open'], df['volume'], -df['volume'])
    df['CVD'] = df['delta'].cumsum()
    
    score = 2
    reasons = []
    rvol = df['RVOL'].iloc[-1]
    
    if rvol > 5.0: score += 1; reasons.append("Nuclear RVOL")
    elif rvol > 2.0: reasons.append("Valid RVOL")
    else: reasons.append("Low RVOL")

    return df, basis, score, reasons

def check_fakeout(df, min_rvol):
    if df['RVOL'].iloc[-1] < min_rvol: return False, "Fakeout (Low Vol)"
    return True, ""