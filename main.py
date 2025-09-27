#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Zaffex - CoinEx spot (Railway-ready)

- Signal: RSI(14) < 30 on 1m (long-only)
- Exits: TP/SL in percent
- Correct fees: notional-based (entry + exit)
- Per-trade timeout
- Loss cooldown per (symbol, mode)
- Sizing per mode: cap / lots
- Env-only config (Railway). Safe defaults for the rest.
- Logs include UTC timestamp and entry reason (RSI). Guards against opening on shutdown.
"""

import os
import time
import signal
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import ccxt

# ------------- Time and logging helpers -------------

def ts() -> str:
    """UTC timestamp for logs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

# ------------- Config (ENV only; safe defaults) -------------

def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    return v if (v is not None and v != "") else default

def _get_bool(name: str, default: str = "0") -> bool:
    return (_get(name, default) or "").strip().lower() in ("1", "true", "yes", "y")

def _get_list(name: str, default: str = "") -> List[str]:
    raw = _get(name, default) or ""
    return [s.strip() for s in raw.split(",") if s.strip()]

def _get_float(name: str, default: str) -> float:
    try:
        return float(_get(name, default))
    except Exception:
        return float(default)

def _get_int(name: str, default: str) -> int:
    try:
        return int(float(_get(name, default)))
    except Exception:
        return int(default)

def _redact(s: str) -> str:
    if not s:
        return ""
    return (s[:3] + "..." + s[-3:]) if len(s) > 6 else "***"

CONFIG: Dict = {
    # Exchange / keys
    "EXCHANGE": _get("EXCHANGE", "coinex"),
    "API_KEY": _get("API_KEY", ""),
    "API_SECRET": _get("API_SECRET", ""),
    "LIVE": _get_bool("LIVE", "0"),

    # Trading
    "SYMBOLS": _get_list("SYMBOLS", "BTC/USDT,ETH/USDT"),
    "TIMEFRAME": _get("TIMEFRAME", "1m"),
    "RSI_PERIOD": _get_int("RSI_PERIOD", "14"),
    "RSI_THRESHOLD": _get_int("RSI_THRESHOLD", "30"),
    "TP_PCT": _get_float("TAKE_PROFIT_PCT", "1.0"),
    "SL_PCT": _get_float("STOP_LOSS_PCT", "1.2"),
    "SIGNAL_COOLDOWN": _get_int("SIGNAL_COOLDOWN", "300"),  # seconds

    # Fees / Timeout / Cooldown
    "FEE_RATE": _get_float("FEE_RATE", "0.002"),    # 0.20% per side (CoinEx taker typical)
    "TIMEOUT_MIN": _get_int("TIMEOUT_MIN", "25"),
    "LOSS_COOLDOWN_SEC": _get_int("LOSS_COOLDOWN_SEC", "900"),

    # Sizing per mode (cap and lots)
    "LOT_SIZE_AGRESIVO": _get_int("LOT_SIZE_AGRESIVO", "3"),
    "LOT_SIZE_MODERADO": _get_int("LOT_SIZE_MODERADO", "4"),
    "LOT_SIZE_CONSERVADOR": _get_int("LOT_SIZE_CONSERVADOR", "5"),
    "CAPITAL_AGRESIVO": _get_float("CAPITAL_AGRESIVO", "50"),
    "CAPITAL_MODERADO": _get_float("CAPITAL_MODERADO", "500"),
    "CAPITAL_CONSERVADOR": _get_float("CAPITAL_CONSERVADOR", "10000"),

    # Telegram
    "TELEGRAM_TOKEN": _get("TELEGRAM_TOKEN", ""),
    "TELEGRAM_ALLOWED_IDS": _get_list("TELEGRAM_ALLOWED_IDS", ""),
}

def boot_summary() -> str:
    return (
        f"[{ts()}] [BOOT] EXCHANGE={CONFIG['EXCHANGE']} LIVE={CONFIG['LIVE']} TZ=UTC\n"
        f"TF={CONFIG['TIMEFRAME']} Symbols={','.join(CONFIG['SYMBOLS'])}\n"
        f"RSI(p={CONFIG['RSI_PERIOD']},th={CONFIG['RSI_THRESHOLD']}) "
        f"TP={CONFIG['TP_PCT']}% SL={CONFIG['SL_PCT']}% Cooldown={CONFIG['SIGNAL_COOLDOWN']}s\n"
        f"FeeRate={CONFIG['FEE_RATE']} Timeout={CONFIG['TIMEOUT_MIN']}m LossCooldown={CONFIG['LOSS_COOLDOWN_SEC']}s\n"
        f"Caps A={CONFIG['CAPITAL_AGRESIVO']}/x{CONFIG['LOT_SIZE_AGRESIVO']}, "
        f"M={CONFIG['CAPITAL_MODERADO']}/x{CONFIG['LOT_SIZE_MODERADO']}, "
        f"C={CONFIG['CAPITAL_CONSERVADOR']}/x{CONFIG['LOT_SIZE_CONSERVADOR']}\n"
        f"Keys api={_redact(CONFIG['API_KEY'])} secret={_redact(CONFIG['API_SECRET'])}"
    )

# ------------- Telegram (optional) -------------

notifier = None
try:
    from telegram_notifier import TelegramNotifier
    if CONFIG["TELEGRAM_TOKEN"] and CONFIG["TELEGRAM_ALLOWED_IDS"]:
        notifier = TelegramNotifier(CONFIG["TELEGRAM_TOKEN"], ",".join(CONFIG["TELEGRAM_ALLOWED_IDS"]))
except Exception as _e:
    print(f"[{ts()}] [WARN] Telegram init: {_e}")

def tg(msg: str):
    print(msg)
    try:
        if notifier and notifier.enabled():
            notifier.send(msg)
    except Exception as _e:
        print(f"[{ts()}] [WARN] Telegram send failed: {_e}")

# ------------- CCXT / CoinEx -------------

def build_exchange():
    if CONFIG["EXCHANGE"].lower() != "coinex":
        raise RuntimeError("This main.py is prepared for CoinEx spot")
    return ccxt.coinex({
        "apiKey": CONFIG["API_KEY"],
        "secret": CONFIG["API_SECRET"],
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

def load_markets_retry(ex, retries: int = 5, delay: float = 2.0):
    for i in range(1, retries + 1):
        try:
            return ex.load_markets()
        except Exception as e:
            print(f"[{ts()}] [WARN] load_markets {i}/{retries} -> {e}")
            time.sleep(delay * i)
    raise RuntimeError("load_markets failed")

# ------------- RSI (Wilder) over OHLCV -------------

def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses += -delta
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# ------------- Market helpers and sizing -------------

def now_ts() -> float:
    return time.time()

def compute_fee(notional: float) -> float:
    return notional * CONFIG["FEE_RATE"]

def fetch_ticker_price(ex, symbol: str) -> float:
    t = ex.fetch_ticker(symbol)
    return float(t["last"])

def fetch_ohlcv_closes(ex, symbol: str, timeframe: str, limit: int = 200) -> List[float]:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return [float(c[4]) for c in ohlcv]

def clamp_qty_price(ex, symbol: str, qty: float, price: float) -> Tuple[float, float]:
    qty_p = float(ex.amount_to_precision(symbol, qty))
    price_p = float(ex.price_to_precision(symbol, price))
    return qty_p, price_p

def size_from_notional(ex, markets, symbol: str, notional_cap: float) -> Tuple[float, float]:
    price = fetch_ticker_price(ex, symbol)
    if price <= 0:
        return 0.0, price
    qty = notional_cap / price
    qty, price = clamp_qty_price(ex, symbol, qty, price)
    m = markets.get(symbol, {})
    min_cost = (m.get("limits", {}).get("cost", {}) or {}).get("min", 0) or 0
    if min_cost and qty * price < min_cost:
        return 0.0, price
    min_qty = (m.get("limits", {}).get("amount", {}) or {}).get("min", 0) or 0
    if min_qty and qty < min_qty:
        qty = float(min_qty)
        qty, price = clamp_qty_price(ex, symbol, qty, price)
    return qty, price

def notional_per_mode(mode: str) -> float:
    if mode == "agresivo":
        return CONFIG["CAPITAL_AGRESIVO"] / CONFIG["LOT_SIZE_AGRESIVO"]
    if mode == "moderado":
        return CONFIG["CAPITAL_MODERADO"] / CONFIG["LOT_SIZE_MODERADO"]
    if mode in ("conservador", "conservativo"):
        return CONFIG["CAPITAL_CONSERVADOR"] / CONFIG["LOT_SIZE_CONSERVADOR"]
    return 0.0

# ------------- Bot state -------------

RUNNING = True
positions: Dict[str, Dict] = {}              # one position per symbol
last_signal_time: Dict[str, float] = {}      # signal cooldown per symbol
last_loss_time: Dict[Tuple[str, str], float] = {}  # cooldown per (symbol, mode)
equity: float = 0.0

def signal_handler(sig, frame):
    global RUNNING
    RUNNING = False
    print(f"[{ts()}] [STOP] Shutdown signal received; stopping loop...")

# ------------- Open / Close -------------

def open_position(ex, markets, symbol: str, mode: str, context: str = ""):
    if not RUNNING:
        return  # guard: do not open when shutting down
    key_cool = (symbol, mode)
    if now_ts() - last_loss_time.get(key_cool, 0) < CONFIG["LOSS_COOLDOWN_SEC"]:
        return

    notional_cap = notional_per_mode(mode)
    if notional_cap <= 0:
        return

    qty, price = size_from_notional(ex, markets, symbol, notional_cap)
    if qty <= 0:
        return

    tp = price * (1 + CONFIG["TP_PCT"] / 100.0)
    sl = price * (1 - CONFIG["SL_PCT"] / 100.0)
    _, tp = clamp_qty_price(ex, symbol, qty, tp)
    _, sl = clamp_qty_price(ex, symbol, qty, sl)

    notional_entry = price * qty
    entry_fee = compute_fee(notional_entry)

    if CONFIG["LIVE"]:
        try:
            ex.create_order(symbol, "market", "buy", qty)
        except Exception as e:
            print(f"[{ts()}] [OPEN/ERR] {symbol} {mode} {e}")
            return

    positions[symbol] = {
        "mode": mode,
        "entry": price,
        "qty": qty,
        "tp": tp,
        "sl": sl,
        "opened_at": now_ts(),
        "entry_fee": entry_fee,
    }
    tg(f"[{ts()}] [OPEN] {symbol} {mode.upper()} qty={qty:.8f} @ {price:.4f} tp={tp:.4f} sl={sl:.4f} {context}")

def maybe_close(ex, symbol: str):
    global equity
    pos = positions.get(symbol)
    if not pos:
        return
    price = fetch_ticker_price(ex, symbol)

    should_close, reason = False, None
    if price >= pos["tp"]:
        should_close, reason = True, "TP"
    elif price <= pos["sl"]:
        should_close, reason = True, "SL"
    elif now_ts() - pos.get("opened_at", now_ts()) >= CONFIG["TIMEOUT_MIN"] * 60:
        should_close, reason = True, "TIMEOUT"

    if not should_close:
        return

    notional_entry = pos["entry"] * pos["qty"]
    notional_exit = price * pos["qty"]
    entry_fee = pos.get("entry_fee", compute_fee(notional_entry))
    exit_fee = compute_fee(notional_exit)
    gross_pnl = (price - pos["entry"]) * pos["qty"]
    pnl_net = gross_pnl - (entry_fee + exit_fee)

    if CONFIG["LIVE"]:
        try:
            ex.create_order(symbol, "market", "sell", pos["qty"])
        except Exception as e:
            print(f"[{ts()}] [CLOSE/ERR] {symbol} {pos['mode']} sell failed: {e}")

    equity += pnl_net
    tg(f"[{ts()}] [CLOSE] {symbol} {pos['mode'].upper()} reason={reason} gross={gross_pnl:+.6f} fees={(entry_fee+exit_fee):.6f} pnl={pnl_net:+.6f}")

    if pnl_net < 0:
        last_loss_time[(symbol, pos["mode"])] = now_ts()

    positions.pop(symbol, None)

# ------------- Main loop -------------

def main():
    global RUNNING

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    ex = build_exchange()
    markets = load_markets_retry(ex)

    boot = boot_summary()
    print(boot)
    if notifier and notifier.enabled():
        tg(boot)

    while RUNNING:
        loop_start = now_ts()

        for symbol in CONFIG["SYMBOLS"]:
            try:
                # Signal cooldown per symbol
                last_sig = last_signal_time.get(symbol, 0.0)
                if now_ts() - last_sig < CONFIG["SIGNAL_COOLDOWN"]:
                    maybe_close(ex, symbol)
                    continue

                # RSI signal
                closes = fetch_ohlcv_closes(ex, symbol, CONFIG["TIMEFRAME"], limit=max(200, CONFIG["RSI_PERIOD"] + 50))
                rsi_val = rsi_wilder(closes, CONFIG["RSI_PERIOD"])
                if rsi_val is None:
                    continue

                # Lightweight debug
                try:
                    last_price = closes[-1]
                    print(f"[{ts()}] [DEBUG] {symbol} | Price: {last_price:.2f} | RSI: {rsi_val:.2f}")
                except Exception:
                    pass

                # Manage exits
                maybe_close(ex, symbol)

                # Entry (long-only) if RSI < threshold and no open position
                if RUNNING and (symbol not in positions) and (rsi_val < CONFIG["RSI_THRESHOLD"]):
                    entry_ctx = f"(RSI={rsi_val:.2f} < th={CONFIG['RSI_THRESHOLD']})"
                    for mode in ("agresivo", "moderado", "conservador"):
                        open_position(ex, markets, symbol, mode, context=entry_ctx)
                    last_signal_time[symbol] = now_ts()

            except Exception as e:
                print(f"[{ts()}] [LOOP/WARN] {symbol} -> {e}")

        elapsed = now_ts() - loop_start
        sleep_s = max(1.0 - elapsed, 0.1)
        time.sleep(sleep_s)

    print(f"[{ts()}] [EXIT] Loop stopped.")

if __name__ == "__main__":
    main()
