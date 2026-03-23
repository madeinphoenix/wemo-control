#!/usr/bin/env python3
"""Wemo Control Server - Zero-dependency local web server for controlling Wemo switches and dimmers."""

import concurrent.futures
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

SOAP_SET_BRIGHTNESS = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:SetBinaryState xmlns:u="urn:Belkin:service:basicevent:1">
<BinaryState>{state}</BinaryState>
<brightness>{brightness}</brightness>
</u:SetBinaryState>
</s:Body>
</s:Envelope>"""

# Cache discovered devices to avoid repeated scans
_device_cache = {}
_cache_lock = threading.Lock()


def _check_wemo_port(ip, port):
    """Check if a Wemo device is listening on ip:port."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex((ip, port))
        sock.close()
        if result == 0:
            return (ip, port)
    except Exception:
        pass
    return None


def ssdp_discover(timeout=3):
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


def port_scan_discover(exclude_ips=None):
    """Find Wemo devices via port scanning (catches dimmers that don't respond to SSDP)."""
    exclude_ips = exclude_ips or set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        return {}

    subnet = ".".join(local_ip.split(".")[:3])
    targets = []
    for i in range(1, 255):
        ip = f"{subnet}.{i}"
        if ip == local_ip or ip in exclude_ips:
            continue
        for port in [49152, 49153]:
            targets.append((ip, port))

    found = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=150) as ex:
        futures = {ex.submit(_check_wemo_port, ip, port): (ip, port) for ip, port in targets}
        for f in concurrent.futures.as_completed(futures):
            result = f.result()
            if result:
                ip, port = result
                if ip in found:
                    continue
                try:
                    req = Request(f"http://{ip}:{port}/setup.xml",
                                  headers={"User-Agent": "WemoControl/1.0"})
                    with urlopen(req, timeout=2) as resp:
                        xml = resp.read().decode("utf-8", errors="ignore")
                    if "belkin" in xml.lower():
                        found[ip] = f"http://{ip}:{port}/setup.xml"
                except Exception:
                    pass
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
        parsed = urlparse(location)
        port = parsed.port or 49153
        is_dimmer = "dimmer" in (device_type.group(1) if device_type else "").lower()
        return {
            "ip": ip,
            "port": port,
            "name": name.group(1) if name else ip,
            "model": model.group(1) if model else "Unknown",
            "type": device_type.group(1) if device_type else "Unknown",
            "serial": serial.group(1) if serial else "Unknown",
            "firmware": firmware.group(1) if firmware else "Unknown",
            "isDimmer": is_dimmer,
        }
    except Exception as e:
        return {"ip": ip, "port": 49153, "name": ip, "model": "Unknown",
                "type": "Unknown", "serial": "Unknown", "firmware": "Unknown",
                "isDimmer": False, "error": str(e)}


def get_wemo_state(ip, port=49153):
    """Get the current state (and brightness for dimmers) of a Wemo device."""
    url = f"http://{ip}:{port}/upnp/control/basicevent1"
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": '"urn:Belkin:service:basicevent:1#GetBinaryState"',
    }
    req = Request(url, data=SOAP_GET_STATE.encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
        state_match = re.search(r"<BinaryState>(\d+)</BinaryState>", body)
        brightness_match = re.search(r"<brightness>(\d+)</brightness>", body)
        result = {}
        if state_match:
            result["state"] = int(state_match.group(1))
        if brightness_match:
            result["brightness"] = int(brightness_match.group(1))
        return result if result else None
    except Exception:
        pass
    return None


def set_wemo_state(ip, state, port=49153, brightness=None):
    """Set the state (and optionally brightness) of a Wemo device."""
    url = f"http://{ip}:{port}/upnp/control/basicevent1"
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPACTION": '"urn:Belkin:service:basicevent:1#SetBinaryState"',
    }
    if brightness is not None:
        body = SOAP_SET_BRIGHTNESS.format(state=state, brightness=brightness)
    else:
        body = SOAP_SET_STATE.format(state=state)
    req = Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=5) as resp:
            result = resp.read().decode("utf-8", errors="ignore")
        state_match = re.search(r"<BinaryState>(\d+)</BinaryState>", result)
        brightness_match = re.search(r"<brightness>(\d+)</brightness>", result)
        out = {}
        if state_match:
            out["state"] = int(state_match.group(1))
        if brightness_match:
            out["brightness"] = int(brightness_match.group(1))
        return out if out else None
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
        elif self.path.startswith("/api/device/") and self.path.endswith("/brightness"):
            self._handle_set_brightness()
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
        """Discover Wemo devices via SSDP + cached devices, with background port scan."""
        ssdp_found = ssdp_discover(timeout=3)

        # Build results from SSDP + cache
        all_found = dict(ssdp_found)
        with _cache_lock:
            for ip, cached in _device_cache.items():
                if ip not in all_found:
                    port = cached.get("port", 49153)
                    all_found[ip] = f"http://{ip}:{port}/setup.xml"

        devices = []
        for ip, location in all_found.items():
            # Use cached info if available, otherwise fetch
            with _cache_lock:
                cached = _device_cache.get(ip)
            if cached and ip not in ssdp_found:
                info = dict(cached)
            else:
                info = fetch_device_info(ip, location)
            state_info = get_wemo_state(ip, info.get("port", 49153))
            if state_info:
                info["state"] = state_info.get("state")
                if "brightness" in state_info:
                    info["brightness"] = state_info["brightness"]
                    info["isDimmer"] = True
            else:
                info["state"] = None
            devices.append(info)
            with _cache_lock:
                _device_cache[ip] = info

        devices.sort(key=lambda d: d.get("name", ""))
        self._send_json({"devices": devices})

        # Run port scan in background to find devices SSDP missed (e.g. dimmers)
        def _bg_scan():
            with _cache_lock:
                known = set(_device_cache.keys())
            scan_found = port_scan_discover(exclude_ips=known)
            for ip, location in scan_found.items():
                info = fetch_device_info(ip, location)
                state_info = get_wemo_state(ip, info.get("port", 49153))
                if state_info:
                    info["state"] = state_info.get("state")
                    if "brightness" in state_info:
                        info["brightness"] = state_info["brightness"]
                        info["isDimmer"] = True
                with _cache_lock:
                    _device_cache[ip] = info

        threading.Thread(target=_bg_scan, daemon=True).start()

    def _handle_get_state(self):
        ip = self._extract_ip()
        if not ip:
            self._send_json({"error": "Missing IP"}, 400)
            return
        port = self._get_device_port(ip)
        state_info = get_wemo_state(ip, port)
        if state_info:
            self._send_json({"ip": ip, **state_info})
        else:
            self._send_json({"error": "Device not responding"}, 503)

    def _handle_toggle(self):
        ip = self._extract_ip()
        if not ip:
            self._send_json({"error": "Missing IP"}, 400)
            return
        port = self._get_device_port(ip)
        state_info = get_wemo_state(ip, port)
        if not state_info:
            self._send_json({"error": "Device not responding"}, 503)
            return
        current = state_info.get("state", 0)
        new_state = 0 if current else 1
        result = set_wemo_state(ip, new_state, port)
        if result:
            self._send_json({"ip": ip, **result})
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
        brightness = data.get("brightness")
        result = set_wemo_state(ip, state, port, brightness=brightness)
        if result:
            self._send_json({"ip": ip, **result})
        else:
            self._send_json({"error": "Failed to set state"}, 500)

    def _handle_set_brightness(self):
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
        brightness = data.get("brightness")
        if brightness is None or not (0 <= brightness <= 100):
            self._send_json({"error": "brightness must be 0-100"}, 400)
            return
        port = self._get_device_port(ip)
        # If brightness > 0, ensure device is on
        state = 1 if brightness > 0 else 0
        result = set_wemo_state(ip, state, port, brightness=brightness)
        if result:
            self._send_json({"ip": ip, **result})
        else:
            self._send_json({"error": "Failed to set brightness"}, 500)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(format, *args)


def _startup_scan():
    """Run initial discovery at startup to pre-populate cache."""
    print("Running startup device scan...")
    ssdp_found = ssdp_discover(timeout=3)
    for ip, location in ssdp_found.items():
        info = fetch_device_info(ip, location)
        state_info = get_wemo_state(ip, info.get("port", 49153))
        if state_info:
            info["state"] = state_info.get("state")
            if "brightness" in state_info:
                info["brightness"] = state_info["brightness"]
                info["isDimmer"] = True
        with _cache_lock:
            _device_cache[ip] = info
    # Port scan for devices SSDP missed
    scan_found = port_scan_discover(exclude_ips=set(ssdp_found.keys()))
    for ip, location in scan_found.items():
        info = fetch_device_info(ip, location)
        state_info = get_wemo_state(ip, info.get("port", 49153))
        if state_info:
            info["state"] = state_info.get("state")
            if "brightness" in state_info:
                info["brightness"] = state_info["brightness"]
                info["isDimmer"] = True
        with _cache_lock:
            _device_cache[ip] = info
    with _cache_lock:
        count = len(_device_cache)
    print(f"Found {count} device(s) on startup.")


def main():
    server = HTTPServer(("0.0.0.0", PORT), WemoHandler)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    # Pre-populate cache in background so first page load is fast
    threading.Thread(target=_startup_scan, daemon=True).start()

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
