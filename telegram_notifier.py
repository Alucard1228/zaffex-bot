# telegram_notifier.py
# -*- coding: utf-8 -*-
import requests
import logging

log = logging.getLogger("tg")

def _fmt_money(x: float) -> str:
    sign = "" if x >= 0 else "-"
    v = abs(x)
    if v >= 1000:
        return f"{sign}${v:,.2f}"
    return f"{sign}${v:.2f}"

def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"

def TPtoSL(entry: float, tp: float, sl: float) -> float:
    if entry <= 0:
        return 0.0
    up = abs(tp - entry)
    dn = abs(entry - sl)
    if dn == 0:
        return 0.0
    return round(up / dn, 1)

class TelegramNotifier:
    def __init__(self, token: str = "", allowed_chat_ids=None, **kwargs):
        self.token = token or ""
        self.allowed = []
        if allowed_chat_ids:
            for cid in allowed_chat_ids:
                try:
                    self.allowed.append(int(str(cid).strip()))
                except Exception:
                    pass
        self.api = f"https://api.telegram.org/bot{self.token}" if self.token else ""
        self.session = requests.Session() if self.token else None

    def _post(self, text: str, disable_web_page_preview: bool = True):
        if not self.session or not self.api or not self.allowed:
            return
        for chat_id in self.allowed:
            try:
                self.session.post(
                    f"{self.api}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": disable_web_page_preview
                    },
                    timeout=10
                )
            except Exception as e:
                log.warning(f"Telegram send fail: {e}")

    def broadcast(self, text: str):
        self._post(text)

    def send_open(self, symbol, mode, side, lots, entry, sl, tp, timeframe, size_usd, qty, rsi_value=None):
        rr = TPtoSL(entry, tp, sl)
        rsi_txt = f"{rsi_value:.1f}" if rsi_value is not None else "n/a"
        text = (
            f"⚡ <b>NUEVA POSICIÓN</b>\n"
            f"{'🟢' if side == 'LONG' else '🔴'} <b>{side}</b> | {symbol}\n\n"
            f"📊 <b>Modo:</b> {mode} ({lots} lotes)\n"
            f"💰 <b>Capital:</b> ${size_usd:,.2f}\n"
            f"📈 <b>Entrada:</b> ${entry:,.4f}\n"
            f"✅ <b>TP:</b> ${tp:,.4f} ({TAKE_PROFIT_PCT:.1f}%)\n"
            f"🛑 <b>SL:</b> ${sl:,.4f} ({STOP_LOSS_PCT:.1f}%)\n"
            f"⚖️ <b>R:R</b> = 1:{rr:.1f}\n"
            f"🕒 <b>TF:</b> {timeframe} | RSI: {rsi_txt}\n"
        )
        self._post(text)

    def send_partial_tp(self, symbol, mode, side, partial_pct, price):
        text = (
            f"🟢 <b>TP Parcial</b>\n\n"
            f"🪙 <b>Símbolo:</b> {symbol}\n"
            f"🎯 <b>Modo:</b> {mode} · {side}\n"
            f"🔹 <b>Ejecutado:</b> {partial_pct:.0f}% @ ${price:,.4f}\n"
        )
        self._post(text)

    def send_close(self, symbol, mode, side, reason, gross, fees, pnl, duration_sec, entry, exit_price, current_capital):
        dur = format_duration(duration_sec)
        emoji = "✅" if pnl >= 0 else "❌"
        change_pct = abs((exit_price - entry) / entry * 100)
        text = (
            f"{emoji} <b>OPERACIÓN CERRADA</b>\n"
            f"{'🟢' if side == 'LONG' else '🔴'} {symbol} | {mode}\n\n"
            f"📍 <b>Entrada:</b> ${entry:,.4f} → <b>Salida:</b> ${exit_price:,.4f}\n"
            f"📊 <b>PnL:</b> {_fmt_money(pnl)} ({change_pct:.2f}%)\n"
            f"💰 <b>Capital actual:</b> ${current_capital:,.2f}\n"
            f"⏱️ <b>Duración:</b> {dur} | <b>Razón:</b> {reason}\n"
        )
        self._post(text)

    def send_hourly_summary(self, total_capital, trades, wins, losses, hourly_pnl):
        if trades == 0:
            text = (
                f"📊 <b>RESUMEN HORARIO</b>\n\n"
                f"🕒 <b>Última hora:</b> Sin operaciones\n"
                f"💰 <b>Capital actual:</b> ${total_capital:,.2f}\n"
            )
        else:
            win_rate = wins / trades * 100
            emoji = "📈" if hourly_pnl >= 0 else "📉"
            text = (
                f"{emoji} <b>RESUMEN HORARIO</b>\n\n"
                f"🧾 <b>Operaciones:</b> {trades} (✅ {wins} · ❌ {losses})\n"
                f"🎯 <b>Win Rate:</b> {win_rate:.1f}%\n"
                f"📊 <b>PnL esta hora:</b> {_fmt_money(hourly_pnl)}\n"
                f"💰 <b>Capital actual:</b> ${total_capital:,.2f}\n"
            )
        self._post(text)
