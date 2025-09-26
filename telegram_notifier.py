# telegram_notifier.py
import requests
import time
from typing import Optional

class TelegramNotifier:
    def __init__(self, token: Optional[str], allowed_ids: Optional[str]):
        self.bot_token = str(token or "").strip()
        self.allowed_ids = [id_str.strip() for id_str in str(allowed_ids or "").split(",") if id_str.strip()]
        self._enabled = bool(self.bot_token and self.allowed_ids)
        self.last_message_time = 0
        self.message_cooldown = 1  # 1 segundo entre mensajes

    def enabled(self) -> bool:
        return self._enabled

    def _send_to_all(self, text: str, parse_mode: str = "HTML"):
        """Enviar mensaje a todos los IDs permitidos"""
        if not self._enabled:
            return
        
        # Evitar spam (cooldown de mensajes)
        current_time = time.time()
        if current_time - self.last_message_time < self.message_cooldown:
            time.sleep(self.message_cooldown - (current_time - self.last_message_time))
        
        for chat_id in self.allowed_ids:
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": text[:4000],
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True
                }
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    self.last_message_time = time.time()
                else:
                    print(f"[TELEGRAM] Error {response.status_code}: {response.text}")
            except Exception as e:
                print(f"[TELEGRAM] Excepción: {e}")

    def send_open(self, symbol: str, mode: str, lotes: int, entry: float, sl: float, tp: float, 
                  equity: float, rsi: float, qty_total: float, usdt_total: float):
        """Notificación mejorada para apertura"""
        # Calcular porcentajes
        sl_pct = ((entry - sl) / entry) * 100
        tp_pct = ((tp - entry) / entry) * 100
        risk_reward = tp_pct / sl_pct if sl_pct > 0 else 0
        
        # Emoji por modo
        mode_emoji = {"agresivo": "🔥", "moderado": "⚡", "conservador": "🛡️"}
        emoji = mode_emoji.get(mode.lower(), "📈")
        
        text = (
            f"{emoji} <b>APERTURA DE POSICIÓN</b>\n\n"
            f"<b>🪙 Símbolo:</b> {symbol}\n"
            f"<b>🎯 Modo:</b> {mode.title()}\n"
            f"<b>📊 Lotes:</b> {lotes}\n\n"
            f"<b>💰 Entrada:</b> ${entry:,.2f}\n"
            f"<b>🛑 Stop Loss:</b> ${sl:,.2f} ({sl_pct:.1f}%)\n"
            f"<b>✅ Take Profit:</b> ${tp:,.2f} ({tp_pct:.1f}%)\n"
            f"<b>⚖️ Risk/Reward:</b> 1:{risk_reward:.1f}\n\n"
            f"<b>📈 RSI(9):</b> {rsi:.1f}\n"
            f"<b>⏰ Timeframe:</b> 1m\n"
            f"<b>💼 Tamaño:</b> ${usdt_total:,.2f} ({qty_total:.6f} {symbol.split('/')[0]})\n"
            f"<b>🏦 Equity:</b> ${equity:,.2f}"
        )
        self._send_to_all(text)

    def send_close(self, symbol: str, mode: str, exit_price: float, pnl: float, pnl_pct: float, 
                   reason: str, duration_minutes: float, win_rate: float, equity: float, 
                   total_ops: int, entry_price: float):
        """Notificación mejorada para cierre"""
        # Emoji según resultado
        if pnl >= 0:
            emoji = "✅" if reason == "TP" else "🎯"
            pnl_color = "green"
        else:
            emoji = "❌" if reason == "SL" else "⚠️"
            pnl_color = "red"
        
        # Razón legible
        reason_text = {
            "TP": "Take Profit Alcanzado",
            "SL": "Stop Loss Activado",
            "MANUAL": "Cierre Manual"
        }.get(reason, reason)
        
        # Calcular retorno sobre riesgo
        risk_pct = 1.2  # SL fijo de 1.2%
        r_multiple = abs(pnl_pct) / risk_pct if risk_pct > 0 else 0
        
        text = (
            f"{emoji} <b>CLOSE - {reason_text}</b>\n\n"
            f"<b>🪙 Símbolo:</b> {symbol}\n"
            f"<b>🎯 Modo:</b> {mode.title()}\n\n"
            f"<b>💰 Entrada:</b> ${entry_price:,.2f}\n"
            f"<b>📤 Salida:</b> ${exit_price:,.2f}\n"
            f"<b>📊 PnL:</b> <span class='{pnl_color}'>{pnl:+,.6f}</span> USDT ({pnl_pct:+.2f}%)\n"
            f"<b>⏱️ Duración:</b> {duration_minutes:.0f} min\n"
            f"<b>📈 R-Multiple:</b> {r_multiple:.2f}R\n\n"
            f"<b>📊 Win Rate:</b> {win_rate:.1f}% ({total_ops} ops)\n"
            f"<b>🏦 Equity:</b> ${equity:,.2f}"
        )
        self._send_to_all(text)

    def send_market_alert(self, symbol: str, alert_type: str, current_price: float, 
                         rsi: float, volatility: float = 0):
        """Alertas de mercado en tiempo real"""
        alert_emojis = {
            "OVERSOLD": "⚠️",
            "OVERBOUGHT": "⚠️", 
            "VOLATILITY": "⚡",
            "SIGNAL": "🎯"
        }
        
        alert_messages = {
            "OVERSOLD": f"RSI(9) = {rsi:.1f} (Sobreventa Extrema)",
            "OVERBOUGHT": f"RSI(9) = {rsi:.1f} (Sobrecompra Extrema)",
            "VOLATILITY": f"Volatilidad Alta: {volatility:.2f}%",
            "SIGNAL": f"Señal de Trading Detectada"
        }
        
        emoji = alert_emojis.get(alert_type, "🔔")
        message = alert_messages.get(alert_type, alert_type)
        
        text = (
            f"{emoji} <b>ALERTA DE MERCADO</b>\n\n"
            f"<b>🪙 Símbolo:</b> {symbol}\n"
            f"<b>📍 Precio:</b> ${current_price:,.2f}\n"
            f"<b>📢 Alerta:</b> {message}\n"
            f"<b>⏰ Timeframe:</b> 1m"
        )
        self._send_to_all(text)

    def send_summary(self, period: str, trades: int, wins: int, losses: int, 
                    win_rate: float, pnl: float, equity: float, 
                    max_drawdown: float = 0, by_mode: dict = None):
        """Resumen profesional con gráfico ASCII"""
        # Gráfico ASCII de win rate
        win_bars = "█" * int(win_rate / 10) if win_rate > 0 else "░"
        loss_bars = "░" * (10 - int(win_rate / 10)) if win_rate < 100 else ""
        win_graph = f"<code>{win_bars}{loss_bars}</code> {win_rate:.1f}%"
        
        # Determinar emoji por rendimiento
        if pnl > 0:
            summary_emoji = "📊" if pnl < 1 else "🚀"
        elif pnl < 0:
            summary_emoji = "📉" if pnl > -1 else "⚠️"
        else:
            summary_emoji = "📋"
        
        text = (
            f"{summary_emoji} <b>RESUMEN {period.upper()}</b>\n\n"
            f"<b>📈 Operaciones:</b> {trades}\n"
            f"<b>✅ Ganadas:</b> {wins}\n"
            f"<b>❌ Perdidas:</b> {losses}\n"
            f"<b>📊 Win Rate:</b> {win_graph}\n"
            f"<b>💰 PnL Total:</b> {pnl:+,.6f} USDT\n"
            f"<b>🏦 Equity Actual:</b> ${equity:,.2f}\n"
            f"<b>📉 Máx. Drawdown:</b> {max_drawdown:.2f}%"
        )
        
        # Añadir estadísticas por modo si existen
        if by_mode and any(count > 0 for count in by_mode.values()):
            text += "\n\n<b>📊 Por Modo:</b>"
            for mode, count in by_mode.items():
                if count > 0:
                    mode_icon = {"agresivo": "🔥", "moderado": "⚡", "conservador": "🛡️"}.get(mode, "•")
                    text += f"\n{mode_icon} {mode.title()}: {count}"
        
        self._send_to_all(text)

    def send_status(self, equity: float, positions: list, rsi_btc: float = 0, rsi_eth: float = 0):
        """Comando /status - Estado actual"""
        open_positions = len(positions)
        total_risk = sum(pos.get('qty', 0) * pos.get('entry', 0) for pos in positions) if positions else 0
        
        text = (
            f"🤖 <b>ESTADO DEL BOT</b>\n\n"
            f"<b>🏦 Equity:</b> ${equity:,.2f}\n"
            f"<b>📈 Posiciones Abiertas:</b> {open_positions}\n"
            f"<b>💰 Riesgo Total:</b> ${total_risk:,.2f}\n\n"
            f"<b>📊 RSI Actual:</b>\n"
            f"• BTC/USDT: {rsi_btc:.1f}\n"
            f"• ETH/USDT: {rsi_eth:.1f}\n\n"
            f"<b>⚙️ Modos Activos:</b> 3\n"
            f"<b>⏰ Última Actualización:</b> Ahora"
        )
        self._send_to_all(text)

    def send_error(self, error_msg: str):
        """Notificación de errores mejorada"""
        text = f"🚨 <b>ERROR CRÍTICO</b>\n<pre>{error_msg[:300]}</pre>"
        self._send_to_all(text, parse_mode="HTML")

    def send_pause(self, minutes: int, reason: str = "Pérdidas diarias"):
        """Notificación de pausa mejorada"""
        text = (
            f"⏸️ <b>PAUSA TEMPORAL</b>\n\n"
            f"<b>⏰ Duración:</b> {minutes} minutos\n"
            f"<b>📝 Razón:</b> {reason}\n"
            f"<b>🔄 Reanudación:</b> Automática\n"
            f"<b>📊 Estado:</b> Protegiendo capital"
        )
        self._send_to_all(text)

    def send(self, message: str):
        """Método genérico para retrocompatibilidad"""
        self._send_to_all(message)
