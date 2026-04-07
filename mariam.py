import json

import time
import threading
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple
import pandas as pd
import requests
import telebot

TELEGRAM_TOKEN = "8199211655:AAEegKyOHtx5ZgWtrKupgLAKFxKaJAGMWvc"
CHAT_ID = "7877278192"
FASTFOREX_API_KEY = "d93532815e-2d9b94688e-tcxqsn"

FASTFOREX_OHLC_URL = "https://api.fastforex.io/fx/ohlc/time-series"

FOREX_PAIRS = [
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD",
    "NZD/USD", "USD/CAD", "EUR/GBP", "EUR/JPY", "GBP/JPY",
    "AUD/JPY", "CHF/JPY", "EUR/CHF", "GBP/CHF", "AUD/CHF",
    "CAD/JPY", "EUR/AUD", "GBP/AUD", "AUD/NZD", "EUR/NZD",
    "GBP/NZD", "NZD/JPY", "CAD/CHF", "AUD/CAD", "GBP/CAD"
]

ALL_SYMBOLS = [f"FX:{p.replace('/', '')}" for p in FOREX_PAIRS]

TIMEFRAMES = {5, 15}
DEFAULT_TIMEFRAME_MINUTES = 5

ACTIVE_BATCH_SIZE = 3
POLL_INTERVAL_SECONDS = 60
ROTATE_BATCH_SECONDS = 15 * 60
HISTORY_BARS = 120
MINUTE_PAGE_LIMIT = 100
DB_PATH = "signals.db"

MIN_REQUIRED_AGREEMENT = 2
MAX_REQUIRED_AGREEMENT = 3
MIN_STRATEGY_WEIGHT = 0.55
MAX_STRATEGY_WEIGHT = 1.50
WEIGHT_DECAY = 0.90
ROLLING_ACCURACY_WINDOW = 30

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
requests_session = requests.Session()
requests_session.headers.update({"User-Agent": "Mozilla/5.0"})

runtime_lock = threading.RLock()
stop_event = threading.Event()

batch_start_index = 0
current_timeframe_minutes = DEFAULT_TIMEFRAME_MINUTES

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS candles (
        pair TEXT NOT NULL,
        timeframe INTEGER NOT NULL,
        bucket INTEGER NOT NULL,
        o REAL,
        h REAL,
        l REAL,
        c REAL,
        v REAL,
        PRIMARY KEY (pair, timeframe, bucket)
    )
    """
)

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair TEXT NOT NULL,
        timeframe INTEGER NOT NULL,
        direction TEXT NOT NULL,
        entry REAL NOT NULL,
        signal_bucket INTEGER NOT NULL,
        expiry_bucket INTEGER NOT NULL,
        strategy_votes TEXT NOT NULL,
        strength REAL NOT NULL,
        status TEXT NOT NULL,
        result TEXT,
        opened_ts TEXT NOT NULL,
        closed_ts TEXT
    )
    """
)

cur.execute(
    """
    CREATE TABLE IF NOT EXISTS strategy_stats (
        timeframe INTEGER NOT NULL,
        strategy TEXT NOT NULL,
        wins INTEGER NOT NULL DEFAULT 0,
        losses INTEGER NOT NULL DEFAULT 0,
        ties INTEGER NOT NULL DEFAULT 0,
        weight REAL NOT NULL DEFAULT 1.0,
        updated_ts TEXT NOT NULL,
        PRIMARY KEY (timeframe, strategy)
    )
    """
)
conn.commit()


@dataclass
class PairState:
    history: Deque[dict] = field(default_factory=lambda: deque(maxlen=HISTORY_BARS))
    history_seeded: bool = False
    last_closed_bucket: Optional[int] = None
    next_seed_retry_epoch: int = 0
    last_price: Optional[float] = None
    last_poll_utc: str = "-"
    last_update_utc: str = "-"
    last_error: str = "-"
    last_signal: str = "-"
    last_strategy_votes: str = "-"
    last_strength: float = 0.0
    last_outcome: str = "-"


states: Dict[str, PairState] = {sym: PairState() for sym in ALL_SYMBOLS}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts() -> str:
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def get_tf() -> int:
    with runtime_lock:
        return int(current_timeframe_minutes)


def set_tf(new_tf: int):
    global current_timeframe_minutes
    if new_tf not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {new_tf}")
    with runtime_lock:
        current_timeframe_minutes = int(new_tf)
    reset_runtime_state()


def floor_bucket(epoch_s: int, tf_min: int) -> int:
    tf_seconds = tf_min * 60
    return epoch_s - (epoch_s % tf_seconds)


def current_bucket(tf_min: Optional[int] = None) -> int:
    return floor_bucket(int(time.time()), tf_min or get_tf())


def tf_seconds() -> int:
    return get_tf() * 60


def tf_seconds_from_value(tf: int) -> int:
    return int(tf) * 60


def tg_send(text: str):
    try:
        bot.send_message(CHAT_ID, text)
    except Exception as e:
        print(f"[Telegram] send failed: {e}")


def parse_dtm_to_epoch(dtm_value) -> int:
    if isinstance(dtm_value, (int, float)):
        v = int(dtm_value)
        return v // 1000 if v > 10**12 else v
    s = str(dtm_value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def candles_to_df(candles: List[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    for col in ["o", "h", "l", "c", "v"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["o", "h", "l", "c"])


def current_batch_symbols() -> List[str]:
    with runtime_lock:
        start = batch_start_index
    end = start + ACTIVE_BATCH_SIZE
    if end <= len(ALL_SYMBOLS):
        return ALL_SYMBOLS[start:end]
    return ALL_SYMBOLS[start:] + ALL_SYMBOLS[: end - len(ALL_SYMBOLS)]


def advance_batch():
    global batch_start_index
    with runtime_lock:
        batch_start_index = (batch_start_index + ACTIVE_BATCH_SIZE) % len(ALL_SYMBOLS)


def params_for_tf(tf: int) -> dict:
    if tf == 5:
        return {
            "ema_fast": 9,
            "ema_slow": 21,
            "rsi_period": 14,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "bb_window": 20,
            "bb_mult": 2.0,
            "rsi_buy_max": 42,
            "rsi_sell_min": 58,
            "reversal_buy_rsi": 30,
            "reversal_sell_rsi": 70,
            "squeeze_lookback": 60,
            "squeeze_percentile": 0.20,
            "strong_body_ratio": 0.72,
            "min_range_expansion": 1.10,
            "pullback_distance": 0.0014,
        }
    return {
        "ema_fast": 12,
        "ema_slow": 34,
        "rsi_period": 14,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "bb_window": 20,
        "bb_mult": 2.2,
        "rsi_buy_max": 45,
        "rsi_sell_min": 55,
        "reversal_buy_rsi": 33,
        "reversal_sell_rsi": 67,
        "squeeze_lookback": 50,
        "squeeze_percentile": 0.25,
        "strong_body_ratio": 0.68,
        "min_range_expansion": 1.08,
        "pullback_distance": 0.0018,
    }


def add_indicators(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    p = params_for_tf(tf)
    df = df.copy()

    df["ema_fast"] = df["c"].ewm(span=p["ema_fast"], adjust=False).mean()
    df["ema_slow"] = df["c"].ewm(span=p["ema_slow"], adjust=False).mean()

    delta = df["c"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / p["rsi_period"], adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / p["rsi_period"], adjust=False).mean().replace(0, pd.NA)
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    ema_fast = df["c"].ewm(span=p["macd_fast"], adjust=False).mean()
    ema_slow = df["c"].ewm(span=p["macd_slow"], adjust=False).mean()
    macd = ema_fast - ema_slow
    macd_signal = macd.ewm(span=p["macd_signal"], adjust=False).mean()
    df["macd_hist"] = macd - macd_signal

    mid = df["c"].rolling(window=p["bb_window"]).mean()
    std = df["c"].rolling(window=p["bb_window"]).std(ddof=0)
    df["bb_mid"] = mid
    df["bb_upper"] = mid + p["bb_mult"] * std
    df["bb_lower"] = mid - p["bb_mult"] * std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    df["range"] = df["h"] - df["l"]
    df["body"] = (df["c"] - df["o"]).abs()
    df["body_ratio"] = df["body"] / df["range"].replace(0, pd.NA)
    return df


def strat_trend_pullback(df: pd.DataFrame, tf: int) -> Optional[str]:
    p = params_for_tf(tf)
    if len(df) < max(p["ema_slow"], p["rsi_period"], p["macd_slow"]) + 3:
        return None

    d = add_indicators(df, tf)
    last = d.iloc[-1]
    prev = d.iloc[-2]
    prev2 = d.iloc[-3]

    if pd.isna(last["rsi"]) or pd.isna(last["macd_hist"]):
        return None

    uptrend = last["ema_fast"] > last["ema_slow"] and last["c"] > last["ema_slow"]
    pullback = min(last["l"], prev["l"]) <= float(last["ema_fast"]) * (1 + p["pullback_distance"])
    rejection = last["c"] > last["o"] and last["c"] > prev["h"]
    momentum = last["macd_hist"] > prev["macd_hist"] >= prev2["macd_hist"]
    rsi_ok = 48 <= float(last["rsi"]) <= p["rsi_buy_max"]

    if uptrend and pullback and rejection and momentum and rsi_ok:
        return "BUY"

    downtrend = last["ema_fast"] < last["ema_slow"] and last["c"] < last["ema_slow"]
    pullback = max(last["h"], prev["h"]) >= float(last["ema_fast"]) * (1 - p["pullback_distance"])
    rejection = last["c"] < last["o"] and last["c"] < prev["l"]
    momentum = last["macd_hist"] < prev["macd_hist"] <= prev2["macd_hist"]
    rsi_ok = p["rsi_sell_min"] <= float(last["rsi"]) <= 52

    if downtrend and pullback and rejection and momentum and rsi_ok:
        return "SELL"

    return None


def strat_squeeze_breakout(df: pd.DataFrame, tf: int) -> Optional[str]:
    p = params_for_tf(tf)
    need = max(p["bb_window"], p["squeeze_lookback"], p["ema_slow"]) + 2
    if len(df) < need:
        return None

    d = add_indicators(df, tf)
    last = d.iloc[-1]
    prev = d.iloc[-2]

    if pd.isna(last["bb_width"]) or pd.isna(last["bb_upper"]) or pd.isna(last["bb_lower"]):
        return None

    recent_width = d["bb_width"].tail(p["squeeze_lookback"])
    width_min = recent_width.min()
    width_threshold = recent_width.quantile(p["squeeze_percentile"])
    is_squeeze = float(last["bb_width"]) <= float(width_threshold) or float(last["bb_width"]) <= float(width_min) * 1.15

    range_expansion = float(last["range"]) >= float(prev["range"]) * p["min_range_expansion"]
    strong_body = pd.notna(last["body_ratio"]) and float(last["body_ratio"]) >= p["strong_body_ratio"]
    bullish_break = last["c"] > last["bb_upper"] and last["c"] > prev["h"] and strong_body and range_expansion
    bearish_break = last["c"] < last["bb_lower"] and last["c"] < prev["l"] and strong_body and range_expansion

    if is_squeeze and bullish_break:
        return "BUY"
    if is_squeeze and bearish_break:
        return "SELL"
    return None


def strat_reversal_engulfing(df: pd.DataFrame, tf: int) -> Optional[str]:
    p = params_for_tf(tf)
    if len(df) < max(p["ema_slow"], p["rsi_period"], p["macd_slow"]) + 3:
        return None

    d = add_indicators(df, tf)
    last = d.iloc[-1]
    prev = d.iloc[-2]
    prev2 = d.iloc[-3]

    if pd.isna(last["rsi"]) or pd.isna(last["macd_hist"]):
        return None

    bullish_engulf = (
        last["c"] > last["o"]
        and prev["c"] < prev["o"]
        and last["o"] <= prev["c"]
        and last["c"] >= prev["o"]
    )
    bearish_engulf = (
        last["c"] < last["o"]
        and prev["c"] > prev["o"]
        and last["o"] >= prev["c"]
        and last["c"] <= prev["o"]
    )

    macd_turn_up = last["macd_hist"] > prev["macd_hist"] >= prev2["macd_hist"]
    macd_turn_down = last["macd_hist"] < prev["macd_hist"] <= prev2["macd_hist"]

    bullish_reversal = (
        float(last["rsi"]) <= p["reversal_buy_rsi"]
        and bullish_engulf
        and macd_turn_up
        and last["c"] > prev["h"]
    )
    bearish_reversal = (
        float(last["rsi"]) >= p["reversal_sell_rsi"]
        and bearish_engulf
        and macd_turn_down
        and last["c"] < prev["l"]
    )

    if bullish_reversal:
        return "BUY"
    if bearish_reversal:
        return "SELL"
    return None


STRATEGIES = {
    "TREND_PULLBACK": strat_trend_pullback,
    "SQUEEZE_BREAKOUT": strat_squeeze_breakout,
    "REVERSAL_ENGULFING": strat_reversal_engulfing,
}


def upsert_candle_db(pair: str, timeframe: int, candle: dict):
    cur.execute(
        """
        INSERT INTO candles(pair, timeframe, bucket, o, h, l, c, v)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pair, timeframe, bucket) DO UPDATE SET
            o=excluded.o,
            h=excluded.h,
            l=excluded.l,
            c=excluded.c,
            v=excluded.v
        """,
        (pair, timeframe, candle["t"], candle["o"], candle["h"], candle["l"], candle["c"], candle["v"]),
    )
    conn.commit()


def load_recent_candles(pair: str, timeframe: int, limit: int = HISTORY_BARS) -> List[dict]:
    cur.execute(
        """
        SELECT bucket, o, h, l, c, v
        FROM candles
        WHERE pair=? AND timeframe=?
        ORDER BY bucket DESC
        LIMIT ?
        """,
        (pair, timeframe, limit),
    )
    rows = cur.fetchall()
    candles = [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5] or 0)}
        for r in rows
    ]
    candles.reverse()
    return candles


def log_signal(pair: str, timeframe: int, direction: str, entry: float, signal_bucket: int,
               expiry_bucket: int, strategy_votes: List[dict], strength: float, status: str = "OPEN") -> int:
    cur.execute(
        """
        INSERT INTO signals(
            pair, timeframe, direction, entry, signal_bucket, expiry_bucket,
            strategy_votes, strength, status, result, opened_ts, closed_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair, timeframe, direction, entry, signal_bucket, expiry_bucket,
            json.dumps(strategy_votes), strength, status, None, ts(), None,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_signal_result(signal_id: int, result: str):
    cur.execute(
        """
        UPDATE signals
        SET status='CLOSED', result=?, closed_ts=?
        WHERE id=?
        """,
        (result, ts(), signal_id),
    )
    conn.commit()


def ensure_strategy_row(timeframe: int, strategy: str):
    cur.execute(
        """
        INSERT OR IGNORE INTO strategy_stats(timeframe, strategy, wins, losses, ties, weight, updated_ts)
        VALUES (?, ?, 0, 0, 0, 1.0, ?)
        """,
        (timeframe, strategy, ts()),
    )
    conn.commit()


def get_strategy_weight(timeframe: int, strategy: str) -> float:
    ensure_strategy_row(timeframe, strategy)
    cur.execute("SELECT weight FROM strategy_stats WHERE timeframe=? AND strategy=?", (timeframe, strategy))
    row = cur.fetchone()
    return float(row[0]) if row else 1.0


def update_strategy_stats(timeframe: int, strategy: str, result: str):
    ensure_strategy_row(timeframe, strategy)
    cur.execute("SELECT wins, losses, ties, weight FROM strategy_stats WHERE timeframe=? AND strategy=?", (timeframe, strategy))
    wins, losses, ties, weight = cur.fetchone()

    if result == "WIN":
        wins += 1
    elif result == "LOSS":
        losses += 1
    else:
        ties += 1

    total = wins + losses + ties
    acc = (wins + 0.5 * ties) / max(1, total)
    target_weight = 0.75 + acc
    new_weight = weight * WEIGHT_DECAY + target_weight * (1 - WEIGHT_DECAY)
    new_weight = max(MIN_STRATEGY_WEIGHT, min(MAX_STRATEGY_WEIGHT, new_weight))

    cur.execute(
        """
        UPDATE strategy_stats
        SET wins=?, losses=?, ties=?, weight=?, updated_ts=?
        WHERE timeframe=? AND strategy=?
        """,
        (wins, losses, ties, new_weight, ts(), timeframe, strategy),
    )
    conn.commit()


def rolling_accuracy(timeframe: int) -> float:
    cur.execute(
        """
        SELECT result
        FROM signals
        WHERE timeframe=? AND status='CLOSED'
        ORDER BY id DESC
        LIMIT ?
        """,
        (timeframe, ROLLING_ACCURACY_WINDOW),
    )
    rows = cur.fetchall()
    if not rows:
        return 0.0
    wins = sum(1 for (r,) in rows if r == "WIN")
    ties = sum(1 for (r,) in rows if r == "TIE")
    return (wins + 0.5 * ties) / len(rows)


def adaptive_required_agreement(timeframe: int) -> int:
    return 3 if rolling_accuracy(timeframe) < 0.42 else 2


def fetch_fastforex_ohlc_page(pair: str, end_epoch_ms: Optional[int] = None, limit: int = MINUTE_PAGE_LIMIT) -> dict:
    clean = pair.replace("FX:", "").replace("/", "")
    params = {
        "api_key": FASTFOREX_API_KEY,
        "pair": clean,
        "interval": "PT1M",
        "limit": min(max(1, limit), MINUTE_PAGE_LIMIT),
        "dtmfmt": "ISO",
    }
    if end_epoch_ms is not None:
        params["end"] = end_epoch_ms

    r = requests_session.get(FASTFOREX_OHLC_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response: {data}")
    return data


def fetch_minute_history(pair: str, minutes_back: int) -> List[dict]:
    needed = max(1, int(minutes_back))
    end_ms = int(time.time() * 1000)
    collected: List[dict] = []
    seen = set()
    safety_pages = 0

    while len(collected) < needed and safety_pages < 30:
        safety_pages += 1
        data = fetch_fastforex_ohlc_page(pair, end_epoch_ms=end_ms, limit=MINUTE_PAGE_LIMIT)
        results = data.get("results", [])
        if not isinstance(results, list) or not results:
            break

        parsed = []
        for row in results:
            if not isinstance(row, dict):
                continue
            dtm = row.get("dtm")
            if dtm is None:
                continue
            epoch = parse_dtm_to_epoch(dtm)
            if epoch in seen:
                continue
            o = safe_float(row.get("o"))
            h = safe_float(row.get("h"))
            l = safe_float(row.get("l"))
            c = safe_float(row.get("c"))
            v = safe_float(row.get("v"), 0) or 0
            if None in (o, h, l, c):
                continue
            parsed.append({"t": epoch, "o": o, "h": h, "l": l, "c": c, "v": v})
            seen.add(epoch)

        if not parsed:
            break

        parsed.sort(key=lambda x: x["t"])
        collected = parsed + collected
        oldest_epoch = parsed[0]["t"]
        end_ms = int((oldest_epoch - 1) * 1000)

        if len(results) < MINUTE_PAGE_LIMIT:
            break

    collected.sort(key=lambda x: x["t"])
    return collected[-needed:]


def aggregate_1m_to_tf(rows: List[dict], tf: int) -> List[dict]:
    bars: Dict[int, dict] = {}
    tf_sec = tf_seconds_from_value(tf)
    now_min_bucket = floor_bucket(int(time.time()), 60)

    for row in sorted(rows, key=lambda r: int(r["t"])):
        minute_bucket = floor_bucket(int(row["t"]), 60)
        if minute_bucket >= now_min_bucket:
            continue
        tf_bucket = floor_bucket(int(row["t"]), tf_sec)
        o = safe_float(row.get("o"))
        h = safe_float(row.get("h"))
        l = safe_float(row.get("l"))
        c = safe_float(row.get("c"))
        v = safe_float(row.get("v"), 0) or 0
        if None in (o, h, l, c):
            continue
        if tf_bucket not in bars:
            bars[tf_bucket] = {"t": tf_bucket, "o": o, "h": h, "l": l, "c": c, "v": v}
        else:
            bars[tf_bucket]["h"] = max(bars[tf_bucket]["h"], h)
            bars[tf_bucket]["l"] = min(bars[tf_bucket]["l"], l)
            bars[tf_bucket]["c"] = c
            bars[tf_bucket]["v"] += v

    out = sorted(bars.values(), key=lambda x: x["t"])
    return out


def load_symbol_from_db(symbol: str, tf: int):
    cached = load_recent_candles(symbol, tf, HISTORY_BARS)
    if cached:
        states[symbol].history.clear()
        states[symbol].history.extend(cached)
        states[symbol].last_closed_bucket = cached[-1]["t"]
        states[symbol].history_seeded = True


def seed_symbol_history(symbol: str) -> bool:
    tf = get_tf()
    state = states[symbol]
    now_epoch = int(time.time())
    if state.history_seeded:
        return True
    if now_epoch < state.next_seed_retry_epoch:
        return False

    try:
        lookback_minutes = max(tf * (HISTORY_BARS + 10), 300)
        minute_rows = fetch_minute_history(symbol, lookback_minutes)
        if not minute_rows:
            raise RuntimeError("empty bootstrap response")

        aggregated = aggregate_1m_to_tf(minute_rows, tf)
        if not aggregated:
            raise RuntimeError("could not aggregate bootstrap candles")

        state.history.clear()
        for c in aggregated[-HISTORY_BARS:]:
            upsert_candle_db(symbol, tf, c)
            state.history.append(c)

        if state.history:
            state.last_closed_bucket = state.history[-1]["t"]
        state.history_seeded = True
        state.last_error = "-"
        state.last_update_utc = ts()
        print(f"[BOOTSTRAP] {symbol} tf={tf}: loaded {len(aggregated)} candles")
        return True
    except Exception as e:
        state.last_error = f"{ts()} | bootstrap error: {e}"
        state.next_seed_retry_epoch = now_epoch + 300
        print(f"[BOOTSTRAP] {symbol} error: {e}")
        return False


def bootstrap_from_db():
    tf = get_tf()
    for symbol in ALL_SYMBOLS:
        load_symbol_from_db(symbol, tf)


def reset_runtime_state():
    tf = get_tf()
    print(f"[RESET] switching runtime to {tf}m")
    for s in states.values():
        s.history.clear()
        s.history_seeded = False
        s.last_closed_bucket = None
        s.next_seed_retry_epoch = 0
        s.last_signal = "-"
        s.last_strategy_votes = "-"
        s.last_strength = 0.0
        s.last_outcome = "-"
        s.last_error = "-"
        s.last_price = None
        s.last_poll_utc = "-"
        s.last_update_utc = "-"
    bootstrap_from_db()
    for symbol in current_batch_symbols():
        seed_symbol_history(symbol)


def evaluate_expired_signals(symbol: str, closed_candle: dict):
    tf = get_tf()
    closed_bucket = int(closed_candle["t"])
    close_price = float(closed_candle["c"])

    cur.execute(
        """
        SELECT id, direction, entry, strategy_votes
        FROM signals
        WHERE pair=? AND timeframe=? AND status='OPEN' AND expiry_bucket=?
        ORDER BY id ASC
        """,
        (symbol, tf, closed_bucket),
    )
    rows = cur.fetchall()
    if not rows:
        return

    for signal_id, direction, entry, strategy_votes_json in rows:
        entry = float(entry)
        if direction == "BUY":
            result = "WIN" if close_price > entry else "LOSS" if close_price < entry else "TIE"
        else:
            result = "WIN" if close_price < entry else "LOSS" if close_price > entry else "TIE"

        update_signal_result(int(signal_id), result)

        try:
            votes = json.loads(strategy_votes_json)
        except Exception:
            votes = []

        for vote in votes:
            strat = vote.get("strategy")
            if strat:
                update_strategy_stats(tf, strat, result)

        state = states[symbol]
        state.last_outcome = result
        state.last_update_utc = ts()
        tg_send(
            f"<b>📊 Signal result</b>\n"
            f"Pair: <b>{symbol}</b>\n"
            f"Direction: <b>{direction}</b>\n"
            f"Result: <b>{result}</b>\n"
            f"Entry: <code>{entry}</code>\n"
            f"Close: <code>{close_price}</code>\n"
            f"Timeframe: <code>{tf}m</code>"
        )


def vote_strategies(df: pd.DataFrame, tf: int) -> List[dict]:
    votes = []
    for name, fn in STRATEGIES.items():
        try:
            signal = fn(df, tf)
            if signal:
                weight = get_strategy_weight(tf, name)
                votes.append({"strategy": name, "direction": signal, "weight": weight})
        except Exception as e:
            print(f"[STRATEGY] {name} error: {e}")
    return votes


def consensus_signal(votes: List[dict], tf: int) -> Tuple[Optional[str], float, int]:
    if not votes:
        return None, 0.0, 0

    by_dir = {"BUY": [], "SELL": []}
    for v in votes:
        by_dir[v["direction"]].append(v)

    required = adaptive_required_agreement(tf)
    best_dir = None
    best_weight = 0.0
    best_count = 0

    for direction, items in by_dir.items():
        count = len(items)
        weight_sum = sum(float(i.get("weight", 1.0)) for i in items)
        if count > best_count or (count == best_count and weight_sum > best_weight):
            best_dir = direction
            best_weight = weight_sum
            best_count = count

    if best_dir is None or best_count < required:
        return None, best_weight, best_count

    return best_dir, best_weight, best_count


def maybe_emit_signal(symbol: str, closed_candle: dict):
    tf = get_tf()
    state = states[symbol]
    candles = list(state.history)
    params = params_for_tf(tf)
    min_bars = max(params["ema_slow"], params["rsi_period"], params["macd_slow"], params["bb_window"]) + 4

    if len(candles) < min_bars:
        return

    df = candles_to_df(candles)
    if len(df) < min_bars:
        return

    votes = vote_strategies(df, tf)
    direction, weight_sum, vote_count = consensus_signal(votes, tf)
    if not direction:
        return

    strength = round(weight_sum, 2)
    signal_bucket = int(df.iloc[-1]["t"])
    expiry_bucket = signal_bucket + tf_seconds()
    entry = float(df.iloc[-1]["c"])

    state.last_signal = direction
    state.last_strategy_votes = ", ".join([v["strategy"] for v in votes])
    state.last_strength = strength
    state.last_update_utc = ts()

    signal_id = log_signal(
        pair=symbol,
        timeframe=tf,
        direction=direction,
        entry=entry,
        signal_bucket=signal_bucket,
        expiry_bucket=expiry_bucket,
        strategy_votes=votes,
        strength=strength,
        status="OPEN",
    )

    display_votes = ", ".join([f'{v["strategy"]}:{v["direction"]}' for v in votes])
    tg_send(
        f"<b>✅ Confirmed signal</b>\n"
        f"Pair: <b>{symbol}</b>\n"
        f"Direction: <b>{direction}</b>\n"
        f"Timeframe: <code>{tf}m</code>\n"
        f"Strategies: <code>{display_votes}</code>\n"
        f"Agreement: <b>{vote_count}/3</b>\n"
        f"Strength: <code>{strength:.2f}</code>\n"
        f"Entry: <code>{entry}</code>\n"
        f"Signal ID: <code>{signal_id}</code>"
    )


def process_closed_candle(symbol: str, candle: dict):
    tf = get_tf()
    state = states[symbol]
    bucket = int(candle["t"])

    if state.last_closed_bucket is not None and bucket <= state.last_closed_bucket:
        return

    state.history.append(candle)
    upsert_candle_db(symbol, tf, candle)
    state.last_closed_bucket = bucket
    state.history_seeded = True
    state.last_update_utc = ts()

    evaluate_expired_signals(symbol, candle)
    maybe_emit_signal(symbol, candle)


def sync_symbol_history(symbol: str) -> bool:
    tf = get_tf()
    state = states[symbol]
    now_epoch = int(time.time())
    if now_epoch < state.next_seed_retry_epoch:
        return False

    try:
        lookback_minutes = max(tf * (HISTORY_BARS + 10), 300)
        minute_rows = fetch_minute_history(symbol, lookback_minutes)
        if not minute_rows:
            raise RuntimeError("empty history response")

        aggregated = aggregate_1m_to_tf(minute_rows, tf)
        if not aggregated:
            raise RuntimeError("no aggregated candles")

        if state.last_closed_bucket is None and not state.history:
            for c in aggregated[-HISTORY_BARS:]:
                upsert_candle_db(symbol, tf, c)
                state.history.append(c)
            if state.history:
                state.last_closed_bucket = state.history[-1]["t"]
                state.history_seeded = True
            return True

        new_bars = []
        for c in aggregated:
            if state.last_closed_bucket is None or c["t"] > state.last_closed_bucket:
                new_bars.append(c)

        for c in new_bars:
            process_closed_candle(symbol, c)

        state.history_seeded = True
        state.last_error = "-"
        state.last_poll_utc = ts()
        return True
    except Exception as e:
        state.last_error = f"{ts()} | {e}"
        state.next_seed_retry_epoch = now_epoch + 180
        print(f"[{symbol}] sync error: {e}")
        return False


def poll_prices_loop():
    while not stop_event.is_set():
        loop_start = time.time()
        batch = current_batch_symbols()
        tf = get_tf()
        print(f"[POLL] tf={tf} batch={batch}")

        for symbol in batch:
            if stop_event.is_set():
                break
            try:
                sync_symbol_history(symbol)
            except Exception as e:
                states[symbol].last_error = f"{ts()} | {e}"
                print(f"[{symbol}] fetch error: {e}")
            time.sleep(0.05)

        elapsed = time.time() - loop_start
        sleep_for = max(1.0, POLL_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_for)


def rotation_loop():
    while not stop_event.is_set():
        time.sleep(ROTATE_BATCH_SECONDS)
        if stop_event.is_set():
            break
        advance_batch()
        print(f"[ROTATE] next batch -> {current_batch_symbols()}")


@bot.message_handler(commands=["start"])
def cmd_start(message):
    tf = get_tf()
    tg_send(
        f"🚀 <b>Mariam Bot Started</b>\n"
        f"Pairs: <b>{len(ALL_SYMBOLS)}</b>\n"
        f"Active batch: <b>{len(current_batch_symbols())}</b>\n"
        f"Current timeframe: <code>{tf}m</code>\n"
        f"Mode: <code>API OHLC minute sync + 5m/15m aggregation</code>"
    )


@bot.message_handler(commands=["tf"])
def cmd_tf(message):
    parts = message.text.strip().split()
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Use: <code>/tf 5</code> or <code>/tf 15</code>")
        return
    try:
        new_tf = int(parts[1])
        set_tf(new_tf)
        tg_send(f"✅ Timeframe changed to <b>{new_tf}m</b>\nHistory reset and reseeded.")
    except Exception as e:
        bot.send_message(message.chat.id, f"Could not change timeframe: <code>{e}</code>")


@bot.message_handler(commands=["status"])
def cmd_status(message):
    tf = get_tf()
    current_active = current_batch_symbols()
    ordered = sorted(current_active, key=lambda s: states[s].last_update_utc, reverse=True)
    lines = [
        f"<b>Status</b>",
        f"Polling: <b>running</b>",
        f"Timeframe: <code>{tf}m</code>",
        f"Active batch size: <b>{len(current_active)}</b>",
        f"Active symbols: <code>{', '.join(current_active)}</code>",
        f"Rolling accuracy: <b>{rolling_accuracy(tf)*100:.1f}%</b>",
        "",
    ]
    for sym in ordered:
        s = states[sym]
        lines.append(
            f"{sym}: signal=<code>{s.last_signal}</code> | outcome=<code>{s.last_outcome}</code> | poll=<code>{s.last_poll_utc}</code> | err=<code>{s.last_error}</code>"
        )
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["pairs"])
def cmd_pairs(message):
    bot.send_message(message.chat.id, "<b>Current batch</b>\n<code>" + ", ".join(current_batch_symbols()) + "</code>")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    tf = get_tf()
    cur.execute(
        """
        SELECT strategy, wins, losses, ties, weight
        FROM strategy_stats
        WHERE timeframe=?
        ORDER BY strategy ASC
        """,
        (tf,),
    )
    rows = cur.fetchall()
    if not rows:
        bot.send_message(message.chat.id, "No strategy stats yet.")
        return

    out = [f"<b>Stats {tf}m</b>"]
    for strategy, wins, losses, ties, weight in rows:
        total = wins + losses + ties
        acc = ((wins + 0.5 * ties) / total * 100) if total else 0.0
        out.append(f"{strategy}: W{wins} L{losses} T{ties} | acc=<b>{acc:.1f}%</b> | weight=<code>{float(weight):.2f}</code>")
    bot.send_message(message.chat.id, "\n".join(out))


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    stop_event.set()
    tg_send("🛑 <b>Bot stopping</b>")


def main():
    bootstrap_from_db()
    for symbol in current_batch_symbols():
        seed_symbol_history(symbol)

    threading.Thread(target=poll_prices_loop, daemon=True).start()
    threading.Thread(target=rotation_loop, daemon=True).start()

    tg_send("👋 Mariam bot is starting...")

    while not stop_event.is_set():
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            print(f"[Telegram] polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
