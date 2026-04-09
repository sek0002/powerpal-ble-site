from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse


load_dotenv()

APP_TITLE = os.getenv("APP_TITLE", "Powerpal BLE Site")
STATE_FILE = Path(os.getenv("BLE_STATE_FILE", "data/latest_ble.json"))


def _default_state() -> dict[str, Any]:
    return {
        "grid_usage_watts": None,
        "battery_percent": None,
        "observed_at": None,
        "state": "starting",
        "last_error": None,
        "last_success_at": None,
        "resolved_address": None,
        "configured_batch_minutes": None,
        "device_batch_minutes": None,
    }


def _load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return _default_state()
    try:
        return {**_default_state(), **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except Exception:
        state = _default_state()
        state["state"] = "error"
        state["last_error"] = f"Unable to read state file {STATE_FILE}"
        return state


def _text_payload(state: dict[str, Any]) -> str:
    def fmt(value: object) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value)

    return "\n".join(
        [
            fmt(state.get("grid_usage_watts")),
            fmt(state.get("battery_percent")),
            fmt(state.get("observed_at")),
            fmt(state.get("state")),
        ]
    )


app = FastAPI(title=APP_TITLE)


@app.get("/", response_class=PlainTextResponse)
async def root() -> PlainTextResponse:
    return PlainTextResponse(_text_payload(_load_state()))


@app.get("/html", response_class=HTMLResponse)
async def html_page() -> HTMLResponse:
    state = _load_state()
    body_text = _text_payload(state)
    now = datetime.now(timezone.utc).isoformat()
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(APP_TITLE)}</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #e5e7eb;
    }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      padding: 20px;
      border-radius: 18px;
      background: rgba(17, 24, 39, 0.92);
      border: 1px solid rgba(148, 163, 184, 0.18);
    }}
    h1 {{ margin-top: 0; }}
    .meta {{ color: #94a3b8; margin-bottom: 18px; }}
    pre {{
      padding: 16px;
      border-radius: 14px;
      background: rgba(2, 6, 23, 0.88);
      color: #e5e7eb;
      overflow: auto;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(APP_TITLE)}</h1>
    <div class="meta">Simple BLE text page for remote scraping. Updated {html.escape(now)}.</div>
    <pre>{html.escape(body_text)}</pre>
  </main>
</body>
</html>"""
    )


@app.get("/api/status")
async def api_status() -> dict[str, object]:
    return _load_state()
