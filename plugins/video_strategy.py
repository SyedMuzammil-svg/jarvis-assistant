import sys
import json
import os
from datetime import datetime
from pathlib import Path
import logging

_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

try:
    from core import gemini
    from google.genai import types as genai_types
except ImportError:
    gemini = None
    genai_types = None

try:
    import cv2
except ImportError:
    cv2 = None

STRATEGIES_DIR = _BASE / "strategies"

PLUGIN = {
    "name": "video_strategy",
    "description": (
        "Analyze a trading course video to extract trading strategies. "
        "Give it a video file path and it will watch the video, analyze charts, "
        "indicators, and text to extract buy/sell rules, entry/exit conditions, "
        "and risk management rules. Use when user says 'analyze this trading video', "
        "'learn strategy from video', or 'extract strategy from this video'. "
        "Also use 'list' to see all learned strategies, 'show' to see details, "
        "'delete' to remove a strategy."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "analyze | list | show | delete"
            },
            "video_path": {
                "type": "STRING",
                "description": "Full path to the trading video file"
            },
            "strategy_name": {
                "type": "STRING",
                "description": "Name for the strategy (for show/delete)"
            },
            "frame_interval": {
                "type": "NUMBER",
                "description": "Seconds between frame captures (default 15)"
            },
            "max_frames": {
                "type": "NUMBER",
                "description": "Maximum frames to analyze (default 20)"
            },
        },
        "required": ["action"],
    },
}

def analyze_video(video_path: str, strategy_name: str, frame_interval: int, max_frames: int):
    if not cv2:
        return "Error: cv2 (opencv-python) is not installed. Please tell the user to install opencv-python to use this feature."
    if not gemini:
        return "Error: Could not import core.gemini module."
        
    if not os.path.exists(video_path):
        return f"Error: Video file not found at {video_path}."
        
    if not strategy_name:
        strategy_name = f"strategy_{int(datetime.now().timestamp())}"
        
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return f"Error: Failed to open video file {video_path}."
        
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0:
        fps = 30 # fallback
        
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    interval_frames = int(fps * frame_interval)
    
    analyzed_data = []
    
    current_frame = 0
    frames_processed = 0
    
    while frames_processed < max_frames and current_frame < total_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, current_frame)
        ret, frame = cap.read()
        if not ret:
            break
            
        # encode frame to jpeg
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            current_frame += interval_frames
            continue
            
        jpeg_bytes = buffer.tobytes()
        image_part = genai_types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")
        prompt = """
Analyze this trading chart/video frame. Extract:
1. What indicators are shown (RSI, MACD, EMA, SMA, Bollinger, etc.) with their settings
2. Any buy/sell signals or rules mentioned
3. Entry conditions
4. Exit conditions (stop loss, take profit)
5. Timeframe shown
6. Any text/annotations about the strategy
7. Chart patterns (head & shoulders, double top, etc.)

Return as JSON:
{
    "indicators": [{"name": "...", "period": ..., "value": ...}],
    "entry_rules": ["..."],
    "exit_rules": ["..."],
    "timeframe": "...",
    "patterns": ["..."],
    "annotations": ["..."],
    "confidence": 0.0-1.0
}
"""
        try:
            response_text = gemini.text([prompt, image_part], tier=gemini.SMART,
                                        timeout_ms=30000, default="")
            
            import re
            json_match = re.search(r'```(?:json)?\n(.*?)\n```', response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_str = response_text
                
            try:
                frame_data = json.loads(json_str)
                analyzed_data.append(frame_data)
            except json.JSONDecodeError:
                pass
                
        except Exception as e:
            logging.error(f"Error calling gemini: {e}")
            
        frames_processed += 1
        current_frame += interval_frames
        
    cap.release()
    
    if not analyzed_data:
        return "I analyzed the video but could not extract any structured trading strategy data. It may not contain clear trading content or the analysis failed."
        
    compiled_strategy = {
        "name": strategy_name,
        "source_video": video_path,
        "created_at": datetime.now().isoformat(),
        "frames_analyzed": frames_processed,
        "indicators": [],
        "entry_rules": [],
        "exit_rules": [],
        "risk_management": {},
        "timeframe": "",
        "patterns": [],
        "annotations": [],
        "confidence": 0.0,
        "raw_analysis": analyzed_data
    }
    
    for data in analyzed_data:
        if "indicators" in data and isinstance(data["indicators"], list):
            for ind in data["indicators"]:
                if ind not in compiled_strategy["indicators"]:
                    compiled_strategy["indicators"].append(ind)
        if "entry_rules" in data and isinstance(data["entry_rules"], list):
            compiled_strategy["entry_rules"].extend(data["entry_rules"])
        if "exit_rules" in data and isinstance(data["exit_rules"], list):
            compiled_strategy["exit_rules"].extend(data["exit_rules"])
        if "patterns" in data and isinstance(data["patterns"], list):
            compiled_strategy["patterns"].extend(data["patterns"])
        if "annotations" in data and isinstance(data["annotations"], list):
            compiled_strategy["annotations"].extend(data["annotations"])
            
    compiled_strategy["entry_rules"] = list(set(str(r) for r in compiled_strategy["entry_rules"]))
    compiled_strategy["exit_rules"] = list(set(str(r) for r in compiled_strategy["exit_rules"]))
    compiled_strategy["patterns"] = list(set(str(r) for r in compiled_strategy["patterns"]))
    compiled_strategy["annotations"] = list(set(str(r) for r in compiled_strategy["annotations"]))
    
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
    file_path = STRATEGIES_DIR / f"{strategy_name}.json"
    
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(compiled_strategy, f, indent=4)
        
    return f"Successfully extracted strategy '{strategy_name}' from video. Analyzed {frames_processed} frames. Found {len(compiled_strategy['indicators'])} indicators and {len(compiled_strategy['entry_rules'])} entry rules. Strategy saved."

def list_strategies():
    if not STRATEGIES_DIR.exists():
        return "No strategies have been extracted yet."
        
    strategies = []
    for file in STRATEGIES_DIR.glob("*.json"):
        strategies.append(file.stem)
        
    if not strategies:
        return "No strategies have been extracted yet."
        
    return "Saved strategies:\n" + "\n".join(f"- {s}" for s in strategies)

def show_strategy(strategy_name: str):
    if not strategy_name:
        return "Please provide a strategy name to show."
        
    file_path = STRATEGIES_DIR / f"{strategy_name}.json"
    if not file_path.exists():
        return f"Strategy '{strategy_name}' not found."
        
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    out = f"Strategy: {data.get('name')}\n"
    out += f"Source Video: {data.get('source_video')}\n"
    out += f"Indicators: {json.dumps(data.get('indicators'), indent=2)}\n"
    out += f"Entry Rules:\n" + "\n".join(f" - {r}" for r in data.get('entry_rules', [])) + "\n"
    out += f"Exit Rules:\n" + "\n".join(f" - {r}" for r in data.get('exit_rules', [])) + "\n"
    return out

def delete_strategy(strategy_name: str):
    if not strategy_name:
        return "Please provide a strategy name to delete."
        
    file_path = STRATEGIES_DIR / f"{strategy_name}.json"
    if not file_path.exists():
        return f"Strategy '{strategy_name}' not found."
        
    try:
        file_path.unlink()
        return f"Strategy '{strategy_name}' has been deleted."
    except Exception as e:
        return f"Error deleting strategy: {e}"

def run(parameters, player=None, session_memory=None):
    action = parameters.get("action")
    
    if action == "analyze":
        video_path = parameters.get("video_path")
        if not video_path:
            return "Please provide a video path to analyze."
        strategy_name = parameters.get("strategy_name")
        frame_interval = parameters.get("frame_interval", 15)
        max_frames = parameters.get("max_frames", 20)
        return analyze_video(video_path, strategy_name, frame_interval, max_frames)
    elif action == "list":
        return list_strategies()
    elif action == "show":
        strategy_name = parameters.get("strategy_name")
        return show_strategy(strategy_name)
    elif action == "delete":
        strategy_name = parameters.get("strategy_name")
        return delete_strategy(strategy_name)
    else:
        return f"Unknown action: {action}"
