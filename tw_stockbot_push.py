# =========================================
# 📈 TW Stockbot Push.py (Clean Version)
# Author: PT
# Purpose: Taiwan Stock Price Monitor with MA Alerts (MA10 / MA20)
# =========================================

import os, json, requests, datetime as dt
import yfinance as yf
from upstash_redis import Redis
from statistics import mean

# ===== LINE Messaging API =====
def line_send(message: str) -> bool:
    """
    發送訊息到 LINE (Messaging API)
    需要：
      - LINE_CHANNEL_TOKEN
      - LINE_USER_ID 或 LINE_GROUP_ID
    """
    channel_token = os.getenv("LINE_CHANNEL_TOKEN")
    target_id = os.getenv("LINE_USER_ID") or os.getenv("LINE_GROUP_ID")

    if not channel_token:
        print("⚠️ 缺少 LINE_CHANNEL_TOKEN")
        return False
    if not target_id:
        print("⚠️ 缺少 LINE_USER_ID / LINE_GROUP_ID")
        return False

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {channel_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "to": target_id,
        "messages": [{"type": "text", "text": message}]
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        if resp.status_code == 200:
            print("✅ LINE 推播成功")
            return True
        else:
            print(f"⚠️ LINE API 回應 {resp.status_code}：{resp.text[:150]}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"🛑 推播例外：{e}")
        return False


# ===== 測試模式 (Render Environment: TEST_LINE=1) =====
if os.getenv("TEST_LINE") == "1":
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"🔔 LINE 測試訊息（Render）{now}"
    line_send(msg)
    raise SystemExit(0)


# ===== Redis 去重通知設定 =====
def dedup_check_and_set(redis_client, key: str, expire: int = 3600) -> bool:
    """
    檢查此 key 是否已存在，若不存在則建立並回傳 True。
    用於避免重複發送相同警報。
    """
    try:
        if redis_client.get(key):
            return False
        redis_client.set(key, "1", ex=expire)
        return True
    except Exception as e:
        print(f"⚠️ Redis 無法使用：{e}")
        return True


# ===== 主程式參數設定 =====
CODES = ["2330", "3017", "3661", "3324", "2421", "6230", "6415"]
TOUCH_TOL = 0.005  # 觸碰容差 ±0.5%

redis_url = os.getenv("UPSTASH_REDIS_REST_URL")
redis_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
redis_client = None
if redis_url and redis_token:
    try:
        redis_client = Redis(url=redis_url, token=redis_token)
    except Exception as e:
        print(f"⚠️ Redis 初始化失敗：{e}")


# ===== 主流程 =====
def fmt2(x): return f"{x:.2f}" if x is not None else "--"

lines, alerts = [], []

for code in CODES:
    try:
        # ---- 抓取即時價 (TWSE) ----
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw"
        res = requests.get(url, timeout=8)
        js = res.json()
        data = js.get("msgArray", [{}])[0]
        name = data.get("n", "")
        price = float(data["z"]) if data.get("z") not in ("-", "0", None) else None
        prev_close = float(data["y"]) if data.get("y") not in ("-", None) else None

    except Exception:
        # ---- 若 TWSE 失敗改用 yfinance ----
        ticker = f"{code}.TW"
        yfdata = yf.download(ticker, period="5d", interval="1d", progress=False)
        if yfdata.empty:
            print(f"⚠️ 無法取得 {code} 資料")
            continue
        name = code
        price = float(yfdata["Close"].iloc[-1])
        prev_close = float(yfdata["Close"].iloc[-2])

    # ---- 計算 MA10, MA20 ----
    try:
        ticker = f"{code}.TW"
        hist = yf.download(ticker, period="30d", interval="1d", progress=False)
        ma10 = mean(hist["Close"].tail(10))
        ma20 = mean(hist["Close"].tail(20))
        prev_ma20 = mean(hist["Close"].tail(21).head(20))
    except Exception:
        ma10 = ma20 = prev_ma20 = None

    # ---- 計算漲跌幅 ----
    chg_txt = ""
    if price and prev_close:
        chg = (price - prev_close) / prev_close * 100
        symbol = "🔺" if chg > 0 else ("🔻" if chg < 0 else "⏺")
        chg_txt = f"{symbol}{chg:.2f}%"

    # ---- 事件判定 ----
    event = None
    if ma20 and price and abs(price - ma20) / ma20 <= TOUCH_TOL:
        event = "touch20"
    elif prev_close and prev_ma20 and prev_close < prev_ma20 and price > ma20:
        event = "crossup20"
    elif prev_close and prev_ma20 and prev_close > prev_ma20 and price < ma20:
        event = "crossdown20"
    elif ma10 and price and abs(price - ma10) / ma10 <= TOUCH_TOL:
        event = "touch10"

    # ---- 防重複發送 ----
    if event:
        key = f"{code}:{event}:{dt.date.today()}"
        if dedup_check_and_set(redis_client, key):
            if event == "crossup20":
                alerts.append(f"⬆️ {name}（{code}）突破 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
            elif event == "crossdown20":
                alerts.append(f"⬇️ {name}（{code}）跌破 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
            elif event == "touch20":
                alerts.append(f"📍 {name}（{code}）接近 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
            elif event == "touch10":
                alerts.append(f"📍 {name}（{code}）接近 MA10｜今價 {fmt2(price)}｜MA10 {fmt2(ma10)}")

    # ---- 彙整顯示 ----
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
