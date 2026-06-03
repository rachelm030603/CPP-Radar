#!/usr/bin/env python3
"""
radar_server_flask.py

Simple test server for radar_data_collector.py.
This is for testing. For a real online deployment, run it behind HTTPS on a VPS,
Render, Railway, Fly.io, or another server platform.

Install:
    pip install flask

Run:
    python radar_server_flask.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request

app = Flask(__name__)
DATA_FILE = Path("received_radar_data.jsonl")


@app.route("/api/radar", methods=["POST"])
def receive_radar():
    payload: Dict[str, Any] = request.get_json(force=True, silent=True) or {}
    record = {
        "received_at": time.time(),
        "payload": payload,
    }
    with DATA_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    print(f"Received frame={payload.get('frame_count')} targets={payload.get('target_count')}", flush=True)
    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
