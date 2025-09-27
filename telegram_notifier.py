import re
import requests
from typing import List, Optional

def _fmt_money(x: float) -> str:
    sign = "-" if x < 0 else ""
    v = abs(x)
    return f"{sign}${v:,.2f}"

def _fmt_qty(x: float) -> str:
    return f"{x:.6f}"

def _fmt_pct(x: float) -> str:
    return f"{x:.1f}%"

def _fmt_dur(secs: float) -> str:
    try:
        secs = max(0, int(secs))
    except Exception:
        return "n/a"
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def _extract_rsi_from_reason(reason: str) -> Optional[float]:
    # espera "(RSI=25.79 < th=30)" o similar
    if not reason:
        return None
    m = re.search(r"RSI\s*=\s*([0-9]+(?:\.[0-9]+)?)", reason, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None

class TelegramNotifier:
    def __init__(self, bot_token: str, allowed_ids_csv: str):
        self.bot_token = (bot_token or "").strip()
        self.allowed_ids: List[str] = [s.strip() for s in (allowed_ids_csv or "").split(",") if s.strip()]
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def enabled(self) -> bool:
        return bool(self.bot_token and self.allowed_ids)

    def _send_raw(self, chat_id: str, text: str, parse_mode: Optional[str] = None, disable_web_page_preview: bool = True):
        if not self.enabled():
            return
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": disable_web_page_preview}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(self.base_url, json=payload, timeout=10)
            r.raise_for_status()
        except Exception:
            # fallback sin parse_mode
            if parse_mode:
                try:
                    payload.pop("parse_mode", None)
                    requests.post(self.base_url, json=payload, timeout=10)
                except Exception:
                    pass

    def send(self, text: str, parse_mode: Optional[str] = None):
        for cid in self.allowed_ids:
            self._send_raw(cid, text, parse_mode=parse_mode)

    # -------- mensajes “rich” --------
    def send_trade_open_rich(self, *, symbol: str, mode: str, lots: int, timeframe: str,
                             price: float, tp: float, sl: float, qty: float,
                             reason: str, tp_pct: float, sl_pct: float,
                             account_balance: Optional[float] = None):
        header = "🔥 APERTURA DE POSICIÓN" if mode.lower() == "agresivo" else ("⚡ APERTURA DE POSICIÓN" if mode.lower() == "moderado" else "🛡️ APERTURA DE POSICIÓN")
        rr = (tp_pct / sl_pct) if sl_pct > 0 else 0.0
        rsi = _extract_rsi_from_reason(reason)
        notional = price * qty

        lines = []
        lines.append(f"{header}\n")
        lines.append(f"🪙 Símbolo: {symbol}")
        lines.append(f"🎯 Modo: {mode.capitalize()}")
        lines.append(f"📊 Lotes: {lots}\n")
        lines.append(f"💰 Entrada: {_fmt_money(price)}")
        lines.append(f"🛑 Stop Loss: {_fmt_money(sl)} ({_fmt_pct(sl_pct)})")
        lines.append(f"✅ Take Profit: {_fmt_money(tp)} ({_fmt_pct(tp_pct)})")
        lines.append(f"⚖️ Risk/Reward: 1:{rr:.1f}\n")
        if rsi is not None:
            lines.append(f"📈 RSI(14): {rsi:.1f}")
        lines.append(f"⏰ Timeframe: {timeframe}")
        lines.append(f"💼 Tamaño: {_fmt_money(notional)} ({_fmt_qty(qty)} {symbol.split('/')[0]})")
        if account_balance is not None:
            lines.append(f"🏦 Equity: {_fmt_money(account_balance)}")
        msg = "\n".join(lines)
        self.send(msg)

    def send_trade_close_rich(self, *, symbol: str, mode: str, reason: str,
                              gross: float, fees: float, pnl: float,
                              entry: float, qty: float, hold_sec: float):
        emoji = "✅" if pnl >= 0 else "❌"
        notional = entry * qty if entry and qty else 0.0
        roi = (pnl / notional * 100.0) if notional > 0 else 0.0
        header = f"{emoji} CIERRE DE POSICIÓN"
        lines = []
        lines.append(f"{header}\n")
        lines.append(f"🪙 Símbolo: {symbol}")
        lines.append(f"🎯 Modo: {mode.capitalize()}\n")
        lines.append(f"💵 Gross: {_fmt_money(gross)}")
        lines.append(f"💸 Fees: {_fmt_money(fees)}")
        lines.append(f"📊 PnL: {_fmt_money(pnl)} ({_fmt_pct(roi)})")
        lines.append(f"⏱️ Duración: {_fmt_dur(hold_sec)}")
        lines.append(f"🧾 Motivo: {reason}")
        msg = "\n".join(lines)
        self.send(msg)

    # -------- compat (resúmenes/legacy) --------
    def fmt_money(self, x: float) -> str:
        sign = "-" if x < 0 else ""
        v = abs(x)
        return f"{sign}{v:,.2f}"

    def send_trade_open(self, symbol: str, mode: str, qty: float, price: float, tp: float, sl: float, reason: str):
        msg = (
            f"🟢 OPEN\n"
            f"• {symbol} {mode.upper()}\n"
            f"• qty={qty:.8f} @ {price:,.4f}\n"
            f"• TP={tp:,.4f} | SL={sl:,.4f}\n"
            f"• reason: {reason}"
        )
        self.send(msg)

    def send_trade_close(self, symbol: str, mode: str, reason: str, gross: float, fees: float, pnl: float,
                         entry: float = None, qty: float = None, hold_sec: float = None):
        emoji = "✅" if pnl >= 0 else "❌"
        roi = None
        if entry and qty:
            notional = entry * qty
            if notional > 0:
                roi = pnl / notional * 100.0
        dur = _fmt_dur(hold_sec) if hold_sec is not None else "n/a"
        roi_line = f" | ROI={roi:.2f}%" if roi is not None else ""
        msg = (
            f"{emoji} CLOSE ({reason})\n"
            f"• {symbol} {mode.upper()}\n"
            f"• gross={self.fmt_money(gross)} | fees={self.fmt_money(fees)}\n"
            f"• PnL={self.fmt_money(pnl)}{roi_line} | dur={dur}"
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
