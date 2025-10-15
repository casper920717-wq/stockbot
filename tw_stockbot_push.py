# tw_stockbot_push.py
# 需求 (先裝一次):
#   python3 -m pip install --upgrade requests yfinance pandas certifi

import os, json, time, math
import requests, certifi, urllib3
import pandas as pd
import yfinance as yf
from datetime import datetime

# 關閉因 verify=False 觸發的警告（僅在 SSL 回退時使用，正常情況不會用到）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============ ① 監測清單（台股） ============
# 可用 "2330" 或 "2330.TW" / 櫃買 "5483" 或 "5483.TWO"
TICKERS = ["2330.TW", "2303.TW", "2412.TW"]

# ============ ② LINE Messaging API ============
ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")   # 你先前已設定好的環境變數
PUSH_MODE = "broadcast"                         # "broadcast" 或 "push"
LINE_USER_ID = os.getenv("LINE_USER_ID")        # 若用 push 模式需要

def line_send(text: str):
    if not ACCESS_TOKEN:
        print("[WARN] 未設定 LINE_ACCESS_TOKEN，以下訊息僅印出：\n", text)
        return
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ACCESS_TOKEN}"}
    payload = {"messages": [{"type": "text", "text": text[:4900]}]}  # 文字上限保守 4900
    if PUSH_MODE == "broadcast":
        url = "https://api.line.me/v2/bot/message/broadcast"
        res = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
    else:
        if not LINE_USER_ID:
            print("[WARN] PUSH 模式但未設定 LINE_USER_ID，訊息僅印出：\n", text); return
        url = "https://api.line.me/v2/bot/message/push"
        body = {"to": LINE_USER_ID, **payload}
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
    if res.status_code != 200:
        print("[LINE 推播失敗]", res.status_code, res.text)

# ============ ③ 參數 ============
PERIOD   = "6mo"   # 用來計算 MA 的歷史區間
INTERVAL = "1d"
# 觸發設定
ENABLE_ALERT = True        # 是否啟用 MA20 警報
TOUCH_TOL = 0.005          # 「接近 MA20」容忍度 0.5%（可調）
ALERT_HEADER = "📣 STOCKBOT 警報"

# ============ ④ 工具 ============
def tw_code_of(tk: str) -> str:
    return "".join(ch for ch in tk if ch.isdigit())

def to_float(x):
    try:
        if x is None: return None
        if isinstance(x, str) and x.strip() in {"", "-", "NaN"}: return None
        if isinstance(x, pd.Series):
            x = x.iloc[0]
        v = float(x)
        if math.isfinite(v): return v
    except Exception:
        pass
    return None

# ============ ⑤ TWSE 即時報價（含 SSL 自動回退） ============
def fetch_twse_quotes(codes):
    def call_api(ex, codes_):
        if not codes_: return {}
        ch = "|".join([f"{ex}_{c}.tw" for c in codes_])
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ch}&_={int(time.time()*1000)}"
        headers = {"Referer": "https://mis.twse.com.tw/stock/index.jsp", "User-Agent": "Mozilla/5.0"}
        try:
            r = requests.get(url, headers=headers, timeout=10, verify=certifi.where())
            r.raise_for_status()
        except requests.exceptions.SSLError as e:
            print("[WARN] SSL 憑證驗證失敗，改以不驗證方式重試一次：", e)
            r = requests.get(url, headers=headers, timeout=10, verify=False)
            r.raise_for_status()
        data = r.json()
        return {it.get("c"): it for it in data.get("msgArray", [])}
    tse_res = call_api("tse", codes)
    missing = [c for c in codes if c not in tse_res]
    otc_res = call_api("otc", missing) if missing else {}
    return {**tse_res, **otc_res}

# ============ ⑥ 主流程 ============
def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    tw_codes = [tw_code_of(tk) for tk in TICKERS]
    tw_quotes = fetch_twse_quotes(tw_codes) if tw_codes else {}

    lines = [f"📊 STOCKBOT 摘要（{date_str}）"]
    alerts = []

    for tk in TICKERS:
        code = tw_code_of(tk)
        yahoo_symbol = tk if (tk.endswith(".TW") or tk.endswith(".TWO")) else f"{code}.TW"

        # 歷史（日線）供昨收與 MA 計算
        df = yf.download(yahoo_symbol, period=PERIOD, interval=INTERVAL, auto_adjust=True, progress=False)
        if df is None or df.empty or "Close" not in df:
            lines.append(f"{code} 取得資料失敗")
            continue

        close = df["Close"]
        df["MA10"] = close.rolling(10).mean()
        df["MA20"] = close.rolling(20).mean()

        y_close = to_float(close.iloc[-1])
        ma10    = to_float(df["MA10"].iloc[-1])
        ma20    = to_float(df["MA20"].iloc[-1])
        # 取前一筆供穿越判斷
        prev_close = to_float(close.iloc[-2]) if len(close) >= 2 else None
        prev_ma20  = to_float(df["MA20"].iloc[-2]) if len(close) >= 2 else None

        q = tw_quotes.get(code, {})
        rt_price = to_float(q.get("z"))
        rt_time  = q.get("t") or "-"
        name     = q.get("n") or "—"
        market   = q.get("ex") or "tse/otc"

        today_price = rt_price if rt_price is not None else y_close
        source_tag  = "TWSE 即時" if rt_price is not None else "昨收(回退)"

        def fmt(x): return f"{x:.2f}" if isinstance(x, (int,float)) and x is not None else "—"

        # 摘要列
        lines.append(
            f"{code} {name}｜昨收 {fmt(y_close)}｜今價 {fmt(today_price)}（{source_tag} {rt_time}）｜MA10 {fmt(ma10)}｜MA20 {fmt(ma20)}"
        )

        # ===== 警報（可依需求調整/關閉）=====
        if ENABLE_ALERT and today_price is not None and ma20 is not None:
            touched = abs(today_price - ma20) / ma20 <= TOUCH_TOL
            crossed_up = (prev_close is not None and prev_ma20 is not None and
                          prev_close < prev_ma20 and today_price > ma20)
            crossed_down = (prev_close is not None and prev_ma20 is not None and
                            prev_close > prev_ma20 and today_price < ma20)

            if crossed_up:
                alerts.append(f"🔼 {code} {name}\n今價 {fmt(today_price)} 上穿 MA20 {fmt(ma20)}")
            elif crossed_down:
                alerts.append(f"🔽 {code} {name}\n今價 {fmt(today_price)} 下穿 MA20 {fmt(ma20)}")
            elif touched:
                alerts.append(f"⏸ {code} {name}\n今價 {fmt(today_price)} 接近 MA20 {fmt(ma20)}（±{int(TOUCH_TOL*1000)/10}%）")

    # 推播摘要
    summary_msg = "\n".join(lines)
    line_send(summary_msg)
    print(summary_msg)

    # 推播警報（若有）
    if alerts:
        alert_msg = f"{ALERT_HEADER}\n" + "\n\n".join(alerts)
        line_send(alert_msg)
        print(alert_msg)
    else:
        print("（無警報觸發）")

if __name__ == "__main__":
    main()
