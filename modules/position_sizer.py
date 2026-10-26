"""Position sizing for the 20-symbol futures scanner.

The user-selected model allocates 10% of current account equity as isolated
margin, then applies the configured 10x leverage to calculate ORDER NOTIONAL.
For a 100 USDT account: 10 USDT margin and 100 USDT notional.

Stop loss and take profit remain ATR-based in the original strategy. The margin
allocation is NOT the percentage of equity at risk: actual losses depend on the
stop distance, slippage, and liquidation mechanics.
"""
import math
import config
from modules.state import push_log


def calculate_position_size(
    account_balance: float,
    atr: float,
    entry_price: float,
    win_rate: float = 0.5,
    avg_win: float = 0.03,
    avg_loss: float = 0.015,
) -> float:
    """Return position notional in USDT; compounds automatically with equity.

    Signature intentionally retains the original signal/volatility arguments so
    all existing call sites and the unchanged signal engine remain compatible.
    'win_rate' and Kelly assumptions no longer override the requested fixed
    10% margin allocation. Binance's quantity/step constraints are applied by
    the live order manager when the order quantity is rounded.
    """
    if not all(math.isfinite(float(x)) for x in (account_balance, atr, entry_price)):
        return 0.0
    if account_balance <= 0 or atr <= 0 or entry_price <= 0:
        return 0.0

    margin_fraction = config.POSITION_MARGIN_FRACTION
    leverage = config.LEVERAGE
    if not 0 < margin_fraction <= 1 or not 1 <= leverage <= 125:
        raise ValueError("Invalid isolated-margin fraction or futures leverage.")

    allocated_margin_usdt = float(account_balance) * margin_fraction
    position_notional_usdt = allocated_margin_usdt * leverage

    # Binance minimum order notional; avoid placing orders smaller than 5 USDT.
    if position_notional_usdt < 5.0:
        push_log(
            f"Position skipped: notional {position_notional_usdt:.4f} USDT "
            f"(margin {allocated_margin_usdt:.4f}, leverage {leverage}x) is below 5 USDT.",
            "warning",
        )
        return 0.0
    return position_notional_usdt
