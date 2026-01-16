import numpy as np
import pandas_ta as ta

def calculate_z_score(series, window=20):
    """
    Calculates the Rolling Z-Score.
    Formula: (Value - Mean) / StdDev
    """
    mean = series.rolling(window=window).mean()
    std = series.rolling(window=window).std()
    z_score = (series - mean) / std
    return z_score

def calculate_zeta_field(df, basis):
    """
    🧮 Citadel Quant ζ-Field Math Implementation
    Formula: Ξ_t = Norm[0,100] [ V + F + C + B + S + A + H + T ]
    """
    try:
        # 1. V(r): Volatility (Normalized ATR)
        # Low volatility = 0, High volatility = 1 (inverted if we want stability, but usually volatility = opportunity)
        natr = ta.natr(df['high'], df['low'], df['close'], length=14)
        v_term = expit(natr.iloc[-1]) # Sigmoid to 0-1

        # 2. F(x): Flow (Chaikin Money Flow)
        cmf = ta.cmf(df['high'], df['low'], df['close'], df['volume'], length=20)
        f_term = (cmf.iloc[-1] + 1) / 2 # Normalize -1..1 to 0..1

        # 3. C(x,y): Cyclicality (CCI)
        cci = ta.cci(df['high'], df['low'], df['close'], length=20)
        c_term = expit(cci.iloc[-1] / 100) # Normalize typical CCI range

        # 4. B(r): Basis (Spot Premium)
        # Near 0 is stable (0.5), High negative/positive is imbalance
        b_term = 1.0 - min(abs(basis) * 100, 1.0) 

        # 5. S(x,y): Structure (RSI)
        rsi = ta.rsi(df['close'], length=14)
        s_term = rsi.iloc[-1] / 100.0

        # 6. A(x,y): Acceleration (ROC)
        roc = ta.roc(df['close'], length=9)
        a_term = expit(roc.iloc[-1])

        # 7. H(r): Heat (RVOL)
        # Cap RVOL at 5.0 for normalization
        rvol = df['volume'].iloc[-1] / df['volume'].rolling(20).mean().iloc[-1]
        h_term = min(rvol / 5.0, 1.0)

        # 8. T(x,y): Time/Trend (ADX)
        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        t_term = adx['ADX_14'].iloc[-1] / 100.0

        # === SUMMATION & NORMALIZATION ===
        # Ξ = Sum of all fields / Number of fields * 100
        zeta_raw = (v_term + f_term + c_term + b_term + s_term + a_term + h_term + t_term)
        zeta_score = (zeta_raw / 8.0) * 100.0
        
        # Scoring Logic based on Field Strength
        score_add = 0
        reason = ""
        
        # Extreme Field Strength (>75 or <25 usually indicates strong signal)
        if zeta_score > 70:
            score_add += 1
            reason = f"ζ-Field High ({zeta_score:.1f})"
        elif zeta_score < 30:
            score_add += 1
            reason = f"ζ-Field Low ({zeta_score:.1f})" # Oversold/Value area

        return zeta_score, score_add, reason

    except Exception as e:
        # print(f"Zeta Error: {e}")
        return 50.0, 0, ""

def calculate_metrics(df, ticker):
    # 1. Basis
    mark = float(ticker.get('last', 0))
    index = float(ticker.get('info', {}).get('indexPrice', mark))
    basis = (mark - index) / index if index > 0 else 0
    
    # 2. RVOL (Standard)
    df['Vol_SMA'] = ta.sma(df['volume'], length=20)
    df['RVOL'] = df['volume'] / df['Vol_SMA']
    
    # 3. Z-Score (Standard)
    df['Vol_Z'] = calculate_z_score(df['volume'], window=20)
    z_score = df['Vol_Z'].iloc[-1]
    
    # 4. CVD
    df['delta'] = np.where(df['close'] > df['open'], df['volume'], -df['volume'])
    df['CVD'] = df['delta'].cumsum()
    
    # 5. NEW: Citadel ζ-Field Math
    zeta_score, zeta_bonus, zeta_reason = calculate_zeta_field(df, basis)
    
    # === SCORING ===
    score = 2
    reasons = []
    
    # RVOL Logic
    rvol = df['RVOL'].iloc[-1]
    if rvol > 5.0:
        score += 1
        reasons.append("Nuclear RVOL")
    elif rvol > 2.0:
        reasons.append("Valid RVOL")
    
    # Z-Score Logic
    if z_score > 3.0:
        score += 2
        reasons.append(f"High Z-Score ({z_score:.1f})")
        
    # Zeta Field Logic
    if zeta_bonus > 0:
        score += zeta_bonus
        reasons.append(zeta_reason)

    return df, basis, z_score, zeta_score, score, reasons

def check_fakeout(df, min_rvol):
    if df['RVOL'].iloc[-1] < min_rvol: return False, "Fakeout (Low Vol)"
    return True, ""