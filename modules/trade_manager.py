import time
import math
from datetime import datetime, timezone
from binance.client import Client
from binance.exceptions import BinanceAPIException
import config
from modules.state import state, state_lock, push_log
from modules.alerting import send_telegram
from modules.learning_engine import save_trade

# Keep a cache of exchange info for decimal rounding
symbol_rules = {}

def execute_client_call(func, *args, **kwargs):
    """Executes a Binance Client API call, handling rate limit HTTP 429 exceptions gracefully."""
    for attempt in range(3):
        try:
            return func(*args, **kwargs)
        except BinanceAPIException as e:
            if e.status_code == 429 or e.code == -1003 or "429" in str(e) or "rate limit" in str(e).lower():
                push_log(f"⚠️ RATE LIMIT (429) DETECTED: {e.message}. Sleeping 60s before retry attempt {attempt+1}...", "warning")
                time.sleep(60)
            else:
                raise e
    return func(*args, **kwargs)


def get_client() -> Client:
    if config.TESTNET:
        client = Client(config.API_KEY, config.API_SECRET, testnet=True)
        client.FUTURES_URL = "https://testnet.binancefuture.com"
    else:
        client = Client(config.API_KEY, config.API_SECRET)
    return client

def init_exchange_info(client: Client, symbol: str):
    """Fetches exchange rules (step size, tick size) for rounding orders."""
    global symbol_rules
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                step_size = 0.001
                tick_size = 0.01
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                    elif f["filterType"] == "PRICE_FILTER":
                        tick_size = float(f["tickSize"])
                symbol_rules[symbol] = {
                    "step_size": step_size,
                    "tick_size": tick_size
                }
                push_log(f"Cached constraints for {symbol}: Step={step_size}, Tick={tick_size}")
                return
    except Exception as e:
        push_log(f"Exchange info initialization error: {e}", "warning")
        # standard fallback
        symbol_rules[symbol] = {"step_size": 0.001, "tick_size": 0.01}

def round_qty(symbol: str, qty: float) -> float:
    step = symbol_rules.get(symbol, {}).get("step_size", 0.001)
    precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
    val = round(round(qty / step) * step, precision)
    return val

def round_price(symbol: str, price: float) -> float:
    tick = symbol_rules.get(symbol, {}).get("tick_size", 0.01)
    precision = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
    val = round(round(price / tick) * tick, precision)
    return val

def enforce_leverage_and_margin(client: Client, symbol: str):
    """Explicitly configures leverage to 3x and Isolated Margin mode on startup/reconnects."""
    try:
        client.futures_change_leverage(symbol=symbol, leverage=3)
        push_log(f"Leverage configured explicitly to 3x Isolated for {symbol}.")
    except Exception as e:
        push_log(f"CRITICAL: Failed to enforce 3x leverage for {symbol}: {e}. Halting.", "error")
        raise e

    try:
        client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        push_log(f"Margin mode configured explicitly to ISOLATED for {symbol}.")
    except Exception as e:
        if "No need to change" not in str(e):
            push_log(f"Isolated margin selection warning: {e}", "warning")

def reconcile_positions(client: Client, symbol: str):
    """Reconciles internal trade state with active Binance positions to avoid doubling trades."""
    try:
        pos_info = client.futures_position_information(symbol=symbol)
        for pos in pos_info:
            if pos["symbol"] == symbol:
                amt = float(pos["positionAmt"])
                if amt != 0:
                    direction = "LONG" if amt > 0 else "SHORT"
                    qty = abs(amt)
                    entry_price = float(pos["entryPrice"])
                    
                    push_log(f"🔌 RECONCILIATION: Found active position on Binance: {direction} {qty} {symbol} @ ${entry_price:.2f}")

                    # Attempt to resolve SL/TP prices from open orders
                    sl_price = 0.0
                    tp_price = 0.0
                    open_orders = client.futures_get_open_orders(symbol=symbol)
                    for o in open_orders:
                        if o["type"] == "STOP_MARKET":
                            sl_price = float(o["stopPrice"])
                        elif o["type"] == "TAKE_PROFIT_MARKET":
                            tp_price = float(o["stopPrice"])

                    # Reconstruct the trade dict
                    trade = {
                        "id": "reconciled_" + str(int(time.time())),
                        "symbol": symbol,
                        "direction": direction,
                        "strategy": "reconciled_startup",
                        "entry_price": entry_price,
                        "qty": qty,
                        "sl_price": sl_price if sl_price > 0.0 else (entry_price * 0.985 if direction == "LONG" else entry_price * 1.015),
                        "tp_price": tp_price if tp_price > 0.0 else (entry_price * 1.03 if direction == "LONG" else entry_price * 0.97),
                        "entry_time": datetime.now(timezone.utc).isoformat(),
                        "status": "open",
                        "peak_price": entry_price,
                        "pnl": 0.0,
                        "partially_closed": False,
                        "atr_at_entry": (entry_price * 0.01) # fallback approximation
                    }

                    with state_lock:
                        state["open_trade"] = trade
                    
                    push_log(f"Synced local trade state: SL=${trade['sl_price']:.2f}, TP=${trade['tp_price']:.2f}")
                    send_telegram(f"🔌 Reconciled open position: {direction} {qty} {symbol} @ ${entry_price:.2f} synced.")
                    return
        push_log(f"Startup reconciliation: No active position found on Binance for {symbol}.")
    except Exception as e:
        push_log(f"Startup position reconciliation failed: {e}. Bot will continue with empty trade state.", "warning")

def open_trade_limit(client: Client, signal: str, strategy: str, size_usdt: float, atr: float, signals_state: dict = None, sl_mult: float = 1.5, tp_mult: float = 3.0, symbol: str = None):
    """
    Places a Limit Order to act as maker and minimize fees.
    Cancels the order if unfilled after 30 seconds.
    """
    symbol = symbol if symbol else config.SYMBOL
    if size_usdt <= 0:
        return

    # Check and init exchange filters
    if symbol not in symbol_rules:
        init_exchange_info(client, symbol)

    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        current_price = float(ticker["price"])
        
        # Calculate quantity
        qty = round_qty(symbol, size_usdt / current_price)
        if qty <= 0:
            push_log("Qty rounded to 0. Skipping trade.", "warning")
            return

        side = "BUY" if signal == "LONG" else "SELL"
        
        # Calculate SL/TP levels (ATR-based)
        sl_dist = atr * sl_mult
        tp_dist = atr * tp_mult
        
        # Place limit price slightly favorable to guarantee Maker execution (optional)
        # For simplicity, place limit at current tick price
        limit_price = round_price(symbol, current_price)
        
        sl_price = round_price(symbol, limit_price - sl_dist if signal == "LONG" else limit_price + sl_dist)
        tp_price = round_price(symbol, limit_price + tp_dist if signal == "LONG" else limit_price - tp_dist)

        push_log(f"Placing LIMIT {side} order for {qty} {symbol} @ ${limit_price:.2f}...")
        
        order = execute_client_call(
            client.futures_create_order,
            symbol=symbol,
            side=side,
            type="LIMIT",
            price=limit_price,
            quantity=qty,
            timeInForce="GTC"
        )
        
        order_id = order["orderId"]
        
        # Wait 30 seconds for fill
        filled = False
        executed_qty = 0.0
        avg_price = limit_price
        for _ in range(30):
            time.sleep(1)
            status = execute_client_call(client.futures_get_order, symbol=symbol, orderId=order_id)
            executed_qty = float(status.get("executedQty", 0.0))
            avg_price = float(status.get("avgPrice", limit_price))
            if status["status"] == "FILLED":
                filled = True
                break
            elif status["status"] in ["CANCELED", "REJECTED", "EXPIRED"]:
                push_log(f"Limit order {order_id} ended with status: {status['status']}", "warning")
                if executed_qty > 0.0:
                    break
                return

        if not filled:
            push_log(f"Limit order {order_id} remained unfilled for 30s. Cancelling remaining...", "warning")
            try:
                execute_client_call(client.futures_cancel_order, symbol=symbol, orderId=order_id)
            except Exception as ce:
                pass
            
            # Fetch final status after cancellation
            try:
                status = execute_client_call(client.futures_get_order, symbol=symbol, orderId=order_id)
                executed_qty = float(status.get("executedQty", executed_qty))
                avg_price = float(status.get("avgPrice", avg_price))
            except Exception:
                pass


        if executed_qty <= 0.0:
            push_log("Order was completely unfilled. Aborting trade.", "warning")
            return

        if executed_qty < qty:
            push_log(f"⚠️ PARTIAL FILL DETECTED: Filled {executed_qty} out of {qty} {symbol}. Adjusting position size to filled quantity.", "warning")
            qty = executed_qty
            limit_price = avg_price


        # Place SL and TP on exchange
        sl_side = "SELL" if signal == "LONG" else "BUY"
        
        # Retry logic for Stop Loss
        sl_order = None
        for attempt in range(3):
            try:
                sl_order = execute_client_call(
                    client.futures_create_order,
                    symbol=symbol,
                    side=sl_side,
                    type="STOP_MARKET",
                    stopPrice=sl_price,
                    closePosition=True
                )
                break
            except Exception as e:
                push_log(f"Attempt {attempt+1} to place SL order failed: {e}. Retrying...", "warning")
                time.sleep(1)
        if not sl_order:
            msg = f"🚨 CRITICAL: Failed to place Stop Loss on exchange for {symbol}! Manual action required immediately."
            push_log(msg, "error")
            send_telegram(msg)
            raise Exception("Failed to place Stop Loss order")

        push_log(f"Stop Loss order placed successfully: ID={sl_order.get('orderId')}, Status={sl_order.get('status')}")
        
        # Retry logic for Take Profit
        tp_order = None
        for attempt in range(3):
            try:
                tp_order = execute_client_call(
                    client.futures_create_order,
                    symbol=symbol,
                    side=sl_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=tp_price,
                    closePosition=True
                )
                break
            except Exception as e:
                push_log(f"Attempt {attempt+1} to place TP order failed: {e}. Retrying...", "warning")
                time.sleep(1)
        if not tp_order:
            msg = f"🚨 CRITICAL: Failed to place Take Profit on exchange for {symbol}! Manual action required immediately."
            push_log(msg, "error")
            send_telegram(msg)
            raise Exception("Failed to place Take Profit order")

        push_log(f"Take Profit order placed successfully: ID={tp_order.get('orderId')}, Status={tp_order.get('status')}")



        trade = {
            "id":           str(order_id),
            "symbol":       symbol,
            "direction":    signal,
            "strategy":     strategy,
            "entry_price":  limit_price,
            "qty":          qty,
            "sl_price":     sl_price,
            "tp_price":     tp_price,
            "entry_time":   datetime.now(timezone.utc).isoformat(),
            "status":       "open",
            "peak_price":   limit_price,
            "pnl":          0.0,
            "partially_closed": False,
            "atr_at_entry": atr,
            "market_phase": state.get("market_regime", "RANGING"),
            "timeframe":    "5m",
            # Signal trace for DB learning
            "signal_ema":     signals_state.get("ema", 0) if signals_state else 0,
            "signal_rsi":     signals_state.get("rsi", 0) if signals_state else 0,
            "signal_vwap":    signals_state.get("vwap", 0) if signals_state else 0,
            "signal_volume":  signals_state.get("volume", 0) if signals_state else 0,
            "signal_macd":    signals_state.get("macd", 0) if signals_state else 0,
            "signal_funding": signals_state.get("funding", 0) if signals_state else 0,
            "signal_oi":      signals_state.get("oi", 0) if signals_state else 0,
        }

        with state_lock:
            state["open_trade"] = trade

        # Persist open trade state in SQLite database for recovery
        save_trade(trade)

        msg = f"✅ OPENED {signal} | {qty} {symbol} @ ${limit_price:.2f} | SL=${sl_price:.2f} TP=${tp_price:.2f}"
        push_log(msg)
        send_telegram(f"⚡ <b>Trade Opened</b>\n{msg}\nStrategy: {strategy}")

    except BinanceAPIException as e:
        push_log(f"❌ Binance Order Error: {e}", "error")
        with state_lock:
            state["errors"].append(str(e))

def close_position_market(client: Client, reason: str = "manual"):
    """Closes the active trade immediately at market price."""
    with state_lock:
        trade = state["open_trade"]
    if not trade:
        return

    symbol = trade["symbol"]
    try:
        side = "SELL" if trade["direction"] == "LONG" else "BUY"
        qty = trade["qty"]
        
        # Check current position size from Binance directly to ensure we match perfectly
        pos_info = client.futures_position_information(symbol=symbol)
        for pos in pos_info:
            if pos["symbol"] == symbol:
                amt = float(pos["positionAmt"])
                if amt != 0:
                    qty = abs(amt)

        # Place market order to close
        client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
            reduceOnly=True
        )

        # Cancel all open orders on exchange
        client.futures_cancel_all_open_orders(symbol=symbol)

        ticker = client.futures_symbol_ticker(symbol=symbol)
        exit_price = float(ticker["price"])

        # Calculate PnL
        if trade["direction"] == "LONG":
            pnl = (exit_price - trade["entry_price"]) * qty
        else:
            pnl = (trade["entry_price"] - exit_price) * qty

        # Taker fee calculation for exiting at market (0.04% of size)
        exit_fee = qty * exit_price * 0.0004
        entry_fee = qty * trade["entry_price"] * 0.0002 # limit maker entry
        total_fee = exit_fee + entry_fee

        trade["exit_price"] = exit_price
        trade["exit_time"] = datetime.now(timezone.utc).isoformat()
        trade["pnl"] = round(pnl, 4)
        trade["fee_paid"] = round(total_fee, 4)
        trade["status"] = "closed"
        trade["close_reason"] = reason

        with state_lock:
            state["closed_trades"].insert(0, trade)
            state["closed_trades"] = state["closed_trades"][:200]
            state["open_trade"] = None
            state["total_pnl"] += pnl
            if pnl >= 0:
                state["win_count"] += 1
            else:
                state["loss_count"] += 1

        # Write to SQLite
        save_trade(trade)

        msg = f"{'🟢 WIN' if pnl >= 0 else '🔴 LOSS'} CLOSED {trade['direction']} | PnL: ${pnl:+.4f} | Fee: ${total_fee:.4f} | Reason: {reason}"
        push_log(msg)
        send_telegram(f"⏹ <b>Trade Closed</b>\n{msg}")

    except Exception as e:
        push_log(f"❌ Market Close Error: {e}", "error")

def manage_trailing_stop(client: Client):
    """
    Adjusts trailing stop based on price performance.
    Moves to breakeven, trailing, or executes a partial close (70%/30%).
    """
    with state_lock:
        trade = state["open_trade"]
    if not trade:
        return

    symbol = trade["symbol"]
    if symbol not in symbol_rules:
        init_exchange_info(client, symbol)

    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])
        
        entry = trade["entry_price"]
        direction = trade["direction"]
        atr = trade.get("atr_at_entry", 0.0)
        
        # Calculate peaks
        updated_peak = False
        if direction == "LONG":
            if price > trade["peak_price"]:
                trade["peak_price"] = price
                updated_peak = True
            profit_pct = (price - entry) / entry
            profit_atr = (price - entry) / (atr + 1e-8)
        else:
            if price < trade["peak_price"]:
                trade["peak_price"] = price
                updated_peak = True
            profit_pct = (entry - price) / entry
            profit_atr = (entry - price) / (atr + 1e-8)

        if updated_peak:
            with state_lock:
                state["open_trade"]["peak_price"] = trade["peak_price"]

        # ── Breakeven Trigger (1x ATR) ─────────────────────────────
        # If profit moves 1x ATR in profit, adjust SL to Entry
        if profit_atr >= 1.0 and trade["sl_price"] != entry:
            new_sl = entry
            # Cancel old open orders on Binance and place new SL order
            client.futures_cancel_all_open_orders(symbol=symbol)
            sl_side = "SELL" if direction == "LONG" else "BUY"
            
            client.futures_create_order(
                symbol=symbol,
                side=sl_side,
                type="STOP_MARKET",
                stopPrice=new_sl,
                closePosition=True
            )
            # Re-place TP
            client.futures_create_order(
                symbol=symbol,
                side=sl_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=trade["tp_price"],
                closePosition=True
            )

            with state_lock:
                state["open_trade"]["sl_price"] = new_sl
            push_log(f"🔁 Breakeven Triggered. Moved Stop Loss to Entry price: ${new_sl:.2f}")

        # ── Trailing Stop Trigger (2x ATR) ──────────────────────────
        # Trail SL at 1x ATR behind peak
        elif profit_atr >= 2.0:
            if direction == "LONG":
                target_sl = round_price(symbol, trade["peak_price"] - (atr * 1.0))
                # Only move SL upwards
                if target_sl > trade["sl_price"]:
                    update_sl_on_exchange(client, symbol, direction, target_sl, trade["tp_price"])
            else:
                target_sl = round_price(symbol, trade["peak_price"] + (atr * 1.0))
                # Only move SL downwards
                if target_sl < trade["sl_price"] or trade["sl_price"] == entry:
                    update_sl_on_exchange(client, symbol, direction, target_sl, trade["tp_price"])

        # ── Partial Take Profit (3x ATR) ──────────────────────────
        # Close 70% of position and trail remaining 30%
        elif profit_atr >= 3.0 and not trade.get("partially_closed", False):
            # Enforce Binance minimum notional limits
            remaining_qty = round_qty(symbol, trade["qty"] * 0.30)
            remaining_notional = remaining_qty * price
            
            if remaining_notional >= 5.0:
                # We can safely partial close 70%
                partial_qty = round_qty(symbol, trade["qty"] * 0.70)
                side = "SELL" if direction == "LONG" else "BUY"
                
                push_log(f"💰 PARTIAL TAKE PROFIT: Price hit 3x ATR. Selling 70% ({partial_qty} {symbol})...")
                
                client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=partial_qty,
                    reduceOnly=True
                )
                
                # Update local trade properties
                with state_lock:
                    state["open_trade"]["qty"] = remaining_qty
                    state["open_trade"]["partially_closed"] = True
                    # Set trailing stop tighter to lock in profit (e.g. 0.5x ATR behind peak)
                    new_sl = round_price(symbol, price - (atr * 0.5) if direction == "LONG" else price + (atr * 0.5))
                    state["open_trade"]["sl_price"] = new_sl
                
                # Cancel open exchange orders and write tighter SL for the remaining 30%
                client.futures_cancel_all_open_orders(symbol=symbol)
                client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type="STOP_MARKET",
                    stopPrice=new_sl,
                    closePosition=True
                )
                push_log(f"Tighter trailing stop placed for remaining 30% ({remaining_qty} {symbol}) at ${new_sl:.2f}")
                send_telegram(f"💰 <b>Partial Take Profit</b>\nClosed 70% of {symbol} at ${price:.2f}. Remaining 30% is trailing.")
            else:
                # Remaining 30% is under minimum notional, close 100% of the trade at market to secure profits
                push_log(f"💰 FULL TAKE PROFIT: Price hit 3x ATR. Sizing too small for partial, closing 100%.")
                close_position_market(client, "take_profit")

    except Exception as e:
        push_log(f"Error in trailing stop check: {e}", "warning")

def update_sl_on_exchange(client: Client, symbol: str, direction: str, new_sl: float, tp_price: float):
    try:
        client.futures_cancel_all_open_orders(symbol=symbol)
        sl_side = "SELL" if direction == "LONG" else "BUY"
        client.futures_create_order(
            symbol=symbol,
            side=sl_side,
            type="STOP_MARKET",
            stopPrice=new_sl,
            closePosition=True
        )
        # Re-place TP
        client.futures_create_order(
            symbol=symbol,
            side=sl_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price,
            closePosition=True
        )
        with state_lock:
            state["open_trade"]["sl_price"] = new_sl
        push_log(f"🔁 Adjusted trailing stop to ${new_sl:.2f}")
    except Exception as e:
        push_log(f"Failed to adjust trailing stop on exchange: {e}", "warning")

def check_external_close(client: Client, symbol: str):
    """Syncs state if the position was closed externally on Binance (e.g. SL/TP order filled)."""
    with state_lock:
        trade = state["open_trade"]
    if not trade:
        return

    try:
        pos_info = client.futures_position_information(symbol=symbol)
        for pos in pos_info:
            if pos["symbol"] == symbol:
                amt = float(pos["positionAmt"])
                if amt == 0:
                    # Sync
                    push_log("📋 Sync: Position closed on Binance exchange directly (SL/TP hit).")
                    
                    # Fetch last trades to find exit price/commission fees
                    ticker = client.futures_symbol_ticker(symbol=symbol)
                    exit_price = float(ticker["price"])

                    qty = trade["qty"]
                    if direction := trade["direction"] == "LONG":
                        pnl = (exit_price - trade["entry_price"]) * qty
                    else:
                        pnl = (trade["entry_price"] - exit_price) * qty

                    total_fee = qty * exit_price * 0.0004 + qty * trade["entry_price"] * 0.0002

                    trade["exit_price"] = exit_price
                    trade["exit_time"] = datetime.now(timezone.utc).isoformat()
                    trade["pnl"] = round(pnl, 4)
                    trade["fee_paid"] = round(total_fee, 4)
                    trade["status"] = "closed"
                    trade["close_reason"] = "sl_tp_hit"

                    with state_lock:
                        state["closed_trades"].insert(0, trade)
                        state["open_trade"] = None
                        state["total_pnl"] += pnl
                        if pnl >= 0:
                            state["win_count"] += 1
                        else:
                            state["loss_count"] += 1

                    # Write to SQLite
                    save_trade(trade)

                    # Cancel any leftover TP/SL order
                    client.futures_cancel_all_open_orders(symbol=symbol)

                    msg = f"🎯 CLOSED (SL/TP trigger) | PnL: ${pnl:+.4f} | Reason: sl_tp_hit"
                    push_log(msg)
                    send_telegram(f"🎯 <b>Position Closed by Exchange</b>\n{msg}")
    except Exception as e:
        pass

def update_unrealised_pnl_state(client: Client, symbol: str):
    """Calculates open trade's current PnL and updates state variables."""
    with state_lock:
        trade = state["open_trade"]
    if not trade:
        return
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker["price"])
        
        qty = trade["qty"]
        if trade["direction"] == "LONG":
            pnl = (price - trade["entry_price"]) * qty
        else:
            pnl = (trade["entry_price"] - price) * qty

        with state_lock:
            state["open_trade"]["pnl"] = round(pnl, 4)
            state["open_trade"]["current_price"] = price
    except Exception:
        pass
