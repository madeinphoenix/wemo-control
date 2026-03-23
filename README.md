# Wemo Control

A zero-dependency local web app for controlling Wemo light switches on your network. Built because the official Wemo app stopped working, but the switches still respond to local SOAP/UPnP commands.

## Requirements

- Python 3 (no pip installs needed)
- Wemo devices on the same local network

## Usage

```bash
python3 server.py
```

Then open from any device on your network:
- **Local:** http://localhost:8080
- **Network:** http://YOUR_MAC_IP:8080 (e.g., http://192.168.0.186:8080)

## Features

- Auto-discovers Wemo devices via SSDP
- Toggle switches on/off
- All On / All Off controls
- Auto-refreshes state every 10 seconds
- Mobile-friendly dark UI
- Zero dependencies — uses only Python standard library

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/devices` | Discover all Wemo devices |
| GET | `/api/device/<ip>/state` | Get device state |
| POST | `/api/device/<ip>/toggle` | Toggle device |
| POST | `/api/device/<ip>/state` | Set state (`{"state": 0}` or `{"state": 1}`) |
