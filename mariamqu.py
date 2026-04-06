import json
import time
import threading
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

import pandas as pd
import requests
import telebot

# =====================================================
# CONFIG
# =====================================================

TELEGRAM_TOKEN = "8199211655:AAEhJXQqLgX_SSxvRU1Ge7yrEFb-nnNB34g"
CHAT_ID = "7877278192"

FASTFOREX_API_KEY = "d93532815e-2d9b94688e-tcxqsn"
FASTFOREX_FETCH_ONE_URL = "https://api.fastforex.io/fetch-one"
FASTFOREX_OHLC_URL = "https://api.fastforex.io/fx/ohlc/time-series"

FOREX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "CHF/JPY", "EUR/CHF", "GBP/CHF", "AUD/CHF",
    "CAD/JPY", "EUR/AUD", "GBP/AUD", "AUD/NZD", "EUR/NZD",
    "GBP/NZD", "NZD/JPY", "CAD/CHF", "AUD/CAD", "GBP/CAD"
]

ALL_SYMBOLS = [f"FX:{p.replace('/', '')}" for p in FOREX_PAIRS]

# Conservative usage for the trial key.
ACTIVE_BATCH_SIZE = 3
POLL_INTERVAL_SECONDS = 5
ROTATE_BATCH_SECONDS = 15 * 60

# 5m candle logic
CANDLE_MINUTES = 5
PRE_ALERT_SECONDS = 25
FINAL_ALERT_SECONDS = 5

# Faster startup settings so it starts signaling sooner after bootstrap
EMA_FAST = 5
EMA_SLOW = 13
RSI_PERIOD = 7
MACD_FAST = 8
MACD_SLOW = 17
MACD_SIGNAL = 5

HISTORY_BARS = 80
MIN_HISTORY_FOR_PRIORITY = 15

BOOTSTRAP_1M_LIMIT = 100

DB_PATH = "signals.db"

# =====================================================
# INIT
# =====================================================

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
requests_session = requests.Session()
requests_session.headers.update({"User-Agent": "Mozilla/5.0"})

# =====================================================
# DATABASE
# =====================================================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL,
        signal TEXT NOT NULL,
        stage TEXT NOT NULL,
        entry REAL,
        ts TEXT NOT NULL
    )
    """
)

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS candles (
        pair TEXT NOT NULL,
        bucket INTEGER NOT NULL,
        o REAL,
        h REAL,
        l REAL,
        c REAL,
        v REAL,
        PRIMARY KEY (pair, bucket)
    )
    """
)

conn.commit()

# =====================================================
# STATE
# =====================================================

@dataclass
class PairState:
    history: Deque[dict] = field(default_factory=lambda: deque(maxlen=HISTORY_BARS))
    current_candle: Optional[dict] = None
    current_bucket: Optional[int] = None

    pre_alerted_bucket: Optional[int] = None
    final_alerted_bucket: Optional[int] = None
    last_confirmed_bucket: Optional[int] = None

    last_signal: str = "-"
    last_update_utc: str = "-"
    last_poll_utc: str = "-"
    last_error: str = "-"
    last_priority_score: float = 0.0
    last_price: Optional[float] = None

    history_seeded: bool = False
    next_seed_retry_epoch: int = 0

states: Dict[str, PairState] = {sym: PairState() for sym in ALL_SYMBOLS}
stop_event = threading.Event()

batch_start_index = 0
batch_lock = threading.Lock()

# =====================================================
# UTILITIES
# =====================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def ts() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")

def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default

def floor_5m_epoch(epoch_s: int) -> int:
    return epoch_s - (epoch_s % (CANDLE_MINUTES * 60))

def candle_bucket_now() -> int:
    return floor_5m_epoch(int(time.time()))

def tg_send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        print(f"[Telegram] send failed: {e}")

def candles_to_df(candles: List[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    for col in ["o", "h", "l", "c", "v"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["o", "h", "l", "c"])

def current_batch_symbols() -> List[str]:
    with batch_lock:
        start = batch_start_index
    end = start + ACTIVE_BATCH_SIZE
    if end <= len(ALL_SYMBOLS):
        return ALL_SYMBOLS[start:end]
    return ALL_SYMBOLS[start:] + ALL_SYMBOLS[: end - len(ALL_SYMBOLS)]

def advance_batch():
    global batch_start_index
    with batch_lock:
        batch_start_index = (batch_start_index + ACTIVE_BATCH_SIZE) % len(ALL_SYMBOLS)

def normalize_symbol(raw_symbol: str) -> Optional[str]:
    if not raw_symbol:
        return None

    if raw_symbol in states:
        return raw_symbol

    if raw_symbol.endswith("_5_0s"):
        raw_symbol = raw_symbol[:-5]

    if raw_symbol.startswith("FX:"):
        if raw_symbol in states:
            return raw_symbol

    if len(raw_symbol) == 6 and raw_symbol.isalpha():
        candidate = f"FX:{raw_symbol}"
        if candidate in states:
            return candidate

    if "/" in raw_symbol:
        candidate = f"FX:{raw_symbol.replace('/', '')}"
        if candidate in states:
            return candidate

    return None

def parse_dtm_to_epoch(dtm_value) -> int:
    if isinstance(dtm_value, (int, float)):
        v = int(dtm_value)
        if v > 10**12:
            return v // 1000
        return v

    s = str(dtm_value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

# =====================================================
# CANDLE NORMALIZATION
# =====================================================

def ensure_list_candles(raw) -> List[dict]:
    if raw is None:
        return []

    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = []
        for k, v in raw.items():
            if isinstance(v, dict):
                cand = dict(v)
                if "t" not in cand:
                    try:
                        cand["t"] = int(k)
                    except Exception:
                        pass
                items.append(cand)
    else:
        return []

    normalized = []
    for item in items:
        if not isinstance(item, dict):
            continue

        bucket = item.get("t") or item.get("time") or item.get("timestamp")
        open_ = item.get("o", item.get("open"))
        high_ = item.get("h", item.get("high"))
        low_ = item.get("l", item.get("low"))
        close_ = item.get("c", item.get("close"))
        volume_ = item.get("v", item.get("volume", 0))

        if bucket is None or open_ is None or high_ is None or low_ is None or close_ is None:
            continue

        try:
            bucket = int(bucket)
        except Exception:
            continue

        normalized.append(
            {
                "t": bucket,
                "o": safe_float(open_),
                "h": safe_float(high_),
                "l": safe_float(low_),
                "c": safe_float(close_),
                "v": safe_float(volume_, 0) or 0,
            }
        )

    normalized.sort(key=lambda x: x["t"])
    return normalized

def aggregate_1m_to_5m(rows: List[dict]) -> List[dict]:
    bars: Dict[int, dict] = {}

    for row in sorted(rows, key=lambda r: parse_dtm_to_epoch(r["dtm"])):
        ts_epoch = parse_dtm_to_epoch(row["dtm"])
        bucket = floor_5m_epoch(ts_epoch)

        o = safe_float(row.get("o"))
        h = safe_float(row.get("h"))
        l = safe_float(row.get("l"))
        c = safe_float(row.get("c"))
        v = safe_float(row.get("v"), 0) or 0

        if None in (o, h, l, c):
            continue

        if bucket not in bars:
            bars[bucket] = {"t": bucket, "o": o, "h": h, "l": l, "c": c, "v": v}
        else:
            bars[bucket]["h"] = max(bars[bucket]["h"], h)
            bars[bucket]["l"] = min(bars[bucket]["l"], l)
            bars[bucket]["c"] = c
            bars[bucket]["v"] += v

    out = sorted(bars.values(), key=lambda x: x["t"])

    # Avoid duplicating the current live candle on startup.
    now_bucket = candle_bucket_now()
    if out and out[-1]["t"] == now_bucket:
        out = out[:-1]

    return out

# =====================================================
# INDICATORS & STRATEGY
# =====================================================

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ema_fast"] = df["c"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["c"].ewm(span=EMA_SLOW, adjust=False).mean()

    delta = df["c"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean().replace(0, pd.NA)
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    ema12 = df["c"].ewm(span=MACD_FAST, adjust=False).mean()
    ema26 = df["c"].ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema12 - ema26
    macd_signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = macd - macd_signal

    return df

def strong_signal(df: pd.DataFrame) -> Optional[str]:
    if len(df) < max(EMA_SLOW, RSI_PERIOD, MACD_SLOW) + 2:
        return None

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    buy_cross = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    buy_macd = last["macd_hist"] > 0 and last["macd_hist"] >= prev["macd_hist"]
    buy_rsi = pd.notna(last["rsi"]) and last["rsi"] < 68

    sell_cross = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]
    sell_macd = last["macd_hist"] < 0 and last["macd_hist"] <= prev["macd_hist"]
    sell_rsi = pd.notna(last["rsi"]) and last["rsi"] > 32

    if buy_cross and buy_macd and buy_rsi:
        return "BUY"
    if sell_cross and sell_macd and sell_rsi:
        return "SELL"
    return None

def preview_signal(df: pd.DataFrame) -> Optional[str]:
    if len(df) < max(EMA_SLOW, RSI_PERIOD, MACD_SLOW) + 1:
        return None

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    buy_bias = (
        last["c"] > last["ema_slow"]
        and last["ema_fast"] >= last["ema_slow"]
        and last["macd_hist"] >= prev["macd_hist"]
        and pd.notna(last["rsi"])
        and last["rsi"] < 72
    )

    sell_bias = (
        last["c"] < last["ema_slow"]
        and last["ema_fast"] <= last["ema_slow"]
        and last["macd_hist"] <= prev["macd_hist"]
        and pd.notna(last["rsi"])
        and last["rsi"] > 28
    )

    if buy_bias:
        return "BUY"
    if sell_bias:
        return "SELL"
    return None

def compute_pair_priority(symbol: str) -> float:
    state = states[symbol]
    candles = list(state.history)
    if state.current_candle:
        candles = candles + [state.current_candle]

    if len(candles) < MIN_HISTORY_FOR_PRIORITY:
        return 10_000.0 - len(candles)

    df = candles_to_df(candles)
    if len(df) < max(EMA_SLOW, RSI_PERIOD, MACD_SLOW) + 2:
        return 8_000.0 - len(df)

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0.0

    ema_gap = abs(float(last["ema_fast"]) - float(last["ema_slow"]))
    if ema_gap <= float(last["c"]) * 0.0003:
        score += 3.0
    elif ema_gap <= float(last["c"]) * 0.0007:
        score += 2.0
    elif ema_gap <= float(last["c"]) * 0.0012:
        score += 1.0

    hist = float(last["macd_hist"])
    prev_hist = float(prev["macd_hist"])
    if abs(hist) < abs(float(last["c"])) * 0.00015:
        score += 3.0
    elif abs(hist) < abs(float(last["c"])) * 0.0003:
        score += 2.0
    elif (hist > prev_hist and prev_hist < 0) or (hist < prev_hist and prev_hist > 0):
        score += 2.0

    rsi = last.get("rsi")
    if pd.notna(rsi):
        rsi = float(rsi)
        if rsi <= 35 or rsi >= 65:
            score += 2.0
        elif rsi <= 42 or rsi >= 58:
            score += 1.0

    last_body = abs(float(last["c"]) - float(last["o"]))
    last_range = max(1e-9, float(last["h"]) - float(last["l"]))
    body_ratio = last_body / last_range

    if body_ratio >= 0.75:
        score += 1.5
    elif body_ratio >= 0.55:
        score += 1.0

    if float(last["c"]) > float(last["ema_slow"]) and float(last["ema_fast"]) > float(last["ema_slow"]):
        score += 0.75
    if float(last["c"]) < float(last["ema_slow"]) and float(last["ema_fast"]) < float(last["ema_slow"]):
        score += 0.75

    score += min(1.0, body_ratio)
    return score

# =====================================================
# STORAGE
# =====================================================

def upsert_candle_db(pair: str, candle: dict):
    cur.execute(
        """
        INSERT INTO candles(pair, bucket, o, h, l, c, v)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pair, bucket) DO UPDATE SET
            o=excluded.o,
            h=excluded.h,
            l=excluded.l,
            c=excluded.c,
            v=excluded.v
        """,
        (pair, candle["t"], candle["o"], candle["h"], candle["l"], candle["c"], candle["v"]),
    )
    conn.commit()

def load_recent_candles(pair: str, limit: int = HISTORY_BARS) -> List[dict]:
    cur.execute(
        "SELECT bucket, o, h, l, c, v FROM candles WHERE pair=? ORDER BY bucket ASC LIMIT ?",
        (pair, limit),
    )
    rows = cur.fetchall()
    return [
        {
            "t": int(r[0]),
            "o": float(r[1]),
            "h": float(r[2]),
            "l": float(r[3]),
            "c": float(r[4]),
            "v": float(r[5] or 0),
        }
        for r in rows
    ]

def log_signal(pair: str, signal: str, stage: str, entry: Optional[float]):
    cur.execute(
        "INSERT INTO signals(pair, signal, stage, entry, ts) VALUES(?,?,?,?,?)",
        (pair, signal, stage, entry, ts()),
    )
    conn.commit()

# =====================================================
# FASTFOREX FETCH
# =====================================================

def fetch_fastforex_price(pair: str) -> float:
    clean = pair.replace("FX:", "").replace("/", "")
    if len(clean) != 6:
        raise ValueError(f"Invalid pair format: {pair}")

    base = clean[:3]
    quote = clean[3:]

    params = {
        "from": base,
        "to": quote,
        "api_key": FASTFOREX_API_KEY,
    }

    r = requests_session.get(FASTFOREX_FETCH_ONE_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response: {data}")

    result = data.get("result", data)

    if isinstance(result, dict):
        if quote in result:
            return float(result[quote])
        if "price" in result:
            return float(result["price"])
        if "rate" in result:
            return float(result["rate"])
        if len(result) == 1:
            return float(next(iter(result.values())))

    if isinstance(result, (int, float, str)):
        return float(result)

    raise RuntimeError(f"Unexpected result: {data}")

def fetch_fastforex_ohlc_history(pair: str, limit: int = BOOTSTRAP_1M_LIMIT) -> List[dict]:
    clean = pair.replace("FX:", "").replace("/", "")
    params = {
        "api_key": FASTFOREX_API_KEY,
        "pair": clean,
        "interval": "PT1M",
        "limit": min(max(1, limit), 100),
        "dtmfmt": "ISO",
    }

    r = requests_session.get(FASTFOREX_OHLC_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected history response: {data}")

    results = data.get("results", [])
    if not isinstance(results, list):
        raise RuntimeError(str(data))

    return results

def seed_symbol_history(symbol: str) -> bool:
    state = states[symbol]
    now_epoch = int(time.time())
    if state.history_seeded:
        return True
    if now_epoch < state.next_seed_retry_epoch:
        return False

    try:
        minute_rows = fetch_fastforex_ohlc_history(symbol, BOOTSTRAP_1M_LIMIT)
        if not minute_rows:
            raise RuntimeError("empty bootstrap response")

        aggregated = aggregate_1m_to_5m(minute_rows)
        if not aggregated:
            raise RuntimeError("could not aggregate bootstrap candles")

        for c in aggregated:
            upsert_candle_db(symbol, c)
            state.history.append(c)

        state.history_seeded = True
        state.last_error = "-"
        state.last_update_utc = ts()
        print(f"[BOOTSTRAP] {symbol}: loaded {len(aggregated)} 5m candles")
        return True

    except Exception as e:
        state.last_error = f"{ts()} | bootstrap error: {e}"
        state.next_seed_retry_epoch = now_epoch + 300
        print(f"[BOOTSTRAP] {symbol} error: {e}")
        return False

# =====================================================
# BATCH / ALERT LOGIC
# =====================================================

def maybe_alerts(symbol: str):
    state = states[symbol]
    candles = list(state.history)
    if state.current_candle:
        candles = candles + [state.current_candle]

    if len(candles) < max(EMA_SLOW, RSI_PERIOD, MACD_SLOW) + 2:
        return

    df = candles_to_df(candles)
    sig_pre = preview_signal(df)
    if not sig_pre:
        return

    bucket = state.current_bucket or candle_bucket_now()
    remaining = (bucket + (CANDLE_MINUTES * 60)) - int(time.time())

    if remaining <= PRE_ALERT_SECONDS and state.pre_alerted_bucket != bucket:
        state.pre_alerted_bucket = bucket
        state.last_signal = f"PRE {sig_pre}"
        state.last_update_utc = ts()
        state.last_priority_score = compute_pair_priority(symbol)
        log_signal(symbol, sig_pre, "PRE", float(df.iloc[-1]["c"]))
        tg_send(
            f"<b>⚡ Possible signal</b>\n"
            f"Pair: <b>{symbol}</b>\n"
            f"Direction: <b>{sig_pre}</b>\n"
            f"Remaining: <b>{remaining}s</b>\n"
            f"Frame: <code>5m</code>\n"
            f"Priority score: <code>{state.last_priority_score:.2f}</code>"
        )

    sig_final = strong_signal(df) or sig_pre
    if remaining <= FINAL_ALERT_SECONDS and state.final_alerted_bucket != bucket:
        state.final_alerted_bucket = bucket
        state.last_confirmed_bucket = bucket
        state.last_signal = f"FINAL {sig_final}"
        state.last_update_utc = ts()
        state.last_priority_score = compute_pair_priority(symbol)
        log_signal(symbol, sig_final, "FINAL", float(df.iloc[-1]["c"]))
        tg_send(
            f"<b>✅ Signal confirmation</b>\n"
            f"Pair: <b>{symbol}</b>\n"
            f"Direction: <b>{sig_final}</b>\n"
            f"Remaining: <b>{remaining}s</b>\n"
            f"Frame: <code>5m</code>\n"
            f"Priority score: <code>{state.last_priority_score:.2f}</code>"
        )

def finalize_previous_candle(symbol: str):
    state = states[symbol]
    if state.current_candle is None:
        return

    closed = state.current_candle
    state.history.append(closed)
    upsert_candle_db(symbol, closed)

    state.last_update_utc = ts()

    if len(state.history) >= max(EMA_SLOW, RSI_PERIOD, MACD_SLOW) + 2:
        df = candles_to_df(list(state.history))
        sig = strong_signal(df) or preview_signal(df)
        if sig and state.last_confirmed_bucket != closed["t"]:
            state.last_confirmed_bucket = closed["t"]
            state.last_signal = sig
            entry = float(df.iloc[-1]["c"])
            log_signal(symbol, sig, "CONFIRMED", entry)
            tg_send(
                f"<b>✅ Confirmed 5m signal</b>\n"
                f"Pair: <b>{symbol}</b>\n"
                f"Direction: <b>{sig}</b>\n"
                f"Entry: <code>{entry}</code>\n"
                f"Time: <code>{ts()}</code>"
            )

def handle_price_tick(symbol: str, price: float):
    state = states[symbol]
    now_bucket = candle_bucket_now()

    if state.current_candle is None:
        state.current_bucket = now_bucket
        state.current_candle = {
            "t": now_bucket,
            "o": price,
            "h": price,
            "l": price,
            "c": price,
            "v": 0,
        }
        state.last_price = price
        state.last_poll_utc = ts()
        state.last_priority_score = compute_pair_priority(symbol)
        maybe_alerts(symbol)
        return

    if now_bucket != state.current_bucket:
        finalize_previous_candle(symbol)
        state.pre_alerted_bucket = None
        state.final_alerted_bucket = None

        state.current_bucket = now_bucket
        state.current_candle = {
            "t": now_bucket,
            "o": price,
            "h": price,
            "l": price,
            "c": price,
            "v": 0,
        }
    else:
        c = state.current_candle
        c["h"] = max(float(c["h"]), price)
        c["l"] = min(float(c["l"]), price)
        c["c"] = price
        c["v"] = float(c.get("v", 0) or 0)

    state.last_price = price
    state.last_poll_utc = ts()
    state.last_priority_score = compute_pair_priority(symbol)
    maybe_alerts(symbol)

# =====================================================
# POLLING & ROTATION
# =====================================================

def poll_prices_loop():
    while not stop_event.is_set():
        loop_start = time.time()
        batch = current_batch_symbols()
        print(f"[POLL] active batch: {batch}")

        for symbol in batch:
            if stop_event.is_set():
                break

            try:
                if not states[symbol].history_seeded:
                    seed_symbol_history(symbol)

                price = fetch_fastforex_price(symbol)
                handle_price_tick(symbol, price)
                print(f"[POLL] {symbol} -> {price}")

            except Exception as e:
                states[symbol].last_error = f"{ts()} | {e}"
                print(f"[{symbol}] fetch error: {e}")

            time.sleep(0.05)

        elapsed = time.time() - loop_start
        sleep_for = max(0.1, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_for)

def rotation_loop():
    while not stop_event.is_set():
        time.sleep(ROTATE_BATCH_SECONDS)
        if stop_event.is_set():
            break
        advance_batch()
        print(f"[ROTATE] next batch -> {current_batch_symbols()}")

def bootstrap_from_db():
    for symbol in ALL_SYMBOLS:
        cached = load_recent_candles(symbol, HISTORY_BARS)
        if cached:
            states[symbol].history.extend(cached)

# =====================================================
# TELEGRAM COMMANDS
# =====================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    tg_send(
        f"🚀 <b>Mariam Bot Started</b>\n"
        f"Pairs: <b>{len(ALL_SYMBOLS)}</b>\n"
        f"Active batch: <b>{len(current_batch_symbols())}</b>\n"
        f"Mode: <code>FastForex polling + local 5m candles</code>\n"
        f"Frame: <code>5m</code>"
    )

@bot.message_handler(commands=["status"])
def cmd_status(message):
    current_active = current_batch_symbols()
    ordered = sorted(current_active, key=lambda s: states[s].last_priority_score, reverse=True)
    lines = [
        f"<b>Status</b>",
        f"Polling: <b>running</b>",
        f"Active batch size: <b>{len(current_active)}</b>",
        f"Active symbols: <code>{', '.join(current_active)}</code>",
        f"Time: <code>{ts()}</code>",
        "",
    ]
    for sym in ordered:
        s = states[sym]
        lines.append(
            f"{sym}: price=<code>{s.last_price}</code> | "
            f"score=<code>{s.last_priority_score:.2f}</code> | "
            f"signal=<code>{s.last_signal}</code> | "
            f"poll=<code>{s.last_poll_utc}</code> | "
            f"err=<code>{s.last_error}</code>"
        )
    bot.send_message(message.chat.id, "\n".join(lines))

@bot.message_handler(commands=["pairs"])
def cmd_pairs(message):
    current_active = current_batch_symbols()
    bot.send_message(
        message.chat.id,
        "<b>Current batch</b>\n<code>" + ", ".join(current_active) + "</code>"
    )

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    cur.execute(
        "SELECT pair, stage, COUNT(*) FROM signals GROUP BY pair, stage ORDER BY pair, stage"
    )
    rows = cur.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "No signals recorded yet.")
        return

    out = ["<b>Stats</b>"]
    for pair, stage, count in rows:
        out.append(f"{pair} | {stage}: <b>{count}</b>")
    bot.send_message(message.chat.id, "\n".join(out))

@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    stop_event.set()
    tg_send("🛑 <b>Bot stopping</b>")

# =====================================================
# MAIN
# =====================================================

def main():
    bootstrap_from_db()

    # Seed the first active batch right away for faster signals.
    for symbol in current_batch_symbols():
        if not states[symbol].history_seeded:
            seed_symbol_history(symbol)

    threading.Thread(target=poll_prices_loop, daemon=True).start()
    threading.Thread(target=rotation_loop, daemon=True).start()

    tg_send("👋 Mariam FastForex bot is starting...")

    while not stop_event.is_set():
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"[Telegram] polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
