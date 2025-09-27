#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Zaffex - Réplica exacta para CoinEx
- Cierre automático por TP/SL
- RSI corregido
- 200 velas para precisión
"""

import os
import time
import signal
import json
import csv
from datetime import datetime, timezone
import requests
import ccxt
import ta

RUNNING = True
STATE_FILE = "paper_state.json"
CSV_FILE = "operaciones.csv"

daily_stats = {
    'trades': [],
    'hourly_ops': {},
    'pnl_history': [],
    'mode_ops': {'agresivo': 0, 'moderado': 0, 'conservador': 0},
    'best_trade': 0,
    'worst_trade': 0,
    'start_equity': 235.0
}

def signal_handler(signum, frame):
    global RUNNING
    print(f"[SIGNAL] Apagando bot gracefully...")
    RUNNING = False

def load_env():
    from dotenv import load_dotenv
    load_dotenv()
    return {
        'EXCHANGE': os.getenv('EXCHANGE', 'coinex'),
        'API_KEY': os.getenv('API_KEY', ''),
        'API_SECRET': os.getenv('API_SECRET', ''),
        'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN', ''),
        'TELEGRAM_IDS': os.getenv('TELEGRAM_ALLOWED_IDS', ''),
        'SYMBOLS': os.getenv('SYMBOLS', 'BTC/USDT,ETH/USDT').split(','),
        'TIMEFRAME': os.getenv('TIMEFRAME', '1m'),
        'RSI_PERIOD': int(os.getenv('RSI_PERIOD', '9')),
        'RSI_THRESHOLD': float(os.getenv('RSI_THRESHOLD', '25')),
        'TP_PCT': float(os.getenv('TAKE_PROFIT_PCT', '1.0')),
        'SL_PCT': float(os.getenv('STOP_LOSS_PCT', '1.2')),
        'LOT_AGRESIVO': int(os.getenv('LOT_SIZE_AGRESIVO', '3')),
        'LOT_MODERADO': int(os.getenv('LOT_SIZE_MODERADO', '4')),
        'LOT_CONSERVADOR': int(os.getenv('LOT_SIZE_CONSERVADOR', '5')),
        'CAP_AGRESIVO': float(os.getenv('CAPITAL_AGRESIVO', '47')),
        'CAP_MODERADO': float(os.getenv('CAPITAL_MODERADO', '35')),
        'CAP_CONSERVADOR': float(os.getenv('CAPITAL_CONSERVADOR', '23')),
        'SIGNAL_COOLDOWN': int(os.getenv('SIGNAL_COOLDOWN', '300')),
    }

def send_telegram(message, config):
    if not config['TELEGRAM_TOKEN'] or not config['TELEGRAM_IDS']:
        return
    try:
        url = f"https://api.telegram.org/bot{config['TELEGRAM_TOKEN']}/sendMessage"
        for chat_id in config['TELEGRAM_IDS'].split(','):
            requests.post(url, json={'chat_id': chat_id.strip(), 'text': message[:4000], 'parse_mode': 'HTML'}, timeout=5)
    except:
        pass

def calculate_rsi(prices, period=14):
    """Calcular RSI usando librería ta - MÁS PRECISO"""
    try:
        import pandas as pd
        import ta
        if len(prices) < period + 1:
            return 50.0
        series = pd.Series(prices)
        rsi_series = ta.momentum.RSIIndicator(close=series, window=period).rsi()
        rsi_value = rsi_series.iloc[-1]
        # Manejar NaN
        if pd.isna(rsi_value):
            return 50.0
        return float(rsi_value)
    except:
        # Fallback a cálculo manual si falla
        return calculate_rsi_manual(prices, period)

def calculate_rsi_manual(prices, period=14):
    """Cálculo manual de RSI - VERSIÓN CORREGIDA"""
    if len(prices) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    if len(gains) < period:
        return 50.0
    
    # Usar Wilder's Smoothing (método original)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_exchange(config):
    try:
        exchange = ccxt.coinex({'apiKey': config['API_KEY'], 'secret': config['API_SECRET'], 'enableRateLimit': True, 'options': {'defaultType': 'spot',}})
        exchange.load_markets()
        return exchange
    except:
        return ccxt.binance({'enableRateLimit': True})

def fetch_ohlcv(exchange, symbol, timeframe, limit=200):
    try:
        if symbol not in exchange.markets:
            if 'BTC/USDT' in exchange.markets:
                symbol = 'BTC/USDT'
            elif 'ETH/USDT' in exchange.markets:
                symbol = 'ETH/USDT'
        return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        print(f"[ERROR] Fetch {symbol}: {e}")
        raise e

def save_state(equity, positions):
    with open(STATE_FILE, 'w') as f:
        json.dump({'equity': equity, 'positions': positions}, f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return 235.0, []
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
            return data.get('equity', 235.0), data.get('positions', [])
    except:
        return 235.0, []

def log_operation(operation_type, data):
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['timestamp', 'type', 'symbol', 'mode', 'entry', 'exit', 'pnl', 'equity'])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            operation_type,
            data.get('symbol', ''),
            data.get('mode', ''),
            data.get('entry', ''),
            data.get('exit', ''),
            data.get('pnl', ''),
            data.get('equity', '')
        ])

def main():
    global RUNNING
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    config = load_env()
    exchange = get_exchange(config)
    equity, positions = load_state()
    daily_stats['start_equity'] = equity
    
    # Convertir posiciones a formato correcto
    position_objects = []
    for pos in positions:
        if isinstance(pos, dict):
            position_objects.append({
                'mode': pos.get('mode', 'moderado'),
                'symbol': pos.get('symbol', 'BTC/USDT'),
                'entry': float(pos.get('entry', 0)),
                'qty': float(pos.get('qty', 0)),
                'sl': float(pos.get('sl', 0)),
                'tp': float(pos.get('tp', 0)),
                'open_time': pos.get('open_time', time.time())
            })
    positions = position_objects
    last_fetch = {}
    last_signal_time = {}
    
    print("[INFO] Bot Zaffex iniciado - CoinEx - Saldo: $235")
    
    while RUNNING:
        try:
            for symbol in config['SYMBOLS']:
                if symbol not in exchange.markets:
                    continue
                now = time.time()
                if symbol in last_fetch and (now - last_fetch[symbol]) < 2:
                    continue
                
                # Obtener datos con 200 velas
                ohlcv = fetch_ohlcv(exchange, symbol, config['TIMEFRAME'], limit=200)
                closes = [candle[4] for candle in ohlcv]
                current_price = closes[-1]
                rsi = calculate_rsi(closes, config['RSI_PERIOD'])
                print(f"[DEBUG] {symbol} → Precio: {current_price:.2f} | RSI: {rsi:.2f}")
                
                # === CIERRE DE POSICIONES EXISTENTES ===
                positions_to_remove = []
                for i, pos in enumerate(positions):
                    if pos['symbol'] != symbol:
                        continue
                    pnl = 0
                    should_close = False
                    reason = ""
                    if current_price >= pos['tp']:
                        should_close = True
                        reason = "TP"
                        pnl = (current_price - pos['entry']) * pos['qty']
                    elif current_price <= pos['sl']:
                        should_close = True
                        reason = "SL"
                        pnl = (current_price - pos['entry']) * pos['qty']
                    if should_close:
                        fee = abs(pnl) * 0.001
                        equity += pnl - fee
                        log_operation('CLOSE', {'symbol': symbol, 'mode': pos['mode'], 'entry': pos['entry'], 'exit': current_price, 'pnl': pnl - fee, 'equity': equity})
                        # Notificación
                        try:
                            from telegram_notifier import TelegramNotifier
                            tg_notifier = TelegramNotifier(config['TELEGRAM_TOKEN'], config['TELEGRAM_IDS'])
                            duration_minutes = (time.time() - pos['open_time']) / 60
                            pnl_pct = (pnl / (pos['entry'] * pos['qty'])) * 100 if (pos['entry'] * pos['qty']) > 0 else 0
                            tg_notifier.send_close(symbol=symbol, mode=pos['mode'], exit_price=current_price, pnl=pnl - fee, pnl_pct=pnl_pct, reason=reason, duration_minutes=duration_minutes, win_rate=0, equity=equity, total_ops=1, entry_price=pos['entry'])
                        except:
                            send_telegram(f"✅ CLOSE {symbol} {reason} PnL: {pnl - fee:.6f}", config)
                        print(f"[CLOSE] {symbol} {reason} PnL: {pnl - fee:.6f}")
                        positions_to_remove.append(i)
                for i in sorted(positions_to_remove, reverse=True):
                    positions.pop(i)
                
                # === APERTURA DE NUEVAS POSICIONES ===
                cooldown_key = f"{symbol}_last_signal"
                last_signal = last_signal_time.get(cooldown_key, 0)
                cooldown_period = config['SIGNAL_COOLDOWN']
                if rsi < config['RSI_THRESHOLD'] and (now - last_signal) > cooldown_period:
                    last_signal_time[cooldown_key] = now
                    modes = [('agresivo', config['LOT_AGRESIVO'], config['CAP_AGRESIVO']), ('moderado', config['LOT_MODERADO'], config['CAP_MODERADO']), ('conservador', config['LOT_CONSERVADOR'], config['CAP_CONSERVADOR'])]
                    for mode, lotes, capital in modes:
                        lot_size = capital / lotes
                        qty = lot_size / current_price
                        sl_price = current_price * (1 - config['SL_PCT'] / 100)
                        tp_price = current_price * (1 + config['TP_PCT'] / 100)
                        new_position = {'mode': mode, 'symbol': symbol, 'entry': current_price, 'qty': qty, 'sl': sl_price, 'tp': tp_price, 'open_time': time.time()}
                        positions.append(new_position)
                        log_operation('OPEN', {'symbol': symbol, 'mode': mode, 'entry': current_price, 'equity': equity})
                        # Notificación
                        try:
                            from telegram_notifier import TelegramNotifier
                            tg_notifier = TelegramNotifier(config['TELEGRAM_TOKEN'], config['TELEGRAM_IDS'])
                            tg_notifier.send_open(symbol=symbol, mode=mode, lotes=lotes, entry=current_price, sl=sl_price, tp=tp_price, equity=equity, rsi=rsi, qty_total=qty, usdt_total=lot_size)
                        except:
                            send_telegram(f"📈 OPEN {symbol} {mode} x{lotes} @ {current_price:.2f}", config)
                        print(f"[OPEN] {symbol} {mode} x{lotes} @ {current_price:.2f}")
                last_fetch[symbol] = now
            save_state(equity, positions)
            time.sleep(2)
        except Exception as e:
            print(f"[ERROR] {str(e)[:100]}")
            time.sleep(5)
    print("[SHUTDOWN] Bot detenido correctamente")

if __name__ == "__main__":
    main()

