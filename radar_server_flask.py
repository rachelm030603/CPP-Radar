#!/usr/bin/env python3
"""
radar_server_flask.py

Public radar server:
  - POST /api/radar   -> ingest a frame from the Pi collector, store in Postgres
  - GET  /             -> live-view webpage (auto-refreshing top-down plot)
  - GET  /api/latest   -> JSON: most recent frame + its targets
  - GET  /health        -> health check

Storage: Postgres (works with any provider, e.g. Neon's free tier). Local
SQLite was dropped because Render's free web services have an ephemeral
filesystem -- a local DB file is wiped on every redeploy/restart/spin-down.
Postgres on Neon persists independently of the web service.

Install:
    pip install flask psycopg2-binary gunicorn

Configure:
    export DATABASE_URL="postgresql://user:pass@host/dbname?sslmode=require"

Run locally:
    python radar_server_flask.py

Run in production (e.g. on Render), via gunicorn:
    gunicorn radar_server_flask:app --bind 0.0.0.0:$PORT
"""

from __future__ import annotations

import os
import time
from contextlib import closing
from typing import Any, Dict

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Example:\n"
        "  export DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require"
    )


def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    with closing(get_db()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS frames (
                id SERIAL PRIMARY KEY,
                device_id TEXT NOT NULL,
                timestamp_unix DOUBLE PRECISION NOT NULL,      -- when the Pi sent it
                radar_timestamp_unix DOUBLE PRECISION,          -- radar hardware clock
                received_at DOUBLE PRECISION NOT NULL,          -- when the server got it
                frame_count INTEGER,
                frame_seq INTEGER,
                target_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS targets (
                id SERIAL PRIMARY KEY,
                frame_id INTEGER NOT NULL REFERENCES frames(id) ON DELETE CASCADE,
                range_m DOUBLE PRECISION,
                velocity_mps DOUBLE PRECISION,
                azimuth_deg DOUBLE PRECISION,
                elevation_deg DOUBLE PRECISION,
                x_m DOUBLE PRECISION,
                y_m DOUBLE PRECISION,
                z_m DOUBLE PRECISION
            );

            CREATE INDEX IF NOT EXISTS idx_frames_received_at ON frames(received_at);
            CREATE INDEX IF NOT EXISTS idx_frames_device_id ON frames(device_id);
            CREATE INDEX IF NOT EXISTS idx_targets_frame_id ON targets(frame_id);
            """
        )
        conn.commit()


@app.route("/api/radar", methods=["POST"])
def receive_radar():
    payload: Dict[str, Any] = request.get_json(force=True, silent=True) or {}

    device_id = payload.get("device_id", "unknown")
    targets = payload.get("targets", []) or []
    received_at = time.time()

    with closing(get_db()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO frames
                (device_id, timestamp_unix, radar_timestamp_unix, received_at,
                 frame_count, frame_seq, target_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                device_id,
                payload.get("timestamp_unix", received_at),
                payload.get("radar_timestamp_unix"),
                received_at,
                payload.get("frame_count"),
                payload.get("frame_seq"),
                len(targets),
            ),
        )
        frame_id = cur.fetchone()[0]

        if targets:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO targets
                    (frame_id, range_m, velocity_mps, azimuth_deg, elevation_deg, x_m, y_m, z_m)
                VALUES %s
                """,
                [
                    (
                        frame_id,
                        t.get("range_m"),
                        t.get("velocity_mps"),
                        t.get("azimuth_deg"),
                        t.get("elevation_deg"),
                        t.get("x_m"),
                        t.get("y_m"),
                        t.get("z_m"),
                    )
                    for t in targets
                ],
            )
        conn.commit()

    print(
        f"Received frame={payload.get('frame_count')} targets={len(targets)} "
        f"(frame_id={frame_id})",
        flush=True,
    )
    return jsonify({"status": "ok", "frame_id": frame_id})


@app.route("/api/latest", methods=["GET"])
def latest_frame():
    """Most recent frame + its targets, for the live-view page to poll."""
    with closing(get_db()) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM frames ORDER BY received_at DESC LIMIT 1")
        frame = cur.fetchone()
        if frame is None:
            return jsonify({"frame": None, "targets": []})

        cur.execute("SELECT * FROM targets WHERE frame_id = %s", (frame["id"],))
        targets = cur.fetchall()

    return jsonify({"frame": dict(frame), "targets": [dict(t) for t in targets]})


LIVE_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Live Radar</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0b0f14; color: #e6edf3; margin: 0; padding: 1.5rem; }
    h1 { font-size: 1.1rem; font-weight: 600; color: #9fb3c8; margin: 0 0 1rem; }
    #wrap { display: flex; gap: 2rem; flex-wrap: wrap; }
    canvas { background: #10161d; border: 1px solid #22303c; border-radius: 8px; }
    table { border-collapse: collapse; font-size: 0.85rem; }
    th, td { padding: 0.3rem 0.7rem; text-align: right; border-bottom: 1px solid #22303c; }
    th { color: #9fb3c8; font-weight: 600; }
    #meta { color: #6b7f91; font-size: 0.8rem; margin-top: 0.5rem; }
  </style>
</head>
<body>
  <h1>Live Radar Feed</h1>
  <div id="wrap">
    <canvas id="plot" width="420" height="420"></canvas>
    <table id="targets">
      <thead><tr><th>Range (m)</th><th>Velocity (m/s)</th><th>Azimuth (deg)</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div id="meta">waiting for data...</div>

<script>
const MAX_RANGE_M = 10; // adjust to your radar's max useful range
const canvas = document.getElementById('plot');
const ctx = canvas.getContext('2d');
const cx = canvas.width / 2, cy = canvas.height;
const scale = canvas.height / MAX_RANGE_M;

function drawGrid() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = '#22303c';
  for (let r = 2; r <= MAX_RANGE_M; r += 2) {
    ctx.beginPath();
    ctx.arc(cx, cy, r * scale, Math.PI, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(cx, 0); ctx.lineTo(cx, cy);
  ctx.stroke();
}

function draw(targets) {
  drawGrid();
  ctx.fillStyle = '#4fd1c5';
  for (const t of targets) {
    const px = cx + (t.x_m || 0) * scale;
    const py = cy - (t.y_m || 0) * scale;
    ctx.beginPath();
    ctx.arc(px, py, 5, 0, 2 * Math.PI);
    ctx.fill();
  }
}

async function poll() {
  try {
    const res = await fetch('/api/latest');
    const data = await res.json();
    if (!data.frame) return;
    draw(data.targets);

    const tbody = document.querySelector('#targets tbody');
    tbody.innerHTML = '';
    for (const t of data.targets) {
      const row = document.createElement('tr');
      row.innerHTML = `<td>${(t.range_m ?? 0).toFixed(2)}</td><td>${(t.velocity_mps ?? 0).toFixed(2)}</td><td>${(t.azimuth_deg ?? 0).toFixed(1)}</td>`;
      tbody.appendChild(row);
    }

    const age = (Date.now() / 1000 - data.frame.received_at).toFixed(1);
    document.getElementById('meta').textContent =
      `device: ${data.frame.device_id} | frame #${data.frame.frame_count} | ${data.targets.length} targets | ${age}s ago`;
  } catch (e) {
    document.getElementById('meta').textContent = 'connection error';
  }
}

drawGrid();
setInterval(poll, 500);
poll();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def live_page():
    return render_template_string(LIVE_PAGE)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    # gunicorn imports this module without running __main__, so make sure
    # the tables exist before the first request either way.
    init_db()
