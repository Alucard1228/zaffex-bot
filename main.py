#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zaffex bot — CoinEx spot
- RSI(14) < threshold on 1m => abre 3 modos (agresivo/moderado/conservador) simultáneamente
- TP/SL por % + timeout por trade
- Fees en entrada y salida
- Cooldown tras pérdida por (symbol, mode)
- Tamaños por modo = capital_modo / lotes_modo
- Estadísticas persistentes + resumen cada hora por Telegram
- Singleton lock para Railway
- **FIX**: posiciones por clave (symbol, mode) para no pisar órdenes
"""

import os
import time
import signal
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import ccxt

# ---------------- Utilidades de tiempo / log ----------------
def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

_last_debug: Dict[str, float] = {}
def debug_throttled(tag: str, text: str, every_sec: float = 5.0):
    now = time.time()
    last = _last_debug.get(tag, 0.0)
    if now - last >= every_sec:
        print(f"[{ts()}] [DEBUG] {tag} | {text}")
        _last_debug[tag] = now

# ---------------- Singleton lock ----------------
_lock_file = "/tmp/zaffex.lock"
_lock_fd = None

def acquire_lock() -> bool:
    global _lock_fd
    try:
        _lock_fd = os.open(_lock_file, os.O_CREAT | os.O_RDWR)
        try:
            import fcntl
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as e:
            print(f"[{ts()}] [WARN] Another instance seems running (lock). {e}")
            return False
        try:
            os.ftruncate(_lock_fd, 0)
        except Exception:
            pass
        os.write(_lock_fd, str(os.getpid()).encode("utf-8"))
        return True
    except Exception as e:
        print(f"[{ts()}] [WARN] Could not create/open lock file: {e}")
        return False

def release_lock():
    global _lock_fd
    try:
        if _lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(_lock_fd)
            except Exception:
                pass
            _lock_fd = None
            try:
                if os.path.exists(_lock_file):
                    os.remove(_lock_file)
            except Exception:
                pass
    except Exception:
        pass

# ---------------- Config (ENV) ----------------
def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    return v if (v is not None and v != "") else default

def _get_bool(name: str, default: str = "0") -> bool:
    return (_get(name, default) or "").strip().lower() in ("1","true","yes","y")

def _get_list(name: str, default: str = "") -> List[str]:
    raw = _get(name, default) or ""
    return [s.strip() for s in raw.split(",") if s.strip()]

def _get_float(name: str, default: str) -> float:
    try: return float(_get(name, default))
    except Exception: return float(default)

def _get_int(name: str, default: str) -> int:
    try: return int(float(_get(name, default)))
    except Exception: return int(default)

def _redact(s: str) -> str:
    if not s: return ""
    return (s[:3]+"..."+s[-3:]) if len(s)>6 else "***"

CONFIG: Dict = {
    "EXCHANGE": _get("EXCHANGE","coinex"),
    "API_KEY": _get("API_KEY",""),
    "API_SECRET": _get("API_SECRET",""),
    "LIVE": _get_bool("LIVE","0"),
    "SYMBOLS": _get_list("SYMBOLS","BTC/USDT,ETH/USDT"),
    "TIMEFRAME": _get("TIMEFRAME","1m"),
    "RSI_PERIOD": _get_int("RSI_PERIOD","14"),
    "RSI_THRESHOLD": _get_int("RSI_THRESHOLD","30"),
    "TP_PCT": _get_float("TAKE_PROFIT_PCT","1.0"),
    "SL_PCT": _get_float("STOP_LOSS_PCT","1.2"),
    "SIGNAL_COOLDOWN": _get_int("SIGNAL_COOLDOWN","300"),
    "FEE_RATE": _get_float("FEE_RATE","0.002"),
    "TIMEOUT_MIN": _get_int("TIMEOUT_MIN","25"),
    "LOSS_COOLDOWN_SEC": _get_int("LOSS_COOLDOWN_SEC","900"),
    "LOT_SIZE_AGRESIVO": _get_int("LOT_SIZE_AGRESIVO","3"),
    "LOT_SIZE_MODERADO": _get_int("LOT_SIZE_MODERADO","4"),
    "LOT_SIZE_CONSERVADOR": _get_int("LOT_SIZE_CONSERVADOR","5"),
    "CAPITAL_AGRESIVO": _get_float("CAPITAL_AGRESIVO","50"),
    "CAPITAL_MODERADO": _get_float("CAPITAL_MODERADO","500"),
    "CAPITAL_CONSERVADOR": _get_float("CAPITAL_CONSERVADOR","10000"),
    "TELEGRAM_TOKEN": _get("TELEGRAM_TOKEN",""),
    "TELEGRAM_ALLOWED_IDS": _get_list("TELEGRAM_ALLOWED_IDS",""),
    "SUMMARY_ENABLED": _get_bool("SUMMARY_ENABLED","1"),
    "SUMMARY_EVERY_MIN": _get_int("SUMMARY_EVERY_MIN","60"),
    "ACCOUNT_START": _get_float("ACCOUNT_START","0"),
}

def boot_summary() -> str:
    return (
        f"[{ts()}] [BOOT] EXCHANGE={CONFIG['EXCHANGE']} LIVE={CONFIG['LIVE']} TZ=UTC\n"
        f"TF={CONFIG['TIMEFRAME']} Symbols={','.join(CONFIG['SYMBOLS'])}\n"
        f"RSI(p={CONFIG['RSI_PERIOD']},th={CONFIG['RSI_THRESHOLD']}) TP={CONFIG['TP_PCT']}% SL={CONFIG['SL_PCT']}% Cooldown={CONFIG['SIGNAL_COOLDOWN']}s\n"
        f"FeeRate={CONFIG['FEE_RATE']} Timeout={CONFIG['TIMEOUT_MIN']}m LossCooldown={CONFIG['LOSS_COOLDOWN_SEC']}s\n"
        f"Caps A={CONFIG['CAPITAL_AGRESIVO']}/x{CONFIG['LOT_SIZE_AGRESIVO']}, M={CONFIG['CAPITAL_MODERADO']}/x{CONFIG['LOT_SIZE_MODERADO']}, C={CONFIG['CAPITAL_CONSERVADOR']}/x{CONFIG['LOT_SIZE_CONSERVADOR']}\n"
        f"Keys api={_redact(CONFIG['API_KEY'])} secret={_redact(CONFIG['API_SECRET'])}"
    )

# ---------------- Telegram ----------------
notifier = None
try:
    from telegram_notifier import TelegramNotifier
    if CONFIG["TELEGRAM_TOKEN"] and CONFIG["TELEGRAM_ALLOWED_IDS"]:
        notifier = TelegramNotifier(CONFIG["TELEGRAM_TOKEN"], ",".join(CONFIG["TELEGRAM_ALLOWED_IDS"]))
except Exception as _e:
    print(f"[{ts()}] [WARN] Telegram init: {_e}]")

def tg(msg: str):
    print(msg)
    try:
        if notifier and notifier.enabled():
            notifier.send(msg)
    except Exception as _e:
        print(f"[{ts()}] [WARN] Telegram send failed: {_e}]")

# ---------------- CCXT / CoinEx ----------------
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
    for i in range(1, retries+1):
        try:
            return ex.load_markets()
        except Exception as e:
            print(f"[{ts()}] [WARN] load_markets {i}/{retries} -> {e}")
            time.sleep(delay*i)
    raise RuntimeError("load_markets failed")

# ---------------- RSI (Wilder) ----------------
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = max(d, 0.0)
        l = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# ---------------- Helpers mercado ----------------
def now_ts() -> float: return time.time()
def compute_fee(notional: float) -> float: return notional * CONFIG["FEE_RATE"]
def fetch_ticker_price(ex, symbol: str) -> float: return float(ex.fetch_ticker(symbol)["last"])
def fetch_ohlcv_closes(ex, symbol: str, timeframe: str, limit: int = 200) -> List[float]:
    return [float(c[4]) for c in ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)]
def clamp_qty_price(ex, symbol: str, qty: float, price: float) -> Tuple[float, float]:
    return float(ex.amount_to_precision(symbol, qty)), float(ex.price_to_precision(symbol, price))

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
    if mode in ("conservador","conservativo"):
        return CONFIG["CAPITAL_CONSERVADOR"] / CONFIG["LOT_SIZE_CONSERVADOR"]
    return 0.0

# ---------------- Estadísticas persistentes ----------------
STATS_FILE = "/tmp/zaffex_stats.json"
STATS = None

def _stats_default():
    return {
        "trades": 0, "wins": 0, "losses": 0,
        "pnl": 0.0, "fees": 0.0, "volume": 0.0,
        "balance": float(CONFIG.get("ACCOUNT_START", 0.0)),
        "h_trades": 0, "h_wins": 0, "h_losses": 0,
        "h_pnl": 0.0, "h_fees": 0.0, "h_volume": 0.0,
        "h_started": 0.0, "last_summary": 0.0
    }

def load_stats():
    try:
        import json
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    s = _stats_default()
    s["h_started"] = time.time()
    s["last_summary"] = time.time()
    return s

def save_stats(s):
    try:
        import json
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f)
    except Exception:
        pass

def stats_on_close(s, gross: float, fees: float, pnl: float, notional_exit: float):
    s["trades"] += 1
    if pnl >= 0: s["wins"] += 1
    else: s["losses"] += 1
    s["pnl"] += pnl
    s["fees"] += fees
    s["volume"] += abs(notional_exit)
    s["balance"] = float(CONFIG.get("ACCOUNT_START", 0.0)) + s["pnl"]
    s["h_trades"] += 1
    if pnl >= 0: s["h_wins"] += 1
    else: s["h_losses"] += 1
    s["h_pnl"] += pnl
    s["h_fees"] += fees
    s["h_volume"] += abs(notional_exit)

def maybe_send_summary(s):
    if not CONFIG.get("SUMMARY_ENABLED", True):
        return
    now = time.time()
    every = int(CONFIG.get("SUMMARY_EVERY_MIN", 60)) * 60
    if now - s.get("last_summary", 0.0) >= every:
        window = {"trades": s["h_trades"], "wins": s["h_wins"], "losses": s["h_losses"],
                  "pnl": s["h_pnl"], "fees": s["h_fees"], "volume": s["h_volume"]}
        totals = {"trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
                  "pnl": s["pnl"], "fees": s["fees"], "volume": s["volume"], "balance": s["balance"]}
        try:
            if notifier and notifier.enabled():
                notifier.send_summary("last hour", window, totals)
        except Exception:
            pass
        s["h_trades"] = s["h_wins"] = s["h_losses"] = 0
        s["h_pnl"] = s["h_fees"] = s["h_volume"] = 0.0
        s["h_started"] = now
        s["last_summary"] = now
        save_stats(s)

# ---------------- Estado del bot ----------------
RUNNING = True
# CLAVE: posiciones por (symbol, mode)
positions: Dict[Tuple[str, str], Dict] = {}
last_signal_time: Dict[str, float] = {}
last_loss_time: Dict[Tuple[str, str], float] = {}
equity: float = 0.0

def signal_handler(sig, frame):
    global RUNNING
    RUNNING = False
    print(f"[{ts()}] [STOP] Shutdown signal received; stopping loop...]")

# ---------------- Abrir / Cerrar ----------------
def open_position(ex, markets, symbol: str, mode: str, context: str = ""):
    key = (symbol, mode)
    if key in positions:
        debug_throttled(f"{symbol}:{mode}", "already open, skip open")
        return
    if now_ts() - last_loss_time.get(key, 0) < CONFIG["LOSS_COOLDOWN_SEC"]:
        debug_throttled(f"{symbol}:{mode}", "in loss cooldown, skip open")
        return
    notional_cap = notional_per_mode(mode)
    if notional_cap <= 0:
        return
    qty, price = size_from_notional(ex, markets, symbol, notional_cap)
    if qty <= 0:
        debug_throttled(f"{symbol}:{mode}", "qty <= 0 (min cost/amount?)")
        return
    tp = float(ex.price_to_precision(symbol, price * (1 + CONFIG["TP_PCT"] / 100.0)))
    sl = float(ex.price_to_precision(symbol, price * (1 - CONFIG["SL_PCT"] / 100.0)))
    entry_fee = compute_fee(price * qty)

    if CONFIG["LIVE"]:
        try:
            ex.create_order(symbol, "market", "buy", qty)
        except Exception as e:
            print(f"[{ts()}] [OPEN/ERR] {symbol} {mode} {e}")
            return

    positions[key] = {
        "entry": price,
        "qty": qty,
        "tp": tp,
        "sl": sl,
        "opened_at": now_ts(),
        "entry_fee": entry_fee,
    }

    try:
        if notifier and notifier.enabled():
            notifier.send_trade_open(symbol, mode, qty, price, tp, sl, context)
        else:
            tg(f"[{ts()}] [OPEN] {symbol} {mode.upper()} qty={qty:.8f} @ {price:.4f} tp={tp:.4f} sl={sl:.4f} {context}")
    except Exception:
        pass

def maybe_close_one(ex, key: Tuple[str, str]):
    global equity, STATS
    pos = positions.get(key)
    if not pos:
        return
    symbol, mode = key
    price = fetch_ticker_price(ex, symbol)
    why = None
    if price >= pos["tp"]:
        why = "TP"
    elif price <= pos["sl"]:
        why = "SL"
    elif now_ts() - pos.get("opened_at", now_ts()) >= CONFIG["TIMEOUT_MIN"] * 60:
        why = "TIMEOUT"
    if not why:
        debug_throttled(f"{symbol}:{mode}", f"holding @ {price:.4f} (tp={pos['tp']:.4f} sl={pos['sl']:.4f})")
        return

    notional_entry = pos["entry"] * pos["qty"]
    notional_exit = price * pos["qty"]
    entry_fee = pos.get("entry_fee", compute_fee(notional_entry))
    exit_fee = compute_fee(notional_exit)
    gross_pnl = (price - pos["entry"]) * pos["qty"]
    pnl_net = gross_pnl - (entry_fee + exit_fee)

    if CONFIG["LIVE"]:
        try:
            ccxt_response = None
            ccxt_response = ex.create_order(symbol, "market", "sell", pos["qty"])
        except Exception as e:
            print(f"[{ts()}] [CLOSE/ERR] {symbol} {mode} sell failed: {e}")

    equity += pnl_net

    try:
        stats_on_close(STATS, gross_pnl, (entry_fee + exit_fee), pnl_net, notional_exit)
        save_stats(STATS)
    except Exception:
        pass

    try:
        if notifier and notifier.enabled():
            notifier.send_trade_close(symbol, mode, why, gross_pnl, (entry_fee + exit_fee), pnl_net)
        else:
            tg(f"[{ts()}] [CLOSE] {symbol} {mode.upper()} reason={why} gross={gross_pnl:+.6f} fees={(entry_fee+exit_fee):.6f} pnl={pnl_net:+.6f}")
    except Exception:
        pass

    if pnl_net < 0:
        last_loss_time[key] = now_ts()

    positions.pop(key, None)

# ---------------- Main loop ----------------
def main():
    global RUNNING, STATS

    if not acquire_lock():
        return

    try:
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        STATS = load_stats()

        ex = build_exchange()
        markets = load_markets_retry(ex)

        boot = boot_summary()
        print(boot)
        try:
            totals_line = f"[{ts()}] [TOTALS] trades={STATS['trades']} wins={STATS['wins']} losses={STATS['losses']} pnl={STATS['pnl']:.2f} fees={STATS['fees']:.2f} volume={STATS['volume']:.2f} balance={STATS['balance']:.2f}"
            print(totals_line)
            if notifier and notifier.enabled():
                notifier.send(totals_line)
        except Exception:
            pass
        try:
            if notifier and notifier.enabled():
                notifier.send(boot)
        except Exception as _e:
            print(f"[{ts()}] [WARN] Telegram boot send failed: {_e}]")

        while RUNNING:
            loop_start = now_ts()

            # Primero intentamos cerrar las posiciones existentes de todos los keys
            # (evita que un cooldown por símbolo bloquee cierres)
            for key in list(positions.keys()):
                try:
                    maybe_close_one(ex, key)
                except Exception as e:
                    print(f"[{ts()}] [CLOSE/WARN] {key} -> {e}")

            # Luego escaneamos símbolos para nuevas señales
            for symbol in CONFIG["SYMBOLS"]:
                try:
                    last_sig = last_signal_time.get(symbol, 0.0)
                    # El cooldown afecta solo a nuevos OPEN, no a los CLOSE
                    closes = fetch_ohlcv_closes(ex, symbol, CONFIG["TIMEFRAME"], limit=max(200, CONFIG["RSI_PERIOD"] + 50))
                    rsi_val = rsi_wilder(closes, CONFIG["RSI_PERIOD"])
                    if rsi_val is None:
                        continue
                    last_price = closes[-1]
                    debug_throttled(symbol, f"Precio: {last_price:.2f} | RSI: {rsi_val:.2f}", every_sec=5.0)

                    if now_ts() - last_sig >= CONFIG["SIGNAL_COOLDOWN"]:
                        if rsi_val < CONFIG["RSI_THRESHOLD"]:
                            entry_ctx = f"(RSI={rsi_val:.2f} < th={CONFIG['RSI_THRESHOLD']})"
                            for mode in ("agresivo","moderado","conservador"):
                                open_position(ex, markets, symbol, mode, context=entry_ctx)
                            last_signal_time[symbol] = now_ts()
                except Exception as e:
                    print(f"[{ts()}] [LOOP/WARN] {symbol} -> {e}")

            try:
                maybe_send_summary(STATS)
            except Exception:
                pass

            elapsed = now_ts() - loop_start
            time.sleep(max(1.0 - elapsed, 0.1))
    finally:
        print(f"[{ts()}] [EXIT] Loop stopped.")
        release_lock()

if __name__ == "__main__":
    main()
