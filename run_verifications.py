import os
import sys
import time
from datetime import datetime, timezone
import pandas as pd

# Adjust import path to include current directory
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from modules.state import state, state_lock
from modules.learning_engine import init_db, get_active_weights, get_daily_fees_paid, get_connection
from modules.fee_guardian import passes_fee_gate
from modules.position_sizer import calculate_position_size
from modules.circuit_breaker import check_circuit_breaker
from modules.trade_manager import get_client
from modules.signals import calculate_signals
from main import fetch_candles, get_market_metrics

def main():
    print("=" * 60)
    print("⚡ BINANCE FUTURES TRADING BOT VERIFICATION SUITE ⚡")
    print("=" * 60)
    
    init_db()
    client = get_client()
    
    # -------------------------------------------------------------
    # 1. LIVE PRINT OF FEE GATE
    # -------------------------------------------------------------
    print("\n[1] Fee Guardian ATR Validation Test")
    print("-" * 50)
    print("Simulating fee gate with a very small ATR (0.01) at $60,000 price and $10 size:")
    result_small = passes_fee_gate(atr=0.01, entry_price=60000.0, position_size_usdt=10.0)
    print(f"Result for ATR 0.01: {result_small} (Expected: False)")
    
    print("\nSimulating fee gate with a normal ATR (150.0) at $60,000 price and $10 size:")
    result_normal = passes_fee_gate(atr=150.0, entry_price=60000.0, position_size_usdt=10.0)
    print(f"Result for ATR 150.0: {result_normal} (Expected: True)")
    
    # -------------------------------------------------------------
    # 2. MAKER VS TAKER COMMISSIONS LOGIC
    # -------------------------------------------------------------
    print("\n[2] Commission Calculation Logic")
    print("-" * 50)
    print("Fee percentages are configured inside `modules/trade_manager.py`:")
    print("  - Maker Order (Entry): 0.02% (multiplier: 0.0002)")
    print("  - Taker Order (Exit):  0.04% (multiplier: 0.0004)")
    print("  - Total expected round-trip fee: 0.06% of position notional value.")
    
    # -------------------------------------------------------------
    # 3. CONFLUENCE & DYNAMIC SIGNALS RUN
    # -------------------------------------------------------------
    print("\n[3] Live Confluence & Signal Calculation for Configured Pairs")
    print("-" * 50)
    for symbol in config.ACTIVE_PAIRS:
        print(f"\nEvaluating signals for {symbol}:")
        try:
            candles_dict = {}
            for tf in ["1m", "5m", "15m", "1h"]:
                candles_dict[tf] = fetch_candles(client, tf, 250, symbol=symbol)
                
            funding, oi_chg = get_market_metrics(client, symbol)
            weights = get_active_weights()
            
            signals_res = calculate_signals(symbol, candles_dict, funding, oi_chg, weights)
            print("Individual Signal States (1 = LONG, -1 = SHORT, 0 = NEUTRAL):")
            for key, val in signals_res["states"].items():
                print(f"  - {key.upper()}: {val} (Weight: {weights.get(key, 1.0)})")
            print(f"Macro Trend: {signals_res['macro_trend']}")
            print(f"Long Confluence Score:  {signals_res['long_score']}")
            print(f"Short Confluence Score: {signals_res['short_score']}")
            print(f"Confluence Direction:   {signals_res.get('confluence_direction')}")
            print(f"Agreeing Strategies:    {signals_res.get('confluence_strategies')}")
        except Exception as e:
            print(f"Error executing live signal analysis for {symbol}: {e}")

    # -------------------------------------------------------------
    # 4. MINIMUM NOTIONAL GATE CHECK
    # -------------------------------------------------------------
    print("\n[4] Position Sizer & Minimum Notional gate check")
    print("-" * 50)
    print("Testing position sizing on $10 account with ATR 120.0:")
    size_1 = calculate_position_size(account_balance=10.0, atr=120.0, entry_price=60000.0)
    print(f"Calculated Size: ${size_1:.2f} (Expected: $0.00 due to $5 minimum notional check)")
    
    print("\nTesting position sizing on $200 account with ATR 120.0:")
    size_2 = calculate_position_size(account_balance=200.0, atr=120.0, entry_price=60000.0)
    print(f"Calculated Size: ${size_2:.2f} (Expected: > $5.00)")

    # -------------------------------------------------------------
    # 5. SQLITE SIGNAL WEIGHTS
    # -------------------------------------------------------------
    print("\n[5] Current SQLite weights table")
    print("-" * 50)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM signal_weights")
    rows = cursor.fetchall()
    # Print distinct signal weight parameters to avoid excessive line flooding
    seen = set()
    for r in rows:
        name = r['signal_name']
        if name not in seen:
            seen.add(name)
            print(f"Signal: {r['signal_name']:<10} | Weight: {r['weight']:<5.4f} | Wins: {r['win_count']:<3} | Losses: {r['loss_count']:<3}")
    conn.close()

    # -------------------------------------------------------------
    # 6. DAILY FEES PAID THROTTLING
    # -------------------------------------------------------------
    print("\n[6] Daily Fees Paid and Throttling")
    print("-" * 50)
    print(f"Current Daily Fees Paid (Live):  ${get_daily_fees_paid('live'):.4f}")
    print(f"Current Daily Fees Paid (Paper): ${get_daily_fees_paid('paper'):.4f}")
    print(f"Throttling limit: {config.FEE_THROTTLE_LIMIT * 100}% of balance.")

    # -------------------------------------------------------------
    # 7. CIRCUIT BREAKER VERIFICATION
    # -------------------------------------------------------------
    print("\n[7] Drawdown Circuit Breaker Simulation")
    print("-" * 50)
    print("Simulating a normal condition ($10 balance, peak $10, daily start $10):")
    cb_normal = check_circuit_breaker(current_balance=10.0, peak_balance=10.0, daily_start_balance=10.0)
    print(f"Circuit Breaker Triggered: {cb_normal} (Expected: False)")
    
    print("\nSimulating a 10% daily drawdown ($9.00 balance, peak $10, daily start $10):")
    cb_drawdown = check_circuit_breaker(current_balance=9.00, peak_balance=10.0, daily_start_balance=10.0)
    print(f"Circuit Breaker Triggered: {cb_drawdown} (Expected: True)")
    
    # -------------------------------------------------------------
    # 8. LAST 5 TRADES IN DATABASE
    # -------------------------------------------------------------
    print("\n[8] Last 5 Trades stored in Database")
    print("-" * 50)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5")
    rows = cursor.fetchall()
    if len(rows) == 0:
        print("No trades stored in database yet.")
    else:
        for r in rows:
            print(f"ID: {r['id']:<15} | Pair: {r['pair']:<10} | Mode: {r['mode']:<6} | Net PnL: ${r['net_pnl']:+.4f} | Hour: {r['hour_of_day']}")
    conn.close()

if __name__ == "__main__":
    main()
