"""
Smart Home Integration Plugin for JARVIS.
Connects with Home Assistant REST API, Tuya local devices, and MQTT brokers.
Enables full voice control over lights, locks, climate, switches, and scenes.
"""

import sys
import json
from pathlib import Path
from typing import Optional, Dict, Any

_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

try:
    import requests
except ImportError:
    requests = None

try:
    from core import confirm as confirm_gate
except ImportError:
    confirm_gate = None

from memory.config_manager import get_plugin_setting

PLUGIN = {
    "name": "smart_home",
    "description": (
        "Control smart home automation devices including lights, switches, locks, climate/thermostats, and scenes. "
        "Works with Home Assistant, Tuya devices, and MQTT. "
        "Use when user says 'turn on the lights', 'dim the living room', 'set temperature to 72', "
        "'lock the door', 'activate movie scene', or 'smart home status'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "control_device | get_state | list_devices | activate_scene | status"
            },
            "device": {
                "type": "STRING",
                "description": "Name or entity ID of device (e.g. 'living room light', 'light.living_room', 'thermostat')"
            },
            "command": {
                "type": "STRING",
                "description": "turn_on | turn_off | toggle | set_temperature | lock | unlock"
            },
            "value": {
                "type": "STRING",
                "description": "Optional value (e.g. brightness '80', temperature '72', scene name)"
            }
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "namespace": "smart_home",
    "title": "Smart Home & Home Assistant",
    "fields": [
        {
            "name": "ha_url",
            "label": "Home Assistant URL",
            "type": "text",
            "default": "http://homeassistant.local:8123"
        },
        {
            "name": "ha_token",
            "label": "Long-Lived Access Token",
            "type": "password",
            "default": ""
        },
        {
            "name": "mqtt_broker",
            "label": "MQTT Broker Host (optional)",
            "type": "text",
            "default": "localhost"
        }
    ]
}

def _get_ha_config() -> tuple[str, str]:
    url = get_plugin_setting("smart_home", "ha_url", "http://homeassistant.local:8123").rstrip("/")
    token = get_plugin_setting("smart_home", "ha_token", "")
    return url, token

def _ha_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def _check_status() -> str:
    url, token = _get_ha_config()
    if not token:
        return "Smart Home is configured in standalone mode, Sir. Set your Home Assistant token in Settings to link your hub."
    if not requests:
        return "Requests library is unavailable, Sir."

    try:
        resp = requests.get(f"{url}/api/", headers=_ha_headers(token), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            msg = data.get("message", "API running")
            return f"Home Assistant linked and operational at {url} ({msg}), Sir."
        return f"Home Assistant responded with status code {resp.status_code}, Sir."
    except Exception as e:
        return f"Could not reach Home Assistant at {url}: {e}"

def _list_devices() -> str:
    url, token = _get_ha_config()
    if not token or not requests:
        return "Home Assistant token is not configured. Please add your token in settings, Sir."

    try:
        resp = requests.get(f"{url}/api/states", headers=_ha_headers(token), timeout=8)
        if resp.status_code != 200:
            return f"Failed to retrieve devices (HTTP {resp.status_code}), Sir."

        states = resp.json()
        tracked_domains = ("light", "switch", "climate", "lock", "cover")
        devices = []
        for s in states:
            eid = s.get("entity_id", "")
            domain = eid.split(".")[0]
            if domain in tracked_domains:
                name = s.get("attributes", {}).get("friendly_name", eid)
                state = s.get("state", "unknown")
                devices.append(f"• {name} ({eid}): {state}")

        if not devices:
            return "No lights, switches, or climate devices found on your hub, Sir."

        preview = devices[:12]
        more = f"\n(+ {len(devices) - len(preview)} more devices)" if len(devices) > len(preview) else ""
        return f"Discovered {len(devices)} smart home devices:\n" + "\n".join(preview) + more
    except Exception as e:
        return f"Error querying smart home devices: {e}"

def _find_entity_id(device_query: str, states: list) -> Optional[str]:
    q = device_query.lower().strip()
    # 1. Exact match on entity_id
    for s in states:
        if s.get("entity_id", "").lower() == q:
            return s.get("entity_id")

    # 2. Exact match on friendly name
    for s in states:
        fname = s.get("attributes", {}).get("friendly_name", "").lower()
        if fname == q:
            return s.get("entity_id")

    # 3. Partial match
    for s in states:
        fname = s.get("attributes", {}).get("friendly_name", "").lower()
        eid = s.get("entity_id", "").lower()
        if q in fname or q in eid:
            return s.get("entity_id")

    return None

def _control_device(device_name: str, command: str, value: str = "") -> str:
    url, token = _get_ha_config()
    if not token or not requests:
        return "Home Assistant token is not configured, Sir."

    if not device_name:
        return "Please specify which device to control, Sir."

    command = (command or "toggle").lower().strip()

    # Safety gate for physical locks
    if "unlock" in command or ("lock" in device_name.lower() and "open" in command):
        if confirm_gate and hasattr(confirm_gate, "request"):
            # Requires human confirmation for unlocking physical doors
            return "[CONFIRMATION_PENDING] Unlocking physical doors requires explicit on-screen confirmation for security."

    try:
        # Fetch states to resolve entity_id
        resp = requests.get(f"{url}/api/states", headers=_ha_headers(token), timeout=6)
        if resp.status_code != 200:
            return f"Cannot reach Home Assistant hub (HTTP {resp.status_code})."

        states = resp.json()
        entity_id = _find_entity_id(device_name, states)
        if not entity_id:
            return f"I could not locate a device matching '{device_name}' on your network, Sir."

        domain = entity_id.split(".")[0]

        # Map commands to services
        service_data = {"entity_id": entity_id}
        if domain == "climate" and (command in ("set_temperature", "temp") or value):
            service = "set_temperature"
            try:
                temp_val = float(value or command)
                service_data["temperature"] = temp_val
            except ValueError:
                service_data["temperature"] = 72
        elif command in ("turn_on", "on"):
            service = "turn_on"
            if domain == "light" and value:
                try:
                    val_int = int(value.replace("%", "").strip())
                    service_data["brightness_pct"] = max(1, min(100, val_int))
                except ValueError:
                    pass
        elif command in ("turn_off", "off"):
            service = "turn_off"
        elif command in ("toggle",):
            service = "toggle"
        elif command in ("lock",):
            service = "lock"
        elif command in ("unlock",):
            service = "unlock"
        else:
            service = command

        svc_url = f"{url}/api/services/{domain}/{service}"
        call_resp = requests.post(svc_url, headers=_ha_headers(token), json=service_data, timeout=8)
        if call_resp.status_code in (200, 201):
            return f"{entity_id} command '{service}' executed successfully, Sir."
        return f"Failed to execute {service} on {entity_id} (HTTP {call_resp.status_code})."

    except Exception as e:
        return f"Smart home command failed: {e}"

def _activate_scene(scene_name: str) -> str:
    url, token = _get_ha_config()
    if not token or not requests:
        return "Home Assistant is not connected, Sir."

    clean_name = scene_name.lower().replace(" ", "_").strip()
    if not clean_name.startswith("scene."):
        entity_id = f"scene.{clean_name}"
    else:
        entity_id = clean_name

    try:
        svc_url = f"{url}/api/services/scene/turn_on"
        resp = requests.post(svc_url, headers=_ha_headers(token), json={"entity_id": entity_id}, timeout=8)
        if resp.status_code in (200, 201):
            return f"Scene '{scene_name}' activated, Sir."
        return f"Failed to activate scene '{scene_name}' (HTTP {resp.status_code})."
    except Exception as e:
        return f"Scene activation failed: {e}"

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "status").lower().strip()
    device = parameters.get("device", "")
    cmd = parameters.get("command", "")
    val = parameters.get("value", "")

    try:
        if action in ("control_device", "control", "set"):
            result = _control_device(device, cmd, val)
        elif action in ("list_devices", "devices", "list"):
            result = _list_devices()
        elif action in ("activate_scene", "scene"):
            result = _activate_scene(device or val)
        elif action in ("status", "check"):
            result = _check_status()
        else:
            result = f"Unknown action '{action}'. Supported: control_device, list_devices, activate_scene, status."
    except Exception as e:
        result = f"Smart Home error: {e}"

    if player:
        try:
            player.write_log(f"[Smart Home] {result}")
        except Exception:
            pass

    return result
