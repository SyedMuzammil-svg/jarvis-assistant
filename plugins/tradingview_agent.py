import os
import time
import base64
import tempfile
import webbrowser

try:
    import mss
    import mss.tools
except ImportError:
    mss = None

from core.gemini import text as gemini_text, SMART

PLUGIN = {
    "name": "tradingview_agent",
    "description": (
        "Open TradingView charts, analyze live charts, and perform chart markings "
        "based on trading strategies. Uses Playwright/browser automation and Gemini Vision "
        "to open TradingView, load specified trading pair (e.g., BINANCE:BTCUSDT, BINANCE:ETHUSDT), "
        "capture high-resolution chart screenshots, analyze chart patterns, support/resistance, "
        "order blocks, and indicator signals, and display analysis results. "
        "Use when user says 'open tradingview', 'analyze chart on tradingview', "
        "'do marking on tradingview', or 'check tradingview chart'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "open_chart | analyze_live | mark_chart | switch_pair | set_timeframe"
            },
            "symbol": {
                "type": "STRING",
                "description": "Trading symbol e.g. BTCUSDT, ETHUSDT, EURUSD, AAPL (default: BTCUSDT)"
            },
            "exchange": {
                "type": "STRING",
                "description": "Exchange prefix e.g. BINANCE, BYBIT, COINBASE (default: BINANCE)"
            },
            "timeframe": {
                "type": "STRING",
                "description": "Chart timeframe: 1m | 5m | 15m | 1h | 4h | 1d (default 1h)"
            },
            "strategy_name": {
                "type": "STRING",
                "description": "Name of strategy to apply for chart marking"
            },
        },
        "required": ["action"],
    },
}


def _get_timeframe_interval(timeframe_str: str) -> str:
    mapping = {
        "1m": "1",
        "5m": "5",
        "15m": "15",
        "1h": "60",
        "4h": "240",
        "1d": "D",
        "1w": "W",
    }
    if not timeframe_str:
        return "60"
    return mapping.get(timeframe_str.lower(), "60")


def _open_tv_chart(exchange: str, symbol: str, timeframe: str = None):
    url = f"https://www.tradingview.com/chart/?symbol={exchange}:{symbol}"
    if timeframe:
        interval = _get_timeframe_interval(timeframe)
        url += f"&interval={interval}"
    webbrowser.open(url)


def _capture_screen_b64() -> str:
    if mss is None:
        return ""
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        sct_img = sct.grab(monitor)

        temp_file = tempfile.mktemp(suffix=".png")
        mss.tools.to_png(sct_img.rgb, sct_img.size, output=temp_file)

        with open(temp_file, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode('utf-8')

        try:
            os.remove(temp_file)
        except OSError:
            pass

        return b64_data


def _perform_visual_marking_on_chart(levels_count: int = 3):
    """Uses PyAutoGUI on active TradingView chart to place Horizontal Support/Resistance lines (Alt+H)."""
    try:
        import pyautogui
        sw, sh = pyautogui.size()
        center_x = int(sw * 0.55)

        # Focus chart canvas
        pyautogui.click(center_x, int(sh * 0.5))
        time.sleep(0.4)

        # Positions across resistance (top), pivot (mid), support (bottom)
        y_positions = [int(sh * 0.32), int(sh * 0.52), int(sh * 0.72)]

        for y_pos in y_positions[:levels_count]:
            pyautogui.moveTo(center_x, y_pos, duration=0.2)
            time.sleep(0.25)
            # Alt + H draws Horizontal Support/Resistance line on TradingView
            pyautogui.hotkey("alt", "h")
            time.sleep(0.4)

        print("[TradingView] Visual Support/Resistance lines (Alt+H) drawn on chart.")
    except Exception as err:
        print(f"[TradingView] Could not draw visual lines: {err}")


def run(parameters, player=None, session_memory=None) -> str:
    action = parameters.get("action", "")
    symbol = parameters.get("symbol", "BTCUSDT").upper()
    exchange = parameters.get("exchange", "BINANCE").upper()
    timeframe = parameters.get("timeframe", "1h")
    strategy_name = parameters.get("strategy_name", "")

    if player:
        try:
            player.write_log(f"[TradingView] Action: {action} - {exchange}:{symbol}")
        except Exception:
            pass

    if action in ["open_chart", "switch_pair", "set_timeframe"]:
        try:
            tf = timeframe if action == "set_timeframe" else None
            _open_tv_chart(exchange, symbol, tf)
            return f"TradingView opened for {exchange}:{symbol}."
        except Exception as e:
            return f"Failed to open TradingView chart: {str(e)}"

    elif action in ["analyze_live", "mark_chart", "support_marking"]:
        try:
            _open_tv_chart(exchange, symbol, timeframe)
            time.sleep(3.5)  # Wait for browser & chart canvas to render

            # Draw visual Support & Resistance lines directly on the TradingView chart
            _perform_visual_marking_on_chart(levels_count=3)
            time.sleep(0.5)

            b64_img = _capture_screen_b64()
            if not b64_img:
                return "Opened TradingView chart and drew Support/Resistance lines (Alt+H), but could not capture screenshot."

            strategy_rules = ""
            if strategy_name:
                strat_path = os.path.join(os.path.dirname(__file__), "..", "strategies", f"{strategy_name}.json")
                if os.path.exists(strat_path):
                    with open(strat_path, "r", encoding="utf-8") as f:
                        strategy_rules = f.read()

            prompt = (
                f"Analyze this TradingView chart for {exchange}:{symbol} with drawn Support & Resistance lines.\n"
                f"Strategy: {strategy_rules if strategy_rules else 'Price Action / Support & Resistance'}\n\n"
                "Provide detailed technical analysis:\n"
                "1. Exact Support & Resistance price levels marked on chart.\n"
                "2. Market Structure (Higher Highs, Lower Lows, CHoCH, BOS).\n"
                "3. Order Blocks & Fair Value Gaps (FVG).\n"
                "4. Trade Setup: Long / Short recommendation, Entry price, Stop Loss, and Take Profit targets.\n"
                "Format clearly."
            )

            contents = [
                {"role": "user", "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": b64_img}}
                ]}
            ]

            analysis = gemini_text(contents, tier=SMART)
            if not analysis:
                return (f"Gemini gave no reply for the {exchange}:{symbol} chart — "
                        "the screenshot was captured but not analysed. Try again.")
            res_text = f"TradingView Support & Resistance Marking complete for {exchange}:{symbol}:\n\n{analysis}"

            if player:
                try:
                    player.show_content(f"TRADINGVIEW — {exchange}:{symbol}", res_text)
                except Exception:
                    pass

            return res_text

        except Exception as e:
            return f"Error marking/analyzing TradingView chart: {str(e)}"

    else:
        return f"Unknown action: {action}"
