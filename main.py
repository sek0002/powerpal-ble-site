from __future__ import annotations

import asyncio
import os
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pytz
from bleak import BleakClient, BleakError, BleakScanner
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse


load_dotenv()

APP_TITLE = os.getenv("APP_TITLE", "Powerpal BLE Site")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Australia/Melbourne")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8002"))
BLE_MAC = os.getenv("BLE_MAC", "C9:91:09:7A:2C:B9")
BLE_PAIRING_CODE = os.getenv("BLE_PAIRING_CODE", "774034")
BLE_CONNECTION_TIMEOUT_SECONDS = float(os.getenv("BLE_CONNECTION_TIMEOUT_SECONDS", "30"))
BLE_RETRY_DELAY_SECONDS = float(os.getenv("BLE_RETRY_DELAY_SECONDS", "5"))

PAIRING_CODE_CHAR = "59da0011-12f4-25a6-7d4f-55961dce4205"
POWERPAL_FREQ_CHAR = "59da0013-12f4-25a6-7d4f-55961dce4205"
NOTIFY_CHAR = "59da0001-12f4-25a6-7d4f-55961dce4205"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"


@dataclass
class LatestBleState:
    grid_usage_watts: Optional[float] = None
    battery_percent: Optional[int] = None
    observed_at: Optional[str] = None
    state: str = "starting"
    last_error: Optional[str] = None
    last_success_at: Optional[str] = None
    resolved_address: Optional[str] = None
    resolved_name: Optional[str] = None


class PowerpalBleSitePoller:
    def __init__(self) -> None:
        self._stopped = asyncio.Event()
        self._melbourne_tz = pytz.timezone(TIMEZONE_NAME)
        self.latest = LatestBleState()

    @staticmethod
    def convert_pairing_code(original_pairing_code: str) -> bytes:
        return int(original_pairing_code).to_bytes(4, byteorder="little")

    async def _resolve_device(self) -> Any:
        devices = await BleakScanner.discover(timeout=BLE_CONNECTION_TIMEOUT_SECONDS, return_adv=True)
        exact_match = None
        name_match = None
        for _, (device, _) in devices.items():
            if (device.address or "").lower() == BLE_MAC.lower():
                exact_match = device
                break
            if "powerpal" in (device.name or "").lower() and name_match is None:
                name_match = device
        if exact_match is not None:
            return exact_match
        if name_match is not None:
            return name_match
        raise BleakError(f"Could not find Powerpal device during scan for {BLE_MAC}")

    def _parse_notification(self, data: bytearray) -> dict[str, Any]:
        if len(data) < 6:
            raise ValueError(f"Expected at least 6 BLE bytes, received {len(data)}")
        timestamp = struct.unpack_from("<I", data, 0)[0]
        int_array = list(data)
        pulse_sum = int_array[4] + int_array[5]
        usage_watts = pulse_sum / 0.8
        utc_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return {
            "grid_usage_watts": usage_watts,
            "observed_at": utc_time.astimezone(self._melbourne_tz).isoformat(),
            "pulse_sum": pulse_sum,
        }

    async def run(self) -> None:
        self.latest.state = "starting"
        while not self._stopped.is_set():
            try:
                self.latest.state = "connecting"
                await self._run_session()
                if not self._stopped.is_set():
                    self.latest.state = "disconnected"
                    self.latest.last_error = "BLE disconnected"
                    await asyncio.sleep(BLE_RETRY_DELAY_SECONDS)
            except Exception as exc:
                self.latest.state = "error"
                self.latest.last_error = str(exc)
                await asyncio.sleep(BLE_RETRY_DELAY_SECONDS)

    async def stop(self) -> None:
        self._stopped.set()

    async def _run_session(self) -> None:
        resolved_device = await self._resolve_device()

        def notification_handler(_: Any, data: bytearray) -> None:
            payload = self._parse_notification(bytearray(data))
            self.latest.grid_usage_watts = payload["grid_usage_watts"]
            self.latest.observed_at = payload["observed_at"]
            self.latest.state = "connected"
            self.latest.last_error = None
            self.latest.last_success_at = datetime.now(timezone.utc).isoformat()

        async with BleakClient(resolved_device, timeout=BLE_CONNECTION_TIMEOUT_SECONDS) as client:
            self.latest.resolved_address = getattr(resolved_device, "address", BLE_MAC)
            self.latest.resolved_name = getattr(resolved_device, "name", None)

            try:
                await client.pair()
            except Exception:
                pass

            await client.write_gatt_char(
                PAIRING_CODE_CHAR,
                self.convert_pairing_code(BLE_PAIRING_CODE),
                response=False,
            )
            await asyncio.sleep(2.0)
            await client.write_gatt_char(
                POWERPAL_FREQ_CHAR,
                int(1).to_bytes(4, byteorder="little"),
                response=False,
            )
            try:
                battery_value = await client.read_gatt_char(BATTERY_CHAR)
                if battery_value:
                    self.latest.battery_percent = int(battery_value[0])
            except Exception:
                pass

            await client.start_notify(NOTIFY_CHAR, notification_handler)
            self.latest.state = "connected"
            try:
                while not self._stopped.is_set():
                    await asyncio.sleep(1.0)
            finally:
                try:
                    await client.stop_notify(NOTIFY_CHAR)
                except Exception:
                    pass


poller = PowerpalBleSitePoller()


def _text_payload() -> str:
    latest = poller.latest

    def fmt(value: object) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value)

    return "\n".join(
        [
            fmt(latest.grid_usage_watts),
            fmt(latest.battery_percent),
            fmt(latest.observed_at),
            fmt(latest.state),
        ]
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(poller.run(), name="powerpal-ble-site-poller")
    try:
        yield
    finally:
        await poller.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title=APP_TITLE, lifespan=lifespan)


@app.get("/", response_class=PlainTextResponse)
async def root() -> PlainTextResponse:
    return PlainTextResponse(_text_payload())


@app.get("/html", response_class=HTMLResponse)
async def html_page() -> HTMLResponse:
    latest = poller.latest
    body_text = _text_payload()
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{APP_TITLE}</title>
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
    <h1>{APP_TITLE}</h1>
    <div class="meta">State: {latest.state} | Battery: {latest.battery_percent if latest.battery_percent is not None else "-"}%</div>
    <pre>{body_text}</pre>
  </main>
</body>
</html>"""
    )


@app.get("/api/status")
async def api_status() -> dict[str, object]:
    latest = poller.latest
    return {
        "grid_usage_watts": latest.grid_usage_watts,
        "battery_percent": latest.battery_percent,
        "observed_at": latest.observed_at,
        "state": latest.state,
        "last_error": latest.last_error,
        "last_success_at": latest.last_success_at,
        "resolved_address": latest.resolved_address,
        "resolved_name": latest.resolved_name,
    }
