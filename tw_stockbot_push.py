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
DEFAULT_CODES = ["2330", "2603", "3206", "3324", "8446"]
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


def _arrow(p: Optional[float]) -> str:
    if p is None or math.isnan(p):
        return "—"
    if p > 0:
        return "▲"
    if p < 0:
        return "▼"
    return "—"


def _fmt_pct(p: Optional[float]) -> str:
    if p is None or math.isnan(p):
        return "—"
    return f"{abs(p):.2f}%"  # 百分比顯示絕對值，方向用箭頭表達


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
            arrow = _arrow(pct_change)
            pct_s = _fmt_pct(pct_change)
            if signals:
                # 同一行：代碼 + 漲跌幅 + 訊號
                lines.append(f"{base} {arrow}{pct_s}（{ '；'.join(signals) }）")
            else:
                lines.append(f"{base} {arrow}{pct_s}")

            time.sleep(0.2)  # 降低 Yahoo 節流
        time.sleep(0.5)

    # 整理並推播（每則限制長度，簡單切段）
    if lines:
        header = f"【台股盤中】{now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        buf = header
        chunks: List[str] = []
        for line in lines:
            if len(buf) + 1 + len(line) > 900:
                chunks.append(buf)
                buf = header + "\n" + line
            else:
                buf += "\n" + line
        if buf:
            chunks.append(buf)

        for part in chunks:
            print(part)
            send_line_text(part)
            time.sleep(1.0)
    else:
        print("[INFO] 本次沒有可顯示的個股資訊。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", e)
        sys.exit(1)
