#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tw_stockbot_push.py — 台股 MA10/MA20 突破 + 狀態推播（yfinance + LINE）

功能：
- 週六日不執行
- 平日 09:00–13:30 執行（可用 TIME_WINDOW_CHECK=false 關閉）
- 每檔股票一定會列出一行
- 最終只會送出「一條」LINE 訊息（所有股票合併）
"""

import os
import sys
import time
import warnings
from typing import Optional, Tuple, List
from datetime import datetime
import pytz
import requests
import yfinance as yf
import pandas as pd

# ========= 時區與週末判斷 =========
TZ_TAIPEI = pytz.timezone("Asia/Taipei")

_now_tw = datetime.now(TZ_TAIPEI)
if _now_tw.weekday() >= 5:  # 週六(5)、週日(6)
    print(f"[INFO] 台北時間 {_now_tw.strftime('%Y-%m-%d %H:%M:%S %Z')} 為週末，不執行。")
    sys.exit(0)

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*possibly delisted.*")
warnings.filterwarnings("ignore", message=".*Quote not found for symbol.*")

# ========= 使用者設定 =========
WATCH_CODES = [ "2059","2330", "2344", "8299", "6412", "2454", "3443", "3661", "6515", "3711"]

TIME_WINDOW_CHECK = os.getenv("TIME_WINDOW_CHECK", "true").lower() == "true"

# ========= LINE 設定 =========
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or os.getenv("LINE_CHANNEL_TOKEN")
LINE_TO = os.getenv("LINE_TO") or os.getenv("LINE_USER_ID")

LINE_API_URL = "https://api.line.me/v2/bot/message/push"

def send_line_text(msg: str) -> None:
    """送純文字訊息到 LINE（若沒設定 token / to，就只印出不送）。"""
    if not LINE_TOKEN or not LINE_TO:
        print("[LINE 模擬]\n" + msg)
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }
    body = {
        "to": LINE_TO,
        "messages": [{"type": "text", "text": msg}],
    }
    try:
        resp = requests.post(LINE_API_URL, json=body, headers=headers, timeout=10)
        if not 200 <= resp.status_code < 300:
            print(f"[LINE ERROR] status={resp.status_code}, body={resp.text}")
    except Exception as e:
        print("[LINE EXCEPTION]", e)


# ========= 時間判斷 =========
def is_in_trading_window(now: Optional[datetime] = None) -> bool:
    if now is None:
        now = datetime.now(TZ_TAIPEI)
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return start <= now <= end


# ========= yfinance 工具 =========
def _resolve_symbol(code: str) -> Optional[str]:
    code = code.strip()
    if "." in code:
        primary = code
        backup = None
        if code.endswith(".TW"):
            backup = code[:-3] + ".TWO"
        elif code.endswith(".TWO"):
            backup = code[:-4] + ".TW"
        candidates = [primary] + ([backup] if backup else [])
    else:
        candidates = [f"{code}.TW", f"{code}.TWO"]

    for sym in candidates:
        try:
            data = yf.download(sym, period="5d", interval="1d", progress=False)
            if isinstance(data, pd.DataFrame) and not data.empty:
                closes = data["Close"].dropna()
                if len(closes) > 0:
                    return sym
        except Exception:
            continue
    return None


def _fetch_daily_closes(symbol: str, period: str = "60d") -> pd.Series:
    data = yf.download(symbol, period=period, interval="1d", progress=False)
    if not isinstance(data, pd.DataFrame) or data.empty:
        return pd.Series(dtype=float)
    closes = data["Close"].dropna()
    return closes


def _moving_mean(s: pd.Series, n: int) -> Optional[float]:
    return float(s.tail(n).mean()) if len(s) >= n else None


# ========= 判斷突破與目前狀態 =========
def analyze_symbol(symbol: str) -> Tuple[Optional[float], List[str], Optional[str]]:
    closes = _fetch_daily_closes(symbol)
    if len(closes) < 2:
        return None, [], None

    today_close = float(closes.iloc[-1])
    y_close = float(closes.iloc[-2])
    pct_change = None if y_close == 0 else 100.0 * (today_close - y_close) / y_close

    signals = []
    ma_status = None

    if len(closes) >= 21:
        ma10_y = _moving_mean(closes.iloc[:-1], 10)
        ma10_t = _moving_mean(closes, 10)
        ma20_y = _moving_mean(closes.iloc[:-1], 20)
        ma20_t = _moving_mean(closes, 20)

        if None not in (ma10_y, ma10_t, ma20_y, ma20_t):
            if (y_close < ma20_y) and (today_close > ma20_t):
                signals.append("向上突破 MA20，買進")
            elif (y_close > ma20_y) and (today_close < ma20_t):
                signals.append("向下跌落 MA20，賣出")

            if (y_close < ma10_y) and (today_close > ma10_t):
                signals.append("向上突破 MA10，買進")
            elif (y_close > ma10_y) and (today_close < ma10_t):
                signals.append("向下跌落 MA10，賣出")

            parts = []
            parts.append("高於 MA10" if today_close > ma10_t else "低於 MA10")
            parts.append("高於 MA20" if today_close > ma20_t else "低於 MA20")
            ma_status = "、".join(parts)

    else:
        if len(closes) >= 11:
            ma10_t = _moving_mean(closes, 10)
            ma_status = "高於 MA10" if today_close > ma10_t else "低於 MA10"

    return pct_change, signals, ma_status


def _consolidate_signals(code: str, signals: List[str]) -> List[str]:
    up_levels = []
    down_levels = []

    for s in signals:
        lvl = "MA10" if "MA10" in s else ("MA20" if "MA20" in s else None)
        if not lvl:
            continue
        if "買進" in s:
            up_levels.append(lvl)
        elif "賣出" in s:
            down_levels.append(lvl)

    msgs = []
    if up_levels:
        lvls = sorted(up_levels, key=lambda x: int(x[2:]))
        msgs.append(f"{code}｜向上突破 {' '.join(lvls)}，買進")
    if down_levels:
        lvls = sorted(down_levels, key=lambda x: int(x[2:]))
        msgs.append(f"{code}｜向下跌落 {' '.join(lvls)}，賣出")
    return msgs


# ========= 主流程（只送一條 LINE 訊息） =========
def main() -> None:
    now_tw = datetime.now(TZ_TAIPEI)
    print("[INFO] Now (Taipei):", now_tw.strftime("%Y-%m-%d %H:%M:%S %Z"))

    if TIME_WINDOW_CHECK and not is_in_trading_window(now_tw):
        print("[INFO] 不在 09:00–13:30，不執行。")
        return

    resolved = {}
    for code in WATCH_CODES:
        sym = _resolve_symbol(code)
        if sym:
            resolved[code] = sym

    if not resolved:
        print("[ERROR] 無可用股票")
        return

    all_msgs = []

    for code in WATCH_CODES:
        if code not in resolved:
            continue

        sym = resolved[code]
        pct_change, signals, ma_status = analyze_symbol(sym)
        base = sym.split(".")[0]

        if signals:
            all_msgs.extend(_consolidate_signals(base, signals))
        else:
            text = f"{base} 目前 {ma_status}" if ma_status else f"{base} 目前 無法計算 MA10/MA20"
            all_msgs.append(text)

    final_text = "\n".join(all_msgs)
    print("[FINAL MESSAGE]\n" + final_text)
    send_line_text(final_text)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", e)
        sys.exit(1)
