"""
Telegram Bot Remote Plugin for JARVIS.
Allows sending messages, system alerts, screenshots, and checking incoming commands via Telegram Bot API.
Uses standard `requests` library — no external python-telegram-bot dependency needed.
"""

import sys
import json
import base64
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

try:
    import requests
except ImportError:
    requests = None

from memory.config_manager import get_plugin_setting, save_plugin_config, get_plugin_config

PLUGIN = {
    "name": "telegram_bot",
    "description": (
        "Send and receive Telegram messages, notifications, and screenshots. "
        "Allows JARVIS to report status remotely to your phone via Telegram. "
        "Use when user says 'send to Telegram', 'notify my phone', 'telegram me', "
        "or 'check my Telegram messages'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "send_message | send_screenshot | get_updates | status"
            },
            "message": {
                "type": "STRING",
                "description": "Message text to send"
            },
            "chat_id": {
                "type": "STRING",
                "description": "Target Telegram Chat ID (optional if configured in settings)"
            },
            "caption": {
                "type": "STRING",
                "description": "Caption for photos or screenshots (optional)"
            }
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "namespace": "telegram_bot",
    "title": "Telegram Remote Control",
    "fields": [
        {
            "name": "bot_token",
            "label": "Telegram Bot Token (from @BotFather)",
            "type": "password",
            "default": ""
        },
        {
            "name": "chat_id",
            "label": "Your Telegram Chat ID",
            "type": "text",
            "default": ""
        },
        {
            "name": "allowed_users",
            "label": "Allowed User IDs (comma-separated)",
            "type": "text",
            "default": ""
        }
    ]
}

def _get_token(params: dict) -> str:
    token = params.get("bot_token")
    if not token:
        token = get_plugin_setting("telegram_bot", "bot_token", "")
    return str(token).strip()

def _get_chat_id(params: dict) -> str:
    cid = params.get("chat_id")
    if not cid:
        cid = get_plugin_setting("telegram_bot", "chat_id", "")
    return str(cid).strip()

def _api_call(token: str, method: str, data: dict = None, files: dict = None, timeout: int = 15) -> dict:
    if not requests:
        return {"ok": False, "description": "requests library is not installed"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if files:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            resp = requests.post(url, json=data or {}, timeout=timeout)
        return resp.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}

def _action_status(token: str) -> str:
    if not token:
        return "Telegram bot token is not configured, Sir. Please configure it in Settings -> Plugins -> Telegram."
    res = _api_call(token, "getMe")
    if res.get("ok"):
        bot_info = res.get("result", {})
        username = bot_info.get("username", "Unknown")
        first_name = bot_info.get("first_name", "Bot")
        return f"Telegram Bot is online, Sir. Connected as @{username} ({first_name})."
    return f"Failed to connect to Telegram: {res.get('description', 'Unknown error')}"

def _action_send_message(token: str, chat_id: str, message: str) -> str:
    if not token:
        return "Telegram bot token is missing, Sir."
    if not chat_id:
        return "Telegram Chat ID is missing. Please provide chat_id or set it in plugin settings, Sir."
    if not message:
        return "Cannot send an empty message to Telegram, Sir."

    res = _api_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    })
    if res.get("ok"):
        return f"Message dispatched to Telegram, Sir."
    return f"Failed to send message: {res.get('description', 'Unknown error')}"

def _action_send_screenshot(token: str, chat_id: str, caption: str) -> str:
    if not token or not chat_id:
        return "Telegram bot token and chat_id are required to send screenshots, Sir."
    
    # Grab screenshot using mss or screen_processor
    try:
        import mss
        from PIL import Image
        import io
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            sct_img = sct.grab(mon)
            img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            img.thumbnail((1920, 1080))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            buf.seek(0)
            
            files = {"photo": ("screenshot.jpg", buf, "image/jpeg")}
            data = {"chat_id": chat_id, "caption": caption or "Current Desktop Snapshot — JARVIS"}
            res = _api_call(token, "sendPhoto", data=data, files=files, timeout=25)
            if res.get("ok"):
                return "Desktop screenshot sent to your Telegram, Sir."
            return f"Telegram upload failed: {res.get('description', 'Unknown error')}"
    except Exception as e:
        return f"Failed to capture or send screenshot: {e}"

def _action_get_updates(token: str) -> str:
    if not token:
        return "Telegram bot token is not configured, Sir."
    res = _api_call(token, "getUpdates", {"limit": 5, "timeout": 5})
    if not res.get("ok"):
        return f"Failed to fetch Telegram updates: {res.get('description')}"

    updates = res.get("result", [])
    if not updates:
        return "No recent messages on Telegram, Sir."

    messages = []
    for u in updates[-5:]:
        msg = u.get("message", {})
        sender = msg.get("from", {}).get("first_name", "Unknown")
        text = msg.get("text", "[No text content]")
        messages.append(f"{sender}: {text}")

    return f"Here are the latest Telegram messages, Sir:\n" + "\n".join(messages)

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "status").lower().strip()
    token = _get_token(parameters)
    chat_id = _get_chat_id(parameters)
    message = parameters.get("message", "")
    caption = parameters.get("caption", "")

    try:
        if action == "status":
            result = _action_status(token)
        elif action in ("send_message", "send", "message"):
            result = _action_send_message(token, chat_id, message)
        elif action in ("send_screenshot", "screenshot", "screen"):
            result = _action_send_screenshot(token, chat_id, caption or message)
        elif action in ("get_updates", "messages", "read"):
            result = _action_get_updates(token)
        else:
            result = f"Unknown Telegram action '{action}'. Supported: status, send_message, send_screenshot, get_updates."
    except Exception as e:
        result = f"Telegram error: {e}"

    if player:
        try:
            player.write_log(f"[Telegram] {result}")
        except Exception:
            pass

    return result
