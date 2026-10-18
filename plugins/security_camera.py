"""
Security Camera & Motion Detection Plugin for JARVIS.
Uses OpenCV BackgroundSubtractorMOG2 to detect motion, capture security snapshots,
and record security events without cloud dependencies.
"""

import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

from memory.config_manager import get_plugin_setting

PLUGIN = {
    "name": "security_camera",
    "description": (
        "Monitor webcam for motion and security events. "
        "Can take security snapshots, start background motion monitoring, "
        "report camera status, or check security logs. "
        "Use when user says 'turn on security camera', 'watch the room', "
        "'detect motion', or 'security status'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | start_monitoring | stop_monitoring | snapshot | list_events"
            },
            "sensitivity": {
                "type": "STRING",
                "description": "low | medium | high (default: medium)"
            }
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "namespace": "security_camera",
    "title": "Security Camera Settings",
    "fields": [
        {
            "name": "camera_index",
            "label": "Webcam Index (default: 0)",
            "type": "number",
            "default": 0
        },
        {
            "name": "sensitivity",
            "label": "Default Sensitivity (low/medium/high)",
            "type": "text",
            "default": "medium"
        }
    ]
}

_SECURITY_DIR = _BASE / "memory" / "security"
_SECURITY_DIR.mkdir(parents=True, exist_ok=True)

class SecurityDaemon:
    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._threshold = 0.02  # 2% of frame pixels changing
        self._last_event_time = 0
        self._cooldown = 10.0   # seconds between motion alerts
        self._event_count = 0
        self._player = None

    def set_sensitivity(self, level: str):
        level = (level or "medium").lower()
        if level == "high":
            self._threshold = 0.008
        elif level == "low":
            self._threshold = 0.05
        else:
            self._threshold = 0.02

    def start(self, player=None):
        if self._running:
            return "Security monitoring is already active, Sir."
        if not cv2:
            return "OpenCV is not available for security camera monitoring."

        self._player = player
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        return "Security camera monitoring activated. I am watching for movement, Sir."

    def stop(self):
        if not self._running:
            return "Security camera is not currently active, Sir."
        self._running = False
        return "Security camera monitoring has been deactivated, Sir."

    def is_active(self) -> bool:
        return self._running

    def _monitor_loop(self):
        cam_idx = int(get_plugin_setting("security_camera", "camera_index", 0))
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
        if not cap.isOpened():
            cap = cv2.VideoCapture(cam_idx)

        if not cap.isOpened():
            self._running = False
            if self._player:
                try:
                    self._player.write_log("Security Camera: Failed to access webcam hardware.")
                except Exception:
                    pass
            return

        fgbg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=True)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

        try:
            while self._running:
                ret, frame = cap.read()
                if not ret or frame is None:
                    time.sleep(0.5)
                    continue

                # Apply background subtraction
                fgmask = fgbg.apply(frame)
                fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, kernel)

                # Calculate motion ratio
                non_zero = np.count_nonzero(fgmask > 200)
                total_pixels = fgmask.size
                ratio = non_zero / total_pixels

                now = time.time()
                if ratio > self._threshold and (now - self._last_event_time) > self._cooldown:
                    self._last_event_time = now
                    self._event_count += 1
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    snap_path = _SECURITY_DIR / f"motion_{timestamp}.jpg"
                    cv2.imwrite(str(snap_path), frame)

                    msg = f"Motion detected ({ratio*100:.1f}% change). Snapshot saved to {snap_path.name}."
                    if self._player:
                        try:
                            self._player.write_log(f"[Security Alert] {msg}")
                        except Exception:
                            pass
                    print(f"[Security] 🚨 {msg}")

                time.sleep(0.2)  # Check at ~5 FPS to keep CPU low
        finally:
            cap.release()

_daemon = SecurityDaemon()

def _take_snapshot() -> str:
    if not cv2:
        return "OpenCV is not available to capture snapshots."
    cam_idx = int(get_plugin_setting("security_camera", "camera_index", 0))
    cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
    if not cap.isOpened():
        cap = cv2.VideoCapture(cam_idx)
    if not cap.isOpened():
        return "Could not open camera device, Sir."
    
    try:
        # Allow camera auto-exposure to settle
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        if not ret or frame is None:
            return "Failed to capture video frame from camera, Sir."
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_path = _SECURITY_DIR / f"snapshot_{timestamp}.jpg"
        cv2.imwrite(str(snap_path), frame)
        return f"Snapshot captured and saved as {snap_path.name}, Sir."
    finally:
        cap.release()

def _list_events() -> str:
    snaps = sorted(_SECURITY_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not snaps:
        return "No security camera events or snapshots recorded, Sir."
    lines = []
    for s in snaps[:6]:
        mtime = datetime.fromtimestamp(s.stat().st_mtime).strftime("%b %d %H:%M:%S")
        lines.append(f"• {s.name} ({mtime})")
    status = "Active" if _daemon.is_active() else "Standby"
    return f"Security Camera [{status}] — Recent Events ({len(snaps)} total):\n" + "\n".join(lines)

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "status").lower().strip()
    sens = parameters.get("sensitivity", "medium")
    _daemon.set_sensitivity(sens)

    try:
        if action in ("start_monitoring", "start", "watch", "on"):
            return _daemon.start(player=player)
        elif action in ("stop_monitoring", "stop", "off"):
            return _daemon.stop()
        elif action in ("snapshot", "photo", "capture"):
            return _take_snapshot()
        elif action in ("list_events", "events", "log", "history"):
            return _list_events()
        elif action == "status":
            st = "Active and monitoring" if _daemon.is_active() else "Standby (inactive)"
            return f"Security Camera status: {st}, Sir."
        else:
            return f"Unknown security camera action '{action}'. Supported: status, start_monitoring, stop_monitoring, snapshot, list_events."
    except Exception as e:
        return f"Security camera operation failed: {e}"
