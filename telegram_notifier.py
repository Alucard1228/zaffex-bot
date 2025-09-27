import os
import requests
from typing import List

class TelegramNotifier:
    def __init__(self, bot_token: str, allowed_ids_csv: str):
        self.bot_token = (bot_token or "").strip()
        self.allowed_ids: List[str] = [s.strip() for s in (allowed_ids_csv or "").split(",") if s.strip()]
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def enabled(self) -> bool:
        return bool(self.bot_token and self.allowed_ids)

    def _send_raw(self, chat_id: str, text: str, parse_mode: str = None, disable_web_page_preview: bool = True):
        if not self.enabled():
            return
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": disable_web_page_preview}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(self.base_url, json=payload, timeout=10)
            r.raise_for_status()
        except Exception:
            if parse_mode:
                try:
                    payload.pop("parse_mode", None)
                    r = requests.post(self.base_url, json=payload, timeout=10)
                    r.raise_for_status()
                except Exception:
                    pass

    def send(self, text: str, parse_mode: str = None):
        for cid in self.allowed_ids:
            self._send_raw(cid, text, parse_mode=parse_mode)

    def fmt_money(self, x: float) -> str:
        sign = "-" if x < 0 else ""
        v = abs(x)
        return f"{sign}{v:,.2f}"

    def fmt_qty(self, x: float) -> str:
        return f"{x:.8f}"

    def send_trade_open(self, symbol: str, mode: str, qty: float, price: float, tp: float, sl: float, reason: str):
        msg = (
            f"🟢 OPEN\n"
            f"• {symbol} {mode.upper()}\n"
            f"• qty={self.fmt_qty(qty)} @ {price:,.4f}\n"
            f"• TP={tp:,.4f} | SL={sl:,.4f}\n"
            f"• reason: {reason}"
        )
        self.send(msg)

    def send_trade_close(self, symbol: str, mode: str, reason: str, gross: float, fees: float, pnl: float):
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} CLOSE ({reason})\n"
            f"• {symbol} {mode.upper()}\n"
            f"• gross={self.fmt_money(gross)} | fees={self.fmt_money(fees)}\n"
            f"• PnL={self.fmt_money(pnl)}"
        )
        self.send(msg)

    def send_summary(self, horizon: str, stats: dict, totals: dict):
        msg = (
            f"📊 SUMMARY ({horizon})\n"
            f"• trades={stats.get('trades',0)} | wins={stats.get('wins',0)} | losses={stats.get('losses',0)}\n"
            f"• pnl={self.fmt_money(stats.get('pnl',0.0))} | fees={self.fmt_money(stats.get('fees',0.0))}\n"
            f"• volume={self.fmt_money(stats.get('volume',0.0))}\n"
            f"— totals —\n"
            f"• trades={totals.get('trades',0)} | wins={totals.get('wins',0)} | losses={totals.get('losses',0)}\n"
            f"• pnl={self.fmt_money(totals.get('pnl',0.0))} | fees={self.fmt_money(totals.get('fees',0.0))}\n"
            f"• volume={self.fmt_money(totals.get('volume',0.0))} | balance={self.fmt_money(totals.get('balance',0.0))}"
        )
        self.send(msg)
