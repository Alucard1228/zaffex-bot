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
    """
    Clase compatible con:
      TelegramNotifier(token=..., allowed_chat_ids=[...])
    y también con llamada posicional:
      TelegramNotifier("BOT_TOKEN", [12345])
    """
    def __init__(self, token: str = "", allowed_chat_ids=None, **kwargs):
        # compat: permitir argumentos posicionales antiguos
        if isinstance(token, (list, tuple)) and allowed_chat_ids is None:
            # Caso viejo: TelegramNotifier([ids]) – no recomendado, pero evitamos crashear
            allowed_chat_ids = token
            token = ""
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

    # ----- Mensajes de negocio -----
    def send_open(self, symbol, mode, side, lots, entry, sl, tp, rsi, timeframe, size_usd, qty):
        text = (
            f"🔥 <b>{'LONG' if side=='LONG' else 'SHORT'} ABIERTO</b>\n\n"
            f"🪙 <b>Símbolo:</b> {symbol}\n"
            f"🎯 <b>Modo:</b> {mode} — Lado: <b>{side}</b>\n"
            f"📊 <b>Lotes:</b> {lots}\n\n"
            f"💰 <b>Entrada:</b> ${entry:,.2f}\n"
            f"🛑 <b>Stop Loss:</b> ${sl:,.2f}\n"
            f"✅ <b>Take Profit:</b> ${tp:,.2f}\n"
            f"⚖️ <b>Risk/Reward:</b> 1:{(TPtoSL(entry, tp, sl)):.1f}\n\n"
            f"⏰ <b>Timeframe:</b> {timeframe}\n"
            f"💼 <b>Tamaño:</b> ${size_usd:,.2f} ({qty} base)\n"
        )
        self._post(text)

    def send_partial_tp(self, symbol, mode, side, partial_pct, price):
        text = (
            f"🟢 <b>TP Parcial</b>\n\n"
            f"🪙 <b>Símbolo:</b> {symbol}\n"
            f"🎯 <b>Modo:</b> {mode} · Lado: <b>{side}</b>\n"
            f"🔹 <b>Ejecutado:</b> {partial_pct:.0f}% @ ${price:,.2f}\n"
        )
        self._post(text)

    def send_close(self, symbol, mode, side, reason, gross, fees, pnl, duration_sec):
        dur = format_duration(duration_sec)
        emoji = "✅" if pnl >= 0 else "❌"
        text = (
            f"{emoji} <b>CIERRE {side} ({reason})</b>\n\n"
            f"🪙 <b>Símbolo:</b> {symbol}\n"
            f"🎯 <b>Modo:</b> {mode}\n\n"
            f"💵 <b>Gross:</b> {self._fmt_money(gross)}\n"
            f"💸 <b>Fees:</b> {self._fmt_money(fees)}\n"
            f"📊 <b>PnL:</b> {self._fmt_money(pnl)}\n"
            f"⏱️ <b>Duración:</b> {dur}\n"
        )
        self._post(text)

    def send_totals(self, trades, wins, losses, pnl, fees, gross):
        text = (
            f"📊 <b>RESUMEN 1h</b>\n\n"
            f"🧾 <b>Operaciones:</b> {trades}  (✅ {wins} · ❌ {losses})\n"
            f"💵 <b>Gross:</b> {self._fmt_money(gross)}\n"
            f"💸 <b>Fees:</b> {self._fmt_money(fees)}\n"
            f"📈 <b>PnL neto:</b> {self._fmt_money(pnl)}\n"
        )
        self._post(text)

    @staticmethod
    def _fmt_money(x: float) -> str:
        sign = "" if x >= 0 else "-"
        v = abs(x)
        if v >= 1000:
            return f"{sign}${v:,.2f}"
        return f"{sign}${v:.2f}"
