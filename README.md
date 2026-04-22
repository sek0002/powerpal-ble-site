# Powerpal BLE Site

A minimal standalone app that:

- runs a standalone Powerpal BLE poller process
- stores the latest reading on disk
- exposes it as a simple text page for another app to scrape

## Output format

`GET /` returns four plain-text lines:

1. latest BLE grid usage watts
2. battery percent
3. observed timestamp
4. BLE state

There is also:

- `GET /html` for a simple human-readable page
- `GET /api/status` for JSON status

## Configure

Copy `.env.example` to `.env` and set at least:

- `BLE_MAC`
- `BLE_PAIRING_CODE`

Optional recovery settings:

- `BLE_SYSTEMCTL_RESTART_THRESHOLD=100`
- `BLE_SYSTEMCTL_RESTART_COMMAND="systemctl restart bluetooth"`
- `BLE_SYSTEMCTL_RESTART_COOLDOWN_SECONDS=30`

When the poller records 100 consecutive disconnect/error cycles without a successful
notification, it will run the configured bluetooth restart command, wait for the
cooldown, and then restart the poller process itself.

## Run locally

```bash
python3 ble_poller.py
```

In another terminal:

```bash
python3 -m uvicorn main:app --host 0.0.0.0 --port 8002
```

Then open:

- [http://localhost:8002/](http://localhost:8002/)
- [http://localhost:8002/html](http://localhost:8002/html)

## Raspberry Pi service

```bash
sudo apt update
sudo apt install -y python3 python3-pip bluetooth bluez
cd /opt
sudo git clone <repo-url> powerpal-ble-site
sudo chown -R "$USER":"$USER" /opt/powerpal-ble-site
cd /opt/powerpal-ble-site
cp .env.example .env
sudo cp deploy/powerpal-ble-poller.service /etc/systemd/system/powerpal-ble-poller.service
sudo cp deploy/powerpal-ble-site.service /etc/systemd/system/powerpal-ble-site.service
sudo systemctl daemon-reload
sudo systemctl enable powerpal-ble-poller
sudo systemctl enable powerpal-ble-site
sudo systemctl start powerpal-ble-poller
sudo systemctl start powerpal-ble-site
```
