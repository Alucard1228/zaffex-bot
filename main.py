#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Zaffex - Réplica exacta para CoinEx
- Símbolos correctos: BTC/USDT, ETH/USDT
- Timeframe: 1m
- RSI(9) < 25 para entrada
- Cooldown de 5 minutos entre señales
- Estadísticas diarias para gráficos
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

# Variables globales para estadísticas diarias
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
        'SIGNAL_COOLDOWN': int(os.getenv('SIGNAL_COOLDOWN', '300')),
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
    """Calcular RSI manualmente sin pandas - VERSIÓN CORREGIDA"""
    if len(prices) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    
    # Calcular cambios de precio
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    # Si no hay suficientes datos, retornar 50
    if len(gains) < period:
        return 50.0
    
    # Calcular promedios
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    # Caso especial: sin pérdidas ni ganancias (precio constante)
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    
    # Caso especial: solo ganancias (sin pérdidas)
    if avg_loss == 0:
        return 100.0
    
    # Caso especial: solo pérdidas (sin ganancias)  
    if avg_gain == 0:
        return 0.0
    
    # Cálculo normal
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
        print(f"[COINEX] Mercados cargados: {len(exchange.markets)} pares")
        return exchange
    except Exception as e:
        print(f"[ERROR] CoinEx init: {e}")
        return ccxt.binance({'enableRateLimit': True})

def fetch_ohlcv(exchange, symbol, timeframe, limit=50):
    """Obtener datos OHLCV de CoinEx"""
    try:
        # Verificar que el símbolo exista
        if symbol not in exchange.markets:
            print(f"[WARN] Símbolo {symbol} no encontrado. Mercados disponibles: {list(exchange.markets.keys())[:10]}")
            # Intentar encontrar variante
            if 'BTC/USDT' in exchange.markets:
                symbol = 'BTC/USDT'
            elif 'ETH/USDT' in exchange.markets:
                symbol = 'ETH/USDT'
        
        return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        print(f"[ERROR] Fetch {symbol}: {e}")
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

def update_daily_stats(pnl: float, mode: str, symbol: str):
    """Actualizar estadísticas diarias"""
    from datetime import datetime
    
    # Registrar operación
    daily_stats['trades'].append({
        'timestamp': datetime.now(),
        'pnl': pnl,
        'mode': mode,
        'symbol': symbol
    })
    
    # Actualizar por hora
    current_hour = datetime.now().hour
    daily_stats['hourly_ops'][current_hour] = daily_stats['hourly_ops'].get(current_hour, 0) + 1
    
    # Actualizar por modo
    if mode in daily_stats['mode_ops']:
        daily_stats['mode_ops'][mode] += 1
    
    # Actualizar PnL history
    current_pnl = sum(t['pnl'] for t in daily_stats['trades'])
    daily_stats['pnl_history'].append(current_pnl)
    
    # Actualizar mejor/peor trade
    if pnl > daily_stats['best_trade']:
        daily_stats['best_trade'] = pnl
    if pnl < daily_stats['worst_trade']:
        daily_stats['worst_trade'] = pnl

def send_daily_summary_if_needed(tg, equity: float):
    """Enviar resumen diario al final del día"""
    from datetime import datetime, timezone
    
    now = datetime.now(timezone.utc)
    # Enviar resumen a las 23:59 UTC
    if now.hour == 23 and now.minute == 59 and len(daily_stats['trades']) > 0:
        trades = len(daily_stats['trades'])
        wins = len([t for t in daily_stats['trades'] if t['pnl'] > 0])
        losses = trades - wins
        pnl_total = sum(t['pnl'] for t in daily_stats['trades'])
        
        # Calcular drawdown máximo
        equity_values = [daily_stats['start_equity']]
        running_equity = daily_stats['start_equity']
        for trade in daily_stats['trades']:
            running_equity += trade['pnl']
            equity_values.append(running_equity)
        
        min_equity = min(equity_values)
        drawdown_pct = (daily_stats['start_equity'] - min_equity) / daily_stats['start_equity'] * 100
        max_dd = max(0, drawdown_pct)
        
        if tg.enabled():
            # Importar la clase actualizada de telegram_notifier
            from telegram_notifier import TelegramNotifier
            tg_notifier = TelegramNotifier(os.getenv('TELEGRAM_TOKEN'), os.getenv('TELEGRAM_ALLOWED_IDS'))
            tg_notifier.send_daily_summary(
                date=now.strftime('%Y-%m-%d'),
                trades=trades,
                wins=wins,
                losses=losses,
                pnl_total=pnl_total,
                equity=equity,
                max_drawdown=max_dd,
                hourly_data=daily_stats['hourly_ops'],
                pnl_trend=daily_stats['pnl_history'],
                mode_data=daily_stats['mode_ops'],
                best_trade=daily_stats['best_trade'],
                worst_trade=daily_stats['worst_trade']
            )
        
        # Resetear estadísticas para el nuevo día
        daily_stats['trades'] = []
        daily_stats['hourly_ops'] = {}
        daily_stats['pnl_history'] = []
        daily_stats['mode_ops'] = {'agresivo': 0, 'moderado': 0, 'conservador': 0}
        daily_stats['best_trade'] = 0
        daily_stats['worst_trade'] = 0
        daily_stats['start_equity'] = equity

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
    daily_stats['start_equity'] = equity
    
    # Notificación de inicio
    send_telegram("🤖 Bot Zaffex - CoinEx\nSaldo: $235\nPares: BTC/USDT, ETH/USDT", config)
    print("[INFO] Bot Zaffex iniciado - CoinEx - Saldo: $235")
    
    last_fetch = {}
    last_signal_time = {}  # Control de cooldown por símbolo
    
    while RUNNING:
        try:
            # Verificar si es hora de enviar resumen diario
            send_daily_summary_if_needed(None, equity)
            
            for symbol in config['SYMBOLS']:
                # Verificar que el símbolo esté disponible
                if symbol not in exchange.markets:
                    print(f"[SKIP] Símbolo no disponible: {symbol}")
                    continue
                
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
                print(f"[DEBUG] {symbol} → Precio: {current_price:.2f} | RSI: {rsi:.2f}")
                
                # Verificar señales de entrada CON COOLDOWN
                cooldown_key = f"{symbol}_last_signal"
                last_signal = last_signal_time.get(cooldown_key, 0)
                cooldown_period = config['SIGNAL_COOLDOWN']  # 300 segundos = 5 minutos
                
                if rsi < config['RSI_THRESHOLD'] and (now - last_signal) > cooldown_period:
                    # Registrar el tiempo de la señal
                    last_signal_time[cooldown_key] = now
                    
                    # Abrir posiciones en los 3 modos
                    modes = [
                        ('agresivo', config['LOT_AGRESIVO'], config['CAP_AGRESIVO']),
                        ('moderado', config['LOT_MODERADO'], config['CAP_MODERADO']),
                        ('conservador', config['LOT_CONSERVADOR'], config['CAP_CONSERVADOR'])
                    ]
                    
                    for mode, lotes, capital in modes:
                        lot_size = capital / lotes
                        qty = lot_size / current_price
                        sl_price = current_price * (1 - config['SL_PCT'] / 100)
                        tp_price = current_price * (1 + config['TP_PCT'] / 100)
                        
                        # Registrar apertura
                        log_operation('OPEN', {
                            'symbol': symbol,
                            'mode': mode,
                            'entry': current_price,
                            'equity': equity
                        })
                        
                        # Notificación mejorada (requiere telegram_notifier actualizado)
                        try:
                            from telegram_notifier import TelegramNotifier
                            tg_notifier = TelegramNotifier(config['TELEGRAM_TOKEN'], config['TELEGRAM_IDS'])
                            tg_notifier.send_open(
                                symbol=symbol,
                                mode=mode,
                                lotes=lotes,
                                entry=current_price,
                                sl=sl_price,
                                tp=tp_price,
                                equity=equity,
                                rsi=rsi,
                                qty_total=qty,
                                usdt_total=lot_size
                            )
                        except:
                            # Fallback a notificación simple
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

