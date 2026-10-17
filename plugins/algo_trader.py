import os
import sys
import json
import time
import threading
from pathlib import Path
from typing import Dict, Any, Optional

# Loaded by file path, so the project root has to be on sys.path before core is
# importable (same pattern plugins/video_strategy.py uses).
_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

try:
    from core import confirm as confirm_gate
except ImportError:
    confirm_gate = None

try:
    import ccxt
except ImportError:
    ccxt = None

PLUGIN = {
    "name": "algo_trader",
    "description": (
        "Execute cryptocurrency trades on Binance exchange. "
        "Supports market orders, limit orders, stop-loss, and take-profit. "
        "Can run automated trading strategies loaded from the strategies/ folder. "
        "Has paper trading mode (default ON) for safe testing. "
        "Use when user wants to buy, sell, trade crypto, check balance, "
        "manage positions, run a strategy, or check trade history. "
        "ALWAYS starts in paper trading mode for safety."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "buy | sell | balance | positions | history | "
                    "start_strategy | stop_strategy | strategy_status | "
                    "set_paper_mode | pnl"
                )
            },
            "symbol": {
                "type": "STRING",
                "description": "Trading pair e.g. BTC/USDT, ETH/USDT"
            },
            "amount": {
                "type": "NUMBER",
                "description": "Amount to buy/sell (in base currency)"
            },
            "price": {
                "type": "NUMBER",
                "description": "Limit price (optional, market order if omitted)"
            },
            "order_type": {
                "type": "STRING",
                "description": "market | limit (default: market)"
            },
            "stop_loss": {
                "type": "NUMBER",
                "description": "Stop loss percentage e.g. 2 for 2%"
            },
            "take_profit": {
                "type": "NUMBER",
                "description": "Take profit percentage e.g. 5 for 5%"
            },
            "strategy_name": {
                "type": "STRING",
                "description": "Name of strategy file in strategies/ folder"
            },
            "mode": {
                "type": "STRING",
                "description": "on | off (for set_paper_mode)"
            },
        },
        "required": ["action"],
    },
}

MEMORY_DIR = "memory"
PAPER_TRADES_FILE = os.path.join(MEMORY_DIR, "paper_trades.json")
CONFIG_DIR = "config"
API_KEYS_FILE = os.path.join(CONFIG_DIR, "api_keys.json")
STRATEGIES_DIR = "strategies"

_stop_events: Dict[str, threading.Event] = {}
_strategy_threads: Dict[str, threading.Thread] = {}

def _init_storage():
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(STRATEGIES_DIR, exist_ok=True)
    if not os.path.exists(PAPER_TRADES_FILE):
        default_state = {
            "paper_mode": True,
            "paper_balance": {"USDT": 10000.0},
            "paper_positions": {},
            "trade_history": [],
            "strategies_running": []
        }
        with open(PAPER_TRADES_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_state, f, indent=4)

def _load_paper_state() -> dict:
    _init_storage()
    with open(PAPER_TRADES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def _save_paper_state(state: dict):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(PAPER_TRADES_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=4)

def _load_config() -> dict:
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _get_exchange(paper=False):
    if ccxt is None:
        raise RuntimeError("ccxt library is not installed. Please install it using 'pip install ccxt'")
    
    if paper:
        return None
    
    config = _load_config()
    api_key = config.get('binance_api_key', '')
    api_secret = config.get('binance_api_secret', '')
    
    if not api_key or not api_secret:
        raise RuntimeError(
            "Binance API key not configured. Add 'binance_api_key' and "
            "'binance_api_secret' to config/api_keys.json"
        )
    
    return ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def _get_current_price(symbol: str) -> float:
    if ccxt is None:
        raise RuntimeError("ccxt library is not installed.")
    exchange = ccxt.binance({'enableRateLimit': True})
    ticker = exchange.fetch_ticker(symbol)
    return ticker['last']

def _run_strategy(strategy_name: str, strategy_data: dict, exchange, paper_mode: bool):
    _stop_events[strategy_name] = threading.Event()
    stop_event = _stop_events[strategy_name]
    
    while not stop_event.is_set():
        try:
            # Here you would check market data, strategy rules, and execute trades
            # Placeholder for actual strategy execution
            time.sleep(60)
        except Exception as e:
            print(f"Strategy {strategy_name} error: {str(e)}")
            time.sleep(60)
            
    print(f"Strategy {strategy_name} stopped.")

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action")
    if not action:
        return "Error: 'action' parameter is required."

    _init_storage()
    state = _load_paper_state()
    paper_mode = state.get("paper_mode", True)

    try:
        if action == "set_paper_mode":
            mode = parameters.get("mode", "").lower()
            if mode == "on":
                state["paper_mode"] = True
                _save_paper_state(state)
                return "Paper trading mode has been turned ON."
            elif mode == "off":
                state["paper_mode"] = False
                _save_paper_state(state)
                return "Paper trading mode has been turned OFF. Warning: Live trades will use real funds."
            else:
                return "Error: mode must be 'on' or 'off'."

        elif action == "balance":
            if paper_mode:
                balance = state.get("paper_balance", {})
                return f"Paper Balance: {json.dumps(balance, indent=2)}"
            else:
                exchange = _get_exchange(paper=False)
                balance = exchange.fetch_balance()
                # filter non-zero
                non_zero = {k: v for k, v in balance.get('total', {}).items() if v > 0}
                return f"Live Binance Balance: {json.dumps(non_zero, indent=2)}"

        elif action == "buy" or action == "sell":
            symbol = parameters.get("symbol")
            amount = parameters.get("amount")
            order_type = parameters.get("order_type", "market")
            price = parameters.get("price")
            
            if not symbol or not amount:
                return "Error: 'symbol' and 'amount' are required for trading."
                
            amount = float(amount)
            
            if paper_mode:
                current_price = _get_current_price(symbol)
                trade_price = price if (order_type == "limit" and price) else current_price
                cost = amount * trade_price
                
                base_asset, quote_asset = symbol.split('/')
                
                if action == "buy":
                    if state["paper_balance"].get(quote_asset, 0) < cost:
                        return f"Error: Insufficient {quote_asset} balance for paper trade."
                    state["paper_balance"][quote_asset] -= cost
                    state["paper_balance"][base_asset] = state["paper_balance"].get(base_asset, 0) + amount
                else: # sell
                    if state["paper_balance"].get(base_asset, 0) < amount:
                        return f"Error: Insufficient {base_asset} balance for paper trade."
                    state["paper_balance"][base_asset] -= amount
                    state["paper_balance"][quote_asset] = state["paper_balance"].get(quote_asset, 0) + cost
                    
                trade_record = {
                    "action": action,
                    "symbol": symbol,
                    "amount": amount,
                    "price": trade_price,
                    "cost": cost,
                    "time": time.time(),
                    "type": order_type
                }
                state["trade_history"].append(trade_record)
                _save_paper_state(state)
                
                return f"Paper trade executed: {action.upper()} {amount} {symbol} at {trade_price} {quote_asset} (Total: {cost} {quote_asset})"
                
            else:
                # A live order moves real money and cannot be un-sent, so it is
                # parked behind the HUD gate: the user presses CONFIRM, or the
                # order is never built. The model cannot write its own permission
                # slip — the old flag in session_memory let it.
                if confirm_gate is None:
                    return ("Error: the confirmation gate is unavailable, so the live "
                            f"order ({action.upper()} {amount} {symbol}) was NOT sent.")

                def _send_live_order(a=action, s=symbol, t=order_type, q=amount, p=price):
                    exchange = _get_exchange(paper=False)
                    order = exchange.create_order(s, t, a, q, p)
                    return f"Live trade executed: {json.dumps(order, indent=2)}"

                # No pre-check for a banner already on screen: core/confirm.py
                # queues this order behind it and refuses an exact repeat by key.
                return confirm_gate.request(
                    key=f"live_{action}_{symbol}",
                    title=f"Live {action.upper()}: {amount} {symbol}",
                    detail=f"{order_type} order with real funds — this cannot be undone.",
                    run=_send_live_order,
                )

        elif action == "positions":
            if paper_mode:
                return f"Paper Positions: {json.dumps(state.get('paper_balance', {}), indent=2)}"
            else:
                exchange = _get_exchange(paper=False)
                balance = exchange.fetch_balance()
                non_zero = {k: v for k, v in balance.get('total', {}).items() if v > 0}
                return f"Live Positions: {json.dumps(non_zero, indent=2)}"

        elif action == "history":
            if paper_mode:
                return f"Paper Trade History: {json.dumps(state.get('trade_history', []), indent=2)}"
            else:
                exchange = _get_exchange(paper=False)
                symbol = parameters.get("symbol")
                if symbol:
                    trades = exchange.fetch_my_trades(symbol)
                    return f"Live Trade History for {symbol}: {json.dumps(trades, indent=2)}"
                else:
                    return "Error: 'symbol' is required to fetch live trade history."

        elif action == "pnl":
            if paper_mode:
                history = state.get('trade_history', [])
                total_pnl = 0
                return f"Paper PnL calculation is simplified. Currently {len(history)} trades exist."
            else:
                return "Live PnL calculation requires fetching all trades and is currently not fully implemented."

        elif action == "start_strategy":
            # _run_strategy is a placeholder loop — it reads no market data and
            # places no orders. Reporting "Started" would tell the user a bot was
            # trading when nothing was, so refuse instead.
            return json.dumps({"error": "Trading strategy execution is not implemented yet"})

        elif action == "stop_strategy":
            strategy_name = parameters.get("strategy_name")
            if not strategy_name:
                return "Error: 'strategy_name' is required."
                
            if strategy_name in _stop_events:
                _stop_events[strategy_name].set()
                if strategy_name in state.get("strategies_running", []):
                    state["strategies_running"].remove(strategy_name)
                    _save_paper_state(state)
                return f"Stopped strategy '{strategy_name}'."
            else:
                return f"Strategy '{strategy_name}' is not currently running."

        elif action == "strategy_status":
            running = state.get("strategies_running", [])
            active_threads = [name for name, t in _strategy_threads.items() if t.is_alive()]
            return f"Strategies marked as running: {running}. Active threads: {active_threads}"

        else:
            return f"Unknown action: {action}"

    except Exception as e:
        return f"Error executing {action}: {str(e)}"
