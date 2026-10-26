from modules.state import state, state_lock, push_log
from modules.alerting import send_telegram
from datetime import datetime, timezone
import config

def check_milestones(current_balance: float):
    """
    Scans balance targets to determine compounding milestones dynamically.
    Issues payout messages and triggers scaling.
    """
    with state_lock:
        starting_bal = state["starting_balance"]
        peak = state["peak_balance"]
        
    # Read milestones dynamically and sort them ascending
    sorted_milestones = sorted(config.MILESTONE_CONFIG.keys())
    
    for target in sorted_milestones:
        if peak >= target and starting_bal < target:
            # We reached a new milestone!
            payout_pct = config.MILESTONE_CONFIG[target]
            payout_amount = target * payout_pct
            reinvest_amount = target - payout_amount

            with state_lock:
                state["starting_balance"] = reinvest_amount
                state["last_milestone_time"] = datetime.now(timezone.utc).isoformat()
            
            msg = (
                f"🎉 <b>MILESTONE REACHED!</b> 🎉\n"
                f"Bot successfully grew the account to <b>${target:.2f}</b>!\n"
                f"💰 Milestone payout: ${payout_amount:.2f} ({payout_pct*100:.0f}%)\n"
                f"🚀 Reinvesting: ${reinvest_amount:.2f} into next growth stage."
            )
            push_log(f"MILESTONE REACHED: Account hit target ${target:.2f}. Payout: ${payout_amount:.2f}", "info")
            send_telegram(msg)
            return
