from datetime import datetime, timedelta, timezone
from modules.state import state, state_lock, push_log
from modules.alerting import send_telegram
import config

def get_weekly_start_balance(default_val: float) -> float:
    import sqlite3
    import config
    balance = default_val
    try:
        conn = sqlite3.connect(config.DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT balance FROM account_snapshots 
            WHERE timestamp >= datetime('now', '-7 days')
            ORDER BY timestamp ASC LIMIT 1
        """)
        row = cursor.fetchone()
        if row and row[0]:
            balance = float(row[0])
        else:
            cursor.execute("SELECT balance FROM account_snapshots ORDER BY timestamp ASC LIMIT 1")
            row2 = cursor.fetchone()
            if row2 and row2[0]:
                balance = float(row2[0])
        conn.close()
    except Exception:
        pass
    return balance

def check_circuit_breaker(current_balance: float, peak_balance: float, daily_start_balance: float) -> bool:
    """
    Checks drawdown thresholds against start balance, peak balance, and weekly start balance.
    Saves pause timestamps inside the state if boundaries are breached.
    """
    if daily_start_balance <= 0 or peak_balance <= 0:
        return False

    daily_loss = (daily_start_balance - current_balance) / daily_start_balance
    peak_loss = (peak_balance - current_balance) / peak_balance
    
    weekly_start = get_weekly_start_balance(current_balance)
    weekly_loss = (weekly_start - current_balance) / weekly_start if weekly_start > 0 else 0.0

    now = datetime.now(timezone.utc)

    # 1. 25% Drawdown from peak balance -> 48h lock out
    if peak_loss >= 0.25:
        until = now + timedelta(hours=48)
        with state_lock:
            state["circuit_breaker_paused"] = True
            state["circuit_breaker_until"] = until.isoformat()
        msg = f"⚠️ CIRCUIT BREAKER: All-time peak drawdown >= 25% breached! Peak: ${peak_balance:.2f}, Current: ${current_balance:.2f}. Trading PAUSED for 48 hours until {until.strftime('%Y-%m-%d %H:%M:%S UTC')}."
        push_log(msg, "error")
        send_telegram(msg)
        return True

    # 2. 20% Weekly Drawdown -> 24h lock out
    if weekly_loss >= config.WEEKLY_LOSS_LIMIT:
        until = now + timedelta(hours=24)
        with state_lock:
            state["circuit_breaker_paused"] = True
            state["circuit_breaker_until"] = until.isoformat()
        msg = f"⚠️ CIRCUIT BREAKER: Weekly drawdown limit >= {config.WEEKLY_LOSS_LIMIT*100}% breached! Weekly Start: ${weekly_start:.2f}, Current: ${current_balance:.2f}. Trading PAUSED for 24 hours until {until.strftime('%Y-%m-%d %H:%M:%S UTC')}."
        push_log(msg, "warning")
        send_telegram(msg)
        return True

    # 3. 10% Daily Drawdown -> 6h lock out
    if daily_loss >= 0.10:
        until = now + timedelta(hours=6)
        with state_lock:
            state["circuit_breaker_paused"] = True
            state["circuit_breaker_until"] = until.isoformat()
        msg = f"⚠️ CIRCUIT BREAKER: Daily loss limit >= 10% breached! Daily Start: ${daily_start_balance:.2f}, Current: ${current_balance:.2f}. Trading PAUSED for 6 hours until {until.strftime('%H:%M:%S UTC')}."
        push_log(msg, "warning")
        send_telegram(msg)
        return True

    return False

def is_circuit_breaker_active() -> bool:
    """Verifies lock timer status. Resets if the pause window has expired."""
    with state_lock:
        paused = state["circuit_breaker_paused"]
        until_str = state["circuit_breaker_until"]

    if not paused:
        return False

    if not until_str:
        with state_lock:
            state["circuit_breaker_paused"] = False
        return False

    until_time = datetime.fromisoformat(until_str).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)

    if now >= until_time:
        with state_lock:
            state["circuit_breaker_paused"] = False
            state["circuit_breaker_until"] = None
        push_log("Circuit breaker cooldown window has expired. Trading re-enabled.")
        send_telegram("ℹ️ Cooldown window expired. Bot trading re-enabled.")
        return False

    return True
