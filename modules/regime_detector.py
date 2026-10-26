import pandas as pd
from modules.indicators import calc_ema, calc_adx, calc_atr

REGIME_CONFIG = {
    'BULL_TREND':      {'long_bias': 0.7, 'short_bias': 0.3, 'max_trades': 4},
    'BEAR_TREND':      {'long_bias': 0.3, 'short_bias': 0.7, 'max_trades': 4},
    'RANGING':         {'long_bias': 0.5, 'short_bias': 0.5, 'max_trades': 3},
    'HIGH_VOLATILITY': {'long_bias': 0.5, 'short_bias': 0.5, 'max_trades': 1},
}

def detect_market_regime(candles_1h: pd.DataFrame) -> str:
    """
    Classifies the current market phase into:
    BULL_TREND, BEAR_TREND, HIGH_VOLATILITY, or RANGING.
    """
    if len(candles_1h) < 50:
        return 'RANGING'

    closes = candles_1h["close"]

    # Calculate indicators
    ema_50 = calc_ema(closes, 50)
    ema_200 = calc_ema(closes, 200)
    adx = calc_adx(candles_1h, 14)
    atr = calc_atr(candles_1h, 14)

    last_adx = adx.iloc[-1]
    last_ema_50 = ema_50.iloc[-1]
    last_ema_200 = ema_200.iloc[-1]
    
    last_atr = atr.iloc[-1]
    atr_30d_avg = atr.rolling(window=min(len(atr), 720)).mean().iloc[-1] # Average of last 30d (720 1-hour candles)

    # Classification logic
    if last_adx > 25:
        if last_ema_50 > last_ema_200:
            return 'BULL_TREND'
        elif last_ema_50 < last_ema_200:
            return 'BEAR_TREND'

    if last_atr > atr_30d_avg * 2.0:
        return 'HIGH_VOLATILITY'

    return 'RANGING'
