import pandas as pd
import numpy as np
from datetime import datetime, timezone
import config
from modules.state import push_log, state
from modules.indicators import (
    calc_ema, calc_rsi, calc_stoch_rsi, calc_macd, 
    calc_atr, calc_bollinger_bands, calc_adx, calc_obv, calc_vwap
)

def detect_rsi_divergence(symbol_or_df, timeframe_or_period=14) -> dict:
    """
    RSI Divergence detection helper.
    Supports dual signatures:
    - detect_rsi_divergence(df, period)
    - detect_rsi_divergence('BTCUSDT', '15m') [for Q8 validation]
    """
    if isinstance(symbol_or_df, str):
        # API-fetch mode
        from modules.trade_manager import get_client
        from main import fetch_candles
        client = get_client()
        df = fetch_candles(client, timeframe_or_period, 100)
        period = 14
    else:
        df = symbol_or_df
        period = timeframe_or_period

    if len(df) < 30:
        return {"divergence": None, "details": {}}

    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    rsi = calc_rsi(closes, period)

    # Core divergence algorithm searching for pivot points
    pivots_low = []
    pivots_high = []
    
    for i in range(5, len(df) - 2):
        # local low pivot
        if lows.iloc[i] == min(lows.iloc[i-3:i+4]):
            pivots_low.append((i, lows.iloc[i], rsi.iloc[i]))
        # local high pivot
        if highs.iloc[i] == max(highs.iloc[i-3:i+4]):
            pivots_high.append((i, highs.iloc[i], rsi.iloc[i]))

    bullish_div = False
    bearish_div = False
    details = {}

    if len(pivots_low) >= 2:
        p1 = pivots_low[-2]  # older pivot
        p2 = pivots_low[-1]  # newer pivot
        # Bullish divergence: price is lower low, but RSI is higher low
        if p2[1] < p1[1] and p2[2] > p1[2]:
            bullish_div = True
            details = {
                "type": "bullish",
                "older_pivot": {"index": p1[0], "price": p1[1], "rsi": p1[2]},
                "newer_pivot": {"index": p2[0], "price": p2[1], "rsi": p2[2]}
            }

    if len(pivots_high) >= 2:
        p1 = pivots_high[-2]  # older pivot
        p2 = pivots_high[-1]  # newer pivot
        # Bearish divergence: price is higher high, but RSI is lower high
        if p2[1] > p1[1] and p2[2] < p1[2]:
            bearish_div = True
            details = {
                "type": "bearish",
                "older_pivot": {"index": p1[0], "price": p1[1], "rsi": p1[2]},
                "newer_pivot": {"index": p2[0], "price": p2[1], "rsi": p2[2]}
            }

    # Fallback to general min/max if no discrete pivots found
    if not bullish_div and not bearish_div:
        c_15 = closes.iloc[-15:]
        r_15 = rsi.iloc[-15:]
        idx_p_min = c_15.idxmin()
        idx_r_min = r_15.idxmin()
        idx_p_max = c_15.idxmax()
        idx_r_max = r_15.idxmax()

        if idx_p_min > idx_r_min and closes.loc[idx_p_min] < closes.loc[idx_r_min] and rsi.loc[idx_p_min] > rsi.loc[idx_r_min]:
            bullish_div = True
            details = {
                "type": "bullish",
                "older_pivot": {"price": closes.loc[idx_r_min], "rsi": rsi.loc[idx_r_min]},
                "newer_pivot": {"price": closes.loc[idx_p_min], "rsi": rsi.loc[idx_p_min]}
            }
        elif idx_p_max > idx_r_max and closes.loc[idx_p_max] > closes.loc[idx_r_max] and rsi.loc[idx_p_max] < rsi.loc[idx_r_max]:
            bearish_div = True
            details = {
                "type": "bearish",
                "older_pivot": {"price": closes.loc[idx_r_max], "rsi": rsi.loc[idx_r_max]},
                "newer_pivot": {"price": closes.loc[idx_p_max], "rsi": rsi.loc[idx_p_max]}
            }

    return {
        "divergence": "bullish" if bullish_div else "bearish" if bearish_div else None,
        "details": details
    }

def calculate_signals(pair: str, candles_dict: dict, funding_rate: float, oi_change_pct: float, weights: dict) -> dict:
    """
    Computes indicators across multiple timeframes.
    Enforces the 2-Strategy Confluence Rule.
    """
    # Standard signal trace mapping
    sig_states = {
        "ema": 0,
        "rsi": 0,
        "vwap": 0,
        "volume": 0,
        "macd": 0,
        "funding": 0,
        "oi": 0
    }

    # Verify timeframes
    for tf in ["1m", "5m", "15m", "1h"]:
        if tf not in candles_dict or len(candles_dict[tf]) < 30:
            return {
                "long_score": 0.0, "short_score": 0.0, "states": sig_states, "macro_trend": "neutral",
                "confluence_direction": None, "confluence_strategies": []
            }

    df_1m = candles_dict["1m"]
    df_5m = candles_dict["5m"]
    df_15m = candles_dict["15m"]
    df_1h = candles_dict["1h"]

    # 1. 1h Macro Trend calculation
    ema_50_1h = calc_ema(df_1h["close"], 50)
    ema_200_1h = calc_ema(df_1h["close"], 200)
    
    if ema_50_1h.iloc[-1] > ema_200_1h.iloc[-1]:
        macro_trend = "bullish"
    elif ema_50_1h.iloc[-1] < ema_200_1h.iloc[-1]:
        macro_trend = "bearish"
    else:
        macro_trend = "neutral"

    # Get market regime
    regime = state.get("market_regime", "RANGING")

    # ── Strategy 1: Trend Following ──
    # Crossover + MACD + ADX > 25. Trades with trend. Disabled in RANGING.
    strat1 = 0
    ema9_15m = calc_ema(df_15m["close"], 9)
    ema21_15m = calc_ema(df_15m["close"], 21)
    _, _, macd_hist = calc_macd(df_15m["close"])
    adx_15m = calc_adx(df_15m, 14)
    
    if regime != "RANGING" and adx_15m.iloc[-1] > 25:
        ema_cross_up = ema9_15m.iloc[-1] > ema21_15m.iloc[-1]
        macd_up = macd_hist.iloc[-1] > macd_hist.iloc[-2]
        
        if ema_cross_up and macd_up and macro_trend == "bullish":
            strat1 = 1
            sig_states["ema"] = 1
            sig_states["macd"] = 1
        elif not ema_cross_up and not macd_up and macro_trend == "bearish":
            strat1 = -1
            sig_states["ema"] = -1
            sig_states["macd"] = -1

    # ── Strategy 2: Mean Reversion ──
    # RSI divergence + BB + VWAP rejection + Stoch RSI entry. Disabled in HIGH_VOLATILITY.
    strat2 = 0
    upper_bb, _, lower_bb = calc_bollinger_bands(df_15m["close"], 20, 2)
    vwap_15m = calc_vwap(df_15m)
    stoch_k_1m, stoch_d_1m = calc_stoch_rsi(df_1m["close"], 14)
    
    if regime != "HIGH_VOLATILITY":
        div_res = detect_rsi_divergence(df_15m, 14)
        div_type = div_res["divergence"]
        
        close_15m = df_15m["close"].iloc[-1]
        
        if div_type == "bullish":
            sig_states["rsi"] = 1
            # price is close to lower band, below VWAP, and Stoch RSI shows bullish cross on 1m
            if close_15m <= lower_bb.iloc[-1] * 1.01 and close_15m < vwap_15m.iloc[-1] and stoch_k_1m.iloc[-1] > stoch_d_1m.iloc[-1]:
                strat2 = 1
                sig_states["vwap"] = 1
        elif div_type == "bearish":
            sig_states["rsi"] = -1
            # price is close to upper band, above VWAP, and Stoch RSI shows bearish cross on 1m
            if close_15m >= upper_bb.iloc[-1] * 0.99 and close_15m > vwap_15m.iloc[-1] and stoch_k_1m.iloc[-1] < stoch_d_1m.iloc[-1]:
                strat2 = -1
                sig_states["vwap"] = -1

    # ── Strategy 3: Volume Breakout ──
    # Vol spike > 2x + Break S/R + OBV.
    strat3 = 0
    vol_avg = df_15m["volume"].iloc[-21:-1].mean()
    vol_spike = df_15m["volume"].iloc[-1] > (vol_avg * 2.0)
    
    recent_high = df_15m["high"].iloc[-21:-1].max()
    recent_low = df_15m["low"].iloc[-21:-1].min()
    
    obv_15m = calc_obv(df_15m)
    atr_15m = calc_atr(df_15m, 14)
    
    if vol_spike and atr_15m.iloc[-1] > atr_15m.iloc[-2]:
        sig_states["volume"] = 1 if obv_15m.iloc[-1] > obv_15m.iloc[-2] else -1
        
        if df_15m["close"].iloc[-1] > recent_high and obv_15m.iloc[-1] > obv_15m.iloc[-2]:
            strat3 = 1
        elif df_15m["close"].iloc[-1] < recent_low and obv_15m.iloc[-1] < obv_15m.iloc[-2]:
            strat3 = -1

    # ── Strategy 4: Funding Rate Fade ──
    # Funding extreme + Open Interest confirm. BTC/ETH perpetual perpetuals.
    strat4 = 0
    if funding_rate > 0.0015:  # Over-leveraged longs
        sig_states["funding"] = -1
        if oi_change_pct > 0:  # confirms short build
            strat4 = -1
            sig_states["oi"] = -1
    elif funding_rate < -0.0015:  # Over-leveraged shorts
        sig_states["funding"] = 1
        if oi_change_pct > 0:  # confirms long build
            strat4 = 1
            sig_states["oi"] = 1

    # ── Traditional Confluence Score (for threshold checks) ──
    long_score = 0.0
    short_score = 0.0

    for name, state_val in sig_states.items():
        weight = weights.get(name, 1.0)
        if state_val == 1:
            long_score += weight
        elif state_val == -1:
            short_score += weight

    # ── 2-Strategy Confluence Rule ──
    # A trade is only entered when at least 2 strategies agree on the same direction
    long_strategies = []
    short_strategies = []

    if strat1 == 1: long_strategies.append("Strategy 1 (Trend)")
    elif strat1 == -1: short_strategies.append("Strategy 1 (Trend)")

    if strat2 == 1: long_strategies.append("Strategy 2 (Mean Reversion)")
    elif strat2 == -1: short_strategies.append("Strategy 2 (Mean Reversion)")

    if strat3 == 1: long_strategies.append("Strategy 3 (Breakout)")
    elif strat3 == -1: short_strategies.append("Strategy 3 (Breakout)")

    if strat4 == 1: long_strategies.append("Strategy 4 (Funding Fade)")
    elif strat4 == -1: short_strategies.append("Strategy 4 (Funding Fade)")

    confluence_direction = None
    confluence_strategies = []

    if len(long_strategies) >= 2:
        confluence_direction = "LONG"
        confluence_strategies = long_strategies
    elif len(short_strategies) >= 2:
        confluence_direction = "SHORT"
        confluence_strategies = short_strategies

    # Filter by 1h Macro Trend (cannot oppose direction)
    if confluence_direction == "LONG" and macro_trend == "bearish":
        confluence_direction = None
        confluence_strategies = []
    elif confluence_direction == "SHORT" and macro_trend == "bullish":
        confluence_direction = None
        confluence_strategies = []

    # Enforce Funding Rate Crowding limits (Gap overrides)
    if funding_rate > 0.0015:
        # Extreme long crowding: suspend all LONG signals
        if confluence_direction == "LONG":
            confluence_direction = None
            confluence_strategies = []
        long_score = 0.0
    elif funding_rate < -0.0015:
        # Extreme short crowding: suspend all SHORT signals
        if confluence_direction == "SHORT":
            confluence_direction = None
            confluence_strategies = []
        short_score = 0.0

    return {
        "long_score": round(long_score, 2),
        "short_score": round(short_score, 2),
        "states": sig_states,
        "macro_trend": macro_trend,
        "confluence_direction": confluence_direction,
        "confluence_strategies": confluence_strategies
    }

def calculate_all_signals(symbol: str) -> dict:
    """Fetches public price and orderbook details automatically to return indicator state for Q6."""
    from modules.trade_manager import get_client
    from modules.learning_engine import get_active_weights
    from main import fetch_candles, get_market_metrics

    client = get_client()
    candles_dict = {}
    for tf in ["1m", "5m", "15m", "1h"]:
        candles_dict[tf] = fetch_candles(client, tf, 250)

    funding, oi_chg = get_market_metrics(client, symbol)
    weights = get_active_weights()
    return calculate_signals(symbol, candles_dict, funding, oi_chg, weights)
