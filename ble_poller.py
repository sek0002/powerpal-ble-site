from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytz
from bleak import BleakClient, BleakError
from dotenv import load_dotenv


load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger(__name__)

TIMEZONE_NAME = os.getenv("TIMEZONE", "Australia/Melbourne")
BLE_MAC = os.getenv("BLE_MAC", "C9:91:09:7A:2C:B9")
BLE_PAIRING_CODE = os.getenv("BLE_PAIRING_CODE", "774034")
BLE_READING_BATCH_SIZE_MINUTES = int(os.getenv("BLE_READING_BATCH_SIZE_MINUTES", "1"))
BLE_CONNECTION_TIMEOUT_SECONDS = float(os.getenv("BLE_CONNECTION_TIMEOUT_SECONDS", "30"))
BLE_RETRY_DELAY_SECONDS = float(os.getenv("BLE_RETRY_DELAY_SECONDS", "5"))
BLE_STATE_FILE = Path(os.getenv("BLE_STATE_FILE", "data/latest_ble.json"))

PAIRING_CODE_CHAR = "59da0011-12f4-25a6-7d4f-55961dce4205"
POWERPAL_FREQ_CHAR = "59da0013-12f4-25a6-7d4f-55961dce4205"
NOTIFY_CHAR = "59da0001-12f4-25a6-7d4f-55961dce4205"
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"


def convert_pairing_code(original_pairing_code: str) -> bytes:
    return int(original_pairing_code).to_bytes(4, byteorder="little")


def _parse_notification(data: bytearray, melbourne_tz: pytz.BaseTzInfo) -> dict[str, Any]:
    if len(data) < 6:
        raise ValueError(f"Expected at least 6 BLE bytes, received {len(data)}")
    timestamp = struct.unpack_from("<I", data, 0)[0]
    int_array = list(data)
    pulse_sum = int_array[4] + int_array[5]
    usage_watts = pulse_sum / 0.8
    utc_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return {
        "grid_usage_watts": usage_watts,
        "observed_at": utc_time.astimezone(melbourne_tz).isoformat(),
        "raw_bytes_hex": data.hex(),
        "pulse_byte_4": int_array[4],
        "pulse_byte_5": int_array[5],
        "pulse_sum": pulse_sum,
        "original_test2_formula": "grid_usage_watts = (byte4 + byte5) / 0.8",
    }


def _write_state(payload: dict[str, Any]) -> None:
    BLE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = BLE_STATE_FILE.with_suffix(BLE_STATE_FILE.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(BLE_STATE_FILE)


class PowerpalBleWorker:
    def __init__(self) -> None:
        self._stopped = asyncio.Event()
        self._melbourne_tz = pytz.timezone(TIMEZONE_NAME)
        self._state: dict[str, Any] = {
            "grid_usage_watts": None,
            "battery_percent": None,
            "observed_at": None,
            "state": "starting",
            "last_error": None,
            "last_success_at": None,
            "resolved_address": BLE_MAC,
            "configured_batch_minutes": BLE_READING_BATCH_SIZE_MINUTES,
            "device_batch_minutes": None,
        }

    def _persist(self) -> None:
        _write_state(self._state)

    async def run(self) -> None:
        self._persist()
        while not self._stopped.is_set():
            try:
                self._state["state"] = "connecting"
                self._state["last_error"] = None
                self._persist()
                await self._run_session()
                if not self._stopped.is_set():
                    self._state["state"] = "disconnected"
                    self._state["last_error"] = "BLE disconnected"
                    self._persist()
                    await asyncio.sleep(BLE_RETRY_DELAY_SECONDS)
            except BleakError as exc:
                LOGGER.warning("BLE error: %s", exc)
                self._state["state"] = "error"
                self._state["last_error"] = str(exc)
                self._persist()
                await asyncio.sleep(BLE_RETRY_DELAY_SECONDS)
            except Exception as exc:
                LOGGER.exception("Unexpected BLE failure")
                self._state["state"] = "error"
                self._state["last_error"] = str(exc)
                self._persist()
                await asyncio.sleep(BLE_RETRY_DELAY_SECONDS)

    async def stop(self) -> None:
        self._stopped.set()

    async def _run_session(self) -> None:
        batch_size_bytes = int(BLE_READING_BATCH_SIZE_MINUTES).to_bytes(4, byteorder="little")

        def notification_handler(_: Any, data: bytearray) -> None:
            payload = _parse_notification(bytearray(data), self._melbourne_tz)
            self._state["grid_usage_watts"] = payload["grid_usage_watts"]
            self._state["observed_at"] = payload["observed_at"]
            self._state["state"] = "connected"
            self._state["last_error"] = None
            self._state["last_success_at"] = datetime.now(timezone.utc).isoformat()
            self._persist()

        client = BleakClient(BLE_MAC)
        await client.connect(timeout=BLE_CONNECTION_TIMEOUT_SECONDS)
        try:
            _ = client.services
            self._state["resolved_address"] = BLE_MAC
            self._state["configured_batch_minutes"] = BLE_READING_BATCH_SIZE_MINUTES
            self._persist()

            await client.write_gatt_char(
                PAIRING_CODE_CHAR,
                convert_pairing_code(BLE_PAIRING_CODE),
                response=True,
            )
            await client.write_gatt_char(
                POWERPAL_FREQ_CHAR,
                batch_size_bytes,
                response=True,
            )
            self._state["device_batch_minutes"] = BLE_READING_BATCH_SIZE_MINUTES

            notify_data = await client.read_gatt_char(NOTIFY_CHAR)
            LOGGER.debug("Initial notify characteristic read: %s", notify_data)

            await client.start_notify(NOTIFY_CHAR, notification_handler)

            try:
                battery_value = await client.read_gatt_char(BATTERY_CHAR)
                if battery_value:
                    self._state["battery_percent"] = int(battery_value[0])
                    self._persist()
            except Exception as exc:
                LOGGER.debug("Unable to read Powerpal battery level", exc_info=exc)

            self._state["state"] = "connected"
            self._persist()
            while not self._stopped.is_set():
                await asyncio.sleep(1.0)
        finally:
            try:
                try:
                    await client.stop_notify(NOTIFY_CHAR)
                except Exception:
                    LOGGER.debug("Unable to stop bleak notifications cleanly", exc_info=True)
                await client.disconnect()
            except Exception:
                LOGGER.debug("Unable to disconnect bleak client cleanly", exc_info=True)


async def _main() -> None:
    worker = PowerpalBleWorker()
    try:
        await worker.run()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
