import sqlite3
import os
from datetime import datetime, timezone
import config
from modules.state import push_log

def get_connection():
    db_dir = os.path.dirname(config.DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(config.DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes sqlite database tables."""
    conn = get_connection()
    cursor = conn.cursor()

    # Create trades table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY,
        timestamp TEXT,
        pair TEXT,
        direction TEXT,
        entry_price REAL,
        exit_price REAL,
        position_size REAL,
        pnl_usdt REAL,
        pnl_pct REAL,
        fee_paid REAL,
        net_pnl REAL,
        win INTEGER,
        exit_reason TEXT,
        signal_ema INTEGER,
        signal_rsi INTEGER,
        signal_vwap INTEGER,
        signal_volume INTEGER,
        signal_macd INTEGER,
        signal_funding INTEGER,
        signal_oi INTEGER,
        market_phase TEXT,
        timeframe TEXT,
        atr_at_entry REAL,
        hour_of_day INTEGER,
        day_of_week INTEGER,
        mode TEXT DEFAULT 'live'
    );
    """)

    # Migration checks: Add mode and fee_type columns if database exists from older versions
    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN mode TEXT DEFAULT 'live'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN fee_type TEXT DEFAULT 'TAKER'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN strategy TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN status TEXT DEFAULT 'closed'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN sl_price REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE trades ADD COLUMN tp_price REAL")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Create strategy_performance table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS strategy_performance (
        strategy_name TEXT,
        market_regime TEXT,
        win_count INTEGER DEFAULT 0,
        loss_count INTEGER DEFAULT 0,
        PRIMARY KEY (strategy_name, market_regime)
    );
    """)


    # Create signal_weights table with composite key (signal_name, hour_of_day)
    try:
        cursor.execute("SELECT hour_of_day FROM signal_weights LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("DROP TABLE IF EXISTS signal_weights")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS signal_weights (
        signal_name TEXT,
        hour_of_day INTEGER,
        weight REAL DEFAULT 1.0,
        win_count INTEGER DEFAULT 0,
        loss_count INTEGER DEFAULT 0,
        last_updated TEXT,
        PRIMARY KEY (signal_name, hour_of_day)
    );
    """)

    # Create account_snapshots table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS account_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        balance REAL,
        total_pnl REAL,
        win_rate REAL,
        total_trades INTEGER,
        total_fees_paid REAL
    );
    """)

    # Pre-populate weights if empty
    cursor.execute("SELECT COUNT(*) FROM signal_weights")
    if cursor.fetchone()[0] == 0:
        signals = ['ema', 'rsi', 'vwap', 'volume', 'macd', 'funding', 'oi']
        for sig in signals:
            for hr in range(24):
                cursor.execute("""
                    INSERT INTO signal_weights (signal_name, hour_of_day, weight, last_updated) 
                    VALUES (?, ?, 1.0, ?)
                """, (sig, hr, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        push_log("Initialized default temporal signal weights for 24 hours in SQLite.")

    conn.close()

def save_trade(trade: dict):
    """Inserts or updates a trade in the database, supporting both open and closed status."""
    conn = get_connection()
    cursor = conn.cursor()
    
    timestamp = trade.get("exit_time") or trade.get("entry_time") or datetime.now(timezone.utc).isoformat()
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    
    # Defaults for signals if not explicitly in the trade dict
    signal_ema = trade.get("signal_ema", 0)
    signal_rsi = trade.get("signal_rsi", 0)
    signal_vwap = trade.get("signal_vwap", 0)
    signal_volume = trade.get("signal_volume", 0)
    signal_macd = trade.get("signal_macd", 0)
    signal_funding = trade.get("signal_funding", 0)
    signal_oi = trade.get("signal_oi", 0)
    
    pnl = trade.get("pnl", 0.0)
    fee = trade.get("fee_paid", 0.0)
    net_pnl = pnl - fee
    win = 1 if net_pnl >= 0 else 0
    status = trade.get("status", "closed")
    strategy_name = trade.get("strategy", "unknown")
    market_regime = trade.get("market_phase", "RANGING")

    try:
        cursor.execute("""
        INSERT OR REPLACE INTO trades (
            id, timestamp, pair, direction, entry_price, exit_price, position_size, 
            pnl_usdt, pnl_pct, fee_paid, net_pnl, win, exit_reason,
            signal_ema, signal_rsi, signal_vwap, signal_volume, signal_macd, 
            signal_funding, signal_oi, market_phase, timeframe, atr_at_entry, 
            hour_of_day, day_of_week, mode, fee_type, strategy, status, sl_price, tp_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(trade.get("id")),
            timestamp,
            trade.get("symbol", config.SYMBOL),
            trade.get("direction"),
            trade.get("entry_price"),
            trade.get("exit_price"),
            trade.get("qty") * trade.get("entry_price") if trade.get("qty") and trade.get("entry_price") else 0.0,
            pnl,
            trade.get("pnl_pct", (pnl / (trade.get("qty") * trade.get("entry_price")) * 100) if trade.get("qty") and trade.get("entry_price") else 0.0),
            fee,
            net_pnl,
            win,
            trade.get("close_reason"),
            signal_ema,
            signal_rsi,
            signal_vwap,
            signal_volume,
            signal_macd,
            signal_funding,
            signal_oi,
            market_regime,
            trade.get("timeframe", "5m"),
            trade.get("atr_at_entry", 0.0),
            dt.hour,
            dt.weekday(),
            trade.get("mode", "live"),
            trade.get("fee_type", "TAKER"),
            strategy_name,
            status,
            trade.get("sl_price"),
            trade.get("tp_price")
        ))

        # Only update learning engine temporal weights & strategy stats if the trade is officially closed
        if status == "closed":
            # Update strategy_performance
            cursor.execute("""
                INSERT OR IGNORE INTO strategy_performance (strategy_name, market_regime, win_count, loss_count)
                VALUES (?, ?, 0, 0)
            """, (strategy_name, market_regime))
            
            if win == 1:
                cursor.execute("""
                    UPDATE strategy_performance 
                    SET win_count = win_count + 1 
                    WHERE strategy_name = ? AND market_regime = ?
                """, (strategy_name, market_regime))
            else:
                cursor.execute("""
                    UPDATE strategy_performance 
                    SET loss_count = loss_count + 1 
                    WHERE strategy_name = ? AND market_regime = ?
                """, (strategy_name, market_regime))

            # Update quick counts in weights table for signals present in this trade
            signals_mapping = {
                'ema': signal_ema,
                'rsi': signal_rsi,
                'vwap': signal_vwap,
                'volume': signal_volume,
                'macd': signal_macd,
                'funding': signal_funding,
                'oi': signal_oi
            }
            for name, present in signals_mapping.items():
                if present:
                    col = "win_count" if win == 1 else "loss_count"
                    cursor.execute(f"""
                    UPDATE signal_weights 
                    SET {col} = {col} + 1, last_updated = ? 
                    WHERE signal_name = ? AND hour_of_day = ?
                    """, (datetime.now(timezone.utc).isoformat(), name, dt.hour))

        conn.commit()
        push_log(f"Saved trade {trade.get('id')} ({status}) to DB (Net PnL: ${net_pnl:+.4f})")
    except Exception as e:
        push_log(f"DB Trade Save Error: {e}", "error")
    finally:
        conn.close()

def get_active_weights(hour: int = None) -> dict:
    """Returns a dict of {signal_name: weight} for the specified or current UTC hour."""
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT signal_name, weight FROM signal_weights WHERE hour_of_day = ?", (hour,))
    rows = cursor.fetchall()
    conn.close()
    
    # Fallback to 1.0 if not found
    weights = {row["signal_name"]: row["weight"] for row in rows}
    # Ensure all signals have a weight default
    for sig in ['ema', 'rsi', 'vwap', 'volume', 'macd', 'funding', 'oi']:
        if sig not in weights:
            weights[sig] = 1.0
    return weights

def update_signal_weights():
    """Tunes signal weights periodically based on recent 30-day win rate partitioned by hour of day."""
    conn = get_connection()
    cursor = conn.cursor()
    
    signals = ['ema', 'rsi', 'vwap', 'volume', 'macd', 'funding', 'oi']
    updated = {}

    try:
        for signal in signals:
            for hr in range(24):
                cursor.execute(f"""
                    SELECT 
                        SUM(win) as wins,
                        COUNT(*) as total
                    FROM trades 
                    WHERE signal_{signal} = 1 AND hour_of_day = ?
                    AND timestamp > datetime('now', '-30 days')
                """, (hr,))
                row = cursor.fetchone()
                wins = row["wins"]
                total = row["total"]

                if total and total >= 5:  # Need at least 5 samples per hour slot to adapt
                    win_rate = wins / total
                    new_weight = (win_rate - 0.4) * 5
                    new_weight = max(0.1, min(2.0, new_weight)) # clamp between 0.1 and 2.0
                    
                    cursor.execute("""
                        UPDATE signal_weights 
                        SET weight = ?, last_updated = ?
                        WHERE signal_name = ? AND hour_of_day = ?
                    """, (new_weight, datetime.now(timezone.utc).isoformat(), signal, hr))
                    updated[f"{signal}_{hr}"] = round(new_weight, 2)
        
        conn.commit()
        if updated:
            push_log(f"DB updated signal weights by hour of day: {updated}")
    except Exception as e:
        push_log(f"DB Weight Update Error: {e}", "error")
    finally:
        conn.close()


def save_snapshot(balance: float, mode: str = "live"):
    """Calculates metrics dynamically from trades and inserts account snapshot."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*), SUM(fee_paid), SUM(win) FROM trades WHERE mode = ?", (mode,))
        row = cursor.fetchone()
        total_trades = row[0] or 0
        total_fees = row[1] or 0.0
        wins = row[2] or 0
        win_rate = wins / total_trades if total_trades > 0 else 0.0
        
        # Calculate total PnL
        cursor.execute("SELECT SUM(net_pnl) FROM trades WHERE mode = ?", (mode,))
        row_pnl = cursor.fetchone()
        total_pnl = row_pnl[0] or 0.0
        
        cursor.execute("""
        INSERT INTO account_snapshots (timestamp, balance, total_pnl, win_rate, total_trades, total_fees_paid)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            balance,
            total_pnl,
            win_rate,
            total_trades,
            total_fees
        ))
        conn.commit()
    except Exception as e:
        push_log(f"DB Snapshot Save Error: {e}", "error")
    finally:
        conn.close()

def get_historical_peak_balance(mode: str = "live", default_val: float = 10.0) -> float:
    """Gets the historical peak balance from account snapshots."""
    conn = get_connection()
    cursor = conn.cursor()
    peak = default_val
    try:
        cursor.execute("SELECT MAX(balance) as peak FROM account_snapshots")
        row = cursor.fetchone()
        if row and row["peak"]:
            peak = max(peak, float(row["peak"]))
    except Exception:
        pass
    finally:
        conn.close()
    return peak


def get_closed_trades(limit: int = 50, mode: str = "live") -> list:
    conn = get_connection()
    cursor = conn.cursor()
    trades_list = []
    try:
        cursor.execute("""
        SELECT * FROM trades 
        WHERE mode = ?
        ORDER BY timestamp DESC 
        LIMIT ?
        """, (mode, limit))
        for row in cursor.fetchall():
            # Reconstruct dictionary matching dashboard expectations
            trades_list.append({
                "id": row["id"],
                "exit_time": row["timestamp"],
                "symbol": row["pair"],
                "direction": row["direction"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "qty": row["position_size"] / row["entry_price"] if row["entry_price"] else 0.0,
                "pnl": row["pnl_usdt"],
                "fee_paid": row["fee_paid"],
                "close_reason": row["exit_reason"]
            })
    except Exception as e:
        push_log(f"DB Load Trades Error: {e}", "error")
    finally:
        conn.close()
    return trades_list

def get_daily_fees_paid(mode: str = "live") -> float:
    """Calculates total fees paid in the last 24 hours from the database."""
    conn = get_connection()
    cursor = conn.cursor()
    total_fees = 0.0
    try:
        # Get fees in the last 24 hours (using SQLite datetime function)
        cursor.execute("""
            SELECT SUM(fee_paid) as total 
            FROM trades 
            WHERE mode = ? AND status = 'closed' AND timestamp > datetime('now', '-1 day')
        """, (mode,))
        row = cursor.fetchone()
        if row and row["total"]:
            total_fees = float(row["total"])
    except Exception as e:
        push_log(f"DB Daily Fees Fetch Error: {e}", "warning")
    finally:
        conn.close()
    return total_fees

def get_daily_gross_pnl(mode: str = "live") -> float:
    """Calculates total gross PnL in the last 24 hours (excluding fees) from the database."""
    conn = get_connection()
    cursor = conn.cursor()
    gross_pnl = 0.0
    try:
        cursor.execute("""
            SELECT SUM(pnl_usdt) as total 
            FROM trades 
            WHERE mode = ? AND status = 'closed' AND timestamp > datetime('now', '-1 day')
        """, (mode,))
        row = cursor.fetchone()
        if row and row["total"]:
            gross_pnl = float(row["total"])
    except Exception as e:
        push_log(f"DB Daily Gross PnL Fetch Error: {e}", "warning")
    finally:
        conn.close()
    return gross_pnl

def get_daily_trades_count(mode: str = "live") -> int:
    """Gets total trades closed in the last 24 hours."""
    conn = get_connection()
    cursor = conn.cursor()
    count = 0
    try:
        cursor.execute("""
            SELECT COUNT(*) as total 
            FROM trades 
            WHERE mode = ? AND status = 'closed' AND timestamp > datetime('now', '-1 day')
        """, (mode,))
        row = cursor.fetchone()
        if row:
            count = int(row["total"])
    except Exception:
        pass
    finally:
        conn.close()
    return count

def get_open_trade_db(mode: str = "paper") -> dict:
    """Loads the open trade of specific mode from the database if exists."""
    conn = get_connection()
    cursor = conn.cursor()
    trade = None
    try:
        cursor.execute("SELECT * FROM trades WHERE mode = ? AND status = 'open' LIMIT 1", (mode,))
        row = cursor.fetchone()
        if row:
            trade = {
                "id": row["id"],
                "symbol": row["pair"],
                "direction": row["direction"],
                "strategy": row["strategy"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "qty": row["position_size"] / row["entry_price"] if row["entry_price"] else 0.0,
                "sl_price": row["sl_price"] or (row["entry_price"] * 0.985 if row["direction"] == "LONG" else row["entry_price"] * 1.015),
                "tp_price": row["tp_price"] or (row["entry_price"] * 1.03 if row["direction"] == "LONG" else row["entry_price"] * 0.97),
                "entry_time": row["timestamp"],
                "status": row["status"],
                "peak_price": row["entry_price"],
                "pnl": row["pnl_usdt"],
                "fee_paid": row["fee_paid"],
                "atr_at_entry": row["atr_at_entry"],
                "market_phase": row["market_phase"],
                "timeframe": row["timeframe"]
            }
    except Exception as e:
        push_log(f"DB Load Open Trade Error: {e}", "warning")
    finally:
        conn.close()
    return trade

def get_strategy_performance() -> list:
    """Queries strategy performance and calculates win rate per strategy and market regime."""
    conn = get_connection()
    cursor = conn.cursor()
    performance = []
    try:
        cursor.execute("""
            SELECT strategy_name, market_regime, win_count, loss_count,
                   (win_count * 100.0 / (win_count + loss_count)) as win_rate
            FROM strategy_performance
            WHERE (win_count + loss_count) > 0
        """)
        for row in cursor.fetchall():
            performance.append({
                "strategy": row["strategy_name"],
                "regime": row["market_regime"],
                "wins": row["win_count"],
                "losses": row["loss_count"],
                "win_rate": round(row["win_rate"], 2)
            })
    except Exception as e:
        push_log(f"DB Strategy Performance Query Error: {e}", "warning")
    finally:
        conn.close()
    return performance


