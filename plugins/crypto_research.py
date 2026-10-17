import json
import logging
import requests
from pathlib import Path
from datetime import datetime

try:
    import ccxt
    _CCXT = True
except ImportError:
    _CCXT = False

try:
    import pandas as pd
    import ta
    _TA = True
except ImportError:
    _TA = False

_BASE = Path(__file__).resolve().parent.parent
_CONFIG = _BASE / "config" / "api_keys.json"

PLUGIN = {
    "name": "crypto_research",
    "description": (
        "Research cryptocurrency markets using Binance and CoinMarketCap data. "
        "Get live prices, market cap rankings, volume analysis, historical data, "
        "technical indicators, and detailed coin information. "
        "Use when user asks about crypto prices, market analysis, coin research, "
        "top coins, market trends, or any cryptocurrency data. "
        "Supports: price check, market overview, coin details, technical analysis, "
        "price alerts, and comparison between coins."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "price | market_overview | coin_info | technical_analysis | "
                    "top_gainers | top_losers | compare | history | orderbook | "
                    "volume_analysis | fear_greed"
                )
            },
            "symbol": {
                "type": "STRING",
                "description": "Crypto symbol e.g. BTC, ETH, SOL or trading pair BTC/USDT"
            },
            "symbols": {
                "type": "STRING",
                "description": "Comma-separated symbols for compare action e.g. BTC,ETH,SOL"
            },
            "timeframe": {
                "type": "STRING",
                "description": "Candle timeframe: 1m|5m|15m|1h|4h|1d|1w (default 1h)"
            },
            "limit": {
                "type": "NUMBER",
                "description": "Number of results (default 10)"
            },
        },
        "required": ["action"],
    },
}

def _get_cmc_key():
    try:
        cfg = json.loads(_CONFIG.read_text(encoding='utf-8'))
        return cfg.get('coinmarketcap_api_key', '')
    except Exception:
        return ''

def _get_exchange():
    """Get Binance exchange instance. Works without API key for public data."""
    if not _CCXT:
        raise RuntimeError("ccxt not installed. Please install it to use this plugin.")
    return ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def _normalize_symbol(symbol: str) -> str:
    if not symbol:
        return ""
    symbol = symbol.upper().strip()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    return symbol

def _format_money(val) -> str:
    if val is None:
        return "N/A"
    try:
        fval = float(val)
        if fval < 0.01:
            return f"${fval:.6f}"
        return f"${fval:,.2f}"
    except (ValueError, TypeError):
        return str(val)

def _action_price(exchange, symbol):
    symbol = _normalize_symbol(symbol)
    ticker = exchange.fetch_ticker(symbol)
    
    price = ticker.get('last')
    change = ticker.get('percentage')
    vol = ticker.get('quoteVolume')
    high = ticker.get('high')
    low = ticker.get('low')
    
    return (f"The current price of {symbol} is {_format_money(price)}. "
            f"24h change is {change:.2f}%. "
            f"24h high: {_format_money(high)}, low: {_format_money(low)}. "
            f"24h volume: {_format_money(vol)}.")

def _action_market_overview(exchange, limit=10):
    cmc_key = _get_cmc_key()
    if cmc_key:
        try:
            url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"
            headers = {'X-CMC_PRO_API_KEY': cmc_key}
            params = {'start': '1', 'limit': str(limit), 'convert': 'USD'}
            res = requests.get(url, headers=headers, params=params).json()
            if res.get('status', {}).get('error_code') == 0:
                data = res['data']
                lines = [f"Market Overview (Top {limit} coins by Market Cap):"]
                for i, c in enumerate(data, 1):
                    quote = c['quote']['USD']
                    lines.append(f"{i}. {c['name']} ({c['symbol']}): {_format_money(quote['price'])} "
                                 f"| 24h: {quote['percent_change_24h']:.2f}% | Market Cap: {_format_money(quote['market_cap'])}")
                return "\n".join(lines)
        except Exception as e:
            logging.error(f"CMC error: {e}")
            
    # Fallback to ccxt
    tickers = exchange.fetch_tickers()
    usdt_pairs = [t for sym, t in tickers.items() if sym.endswith('/USDT') and t.get('quoteVolume') is not None]
    usdt_pairs.sort(key=lambda x: x['quoteVolume'], reverse=True)
    
    lines = [f"Market Overview (Top {limit} coins by Volume on Binance):"]
    for i, t in enumerate(usdt_pairs[:limit], 1):
        lines.append(f"{i}. {t['symbol']}: {_format_money(t['last'])} | 24h: {t.get('percentage', 0):.2f}% | Vol: {_format_money(t['quoteVolume'])}")
    return "\n".join(lines)

def _action_coin_info(exchange, symbol):
    # Mostly an alias for price if CMC is not available, but tries CMC if available
    sym_base = symbol.split('/')[0].upper() if '/' in symbol else symbol.upper()
    cmc_key = _get_cmc_key()
    if cmc_key:
        try:
            url = "https://pro-api.coinmarketcap.com/v2/cryptocurrency/quotes/latest"
            headers = {'X-CMC_PRO_API_KEY': cmc_key}
            params = {'symbol': sym_base, 'convert': 'USD'}
            res = requests.get(url, headers=headers, params=params).json()
            if res.get('status', {}).get('error_code') == 0:
                data = res['data'][sym_base][0]
                quote = data['quote']['USD']
                return (f"Coin Info for {data['name']} ({data['symbol']}):\n"
                        f"Price: {_format_money(quote['price'])}\n"
                        f"Market Cap Rank: {data.get('cmc_rank', 'N/A')}\n"
                        f"Market Cap: {_format_money(quote['market_cap'])}\n"
                        f"24h Change: {quote['percent_change_24h']:.2f}%\n"
                        f"7d Change: {quote['percent_change_7d']:.2f}%\n"
                        f"Circulating Supply: {data.get('circulating_supply', 'N/A'):,.0f} {data['symbol']}")
        except Exception as e:
            logging.error(f"CMC error: {e}")
    
    return _action_price(exchange, symbol)

def _action_technical_analysis(exchange, symbol, timeframe='1h', limit=100):
    if not _TA:
        return "Technical analysis requires the 'pandas' and 'ta' libraries. Please install them."
    symbol = _normalize_symbol(symbol)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    if not ohlcv:
        return f"Could not fetch OHLCV data for {symbol}."
        
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    
    # RSI
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    # MACD
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    # EMA
    df['ema20'] = ta.trend.EMAIndicator(df['close'], window=20).ema_indicator()
    df['ema50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
    # Bollinger Bands
    bb = ta.volatility.BollingerBands(df['close'])
    df['bb_upper'] = bb.bollinger_hband()
    df['bb_lower'] = bb.bollinger_lband()
    
    last = df.iloc[-1]
    
    # Simple signals
    rsi = last['rsi']
    rsi_sig = "Oversold (Buy)" if rsi < 30 else "Overbought (Sell)" if rsi > 70 else "Neutral"
    
    macd_val = last['macd']
    macd_sig_val = last['macd_signal']
    macd_sig = "Bullish (Buy)" if macd_val > macd_sig_val else "Bearish (Sell)"
    
    ema_sig = "Bullish" if last['ema20'] > last['ema50'] else "Bearish"
    
    return (f"Technical Analysis for {symbol} ({timeframe} chart):\n"
            f"Current Price: {_format_money(last['close'])}\n"
            f"RSI (14): {rsi:.2f} - {rsi_sig}\n"
            f"MACD: {macd_val:.2f} (Signal: {macd_sig_val:.2f}) - {macd_sig}\n"
            f"Trend (EMA 20/50): {ema_sig}\n"
            f"Bollinger Bands: Lower {_format_money(last['bb_lower'])}, Upper {_format_money(last['bb_upper'])}")

def _action_top_gainers_losers(exchange, limit=10, is_gainers=True):
    tickers = exchange.fetch_tickers()
    usdt_pairs = [t for sym, t in tickers.items() if sym.endswith('/USDT') and t.get('percentage') is not None and t.get('quoteVolume', 0) > 1000000]
    
    usdt_pairs.sort(key=lambda x: x['percentage'], reverse=is_gainers)
    
    label = "Top Gainers" if is_gainers else "Top Losers"
    lines = [f"{label} (Last 24h, Min 1M USDT Volume):"]
    for i, t in enumerate(usdt_pairs[:limit], 1):
        lines.append(f"{i}. {t['symbol']}: {_format_money(t['last'])} | Change: {t['percentage']:.2f}% | Vol: {_format_money(t['quoteVolume'])}")
    return "\n".join(lines)

def _action_compare(exchange, symbols):
    if not symbols:
        return "No symbols provided for comparison."
    
    sym_list = [s.strip() for s in symbols.split(',')]
    lines = ["Coin Comparison:"]
    
    for s in sym_list:
        norm_s = _normalize_symbol(s)
        try:
            ticker = exchange.fetch_ticker(norm_s)
            lines.append(f"- {norm_s}: {_format_money(ticker.get('last'))} | 24h Change: {ticker.get('percentage'):.2f}% | 24h Vol: {_format_money(ticker.get('quoteVolume'))}")
        except Exception as e:
            lines.append(f"- {norm_s}: Data unavailable ({str(e)})")
            
    return "\n".join(lines)

def _action_history(exchange, symbol, timeframe='1d', limit=5):
    symbol = _normalize_symbol(symbol)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    if not ohlcv:
        return f"Could not fetch historical data for {symbol}."
        
    lines = [f"Historical Data for {symbol} ({timeframe} candles):"]
    for candle in ohlcv:
        dt = datetime.fromtimestamp(candle[0] / 1000.0).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f"[{dt}] Open: {_format_money(candle[1])}, High: {_format_money(candle[2])}, "
                     f"Low: {_format_money(candle[3])}, Close: {_format_money(candle[4])}, Vol: {candle[5]:.2f}")
    return "\n".join(lines)

def _action_orderbook(exchange, symbol, limit=5):
    symbol = _normalize_symbol(symbol)
    ob = exchange.fetch_order_book(symbol, limit=limit)
    
    lines = [f"Order Book for {symbol}:"]
    lines.append("Top Asks (Selling):")
    for price, amt in ob['asks'][:limit]:
        lines.append(f"  Price: {_format_money(price)} | Amount: {amt}")
        
    lines.append("Top Bids (Buying):")
    for price, amt in ob['bids'][:limit]:
        lines.append(f"  Price: {_format_money(price)} | Amount: {amt}")
        
    return "\n".join(lines)

def _action_volume_analysis(exchange, symbol, timeframe='1h'):
    symbol = _normalize_symbol(symbol)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=24) # Last 24 periods
    if not ohlcv:
        return "Could not fetch data."
        
    vols = [c[5] for c in ohlcv[:-1]] # exclude current unfinished candle
    current_vol = ohlcv[-1][5]
    avg_vol = sum(vols) / len(vols) if vols else 0
    
    ratio = (current_vol / avg_vol) * 100 if avg_vol else 0
    
    return (f"Volume Analysis for {symbol} ({timeframe}):\n"
            f"Current period volume: {current_vol:.2f}\n"
            f"Average volume (last {len(vols)} periods): {avg_vol:.2f}\n"
            f"Volume is currently {ratio:.2f}% of the average.")

def _action_fear_greed():
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        res = requests.get(url).json()
        data = res['data'][0]
        val = data['value']
        cls = data['value_classification']
        updated = datetime.fromtimestamp(int(data['timestamp'])).strftime('%Y-%m-%d')
        return f"Crypto Fear & Greed Index (Updated {updated}):\nValue: {val} / 100\nClassification: {cls}"
    except Exception as e:
        return f"Could not fetch Fear & Greed Index: {e}"

def run(parameters, player=None, session_memory=None) -> str:
    action = parameters.get('action')
    symbol = parameters.get('symbol', 'BTC')
    symbols = parameters.get('symbols', 'BTC,ETH')
    timeframe = parameters.get('timeframe', '1h')
    limit = parameters.get('limit', 10)
    
    if player:
        player.write_log(f"crypto_research plugin called: action={action}, symbol={symbol}")
        
    if action == 'fear_greed':
        return _action_fear_greed()
        
    try:
        exchange = _get_exchange()
    except RuntimeError as e:
        return str(e)
        
    try:
        if action == 'price':
            return _action_price(exchange, symbol)
        elif action == 'market_overview':
            return _action_market_overview(exchange, limit)
        elif action == 'coin_info':
            return _action_coin_info(exchange, symbol)
        elif action == 'technical_analysis':
            return _action_technical_analysis(exchange, symbol, timeframe, limit=100)
        elif action == 'top_gainers':
            return _action_top_gainers_losers(exchange, limit, is_gainers=True)
        elif action == 'top_losers':
            return _action_top_gainers_losers(exchange, limit, is_gainers=False)
        elif action == 'compare':
            return _action_compare(exchange, symbols)
        elif action == 'history':
            return _action_history(exchange, symbol, timeframe, limit)
        elif action == 'orderbook':
            return _action_orderbook(exchange, symbol, limit)
        elif action == 'volume_analysis':
            return _action_volume_analysis(exchange, symbol, timeframe)
        else:
            return f"Unknown action: {action}"
    except Exception as e:
        logging.error(f"crypto_research error: {e}")
        return f"An error occurred while fetching crypto data: {str(e)}"
