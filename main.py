# -*- coding: utf-8 -*-
"""
Bot Zaffex - Réplica exacta + Resumen horario y capital en tiempo real
"""

import os
import time
import json
import signal
import logging
from datetime import datetime, timezone

import requests
import ccxt

try:
    import zoneinfo
    _has_zoneinfo = True
except Exception:
    _has_zoneinfo = False

from telegram_notifier import TelegramNotifier

# ---------------------------
# Utilidades de entorno
# ---------------------------
def getenv_str(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v is not None and v != "" else default

def getenv_float(key: str, default: float) -> float:
    v = os.getenv(key)
    try:
        return float(v)
    except Exception:
        return float(default)

def getenv_int(key: str, default: int) -> int:
    v = os.getenv(key)
    try:
        return int(v)
    except Exception:
        return int(default)

def now_tz():
    tzname = getenv_str("TIMEZONE", "UTC")
    if _has_zoneinfo:
        try:
            tz = zoneinfo.ZoneInfo(tzname)
        except Exception:
            tz = timezone.utc
    else:
        tz = timezone.utc
    return datetime.now(tz)

# ---------------------------
# Configuración
# ---------------------------
EXCHANGE_ID = getenv_str("EXCHANGE", "coinex").lower()
LIVE = getenv_int("LIVE", 0)
TIMEFRAME = getenv_str("TIMEFRAME", "1m")
SYMBOLS = [s.strip() for s in getenv_str("SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT").split(",") if s.strip()]
FEE_RATE = getenv_float("FEE_RATE", 0.0005)

RSI_PERIOD = getenv_int("RSI_PERIOD", 14)
RSI_BUY_THRESHOLD = getenv_float("RSI_BUY_THRESHOLD", 30.0)
RSI_SELL_THRESHOLD = getenv_float("RSI_SELL_THRESHOLD", 70.0)
RSI_HYST = getenv_float("RSI_HYSTERESIS", 3.0)

TAKE_PROFIT_PCT = getenv_float("TAKE_PROFIT_PCT", 1.5)
STOP_LOSS_PCT = getenv_float("STOP_LOSS_PCT", 1.0)

TP_PARTIAL_PCT = getenv_float("TP_PARTIAL_PCT", 40.0)
ENABLE_BE = getenv_int("ENABLE_BREAKEVEN", 1) == 1
BE_TRIGGER_PCT = getenv_float("BE_TRIGGER_PCT", 0.60)
BE_OFFSET_PCT = getenv_float("BE_OFFSET_PCT", 0.05)

ENABLE_TRAIL = getenv_int("ENABLE_TRAIL", 1) == 1
TRAIL_TRIGGER_PCT = getenv_float("TRAIL_TRIGGER_PCT", 1.00)
TRAIL_STEP_PCT = getenv_float("TRAIL_STEP_PCT", 0.25)

SIGNAL_COOLDOWN = getenv_int("SIGNAL_COOLDOWN", 300)
TIMEOUT_MIN = getenv_int("TIMEOUT_MIN", 25)
LOSS_COOLDOWN_SEC = getenv_int("LOSS_COOLDOWN_SEC", 900)

CAP_A = min(getenv_float("CAPITAL_AGRESIVO", 20.0), 1000.0)
LOTS_A = max(1, getenv_int("LOT_SIZE_AGRESIVO", 3))

CAP_M = min(getenv_float("CAPITAL_MODERADO", 20.0), 1000.0)
LOTS_M = max(1, getenv_int("LOT_SIZE_MODERADO", 4))

CAP_C = min(getenv_float("CAPITAL_CONSERVADOR", 20.0), 1000.0)
LOTS_C = max(1, getenv_int("LOT_SIZE_CONSERVADOR", 5))

TELEGRAM_TOKEN = getenv_str("TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED_IDS = [i.strip() for i in getenv_str("TELEGRAM_ALLOWED_IDS", "").split(",") if i.strip()]

POLL_SEC = getenv_int("POLL_SEC", 5)

# Capital inicial para paper trading
INITIAL_CAPITAL = getenv_float("INITIAL_CAPITAL", 20.0)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[{asctime}] [{levelname}] {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M:%S%z",
)
log = logging.getLogger("bot")

notifier = TelegramNotifier(
    token=TELEGRAM_TOKEN,
    allowed_chat_ids=TELEGRAM_ALLOWED_IDS
)

# ---------------------------
# Exchange y RSI (igual que antes)
# ---------------------------
def is_swap_symbol(market) -> bool:
    if not market:
        return False
    t = market.get("type")
    return t == "swap"

def build_exchange():
    kwargs = {"enableRateLimit": True}
    ex_class = getattr(ccxt, EXCHANGE_ID)
    exchange = ex_class(kwargs)
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_SECRET")
    if LIVE == 1 and api_key and api_secret:
        exchange.apiKey = api_key
        exchange.secret = api_secret
    return exchange

exchange = build_exchange()
markets = {}
try:
    markets = exchange.load_markets()
except Exception as e:
    log.warning(f"No se pudieron cargar mercados: {e}")

def rsi(values, period=14):
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(values) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))

def fetch_price_and_rsi(symbol: str):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=200)
        closes = [c[4] for c in ohlcv]
        last_close = closes[-1]
        the_rsi = rsi(closes, RSI_PERIOD)
        return float(last_close), float(the_rsi) if the_rsi is not None else None
    except Exception as e:
        log.warning(f"fetch_ohlcv fallo {symbol}: {e}")
        try:
            t = exchange.fetch_ticker(symbol)
            last = t.get("last") or t.get("close")
            return float(last), None
        except Exception as e2:
            log.error(f"fetch_ticker fallo {symbol}: {e2}")
            return None, None

# ---------------------------
# Estado global
# ---------------------------
class Position:
    def __init__(self, symbol, mode, side, qty, entry, tp, sl, opened_ts, notional):
        self.symbol = symbol
        self.mode = mode
        self.side = side
        self.qty = qty
        self.entry = entry
        self.tp = tp
        self.sl = sl
        self.opened_ts = opened_ts
        self.notional = notional
        self.closed = False
        self.closed_ts = None
        self.reason = ""
        self.partial_done = False
        self.be_armed = False
        self.trail_armed = False
        self.trail_stop = None

    def __repr__(self):
        return f"<Pos {self.symbol} {self.mode} {self.side} qty={self.qty} @ {self.entry}>"

positions = {}
last_signal_ts = {}
last_loss_ts = 0

# Contadores globales
pnl_counters = {"trades": 0, "wins": 0, "losses": 0, "gross": 0.0, "fees": 0.0, "pnl": 0.0}

# Capital y métricas horarias
current_capital = INITIAL_CAPITAL
hourly_stats = {
    "start_time": time.time(),
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "pnl": 0.0
}

last_summary_ts = time.time()

MODES = [
    ("agresivo", CAP_A, LOTS_A),
    ("moderado", CAP_M, LOTS_M),
    ("conservador", CAP_C, LOTS_C),
]

def per_lot_cap(cap: float, lots: int) -> float:
    return cap / max(1, float(lots))

def compute_order_qty(symbol: str, dollar_size: float, price: float):
    if price <= 0:
        return 0.0
    return float(round(dollar_size / price, 6))

def compute_tp_sl(side: str, entry: float, tp_pct: float, sl_pct: float):
    if side == "long":
        tp = entry * (1.0 + tp_pct / 100.0)
        sl = entry * (1.0 - sl_pct / 100.0)
    else:
        tp = entry * (1.0 - tp_pct / 100.0)
        sl = entry * (1.0 + sl_pct / 100.0)
    return tp, sl

def price_hit_take(side: str, price: float, tp: float) -> bool:
    return (side == "long" and price >= tp) or (side == "short" and price <= tp)

def price_hit_stop(side: str, price: float, sl: float) -> bool:
    return (side == "long" and price <= sl) or (side == "short" and price >= sl)

def unrealized_pct(side: str, price: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    change = (price - entry) / entry * 100.0
    return change if side == "long" else -change

def maybe_arm_be_and_trail(pos: Position, price: float):
    if ENABLE_BE and not pos.be_armed:
        if unrealized_pct(pos.side, price, pos.entry) >= BE_TRIGGER_PCT:
            offset = BE_OFFSET_PCT / 100.0
            if pos.side == "long":
                pos.sl = pos.entry * (1.0 + offset)
            else:
                pos.sl = pos.entry * (1.0 - offset)
            pos.be_armed = True
    if ENABLE_TRAIL and not pos.trail_armed:
        if unrealized_pct(pos.side, price, pos.entry) >= TRAIL_TRIGGER_PCT:
            step = TRAIL_STEP_PCT / 100.0
            if pos.side == "long":
                pos.trail_stop = price * (1.0 - step)
            else:
                pos.trail_stop = price * (1.0 + step)
            pos.trail_armed = True
    if ENABLE_TRAIL and pos.trail_armed and pos.trail_stop is not None:
        step = TRAIL_STEP_PCT / 100.0
        if pos.side == "long":
            new_stop = price * (1.0 - step)
            if new_stop > pos.trail_stop:
                pos.trail_stop = new_stop
        else:
            new_stop = price * (1.0 + step)
            if new_stop < pos.trail_stop:
                pos.trail_stop = new_stop

def hit_trailing_exit(pos: Position, price: float) -> bool:
    if not (ENABLE_TRAIL and pos.trail_armed and pos.trail_stop):
        return False
    return (pos.side == "long" and price <= pos.trail_stop) or (pos.side == "short" and price >= pos.trail_stop)

def place_open_order_live(symbol, side, qty):
    mkt = markets.get(symbol)
    try:
        if mkt and is_swap_symbol(mkt):
            try:
                if hasattr(exchange, "set_margin_mode"):
                    exchange.set_margin_mode("cross", symbol)
            except Exception:
                pass
            try:
                if hasattr(exchange, "set_leverage"):
                    exchange.set_leverage(3, symbol)
            except Exception:
                pass
        side_ccxt = "buy" if side == "long" else "sell"
        return exchange.create_order(symbol, type="market", side=side_ccxt, amount=qty)
    except Exception as e:
        log.error(f"create_order fallo {symbol} {side} qty={qty}: {e}")
        return None

def close_order_live(symbol, side, qty):
    try:
        side_ccxt = "sell" if side == "long" else "buy"
        return exchange.create_order(symbol, type="market", side=side_ccxt, amount=qty)
    except Exception as e:
        log.error(f"close_order fallo {symbol} {side} qty={qty}: {e}")
        return None

def fee_cost(notional: float) -> float:
    return notional * (FEE_RATE * 2.0)

def record_close(pos: Position, close_price: float, reason: str):
    global current_capital, hourly_stats, last_loss_ts

    pos.closed = True
    pos.closed_ts = time.time()
    pos.reason = reason

    side = pos.side
    entry = pos.entry
    notional = pos.notional
    ret_pct = (close_price - entry) / entry * 100.0
    if side == "short":
        ret_pct = -ret_pct
    gross = notional * (ret_pct / 100.0)
    fees = fee_cost(notional)
    pnl = gross - fees

    # Actualizar capital global
    current_capital += pnl

    # Contadores globales
    pnl_counters["trades"] += 1
    pnl_counters["gross"] += gross
    pnl_counters["fees"] += fees
    pnl_counters["pnl"] += pnl
    if pnl >= 0:
        pnl_counters["wins"] += 1
    else:
        pnl_counters["losses"] += 1
        last_loss_ts = time.time()

    # Métricas horarias
    hourly_stats["trades"] += 1
    if pnl >= 0:
        hourly_stats["wins"] += 1
    else:
        hourly_stats["losses"] += 1
    hourly_stats["pnl"] += pnl

    # Notificación
    notifier.send_close(
        symbol=pos.symbol,
        mode=pos.mode.title(),
        side=side.upper(),
        reason=reason,
        gross=gross,
        fees=fees,
        pnl=pnl,
        duration_sec=int(pos.closed_ts - pos.opened_ts),
        entry=entry,
        exit_price=close_price,
        current_capital=current_capital
    )
    return pnl

def open_position(symbol: str, mode: str, side: str, lot_usd: float, price: float, rsi_value=None):
    qty = compute_order_qty(symbol, lot_usd, price)
    if qty <= 0:
        return None
    tp, sl = compute_tp_sl(side, price, TAKE_PROFIT_PCT, STOP_LOSS_PCT)
    notional = lot_usd
    pos = Position(symbol, mode, side, qty, price, tp, sl, time.time(), notional)
    positions[(symbol, mode)] = pos

    if LIVE == 1:
        place_open_order_live(symbol, side, qty)

    notifier.send_open(
        symbol=symbol,
        mode=mode.title(),
        side=side.upper(),
        lots=MODES[[m[0] for m in MODES].index(mode)][2],
        entry=price,
        sl=sl,
        tp=tp,
        timeframe=TIMEFRAME,
        size_usd=lot_usd,
        qty=qty,
        rsi_value=rsi_value
    )
    return pos

def try_close_logic(symbol: str, mode: str, price: float):
    pos = positions.get((symbol, mode))
    if not pos or pos.closed:
        return
    elapsed = time.time() - pos.opened_ts
    maybe_arm_be_and_trail(pos, price)

    if hit_trailing_exit(pos, price):
        if LIVE == 1:
            close_order_live(pos.symbol, pos.side, pos.qty)
        record_close(pos, price, "TRAIL")
        return

    if not pos.partial_done and price_hit_take(pos.side, price, pos.tp):
        part_pct = max(0.0, min(TP_PARTIAL_PCT, 100.0)) / 100.0
        if part_pct > 0.0:
            close_qty = pos.qty * part_pct
            if LIVE == 1:
                close_order_live(pos.symbol, pos.side, close_qty)
            pos.qty -= close_qty
            pos.partial_done = True
            if ENABLE_BE and not pos.be_armed:
                offset = BE_OFFSET_PCT / 100.0
                if pos.side == "long":
                    pos.sl = pos.entry * (1.0 + offset)
                else:
                    pos.sl = pos.entry * (1.0 - offset)
                pos.be_armed = True
            notifier.send_partial_tp(symbol, mode.title(), pos.side.upper(), TP_PARTIAL_PCT, price)

    if price_hit_stop(pos.side, price, pos.sl):
        if LIVE == 1:
            close_order_live(pos.symbol, pos.side, pos.qty)
        record_close(pos, price, "SL")
        return

    if elapsed >= TIMEOUT_MIN * 60:
        if LIVE == 1:
            close_order_live(pos.symbol, pos.side, pos.qty)
        record_close(pos, price, "TIMEOUT")
        return

def signal_allowed(symbol: str, mode: str, side: str) -> bool:
    now_ts = time.time()
    key = (symbol, mode, side)
    last_ts = last_signal_ts.get(key, 0)
    if now_ts - last_ts < SIGNAL_COOLDOWN:
        return False
    if last_loss_ts and (now_ts - last_loss_ts < LOSS_COOLDOWN_SEC):
        return False
    return True

def mark_signal(symbol: str, mode: str, side: str):
    last_signal_ts[(symbol, mode, side)] = time.time()

def maybe_open_trades(symbol: str, rsi_value: float, price: float):
    long_sig = rsi_value is not None and (rsi_value < RSI_BUY_THRESHOLD - RSI_HYST)
    short_sig = rsi_value is not None and (rsi_value > RSI_SELL_THRESHOLD + RSI_HYST)

    for mode, cap, lots in MODES:
        if cap <= 0:
            continue
        key = (symbol, mode)
        pos = positions.get(key)
        if pos and not pos.closed:
            continue
        lot_usd = per_lot_cap(cap, lots)
        if lot_usd <= 0:
            continue

        if long_sig and signal_allowed(symbol, mode, "long"):
            open_position(symbol, mode, "long", lot_usd, price, rsi_value)
            mark_signal(symbol, mode, "long")

        elif short_sig and signal_allowed(symbol, mode, "short"):
            open_position(symbol, mode, "short", lot_usd, price, rsi_value)
            mark_signal(symbol, mode, "short")

def heartbeat_summary():
    global last_summary_ts, hourly_stats, current_capital
    now_ts = time.time()
    if now_ts - last_summary_ts >= 3600:
        notifier.send_hourly_summary(
            total_capital=current_capital,
            trades=hourly_stats["trades"],
            wins=hourly_stats["wins"],
            losses=hourly_stats["losses"],
            hourly_pnl=hourly_stats["pnl"]
        )
        hourly_stats = {
            "start_time": now_ts,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0
        }
        last_summary_ts = now_ts

_running = True
def handle_sigterm(signum, frame):
    global _running
    _running = False
    log.info("[STOP] Señal de apagado recibida")

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

def boot_banner():
    caps = f"A {CAP_A}/x{LOTS_A} · M {CAP_M}/x{LOTS_M} · C {CAP_C}/x{LOTS_C}"
    mode_txt = "LIVE" if LIVE == 1 else "PAPER 🧪"
    txt = (
        f"🚀 BOT ZAFFEX - CON RESUMEN HORARIO\n"
        f"🧩 Exchange: {EXCHANGE_ID} | Modo: {mode_txt}\n"
        f"💰 Capital inicial: ${INITIAL_CAPITAL:,.2f}\n"
        f"📊 RSI({RSI_PERIOD}) — Buy < {RSI_BUY_THRESHOLD} / Sell > {RSI_SELL_THRESHOLD}\n"
        f"🎯 TP/SL: {TAKE_PROFIT_PCT:.1f}% / {STOP_LOSS_PCT:.1f}%\n"
        f"📈 Símbolos: {', '.join(SYMBOLS)}\n"
    )
    notifier.broadcast(txt)

def main():
    boot_banner()
    api_key = os.getenv("API_KEY", "")
    def mask(s): return f"{s[:3]}...{s[-3:]}" if s and len(s) > 6 else "***"
    log.info(f"API key: {mask(api_key)}")

    global _running
    while _running:
        try:
            for symbol in SYMBOLS:
                price, rsi_val = fetch_price_and_rsi(symbol)
                if price is None:
                    continue
                for mode, _, _ in MODES:
                    try_close_logic(symbol, mode, price)
                maybe_open_trades(symbol, rsi_val or 50.0, price)
            heartbeat_summary()
            time.sleep(POLL_SEC)
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(2)
    log.info("[EXIT] Loop detenido.")

if __name__ == "__main__":
    main()
