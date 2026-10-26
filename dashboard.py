"""
============================================================
  TRADING BOT DASHBOARD — Flask Server
  Open http://localhost:5000 in your browser
============================================================
"""

from flask import Flask, jsonify, render_template, request
from modules.state import state, state_lock
from modules.learning_engine import get_closed_trades, get_daily_fees_paid, get_daily_gross_pnl
import main
import config

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    mode = request.args.get("mode", "test")
    db_mode = "live" if mode == "live" else "paper"
    closed = get_closed_trades(50, db_mode)
    
    # Calculate daily statistics dynamically
    daily_fees = get_daily_fees_paid(db_mode)
    daily_gross = get_daily_gross_pnl(db_mode)
    fee_pct = (daily_fees / daily_gross * 100.0) if daily_gross > 0.0 else 0.0

    with state_lock:
        state["daily_fees_paid"] = daily_fees
        state["daily_gross_pnl"] = daily_gross
        state["fee_pct_of_profit"] = fee_pct
        
        data = dict(state)
        # Swap test paper stats if selected mode is test
        if mode == "test":
            data["balance"] = state["paper_balance"]
            data["starting_balance"] = state["paper_starting_balance"]
            data["peak_balance"] = state["paper_peak_balance"]
            data["open_trade"] = state["paper_open_trade"]
            data["closed_trades"] = closed[:20]
            data["total_pnl"] = state["paper_total_pnl"]
            data["win_count"] = state["paper_win_count"]
            data["loss_count"] = state["paper_loss_count"]
            data["drawdown_pct"] = state["paper_drawdown_pct"]
            data["progress_pct"] = state["paper_progress_pct"]
        else:
            data["closed_trades"] = closed[:20]
    return jsonify(data)


@app.route("/api/start", methods=["POST"])
def api_start():
    req_data = request.json or {}
    mode = req_data.get("mode", "test")
    main.start_bot(mode)
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    main.stop_bot()
    return jsonify({"ok": True})


@app.route("/api/close_trade", methods=["POST"])
def api_close():
    main.manual_close()
    return jsonify({"ok": True})


@app.route("/api/logs")
def api_logs():
    with state_lock:
        return jsonify(state["logs"][:100])


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=config.DASHBOARD_PORT,
        debug=False,
        use_reloader=False
    )
