import numpy as np
from scipy.signal import argrelextrema
from scipy.stats import linregress
from modules.config_loader import CONFIG

def get_slope(values):
    try:
        x = np.arange(len(values))
        slope, _, _, _, _ = linregress(x, values)
        return slope
    except: return 0.0

def check_alignment(values):
    """
    Checks if values are aligned horizontally within the configured tolerance.
    """
    if len(values) < 2: return False
    
    # Load tolerance from config, default to 1.5% if missing
    tol = CONFIG['patterns'].get('tolerance', 0.015)
    
    avg = np.mean(values)
    return all(abs(v - avg) / avg < tol for v in values)

def find_pattern(df):
    if len(df) < 50: return None
    df_idx = df.reset_index(drop=True)
    
    n = 3
    df_idx['min_local'] = df_idx.iloc[argrelextrema(df_idx.low.values, np.less_equal, order=n)[0]]['low']
    df_idx['max_local'] = df_idx.iloc[argrelextrema(df_idx.high.values, np.greater_equal, order=n)[0]]['high']
    
    peaks = df_idx[df_idx['max_local'].notnull()]['max_local'].values
    valleys = df_idx[df_idx['min_local'].notnull()]['min_local'].values
    
    if len(peaks) < 3 or len(valleys) < 3: return None
    
    enabled = CONFIG['patterns']
    slope_highs = get_slope(peaks[-4:])
    slope_lows = get_slope(valleys[-4:])
    
    # 1. Ascending Triangle
    if enabled.get('ascending_triangle'):
        if abs(slope_highs) < 0.0005 and slope_lows > 0.0002:
            return 'ascending_triangle'

    # 2. Descending Triangle
    if enabled.get('descending_triangle'):
        if abs(slope_lows) < 0.0005 and slope_highs < -0.0002:
            return 'descending_triangle'

    # 3. Double Bottom
    # Removed hardcoded 0.01, now uses check_alignment() which reads config
    if enabled.get('double_bottom') and check_alignment(valleys[-2:]): 
        return 'double_bottom'

    # 4. Double Top
    if enabled.get('double_top') and check_alignment(peaks[-2:]): 
        return 'double_top'

    # 5. Bull Flag
    if enabled.get('bull_flag'):
        if -0.002 < slope_highs < -0.0002 and -0.002 < slope_lows < -0.0002:
            return 'bull_flag'

    # 6. Bullish Rectangle
    if enabled.get('bullish_rectangle'):
        if abs(slope_highs) < 0.0005 and abs(slope_lows) < 0.0005:
            return 'bullish_rectangle'
    
    return None