import threading
from datetime import datetime, timezone
import logging
import os
import sys
import config

state = {
    "status":                 "stopped",       # running / stopped / error
    "balance":                config.STARTING_BALANCE,
    "starting_balance":       config.STARTING_BALANCE,
    "peak_balance":           config.STARTING_BALANCE,
    "target":                 config.TARGET_BALANCE,
    "open_trade":             None,            # dict or None
    "closed_trades":          [],              # list of trade dicts
    "total_pnl":              0.0,
    "win_count":              0,
    "loss_count":             0,
    "last_signal":            "—",
    "last_strategy":          "—",
    "last_scan":              "—",
    "errors":                 [],
    "logs":                   [],
    "progress_pct":           0.0,
    "daily_pnl":              0.0,
    "drawdown_pct":           0.0,
    "market_regime":          "RANGING",       # BULL_TREND / BEAR_TREND / HIGH_VOLATILITY / RANGING
    "circuit_breaker_paused": False,
    "circuit_breaker_until":  None,
    "scheduled_maintenance":  [],              # List of ISO timestamps for upcoming maintenance
    "websocket_status":       "disconnected",
    "telegram_status":        "inactive",
    "environment":            "TESTNET" if config.TESTNET else "LIVE",
    "trading_mode":           "test",          # "live" or "test"
    "paper_balance":          config.STARTING_BALANCE,
    "paper_starting_balance": config.STARTING_BALANCE,
    "paper_peak_balance":     config.STARTING_BALANCE,
    "paper_open_trade":       None,
    "paper_closed_trades":    [],
    "paper_total_pnl":        0.0,
    "paper_win_count":        0,
    "paper_loss_count":       0,
    "paper_drawdown_pct":     0.0,
    "paper_progress_pct":     0.0,
    "active_signals":         {},
}

state_lock = threading.Lock()

# ── Console/Terminal UTF-8 Reconfiguration ────────────────────
try:
    if sys.stdout:
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if sys.stderr:
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
except AttributeError:
    pass

# Ensure logs dir exists
os.makedirs("logs", exist_ok=True)

# Configure logger
from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join("logs", "bot.log"), encoding="utf-8", maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("TradingBot")

def push_log(msg: str, level: str = "info"):
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M:%S"), "msg": msg, "level": level}
    with state_lock:
        state["logs"].insert(0, entry)
        state["logs"] = state["logs"][:200]
    # Map 'warning' level to standard logging
    log_func = getattr(log, level if level != "warning" else "warning")
    try:
        log_func(msg)
    except Exception:
        # Fallback if any write still fails
        pass
