# baby_presence

A tiny service that watches an RTSP crib camera and publishes an occupancy state
to MQTT, discoverable as a Home Assistant `binary_sensor` with
`device_class: occupancy`.

- Runs every few seconds on a VM alongside Home Assistant
- Detects "baby in crib" vs. "empty crib" via a fine-tuned image classifier
- Debounces transitions so a single bad frame or a parent leaning over the rail
  doesn't flip the state

## How it works

```
 RTSP camera ──► FrameGrabber (background reader)
                     │
                     ▼
                 Detector ──► Debouncer ──► MQTTPublisher ──► Home Assistant
                     │
                     └──► FrameLogger (optional, for future training)
```

The detector is a factory that picks between two backends based on the model
file:

| Model                               | Sidecar        | Backend                  |
| ----------------------------------- | -------------- | ------------------------ |
| `presence_model.pt` (+ `.json`)     | yes            | MobileNetV3-small (ours) |
| `yolov8s.pt`                        | no             | stock YOLO (COCO person) |

Stock YOLO is a poor fit for a swaddled baby viewed top-down (it was trained on
upright people), so a few hours of labeled frames + transfer learning produces a
much more reliable classifier.

## Setup

### On the VM (Debian)

```bash
git clone <this repo> ~/baby_presence
cd ~/baby_presence
sudo ./scripts/install.sh
```

This creates a `baby_presence` system user, installs into `/opt/baby_presence`,
writes `/etc/baby_presence/baby_presence.env` from `.env.example`, and installs
the systemd unit at `/etc/systemd/system/baby_presence.service`.

If you've already trained a classifier, drop `presence_model.pt` and
`presence_model.json` into the repo root before running the installer and it'll
deploy them automatically.

Edit the config:

```bash
sudoedit /etc/baby_presence/baby_presence.env
```

Set at minimum:

```
RTSP_URL=rtsp://user:pass@camera:554/stream
MQTT_HOST=<broker>
MQTT_USER=<user>
MQTT_PASS=<pass>
MODEL_PATH=/opt/baby_presence/presence_model.pt
```

Then start it:

```bash
sudo systemctl enable --now baby_presence
sudo journalctl -u baby_presence -f
```

On first connect the sensor auto-registers in Home Assistant via MQTT Discovery
(topic prefix `homeassistant/`).

## Training your own model

The project ships with a workflow to collect frames from your own camera and
fine-tune a classifier on them. You need to do this once (and again if your
camera moves, the crib changes significantly, or accuracy drops over time).

### 1. Collect frames

Set `FRAME_LOG_DIR` in the service env to a durable location (NAS share
recommended) and let the service run for 24–48 hours:

```
FRAME_LOG_DIR=/mnt/baby_frames
FRAME_LOG_INTERVAL=30
```

One 67 KB JPEG is saved every 30 s — roughly 200 MB/day. The filename encodes
a timestamp and the detector's current guess:

```
20260421-151823_occupied_c0.82.jpg
```

### 2. Label by timeline

The camera being fixed and transitions being rare means labeling reduces to
marking "baby went in" / "baby came out" events on a timeline. The included UI
does this:

```bash
pip install pillow
python3 scripts/label_ui.py /Volumes/baby_frames
```

<img width="1006" height="854" alt="Screenshot 2026-04-24 at 1 34 58 PM" src="https://github.com/user-attachments/assets/72e473ca-9f09-41a8-b59a-5d9762071b9b" />

Scrub with arrow keys, press `i` / `o` to drop IN/OUT markers at transition
frames, `s` to save a `ranges.csv`. Keys:

| Key                   | Action                     |
| --------------------- | -------------------------- |
| Left / Right          | ±1 frame                   |
| Shift+Left/Right      | ±30 frames                 |
| Cmd/Ctrl+Left/Right   | ±300 frames                |
| Home / End            | first / last frame         |
| `i` / `o`             | mark IN / OUT              |
| `u`                   | undo last marker           |
| `s`                   | save CSV + markers sidecar |

If you prefer, you can skip the UI and hand-author `ranges.csv` directly:

```csv
start,end,label
20260421-143000,20260421-160000,occupied
20260421-161500,20260421-180000,empty
```

Timestamps accept either the filename format (`20260421-143000`) or ISO
(`2026-04-21T14:30:00`).

### 3. Build the dataset

```bash
python3 scripts/label_by_time.py \
    --frames /Volumes/baby_frames \
    --ranges ranges.csv \
    --out ./dataset \
    --buffer 120 \
    --link
```

`--buffer 120` trims 2 minutes off each end of every range so transition
moments (lifting him in/out) don't pollute training data. `--link` uses
symlinks — drop it if you want real copies for offline training.

### 4. Train

```bash
python3 scripts/train.py --dataset ./dataset --output presence_model.pt
```

Uses Apple Silicon MPS if available, CUDA otherwise, CPU as fallback.
Produces `presence_model.pt` + `presence_model.json` (metadata). Both are
needed at deploy time.

Expect near-perfect validation accuracy since the task is easy on a fixed
camera. If accuracy is below ~0.95 something is wrong with the labels or data.

### 5. Deploy

Copy the model files to the VM and restart:

```bash
scp presence_model.pt presence_model.json <user>@<vm>:~/baby_presence/
ssh <user>@<vm> 'cd ~/baby_presence && sudo ./scripts/install.sh'
ssh <user>@<vm> 'sudo systemctl restart baby_presence'
```

## Configuration reference

All config is via environment variables. Required:

| Variable    | Description                                         |
| ----------- | --------------------------------------------------- |
| `RTSP_URL`  | Full RTSP URL to the camera                         |
| `MQTT_HOST` | MQTT broker hostname / IP                           |

Common optional:

| Variable              | Default                               | Description                                  |
| --------------------- | ------------------------------------- | -------------------------------------------- |
| `MQTT_PORT`           | `1883`                                |                                              |
| `MQTT_USER` / `_PASS` | empty                                 | optional broker auth                         |
| `MODEL_PATH`          | `yolov8n.pt`                          | path to `.pt` model file                     |
| `CONFIDENCE`          | `0.25`                                | threshold — probability for classifier, detection confidence for YOLO |
| `SAMPLE_INTERVAL`     | `2.0`                                 | seconds between inferences                   |
| `DEBOUNCE_SECONDS`    | `15.0`                                | state must hold this long before publishing  |
| `FRAME_LOG_DIR`       | unset                                 | if set, logs one frame per interval here     |
| `FRAME_LOG_INTERVAL`  | `60.0`                                | seconds between logged frames                |
| `BASE_TOPIC`          | `babypresence`                        | MQTT state / availability topic prefix       |
| `HA_DISCOVERY_PREFIX` | `homeassistant`                       | MQTT Discovery prefix                        |
| `DEVICE_ID`           | `baby_presence_crib`                  | HA unique_id                                 |
| `DEVICE_NAME`         | `Baby Crib`                           | HA device display name                       |
| `LOG_LEVEL`           | `INFO`                                | set to `DEBUG` for per-frame confidence logs |

The service also reads `.env` in its working directory for local development —
on the VM, systemd reads `/etc/baby_presence/baby_presence.env` directly.

## MQTT topics

| Topic                                                       | Contents                    |
| ----------------------------------------------------------- | --------------------------- |
| `homeassistant/binary_sensor/<device_id>/config`            | HA Discovery payload (retained) |
| `<base_topic>/state`                                        | `ON` / `OFF` (retained)     |
| `<base_topic>/availability`                                 | `online` / `offline` (LWT)  |

## Project layout

```
├── config.py              # env var loading
├── detector.py            # factory → YOLO or classifier
├── main.py                # loop, debouncer, signal handling
├── mqtt_client.py         # HA Discovery + LWT
├── rtsp.py                # background RTSP reader
├── requirements.txt
├── .env.example
├── scripts/
│   ├── install.sh         # Debian installer (systemd)
│   ├── label_by_time.py   # build dataset from ranges.csv
│   ├── label_ui.py        # Tk timeline labeler
│   └── train.py           # fine-tune MobileNetV3-small
└── systemd/
    └── baby_presence.service
```

## Troubleshooting

**`conf=0.8x` on empty crib after restart** — Almost certainly RTSP buffer lag
showing a stale frame from when the baby was actually there. The background
reader thread in `rtsp.py` prevents this; if you're seeing it, check that the
stream isn't reconnecting in a loop.

**`libGL.so.1: cannot open shared object file`** — opencv-python (pulled by
ultralytics) needs `libgl1`. `sudo apt install libgl1`.

**`NNPACK unsupported hardware` warning** — benign. Hypervisor is exposing a
generic CPU type. To silence, set the VM's CPU type to host-passthrough.

**SSL cert errors on model download** — macOS Python.framework installs don't
have CA certs wired up. Run `/Applications/Python\ 3.XX/Install\ Certificates.command`
once.

**Mosquitto says "Client has exceeded timeout, disconnecting."** — Increase
`keepalive` or check that the VM can reach the broker. LWT should mark the
sensor unavailable in HA if the service dies.
