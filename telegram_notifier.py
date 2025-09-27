# telegram_notifier.py
import requests
import time
from datetime import datetime
from typing import Optional, List, Dict

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

    def _create_hourly_bars(self, hourly_ Dict[int, int], max_ops: int = 10) -> str:
        """Crear gráfico de barras por hora"""
        if not hourly_
            return "Sin datos de operaciones"
        
        # Normalizar los datos
        max_val = max(hourly_data.values()) if hourly_data else 1
        if max_val == 0:
            max_val = 1
            
        bars = []
        for hour in range(24):
            count = hourly_data.get(hour, 0)
            if count > 0:
                # Escalar la barra (máximo 10 caracteres)
                bar_length = min(int((count / max_val) * 10), 10)
                bar = "█" * bar_length + "░" * (10 - bar_length)
                bars.append(f"{hour:02d}:00 {bar} {count}")
        
        return "\n".join(bars) if bars else "Sin actividad"

    def _create_pnl_trend(self, pnl_ List[float]) -> str:
        """Crear gráfico de tendencia de PnL"""
        if not pnl_data or len(pnl_data) < 2:
            return "Sin datos de PnL"
        
        # Simplificar a 10 puntos
        step = max(1, len(pnl_data) // 10)
        simplified = pnl_data[::step][:10]
        
        if not simplified:
            return "Sin datos"
        
        min_val = min(simplified)
        max_val = max(simplified)
        range_val = max_val - min_val if max_val != min_val else 1
        
        # Crear gráfico de 5 líneas
        lines = []
        for i in range(5):
            line = ""
            for val in simplified:
                # Normalizar valor a 0-4
                normalized = int(((val - min_val) / range_val) * 4)
                if normalized >= (4 - i):
                    line += "●"
                else:
                    line += "○"
            lines.append(line)
        
        # Formatear con valores
        trend_str = "\n".join([f"{' ' * 6}{line}" for line in reversed(lines)])
        return f"PnL Trend:\n{trend_str}"

    def _create_mode_distribution(self, mode_ Dict[str, int]) -> str:
        """Crear gráfico circular ASCII para distribución por modo"""
        if not mode_
            return "Sin datos por modo"
        
        total = sum(mode_data.values())
        if total == 0:
            return "Sin operaciones"
        
        # Emojis por modo
        mode_emojis = {"agresivo": "🔥", "moderado": "⚡", "conservador": "🛡️"}
        
        distribution = []
        for mode, count in mode_data.items():
            percentage = (count / total) * 100
            emoji = mode_emojis.get(mode.lower(), "•")
            distribution.append(f"{emoji} {mode.title()}: {count} ({percentage:.0f}%)")
        
        return "\n".join(distribution)

    def _create_winrate_progress(self, wins: int, losses: int) -> str:
        """Crear barra de progreso de win rate"""
        total = wins + losses
        if total == 0:
            return "0% (0/0)"
        
        win_rate = (wins / total) * 100
        # Crear barra de 20 caracteres
        filled = int((win_rate / 100) * 20)
        bar = "█" * filled + "░" * (20 - filled)
        
        # Color según win rate
        if win_rate >= 70:
            color = "green"
        elif win_rate >= 50:
            color = "orange"
        else:
            color = "red"
        
        return f"<code>{bar}</code> <b>{win_rate:.1f}%</b> ({wins}/{total})"

    def send_daily_summary(self, date: str, trades: int, wins: int, losses: int, 
                          pnl_total: float, equity: float, max_drawdown: float,
                          hourly_ Dict[int, int] = None,
                          pnl_trend: List[float] = None,
                          mode_ Dict[str, int] = None,
                          best_trade: float = 0,
                          worst_trade: float = 0):
        """Resumen diario con gráficos ASCII avanzados"""
        
        # Determinar emoji por rendimiento
        if pnl_total > 0:
            day_emoji = "🚀" if pnl_total > 1 else "📈"
        elif pnl_total < 0:
            day_emoji = "📉" if pnl_total < -1 else "⚠️"
        else:
            day_emoji = "📊"
        
        # Crear gráficos
        winrate_graph = self._create_winrate_progress(wins, losses)
        hourly_graph = self._create_hourly_bars(hourly_data or {}) if hourly_data else "Sin datos horarios"
        mode_dist = self._create_mode_distribution(mode_data or {}) if mode_data else "Sin datos por modo"
        pnl_trend_graph = self._create_pnl_trend(pnl_trend or []) if pnl_trend else "Sin tendencia"
        
        text = (
            f"{day_emoji} <b>RESUMEN DIARIO - {date}</b>\n\n"
            f"<b>💰 PnL Total:</b> {pnl_total:+,.6f} USDT\n"
            f"<b>🏦 Equity Final:</b> ${equity:,.2f}\n"
            f"<b>📉 Máx. Drawdown:</b> {max_drawdown:.2f}%\n\n"
            f"<b>📊 Operaciones:</b> {trades}\n"
            f"<b>🎯 Win Rate:</b> {winrate_graph}\n\n"
            f"<b>🏆 Mejor Operación:</b> {best_trade:+.6f} USDT\n"
            f"<b>⚠️ Peor Operación:</b> {worst_trade:+.6f} USDT\n\n"
            f"<b>⏰ Actividad por Hora:</b>\n"
            f"<pre>{hourly_graph}</pre>\n\n"
            f"<b>⚖️ Distribución por Modo:</b>\n"
            f"{mode_dist}\n\n"
            f"<b>📈 Tendencia PnL:</b>\n"
            f"<pre>{pnl_trend_graph}</pre>"
        )
        
        self._send_to_all(text)

    def send_weekly_summary(self, week_start: str, week_end: str, total_trades: int,
                           win_rate: float, total_pnl: float, best_day_pnl: float,
                           worst_day_pnl: float, days_with_profit: int):
        """Resumen semanal con gráfico de calor"""
        if total_pnl > 0:
            week_emoji = "🚀" if total_pnl > 5 else "📈"
        elif total_pnl < 0:
            week_emoji = "📉" if total_pnl < -5 else "⚠️"
        else:
            week_emoji = "📊"
        
        # Crear gráfico de calor semanal (simplificado)
        profit_emoji = "🟢" if days_with_profit >= 4 else "🟡" if days_with_profit >= 2 else "🔴"
        
        text = (
            f"{week_emoji} <b>RESUMEN SEMANAL</b>\n"
            f"<b>📅 Período:</b> {week_start} - {week_end}\n\n"
            f"<b>💰 PnL Total:</b> {total_pnl:+,.6f} USDT\n"
            f"<b>📊 Operaciones:</b> {total_trades}\n"
            f"<b>🎯 Win Rate:</b> {win_rate:.1f}%\n"
            f"<b>📈 Días Rentables:</b> {days_with_profit}/7 {profit_emoji}\n\n"
            f"<b>🏆 Mejor Día:</b> {best_day_pnl:+.6f} USDT\n"
            f"<b>⚠️ Peor Día:</b> {worst_day_pnl:+.6f} USDT\n\n"
            f"<b>💡 Estadísticas Clave:</b>\n"
            f"• Promedio por operación: {(total_pnl/total_trades):+.6f} USDT\n"
            f"• Operaciones por día: {total_trades/7:.1f}\n"
            f"• Consistencia: {'Alta' if days_with_profit >= 5 else 'Media' if days_with_profit >= 3 else 'Baja'}"
        )
        
        self._send_to_all(text)

    def send_open(self, symbol: str, mode: str, lotes: int, entry: float, sl: float, tp: float, 
                  equity: float, rsi: float, qty_total: float, usdt_total: float):
        """Notificación mejorada para apertura"""
        sl_pct = ((entry - sl) / entry) * 100
        tp_pct = ((tp - entry) / entry) * 100
        risk_reward = tp_pct / sl_pct if sl_pct > 0 else 0
        
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
        if pnl >= 0:
            emoji = "✅" if reason == "TP" else "🎯"
        else:
            emoji = "❌" if reason == "SL" else "⚠️"
        
        reason_text = {
            "TP": "Take Profit Alcanzado",
            "SL": "Stop Loss Activado",
            "MANUAL": "Cierre Manual"
        }.get(reason, reason)
        
        risk_pct = 1.2
        r_multiple = abs(pnl_pct) / risk_pct if risk_pct > 0 else 0
        
        text = (
            f"{emoji} <b>CLOSE - {reason_text}</b>\n\n"
            f"<b>🪙 Símbolo:</b> {symbol}\n"
            f"<b>🎯 Modo:</b> {mode.title()}\n\n"
            f"<b>💰 Entrada:</b> ${entry_price:,.2f}\n"
            f"<b>📤 Salida:</b> ${exit_price:,.2f}\n"
            f"<b>📊 PnL:</b> {pnl:+,.6f} USDT ({pnl_pct:+.2f}%)\n"
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
