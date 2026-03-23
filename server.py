#!/usr/bin/env python3
"""Wemo Control Server - Zero-dependency local web server for controlling Wemo switches."""

import json
import os
import re
import socket
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

PORT = 8080
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# SOAP templates for Wemo control
SOAP_GET_STATE = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:GetBinaryState xmlns:u="urn:Belkin:service:basicevent:1"/>
</s:Body>
</s:Envelope>"""

SOAP_SET_STATE = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:SetBinaryState xmlns:u="urn:Belkin:service:basicevent:1">
<BinaryState>{state}</BinaryState>
</u:SetBinaryState>
</s:Body>
</s:Envelope>"""

# Cache discovered devices to avoid repeated scans
_device_cache = {}
_cache_lock = threading.Lock()


def ssdp_discover(timeout=5):
    """Discover Wemo devices on the network via SSDP."""
    targets = [
        "urn:Belkin:device:controllee:1",
        "urn:Belkin:device:lightswitch:1",
        "urn:Belkin:device:insight:1",
        "urn:Belkin:device:dimmer:1",
        "urn:Belkin:device:bridge:1",
        "upnp:rootdevice",
    ]
    found = {}
    for st in targets:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {timeout}\r\n"
            f"ST: {st}\r\n\r\n"
        ).encode()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.sendto(msg, ("239.255.255.250", 1900))
            while True:
                data, addr = sock.recvfrom(4096)
                text = data.decode("utf-8", errors="ignore")
                if "belkin" not in text.lower():
                    continue
                ip = addr[0]
                location = ""
                for line in text.split("\r\n"):
                    if line.upper().startswith("LOCATION:"):
                        location = line.split(":", 1)[1].strip()
                if ip not in found and location:
                    found[ip] = location
        except socket.timeout:
            pass
        finally:
            sock.close()
    return found


def fetch_device_info(ip, location):
    """Fetch device name and model from its setup.xml."""
    try:
        req = Request(location, headers={"User-Agent": "WemoControl/1.0"})
        with urlopen(req, timeout=3) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
        name = re.search(r"<friendlyName>(.+?)</friendlyName>", xml)
        model = re.search(r"<modelName>(.+?)</modelName>", xml)
        device_type = re.search(r"<deviceType>(.+?)</deviceType>", xml)
        serial = re.search(r"<serialNumber>(.+?)</serialNumber>", xml)
        firmware = re.search(r"<firmwareVersion>(.+?)</firmwareVersion>", xml)
        # Extract port from location URL
        parsed = urlparse(location)
        port = parsed.port or 49153
        return {
            "ip": ip,
            "port": port,
            "name": name.group(1) if name else ip,
            "model": model.group(1) if model else "Unknown",
            "type": device_type.group(1) if device_type else "Unknown",
            "serial": serial.group(1) if serial else "Unknown",
            "firmware": firmware.group(1) if firmware else "Unknown",
        }
    except Exception as e:
        return {"ip": ip, "port": 49153, "name": ip, "model": "Unknown",
                "type": "Unknown", "serial": "Unknown", "firmware": "Unknown",
                "error": str(e)}


def get_wemo_state(ip, port=49153):
    """Get the current binary state of a Wemo device."""
    url = f"http://{ip}:{port}/upnp/control/basicevent1"
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": '"urn:Belkin:service:basicevent:1#GetBinaryState"',
    }
    req = Request(url, data=SOAP_GET_STATE.encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        match = re.search(r"<BinaryState>(\d+)</BinaryState>", body)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


def set_wemo_state(ip, state, port=49153):
    """Set the binary state of a Wemo device (0=off, 1=on)."""
    url = f"http://{ip}:{port}/upnp/control/basicevent1"
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": '"urn:Belkin:service:basicevent:1#SetBinaryState"',
    }
    body = SOAP_SET_STATE.format(state=state)
    req = Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            result = resp.read().decode("utf-8", errors="ignore")
        match = re.search(r"<BinaryState>(\d+)</BinaryState>", result)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


class WemoHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for Wemo control API and static files."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/api/devices":
            self._handle_discover()
        elif self.path.startswith("/api/device/") and self.path.endswith("/state"):
            self._handle_get_state()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/device/") and self.path.endswith("/toggle"):
            self._handle_toggle()
        elif self.path.startswith("/api/device/") and self.path.endswith("/state"):
            self._handle_set_state()
        else:
            self._send_json({"error": "Not found"}, 404)

    def _extract_ip(self):
        """Extract IP from path like /api/device/192.168.0.19/state."""
        parts = self.path.split("/")
        if len(parts) >= 4:
            return parts[3]
        return None

    def _get_device_port(self, ip):
        """Get the port for a device from cache."""
        with _cache_lock:
            device = _device_cache.get(ip)
            if device:
                return device.get("port", 49153)
        return 49153

    def _handle_discover(self):
        """Discover Wemo devices and return their info + state."""
        found = ssdp_discover(timeout=3)
        devices = []
        for ip, location in found.items():
            info = fetch_device_info(ip, location)
            state = get_wemo_state(ip, info.get("port", 49153))
            info["state"] = state
            devices.append(info)
            with _cache_lock:
                _device_cache[ip] = info
        # Also check cached devices that might not have responded to this scan
        with _cache_lock:
            for ip, cached in _device_cache.items():
                if ip not in found:
                    state = get_wemo_state(ip, cached.get("port", 49153))
                    if state is not None:
                        cached["state"] = state
                        devices.append(cached)
        devices.sort(key=lambda d: d.get("name", ""))
        self._send_json({"devices": devices})

    def _handle_get_state(self):
        ip = self._extract_ip()
        if not ip:
            self._send_json({"error": "Missing IP"}, 400)
            return
        port = self._get_device_port(ip)
        state = get_wemo_state(ip, port)
        if state is not None:
            self._send_json({"ip": ip, "state": state})
        else:
            self._send_json({"error": "Device not responding"}, 503)

    def _handle_toggle(self):
        ip = self._extract_ip()
        if not ip:
            self._send_json({"error": "Missing IP"}, 400)
            return
        port = self._get_device_port(ip)
        current = get_wemo_state(ip, port)
        if current is None:
            self._send_json({"error": "Device not responding"}, 503)
            return
        new_state = 0 if current else 1
        result = set_wemo_state(ip, new_state, port)
        if result is not None:
            self._send_json({"ip": ip, "state": result})
        else:
            self._send_json({"error": "Failed to set state"}, 500)

    def _handle_set_state(self):
        ip = self._extract_ip()
        if not ip:
            self._send_json({"error": "Missing IP"}, 400)
            return
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode() if content_len else "{}"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        state = data.get("state")
        if state not in (0, 1):
            self._send_json({"error": "state must be 0 or 1"}, 400)
            return
        port = self._get_device_port(ip)
        result = set_wemo_state(ip, state, port)
        if result is not None:
            self._send_json({"ip": ip, "state": result})
        else:
            self._send_json({"error": "Failed to set state"}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Quieter logging - only log API calls
        if "/api/" in (args[0] if args else ""):
            super().log_message(format, *args)


def main():
    server = HTTPServer(("0.0.0.0", PORT), WemoHandler)
    # Get local IP for display
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"
    print(f"Wemo Control Server running on:")
    print(f"  Local:   http://localhost:{PORT}")
    print(f"  Network: http://{local_ip}:{PORT}")
    print(f"\nAccess from any device on your network using the Network URL.")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
