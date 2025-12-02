#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tw_stockbot_push.py — 台股 MA10/MA20 突破 + 漲跌幅推播（yfinance-only, LINE Messaging API）

變更點
- 每一檔都顯示「今日漲跌幅」（用 ▲/▼/— 表示方向 + 百分比），
- 若該檔有出現 MA10/MA20 跨日突破，再在同一行加上提示（不顯示價格或均線數值）。

其餘特性
- 只用 yfinance 的「日線」資料
- 自動 fallback：先 .TW → 再 .TWO（或相反）
- 台灣時間 09:00–13:30 執行（可用 ALLOW_OUTSIDE_WINDOW=true 跳過）
- LINE Messaging API（支援 LINE_CHANNEL_ACCESS_TOKEN/LINE_CHANNEL_TOKEN、LINE_TO/LINE_USER_ID）
"""

from datetime import datetime
import pytz

TZ_TAIPEI = pytz.timezone("Asia/Taipei")

# ===== 先做週末檢查（以台北時間為準） =====
now_tw = datetime.now(TZ_TAIPEI)
if now_tw.weekday() >= 5:  # 週六(5)、週日(6)
    print(f"[INFO] 台北時間 {now_tw.strftime('%Y-%m-%d %H:%M:%S %Z')} 為週末，不執行。")
    exit(0)
# ===== 週一～週五才會繼續往下執行 =====
import os
import sys
import time
import math
import warnings
from typing import Optional, Tuple, List
from datetime import datetime, time as dtime

import pytz
import requests
import yfinance as yf
import pandas as pd

# ---- 降噪 ----
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*possibly delisted.*")
warnings.filterwarnings("ignore", message=".*Quote not found for symbol.*")

# ========= 使用者設定 ======
# 要追蹤的股票代碼（純數字即可，也可含 .TW / .TWO）
WATCH_CODES = [ "2059","2330", "2344", "8299", "6412", "2454", "3443", "3661", "6515", "3711"]
# 每次對 LINE 送出訊息之間的最短間隔（秒）
LINE_PUSH_INTERVAL = 1.0

# 是否只在台北時間 09:00–13:30 間執行
# 若設為 "true" 以外，就算超出時間區間也會照跑
TIME_WINDOW_CHECK = os.getenv("TIME_WINDOW_CHECK", "true").lower() == "true"

# ========= LINE 設定 =========
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or os.getenv("LINE_CHANNEL_TOKEN")
LINE_TO = os.getenv("LINE_TO") or os.getenv("LINE_USER_ID")

if not LINE_TOKEN or not LINE_TO:
    print("[WARN] 未提供 LINE token 或 LINE 接收者 ID，將只在終端機印出，不實際推播。")

LINE_API_URL = "https://api.line.me/v2/bot/message/push"


def send_line_text(msg: str) -> None:
    """送純文字訊息到 LINE（若沒設定 token / to，就只印出不送）。"""
    if not LINE_TOKEN or not LINE_TO:
        print("[LINE 模擬]", msg)
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
    """
    檢查目前是否在台北時間 09:00–13:30 之間。
    - RETURN: True = 在此區間內；False = 不在區間內
    """
    if now is None:
        now = datetime.now(TZ_TAIPEI)
    start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    end = now.replace(hour=13, minute=30, second=0, microsecond=0)
    return start <= now <= end


# ========= yfinance 工具 =========
def _resolve_symbol(code: str) -> Optional[str]:
    """根據 code（可含 .TW/.TWO 或純數字）決定優先順序，先用日線快查確認是否有資料。"""
    code = code.strip()
    if "." in code:
        primary = code
        if code.endswith(".TW"):
            backup = code[:-3] + ".TWO"
        elif code.endswith(".TWO"):
            backup = code[:-4] + ".TW"
        else:
            backup = None
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
    """下載日線收盤價，回傳 Close 欄位的 Series（已去除 NaN）。"""
    data = yf.download(symbol, period=period, interval="1d", progress=False)
    if not isinstance(data, pd.DataFrame) or data.empty:
        return pd.Series(dtype=float)
    closes = data["Close"].dropna()
    return closes


def _moving_mean(s: pd.Series, n: int) -> Optional[float]:
    """若長度不足 n，就回 None；否則回最後一筆 n 日均價。"""
    return float(s.tail(n).mean()) if len(s) >= n else None


# ========= 訊號判斷 =========
def analyze_symbol(symbol: str) -> Tuple[Optional[float], List[str], Optional[str]]:
    """
    回傳:
    - 今日漲跌幅百分比 (float 或 None)
    - 訊號列表（突破訊號文字）
    - ma_status: 當日收盤相對 MA10/MA20 的位置描述字串，例如 "高於 MA10、低於 MA20"
    """
    closes = _fetch_daily_closes(symbol)
    if len(closes) < 2:
        return None, [], None

    today_close = float(closes.iloc[-1])
    y_close = float(closes.iloc[-2])
    pct_change = None if y_close == 0 else 100.0 * (today_close - y_close) / y_close

    signals: List[str] = []
    ma_status: Optional[str] = None

    # 至少需要 20 根才能算 MA20_y / MA20_t（順便算 MA10）
    if len(closes) >= 21:
        ma10_y = _moving_mean(closes.iloc[:-1], 10)
        ma10_t = _moving_mean(closes, 10)
        ma20_y = _moving_mean(closes.iloc[:-1], 20)
        ma20_t = _moving_mean(closes, 20)

        if None not in (ma10_y, ma10_t, ma20_y, ma20_t):
            # ===== 突破判斷（沿用原本邏輯） =====
            # MA20
            if (y_close < ma20_y) and (today_close > ma20_t):
                signals.append("向上突破 MA20，買進")
            elif (y_close > ma20_y) and (today_close < ma20_t):
                signals.append("向下突破 MA20，賣出")
            # MA10
            if (y_close < ma10_y) and (today_close > ma10_t):
                signals.append("向上突破 MA10，買進")
            elif (y_close > ma10_y) and (today_close < ma10_t):
                signals.append("向下突破 MA10，賣出")

            # ===== 目前相對 MA10 / MA20 的位置 =====
            parts: List[str] = []
            if ma10_t is not None:
                parts.append("高於 MA10" if today_close > ma10_t else "低於 MA10")
            if ma20_t is not None:
                parts.append("高於 MA20" if today_close > ma20_t else "低於 MA20")
            if parts:
                ma_status = "、".join(parts)

    else:
        # 資料不足 20 根，但如果 >=11，也給 MA10 狀態
        if len(closes) >= 11:
            ma10_t = _moving_mean(closes, 10)
            if ma10_t is not None:
                ma_status = "高於 MA10" if today_close > ma10_t else "低於 MA10"

    return pct_change, signals, ma_status


def _consolidate_signals(code: str, signals: list[str]) -> list[str]:
    """
    把同方向（向上=買進 / 向下=賣出）的 MA10、MA20 合併成一條訊息。
    例如：
      ["向上突破 MA20，買進", "向上突破 MA10，買進"]
      -> ["3206｜向上突破 MA10 MA20，買進"]
    """
    up_levels, down_levels = [], []

    for s in signals:
        lvl = "MA10" if "MA10" in s else ("MA20" if "MA20" in s else None)
        if not lvl:
            continue
        if ("向上突破" in s) or ("突破" in s and "買進" in s):
            up_levels.append(lvl)
        elif ("向下" in s) or ("跌落" in s and "賣出" in s):
            down_levels.append(lvl)

    msgs = []
    if up_levels:
        lvls = sorted(up_levels, key=lambda x: int(x[2:]))  # MA10 → MA20
        msgs.append(f"{code}｜向上突破 {' '.join(lvls)}，買進")
    if down_levels:
        lvls = sorted(down_levels, key=lambda x: int(x[2:]))
        msgs.append(f"{code}｜向下跌落 {' '.join(lvls)}，賣出")
    return msgs


# ========= 主流程 =========
def main() -> None:
    # 1) 時間區間檢查
    now_tw = datetime.now(TZ_TAIPEI)
    print("[INFO] Now (Taipei):", now_tw.strftime("%Y-%m-%d %H:%M:%S %Z"))

    if TIME_WINDOW_CHECK and not is_in_trading_window(now_tw):
        print("[INFO] 不在台北時間 09:00–13:30 之間，程式結束。")
        return

    # 2) 解析每檔股票的實際 yfinance symbol
    resolved = {}
    for code in WATCH_CODES:
        sym = _resolve_symbol(code)
        if sym:
            resolved[code] = sym
            print(f"[RESOLVE] {code} -> {sym}")
        else:
            print(f"[RESOLVE] {code} -> 無有效日線資料，略過")

    if not resolved:
        print("[ERROR] 沒有任何可用的股票代碼。")
        return

    # 3) 逐一分析
    codes = list(resolved.keys())
    BATCH_SIZE = 5  # 一次處理幾檔，避免對 LINE 壓力太大

    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        for code in batch:
            sym = resolved[code]
            pct_change, signals, ma_status = analyze_symbol(sym)
            base = sym.split(".")[0]

            if signals:
                # 有突破：沿用原本格式（多檔 MA10/MA20 會被合併）
                msgs = _consolidate_signals(base, signals)
                for msg in msgs:
                    print(msg)
                    send_line_text(msg)
                    time.sleep(1.0)
            else:
                # 沒有突破：一樣要通知目前在 MA10/MA20 的位置
                if ma_status:
                    msg = f"{base} 目前 {ma_status}"
                else:
                    msg = f"{base} 目前 無法計算 MA10/MA20"
                print(msg)
                send_line_text(msg)
                time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", e)
        sys.exit(1)
