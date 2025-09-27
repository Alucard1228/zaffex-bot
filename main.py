#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Zaffex - Réplica exacta para CoinEx
- Cierre automático por TP/SL
- RSI preciso con librería ta
- 200 velas para cálculo estable
- Estadísticas diarias completas
"""

import os
import time
import signal
import json
import csv
from datetime import datetime, timezone
import requests
import ccxt

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
        'RSI_PERIOD': int(os.getenv('RSI_PERIOD', '14')),
        'RSI_THRESHOLD': float(os.getenv('RSI_THRESHOLD', '30')),
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
    """Calcular RSI usando librería ta - PRECISO Y FIABLE"""
    try:
        import pandas as pd
        import ta
        if len(prices) < period + 1:
            return 50.0
        series = pd.Series(prices)
        rsi_indicator = ta.momentum.RSIIndicator(close=series, window=period)
        rsi_values = rsi_indicator.rsi()
        rsi_value = rsi_values.iloc[-1]
        if pd.isna(rsi_value) or rsi_value is None:
            return 50.0
        return float(rsi_value)
    except Exception as e:
        print(f"[RSI_ERROR] {str(e)[:100]} - Usando RSI=50")
        return 50.0

def get_exchange(config):
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

def fetch_ohlcv(exchange, symbol, timeframe, limit=200):
    try:
        if symbol not in exchange.markets:
            print(f"[WARN] Símbolo {symbol} no encontrado")
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

def update_daily_stats(pnl: float, mode: str, symbol: str):
    from datetime import datetime
    daily_stats['trades'].append({
        'timestamp': datetime.now(),
        'pnl': pnl,
        'mode': mode,
        'symbol': symbol
    })
    current_hour = datetime.now().hour
    daily_stats['hourly_ops'][current_hour] = daily_stats['hourly_ops'].get(current_hour, 0) + 1
    if mode in daily_stats['mode_ops']:
        daily_stats['mode_ops'][mode] += 1
    current_pnl = sum(t['pnl'] for t in daily_stats['trades'])
    daily_stats['pnl_history'].append(current_pnl)
    if pnl > daily_stats['best_trade']:
        daily_stats['best_trade'] = pnl
    if pnl < daily_stats['worst_trade']:
        daily_stats['worst_trade'] = pnl

def send_daily_summary_if_needed(tg, equity: float):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if now.hour == 23 and now.minute == 59 and len(daily_stats['trades']) > 0:
        trades = len(daily_stats['trades'])
        wins = len([t for t in daily_stats['trades'] if t['pnl'] > 0])
        losses = trades - wins
        pnl_total = sum(t['pnl'] for t in daily_stats['trades'])
        equity_values = [daily_stats['start_equity']]
        running_equity = daily_stats['start_equity']
        for trade in daily_stats['trades']:
            running_equity += trade['pnl']
            equity_values.append(running_equity)
        min_equity = min(equity_values)
        drawdown_pct = (daily_stats['start_equity'] - min_equity) / daily_stats['start_equity'] * 100
        max_dd = max(0, drawdown_pct)
        if tg.enabled():
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
        daily_stats['trades'] = []
        daily_stats['hourly_ops'] = {}
        daily_stats['pnl_history'] = []
        daily_stats['mode_ops'] = {'agresivo': 0, 'moderado': 0, 'conservador': 0}
        daily_stats['best_trade'] = 0
        daily_stats['worst_trade'] = 0
        daily_stats['start_equity'] = equity

def main():
    global RUNNING
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    config = load_env()
    exchange = get_exchange(config)
    equity, positions = load_state()
    daily_stats['start_equity'] = equity
    
    # Convertir posiciones guardadas a objetos con todos los datos
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
    
    send_telegram("🤖 Bot Zaffex - CoinEx\nSaldo: $235\nPares: BTC/USDT, ETH/USDT", config)
    print("[INFO] Bot Zaffex iniciado - CoinEx - Saldo: $235")
    
    while RUNNING:
        try:
            send_daily_summary_if_needed(None, equity)
            
            for symbol in config['SYMBOLS']:
                if symbol not in exchange.markets:
                    continue
                
                now = time.time()
                if symbol in last_fetch and (now - last_fetch[symbol]) < 2:
                    continue
                
                # Obtener datos con 200 velas para RSI preciso
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
                    
                    # Verificar si la posición debe cerrarse
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
                        # Calcular fee (0.1%)
                        fee = abs(pnl) * 0.001
                        equity += pnl - fee
                        
                        # Registrar cierre
                        log_operation('CLOSE', {
                            'symbol': symbol,
                            'mode': pos['mode'],
                            'entry': pos['entry'],
                            'exit': current_price,
                            'pnl': pnl - fee,
                            'equity': equity
                        })
                        
                        # Actualizar estadísticas
                        update_daily_stats(pnl - fee, pos['mode'], symbol)
                        
                        # Notificación
                        try:
                            from telegram_notifier import TelegramNotifier
                            tg_notifier = TelegramNotifier(config['TELEGRAM_TOKEN'], config['TELEGRAM_IDS'])
                            duration_minutes = (time.time() - pos['open_time']) / 60
                            pnl_pct = (pnl / (pos['entry'] * pos['qty'])) * 100 if (pos['entry'] * pos['qty']) > 0 else 0
                            wins = len([t for t in daily_stats['trades'] if t['pnl'] > 0])
                            total_ops = len(daily_stats['trades'])
                            win_rate = (wins / total_ops * 100) if total_ops > 0 else 0
                            
                            tg_notifier.send_close(
                                symbol=symbol,
                                mode=pos['mode'],
                                exit_price=current_price,
                                pnl=pnl - fee,
                                pnl_pct=pnl_pct,
                                reason=reason,
                                duration_minutes=duration_minutes,
                                win_rate=win_rate,
                                equity=equity,
                                total_ops=total_ops,
                                entry_price=pos['entry']
                            )
                        except Exception as e:
                            print(f"[ERROR] Notificación de cierre: {str(e)[:100]}")
                            send_telegram(f"✅ CLOSE {symbol} {reason} PnL: {pnl - fee:.6f}", config)
                        
                        print(f"[CLOSE] {symbol} {reason} PnL: {pnl - fee:.6f}")
                        positions_to_remove.append(i)
                
                # Eliminar posiciones cerradas
                for i in sorted(positions_to_remove, reverse=True):
                    positions.pop(i)
                
                # === APERTURA DE NUEVAS POSICIONES ===
                cooldown_key = f"{symbol}_last_signal"
                last_signal = last_signal_time.get(cooldown_key, 0)
                cooldown_period = config['SIGNAL_COOLDOWN']
                
                if rsi < config['RSI_THRESHOLD'] and (now - last_signal) > cooldown_period:
                    last_signal_time[cooldown_key] = now
                    
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
                        
                        # Crear objeto de posición
                        new_position = {
                            'mode': mode,
                            'symbol': symbol,
                            'entry': current_price,
                            'qty': qty,
                            'sl': sl_price,
                            'tp': tp_price,
                            'open_time': time.time()
                        }
                        positions.append(new_position)
                        
                        # Registrar apertura
                        log_operation('OPEN', {
                            'symbol': symbol,
                            'mode': mode,
                            'entry': current_price,
                            'equity': equity
                        })
                        
                        # Notificación
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
                        except Exception as e:
                            print(f"[ERROR] Notificación de apertura: {str(e)[:100]}")
                            send_telegram(f"📈 OPEN {symbol} {mode} x{lotes} @ {current_price:.2f}", config)
                        
                        print(f"[OPEN] {symbol} {mode} x{lotes} @ {current_price:.2f}")
                
                last_fetch[symbol] = now
            
            # Guardar estado con posiciones actualizadas
            save_state(equity, positions)
            time.sleep(2)
            
        except Exception as e:
            print(f"[ERROR] {str(e)[:100]}")
            time.sleep(5)
    
    print("[SHUTDOWN] Bot detenido correctamente")

if __name__ == "__main__":
    main()
