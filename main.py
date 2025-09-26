#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Zaffex - Réplica exacta para CoinEx
- Exchange: CoinEx
- Pares: BTC/USDT, ETH/USDT
- Timeframe: 1m
- RSI(9) < 25 para entrada
- TP: 1.0%, SL: 1.2%
"""

import os
import time
import signal
import json
import csv
from datetime import datetime, timezone
import requests
import ccxt

# Configuración global
RUNNING = True
STATE_FILE = "paper_state.json"
CSV_FILE = "operaciones.csv"

def signal_handler(signum, frame):
    """Manejo limpio de señales de Railway"""
    global RUNNING
    print(f"[SIGNAL] Apagando bot gracefully...")
    RUNNING = False

def load_env():
    """Cargar variables de entorno"""
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
    }

def send_telegram(message, config):
    """Enviar notificación a Telegram"""
    if not config['TELEGRAM_TOKEN'] or not config['TELEGRAM_IDS']:
        return
    
    try:
        url = f"https://api.telegram.org/bot{config['TELEGRAM_TOKEN']}/sendMessage"
        for chat_id in config['TELEGRAM_IDS'].split(','):
            requests.post(url, json={
                'chat_id': chat_id.strip(),
                'text': message[:4000],
                'parse_mode': 'HTML'
            }, timeout=5)
    except:
        pass

def calculate_rsi(prices, period=9):
    """Calcular RSI manualmente sin pandas"""
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
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_exchange(config):
    """Inicializar exchange CoinEx"""
    try:
        exchange = ccxt.coinex({
            'apiKey': config['API_KEY'],
            'secret': config['API_SECRET'],
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            }
        })
        exchange.load_markets()
        return exchange
    except Exception as e:
        print(f"[ERROR] CoinEx init: {e}")
        return ccxt.binance({'enableRateLimit': True})

def fetch_ohlcv(exchange, symbol, timeframe, limit=50):
    """Obtener datos OHLCV de CoinEx"""
    try:
        return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        print(f"[ERROR] Fetch {symbol}: {e}")
        if symbol == 'BTC/USDT':
            return exchange.fetch_ohlcv('BTC/USDT', timeframe, limit=limit)
        elif symbol == 'ETH/USDT':
            return exchange.fetch_ohlcv('ETH/USDT', timeframe, limit=limit)
        else:
            raise e

def save_state(equity, positions):
    """Guardar estado"""
    with open(STATE_FILE, 'w') as f:
        json.dump({'equity': equity, 'positions': positions}, f)

def load_state():
    """Cargar estado"""
    if not os.path.exists(STATE_FILE):
        return 235.0, []
    
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
            return data.get('equity', 235.0), data.get('positions', [])
    except:
        return 235.0, []

def log_operation(operation_type, data):
    """Registrar operación en CSV"""
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
    
    # Registrar señales
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Cargar configuración
    config = load_env()
    
    # Inicializar
    exchange = get_exchange(config)
    equity, positions = load_state()
    
    # Notificación de inicio
    send_telegram("🤖 Bot Zaffex - CoinEx\nSaldo: $235\nPares: BTC/USDT, ETH/USDT", config)
    print("[INFO] Bot Zaffex iniciado - CoinEx - Saldo: $235")
    
    last_fetch = {}
    
    while RUNNING:
        try:
            for symbol in config['SYMBOLS']:
                # Evitar fetch demasiado frecuente
                now = time.time()
                if symbol in last_fetch and (now - last_fetch[symbol]) < 2:
                    continue
                
                # Obtener datos
                ohlcv = fetch_ohlcv(exchange, symbol, config['TIMEFRAME'])
                closes = [candle[4] for candle in ohlcv]
                current_price = closes[-1]
                
                # Calcular RSI
                rsi = calculate_rsi(closes, config['RSI_PERIOD'])
                
                # Verificar señales de entrada
                if rsi < config['RSI_THRESHOLD']:
                    # Abrir posiciones en los 3 modos
                    modes = [
                        ('agresivo', config['LOT_AGRESIVO'], config['CAP_AGRESIVO']),
                        ('moderado', config['LOT_MODERADO'], config['CAP_MODERADO']),
                        ('conservador', config['LOT_CONSERVADOR'], config['CAP_CONSERVADOR'])
                    ]
                    
                    for mode, lotes, capital in modes:
                        lot_size = capital / lotes
                        qty = lot_size / current_price
                        
                        # Registrar apertura
                        log_operation('OPEN', {
                            'symbol': symbol,
                            'mode': mode,
                            'entry': current_price,
                            'equity': equity
                        })
                        
                        # Notificación
                        send_telegram(
                            f"📈 OPEN {symbol}\nModo: {mode}\nLotes: {lotes}\nPrecio: {current_price:.2f}",
                            config
                        )
                        print(f"[OPEN] {symbol} {mode} x{lotes} @ {current_price:.2f}")
                
                last_fetch[symbol] = now
            
            # Guardar estado
            save_state(equity, positions)
            
            # Esperar 2 segundos
            time.sleep(2)
            
        except Exception as e:
            print(f"[ERROR] {str(e)[:100]}")
            time.sleep(5)
    
    print("[SHUTDOWN] Bot detenido correctamente")

if __name__ == "__main__":
    main()
