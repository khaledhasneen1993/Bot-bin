import pandas as pd
import numpy as np

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(com=period - 1, adjust=True).mean()
    loss = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=True).mean()
    rs = gain / (loss + 1e-8)
    return 100 - (100 / (1 + rs))

def calc_stoch_rsi(series: pd.Series, period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    rsi = calc_rsi(series, period)
    rsi_min = rsi.rolling(window=period).min()
    rsi_max = rsi.rolling(window=period).max()
    stoch_rsi = 100 * (rsi - rsi_min) / (rsi_max - rsi_min + 1e-8)
    k = stoch_rsi.rolling(window=smooth_k).mean()
    d = k.rolling(window=smooth_d).mean()
    return k, d

def calc_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def calc_bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    return upper, sma, lower

def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    
    up_move = h - h.shift()
    down_move = l.shift() - l
    
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    
    # Wilder's smoothing equivalent using alpha = 1 / period
    tr_smooth = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * (plus_dm_smooth / (tr_smooth + 1e-8))
    minus_di = 100 * (minus_dm_smooth / (tr_smooth + 1e-8))
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-8)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx

def calc_obv(df: pd.DataFrame) -> pd.Series:
    c, v = df["close"], df["volume"]
    direction = np.sign(c.diff())
    direction.iloc[0] = 0.0
    obv = (direction * v).cumsum()
    return obv

def calc_vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_price * df["volume"]
    
    # Reset cumulative sums at midnight UTC using open_time date
    dates = df["open_time"].dt.date
    cum_pv = pv.groupby(dates).cumsum()
    cum_vol = df["volume"].groupby(dates).cumsum()
    return cum_pv / (cum_vol + 1e-8)

