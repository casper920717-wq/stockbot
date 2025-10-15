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
    """優先取 TWSE 即時價；盤後或取不到 z 時回退昨收 y。"""
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw"
    headers = {
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    r = requests.get(url, headers=headers, timeout=12)
    js = r.json()

    # 取第一筆
    d = (js.get("msgArray") or [{}])[0]
    name = d.get("n", code)

    def _f(v):
        try:
            return float(v)
        except Exception:
            return None

    z = d.get("z")      # 最新成交價（盤後常為 "-"）
    pz = d.get("pz")    # 前一筆成交價（有時可用）
    y = d.get("y")      # 昨收

    price = _f(z) or _f(pz) or None
    prev_close = _f(y)

    # 盤後或取不到 z/pz：改用昨收，避免整支股票被跳過
    if price is None and prev_close is not None:
        price = prev_close

    if price is None and prev_close is None:
        raise RuntimeError(f"TWSE 無法取得 {code} 成交價/昨收")

    return name, price, prev_close


def fetch_yf_last_two(code: str):
    t = f"{code}.TW"
    df = yf.download(t, period="5d", interval="1d", progress=False)
    if df.empty or "Close" not in df:
        raise RuntimeError("yfinance 無資料")

    closes = []
    for v in df["Close"].tolist():
        try:
            closes.append(float(v))
        except (TypeError, ValueError):
            pass

    if len(closes) < 2:
        raise RuntimeError("yfinance 無足夠收盤價")

    return code, closes[-1], closes[-2]


def calc_ma10_ma20(code: str):
    t = f"{code}.TW"
    df = yf.download(t, period="40d", interval="1d", progress=False)
    if df.empty or "Close" not in df:
        return None, None, None

    # 轉成 float 並去掉 NaN
    closes = []
    for v in df["Close"].tolist():
        try:
            closes.append(float(v))
        except (TypeError, ValueError):
            pass

    if len(closes) < 21:
        return None, None, None

    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    prev_ma20 = sum(closes[-21:-1]) / 20  # 昨日的 MA20
    return ma10, ma20, prev_ma20

# ===== 開盤時段防呆（台灣時間）=====
from datetime import datetime, time
from zoneinfo import ZoneInfo  # 標準庫，Python 3.9+

_TZ = ZoneInfo("Asia/Taipei")

def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(_TZ)
    # 週一=0 … 週日=6；週末關閉
    if now.weekday() >= 5:
        return False
    t = now.time()
    # 台股一般盤：09:00–13:30
    return time(9, 0) <= t <= time(13, 30)

_now = datetime.now(_TZ)
if not is_market_open(_now):
    print(f"⏰ 非開盤時間，程式結束（{_now:%Y-%m-%d %H:%M:%S %Z}）")
    import sys
    sys.exit(0)
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
# 只做 MA20 的「昨日→今日」穿越判定（兩種方向）
    if (prev_close is not None and prev_ma20 is not None
    and price is not None and ma20 is not None):
        if prev_close < prev_ma20 and price > ma20:
            event = "crossup20"     # 昨日低於、今日高於
        elif prev_close > prev_ma20 and price < ma20:
            event = "crossdown20"   # 昨日高於、今日低於


    # --- 警報訊息（不做跨執行去重；有需要再說）---
    if event:
        if event == "crossup20":
            alerts.append(f"⬆️ {name}（{code}）突破 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
        elif event == "crossdown20":
            alerts.append(f"⬇️ {name}（{code}）突破 MA20｜今價 {fmt2(price)}｜MA20 {fmt2(ma20)}")
        elif event == "touch10":
            alerts.append(f"📍 {name}（{code}）接近 MA10｜今價 {fmt2(price)}｜MA10 {fmt2(ma10)}")

    # --- 列表輸出（先用直排；之後再做對齊版）---
    lines.append(
    f"{code:<5} {name:<8}｜今價 {fmt2(price):>7}｜{chg_txt:<8}｜MA10 {fmt2(ma10):>7}｜MA20 {fmt2(ma20):>7}"
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
