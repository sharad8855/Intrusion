# AI Intrusion & Virtual Tripwire System

A real-time smart-surveillance platform that monitors CCTV/IP-camera
feeds, detects intrusions using AI (YOLOv11 + ByteTrack), and sends
instant WhatsApp alerts.

**This build is CPU / GPU portable** — the same code runs on a plain
laptop CPU or an NVIDIA GPU and optimizes itself automatically. No
Jetson-specific code.

---

## 1. Hardware Requirements (replaces the old "Jetson only" section)

The system auto-detects the best available compute device at startup.
You do **not** change any code to switch between CPU and GPU.

| Tier | Hardware | Notes |
|------|----------|-------|
| **Minimum (CPU)** | Any x86-64 / ARM CPU, 4 cores, 8 GB RAM | Uses `yolo11n` + OpenVINO/ONNX. ~8–15 FPS for 1 camera. |
| **Recommended (GPU)** | NVIDIA GPU with CUDA (GTX 1060 / RTX 20-series or newer), 6 GB+ VRAM | FP16 + optional TensorRT. 30+ FPS, multi-camera. |
| **Edge option** | NVIDIA Jetson (Orin / Xavier) | Still supported — it is simply detected as a CUDA GPU. |

Common to all tiers: IP CCTV camera(s), local LAN/WiFi, SSD/SD storage.

### How device selection works

`backend/core/device.py` (`DeviceManager`) decides everything:

```
config: device.mode = auto
        │
        ├─ CUDA GPU available?  ──► GPU profile
        │     • FP16 (half precision) on Pascal+ cards
        │     • cudnn.benchmark + TF32 matmul
        │     • imgsz 640, optional TensorRT engine export
        │
        └─ otherwise            ──► CPU profile
              • threads pinned to physical core count
              • imgsz 416 (smaller = faster on CPU)
              • optional OpenVINO / ONNX Runtime export
```

You can force a device in `configs/config.yaml`:

```yaml
device:
  mode: "auto"     # auto | cpu | cuda | cuda:0
```

### Runtime (not just model) optimization

* **Latest-frame reader** — a dedicated thread keeps only the newest
  RTSP frame, so slow inference drops stale frames instead of building
  latency.
* **Adaptive frame-skip** (`pipeline.frame_skip: auto`) — measures
  inference time and processes every Nth frame to hold `target_fps`.
  This is what keeps a CPU-only box usable.
* **One-time accelerated export** — on first run the `.pt` model is
  exported to TensorRT (GPU) or OpenVINO (CPU) and reused afterwards.

---

## 2. Project Structure

```
intrusion-system/
├── backend/
│   ├── core/        config loader + DeviceManager (CPU/GPU logic)
│   ├── detector/    YOLOv11 detector (+ ByteTrack)
│   ├── tracker/     optional IOU fallback tracker
│   ├── rules/       line / circle / polygon rule engine
│   ├── alerts/      WhatsApp notifier (Twilio / Meta / webhook)
│   ├── cleanup/     2-hour snapshot auto-delete
│   ├── database/    SQLAlchemy models + session
│   ├── pipeline.py  per-camera processing thread
│   ├── camera_manager.py
│   └── main.py      FastAPI app
├── frontend/        dashboard (HTML/CSS/JS)
├── configs/         config.yaml
├── snapshots/  recordings/  models/   (auto-created)
└── requirements.txt
```

---

## 3. Installation

```bash
# 1. Python 3.10+  — create a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

# 2. Install PyTorch for YOUR device (pick one)
#    GPU (CUDA 12.1):
pip install torch --index-url https://download.pytorch.org/whl/cu121
#    CPU only:
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 3. Install the rest
pip install -r requirements.txt
```

---

## 4. Running

```bash
# from the project root
python -m backend.main
```

Then open the dashboard at <http://localhost:8000>.

On startup the console prints the resolved device, e.g.:

```
[device] GPU | NVIDIA GeForce RTX 3060 | device=cuda:0 | half=True | imgsz=640
[device] CPU | Intel(R) Core(TM) i5 | device=cpu | half=False | imgsz=416 | threads=6
```

---

## 5. Using the Dashboard

1. **Add a camera** — fill IP / port / credentials, or paste a full
   RTSP URL such as
   `rtsp://admin:pass@192.168.1.10:554/Streaming/Channels/101`.
2. **Live monitoring** — pick a camera to see the annotated AI feed
   (boxes, track IDs, FPS, device).
3. **Draw zones** — choose Line / Circle / Polygon and click on the
   feed:
   * Line — 2 clicks (tripwire crossing)
   * Circle — click centre, then edge (safety radius)
   * Polygon — click points, double-click to finish (restricted area)
4. **Alerts** — when a tracked object triggers a zone, a snapshot is
   saved, a WhatsApp alert is sent, and the evidence card appears.
   Snapshots auto-delete after 2 hours.

---

## 6. Configuration cheatsheet (`configs/config.yaml`)

| Setting | Purpose |
|---------|---------|
| `device.mode` | `auto` / `cpu` / `cuda` |
| `detection.weights` | `yolo11n.pt` (fast) … `yolo11x.pt` (accurate) |
| `detection.imgsz` | `auto` or fixed size; lower = faster |
| `optimization.*` | one-time TensorRT / OpenVINO export |
| `pipeline.target_fps` / `frame_skip` | runtime performance throttle |
| `alerts.whatsapp.provider` | `twilio` / `meta` / `webhook` / `disabled` |
| `cleanup.retention_hours` | evidence retention window (default 2) |

---

## 7. Technology Stack

Python · FastAPI · OpenCV · Ultralytics YOLOv11 · ByteTrack ·
PyTorch · SQLAlchemy (SQLite/PostgreSQL) · APScheduler ·
HTML/CSS/JS dashboard.
