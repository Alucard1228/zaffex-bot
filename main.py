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
                
                ohlcv = fetch_ohlcv(exchange, symbol, config['TIMEFRAME'])
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
                        # Actualizar equity
                        fee = abs(pnl) * 0.001  # 0.1% fee
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
                        except:
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
                        except:
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
