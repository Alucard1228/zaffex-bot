#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zaffex FUTURES (CoinEx swap) — RSI scalping 1m (long + short)
Con TP/SL en exchange (intento) + OCO emulado, y watchdog local como respaldo.
- Entradas RSI: long < buy_th, short > sell_th (con histéresis)
- TP/SL por modo, timeout, cooldown tras pérdida
- Opcional: breakeven y trailing (local)
- Órdenes de salida:
    * Intenta colocar SL (stop-market reduceOnly) y TP parcial (limit reduceOnly) en CoinEx.
    * Si falla la colocación (parámetros no soportados / error red), cae a manejo local (watchdog).
    * OCO emulado: si se ejecuta uno, cancela el otro.
"""

import os, time, json, signal, math
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

import ccxt

# -------------------- helpers de entorno --------------------
def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    return v if (v is not None and v != "") else default

def _get_bool(name: str, default: str = "0") -> bool:
    return (_get(name, default) or "").strip().lower() in ("1","true","yes","y")

def _get_list(name: str, default: str = "") -> List[str]:
    raw = (_get(name, default) or "")
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
    "MARKET_TYPE": _get("COINEX_MARKET_TYPE","swap"),
    "MARGIN_MODE": _get("MARGIN_MODE","cross"),
    "LEVERAGE": _get_int("LEVERAGE","3"),
    "API_KEY": _get("API_KEY",""),
    "API_SECRET": _get("API_SECRET",""),
    "LIVE": _get_bool("LIVE","0"),
    "TIMEZONE": _get("TIMEZONE","America/New_York"),
    "SYMBOLS": _get_list("SYMBOLS","BTC/USDT,ETH/USDT"),
    "TIMEFRAME": _get("TIMEFRAME","1m"),
    "RSI_PERIOD": _get_int("RSI_PERIOD","14"),
    "RSI_BUY_THRESHOLD": _get_int("RSI_BUY_THRESHOLD","30"),
    "RSI_SELL_THRESHOLD": _get_int("RSI_SELL_THRESHOLD","70"),
    "RSI_HYSTERESIS": _get_float("RSI_HYSTERESIS","3.0"),
    "TP_PCT": _get_float("TAKE_PROFIT_PCT","0.40"),
    "SL_PCT": _get_float("STOP_LOSS_PCT","0.50"),
    "TP_PCT_A": _get_float("TP_PCT_A","0.35"),
    "SL_PCT_A": _get_float("SL_PCT_A","0.45"),
    "TP_PCT_M": _get_float("TP_PCT_M","0.50"),
    "SL_PCT_M": _get_float("SL_PCT_M","0.70"),
    "TP_PCT_C": _get_float("TP_PCT_C","0.80"),
    "SL_PCT_C": _get_float("SL_PCT_C","1.00"),
    "SIGNAL_COOLDOWN": _get_int("SIGNAL_COOLDOWN","300"),
    "LOSS_COOLDOWN_SEC": _get_int("LOSS_COOLDOWN_SEC","900"),
    "TIMEOUT_MIN": _get_int("TIMEOUT_MIN","15"),
    "FEE_RATE": _get_float("FEE_RATE","0.0008"),
    "LOT_SIZE_AGRESIVO": _get_int("LOT_SIZE_AGRESIVO","3"),
    "LOT_SIZE_MODERADO": _get_int("LOT_SIZE_MODERADO","4"),
    "LOT_SIZE_CONSERVADOR": _get_int("LOT_SIZE_CONSERVADOR","5"),
    "CAPITAL_AGRESIVO": _get_float("CAPITAL_AGRESIVO","50"),
    "CAPITAL_MODERADO": _get_float("CAPITAL_MODERADO","500"),
    "CAPITAL_CONSERVADOR": _get_float("CAPITAL_CONSERVADOR","10000"),
    "ENABLE_BREAKEVEN": _get_bool("ENABLE_BREAKEVEN","1"),
    "BE_TRIGGER_PCT": _get_float("BE_TRIGGER_PCT","0.30"),
    "BE_OFFSET_PCT": _get_float("BE_OFFSET_PCT","0.05"),
    "ENABLE_TRAIL": _get_bool("ENABLE_TRAIL","1"),
    "TRAIL_TRIGGER_PCT": _get_float("TRAIL_TRIGGER_PCT","0.40"),
    "TRAIL_STEP_PCT": _get_float("TRAIL_STEP_PCT","0.20"),
    "TELEGRAM_TOKEN": _get("TELEGRAM_TOKEN",""),
    "TELEGRAM_ALLOWED_IDS": _get_list("TELEGRAM_ALLOWED_IDS",""),
    "SUMMARY_ENABLED": _get_bool("SUMMARY_ENABLED","1"),
    "SUMMARY_EVERY_MIN": _get_int("SUMMARY_EVERY_MIN","60"),
    "ACCOUNT_START": _get_float("ACCOUNT_START","0"),
    "TP_PARTIAL_PCT": _get_float("TP_PARTIAL_PCT","50"),  # % qty para TP en exchange
}

# -------------------- tiempo --------------------
def now_tz() -> datetime:
    return datetime.now(ZoneInfo(CONFIG["TIMEZONE"]))

def ts() -> str:
    return now_tz().strftime("%Y-%m-%d %H:%M:%S%z")

def pct(x: float) -> float:
    return x/100.0

def compute_fee(notional: float) -> float:
    return notional * CONFIG["FEE_RATE"]

# -------------------- Telegram --------------------
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

# -------------------- CCXT (CoinEx swap) --------------------
def build_exchange():
    if CONFIG["EXCHANGE"].lower() != "coinex":
        raise RuntimeError("Este build es para CoinEx")
    ex = ccxt.coinex({
        "apiKey": CONFIG["API_KEY"],
        "secret": CONFIG["API_SECRET"],
        "enableRateLimit": True,
        "options": {"defaultType": CONFIG["MARKET_TYPE"]},  # 'swap'
    })
    return ex

def load_markets_retry(ex, retries: int = 5, delay: float = 2.0):
    for i in range(1, retries+1):
        try:
            return ex.load_markets()
        except Exception as e:
            print(f"[{ts()}] [WARN] load_markets {i}/{retries} -> {e}")
            time.sleep(delay*i)
    raise RuntimeError("load_markets failed")

def set_symbol_leverage_and_mode(ex, symbol: str):
    lev = CONFIG["LEVERAGE"]
    mmode = CONFIG["MARGIN_MODE"]
    try:
        if hasattr(ex, 'setLeverage'):
            ex.setLeverage(lev, symbol, params={})
    except Exception as e:
        print(f"[{ts()}] [LEV/WARN] {symbol} -> {e}")
    try:
        if hasattr(ex, 'setMarginMode'):
            ex.setMarginMode(mmode, symbol, params={})
    except Exception as e:
        print(f"[{ts()}] [MMODE/WARN] {symbol} -> {e}")

# -------------------- Mercado helpers --------------------
def fetch_price(ex, symbol: str) -> float:
    return float(ex.fetch_ticker(symbol)["last"])

def fetch_closes(ex, symbol: str, timeframe: str, limit: int = 200) -> List[float]:
    return [float(c[4]) for c in ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)]

def clamp_qty_price(ex, symbol: str, qty: float, price: float) -> Tuple[float, float]:
    return float(ex.amount_to_precision(symbol, qty)), float(ex.price_to_precision(symbol, price))

# -------------------- RSI Wilder --------------------
def rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0: gains += d
        else: losses += -d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = max(d, 0.0); l = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# -------------------- Caps / tamaños --------------------
def lots_by_mode(mode: str) -> int:
    return {"agresivo": CONFIG["LOT_SIZE_AGRESIVO"],
            "moderado": CONFIG["LOT_SIZE_MODERADO"],
            "conservador": CONFIG["LOT_SIZE_CONSERVADOR"]}.get(mode, 0)

def cap_by_mode(mode: str) -> float:
    return {"agresivo": CONFIG["CAPITAL_AGRESIVO"],
            "moderado": CONFIG["CAPITAL_MODERADO"],
            "conservador": CONFIG["CAPITAL_CONSERVADOR"]}.get(mode, 0.0)

def tp_sl_by_mode(mode: str) -> Tuple[float,float]:
    if mode == "agresivo": return CONFIG["TP_PCT_A"], CONFIG["SL_PCT_A"]
    if mode == "moderado": return CONFIG["TP_PCT_M"], CONFIG["SL_PCT_M"]
    if mode == "conservador": return CONFIG["TP_PCT_C"], CONFIG["SL_PCT_C"]
    return CONFIG["TP_PCT"], CONFIG["SL_PCT"]

def notional_per_lot(mode: str) -> float:
    lots = max(lots_by_mode(mode), 1)
    return cap_by_mode(mode) / lots

def size_from_notional(ex, symbol: str, notional: float, price: float) -> float:
    qty = notional / price
    qty, _ = clamp_qty_price(ex, symbol, qty, price)
    return qty

# -------------------- Persistencia --------------------
POSITIONS_FILE = "/tmp/zaffex_swap_positions.json"
STATS_FILE = "/tmp/zaffex_swap_stats.json"

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: pass
    return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f)
    except Exception: pass

# -------------------- Estado --------------------
RUNNING = True
positions: Dict[Tuple[str, str, str], Dict] = {}  # (symbol, mode, side) -> pos
last_signal_time: Dict[str, float] = {}
last_loss_time: Dict[Tuple[str,str,str], float] = {}
rsi_state: Dict[str, str] = {}  # 'below' | 'above' | 'mid'
STATS = None

def signal_handler(sig, frame):
    global RUNNING
    RUNNING = False
    print(f"[{ts()}] [STOP] shutdown signal]")

def stats_default():
    return {"trades":0,"wins":0,"losses":0,"pnl":0.0,"fees":0.0,"volume":0.0,
            "balance": float(CONFIG.get("ACCOUNT_START",0.0)),
            "h_trades":0,"h_wins":0,"h_losses":0,"h_pnl":0.0,"h_fees":0.0,"h_volume":0.0,
            "h_started": time.time(), "last_summary": time.time()}

def load_stats():
    s = load_json(STATS_FILE, None)
    if not s: s = stats_default()
    return s

def save_stats(s):
    save_json(STATS_FILE, s)

def stats_on_close(s, pnl_net: float, fees: float, notional_exit: float):
    s["trades"] += 1
    if pnl_net >= 0: s["wins"] += 1
    else: s["losses"] += 1
    s["pnl"] += pnl_net
    s["fees"] += fees
    s["volume"] += abs(notional_exit)
    s["balance"] = float(CONFIG.get("ACCOUNT_START",0.0)) + s["pnl"]
    s["h_trades"] += 1
    if pnl_net >= 0: s["h_wins"] += 1
    else: s["h_losses"] += 1
    s["h_pnl"] += pnl_net
    s["h_fees"] += fees
    s["h_volume"] += abs(notional_exit)

# -------------------- Telegram resumen --------------------
def maybe_send_summary():
    if not CONFIG.get("SUMMARY_ENABLED", True):
        return
    s = STATS
    now = time.time()
    every = int(CONFIG.get("SUMMARY_EVERY_MIN",60))*60
    if now - s.get("last_summary",0.0) >= every:
        window = {"trades": s["h_trades"], "wins": s["h_wins"], "losses": s["h_losses"],
                  "pnl": s["h_pnl"], "fees": s["h_fees"], "volume": s["h_volume"]}
        totals = {"trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
                  "pnl": s["pnl"], "fees": s["fees"], "volume": s["volume"], "balance": s["balance"]}
        try:
            if notifier and notifier.enabled():
                notifier.send_summary("last hour", window, totals)
        except Exception: pass
        s["h_trades"]=s["h_wins"]=s["h_losses"]=0
        s["h_pnl"]=s["h_fees"]=s["h_volume"]=0.0
        s["h_started"]=now; s["last_summary"]=now
        save_stats(s)

# -------------------- Gestión dinámica local --------------------
def _apply_breakeven_and_trailing(pos: Dict, price: float):
    # Breakeven
    if CONFIG["ENABLE_BREAKEVEN"] and not pos.get("be_done", False):
        be_trig = pct(CONFIG["BE_TRIGGER_PCT"])
        be_off = pct(CONFIG["BE_OFFSET_PCT"])
        if pos["side"]=="long":
            if price >= pos["entry"] * (1 + be_trig):
                new_sl = pos["entry"] * (1 + be_off)
                pos["local_sl"] = max(pos.get("local_sl", pos["sl"]), new_sl)
                pos["be_done"] = True
        else:
            if price <= pos["entry"] * (1 - be_trig):
                new_sl = pos["entry"] * (1 - be_off)
                pos["local_sl"] = min(pos.get("local_sl", pos["sl"]), new_sl)
                pos["be_done"] = True

    # Trailing
    if CONFIG["ENABLE_TRAIL"]:
        trig = pct(CONFIG["TRAIL_TRIGGER_PCT"])
        step = pct(CONFIG["TRAIL_STEP_PCT"])
        if pos["side"]=="long":
            if price >= pos["entry"] * (1 + trig):
                pos["trail_active"] = True
                pos["trail_anchor"] = max(pos.get("trail_anchor", pos["entry"]), price)
                trail_sl = pos["trail_anchor"] * (1 - step)
                pos["local_sl"] = max(pos.get("local_sl", pos["sl"]), trail_sl)
        else:
            if price <= pos["entry"] * (1 - trig):
                pos["trail_active"] = True
                pos["trail_anchor"] = min(pos.get("trail_anchor", pos["entry"]), price)
                trail_sl = pos["trail_anchor"] * (1 + step)
                pos["local_sl"] = min(pos.get("local_sl", pos["sl"]), trail_sl)

# -------------------- Exchange TP/SL helpers --------------------
def place_exchange_exits(ex, symbol: str, pos: Dict):
    """
    Intenta colocar SL (stop-market reduceOnly) y TP parcial (limit reduceOnly).
    Guarda ids en pos["sl_id"], pos["tp_id"]. Si algo falla, deja watchdog local.
    """
    qty = pos["qty"]
    side = pos["side"]
    entry = pos["entry"]
    tp = pos["tp"]
    sl = pos["sl"]

    tp_qty = qty * max(0.0, min(1.0, CONFIG["TP_PARTIAL_PCT"]/100.0))
    tp_qty = float(ex.amount_to_precision(symbol, tp_qty))
    sl_qty = qty  # SL debe cubrir todo

    # params genéricos ccxt; CoinEx suele aceptar reduceOnly, timeInForce
    base_params = {"reduceOnly": True, "timeInForce": "GTC"}

    # SL como stop-market reduceOnly
    try:
        if side == "long":
            # cerrar con sell si se activa SL
            sl_params = dict(base_params)
            sl_params.update({"stopPrice": float(sl)})
            try:
                order = ex.create_order(symbol, "stop", "sell", sl_qty, None, sl_params)
            except Exception:
                sl_params2 = dict(base_params); sl_params2.update({"triggerPrice": float(sl)})
                order = ex.create_order(symbol, "market", "sell", sl_qty, None, sl_params2)
        else:
            sl_params = dict(base_params)
            sl_params.update({"stopPrice": float(sl)})
            try:
                order = ex.create_order(symbol, "stop", "buy", sl_qty, None, sl_params)
            except Exception:
                sl_params2 = dict(base_params); sl_params2.update({"triggerPrice": float(sl)})
                order = ex.create_order(symbol, "market", "buy", sl_qty, None, sl_params2)
        pos["sl_id"] = order.get("id")
    except Exception as e:
        print(f"[{ts()}] [SL/EXC/WARN] {symbol} -> {e}")

    # TP parcial como limit reduceOnly
    try:
        if tp_qty > 0:
            if side == "long":
                order = ex.create_order(symbol, "limit", "sell", tp_qty, float(tp), dict(base_params))
            else:
                order = ex.create_order(symbol, "limit", "buy", tp_qty, float(tp), dict(base_params))
            pos["tp_id"] = order.get("id")
    except Exception as e:
        print(f"[{ts()}] [TP/EXC/WARN] {symbol} -> {e}")

def cancel_if_exists(ex, order_id: Optional[str], symbol: str):
    if not order_id: return
    try:
        ex.cancel_order(order_id, symbol)
    except Exception:
        pass

# -------------------- Apertura / Cierre --------------------
def open_position(ex, symbol: str, mode: str, side: str, reason: str):
    key = (symbol, mode, side)
    if key in positions:
        return
    if time.time() - last_loss_time.get(key, 0.0) < CONFIG["LOSS_COOLDOWN_SEC"]:
        return

    price = fetch_price(ex, symbol)
    notional = notional_per_lot(mode)
    if notional <= 0 or price <= 0:
        return
    qty = size_from_notional(ex, symbol, notional, price)
    if qty <= 0: return

    tp_pct, sl_pct = tp_sl_by_mode(mode)
    entry = price
    if side == "long":
        tp = ex.price_to_precision(symbol, entry * (1 + pct(tp_pct)))
        sl = ex.price_to_precision(symbol, entry * (1 - pct(sl_pct)))
    else:
        tp = ex.price_to_precision(symbol, entry * (1 - pct(tp_pct)))
        sl = ex.price_to_precision(symbol, entry * (1 + pct(sl_pct)))
    tp = float(tp); sl = float(sl)
    qty, entry = clamp_qty_price(ex, symbol, qty, entry)

    entry_fee = compute_fee(entry * qty)

    if CONFIG["LIVE"]:
        try:
            side_ccxt = "buy" if side=="long" else "sell"
            ex.create_order(symbol, "market", side_ccxt, qty)
        except Exception as e:
            print(f"[{ts()}] [OPEN/ERR] {symbol} {mode} {side} -> {e}")
            return

    positions[key] = {
        "entry": entry, "qty": qty, "tp": tp, "sl": sl, "side": side,
        "opened_at": time.time(), "entry_fee": entry_fee,
        "trail_active": False, "trail_anchor": entry, "be_done": False,
        # exchange exits
        "sl_id": None, "tp_id": None,
        # local fallback
        "local_sl": sl,
    }

    # intentar colocar SL/TP en exchange
    try:
        if CONFIG["LIVE"]:
            place_exchange_exits(ex, symbol, positions[key])
    except Exception as e:
        print(f"[{ts()}] [EXITS/WARN] {symbol} -> {e}")

    # Telegram
    try:
        if notifier and notifier.enabled():
            notifier.send_trade_open_rich_futures(
                symbol=symbol, mode=mode, side=side, lots=lots_by_mode(mode),
                timeframe=CONFIG["TIMEFRAME"], price=entry, tp=tp, sl=sl, qty=qty,
                reason=reason, tp_pct=tp_pct, sl_pct=sl_pct
            )
        else:
            tg(f"[OPEN] {symbol} {mode} {side} qty={qty} @ {entry} tp={tp} sl={sl} {reason}")
    except Exception: pass

def maybe_close_one(ex, key: Tuple[str,str,str]):
    pos = positions.get(key)
    if not pos: return
    symbol, mode, side = key
    price = fetch_price(ex, symbol)

    # dinámica local
    _apply_breakeven_and_trailing(pos, price)

    # watchdog local (si por lo que sea los exits del exchange no existen)
    sl_watch = pos.get("local_sl", pos["sl"])
    why = None
    if side=="long":
        if price >= pos["tp"]: why="TP(Local)"
        elif price <= sl_watch: why="SL(Local)"
    else:
        if price <= pos["tp"]: why="TP(Local)"
        elif price >= sl_watch: why="SL(Local)"

    if not why and (time.time() - pos["opened_at"] >= CONFIG["TIMEOUT_MIN"]*60):
        why = "TIMEOUT(Local)"

    # Si no hay motivo local, igualmente comprobar si TP/SL en exchange ya se ejecutaron
    if not why and CONFIG["LIVE"] and (pos.get("tp_id") or pos.get("sl_id")):
        try:
            if pos.get("tp_id"):
                info = ex.fetch_order(pos["tp_id"], symbol)
                if info and info.get("status") in ("closed","canceled"):
                    why = "TP(Exch)" if info.get("filled",0)>0 else None
            if not why and pos.get("sl_id"):
                info = ex.fetch_order(pos["sl_id"], symbol)
                if info and info.get("status") in ("closed","canceled"):
                    why = "SL(Exch)" if info.get("filled",0)>0 else None
        except Exception:
            pass

    if not why:
        return

    # cierre: cancela hermanas y manda reduceOnly para asegurar cierre total
    notional_entry = pos["entry"] * pos["qty"]
    notional_exit = price * pos["qty"]
    entry_fee = pos.get("entry_fee", compute_fee(notional_entry))
    exit_fee = compute_fee(notional_exit)
    gross = (price - pos["entry"]) * pos["qty"] * (1 if side=="long" else -1)
    pnl = gross - (entry_fee + exit_fee)

    if CONFIG["LIVE"]:
        try:
            cancel_if_exists(ex, pos.get("tp_id"), symbol)
            cancel_if_exists(ex, pos.get("sl_id"), symbol)
            side_ccxt = "sell" if side=="long" else "buy"
            ex.create_order(symbol, "market", side_ccxt, pos["qty"], None, {"reduceOnly": True})
        except Exception as e:
            print(f"[{ts()}] [CLOSE/ERR] {symbol} {mode} {side} -> {e}")

    # stats
    stats_on_close(STATS, pnl, (entry_fee+exit_fee), notional_exit)
    save_stats(STATS)

    # telegram
    try:
        if notifier and notifier.enabled():
            notifier.send_trade_close_rich_futures(
                symbol=symbol, mode=mode, side=side, reason=why,
                gross=gross, fees=(entry_fee+exit_fee), pnl=pnl,
                entry=pos["entry"], qty=pos["qty"], hold_sec=time.time()-pos["opened_at"]
            )
        else:
            tg(f"[CLOSE {why}] {symbol} {mode} {side} pnl={pnl:+.4f}")
    except Exception: pass

    if pnl < 0:
        last_loss_time[key] = time.time()

    positions.pop(key, None)
    save_json(POSITIONS_FILE, {"positions": {f"{s}|{m}|{d}":v for (s,m,d),v in positions.items()}})

# -------------------- Señales RSI --------------------
def rsi_state_update(symbol: str, rsi_val: float):
    prev = rsi_state.get(symbol, "mid")
    th_buy = CONFIG["RSI_BUY_THRESHOLD"]
    th_sell = CONFIG["RSI_SELL_THRESHOLD"]
    hyst = CONFIG["RSI_HYSTERESIS"]

    new_state = prev
    if rsi_val < th_buy: new_state = "below"
    elif rsi_val > th_sell: new_state = "above"
    else:
        if prev == "below" and rsi_val > th_buy + hyst: new_state = "mid"
        elif prev == "above" and rsi_val < th_sell - hyst: new_state = "mid"
    rsi_state[symbol] = new_state
    return prev, new_state

# -------------------- Boot summary --------------------
def boot_summary() -> str:
    return (
        f"[{ts()}] [BOOT] EXCHANGE={CONFIG['EXCHANGE']} TYPE={CONFIG['MARKET_TYPE']} LIVE={CONFIG['LIVE']} TZ={CONFIG['TIMEZONE']}\n"
        f"TF={CONFIG['TIMEFRAME']} Symbols={','.join(CONFIG['SYMBOLS'])}\n"
        f"RSI(p={CONFIG['RSI_PERIOD']}, buy<{CONFIG['RSI_BUY_THRESHOLD']}, sell>{CONFIG['RSI_SELL_THRESHOLD']}) Hyst={CONFIG['RSI_HYSTERESIS']}\n"
        f"TP/SL A={CONFIG['TP_PCT_A']}/{CONFIG['SL_PCT_A']}%  M={CONFIG['TP_PCT_M']}/{CONFIG['SL_PCT_M']}%  C={CONFIG['TP_PCT_C']}/{CONFIG['SL_PCT_C']}%\n"
        f"Cooldown={CONFIG['SIGNAL_COOLDOWN']}s  Timeout={CONFIG['TIMEOUT_MIN']}m  LossCD={CONFIG['LOSS_COOLDOWN_SEC']}s  FeeRate={CONFIG['FEE_RATE']}\n"
        f"Leverage={CONFIG['LEVERAGE']}x  MarginMode={CONFIG['MARGIN_MODE']}\n"
        f"Caps A={CONFIG['CAPITAL_AGRESIVO']}/x{CONFIG['LOT_SIZE_AGRESIVO']}, M={CONFIG['CAPITAL_MODERADO']}/x{CONFIG['LOT_SIZE_MODERADO']}, C={CONFIG['CAPITAL_CONSERVADOR']}/x{CONFIG['LOT_SIZE_CONSERVADOR']}\n"
        f"Keys api={_redact(CONFIG['API_KEY'])} secret={_redact(CONFIG['API_SECRET'])}"
    )

# -------------------- Main loop --------------------
def main():
    global STATS, RUNNING

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    STATS = load_stats()

    # restaurar posiciones (nota: ids de órdenes de exchange se pierden tras reinicio)
    persisted = load_json(POSITIONS_FILE, {}).get("positions", {})
    for k, v in persisted.items():
        try:
            s, m, d = k.split("|", 2)
            positions[(s, m, d)] = v
            # limpia ids porque tras reinicio no sabemos su estado
            positions[(s, m, d)]["tp_id"] = None
            positions[(s, m, d)]["sl_id"] = None
        except Exception:
            pass

    ex = build_exchange()
    _ = load_markets_retry(ex)

    for sym in CONFIG["SYMBOLS"]:
        try:
            set_symbol_leverage_and_mode(ex, sym)
        except Exception: pass

    try:
        if notifier and notifier.enabled():
            notifier.send_totals_rich(trades=STATS["trades"], wins=STATS["wins"], losses=STATS["losses"],
                                      pnl=STATS["pnl"], fees=STATS["fees"], volume=STATS["volume"], balance=STATS["balance"])
            notifier.send_boot_rich(live=CONFIG["LIVE"], exchange=f"{CONFIG['EXCHANGE']}({CONFIG['MARKET_TYPE']})",
                                    timeframe=CONFIG["TIMEFRAME"], symbols=",".join(CONFIG["SYMBOLS"]),
                                    rsi_p=CONFIG["RSI_PERIOD"], rsi_th=CONFIG["RSI_BUY_THRESHOLD"],
                                    tp_pct=CONFIG["TP_PCT"], sl_pct=CONFIG["SL_PCT"],
                                    cooldown_s=CONFIG["SIGNAL_COOLDOWN"], timeout_m=CONFIG["TIMEOUT_MIN"], loss_cd_s=CONFIG["LOSS_COOLDOWN_SEC"],
                                    cap_a=CONFIG["CAPITAL_AGRESIVO"], lot_a=CONFIG["LOT_SIZE_AGRESIVO"],
                                    cap_m=CONFIG["CAPITAL_MODERADO"], lot_m=CONFIG["LOT_SIZE_MODERADO"],
                                    cap_c=CONFIG["CAPITAL_CONSERVADOR"], lot_c=CONFIG["LOT_SIZE_CONSERVADOR"])
    except Exception as _e:
        print(f"[{ts()}] [WARN] boot telegram: {_e}]")

    while RUNNING:
        loop_start = time.time()

        # cerrar si corresponde
        for key in list(positions.keys()):
            try:
                maybe_close_one(ex, key)
            except Exception as e:
                print(f"[{ts()}] [CLOSE/WARN] {key} -> {e}")

        # señales
        for symbol in CONFIG["SYMBOLS"]:
            try:
                last_sig = last_signal_time.get(symbol, 0.0)
                closes = fetch_closes(ex, symbol, CONFIG["TIMEFRAME"], limit=max(200, CONFIG["RSI_PERIOD"]+50))
                rsi = rsi_wilder(closes, CONFIG["RSI_PERIOD"])
                if rsi is None: continue
                price = closes[-1]
                prev, new = rsi_state_update(symbol, rsi)

                if time.time() - last_sig >= CONFIG["SIGNAL_COOLDOWN"]:
                    if prev != "below" and new == "below":
                        ctx = f"(RSI={rsi:.2f} < {CONFIG['RSI_BUY_THRESHOLD']})"
                        for mode in ("agresivo","moderado","conservador"):
                            open_position(ex, symbol, mode, "long", ctx)
                        last_signal_time[symbol] = time.time()

                    if prev != "above" and new == "above":
                        ctx = f"(RSI={rsi:.2f} > {CONFIG['RSI_SELL_THRESHOLD']})"
                        for mode in ("agresivo","moderado","conservador"):
                            open_position(ex, symbol, mode, "short", ctx)
                        last_signal_time[symbol] = time.time()

            except Exception as e:
                print(f"[{ts()}] [LOOP/WARN] {symbol} -> {e}")

        try:
            maybe_send_summary()
        except Exception:
            pass

        elapsed = time.time() - loop_start
        time.sleep(max(1.0 - elapsed, 0.1))

if __name__ == "__main__":
    main()
