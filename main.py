# -*- coding: utf-8 -*-
"""
Bot scalper RSI 1m con 3 modos (Agresivo/Moderado/Conservador)
- Replica estilo Saffex (caps por modo con topes y lotes)
- Mejoras: TP parcial, Break-even, Trailing, Timeout, Cooldowns
- CoinEx: evita setLeverage/margin en spot; solo en mercados swap
- Demo (LIVE=0) con ejecución local; Live (LIVE=1) con ccxt
"""

import os
import time
import math
import json
import signal
import logging
from datetime import datetime, timezone, timedelta

import requests
import ccxt

try:
    # En algunos contenedores tzdata no está, por eso permitimos fallback UTC
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
# Configuración por entorno
# ---------------------------
EXCHANGE_ID = getenv_str("EXCHANGE", "coinex").lower()
LIVE = getenv_int("LIVE", 0)  # 0 = demo (local), 1 = live (exchange)
TIMEFRAME = getenv_str("TIMEFRAME", "1m")
SYMBOLS = [s.strip() for s in getenv_str("SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]
FEE_RATE = getenv_float("FEE_RATE", 0.002)  # 0.2% por lado

RSI_PERIOD = getenv_int("RSI_PERIOD", 14)
RSI_BUY_TH = getenv_float("RSI_BUY_THRESHOLD", 30.0)
RSI_SELL_TH = getenv_float("RSI_SELL_THRESHOLD", 70.0)
RSI_HYST = getenv_float("RSI_HYSTERESIS", 3.0)

# Estilo Saffex base
TP_PCT = getenv_float("TAKE_PROFIT_PCT", 1.0)      # %
SL_PCT = getenv_float("STOP_LOSS_PCT", 1.2)        # %

# Mejoras
TP_PARTIAL_PCT = getenv_float("TP_PARTIAL_PCT", 40.0)   # % de la posición
ENABLE_BE = getenv_int("ENABLE_BREAKEVEN", 1) == 1
BE_TRIGGER_PCT = getenv_float("BE_TRIGGER_PCT", 0.60)   # %
BE_OFFSET_PCT = getenv_float("BE_OFFSET_PCT", 0.05)     # %

ENABLE_TRAIL = getenv_int("ENABLE_TRAIL", 1) == 1
TRAIL_TRIGGER_PCT = getenv_float("TRAIL_TRIGGER_PCT", 1.00)  # %
TRAIL_STEP_PCT = getenv_float("TRAIL_STEP_PCT", 0.25)         # %

SIGNAL_COOLDOWN = getenv_int("SIGNAL_COOLDOWN", 300)    # sec
TIMEOUT_MIN = getenv_int("TIMEOUT_MIN", 25)             # min
LOSS_COOLDOWN_SEC = getenv_int("LOSS_COOLDOWN_SEC", 900)

# Caps y lotes (estilo Saffex) + tope automático por modo
CAP_A = min(getenv_float("CAPITAL_AGRESIVO", 3.0), 50.0)
LOTS_A = max(1, getenv_int("LOT_SIZE_AGRESIVO", 3))
CAP_M = min(getenv_float("CAPITAL_MODERADO", 5.0), 500.0)
LOTS_M = max(1, getenv_int("LOT_SIZE_MODERADO", 4))
CAP_C = min(getenv_float("CAPITAL_CONSERVADOR", 7.0), 10000.0)
LOTS_C = max(1, getenv_int("LOT_SIZE_CONSERVADOR", 5))

# Telegram
TELEGRAM_TOKEN = getenv_str("TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED_IDS = [i.strip() for i in getenv_str("TELEGRAM_ALLOWED_IDS", "").split(",") if i.strip()]

# Loop timings
POLL_SEC = getenv_int("POLL_SEC", 5)

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

# ---------------------------
# Telegram notifier
# ---------------------------
notifier = TelegramNotifier(
    token=TELEGRAM_TOKEN,
    allowed_chat_ids=TELEGRAM_ALLOWED_IDS
)

def fmt_price(x, tick_size=None):
    if x is None:
        return "n/a"
    if tick_size and tick_size >= 1:
        return f"{x:,.0f}"
    # fallback genérico
    return f"{x:,.4f}"

# ---------------------------
# Exchange / Mercado
# ---------------------------
def is_swap_symbol(market) -> bool:
    # En ccxt coinex, los mercados swap suelen venir con type='swap'
    if not market:
        return False
    t = market.get("type")
    return t == "swap"

def build_exchange():
    kwargs = {
        "enableRateLimit": True,
        "options": {}
    }
    # Si quieres operar swap por defecto, puedes setear 'defaultType': 'swap'
    # pero como tenemos símbolos spot y/o demo, lo dejamos neutro.
    ex_class = getattr(ccxt, EXCHANGE_ID)
    exchange = ex_class(kwargs)
    # Si tienes API (solo para LIVE=1)
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

# ---------------------------
# Estrategia: RSI simple
# ---------------------------
def rsi(values, period=14):
    """
    RSI simple (Wilder) sobre lista de precios de cierre.
    """
    if len(values) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # Suavizado Wilder
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if period >= len(values) - 1:
        return None
    for i in range(period, len(values) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))

def fetch_price_and_rsi(symbol: str):
    """
    Obtiene último precio (bid/ask/last) y RSI del timeframe configurado.
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=max(200, RSI_PERIOD + 50))
        closes = [c[4] for c in ohlcv]
        last_close = closes[-1]
        the_rsi = rsi(closes, RSI_PERIOD)
        return float(last_close), float(the_rsi) if the_rsi is not None else None
    except Exception as e:
        log.warning(f"fetch_ohlcv fallo {symbol}: {e}")
        # Fallback a ticker
        try:
            t = exchange.fetch_ticker(symbol)
            last = t.get("last") or t.get("close")
            return float(last), None
        except Exception as e2:
            log.error(f"fetch_ticker fallo {symbol}: {e2}")
            return None, None

# ---------------------------
# Estado y posiciones (demo/local)
# ---------------------------
class Position:
    def __init__(self, symbol, mode, side, qty, entry, tp, sl, opened_ts, notional):
        self.symbol = symbol
        self.mode = mode  # 'agresivo'|'moderado'|'conservador'
        self.side = side  # 'long'|'short'
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
        self.partial_size = 0.0
        self.be_armed = False
        self.trail_armed = False
        self.trail_stop = None

    def __repr__(self):
        return f"<Pos {self.symbol} {self.mode} {self.side} qty={self.qty} @ {self.entry}>"

positions = {}  # key: (symbol, mode) -> Position or None
last_signal_ts = {}  # key: (symbol, mode, side) to enforce cooldown
last_loss_ts = 0

pnl_counters = {
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "gross": 0.0,
    "fees": 0.0,
    "pnl": 0.0
}
last_summary_ts = time.time()

MODES = [
    ("agresivo", CAP_A, LOTS_A),
    ("moderado", CAP_M, LOTS_M),
    ("conservador", CAP_C, LOTS_C),
]

def mode_multiplier(mode: str) -> int:
    if mode == "agresivo":
        return 3
    if mode == "moderado":
        return 4
    return 5  # conservador

def per_lot_cap(cap: float, lots: int) -> float:
    lots = max(1, lots)
    return cap / float(lots)

def compute_order_qty(symbol: str, dollar_size: float, price: float):
    """
    Cantidad (base) ≈ notional / price (redondeo simple a 6 decimales)
    """
    if price <= 0:
        return 0.0
    qty = dollar_size / price
    return float(round(qty, 6))

def compute_tp_sl(side: str, entry: float, tp_pct: float, sl_pct: float):
    if side == "long":
        tp = entry * (1.0 + tp_pct / 100.0)
        sl = entry * (1.0 - sl_pct / 100.0)
    else:
        tp = entry * (1.0 - tp_pct / 100.0)
        sl = entry * (1.0 + sl_pct / 100.0)
    return tp, sl

def price_hit_take(side: str, price: float, tp: float) -> bool:
    if side == "long":
        return price >= tp
    return price <= tp

def price_hit_stop(side: str, price: float, sl: float) -> bool:
    if side == "long":
        return price <= sl
    return price >= sl

def unrealized_pct(side: str, price: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    change = (price - entry) / entry * 100.0
    return change if side == "long" else (-change)

def maybe_arm_be_and_trail(pos: Position, price: float):
    if ENABLE_BE and not pos.be_armed:
        if unrealized_pct(pos.side, price, pos.entry) >= BE_TRIGGER_PCT:
            # mover SL a BE + offset
            if pos.side == "long":
                pos.sl = pos.entry * (1.0 + BE_OFFSET_PCT / 100.0)
            else:
                pos.sl = pos.entry * (1.0 - BE_OFFSET_PCT / 100.0)
            pos.be_armed = True
    if ENABLE_TRAIL and not pos.trail_armed:
        if unrealized_pct(pos.side, price, pos.entry) >= TRAIL_TRIGGER_PCT:
            # arrancamos trailing: fijamos un primer stop dinámico
            if pos.side == "long":
                pos.trail_stop = price * (1.0 - TRAIL_STEP_PCT / 100.0)
            else:
                pos.trail_stop = price * (1.0 + TRAIL_STEP_PCT / 100.0)
            pos.trail_armed = True
    if ENABLE_TRAIL and pos.trail_armed and pos.trail_stop is not None:
        # actualizar trailing si se aleja a favor
        if pos.side == "long":
            new_stop = price * (1.0 - TRAIL_STEP_PCT / 100.0)
            if new_stop > pos.trail_stop:
                pos.trail_stop = new_stop
        else:
            new_stop = price * (1.0 + TRAIL_STEP_PCT / 100.0)
            if new_stop < pos.trail_stop:
                pos.trail_stop = new_stop

def hit_trailing_exit(pos: Position, price: float) -> bool:
    if not (ENABLE_TRAIL and pos.trail_armed and pos.trail_stop):
        return False
    if pos.side == "long":
        return price <= pos.trail_stop
    else:
        return price >= pos.trail_stop

def place_open_order_live(symbol, side, qty):
    """
    Para LIVE=1: ejecuta market order (simple). En spot CoinEx no hay setLeverage/margin.
    En swap, solo se llama leverage/margin si el mercado es swap.
    """
    mkt = markets.get(symbol)
    try:
        if mkt and is_swap_symbol(mkt):
            # set leverage/margin SOLO si el mercado es swap
            try:
                if hasattr(exchange, "set_margin_mode") and exchange.has.get("setMarginMode"):
                    exchange.set_margin_mode("cross", symbol)
            except Exception:
                pass
            try:
                if hasattr(exchange, "set_leverage") and exchange.has.get("setLeverage"):
                    exchange.set_leverage(3, symbol)  # valor fijo o configurable
            except Exception:
                pass
        params = {}
        side_ccxt = "buy" if side == "long" else "sell"
        order = exchange.create_order(symbol, type="market", side=side_ccxt, amount=qty, params=params)
        return order
    except Exception as e:
        log.error(f"create_order fallo {symbol} {side} qty={qty}: {e}")
        return None

def close_order_live(symbol, side, qty):
    """
    Cierre en LIVE: usa market en sentido inverso si es spot;
    si es swap con position mode, ideal sería reduceOnly, aquí lo simplificamos.
    """
    try:
        side_ccxt = "sell" if side == "long" else "buy"
        order = exchange.create_order(symbol, type="market", side=side_ccxt, amount=qty, params={})
        return order
    except Exception as e:
        log.error(f"close_order fallo {symbol} {side} qty={qty}: {e}")
        return None

def fee_cost(notional: float) -> float:
    # ida y vuelta aproximada
    return notional * (FEE_RATE * 2.0)

def record_close(pos: Position, close_price: float, reason: str):
    """
    Calcula PnL neto aproximado con fees ida y vuelta.
    """
    pos.closed = True
    pos.closed_ts = time.time()
    pos.reason = reason

    # Notional "efectivo": si hubo TP parcial, consideramos toda la notional
    # para fees ida/vuelta (aprox), y PnL por porcentaje sobre notional.
    # Es simple y consistente con las notificaciones anteriores.
    side = pos.side
    entry = pos.entry
    notional = pos.notional
    # Ganancia/perdida bruta aproximada (en función del porcentaje alcanzado)
    # Si reason es TP parcial/TP/trail, estimamos por distancia entrada->close
    ret_pct = (close_price - entry) / entry * 100.0
    if side == "short":
        ret_pct = -ret_pct
    gross = notional * (ret_pct / 100.0)
    fees = fee_cost(notional)
    pnl = gross - fees

    # Estadísticos
    pnl_counters["trades"] += 1
    pnl_counters["gross"] += gross
    pnl_counters["fees"] += fees
    pnl_counters["pnl"] += pnl
    if pnl >= 0:
        pnl_counters["wins"] += 1
    else:
        pnl_counters["losses"] += 1

    # Notificación de cierre
    notifier.send_close(
        symbol=pos.symbol,
        mode=pos.mode.title(),
        side=side.upper(),
        reason=reason,
        gross=gross,
        fees=fees,
        pnl=pnl,
        duration_sec=int(pos.closed_ts - pos.opened_ts)
    )

    return pnl

def open_position(symbol: str, mode: str, side: str, lot_usd: float, price: float):
    qty = compute_order_qty(symbol, lot_usd, price)
    if qty <= 0:
        return None

    tp, sl = compute_tp_sl(side, price, TP_PCT, SL_PCT)
    notional = lot_usd  # aproximamos notional = tamaño en $ por lote
    pos = Position(
        symbol=symbol, mode=mode, side=side, qty=qty,
        entry=price, tp=tp, sl=sl, opened_ts=time.time(), notional=notional
    )
    positions[(symbol, mode)] = pos

    # LIVE: intentar abrir en exchange
    if LIVE == 1:
        place_open_order_live(symbol, side, qty)

    # Notificación apertura
    notifier.send_open(
        symbol=symbol,
        mode=mode.title(),
        side=side.upper(),
        lots=mode_multiplier(mode),
        entry=price,
        sl=sl,
        tp=tp,
        rsi=None,  # se muestra abajo por debug si quieres
        timeframe=TIMEFRAME,
        size_usd=lot_usd,
        qty=qty
    )
    return pos

def try_close_logic(symbol: str, mode: str, price: float):
    pos = positions.get((symbol, mode))
    if not pos or pos.closed:
        return

    elapsed = time.time() - pos.opened_ts

    # Mejoras: BE/Trailing
    maybe_arm_be_and_trail(pos, price)

    # 1) Trailing exit
    if hit_trailing_exit(pos, price):
        if LIVE == 1:
            close_order_live(pos.symbol, pos.side, pos.qty)
        record_close(pos, price, "TRAIL(Local)")
        return

    # 2) TP parcial: una sola vez
    if not pos.partial_done and price_hit_take(pos.side, price, pos.tp):
        # Cerrar porcentaje TP_PARTIAL_PCT
        part_pct = max(0.0, min(TP_PARTIAL_PCT, 100.0)) / 100.0
        if part_pct > 0.0:
            close_qty = pos.qty * part_pct
            if LIVE == 1:
                close_order_live(pos.symbol, pos.side, close_qty)
            pos.qty -= close_qty
            pos.partial_done = True
            pos.partial_size = close_qty
            # mover SL a BE si no estaba
            if ENABLE_BE and not pos.be_armed:
                if pos.side == "long":
                    pos.sl = pos.entry * (1.0 + BE_OFFSET_PCT / 100.0)
                else:
                    pos.sl = pos.entry * (1.0 - BE_OFFSET_PCT / 100.0)
                pos.be_armed = True
            # No cerramos del todo, dejamos correr el resto con trailing/BE
            # Anunciar TP parcial como "TP(Local)" (parcial) para consistencia
            notifier.send_partial_tp(
                symbol=pos.symbol,
                mode=pos.mode.title(),
                side=pos.side.upper(),
                partial_pct=TP_PARTIAL_PCT,
                price=price
            )

    # 3) Stop Loss / BE SL
    if price_hit_stop(pos.side, price, pos.sl):
        if LIVE == 1:
            close_order_live(pos.symbol, pos.side, pos.qty)
        record_close(pos, price, "SL(Local)")
        return

    # 4) TIMEOUT
    if elapsed >= TIMEOUT_MIN * 60:
        if LIVE == 1:
            close_order_live(pos.symbol, pos.side, pos.qty)
        record_close(pos, price, "TIMEOUT(Local)")
        return

def signal_allowed(symbol: str, mode: str, side: str) -> bool:
    global last_loss_ts
    now_ts = time.time()
    # Cooldown por lado y modo
    key = (symbol, mode, side)
    last_ts = last_signal_ts.get(key, 0)
    if now_ts - last_ts < SIGNAL_COOLDOWN:
        return False
    # Cooldown por pérdida global
    if last_loss_ts and (now_ts - last_loss_ts < LOSS_COOLDOWN_SEC):
        return False
    return True

def mark_signal(symbol: str, mode: str, side: str):
    last_signal_ts[(symbol, mode, side)] = time.time()

def mark_loss_if_needed(pnl: float):
    global last_loss_ts
    if pnl < 0:
        last_loss_ts = time.time()

def maybe_open_trades(symbol: str, rsi_value: float, price: float):
    # BUY si RSI < BUYT - hyst; SELL si RSI > SELLT + hyst
    long_sig  = rsi_value is not None and (rsi_value < max(0.0, RSI_BUY_TH - RSI_HYST))
    short_sig = rsi_value is not None and (rsi_value > min(100.0, RSI_SELL_TH + RSI_HYST))

    for mode, cap, lots in MODES:
        if cap <= 0:
            continue
        key = (symbol, mode)
        pos = positions.get(key)
        if pos and not pos.closed:
            continue  # ya en mercado

        lot_usd = per_lot_cap(cap, lots)
        if lot_usd <= 0:
            continue

        if long_sig and signal_allowed(symbol, mode, "long"):
            open_position(symbol, mode, "long", lot_usd, price)
            mark_signal(symbol, mode, "long")

        elif short_sig and signal_allowed(symbol, mode, "short"):
            open_position(symbol, mode, "short", lot_usd, price)
            mark_signal(symbol, mode, "short")

def heartbeat_summary():
    global last_summary_ts
    now_ts = time.time()
    if now_ts - last_summary_ts >= 3600:  # cada 1h
        notifier.send_totals(
            trades=pnl_counters["trades"],
            wins=pnl_counters["wins"],
            losses=pnl_counters["losses"],
            pnl=pnl_counters["pnl"],
            fees=pnl_counters["fees"],
            gross=pnl_counters["gross"]
        )
        last_summary_ts = now_ts

# ---------------------------
# Señales del sistema
# ---------------------------
_running = True
def handle_sigterm(signum, frame):
    global _running
    _running = False
    log.info("[STOP] Señal de apagado recibida; cerrando loop…")

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

# ---------------------------
# Arranque
# ---------------------------
def boot_banner():
    caps = f"A {CAP_A}/x{mode_multiplier('agresivo')} · M {CAP_M}/x{mode_multiplier('moderado')} · C {CAP_C}/x{mode_multiplier('conservador')}"
    mode_txt = "LIVE" if LIVE == 1 else "PAPER 🧪"
    txt = (
        f"🚀 INICIO DEL BOT\n\n"
        f"🧩 Exchange: {EXCHANGE_ID} | Modo: {mode_txt}\n"
        f"⏰ Timeframe: {TIMEFRAME}\n"
        f"🧪 Estrategia: RSI({RSI_PERIOD}) — Buy < {RSI_BUY_TH} / Sell > {RSI_SELL_TH}\n"
        f"🎯 TP/SL (base): {TP_PCT:.2f}% / {SL_PCT:.2f}%\n"
        f"🔧 BE: {('On' if ENABLE_BE else 'Off')} @ {BE_TRIGGER_PCT:.2f}% (+{BE_OFFSET_PCT:.2f}%) · "
        f"Trail: {('On' if ENABLE_TRAIL else 'Off')} @ {TRAIL_TRIGGER_PCT:.2f}% (step {TRAIL_STEP_PCT:.2f}%)\n"
        f"⛓️ Cooldown: {SIGNAL_COOLDOWN}s | ⏱️ Timeout: {TIMEOUT_MIN}m | 🧊 LossCD: {LOSS_COOLDOWN_SEC}s\n"
        f"📦 Símbolos: {', '.join(SYMBOLS)}\n"
        f"💰 Caps: {caps}\n"
    )
    notifier.broadcast(txt)

def main():
    boot_banner()

    # Mensaje al log con llaves deforma resumida (si existen)
    api_key = os.getenv("API_KEY", "")
    api_secret = os.getenv("API_SECRET", "")
    def mask(s):
        if not s:
            return ""
        if len(s) < 7:
            return "***"
        return f"{s[:3]}...{s[-3:]}"
    log.info(f"Keys api={mask(api_key)} secret={mask(api_secret)}")

    # Loop principal
    global _running
    while _running:
        try:
            for symbol in SYMBOLS:
                price, rsi_val = fetch_price_and_rsi(symbol)
                if price is None:
                    continue

                log.debug(f"{symbol} | Precio: {price:.2f} | RSI: {rsi_val:.2f}" if rsi_val is not None else f"{symbol} | Precio: {price:.2f} | RSI: n/a")

                # Cierres/gestión
                for mode, _, _ in MODES:
                    try_close_logic(symbol, mode, price)

                # Aberturas
                maybe_open_trades(symbol, rsi_val if rsi_val is not None else 50.0, price)

            heartbeat_summary()
            time.sleep(POLL_SEC)

        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(2)

    log.info("[EXIT] Loop detenido.")

if __name__ == "__main__":
    main()
