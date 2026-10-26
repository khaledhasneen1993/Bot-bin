import os
from dotenv import load_dotenv

# Load environmental configs
load_dotenv()

# ============================================================
#  BINANCE FUTURES TRADING BOT — CONFIG
#  Fill in your API keys in the .env file.
# ============================================================

# ── Binance API ──────────────────────────────────────────────
API_KEY             = os.getenv("BINANCE_API_KEY", "")
API_SECRET          = os.getenv("BINANCE_API_SECRET", "")
TESTNET             = os.getenv("USE_TESTNET", "false").lower() == "true"

# ── Telegram Alerts ──────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Capital Goal ─────────────────────────────────────────────
STARTING_BALANCE    = float(os.getenv("INITIAL_CAPITAL", "10.0"))
TARGET_BALANCE      = STARTING_BALANCE * 10.0  # Challenge target is always 10x starting capital
DAYS_TO_TARGET      = 30      # Used only for progress display

# ── Trading Settings ─────────────────────────────────────────
SYMBOL            = "BTCUSDT"       # Main trading pair
LEVERAGE          = 20              # Leverage (1–125). FULL leverage = very high risk.
RISK_PER_TRADE    = 0.95            # Use 95% of available balance per trade (aggressive)
MAX_OPEN_POSITIONS  = 3               # Maximum simultaneous open positions allowed (3 max for $10 account)
FEE_RATE            = 0.0008          # Effective round-trip transaction fee percentage (standard 0.08%, or 0.03% if BNB is enabled)
WEEKLY_LOSS_LIMIT   = 0.20            # 20% weekly drawdown limit
MILESTONE_CONFIG    = {
    STARTING_BALANCE * 2.5: 0.0,    # 2.5x milestone: 0% payout (reinvest 100%)
    STARTING_BALANCE * 5.0: 0.10,   # 5.0x milestone: 10% payout
    STARTING_BALANCE * 7.5: 0.10,   # 7.5x milestone: 10% payout
    STARTING_BALANCE * 10.0: 0.15,  # 10.0x milestone: 15% payout (Challenge Complete)
}

CONFLUENCE_THRESHOLD = 4.0          # Minimum confluence score required to trade
FEE_THROTTLE_LIMIT   = 0.02         # Throttling trading when daily fees exceed 2% of account balance


# ── Stop Loss / Take Profit ──────────────────────────────────
STOP_LOSS_PCT     = 0.015           # 1.5% SL from entry (tighter = safer)
TAKE_PROFIT_PCT   = 0.03            # 3% TP from entry (2:1 RR ratio)
TRAILING_STOP     = True            # Enable trailing stop loss
TRAILING_DELTA    = 0.008           # Trail by 0.8% from peak

# ── Strategy Settings ────────────────────────────────────────
ACTIVE_STRATEGIES = [
    "rsi_macd",         # RSI + MACD momentum
    "ema_crossover",    # EMA 9/21 crossover
    "scalping",         # Fast 1m/5m scalping
    "breakout",         # Support/Resistance breakout
]
STRATEGY_TIMEFRAMES = {
    "rsi_macd":      "5m",
    "ema_crossover": "15m",
    "scalping":      "1m",
    "breakout":      "15m",
}

# RSI
RSI_PERIOD        = 14
RSI_OVERSOLD      = 35
RSI_OVERBOUGHT    = 65

# MACD
MACD_FAST         = 12
MACD_SLOW         = 26
MACD_SIGNAL       = 9

# EMA Crossover
EMA_FAST          = 9
EMA_SLOW          = 21

# Scalping
SCALP_EMA_FAST    = 5
SCALP_EMA_SLOW    = 13
SCALP_RSI_LOW     = 40
SCALP_RSI_HIGH    = 60

# Breakout
BREAKOUT_PERIODS  = 20             # Look back N candles for S/R
BREAKOUT_CONFIRM  = 0.002          # 0.2% beyond level to confirm break

# ── Bot Loop ─────────────────────────────────────────────────
LOOP_INTERVAL     = 10             # Seconds between each strategy scan
LOG_FILE          = "bot_log.json" # Trade log file
DASHBOARD_PORT    = 5000           # Dashboard runs at http://localhost:5000
DB_FILE           = os.path.join("data", "trades.db")
ACTIVE_PAIRS      = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


