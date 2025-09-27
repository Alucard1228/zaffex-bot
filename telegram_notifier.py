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
    return f"{x:.2f}%"

def _fmt_dur(secs: float) -> str:
    try:
        secs = max(0, int(secs))
    except Exception:
        return "n/a"
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def _extract_rsi_from_reason(reason: str) -> Optional[float]:
    if not reason: return None
    m = re.search(r"RSI\s*=\s*([0-9]+(?:\.[0-9]+)?)", reason, re.IGNORECASE)
    if m:
        try: return float(m.group(1))
        except Exception: return None
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
            if parse_mode:
                try:
                    payload.pop("parse_mode", None)
                    requests.post(self.base_url, json=payload, timeout=10)
                except Exception:
                    pass

    def send(self, text: str, parse_mode: Optional[str] = None):
        for cid in self.allowed_ids:
            self._send_raw(cid, text, parse_mode=parse_mode)

    # Mensaje de arranque
    def send_boot_rich(self, *, live: bool, exchange: str, timeframe: str, symbols: str,
                       rsi_p: int, rsi_th: int, tp_pct: float, sl_pct: float,
                       cooldown_s: int, timeout_m: int, loss_cd_s: int,
                       cap_a: float, lot_a: int, cap_m: float, lot_m: int, cap_c: float, lot_c: int):
        live_tag = "LIVE ✅" if live else "PAPER 🧪"
        lines = []
        lines.append("🚀 INICIO DEL BOT\n")
        lines.append(f"🧩 Exchange: {exchange} | Modo: {live_tag}")
        lines.append(f"⏰ Timeframe: {timeframe}")
        lines.append(f"🧪 Estrategia: RSI({rsi_p}) — Buy < {rsi_th} / Sell > {rsi_th+40}")
        lines.append(f"🎯 TP/SL (fallback): {_fmt_pct(tp_pct)} / {_fmt_pct(sl_pct)}")
        lines.append(f"⛓️ Cooldown: {cooldown_s}s | ⏱️ Timeout: {timeout_m}m | 🧊 LossCD: {loss_cd_s}s")
        lines.append(f"📦 Símbolos: {symbols}")
        lines.append(f"💰 Caps: A {cap_a}/x{lot_a} · M {cap_m}/x{lot_m} · C {cap_c}/x{lot_c}")
        self.send("\n".join(lines))

    # Totales
    def send_totals_rich(self, *, trades: int, wins: int, losses: int,
                         pnl: float, fees: float, volume: float, balance: float):
        lines = []
        lines.append("📊 ESTADO\n")
        lines.append(f"• Trades: {trades} (W:{wins} / L:{losses})")
        lines.append(f"• PnL: {_fmt_money(pnl)} | Fees: {_fmt_money(fees)}")
        lines.append(f"• Volumen: {_fmt_money(volume)} | Balance: {_fmt_money(balance)}")
        self.send("\n".join(lines))

    # Apertura de trade (futuros)
    def send_trade_open_rich_futures(self, *, symbol: str, mode: str, side: str, lots: int, timeframe: str,
                                     price: float, tp: float, sl: float, qty: float,
                                     reason: str, tp_pct: float, sl_pct: float):
        header = "🔥 LONG ABIERTO" if side=="long" else "❄️ SHORT ABIERTO"
        rr = (tp_pct / sl_pct) if sl_pct > 0 else 0.0
        rsi = _extract_rsi_from_reason(reason)
        notional = price * qty
        lines = []
        lines.append(f"{header}\n")
        lines.append(f"🪙 Símbolo: {symbol}")
        lines.append(f"🎯 Modo: {mode.capitalize()} — Lado: {side.upper()}")
        lines.append(f"📊 Lotes: {lots}\n")
        lines.append(f"💰 Entrada: {_fmt_money(price)}")
        lines.append(f"🛑 Stop Loss: {_fmt_money(sl)} ({_fmt_pct(sl_pct)})")
        lines.append(f"✅ Take Profit: {_fmt_money(tp)} ({_fmt_pct(tp_pct)})")
        lines.append(f"⚖️ Risk/Reward: 1:{rr:.1f}\n")
        if rsi is not None:
            lines.append(f"📈 RSI(14): {rsi:.1f}")
        lines.append(f"⏰ Timeframe: {timeframe}")
        lines.append(f"💼 Tamaño: {_fmt_money(notional)} ({_fmt_qty(qty)} {symbol.split('/')[0]})")
        self.send("\n".join(lines))

    # Cierre de trade (futuros)
    def send_trade_close_rich_futures(self, *, symbol: str, mode: str, side: str, reason: str,
                                      gross: float, fees: float, pnl: float,
                                      entry: float, qty: float, hold_sec: float):
        emoji = "✅" if pnl >= 0 else "❌"
        notional = entry * qty if entry and qty else 0.0
        roi = (pnl / notional * 100.0) if notional > 0 else 0.0
        header = f"{emoji} CIERRE {side.upper()} ({reason})"
        lines = []
        lines.append(f"{header}\n")
        lines.append(f"🪙 Símbolo: {symbol}")
        lines.append(f"🎯 Modo: {mode.capitalize()}\n")
        lines.append(f"💵 Gross: {_fmt_money(gross)}")
        lines.append(f"💸 Fees: {_fmt_money(fees)}")
        lines.append(f"📊 PnL: {_fmt_money(pnl)} ({_fmt_pct(roi)})")
        lines.append(f"⏱️ Duración: {_fmt_dur(hold_sec)}")
        self.send("\n".join(lines))

    def fmt_money(self, x: float) -> str:
        sign = "-" if x < 0 else ""
        v = abs(x)
        return f"{sign}{v:,.2f}"

    # Resumen cada 1h (o el intervalo que configures)
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
