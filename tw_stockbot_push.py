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

# ========= 使用者設定 =========
DEFAULT_CODES = [ "2059","2330", "2344", "8299", "6412", "2454", "3443", "3661", "6515", "3711"]
TW_CODES = [c.strip() for c in os.getenv("TW_CODES", "").split(",") if c.strip()] or DEFAULT_CODES

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))
TZ_TAIPEI = pytz.timezone("Asia/Taipei")
MARKET_START = dtime(9, 0, 0)     # 台灣時間 09:00
MARKET_END   = dtime(13, 30, 0)   # 台灣時間 13:30
ALLOW_OUTSIDE_WINDOW = os.getenv("ALLOW_OUTSIDE_WINDOW", "false").lower() == "true"

# LINE Messaging API（支援兩種命名）
LINE_CHANNEL_ACCESS_TOKEN = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or os.getenv("LINE_CHANNEL_TOKEN") or "").strip()
LINE_TO = (os.getenv("LINE_TO") or os.getenv("LINE_USER_ID") or "").strip()
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"


# ========= 小工具 =========
def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)


def within_tw_session(now: Optional[datetime] = None) -> bool:
    if now is None:
        now = now_taipei()
    t = now.time()
    return MARKET_START <= t <= MARKET_END


def send_line_text(message: str) -> bool:
    """用 LINE Messaging API 推播文字。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TO:
        print("[WARN] LINE Messaging API 環境變數未設定（LINE_CHANNEL_ACCESS_TOKEN/LINE_CHANNEL_TOKEN、LINE_TO/LINE_USER_ID）。")
        return False
    headers = {
        "Authorization": "Bearer " + LINE_CHANNEL_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"to": LINE_TO, "messages": [{"type": "text", "text": message}]}
    try:
        resp = requests.post(LINE_PUSH_API, headers=headers, json=payload, timeout=20)
        if resp.status_code == 200:
            print("[INFO] LINE 推播成功。")
            return True
        print(f"[ERROR] LINE 推播失敗 {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[ERROR] LINE 推播錯誤: {e}")
        return False

# ========= 新增在這裡 👇 =========
def ma_cross_signal(
    code: str,
    prev_close: float,
    now_price: float,
    prev_ma10: float,
    curr_ma10: float,
    prev_ma20: float,
    curr_ma20: float,
) -> str | None:
    """判斷 MA10 / MA20 跨日突破訊號。"""
    if prev_close < prev_ma20 and now_price > curr_ma20:
        return f"{code}｜買進，突破MA20"
    if prev_close > prev_ma20 and now_price < curr_ma20:
        return f"{code}｜賣出，跌落MA20"
    if prev_close < prev_ma10 and now_price > curr_ma10:
        return f"{code}｜買進，突破MA10"
    if prev_close > prev_ma10 and now_price < curr_ma10:
        return f"{code}｜賣出，跌落MA10"
    return None

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
            backup = code.split(".")[0] + ".TW"
    else:
        primary = f"{code}.TW"
        backup = f"{code}.TWO"

    for sym in (primary, backup):
        try:
            df = yf.Ticker(sym).history(period="15d", interval="1d", prepost=False, actions=False, auto_adjust=False)
            if not df.empty and df["Close"].dropna().shape[0] >= 2:
                return sym
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _fetch_daily_closes(symbol: str, tries: int = 3, delay: float = 0.6) -> pd.Series:
    """抓 90 天日線 Close，關閉 auto_adjust，內建重試。"""
    last_err = None
    for _ in range(tries):
        try:
            df = yf.Ticker(symbol).history(period="90d", interval="1d", prepost=False, actions=False, auto_adjust=False)
            closes = df["Close"].dropna() if not df.empty else pd.Series(dtype="float64")
            if len(closes) >= 2:
                return closes
        except Exception as e:
            last_err = e
        time.sleep(delay)
    if last_err:
        print(f"[WARN] 抓日線失敗 {symbol}: {last_err}")
    return pd.Series(dtype="float64")


# ========= 訊號與漲跌幅計算 =========
def _moving_mean(s: pd.Series, n: int) -> Optional[float]:
    return float(s.tail(n).mean()) if len(s) >= n else None


def analyze_symbol(symbol: str) -> Tuple[Optional[float], List[str]]:
    """
    回傳 (今日漲跌幅百分比, 訊號列表)。若日線不足則回 (None, [])
    - 今日漲跌幅 = (今收 - 昨收) / 昨收 * 100
    - 訊號：依昨收/今收相對 MA10/MA20（昨日/今日）判斷跨日突破
    """
    closes = _fetch_daily_closes(symbol)
    if len(closes) < 2:
        return None, []

    today_close = float(closes.iloc[-1])
    y_close = float(closes.iloc[-2])
    pct_change = None if y_close == 0 else 100.0 * (today_close - y_close) / y_close

    signals: List[str] = []
    # 至少需要 20 根才能算 MA20_y / MA20_t
    if len(closes) >= 21:
        ma10_y = _moving_mean(closes.iloc[:-1], 10)
        ma10_t = _moving_mean(closes, 10)
        ma20_y = _moving_mean(closes.iloc[:-1], 20)
        ma20_t = _moving_mean(closes, 20)
        if None not in (ma10_y, ma10_t, ma20_y, ma20_t):
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

    return pct_change, signals



# ========= 主流程 =========
def main():
    now = now_taipei()
    if not within_tw_session(now) and not ALLOW_OUTSIDE_WINDOW:
        print(f"[INFO] 現在 {now.strftime('%Y-%m-%d %H:%M:%S %Z')} 非台股盤中（09:00–13:30），結束。")
        return

    codes = list(TW_CODES)
    lines: List[str] = []
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i+BATCH_SIZE]
        for code in batch:
            sym = _resolve_symbol(code)
            if not sym:
                print(f"[WARN] 無法解析有效市場：{code}")
                continue

            pct_change, signals = analyze_symbol(sym)
            base = sym.split(".")[0]
            if signals:
                msgs = _consolidate_signals(base, signals)
                for msg in msgs:
                    print(msg)
                    send_line_text(msg)
                    time.sleep(1.0)



if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", e)
        sys.exit(1)
