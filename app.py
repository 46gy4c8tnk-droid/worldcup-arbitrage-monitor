"""World Cup cross-bookmaker arbitrage monitor - entry point.

Run:  python app.py
Open: http://127.0.0.1:8788
"""
from flask import Flask, jsonify, request, send_from_directory

from engine import Engine

app = Flask(__name__, static_folder="static")
engine = Engine()


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/state")
def state():
    return jsonify(engine.snapshot())


@app.post("/api/config")
def update_config():
    return jsonify(engine.update_config(request.get_json(force=True)))


@app.post("/api/poll")
def poll_now():
    engine.force_poll()
    return jsonify({"ok": True})


@app.post("/api/reset")
def reset():
    engine.reset_account("manual")
    return jsonify({"ok": True})


@app.post("/api/manual_bet")
def manual_bet():
    data = request.get_json(force=True)
    return jsonify(engine.record_manual_bet(
        data.get("event_id"), data.get("total_stake", 0)))


if __name__ == "__main__":
    import os
    engine.start()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8788)), debug=False)
