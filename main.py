import asyncio
import os
import sys
import time
from datetime import datetime, timezone
import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
import config

from modules.state import state, state_lock, push_log
from modules.alerting import send_telegram, send_trade_alert, send_daily_summary
from modules.learning_engine import (
    init_db, get_active_weights, update_signal_weights, get_closed_trades, 
    save_trade, get_daily_fees_paid, get_historical_peak_balance, save_snapshot,
    get_open_trade_db, get_daily_trades_count
)
from modules.circuit_breaker import check_circuit_breaker, is_circuit_breaker_active
from modules.regime_detector import detect_market_regime
from modules.signals import calculate_signals
from modules.fee_guardian import passes_fee_gate
from modules.position_sizer import calculate_position_size
from modules.trade_manager import (
    get_client, enforce_leverage_and_margin, reconcile_positions, 
    open_trade_limit, close_position_market, manage_trailing_stop, 
    check_external_close, update_unrealised_pnl_state
)
from modules.compounder import check_milestones
from modules.event_watcher import get_upcoming_events, is_maintenance_impending
from modules.backtester import run_weekly_backtest
from modules.indicators import calc_atr

# Watchdog variables for WebSocket price updates
last_ws_update_time = 0.0
emergency_triggered = False

def fetch_candles(client: Client, interval: str, limit: int = 100, symbol: str = None) -> pd.DataFrame:
    """Fetches candle data from Binance REST API."""
    sym = symbol if symbol else config.SYMBOL
    raw = client.futures_klines(symbol=sym, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","trades","tbav","tqav","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df

def get_balance(client: Client) -> float:
    """Fetches USDT balance from Binance Futures. If Testnet balance is 0, defaults to $10."""
    try:
        info = client.futures_account_balance()
        for asset in info:
            if asset["asset"] == "USDT":
                bal = float(asset["availableBalance"])
                if config.TESTNET and bal == 0.0:
                    return 10.0
                return bal
    except Exception as e:
        push_log(f"Balance fetch error: {e}", "warning")
        if config.TESTNET:
            return 10.0
    return 0.0

def get_market_metrics(client: Client, symbol: str) -> tuple[float, float]:
    """Fetches funding rate and calculates 1h open interest change percent."""
    funding_rate = 0.0
    oi_change = 0.0
    try:
        # Get funding rate
        funding_info = client.futures_funding_rate(symbol=symbol, limit=1)
        if funding_info:
            funding_rate = float(funding_info[0]["fundingRate"])
        
        # Get open interest stats over last hour (12 periods of 5m)
        oi_stats = client.futures_open_interest_statistics(symbol=symbol, period="5m", limit=12)
        if len(oi_stats) >= 2:
            oi_start = float(oi_stats[0]["sumOpenInterest"])
            oi_end = float(oi_stats[-1]["sumOpenInterest"])
            if oi_start > 0:
                oi_change = (oi_end - oi_start) / oi_start
    except Exception as e:
        pass
    return funding_rate, oi_change

async def ws_watchdog_task(client: Client):
    """
    Monitors WebSocket feed status. If feed fails to update for >120s:
    1. Triggers REST-based fallback checks to manage active positions.
    2. If disconnected for > 120s and we have an open position, closes it via market fallback.
    """
    global last_ws_update_time, emergency_triggered
    last_ws_update_time = time.time()
    
    while True:
        await asyncio.sleep(5)
        with state_lock:
            status = state["status"]
            open_trade = state["open_trade"]

        if status != "running":
            continue

        elapsed = time.time() - last_ws_update_time
        
        # Update state status
        with state_lock:
            state["websocket_status"] = "connected" if elapsed < 10 else "reconnecting"

        if elapsed >= 120 and open_trade is not None and not emergency_triggered:
            emergency_triggered = True
            msg = "🚨 WebSocket connection lost for > 120s with an open position! Executing emergency position close via REST API fallback."
            push_log(msg, "error")
            send_telegram(msg)
            
            # Execute market close
            close_position_market(client, "ws_timeout_emergency")

async def test_rest_fallback(client: Client):
    """Fallback price feed simulating WS updates if the WS loop disconnects."""
    global last_ws_update_time
    while True:
        await asyncio.sleep(5)
        with state_lock:
            status = state["status"]
            
        if status != "running":
            continue
            
        try:
            with state_lock:
                trade = state["open_trade"]
            sym = trade["symbol"] if trade else config.SYMBOL
            
            # Poll REST price to simulate feed and keep watchdog alive in case WS fails
            ticker = client.futures_symbol_ticker(symbol=sym)
            price = float(ticker["price"])
            last_ws_update_time = time.time()
            
            # If in trade, update price in state
            if trade:
                update_unrealised_pnl_state(client, sym)
        except Exception:
            pass
def open_paper_trade(signal: str, strategy: str, size_usdt: float, atr: float, signals_state: dict, sl_mult: float = 1.5, tp_mult: float = 3.0, symbol: str = None):
    """Simulates opening a paper position without placing actual exchange orders."""
    sym = symbol if symbol else config.SYMBOL
    client = get_client()
    try:
        ticker = client.futures_symbol_ticker(symbol=sym)
        price = float(ticker["price"])
        
        qty = size_usdt / price
        sl_dist = atr * sl_mult
        tp_dist = atr * tp_mult
        
        sl_price = price - sl_dist if signal == "LONG" else price + sl_dist
        tp_price = price + tp_dist if signal == "LONG" else price - tp_dist
        
        trade = {
            "id": "paper_" + str(int(time.time())),
            "symbol": sym,
            "direction": signal,
            "strategy": strategy,
            "entry_price": price,
            "qty": qty,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "peak_price": price,
            "pnl": 0.0,
            "partially_closed": False,
            "atr_at_entry": atr,
            "market_phase": state.get("market_regime", "RANGING"),
            "timeframe": "5m",
            "mode": "paper",
            "signal_ema":     signals_state.get("ema", 0) if signals_state else 0,
            "signal_rsi":     signals_state.get("rsi", 0) if signals_state else 0,
            "signal_vwap":    signals_state.get("vwap", 0) if signals_state else 0,
            "signal_volume":  signals_state.get("volume", 0) if signals_state else 0,
            "signal_macd":    signals_state.get("macd", 0) if signals_state else 0,
            "signal_funding": signals_state.get("funding", 0) if signals_state else 0,
            "signal_oi":      signals_state.get("oi", 0) if signals_state else 0,
        }
        
        with state_lock:
            state["paper_open_trade"] = trade
            current_bal = state["paper_balance"]
            
        # Persist open paper trade state in SQLite database for recovery
        save_trade(trade)
            
        msg = f"🧪 PAPER OPENED {signal} | {qty:.4f} {sym} @ ${price:.2f} | SL=${sl_price:.2f} TP=${tp_price:.2f}"
        push_log(msg)
        
        # Q51 trade alert call
        est_fee = qty * price * 0.0002 # MAKER entry fee
        send_trade_alert(sym, signal, price, qty * price, sl_price, tp_price, "MAKER", est_fee, current_bal)
    except Exception as e:
        push_log(f"Paper Open Trade Error: {e}", "warning")

def close_paper_position(exit_price: float, reason: str):
    """Simulates closing a paper position, recording profits and virtual commissions."""
    with state_lock:
        trade = state["paper_open_trade"]
    if not trade:
        return
        
    entry = trade["entry_price"]
    direction = trade["direction"]
    qty = trade["qty"]
    
    if direction == "LONG":
        pnl = (exit_price - entry) * qty
    else:
        pnl = (entry - exit_price) * qty
        
    # Virtual commissions: Maker entry + taker exit = 0.06% total
    fee = qty * exit_price * 0.0004 + qty * entry * 0.0002
    net_pnl = pnl - fee
    
    trade["exit_price"] = exit_price
    trade["exit_time"] = datetime.now(timezone.utc).isoformat()
    trade["pnl"] = round(pnl, 4)
    trade["fee_paid"] = round(fee, 4)
    trade["status"] = "closed"
    trade["close_reason"] = reason
    trade["mode"] = "paper"
    
    with state_lock:
        state["paper_balance"] += net_pnl
        state["paper_total_pnl"] += net_pnl
        if net_pnl >= 0:
            state["paper_win_count"] += 1
        else:
            state["paper_loss_count"] += 1
        
        state["paper_closed_trades"].insert(0, trade)
        state["paper_open_trade"] = None
        
    # Save to SQLite
    save_trade(trade)
    
    msg = f"🧪 PAPER {'🟢 WIN' if net_pnl >= 0 else '🔴 LOSS'} CLOSED {direction} | PnL: ${net_pnl:+.4f} | Reason: {reason}"
    push_log(msg)
    send_telegram(f"🧪 <b>Paper Trade Closed</b>\n{msg}")

def manage_paper_position(client: Client):
    """Evaluates trailing stops, profit targets, and stop losses for simulated paper positions."""
    with state_lock:
        trade = state["paper_open_trade"]
    if not trade:
        return
        
    try:
        sym = trade.get("symbol", config.SYMBOL)
        ticker = client.futures_symbol_ticker(symbol=sym)
        price = float(ticker["price"])
        
        entry = trade["entry_price"]
        direction = trade["direction"]
        qty = trade["qty"]
        atr = trade.get("atr_at_entry", 0.0)
        
        # Calculate unrealized PnL
        if direction == "LONG":
            pnl = (price - entry) * qty
            profit_atr = (price - entry) / (atr + 1e-8)
            if price > trade["peak_price"]:
                trade["peak_price"] = price
        else:
            pnl = (entry - price) * qty
            profit_atr = (entry - price) / (atr + 1e-8)
            if price < trade["peak_price"]:
                trade["peak_price"] = price
                
        trade["pnl"] = round(pnl, 4)
        trade["current_price"] = price
        
        with state_lock:
            state["paper_open_trade"] = trade
            
        # 1. Stop Loss Check
        hit_sl = False
        if direction == "LONG" and price <= trade["sl_price"]:
            hit_sl = True
        elif direction == "SHORT" and price >= trade["sl_price"]:
            hit_sl = True
            
        if hit_sl:
            close_paper_position(price, "stop_loss")
            return
            
        # 2. Take Profit Check
        hit_tp = False
        if direction == "LONG" and price >= trade["tp_price"]:
            hit_tp = True
        elif direction == "SHORT" and price <= trade["tp_price"]:
            hit_tp = True
            
        if hit_tp:
            close_paper_position(price, "take_profit")
            return
            
        # 3. Breakeven Trigger (1x ATR)
        if profit_atr >= 1.0 and trade["sl_price"] != entry:
            trade["sl_price"] = entry
            push_log(f"🧪 Paper: Moved SL to entry (${entry:.2f})")
            
        # 4. Trailing Stop Trigger (2x ATR)
        elif profit_atr >= 2.0:
            if direction == "LONG":
                target_sl = trade["peak_price"] - (atr * 1.0)
                if target_sl > trade["sl_price"]:
                    trade["sl_price"] = target_sl
                    push_log(f"🧪 Paper: Trailing SL adjusted to ${target_sl:.2f}")
            else:
                target_sl = trade["peak_price"] + (atr * 1.0)
                if target_sl < trade["sl_price"] or trade["sl_price"] == entry:
                    trade["sl_price"] = target_sl
                    push_log(f"🧪 Paper: Trailing SL adjusted to ${target_sl:.2f}")
                    
        # 5. Partial Close Check (3x ATR)
        elif profit_atr >= 3.0 and not trade.get("partially_closed", False):
            remaining_qty = qty * 0.30
            if remaining_qty * price >= 5.0:
                partial_pnl = (price - entry) * (qty * 0.70) if direction == "LONG" else (entry - price) * (qty * 0.70)
                push_log(f"🧪 Paper: Hit 3x ATR. Partially closed 70% of position. Locked in ${partial_pnl:.4f}")
                
                trade["qty"] = remaining_qty
                trade["partially_closed"] = True
                trade["sl_price"] = price - (atr * 0.5) if direction == "LONG" else price + (atr * 0.5)
                
                with state_lock:
                    state["paper_balance"] += partial_pnl
                    state["paper_open_trade"] = trade
            else:
                close_paper_position(price, "take_profit")
                
    except Exception as e:
        push_log(f"Error in paper position check: {e}", "warning")
async def scan_and_trade_task(client: Client):
    """Main trading logic loop. Runs every config.LOOP_INTERVAL seconds."""
    global emergency_triggered
    push_log("Scan and trade loop started.")
    
    # Track execution triggers
    last_regime_time = 0.0
    last_milestone_check = 0.0
    last_event_check = 0.0
    trade_count_snapshot = 0
    loop_counter = 0
    
    # Q43 & Q52 tracking
    last_heartbeat_time = time.time()
    last_daily_summary_date = None
    last_weekly_backtest_date = None
    
    while True:
        await asyncio.sleep(config.LOOP_INTERVAL)
        
        with state_lock:
            status = state["status"]
            mode = state["trading_mode"]
            current_trade = state["open_trade"] if mode == "live" else state["paper_open_trade"]
        if status != "running" and current_trade is None:
            continue

        try:
            # 1. Update Scan timestamp
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            with state_lock:
                state["last_scan"] = now_str

            # 2. Update and fetch balance based on trading mode
            with state_lock:
                mode = state["trading_mode"]

            if mode == "live":
                bal = get_balance(client)
                with state_lock:
                    state["balance"] = bal
                    if bal > state["peak_balance"]:
                        state["peak_balance"] = bal
                    peak_bal = state["peak_balance"]
                    if peak_bal > 0:
                        state["drawdown_pct"] = max(0.0, (peak_bal - bal) / peak_bal * 100)
                    if config.TARGET_BALANCE > config.STARTING_BALANCE:
                        state["progress_pct"] = min(100.0, (bal - config.STARTING_BALANCE) / (config.TARGET_BALANCE - config.STARTING_BALANCE) * 100)
                    
                    current_bal = state["balance"]
                    start_bal = state["starting_balance"]
            else:
                # Test/Paper trading balance logic
                with state_lock:
                    bal = state["paper_balance"]
                    if bal > state["paper_peak_balance"]:
                        state["paper_peak_balance"] = bal
                    peak_bal = state["paper_peak_balance"]
                    if peak_bal > 0:
                        state["paper_drawdown_pct"] = max(0.0, (peak_bal - bal) / peak_bal * 100)
                    state["paper_progress_pct"] = min(100.0, (bal - config.STARTING_BALANCE) / (config.TARGET_BALANCE - config.STARTING_BALANCE) * 100)
                    
                    # Sync to standard UI state properties so it shows correctly on UI
                    state["balance"] = bal
                    state["peak_balance"] = peak_bal
                    state["drawdown_pct"] = state["paper_drawdown_pct"]
                    state["progress_pct"] = state["paper_progress_pct"]
                    
                    current_bal = state["paper_balance"]
                    start_bal = state["paper_starting_balance"]
            
            # Save account snapshot to database periodically (every 5 minutes / 30 loop iterations)
            loop_counter += 1
            if loop_counter % 30 == 0:
                save_snapshot(current_bal, mode)

            # Run circuit breaker check (Daily, Weekly, and Peak drawdown)
            if check_circuit_breaker(current_bal, peak_bal, start_bal) or is_circuit_breaker_active():
                continue

            # 3. Periodically run Regime Detection (every 1 hour)
            current_time = time.time()
            if current_time - last_regime_time >= 3600 or last_regime_time == 0.0:
                last_regime_time = current_time
                try:
                    candles_1h = fetch_candles(client, "1h", 100)
                    regime = detect_market_regime(candles_1h)
                    with state_lock:
                        state["market_regime"] = regime
                    push_log(f"Market regime updated: {regime}")
                except Exception as re:
                    push_log(f"Failed to update market regime: {re}", "warning")

            # 4. Watch for High-Impact Events (every 6 hours)
            event_risk = False
            if current_time - last_event_check >= 21600 or last_event_check == 0.0:
                last_event_check = current_time
                event_risk = get_upcoming_events()

            # 5. Check milestones and run review logs (every 30 minutes)
            if current_time - last_milestone_check >= 1800 or last_milestone_check == 0.0:
                last_milestone_check = current_time
                check_milestones(current_bal)

            # 6. Reconcile Position Sync / Manage paper trades
            if mode == "live":
                with state_lock:
                    live_trade = state["open_trade"]
                if live_trade:
                    check_external_close(client, live_trade["symbol"])
                    update_unrealised_pnl_state(client, live_trade["symbol"])
                    manage_trailing_stop(client)
            else:
                manage_paper_position(client)

            # ── 24h Telegram Heartbeat (Q43) ──
            if time.time() - last_heartbeat_time >= 86400:
                last_heartbeat_time = time.time()
                trades_today = get_daily_trades_count(mode)
                send_telegram(f"ℹ️ <b>ALIVE: Bot running 24h</b> | Balance: ${current_bal:.2f} | Trades today: {trades_today}")

            # ── Daily summary at Midnight UTC (Q52) ──
            now_dt = datetime.now(timezone.utc)
            if now_dt.hour == 0 and now_dt.minute == 0 and last_daily_summary_date != now_dt.date():
                last_daily_summary_date = now_dt.date()
                send_daily_summary(current_bal, mode)

            # ── Weekly Sunday Backtester at Midnight UTC (Q58) ──
            if now_dt.weekday() == 6 and now_dt.hour == 0 and now_dt.minute == 0 and last_weekly_backtest_date != now_dt.date():
                last_weekly_backtest_date = now_dt.date()
                run_weekly_backtest()

            if status != "running":
                continue

            with state_lock:
                current_trade = state["open_trade"] if mode == "live" else state["paper_open_trade"]
                weights = get_active_weights()

            # 7. Check entries if no position is active
            if current_trade is None:
                active_signals_copy = {}
                daily_fees = get_daily_fees_paid(mode)
                threshold = config.CONFLUENCE_THRESHOLD
                if current_bal > 0 and (daily_fees / current_bal) >= config.FEE_THROTTLE_LIMIT:
                    threshold = config.CONFLUENCE_THRESHOLD + 1.0
                    push_log(
                        f"FEE THROTTLE ACTIVE: Daily fees (${daily_fees:.4f}) exceed "
                        f"{config.FEE_THROTTLE_LIMIT*100}% of balance (${current_bal:.2f}). "
                        f"Stricter threshold of {threshold} enforced.",
                        "warning"
                    )

                trade_opened = False
                for symbol in config.ACTIVE_PAIRS:
                    if trade_opened:
                        break
                    try:
                        # Fetch multi-timeframe candles
                        candles_dict = {}
                        for tf in ["1m", "5m", "15m", "1h"]:
                            candles_dict[tf] = fetch_candles(client, tf, 250, symbol=symbol)
                        
                        # Fetch funding rate & OI
                        funding, oi_chg = get_market_metrics(client, symbol)
                        
                        # Evaluate signals
                        signals_res = calculate_signals(symbol, candles_dict, funding, oi_chg, weights)
                        
                        long_score = signals_res["long_score"]
                        short_score = signals_res["short_score"]
                        macro = signals_res["macro_trend"]
                        
                        signal = None
                        strategy_name = "Confluence Strategy"
                        status_str = "Scanning..."
                        
                        # Enforce impending maintenance window check
                        if is_maintenance_impending():
                            status_str = "Skipped: Impending maintenance"
                            push_log(f"⏳ MAINTENANCE IMPENDING: Trading suspended for {symbol}.", "warning")
                        else:
                            # Check 2-strategy confluence rule
                            confluence_dir = signals_res.get("confluence_direction")
                            agreeing_strats = signals_res.get("confluence_strategies", [])

                            if confluence_dir == "LONG" and long_score >= threshold:
                                signal = "LONG"
                                strategy_name = " + ".join(agreeing_strats)
                            elif confluence_dir == "SHORT" and short_score >= threshold:
                                signal = "SHORT"
                                strategy_name = " + ".join(agreeing_strats)
                            else:
                                score = long_score if long_score > short_score else short_score
                                status_str = f"Skipped: Insufficient confluence (Score {score}/{threshold})"
                                if confluence_dir is None and (long_score > 0 or short_score > 0):
                                    if macro == "neutral":
                                        status_str = "Skipped: Neutral macro trend"
                                    elif (long_score > 0 and macro == "bearish") or (short_score > 0 and macro == "bullish"):
                                        status_str = f"Skipped: Trend alignment conflict ({macro} macro)"

                        # Save diagnostic info
                        active_signals_copy[symbol] = {
                            "long_score": long_score,
                            "short_score": short_score,
                            "macro_trend": macro,
                            "confluence_direction": confluence_dir,
                            "confluence_strategies": agreeing_strats,
                            "status": status_str if not signal else f"Signal triggered: {signal}"
                        }

                        if signal:
                            # Retrieve ATR for sizing
                            atr = calc_atr(candles_dict["5m"], 14).iloc[-1]
                            entry_price = candles_dict["5m"]["close"].iloc[-1]
                            
                            # Risk sizer calculation based on trading mode
                            closed_c = state["closed_trades"] if mode == "live" else state["paper_closed_trades"]
                            win_c = state["win_count"] if mode == "live" else state["paper_win_count"]
                            loss_c = state["loss_count"] if mode == "live" else state["paper_loss_count"]
                            win_rate = 0.58 if len(closed_c) == 0 else (win_c / (win_c + loss_c + 1e-8))
                            
                            size_usdt = calculate_position_size(
                                current_bal, atr, entry_price, 
                                win_rate=win_rate
                            )
                            
                            # Adjust if event risk is active (reduce size 50%, double stop distance)
                            if event_risk:
                                size_usdt *= 0.5
                                atr *= 2.0
                                push_log(f"⚠️ Size halved & stops widened due to upcoming high-volatility event for {symbol}.")

                            # Calculate target multipliers based on agreeing strategies
                            sl_mult = 1.0 if "Strategy 2 (Mean Reversion)" in agreeing_strats else 1.5
                            tp_mult = 1.5 if "Strategy 2 (Mean Reversion)" in agreeing_strats else (4.0 if "Strategy 3 (Breakout)" in agreeing_strats else 3.0)

                            if size_usdt >= 5.0:
                                # Evaluate ATR fee gate
                                if passes_fee_gate(atr, entry_price, size_usdt):
                                    if mode == "live":
                                        push_log(f"📡 Signal Triggered for {symbol}: {signal} (Long: {long_score}, Short: {short_score})")
                                        open_trade_limit(client, signal, strategy_name, size_usdt, atr, signals_res["states"], sl_mult=sl_mult, tp_mult=tp_mult, symbol=symbol)
                                        emergency_triggered = False # reset emergency latch on new trade
                                    else:
                                        # Paper trading open position
                                        push_log(f"📡 Signal Triggered for {symbol}: {signal} (Long: {long_score}, Short: {short_score})")
                                        open_paper_trade(signal, strategy_name, size_usdt, atr, signals_res["states"], sl_mult=sl_mult, tp_mult=tp_mult, symbol=symbol)
                                    
                                    trade_opened = True
                                    active_signals_copy[symbol]["status"] = f"Position Opened: {signal}"
                                else:
                                    status_str = "Skipped: Fee Gate Blocked"
                                    active_signals_copy[symbol]["status"] = status_str
                                    push_log(f"📡 Signal for {symbol} skipped because it did not pass the fee gate.", "info")
                            else:
                                status_str = f"Skipped: Position size too small (${size_usdt:.2f} < $5.0)"
                                active_signals_copy[symbol]["status"] = status_str
                                push_log(f"📡 Signal for {symbol} skipped because size ${size_usdt:.2f} is under minimum $5.0.", "info")
                    except Exception as sym_err:
                        push_log(f"Error scanning {symbol}: {sym_err}", "warning")
                        active_signals_copy[symbol] = {
                            "long_score": 0.0, "short_score": 0.0, "macro_trend": "neutral",
                            "confluence_direction": None, "confluence_strategies": [],
                            "status": f"Error: {sym_err}"
                        }
                
                with state_lock:
                    state["active_signals"] = active_signals_copy
            else:
                active_signals_copy = {}
                with state_lock:
                    open_symbol = current_trade.get("symbol", config.SYMBOL)
                for symbol in config.ACTIVE_PAIRS:
                    active_signals_copy[symbol] = {
                        "long_score": 0.0,
                        "short_score": 0.0,
                        "macro_trend": "neutral",
                        "confluence_direction": None,
                        "confluence_strategies": [],
                        "status": f"Monitoring position on {open_symbol}" if symbol == open_symbol else f"Scans paused (Position active on {open_symbol})"
                    }
                with state_lock:
                    state["active_signals"] = active_signals_copy
            
            # 8. Learning weight check (every 20 trades)
            with state_lock:
                total_trades = len(state["closed_trades"]) if mode == "live" else len(state["paper_closed_trades"])
            if total_trades > 0 and total_trades != trade_count_snapshot and total_trades % 20 == 0:
                trade_count_snapshot = total_trades
                update_signal_weights()

        except Exception as e:
            push_log(f"Scanning loop error: {e}", "error")
            err_msg = str(e).lower()
            # Q38: Halt trading immediately if invalid API key or insufficient permissions
            if any(x in err_msg for x in ["api-key", "invalid api-key", "-2014", "-2015", "signature", "auth", "permission", "unauthorized"]):
                with state_lock:
                    state["status"] = "error"
                    state["errors"].append(str(e))
                msg = f"🚨 CRITICAL API AUTH ERROR: {e}. Trading loop HALTED immediately. Review keys."
                push_log(msg, "error")
                send_telegram(msg)
                break

async def start_bot_async(mode: str = "test"):
    """Initializes client configurations, runs startup reconciliation, and schedules loop tasks."""
    try:
        init_db()
        client = get_client()
        
        if mode == "live":
            # Enforce isolated margin type and leverage 3x (GAP 3) for all active pairs
            for symbol in config.ACTIVE_PAIRS:
                try:
                    enforce_leverage_and_margin(client, symbol)
                except Exception as e:
                    push_log(f"Failed to set margin type/leverage for {symbol}: {e}", "warning")
            
            # Reconcile position state for active pairs (GAP 4)
            for symbol in config.ACTIVE_PAIRS:
                reconcile_positions(client, symbol)
                with state_lock:
                    if state["open_trade"] is not None:
                        break

            # Load closed trades cache from DB to populate UI
            closed = get_closed_trades(50, "live")
            # Fetch initial balance
            bal = get_balance(client)
            with state_lock:
                state["balance"] = bal
                state["closed_trades"] = closed
                # Recalculate stats
                wins = sum(1 for t in closed if t["pnl"] >= 0)
                losses = sum(1 for t in closed if t["pnl"] < 0)
                state["win_count"] = wins
                state["loss_count"] = losses
                state["total_pnl"] = sum(t["pnl"] for t in closed)
                state["peak_balance"] = get_historical_peak_balance("live", bal)
                
                state["status"] = "running"
        else:
            # Paper trading / test mode
            closed = get_closed_trades(50, "paper")
            db_trade = get_open_trade_db("paper")
            with state_lock:
                state["paper_closed_trades"] = closed
                # Recalculate stats
                wins = sum(1 for t in closed if t["pnl"] >= 0)
                losses = sum(1 for t in closed if t["pnl"] < 0)
                state["paper_win_count"] = wins
                state["paper_loss_count"] = losses
                state["paper_total_pnl"] = sum(t["pnl"] for t in closed)
                state["paper_balance"] = 10.0 + state["paper_total_pnl"]
                state["paper_starting_balance"] = 10.0
                state["paper_peak_balance"] = get_historical_peak_balance("paper", state["paper_balance"])
                
                state["paper_open_trade"] = db_trade
                
                # Setup base balance fields
                state["balance"] = state["paper_balance"]
                state["closed_trades"] = closed
                state["open_trade"] = db_trade
                state["status"] = "running"
                
            if db_trade:
                push_log(f"🔌 RECONCILIATION: Recovered open paper trade from database: {db_trade['direction']} @ ${db_trade['entry_price']:.2f}")

        push_log(f"🚀 Trading engine started asynchronously in {mode.upper()} mode.")
        send_telegram(f"🚀 <b>Trading Bot Initialized</b>\nStatus: RUNNING ({mode.upper()})\nLeverage: 3x Isolated\nReady to scan signals.")

        # Gather tasks
        await asyncio.gather(
            ws_watchdog_task(client),
            test_rest_fallback(client),
            scan_and_trade_task(client)
        )

    except Exception as e:
        push_log(f"Startup initialization failed: {e}", "error")
        with state_lock:
            state["status"] = "error"
            state["errors"].append(str(e))

def start_bot(mode: str = "test"):
    """Entry point called by dashboard to run background execution."""
    with state_lock:
        state["status"] = "running"
        state["trading_mode"] = mode
    
    # Run the async loop inside a separate daemon thread
    import threading
    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_bot_async(mode))
    
    t = threading.Thread(target=run_loop, daemon=True)
    t.start()

def stop_bot():
    with state_lock:
        state["status"] = "stopped"
    push_log("⏹ Stop requested. Position tracking halted.")
    send_telegram("⏹ <b>Trading Bot Stopped</b>\nScan activity suspended.")

def manual_close():
    try:
        client = get_client()
        with state_lock:
            mode = state["trading_mode"]
            
        if mode == "live":
            close_position_market(client, "manual")
        else:
            ticker = client.futures_symbol_ticker(symbol=config.SYMBOL)
            price = float(ticker["price"])
            close_paper_position(price, "manual")
    except Exception as e:
        push_log(f"Manual Close Error: {e}", "error")
