#!/usr/bin/env python3
"""
DJI Romo - Video Stream & Robot Control

Standalone script for live video + joystick/gamepad control of DJI Romo robot vacuum.
Controls: keyboard (ZQSD/arrows), on-screen buttons, PS5 DualSense or Xbox controller (WebHID).

Usage:
    python3 dji_video_control.py

Requires:
    - .env file with DJI_USER_TOKEN and DJI_DEVICE_SN
    - Agora Python SDK (pip install agora-python-sdk)
    - requests (pip install requests)
"""

import json
import os
import sys
import time
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from functools import partial

import requests

try:
    from agora.rtc.agora_service import AgoraService, AgoraServiceConfig, RTCConnConfig, RtcConnectionPublishConfig
    from agora.rtc.rtc_connection import RTCConnection
    from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
    from agora.rtc.local_user_observer import IRTCLocalUserObserver
    AGORA_SDK_AVAILABLE = True
except ImportError:
    AGORA_SDK_AVAILABLE = False
    print("Error: Agora Python SDK required. Install with: pip install agora-python-sdk")
    sys.exit(1)


class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    END = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'


def load_env():
    env_file = Path(__file__).parent / ".env"
    config = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip()
    return {
        "user_token": config.get("DJI_USER_TOKEN", ""),
        "user_id": config.get("DJI_USER_ID", ""),
        "device_sn": config.get("DJI_DEVICE_SN", ""),
        "api_base_url": config.get("DJI_API_URL", "https://home-api-vg.djigate.com"),
        "locale": config.get("DJI_LOCALE", "en_US"),
    }


class DJIVideoController:
    """Lightweight controller for DJI Romo video stream + joystick/gamepad control."""

    # Mode mapping discovered via Agora DataStream sniffer capture
    AGORA_MODES = {
        "forward": 17,
        "rotate_left": 18,
        "rotate_right": 19,
        "u_turn": 16,
    }

    def __init__(self):
        self.config = load_env()

        # Agora DataStream state
        self.agora_service = None
        self.agora_connection = None
        self.agora_connected = False
        self.agora_stream_ready = False
        self.agora_robot_joined = False
        self.agora_seq_id = 0
        self.agora_send_thread = None
        self.agora_running = False
        self.agora_current_mode = None  # None = not sending

        self.http_server = None

        if not self.config["user_token"]:
            print(f"{Colors.RED}Erreur: Fichier .env non trouvé ou DJI_USER_TOKEN manquant!{Colors.END}")
            print("Lancez d'abord: python3 dji_credentials_extractor.py")
            sys.exit(1)

        if not self.config["device_sn"]:
            print(f"{Colors.RED}Erreur: DJI_DEVICE_SN manquant dans .env!{Colors.END}")
            sys.exit(1)

        self.headers = {
            "x-member-token": self.config["user_token"],
            "X-DJI-locale": self.config["locale"],
            "Content-Type": "application/json",
            "User-Agent": "DJI-Home/1.5.13",
        }

    # ==================== API ====================

    def api_get(self, endpoint):
        """Make GET request to API."""
        url = f"{self.config['api_base_url']}{endpoint}"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            data = response.json()
            if data.get("result", {}).get("code") == 0:
                return data.get("data")
            return None
        except:
            return None

    def api_post(self, endpoint, body=None):
        """Make POST request to API."""
        url = f"{self.config['api_base_url']}{endpoint}"
        try:
            response = requests.post(url, headers=self.headers, json=body or {}, timeout=30)
            data = response.json()
            return data
        except Exception as e:
            return {"error": str(e)}

    # ==================== AGORA DATASTREAM ====================

    def _parse_stream_creds(self, data):
        """Parse Agora credentials from openStream/start API response data."""
        url_str = data.get("url", "")
        params = {}
        for part in url_str.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key] = urllib.parse.unquote(value)
        return {
            "app_id": params.get("app_id", ""),
            "channel": params.get("channel", ""),
            "token": params.get("token", ""),
            "uid": int(params.get("uid", "0")),
            "publish_uid": data.get("publish_uid", 50000),
        }

    def connect_agora(self, creds=None, enter_mode=True):
        """Connect to Agora and set up reliable DataStream for robot control."""
        if self.agora_connected:
            print(f"{Colors.YELLOW}[Agora] Already connected{Colors.END}")
            return True

        if creds is None:
            device_sn = self.config["device_sn"]
            print(f"{Colors.CYAN}[Agora] Getting stream credentials...{Colors.END}")
            result = self.api_post(f"/cr/app/api/v1/devices/{device_sn}/live/openStream/start")
            if result.get("result", {}).get("code") != 0:
                print(f"{Colors.RED}[Agora] Failed to get stream credentials{Colors.END}")
                return False

            data = result.get("data", {})
            creds = self._parse_stream_creds(data)

            debug_file = Path(__file__).parent / "agora_debug.json"
            debug_file.write_text(json.dumps(data, indent=2))

        print(f"{Colors.CYAN}[Agora] Connecting with UID {creds['uid']}{Colors.END}")
        print(f"{Colors.CYAN}[Agora] Channel: {creds['channel']} | UID: {creds['uid']}{Colors.END}")

        controller = self

        class ConnObserver(IRTCConnectionObserver):
            def on_connected(self, conn, info, reason):
                print(f"{Colors.GREEN}[Agora] Connected!{Colors.END}")
                controller.agora_connected = True
                try:
                    new_stream_id = conn._create_data_stream(True, True)
                    if new_stream_id is not None and new_stream_id >= 0:
                        conn._data_stream_id = new_stream_id
                        controller.agora_stream_ready = True
                        print(f"{Colors.GREEN}[Agora] Reliable stream created (ID={new_stream_id}){Colors.END}")
                    else:
                        controller.agora_stream_ready = True
                        print(f"{Colors.YELLOW}[Agora] Using default stream{Colors.END}")
                except Exception as e:
                    controller.agora_stream_ready = True
                    print(f"{Colors.YELLOW}[Agora] Stream setup warning: {e}{Colors.END}")

            def on_disconnected(self, c, i, r):
                controller.agora_connected = False
                controller.agora_stream_ready = False
            def on_connecting(self, c, i, r): pass
            def on_user_joined(self, c, uid):
                if str(uid) == str(creds['publish_uid']):
                    print(f"{Colors.GREEN}[Agora] Robot joined (UID: {uid}){Colors.END}")
                    controller.agora_robot_joined = True
            def on_user_left(self, c, uid, r):
                if str(uid) == str(creds['publish_uid']):
                    controller.agora_robot_joined = False
            def on_stream_message_error(self, c, u, s, e, m, ca):
                if e != 0:
                    print(f"{Colors.RED}[Agora] Stream error: {e}{Colors.END}")
            def on_connection_failure(self, c, i, r): pass
            def on_reconnecting(self, c, i, r): pass
            def on_reconnected(self, c, i, r):
                controller.agora_connected = True
            def on_connection_lost(self, c, i): pass

        class StreamObserver(IRTCLocalUserObserver):
            def on_stream_message(self, local_user, user_id, stream_id, data, length):
                try:
                    text = data.decode('utf-8') if isinstance(data, bytes) else str(data)
                    print(f"{Colors.BLUE}[Agora] <<< UID:{user_id} : {text[:80]}{Colors.END}")
                except Exception:
                    pass

        # Initialize Agora
        print(f"{Colors.CYAN}[Agora] Initializing...{Colors.END}")
        agora_config = AgoraServiceConfig()
        agora_config.app_id = creds["app_id"]
        agora_config.log_path = "/tmp/agora_controller.log"

        self.agora_service = AgoraService()
        if self.agora_service.initialize(agora_config) != 0:
            print(f"{Colors.RED}[Agora] Init failed{Colors.END}")
            return False

        con_config = RTCConnConfig()
        con_config.auto_subscribe_audio = 1
        con_config.auto_subscribe_video = 1
        con_config.client_role_type = 1   # BROADCASTER
        con_config.channel_profile = 1    # LIVE_BROADCASTING

        pub_config = RtcConnectionPublishConfig()
        pub_config.is_publish_audio = 0
        pub_config.is_publish_video = 0

        self.agora_connection = RTCConnection(self.agora_service, con_config, pub_config)
        self.agora_connection.register_observer(ConnObserver())
        self.agora_connection.register_local_user_observer(StreamObserver())

        if self.agora_connection.connect(creds["token"], creds["channel"], str(creds["uid"])) != 0:
            print(f"{Colors.RED}[Agora] Connect failed{Colors.END}")
            return False

        # Wait for connection + stream
        for _ in range(100):
            if self.agora_connected and self.agora_stream_ready:
                break
            time.sleep(0.1)

        if not self.agora_connected:
            print(f"{Colors.RED}[Agora] Connection timeout{Colors.END}")
            return False

        # Wait for robot
        print(f"{Colors.CYAN}[Agora] Waiting for robot...{Colors.END}")
        for _ in range(50):
            if self.agora_robot_joined:
                break
            time.sleep(0.1)

        if not self.agora_robot_joined:
            print(f"{Colors.YELLOW}[Agora] Robot not in channel yet{Colors.END}")

        # Enter control mode if requested
        if enter_mode:
            print(f"{Colors.CYAN}[Agora] Entering control mode (enterModeB)...{Colors.END}")
            self.enter_remote_control_mode()

        # Start continuous send thread
        self.agora_running = True
        self.agora_send_thread = threading.Thread(target=self._agora_send_loop, daemon=True)
        self.agora_send_thread.start()

        print(f"{Colors.GREEN}[Agora] Ready for control!{Colors.END}")
        return True

    def _agora_send_loop(self):
        """Send control messages at 10Hz when a mode is active."""
        while self.agora_running:
            mode = self.agora_current_mode
            if self.agora_connected and self.agora_stream_ready and mode is not None:
                self._send_agora_message_now(mode)
            time.sleep(0.1)  # 10 Hz

    def _send_agora_message_now(self, mode):
        """Send a single Agora DataStream message immediately."""
        if not self.agora_connection or not self.agora_stream_ready:
            return
        msg = json.dumps({
            "seq_id": self.agora_seq_id,
            "timestamp": int(time.time() * 1000),
            "mode": mode,
            "version": 2,
            "x": 1.0,
            "y": 0.0,
        }, separators=(',', ':'))
        self.agora_connection.send_stream_message(msg.encode('utf-8'))
        self.agora_seq_id += 1

    def send_agora_control(self, direction):
        """Set the current control direction via Agora DataStream."""
        if direction == "stop":
            self.agora_current_mode = None
            return True

        mode = self.AGORA_MODES.get(direction)
        if mode is None:
            print(f"{Colors.RED}[Agora] Unknown direction: {direction}{Colors.END}")
            return False

        self.agora_current_mode = mode
        self._send_agora_message_now(mode)
        return True

    def disconnect_agora(self):
        """Disconnect from Agora."""
        self.agora_current_mode = None
        self.agora_running = False
        if self.agora_send_thread:
            self.agora_send_thread.join(timeout=2)
        self.exit_remote_control_mode()
        if self.agora_connection:
            self.agora_connection.disconnect()
            self.agora_connection.release()
            self.agora_connection = None
        if self.agora_service:
            self.agora_service.release()
            self.agora_service = None
        self.agora_connected = False
        self.agora_stream_ready = False

    # ==================== API COMMANDS ====================

    def go_home(self):
        """Send robot back to dock."""
        device_sn = self.config["device_sn"]
        return self.api_post(f"/cr/app/api/v1/devices/{device_sn}/jobs/goHomes/start")

    def stop_live_stream(self):
        """Stop live camera stream."""
        device_sn = self.config["device_sn"]
        return self.api_post(f"/cr/app/api/v1/devices/{device_sn}/live/stop")

    def enter_remote_control_mode(self):
        """Enter remote control mode - must be called before sending movement commands."""
        device_sn = self.config["device_sn"]
        endpoints = [
            (f"/cr/app/api/v1/devices/{device_sn}/live/activationCode/enterModeB", {}),
            (f"/cr/app/api/v1/devices/{device_sn}/live/activationCode/enterMode", {"mode": "control"}),
            (f"/cr/app/api/v1/devices/{device_sn}/rc/enter", {}),
        ]

        for endpoint, body in endpoints:
            result = self.api_post(endpoint, body)
            if result.get("result", {}).get("code") == 0:
                print(f"{Colors.GREEN}Mode controle active via {endpoint}{Colors.END}")
                return result

        return {"error": "Could not enter control mode"}

    def exit_remote_control_mode(self):
        """Exit remote control mode."""
        device_sn = self.config["device_sn"]
        return self.api_post(f"/cr/app/api/v1/devices/{device_sn}/live/activationCode/exitMode")

    # ==================== HTTP SERVER ====================

    def start_control_server(self, port=8765):
        """Start HTTP server for receiving joystick commands from web viewer."""
        if self.http_server:
            return

        handler = partial(ControlAPIHandler, self)
        try:
            self.http_server = HTTPServer(('127.0.0.1', port), handler)
            self.http_server.allow_reuse_address = True
            self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
            self.http_thread.start()
            print(f"{Colors.DIM}Control API server started on http://localhost:{port}{Colors.END}")
        except OSError as e:
            if "Address already in use" in str(e):
                print(f"{Colors.YELLOW}Control server already running on port {port}{Colors.END}")
            else:
                print(f"{Colors.RED}Failed to start control server: {e}{Colors.END}")

    def stop_control_server(self):
        """Stop the control HTTP server."""
        if self.http_server:
            self.http_server.shutdown()

    # ==================== VIDEO VIEWER ====================

    def _create_video_viewer(self, params):
        """Create a video viewer HTML file with embedded Agora parameters."""
        app_id = params.get('app_id', '')
        channel = params.get('channel', '')
        token = params.get('token', '')
        sn = params.get('sn', '')
        uid = params.get('uid', '0')

        print(f"{Colors.DIM}App ID: {app_id}{Colors.END}")
        print(f"{Colors.DIM}Channel: {channel}{Colors.END}")
        print(f"{Colors.DIM}UID: {uid}{Colors.END}")
        print(f"{Colors.DIM}Token length: {len(token)}{Colors.END}")

        token_json = json.dumps(token) if token else '""'

        html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DJI Romo - Live Camera + Joystick</title>
    <script src="https://download.agora.io/sdk/release/AgoraRTC_N-4.20.0.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: #1a1a2e;
            color: #eee;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}
        .header {{
            background: #16213e;
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .header h1 {{ font-size: 1.2rem; color: #4ecca3; }}
        .status {{ display: flex; align-items: center; gap: 10px; }}
        .status-dot {{
            width: 10px; height: 10px;
            border-radius: 50%;
            background: #ff6b6b;
        }}
        .status-dot.connected {{
            background: #4ecca3;
            animation: pulse 2s infinite;
        }}
        @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
        .main-content {{
            flex: 1;
            display: flex;
            padding: 20px;
            gap: 20px;
            background: #0f0f1a;
        }}
        .video-container {{
            flex: 1;
            display: flex;
            justify-content: center;
            align-items: center;
        }}
        #remote-video {{
            width: 100%;
            max-width: 960px;
            aspect-ratio: 16/9;
            background: #000;
            border-radius: 8px;
            overflow: hidden;
        }}
        #remote-video video {{ width: 100%; height: 100%; object-fit: contain; }}
        .joystick-panel {{
            width: 280px;
            background: #16213e;
            border-radius: 8px;
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 15px;
        }}
        .joystick-panel h3 {{ color: #4ecca3; margin-bottom: 5px; }}
        .joystick-grid {{
            display: grid;
            grid-template-columns: repeat(3, 60px);
            grid-template-rows: repeat(3, 60px);
            gap: 5px;
        }}
        .joy-btn {{
            width: 60px;
            height: 60px;
            border: none;
            border-radius: 8px;
            background: #2a2a4a;
            color: #fff;
            font-size: 24px;
            cursor: pointer;
            transition: all 0.1s;
            display: flex;
            align-items: center;
            justify-content: center;
            user-select: none;
        }}
        .joy-btn:hover {{ background: #3a3a5a; }}
        .joy-btn:active, .joy-btn.active {{ background: #4ecca3; color: #1a1a2e; }}
        .joy-btn.stop {{ background: #ff6b6b; font-size: 14px; font-weight: bold; }}
        .joy-btn.stop:hover {{ background: #ee5a5a; }}
        .joy-btn.empty {{ background: transparent; cursor: default; }}
        .controls {{
            background: #16213e;
            padding: 15px 20px;
            display: flex;
            justify-content: center;
            gap: 15px;
        }}
        .btn {{
            padding: 10px 25px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1rem;
            transition: all 0.2s;
        }}
        .btn-primary {{ background: #4ecca3; color: #1a1a2e; }}
        .btn-danger {{ background: #ff6b6b; color: white; }}
        .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .info {{
            background: #16213e;
            padding: 10px 20px;
            font-size: 0.85rem;
            color: #888;
        }}
        .error {{
            background: #ff6b6b22;
            color: #ff6b6b;
            padding: 15px;
            margin: 10px;
            border-radius: 5px;
            display: none;
        }}
        .loading {{
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 15px;
            color: #888;
        }}
        .spinner {{
            width: 40px; height: 40px;
            border: 3px solid #333;
            border-top-color: #4ecca3;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        .data-log {{
            width: 100%;
            max-height: 120px;
            overflow-y: auto;
            background: #0a0a15;
            border-radius: 4px;
            padding: 8px;
            font-size: 10px;
            font-family: monospace;
            color: #888;
        }}
        .data-log .sent {{ color: #4ecca3; }}
        .data-log .error {{ color: #ff6b6b; }}
        .key-hint {{
            font-size: 11px;
            color: #666;
            text-align: center;
            margin-top: 5px;
        }}
        .control-status {{
            font-size: 12px;
            padding: 5px 10px;
            border-radius: 4px;
            background: #2a2a4a;
        }}
        .control-status.active {{ background: #4ecca322; color: #4ecca3; }}
        .gamepad-row {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 5px;
            justify-content: center;
        }}
        .gamepad-toggle {{
            background: #2a2a4a;
            color: #fff;
            border: 1px solid #444;
            padding: 4px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }}
        .gamepad-toggle.on {{
            background: #4ecca322;
            border-color: #4ecca3;
            color: #4ecca3;
        }}
        .gamepad-status {{
            font-size: 11px;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>DJI Romo - Live Camera + Joystick</h1>
        <div class="status">
            <div class="status-dot" id="status-dot"></div>
            <span id="status-text">Connecting...</span>
        </div>
    </div>
    <div class="error" id="error-box"></div>
    <div class="main-content">
        <div class="video-container">
            <div id="remote-video">
                <div class="loading" id="loading">
                    <div class="spinner"></div>
                    <span>Connecting to video stream...</span>
                </div>
            </div>
        </div>
        <div class="joystick-panel">
            <h3>Robot Control</h3>
            <div class="control-status" id="control-status">DataStream: Waiting</div>
            <div class="joystick-grid">
                <div class="joy-btn empty"></div>
                <button class="joy-btn" id="btn-up" data-dir="up">Forward</button>
                <div class="joy-btn empty"></div>
                <button class="joy-btn" id="btn-left" data-dir="left">Rot. L</button>
                <button class="joy-btn stop" id="btn-stop" data-dir="none">STOP</button>
                <button class="joy-btn" id="btn-right" data-dir="right">Rot. R</button>
                <div class="joy-btn empty"></div>
                <button class="joy-btn" id="btn-down" data-dir="down">U-Turn</button>
                <div class="joy-btn empty"></div>
            </div>
            <div class="key-hint">Keyboard: Z/W=Forward Q/A=Rot.Left D/E=Rot.Right S=U-Turn Space=Stop</div>
            <div class="gamepad-row">
                <button class="btn gamepad-toggle" id="btn-gamepad" onclick="toggleGamepad()">Gamepad: OFF</button>
                <span class="gamepad-status" id="gamepad-status"></span>
            </div>
            <div class="data-log" id="data-log"></div>
        </div>
    </div>
    <div class="controls">
        <button class="btn btn-primary" id="btn-enter-control" onclick="enterControlMode()">Enable Control</button>
        <button class="btn" id="btn-audio" onclick="toggleAudio()" style="background:#2a2a4a;color:#fff;">Audio Off</button>
        <button class="btn btn-danger" id="btn-disconnect" onclick="disconnect()">Disconnect</button>
    </div>
    <div class="info">
        <span id="info-text">Channel: {channel}</span>
    </div>

    <script>
        const appId = "{app_id}";
        const channel = "{channel}";
        const token = {token_json} || null;
        const uid = {uid};
        const sn = "{sn}";
        const publishUid = 50000;

        let client = null;
        let remoteVideoTrack = null;
        let remoteAudioTrack = null;
        let audioEnabled = false;
        let controlModeActive = false;
        let currentDirection = 'none';

        const CONTROL_MODES = {{
            'forward': 17,
            'rotate_left': 18,
            'rotate_right': 19,
            'u_turn': 16,
        }};

        function log(msg, type = 'info') {{
            const logEl = document.getElementById('data-log');
            const time = new Date().toLocaleTimeString();
            const cls = type === 'error' ? 'error' : (type === 'sent' ? 'sent' : '');
            logEl.innerHTML = `<div class="${{cls}}">${{time}}: ${{msg}}</div>` + logEl.innerHTML;
            if (logEl.children.length > 50) logEl.lastChild.remove();
        }}

        function showError(msg) {{
            const box = document.getElementById('error-box');
            box.textContent = msg;
            box.style.display = 'block';
            log(msg, 'error');
        }}

        function setStatus(connected, text) {{
            const dot = document.getElementById('status-dot');
            const statusText = document.getElementById('status-text');
            dot.classList.toggle('connected', connected);
            statusText.textContent = text;
        }}

        function setControlStatus(active, text) {{
            const el = document.getElementById('control-status');
            el.textContent = text;
            el.classList.toggle('active', active);
            controlModeActive = active;
        }}

        function setInfo(text) {{
            document.getElementById('info-text').textContent = text;
        }}

        function sendControlData(direction) {{
            if (!controlModeActive) return;

            const dirModeMap = {{
                'up': 'forward',
                'down': 'u_turn',
                'left': 'rotate_left',
                'right': 'rotate_right',
                'none': 'stop'
            }};

            const controlAction = dirModeMap[direction] || 'stop';
            const mode = CONTROL_MODES[controlAction] || null;

            fetch('/control', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ direction: controlAction }})
            }}).then(r => r.json()).then(result => {{
                const via = (result.via || 'http').toUpperCase();
                if (controlAction === 'stop') {{
                    log(`STOP`, 'sent');
                }} else {{
                    log(`${{via}}>> mode=${{mode}} (${{controlAction}})`, 'sent');
                }}
            }}).catch(() => {{}});
        }}

        async function enterControlMode() {{
            log('Activating control mode...');

            try {{
                const response = await fetch('/enter-control', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }}
                }});

                if (response.ok) {{
                    const data = await response.json();
                    if (data.error) {{
                        log('Mode error: ' + data.error, 'error');
                    }} else {{
                        log('Control mode activated', 'sent');
                    }}
                }} else {{
                    log('API error: ' + response.status, 'error');
                }}
            }} catch (e) {{
                log('Connection error: ' + e.message, 'error');
                return;
            }}

            setControlStatus(true, 'Control: Active');
            document.getElementById('btn-enter-control').textContent = 'Control Active';
            document.getElementById('btn-enter-control').disabled = true;
            log('Use arrows or ZQSD keys', 'sent');
        }}

        function setupJoystick() {{
            ['up', 'down', 'left', 'right', 'none'].forEach(dir => {{
                const btn = document.querySelector(`[data-dir="${{dir}}"]`);
                if (!btn) return;

                btn.addEventListener('mousedown', (e) => {{
                    e.preventDefault();
                    currentDirection = dir;
                    btn.classList.add('active');
                    sendControlData(dir);
                }});

                btn.addEventListener('mouseup', (e) => {{
                    e.preventDefault();
                    btn.classList.remove('active');
                    if (dir !== 'none' && currentDirection === dir) {{
                        currentDirection = 'none';
                        sendControlData('none');
                    }}
                }});

                btn.addEventListener('mouseleave', (e) => {{
                    btn.classList.remove('active');
                }});

                btn.addEventListener('touchstart', (e) => {{
                    e.preventDefault();
                    currentDirection = dir;
                    btn.classList.add('active');
                    sendControlData(dir);
                }});

                btn.addEventListener('touchend', (e) => {{
                    e.preventDefault();
                    btn.classList.remove('active');
                    if (dir !== 'none' && currentDirection === dir) {{
                        currentDirection = 'none';
                        sendControlData('none');
                    }}
                }});
            }});

            document.addEventListener('keydown', (e) => {{
                if (e.repeat) return;
                const keyMap = {{
                    'KeyW': 'up', 'KeyZ': 'up', 'ArrowUp': 'up',
                    'KeyQ': 'left', 'KeyA': 'left', 'ArrowLeft': 'left',
                    'KeyD': 'right', 'KeyE': 'right', 'ArrowRight': 'right',
                    'KeyS': 'down', 'ArrowDown': 'down',
                    'Space': 'none'
                }};

                const action = keyMap[e.code];
                if (!action) return;

                e.preventDefault();
                currentDirection = action;
                const btn = document.querySelector(`[data-dir="${{action}}"]`);
                if (btn) btn.classList.add('active');
                sendControlData(action);
            }});

            document.addEventListener('keyup', (e) => {{
                const keyMap = {{
                    'KeyW': 'up', 'KeyZ': 'up', 'ArrowUp': 'up',
                    'KeyQ': 'left', 'KeyA': 'left', 'ArrowLeft': 'left',
                    'KeyD': 'right', 'KeyE': 'right', 'ArrowRight': 'right',
                    'KeyS': 'down', 'ArrowDown': 'down',
                    'Space': 'none'
                }};

                const action = keyMap[e.code];
                if (!action) return;

                const btn = document.querySelector(`[data-dir="${{action}}"]`);
                if (btn) btn.classList.remove('active');
                if (action !== 'none' && currentDirection === action) {{
                    currentDirection = 'none';
                    sendControlData('none');
                }}
            }});
        }}

        // --- Gamepad support via WebHID API (DualSense + Xbox) ---
        let hidDevice = null;
        let controllerType = null;
        let lastGamepadDir = 'none';
        let lastBtnAction1 = false;
        let lastBtnAction2 = false;

        async function toggleGamepad() {{
            const btn = document.getElementById('btn-gamepad');
            const statusEl = document.getElementById('gamepad-status');

            if (hidDevice) {{
                try {{ await hidDevice.close(); }} catch(e) {{}}
                hidDevice = null;
                btn.textContent = 'Gamepad: OFF';
                btn.classList.remove('on');
                statusEl.textContent = '';
                sendControlData('none');
                log('Gamepad disconnected');
                return;
            }}

            if (!navigator.hid) {{
                log('WebHID not available', 'error');
                return;
            }}

            try {{
                log('Selecting gamepad...');
                const devices = await navigator.hid.requestDevice({{
                    filters: [
                        {{ vendorId: 0x054C, productId: 0x0CE6, usagePage: 0x0001, usage: 0x0005 }},
                        {{ vendorId: 0x054C, productId: 0x0DF2, usagePage: 0x0001, usage: 0x0005 }},
                        {{ vendorId: 0x045E, productId: 0x0B13 }},
                        {{ vendorId: 0x045E, productId: 0x0B20 }},
                        {{ vendorId: 0x045E, productId: 0x02FD }},
                        {{ vendorId: 0x045E, productId: 0x02E0 }},
                        {{ vendorId: 0x045E, productId: 0x0B12 }},
                        {{ vendorId: 0x045E, productId: 0x02EA }},
                    ]
                }});
                if (!devices.length) {{
                    log('No device selected', 'error');
                    return;
                }}
                hidDevice = devices[0];
                if (!hidDevice.opened) await hidDevice.open();

                controllerType = (hidDevice.vendorId === 0x054C) ? 'dualsense' : 'xbox';
                const ctrlName = hidDevice.productName || (controllerType === 'dualsense' ? 'DualSense' : 'Xbox Controller');
                btn.textContent = 'Gamepad: ON';
                btn.classList.add('on');
                statusEl.textContent = ctrlName;
                lastGamepadDir = 'none';
                lastBtnAction1 = false;
                lastBtnAction2 = false;
                log(ctrlName + ' connected via WebHID', 'sent');

                hidDevice.addEventListener('inputreport', handleHIDReport);
            }} catch(e) {{
                log('WebHID error: ' + e.message, 'error');
            }}
        }}

        function handleHIDReport(event) {{
            if (controllerType === 'dualsense') handleDualSenseReport(event);
            else if (controllerType === 'xbox') handleXboxReport(event);
        }}

        function updateStickUI(dir, x, y) {{
            const statusEl = document.getElementById('gamepad-status');
            statusEl.textContent = 'X:' + x.toFixed(2) + ' Y:' + y.toFixed(2);
            if (dir !== lastGamepadDir) {{
                lastGamepadDir = dir;
                sendControlData(dir);
                log('Gamepad: ' + dir + ' (X:' + x.toFixed(2) + ' Y:' + y.toFixed(2) + ')', 'sent');
                ['up', 'down', 'left', 'right', 'none'].forEach(d => {{
                    const b = document.querySelector(`[data-dir="${{d}}"]`);
                    if (b) b.classList.toggle('active', d === dir);
                }});
            }}
        }}

        function handleActionButtons(btn1Pressed, btn1Label, btn2Pressed, btn2Label) {{
            if (btn1Pressed && !lastBtnAction1) {{
                sendControlData('down');
                log('Gamepad: U-Turn (' + btn1Label + ')', 'sent');
            }}
            lastBtnAction1 = btn1Pressed;

            if (btn2Pressed && !lastBtnAction2) {{
                fetch('/go-home', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }}
                }}).then(r => r.json()).then(() => {{
                    log('Gamepad: Go Home (' + btn2Label + ')', 'sent');
                }}).catch(() => {{}});
            }}
            lastBtnAction2 = btn2Pressed;
        }}

        function handleDualSenseReport(event) {{
            const {{ data, reportId }} = event;
            let offset = 0;
            if (reportId === 0x31) offset = 1;
            else if (reportId !== 0x01) return;

            const rawX = data.getUint8(offset);
            const rawY = data.getUint8(offset + 1);
            const x = (2 * rawX / 255) - 1.0;
            const y = (2 * rawY / 255) - 1.0;

            let dir = 'none';
            if (y < -0.5) dir = 'up';
            else if (x < -0.5) dir = 'left';
            else if (x > 0.5) dir = 'right';
            updateStickUI(dir, x, y);

            const buttons0 = data.getUint8(offset + 7);
            handleActionButtons(!!(buttons0 & 0x20), 'Cross', !!(buttons0 & 0x80), 'Triangle');
        }}

        function handleXboxReport(event) {{
            const {{ data, reportId }} = event;
            if (reportId !== 0x01 || data.byteLength < 12) return;

            const rawX = data.getUint16(0, true);
            const rawY = data.getUint16(2, true);
            const x = (rawX - 32768) / 32768;
            const y = (rawY - 32768) / 32768;

            let dir = 'none';
            if (y < -0.5) dir = 'up';
            else if (x < -0.5) dir = 'left';
            else if (x > 0.5) dir = 'right';
            updateStickUI(dir, x, y);

            if (data.byteLength >= 14) {{
                const btnRaw = data.getUint16(11, true);
                const aPressed = !!((btnRaw >> 4) & 1);
                const yPressed = !!((btnRaw >> 7) & 1);
                handleActionButtons(aPressed, 'A', yPressed, 'Y');
            }}
        }}

        async function connect() {{
            try {{
                setStatus(false, 'Connecting...');
                setInfo('Connecting to Agora...');
                log('Connecting to Agora...');

                try {{
                    AgoraRTC.setParameter("AUDIO_JITTER_BUFFER_MAX_DELAY", 200);
                    AgoraRTC.setParameter("AUDIO_JITTER_BUFFER_MIN_DELAY", 0);
                    log('Jitter buffer optimized');
                }} catch(e) {{ console.warn('Jitter buffer params:', e); }}

                client = AgoraRTC.createClient({{ mode: 'rtc', codec: 'h264' }});
                log('Mode: rtc/h264');

                client.on('user-published', async (user, mediaType) => {{
                    console.log('User published:', user.uid, mediaType);
                    log(`Robot: ${{mediaType}} UID=${{user.uid}}`);
                    await client.subscribe(user, mediaType);

                    if (mediaType === 'video') {{
                        remoteVideoTrack = user.videoTrack;
                        const container = document.getElementById('remote-video');
                        container.innerHTML = '';
                        remoteVideoTrack.play(container);
                        setStatus(true, 'Connected');
                        setInfo('Video stream active - ' + channel);
                        log('Video active');
                    }}

                    if (mediaType === 'audio') {{
                        remoteAudioTrack = user.audioTrack;
                        log('Audio available');
                        if (audioEnabled) {{
                            remoteAudioTrack.play();
                        }}
                    }}
                }});

                client.on('user-unpublished', (user, mediaType) => {{
                    log(`UID ${{user.uid}} unpub ${{mediaType}}`);
                    if (mediaType === 'video') {{
                        document.getElementById('remote-video').innerHTML = '<div class="loading"><span>Stream interrupted</span></div>';
                    }}
                }});

                client.on('stream-message', (uid, data) => {{
                    try {{
                        const msg = new TextDecoder().decode(data);
                        log(`<< UID ${{uid}}: ${{msg.substring(0, 50)}}`);
                    }} catch (e) {{
                        log(`<< UID ${{uid}}: [binary ${{data.byteLength}}b]`);
                    }}
                }});

                client.on('connection-state-change', (state) => {{
                    console.log('Connection state:', state);
                    log(`Conn: ${{state}}`);
                    if (state === 'DISCONNECTED') {{
                        setStatus(false, 'Disconnected');
                        setControlStatus(false, 'DataStream: Disconnected');
                    }}
                }});

                console.log('Joining channel:', {{ appId, channel, uid, tokenLen: token?.length }});

                const joinedUid = await client.join(appId, channel, token || null, uid);
                console.log('Joined channel with UID:', joinedUid);
                log(`Joined UID=${{joinedUid}}`);

                log('Control via Python backend (Agora DataStream)');
                setControlStatus(false, 'Ready (via server)');

                setStatus(true, 'Connected');
                setInfo('Waiting for video stream...');
                document.getElementById('loading').innerHTML = '<span>Waiting for robot video stream...</span>';

                setupJoystick();

            }} catch (error) {{
                console.error('Connection error:', error);
                showError('Error: ' + error.message);
                setStatus(false, 'Error');
            }}
        }}

        async function disconnect() {{
            try {{
                if (client) {{
                    sendControlData('none');
                    await client.leave();
                    client = null;
                }}
                setStatus(false, 'Disconnected');
                setControlStatus(false, 'DataStream: Disconnected');
                setInfo('Stream stopped');
                document.getElementById('remote-video').innerHTML = '<div class="loading"><span>Disconnected</span></div>';
                log('Disconnected');
            }} catch (error) {{
                console.error('Disconnect error:', error);
            }}
        }}

        function toggleAudio() {{
            const btn = document.getElementById('btn-audio');
            audioEnabled = !audioEnabled;
            if (audioEnabled) {{
                if (remoteAudioTrack) {{
                    remoteAudioTrack.play();
                    log('Audio active');
                }} else {{
                    log('Audio not yet available');
                }}
                btn.textContent = 'Audio On';
                btn.style.background = '#4ecca3';
                btn.style.color = '#1a1a2e';
            }} else {{
                if (remoteAudioTrack) {{
                    remoteAudioTrack.stop();
                    log('Audio disabled');
                }}
                btn.textContent = 'Audio Off';
                btn.style.background = '#2a2a4a';
                btn.style.color = '#fff';
            }}
        }}

        connect();
    </script>
</body>
</html>'''

        viewer_path = Path(__file__).parent / "video_viewer_session.html"
        viewer_path.write_text(html_content)

    # ==================== MAIN FLOW ====================

    def start(self):
        """Start video stream with joystick/gamepad control.

        Strategy: Agora tokens are UID-specific. We need two separate credentials:
        1. First API call -> credentials for Python backend (DataStream control)
        2. Stop stream to free the slot (token remains valid ~24h)
        3. Second API call -> credentials for web viewer (video display)
        """
        device_sn = self.config["device_sn"]
        stream_url = f"/cr/app/api/v1/devices/{device_sn}/live/openStream/start"
        stop_url = f"/cr/app/api/v1/devices/{device_sn}/live/stop"

        # Start control API server
        self.start_control_server(port=8765)

        # Step 1: Get credentials for Python Agora backend (control)
        print(f"{Colors.CYAN}[1/3] Getting Agora credentials for control backend...{Colors.END}")
        result1 = self.api_post(stream_url)
        if result1.get("result", {}).get("code") != 0:
            print(f"{Colors.RED}Failed to get stream credentials{Colors.END}")
            return result1

        data1 = result1.get("data", {})
        python_creds = self._parse_stream_creds(data1)
        print(f"{Colors.DIM}  Backend UID: {python_creds['uid']} | Channel: {python_creds['channel']}{Colors.END}")

        debug_file = Path(__file__).parent / "agora_debug.json"
        debug_file.write_text(json.dumps(data1, indent=2))

        # Step 2: Connect Python Agora SDK
        print(f"{Colors.CYAN}[2/3] Connecting Python Agora backend...{Colors.END}")
        try:
            agora_ok = self.connect_agora(creds=python_creds, enter_mode=False)
            if agora_ok:
                print(f"{Colors.GREEN}Agora backend connected (UID {python_creds['uid']})!{Colors.END}")
            else:
                print(f"{Colors.RED}Agora backend failed{Colors.END}")
        except Exception as e:
            import traceback
            print(f"{Colors.RED}Agora backend error: {e}{Colors.END}")
            traceback.print_exc()

        # Step 3: Stop stream to free slot, then start again for web viewer
        print(f"{Colors.CYAN}[3/3] Getting credentials for web viewer...{Colors.END}")
        self.api_post(stop_url)
        time.sleep(0.5)

        result2 = self.api_post(stream_url)
        if result2.get("result", {}).get("code") != 0:
            print(f"{Colors.RED}Failed to get viewer credentials{Colors.END}")
            return result2

        data2 = result2.get("data", {})
        viewer_creds = self._parse_stream_creds(data2)
        print(f"{Colors.DIM}  Viewer UID: {viewer_creds['uid']} | Channel: {viewer_creds['channel']}{Colors.END}")

        viewer_params = {
            'app_id': viewer_creds['app_id'],
            'channel': viewer_creds['channel'],
            'token': viewer_creds['token'],
            'uid': str(viewer_creds['uid']),
            'sn': device_sn,
        }
        self._create_video_viewer(viewer_params)

        print(f"{Colors.CYAN}Ouverture du viewer video...{Colors.END}")
        webbrowser.open("http://127.0.0.1:8765/")

        return result2


class ControlAPIHandler(BaseHTTPRequestHandler):
    """HTTP handler for joystick control commands."""

    def __init__(self, controller, *args, **kwargs):
        self.controller = controller
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _send_json(self, code, data):
        """Send JSON response with CORS headers."""
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        """Serve the video viewer HTML."""
        if self.path == '/' or self.path == '/viewer':
            viewer_path = Path(__file__).parent / "video_viewer_session.html"
            if viewer_path.exists():
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Permissions-Policy', 'gamepad=(self)')
                self.end_headers()
                self.wfile.write(viewer_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Viewer not found')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handle control commands."""
        if self.path == '/control':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)

                dir_in = data.get('direction', 'none')

                dir_map = {
                    'up': 'forward',
                    'down': 'u_turn',
                    'left': 'rotate_left',
                    'right': 'rotate_right',
                    'none': 'stop',
                    'forward': 'forward',
                    'rotate_left': 'rotate_left',
                    'rotate_right': 'rotate_right',
                    'u_turn': 'u_turn',
                    'stop': 'stop',
                }
                direction = dir_map.get(dir_in, 'stop')

                if self.controller.agora_connected and self.controller.agora_stream_ready:
                    self.controller.send_agora_control(direction)
                    self._send_json(200, {'ok': True, 'direction': direction, 'via': 'agora'})
                else:
                    self._send_json(503, {'error': 'Agora not connected'})

            except Exception as e:
                self._send_json(500, {'error': str(e)})

        elif self.path == '/enter-control':
            result = self.controller.enter_remote_control_mode()
            self._send_json(200, result)

        elif self.path == '/exit-control':
            result = self.controller.exit_remote_control_mode()
            self._send_json(200, result)

        elif self.path == '/go-home':
            result = self.controller.go_home()
            self._send_json(200, result)

        else:
            self.send_response(404)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()


def main():
    print(f"{Colors.CYAN}{Colors.BOLD}")
    print("=" * 50)
    print("  DJI Romo - Video Stream & Robot Control")
    print("=" * 50)
    print(f"{Colors.END}")

    controller = DJIVideoController()

    try:
        result = controller.start()
        if result and not result.get("error"):
            print(f"\n{Colors.GREEN}Viewer ouvert dans le navigateur.{Colors.END}")
            print(f"{Colors.DIM}Ctrl+C pour arreter.{Colors.END}\n")
            while True:
                time.sleep(1)
        else:
            print(f"{Colors.RED}Erreur au demarrage.{Colors.END}")
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Arret en cours...{Colors.END}")
    finally:
        controller.disconnect_agora()
        controller.stop_control_server()
        controller.stop_live_stream()
        print(f"{Colors.GREEN}Arrete proprement.{Colors.END}")


if __name__ == "__main__":
    main()
