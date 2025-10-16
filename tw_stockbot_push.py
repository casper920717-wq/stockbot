#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tw_stockbot_push.py (yfinance-only, Messaging API)
--------------------------------------------------
- 台股抓價只用 yfinance，穩定處理 .TW/.TWO、自動重試、海外/本機皆可
- 取得「最新價（1m 最後一筆）」與「昨收（日線倒數第二筆）」
- 預設推播簡訊息（代號 + 最新/昨收/漲跌幅），也可自行修改為你的訊號邏輯
- 使用 LINE Messaging API (非 LINE Notify)
  需要環境變數：
    LINE_CHANNEL_ACCESS_TOKEN  → Channel access token (long-lived)
    LINE_TO                    → userId / groupId / roomId
- 台灣盤中時段：09:00–13:30（可設 ALLOW_OUTSIDE_WINDOW=True 跳過限制）
"""

import os
import sys
import json
import time
import math
from datetime import datetime, time as dtime

import pytz
import requests
import yfinance as yf
import pandas as pd

# ============ 使用者設定 ============

# 代碼清單：預設從環境變數 TW_CODES（逗號分隔，如 "2330,2603,8446"），
# 若未設則用下方 DEFAULT_CODES。
DEFAULT_CODES = ["2330.TW", "2344", "2408", "2421", "3017", "3206", "3231", "3324", "3515", "3661", "6230", "6415"]
TW_CODES = [c.strip() for c in os.getenv("TW_CODES", "").split(",") if c.strip()] or DEFAULT_CODES

# 每批處理數量（避免一次抓太多被節流）
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "20"))

# 盤中時段（台灣時間）
TZ_TAIPEI = pytz.timezone("Asia/Taipei")
MARKET_START = dtime(9, 0, 0)    # 09:00
MARKET_END   = dtime(13, 30, 0)  # 13:30
ALLOW_OUTSIDE_WINDOW = os.getenv("ALLOW_OUTSIDE_WINDOW", "False").lower() == "true"

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_TO = os.getenv("LINE_TO", "").strip()
LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"

# 快取已解析出的 .TW/.TWO，避免每次都查兩次
TW_SYMBOL_CACHE_FILE = os.getenv("TW_SYMBOL_CACHE", "tw_symbol_cache.json")


# ============ 工具函式 ============

def now_taipei():
    return datetime.now(TZ_TAIPEI)


def within_tw_session(now=None):
    if now is None:
        now = now_taipei()
    t = now.time()
    return MARKET_START <= t <= MARKET_END


def send_line_text(message: str) -> bool:
    """用 LINE Messaging API 推播文字。"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_TO:
        print("[WARN] LINE Messaging API 環境變數未設定（LINE_CHANNEL_ACCESS_TOKEN / LINE_TO）。")
        return False
    headers = {
        "Authorization": "Bearer " + LINE_CHANNEL_ACCESS_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {"to": LINE_TO, "messages": [{"type": "text", "text": message}]}
    try:
        resp = requests.post(LINE_PUSH_API, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            print("[INFO] LINE 推播成功。")
            return True
        print(f"[ERROR] LINE 推播失敗 {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[ERROR] LINE 推播錯誤: {e}")
        return False


def _load_cache():
    try:
        with open(TW_SYMBOL_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict):
    try:
        tmp = TW_SYMBOL_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, TW_SYMBOL_CACHE_FILE)
    except Exception:
        pass


def resolve_tw_symbol(code: str) -> str | None:
    """
    自動決定該代碼要用 .TW (上市) 還是 .TWO (上櫃)。
    請傳「純數字代碼」，不要自行加尾碼。
    """
    if "." in code:
        code = code.split(".")[0]

    cache = _load_cache()
    if code in cache:
        return cache[code]

    for suf in (".TW", ".TWO"):
        sym = f"{code}{suf}"
        try:
            df = yf.Ticker(sym).history(period="10d", interval="1d", prepost=False, actions=False)
            if not df.empty and df["Close"].dropna().shape[0] >= 1:
                cache[code] = sym
                _save_cache(cache)
                return sym
        except Exception:
            pass
        time.sleep(0.3)
    return None


def _retry_history(tkr: yf.Ticker, period: str, interval: str, tries=3, delay=0.6) -> pd.DataFrame:
    last_err = None
    for _ in range(tries):
        try:
            df = tkr.history(period=period, interval=interval, prepost=False, actions=False)
            if not df.empty:
                return df
        except Exception as e:
            last_err = e
        time.sleep(delay)
    if last_err:
        print(f"[WARN] history({period},{interval}) failed: {last_err}")
    return pd.DataFrame()


def get_latest_and_prevclose(code: str):
    """
    以 yfinance 取得：
      latest → 1 分鐘線最後一筆
      prev_close → 日線倒數第二筆（只有一筆時取那筆）
    回傳: (latest, prev_close, resolved_symbol)
    """
    sym = resolve_tw_symbol(code)
    if not sym:
        return None, None, None

    tkr = yf.Ticker(sym)

    # 最新價（含今日）
    h1m = _retry_history(tkr, period="7d", interval="1m")
    latest = float(h1m["Close"].dropna().iloc[-1]) if not h1m.empty else None

    # 昨收（或只有一筆時當昨收）
    h1d = _retry_history(tkr, period="10d", interval="1d")
    prev = None
    if not h1d.empty:
        c = h1d["Close"].dropna()
        if len(c) >= 2:
            prev = float(c.iloc[-2])
        elif len(c) == 1:
            prev = float(c.iloc[-1])

    return latest, prev, sym


def fmt(x, nd=2):
    try:
        return f"{float(x):,.{nd}f}"
    except Exception:
        return "-"


def pct(a, b):
    try:
        if a is None or b is None or math.isnan(float(a)) or math.isnan(float(b)) or b == 0:
            return float("nan")
        return 100.0 * (float(a) - float(b)) / float(b)
    except Exception:
        return float("nan")


# ============ 主流程 ============

def build_rows(codes):
    rows = []
    for code in codes:
        latest, prev, sym = get_latest_and_prevclose(code)
        chg = latest - prev if (latest is not None and prev is not None) else None
        chg_pct = pct(latest, prev) if (latest is not None and prev is not None) else float("nan")
        rows.append({
            "code": code,
            "symbol": sym,
            "latest": latest,
            "prev_close": prev,
            "change": chg,
            "change_pct": chg_pct,
        })
        time.sleep(0.2)  # 降低節流
    return rows


def format_messages(rows, run_dt=None):
    if run_dt is None:
        run_dt = now_taipei()
    ts = run_dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    header = f"【台股快訊】{ts}"
    lines = [header]
    for r in rows:
        code = r["code"]
        latest = r["latest"]
        prev = r["prev_close"]
        chg = r["change"]
        cp = r["change_pct"]
        latest_s = fmt(latest)
        prev_s = fmt(prev)
        if chg is None or pd.isna(chg):
            lines.append(f"{code}: {latest_s}（昨收 {prev_s}）")
        else:
            sign = "+" if chg >= 0 else ""
            cp_s = f"{cp:.2f}%" if not pd.isna(cp) else "-"
            lines.append(f"{code}: {latest_s} / 昨收 {prev_s}  ({sign}{fmt(chg)}, {cp_s})")

    msg = "\n".join(lines)
    if len(msg) <= 900:
        return [msg]

    out = []
    buf = header
    for line in lines[1:]:
        if len(buf) + 1 + len(line) > 900:
            out.append(buf)
            buf = header + "\n" + line
        else:
            buf += "\n" + line
    if buf:
        out.append(buf)
    return out


def main():
    now = now_taipei()
    if not within_tw_session(now) and not ALLOW_OUTSIDE_WINDOW:
        print(f"[INFO] 現在 {now.strftime('%Y-%m-%d %H:%M:%S %Z')} 非台股盤中（09:00–13:30），結束。")
        return

    codes = list(TW_CODES)
    messages = []
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i+BATCH_SIZE]
        rows = build_rows(batch)
        messages.extend(format_messages(rows, run_dt=now))

    for m in messages:
        print(m)
        send_line_text(m)
        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("[FATAL]", e)
        sys.exit(1)
