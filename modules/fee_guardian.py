from modules.state import push_log
import config

def passes_fee_gate(atr: float, entry_price: float, position_size_usdt: float) -> bool:
    """
    Prevents trading if the expected volatility move (1.5x ATR) is too small
    to cover standard exchange transaction fees (config.FEE_RATE round-trip) with profit.
    """
    if entry_price <= 0 or position_size_usdt <= 0:
        return False

    # 1.5x ATR as our target expected move (for Stop Loss distance)
    expected_move = atr * 1.5
    
    # round trip commission fees on position size from config
    fee_cost = position_size_usdt * config.FEE_RATE
    
    # Rule: Target move in USD must be at least 3x the round-trip fee cost in USD
    qty = position_size_usdt / entry_price
    expected_move_usd = expected_move * qty
    
    minimum_required_profit = fee_cost * 3.0

    if expected_move_usd < minimum_required_profit:
        push_log(
            f"FEE GATE BLOCK: Expected Move = ${expected_move_usd:.4f}, Fee Cost = ${fee_cost:.4f}, returning False",
            "info"
        )
        return False
        
    return True

