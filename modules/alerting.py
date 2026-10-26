import requests
import config
from datetime import datetime, timezone
from modules.state import push_log, state_lock, state

def send_telegram(msg: str):
    """Sends an HTML formatted Telegram message using Bot API if token/chat_id are defined."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        with state_lock:
            state["telegram_status"] = "inactive"
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML"
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            with state_lock:
                state["telegram_status"] = "active"
        else:
            push_log(f"Telegram failed: status {r.status_code}, response: {r.text}", "warning")
            with state_lock:
                state["telegram_status"] = "error"
    except Exception as e:
        push_log(f"Telegram connection exception: {e}", "warning")
        with state_lock:
            state["telegram_status"] = "error"

def send_trade_alert(pair: str, direction: str, entry_price: float, position_size_usdt: float, sl_price: float, tp_price: float, fee_type: str, estimated_fee: float, balance: float):
    """Sends a detailed alert when a new trade is opened (Q51)."""
    msg = (
        f"⚡ <b>Trade Opened</b>\n"
        f"Pair: {pair}\n"
        f"Direction: {direction}\n"
        f"Entry Price: ${entry_price:,.2f}\n"
        f"Position Size: ${position_size_usdt:.2f} USDT\n"
        f"Stop Loss: ${sl_price:,.2f}\n"
        f"Take Profit: ${tp_price:,.2f}\n"
        f"Fee Type: {fee_type}\n"
        f"Estimated Fee: ${estimated_fee:.4f} USDT\n"
        f"Current Balance: ${balance:.2f} USDT"
    )
    send_telegram(msg)

def send_daily_summary(balance: float, mode: str = "live"):
    """Sends a summary of today's trades (Q52)."""
    import sqlite3
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    try:
        # Fetch trades closed in last 24h
        cursor.execute("""
            SELECT win, pnl_usdt, fee_paid, net_pnl 
            FROM trades 
            WHERE mode = ? AND status = 'closed' AND timestamp > datetime('now', '-1 day')
        """, (mode,))
        rows = cursor.fetchall()
        
        trades_count = len(rows)
        wins = sum(1 for r in rows if r["win"] == 1)
        losses = trades_count - wins
        win_rate = (wins / trades_count * 100.0) if trades_count > 0 else 0.0
        
        gross_pnl = sum(float(r["pnl_usdt"]) for r in rows)
        total_fees = sum(float(r["fee_paid"]) for r in rows)
        net_pnl = sum(float(r["net_pnl"]) for r in rows)
        
        sign = "+" if net_pnl >= 0 else ""
        gross_sign = "+" if gross_pnl >= 0 else ""
        
        msg = (
            f"📊 <b>Daily Trading Summary (UTC)</b>\n"
            f"Date: {date_str}\n"
            f"Trades Taken: {trades_count}\n"
            f"Wins: {wins} | Losses: {losses}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Gross PnL: {gross_sign}${gross_pnl:.4f}\n"
            f"Total Fees Paid: ${total_fees:.4f}\n"
            f"Net PnL: {sign}${net_pnl:.4f}\n"
            f"Current Account Balance: ${balance:.2f}"
        )
        send_telegram(msg)
    except Exception as e:
        push_log(f"Daily summary generation failed: {e}", "warning")
    finally:
        conn.close()
