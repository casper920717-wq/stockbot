# =========================================
# 📈 TW Stockbot Push.py (Clean / No-Redis)
# 只保留：Messaging API 推播、TEST_LINE 測試、抓價 + MA10/MA20 + 事件判定
# 相依：requests, yfinance, statistics（標準庫）, datetime（標準庫）
# =========================================

import os, json, requests, datetime as dt
import yfinance as yf
from statistics import mean

# ===== LINE Messaging API =====
def line_send(message: str) -> bool:
    """
    使用 LINE Messaging API 推送文字訊息。
    需要環境變數：
      - LINE_CHANNEL_TOKEN
      - LINE_USER_ID 或 LINE_GROUP_ID (擇一)
    """
    channel_token = (os.getenv("LINE_CHANNEL_TOKEN") or "").strip()
    target_id = (os.getenv("LINE_USER_ID") or os.getenv("LINE_GROUP_ID") or "").strip()

    if not channel_token:
        print("⚠️ 缺少 LINE_CHANNEL_TOKEN")
        return False
    if not target_id:
        print("⚠️ 請設定 LINE_USER_ID 或 LINE_GROUP_ID")
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {channel_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "to": target_id,
        "messages": [{"type": "text", "text": str(message)}],
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        if resp.status_code == 200:
            print("✅ LINE 推播成功")
            return True
        else:
            print(f"⚠️ Messaging API 回應 {resp.status_code}：{resp.text[:200]}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"🛑 推播例外：{e}")
        return False


# ===== 測試模式 (Render: TEST_LINE=1) =====
if os.getenv("TEST_LINE") == "1":
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"🔔 LINE 測試訊息（Render）{now}"
    ok = line_send(msg)
    if not ok:
        print(msg)
    raise SystemExit(0)


# ===== 參數設定 =====
CODES = ["2330", "3017", "3661", "3324", "2421", "6230", "6415"]
TOUCH_TOL = 0.005  # 觸碰容差 ±0.5%

def fmt2(x):
    try:
        return f"{float(x):.2f}"
    except Exception:
        return "--"

def fetch_twse_quote(code: str):
    """優先取 TWSE 即時價，失敗丟出例外"""
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw"
    r = requests.get(url, timeout=8)
    js = r.json()
    d = (js.get("msgArray") or [{}])[0]
    name = d.get("n", code)
    z = d.get("z")
    y = d.get("y")
    price = float(z) if z not in (None, "-", "0") else None
    prev_close = float(y) if y not in (None, "-") else None
    return name, price, prev_close

def fetch_yf_last_two(code: str):
    """yfinance 備援：取最近兩天收盤"""
    t = f"{code}.TW"
    df = yf.download(t, period="5d", interval="1d", progress=False)
    if df.empty or len(df["Close"]) < 2:
        raise RuntimeError("yfinance 無資料")
    return code, float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])

def calc_ma10_ma20(code: str):
    """用日線收盤估算 MA10/MA20 與昨日 MA20"""
    t = f"{code}.TW"
    df = yf.download(t, period="40d", interval="1d", progress=False)
    if df.empty or len(df["Close"]) < 21:
        return None, None, None
    close = df["Close"]
    ma10 = mean(close.tail(10))
    ma20 = mean(close.tail(20))
    prev_ma20 = mean(close.tail(21).head(20))
    return ma10, ma20, prev_ma20


# ===== 主流程 =====
lines, alerts = [], []

for code in CODES:
    # --- 價格來源：TWSE -> yfinance 備援 ---
    try:
        name, price, prev_close = fetch_twse_quote(code)
    except Exception:
        try:
            name, price, prev_close = fetch_yf_last_two(code)
        except Exception:
            print(f"⚠️ 無法取得 {code} 價格資料")
            continue

    # --- 均線 ---
    ma10, ma20, prev_ma20 = calc_ma10_ma20(code)

    # --- 漲跌幅文字 ---
    chg_txt = ""
    if price is not None and prev_close is not None:
        chg_pct = (price - prev_close) / prev_close * 100
        symbol = "🔺" if chg_pct > 0 else ("🔻" if chg_pct < 0 else "⏺")
        chg_txt = f"{symbol}{chg_pct:.2f}%"

    # --- 事件判定（沿用你現有的觸碰/上穿/下穿）---
    event = None
    if ma20 is not None and price is not None and abs(price - ma20) / ma20 <= TOUCH_TOL:
        event = "touch20"
    elif (prev_close is not None and prev_ma20 is not None
          and prev_close < prev_ma20 and price > ma20):
        event = "crossup20"
    elif (prev_close is not None and prev_ma20 is not None
          and prev_close > prev_ma20 and price < ma20):
        event = "crossdown20"
    elif ma10 is not None and price is not None and abs(price - ma10) / ma10 <= TOUCH_TOL:
        event = "touch10"

    # --- 警報訊息（不做跨執行去重；有需要再說）---
    if event:
        if event == "crossup20":
            alerts.append(f"⬆️ {name}（{code}）突破 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
        elif event == "crossdown20":
            alerts.append(f"⬇️ {name}（{code}）跌破 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
        elif event == "touch20":
            alerts.append(f"📍 {name}（{code}）接近 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
        elif event == "touch10":
            alerts.append(f"📍 {name}（{code}）接近 MA10｜今價 {fmt2(price)}｜MA10 {fmt2(ma10)}")

    # --- 列表輸出（先用直排；之後再做對齊版）---
    lines.append(
        f"{code} {name}｜今價 {fmt2(price)}｜{chg_txt}｜MA10 {fmt2(ma10)}｜MA20 {fmt2(ma20)}"
    )

# ===== 推播輸出 =====
summary = "📊 今日股價監控\n" + "\n".join(lines)
sent_summary = line_send(summary)
if not sent_summary:
    print(summary)

if alerts:
    alert_msg = "📣 均線警報\n" + "\n".join(alerts)
    sent_alerts = line_send(alert_msg)
    if not sent_alerts:
        print(alert_msg)
