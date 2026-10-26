from modules.state import push_log, state, state_lock

def calculate_position_size(account_balance: float, atr: float, entry_price: float, win_rate: float = 0.5, avg_win: float = 0.03, avg_loss: float = 0.015) -> float:
    """
    Calculates position size using the half-Kelly Criterion.
    Applies streak modifiers and enforces the minimum notional limit of $5 USDT.
    """
    if entry_price <= 0 or atr <= 0:
        return 0.0

    # Ensure valid inputs for Kelly
    if win_rate <= 0 or win_rate >= 1:
        win_rate = 0.5
    if avg_win <= 0:
        avg_win = 0.03
    if avg_loss <= 0:
        avg_loss = 0.015

    # 1. Kelly Criterion
    # kelly = (p * b - q) / b where b is odds (avg_win / avg_loss)
    # kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
    kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
    safe_kelly = kelly * 0.5  # Half-Kelly for safety

    # 2. Base risk percentage constraints
    risk_pct = min(safe_kelly, 0.15)
    risk_pct = max(risk_pct, 0.05)
    risk_pct = min(risk_pct, 0.20)

    # 3. Streak modifiers (read from shared state)
    from datetime import datetime
    with state_lock:
        closed_trades = state.get("closed_trades", [])
        milestone_time_str = state.get("last_milestone_time")
    
    milestone_time = None
    if milestone_time_str:
        milestone_time = datetime.fromisoformat(milestone_time_str.replace("Z", "+00:00"))
        
    # Calculate streaks
    consec_wins = 0
    consec_losses = 0
    
    for t in closed_trades:
        # If trade exit_time precedes milestone time, stop streak aggregation
        exit_time_str = t.get("exit_time")
        if milestone_time and exit_time_str:
            exit_time = datetime.fromisoformat(exit_time_str.replace("Z", "+00:00"))
            if exit_time < milestone_time:
                break
                
        pnl = t.get("pnl", 0.0)
        if pnl > 0:
            if consec_losses == 0:
                consec_wins += 1
            else:
                break
        elif pnl < 0:
            if consec_wins == 0:
                consec_losses += 1
            else:
                break


    if consec_wins >= 5:
        # Increase risk by 2% on hot streak
        risk_pct += 0.02
        push_log(f"🔥 HOT STREAK: {consec_wins} wins. Increasing risk percentage by +2% (now {risk_pct*100:.1f}%)")
    elif consec_losses >= 3:
        # Set to minimum 5% risk on cold streak
        risk_pct = 0.05
        push_log(f"❄️ COLD STREAK: {consec_losses} losses. Reducing risk percentage to minimum 5%")
    elif consec_losses > 0:
        # Drop by 3% after any single loss
        risk_pct -= 0.03
        risk_pct = max(risk_pct, 0.05)
        push_log(f"📉 STREAK RECOVERY: Last trade was a loss. Reducing risk percentage by -3% (now {risk_pct*100:.1f}%)")

    # 4. Position Size in USDT calculation
    # Stop distance in percent = (ATR * 1.5) / Entry Price
    stop_distance_pct = (atr * 1.5) / entry_price
    if stop_distance_pct <= 0:
        return 0.0

    risk_amount_usdt = account_balance * risk_pct
    position_size_usdt = risk_amount_usdt / stop_distance_pct

    # Enforce maximum hard constraint (20% of account balance maximum)
    max_hard_limit = account_balance * 0.20
    if position_size_usdt > max_hard_limit:
        position_size_usdt = max_hard_limit

    # 5. Minimum Notional Limit Check (Binance Futures has $5 minimum)
    if position_size_usdt < 5.0:
        push_log(
            f"❌ NOTIONAL GATE KILLED: Position size ${position_size_usdt:.2f} is below minimum $5.00 USDT requirement. "
            f"Skipping trade setup to prevent client order rejection.",
            "warning"
        )
        return 0.0

    return position_size_usdt
