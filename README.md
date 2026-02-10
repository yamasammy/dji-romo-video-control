# (DJI PATCHED THE TRICK)

# DJI Romo - Video Stream & Robot Control

Live video streaming and remote control for DJI Romo robot vacuums, directly from your browser.

Control your robot with keyboard (ZQSD/arrows), on-screen joystick buttons, or a PS5 DualSense / Xbox controller via WebHID.

![Python](https://img.shields.io/badge/python-3.8+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Live video** from the robot's camera via Agora WebRTC
- **Keyboard control** (ZQSD/WASD + arrow keys)
- **On-screen joystick** with mouse/touch support
- **PS5 DualSense gamepad** via WebHID (works on macOS where the standard Gamepad API doesn't detect DualSense over Bluetooth)
- **Xbox controller** (One S, Series X|S) via WebHID
- **Go Home** command (Triangle on DualSense / Y on Xbox)
- Low-latency control via Agora DataStream at 10Hz

## Prerequisites

1. **Python 3.8+**
2. **Agora Python SDK**
3. A `.env` file with your DJI Home credentials (see below)

## Step 1: Generate your credentials

You first need to extract your DJI Home API credentials using:

**[dji-home-credential-extractor](https://github.com/yamasammy/dji-home-credential-extractor)**

Follow its instructions to get your `DJI_USER_TOKEN` and `DJI_DEVICE_SN`, then come back here.

## Step 2: Setup

```bash
git clone https://github.com/yamasammy/dji-romo-video-control.git
cd dji-romo-video-control

pip install requests agora-python-sdk
```

Copy the template and fill in your credentials:

```bash
cp .env.template .env
```

Edit `.env` with the values from Step 1:

```env
DJI_USER_TOKEN=your_member_token_here
DJI_DEVICE_SN=your_robot_serial_number_here
```

## Step 3: Run

```bash
python3 dji_video_control.py
```

Your browser will open automatically at `http://127.0.0.1:8765/` with the video viewer.

## Usage

1. Click **Enable Control** to activate remote control mode
2. Use **keyboard** (Z/W = Forward, Q/A = Rotate Left, D/E = Rotate Right, S = U-Turn, Space = Stop)
3. Or click the **on-screen buttons**
4. For **gamepad**: click "Gamepad: OFF", select your controller in the Chrome popup, then use the left stick

### DualSense mapping

| Input | Action |
|-------|--------|
| Left stick up | Forward |
| Left stick left | Rotate left |
| Left stick right | Rotate right |
| Left stick down | Disabled (safety) |
| Cross / X button | U-Turn |
| Triangle button | Go Home (return to dock) |
| Release stick | Stop |

### Xbox controller mapping

| Input | Action |
|-------|--------|
| Left stick up | Forward |
| Left stick left | Rotate left |
| Left stick right | Rotate right |
| Left stick down | Disabled (safety) |
| A button | U-Turn |
| Y button | Go Home (return to dock) |
| Release stick | Stop |

## How it works

```
Gamepad (DualSense / Xbox, Bluetooth or USB)
    → Browser WebHID API
    → JS input handler
    → HTTP POST to Python backend (127.0.0.1:8765)
    → Agora Python SDK DataStream
    → Robot
```

The script makes two Agora API calls to get separate credentials for the Python control backend and the web video viewer, both connecting to the same channel with different UIDs. Control messages use the exact format discovered via reverse engineering:

```json
{"seq_id":0,"timestamp":1770335098888,"mode":17,"version":2,"x":1.0,"y":0.0}
```

Where `mode` encodes the direction: 16=U-Turn, 17=Forward, 18=Rotate Left, 19=Rotate Right.

## Notes

- The Agora token is valid for ~24 hours after generation
- Only one active stream session per device is allowed by DJI's API
- The standard browser Gamepad API does not detect DualSense on macOS via Bluetooth — this project uses WebHID as a workaround
- Supported Xbox controllers: Xbox One S (Model 1708), Xbox Series X|S, Xbox Elite Series 2
- Chrome is required for WebHID support (served from localhost for secure context)

## Related projects

- [dji-home-credential-extractor](https://github.com/yamasammy/dji-home-credential-extractor) — Extract DJI Home credentials from Android emulator

## License

MIT
