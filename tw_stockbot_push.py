# tw_stockbot_push.py
# 功能：
# - 每 5 分鐘輪詢（Render Cron）抓台股即時價
# - 若 TWSE 無即時價，備援用 yfinance 1m（延遲 ~10–15 分鐘）
# - 計算 MA10 / MA20；偵測接近/上穿/下穿；用 Upstash Redis 做「當日去重」
# - line_send() 回傳 True/False，避免在 console 重複輸出
# 需要的環境變數（Render → Environment）：
#   LINE_ACCESS_TOKEN
#   UPSTASH_REDIS_REST_URL      （可選；去重用）
#   UPSTASH_REDIS_REST_TOKEN    （可選；去重用）

import requests
try:
    requests.get("https://httpbin.org/ip", timeout=5)
    print("🌍 外網連通 OK")
except Exception as e:
    print("🌍 外網無法連線：", e)




import os, math, time, datetime as dt, requests, pandas as pd, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ======== LINE ========
LINE_ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN")

def line_send(message: str) -> bool:
    import os, time, socket, requests
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter

    token = os.getenv("LINE_NOTIFY_TOKEN")
    if not token:
        print("⚠️ 找不到 LINE Notify Token（環境變數 LINE_NOTIFY_TOKEN）")
        return False

    url = "https://notify-api.line.me/api/notify"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"message": message}

    # 1) 先做 DNS 檢查，方便在 Render Log 快速定位問題
    try:
        dns_ip = socket.gethostbyname("notify-api.line.me")
        print(f"🌐 DNS 解析成功：notify-api.line.me → {dns_ip}")
    except Exception as e:
        print(f"🛑 DNS 解析失敗：{e}")
        # 等 2 秒再試一次（臨時性 DNS 問題很常一兩秒內恢復）
        time.sleep(2)
        try:
            dns_ip = socket.gethostbyname("notify-api.line.me")
            print(f"🌐 二次解析成功：notify-api.line.me → {dns_ip}")
        except Exception as e2:
            print(f"🛑 二次 DNS 仍失敗：{e2}")
            return False

    # 2) 設定 requests Session + Retry（含退避），應對暫時性網路抖動
    session = requests.Session()
    retry = Retry(
        total=3,               # 最多 3 次
        backoff_factor=0.8,    # 0.8, 1.6, 3.2 秒退避
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("POST", "GET"),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    try:
        resp = session.post(url, headers=headers, data=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ LINE Notify 發送成功")
            return True
        else:
            print(f"⚠️ LINE Notify 回應碼：{resp.status_code}，內容：{resp.text[:200]}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"🛑 LINE Notify 發送例外：{e}")
        return False

import os, sys, datetime as dt

# —— 測試模式：只發一則測試訊息就結束 —— 
if os.getenv("TEST_LINE") == "1":
    _msg = f"🔔 LINE 測試訊息（Render）{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    ok = line_send(_msg)
    if not ok:
        print(_msg)
    sys.exit(0)

# ======== Upstash Redis（去重，可選） ========
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

def dedup_check_and_set(key: str, ttl_sec: int = 86400) -> bool:
    """第一次看到 key -> 設定並回傳 True；已存在 -> 回傳 False"""
    if not REDIS_URL or not REDIS_TOKEN:
        return True  # 未設定 Redis 時，不去重
    try:
        r = requests.post(f"{REDIS_URL}/get/{key}",
                          headers={"Authorization": f"Bearer {REDIS_TOKEN}"}, timeout=8)
        if r.ok and (r.json().get("result") is not None):
            return False
        # 設定與過期
        requests.post(f"{REDIS_URL}/set/{key}/{int(time.time())}",
                      headers={"Authorization": f"Bearer {REDIS_TOKEN}"}, timeout=8)
        requests.post(f"{REDIS_URL}/expire/{key}/{ttl_sec}",
                      headers={"Authorization": f"Bearer {REDIS_TOKEN}"}, timeout=8)
        return True
    except Exception as e:
        print("[DEDUP] 失敗，忽略去重：", e)
        return True

# ======== 參數設定 ========
CODES = ["2330", "3017","3661", "3324","2421", "6230","6415"]  # 要監控的台股代碼#奇鋐（3017）#雙鴻（3324）#建準（2421）#超眾（6230）TOUCH_TOL = 0.005
TOUCH_TOL = 0.005                  # ±0.5% 視為接近
PERIOD = "6mo"                     # yfinance 計 MA 用
INTERVAL = "1d"

# ======== 工具 ========
def to_float(x):
    try:
        if x is None: return None
        if isinstance(x, str) and x.strip() in {"", "-", "NaN"}: return None
        if isinstance(x, pd.Series): x = x.iloc[0]
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None

def fmt2(x):
    return f"{x:.2f}" if isinstance(x, (int, float)) and x is not None else "—"

# ======== TWSE 即時價 ========
def fetch_twse_quotes(codes):
    """TWSE 即時價（可能偶爾回 '-' 表示暫無成交）"""
    chs = [f"tse_{c}.tw" for c in codes]  # 上市 tse。若需要上櫃可擴充 otc
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=" + "|".join(chs)
    r = requests.get(url,
                     headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp",
                              "User-Agent": "Mozilla/5.0"},
                     timeout=15, verify=False)
    r.raise_for_status()
    data = r.json().get("msgArray", [])
    return {d.get("c"): d for d in data if "c" in d}

# ======== yfinance：MA 與備援價 ========
def fetch_ma(code):
    """用 yfinance 計算 MA10/MA20，並提供昨收/前一日 MA20（穿越判定用）"""
    import yfinance as yf
    df = yf.download(f"{code}.TW", period=PERIOD, interval=INTERVAL,
                     auto_adjust=True, progress=False)
    if df is None or df.empty or "Close" not in df:
        return None, None, None, (None, None)
    close = df["Close"]
    df["MA10"] = close.rolling(10).mean()
    df["MA20"] = close.rolling(20).mean()
    y_close = to_float(close.iloc[-1])
    ma10 = to_float(df["MA10"].iloc[-1])
    ma20 = to_float(df["MA20"].iloc[-1])
    prev_close = to_float(close.iloc[-2]) if len(close) >= 2 else None
    prev_ma20  = to_float(df["MA20"].iloc[-2]) if len(close) >= 2 else None
    return y_close, ma10, ma20, (prev_close, prev_ma20)

def fallback_price_from_yf_1m(code):
    """TWSE 無價時，用 yfinance 1 分線作為延遲備援（約 10–15 分鐘延遲）"""
    try:
        import yfinance as yf
        df = yf.download(f"{code}.TW", period="1d", interval="1m", progress=False)
        if df is not None and not df.empty and "Close" in df:
            return to_float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"[FALLBACK] yfinance 1m 取得失敗 {code}: {e}")
    return None

# ======== 主程式 ========
def main():
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    quotes = fetch_twse_quotes(CODES)

    lines = [f"📊 台股均線監控（{now_str}）"]
    alerts = []

    for code in CODES:
        q = quotes.get(code, {})
        name = q.get("n") or code

        # 1) 先用 TWSE
        price = to_float(q.get("z"))
        yclose = to_float(q.get("y"))
        source_tag = "TWSE 即時"

        # 2) 若 TWSE 無價，改用 yfinance 1分線（延遲）
        if price is None:
            yf_price = fallback_price_from_yf_1m(code)
            if yf_price is not None:
                price = yf_price
                source_tag = "Yahoo延遲"
            else:
                lines.append(f"⚠️ {name}（{code}）暫無成交價")
                continue

        # 漲跌幅 + 顏色
        chg_pct = ((price - yclose) / yclose) * 100 if (yclose is not None and price is not None) else None
        color = "🔴" if (chg_pct is not None and chg_pct > 0) else ("🟢" if (chg_pct is not None and chg_pct < 0) else "➖")
        chg_txt = f"{color} {chg_pct:+.2f}%" if chg_pct is not None else "—"

        # MA
        y_close, ma10, ma20, prev_pair = fetch_ma(code)
        if ma10 is None or ma20 is None:
            lines.append(f"{code} {name}｜今價 {fmt2(price)}（{source_tag}）｜漲跌 {chg_txt}｜MA10/MA20 無法取得")
            continue
        prev_close, prev_ma20 = prev_pair

        # ===== 事件判定 =====
        event = None
        if ma20 is not None and price is not None and abs(price - ma20) / ma20 <= TOUCH_TOL:
            event = "touch20"
        elif (prev_close is not None and prev_ma20 is not None and
              prev_close < prev_ma20 and price > ma20):
            event = "crossup20"
        elif (prev_close is not None and prev_ma20 is not None and
              prev_close > prev_ma20 and price < ma20):
            event = "crossdown20"
        elif ma10 is not None and price is not None and abs(price - ma10) / ma10 <= TOUCH_TOL:
            event = "touch10"

        # ===== 去重（每日一次）=====
        if event:
            key = f"maalert:{dt.datetime.now():%Y-%m-%d}:{code}:{event}"
            if dedup_check_and_set(key, ttl_sec=60*60*24*2):
                if event == "touch20":
                    alerts.append(f"⚠️ {name}（{code}）接近 MA20｜今價 {fmt2(price)}（{source_tag}）｜MA20 {fmt2(ma20)}")
                elif event == "crossup20":
                    alerts.append(f"🔼 {name}（{code}）上穿 MA20｜今價 {fmt2(price)}（{source_tag}）｜MA20 {fmt2(ma20)}")
                elif event == "crossdown20":
                    alerts.append(f"🔽 {name}（{code}）下穿 MA20｜今價 {fmt2(price)}（{source_tag}）｜MA20 {fmt2(ma20)}")
                elif event == "touch10":
                    alerts.append(f"⚠️ {name}（{code}）接近 MA10｜今價 {fmt2(price)}（{source_tag}）｜MA10 {fmt2(ma10)}")
            else:
                print(f"[SKIP] 去重 {key}")

        if (ma20 is not None) and (price is not None):
            _ma20_status = "🔺高於 MA20" if price > ma20 else ("🔻低於 MA20" if price < ma20 else "等於 MA20")
            lines.append(f"{code} {name}｜今價 {fmt2(price)}｜{chg_txt}｜MA10 {fmt2(ma10)}｜MA20 {fmt2(ma20)}｜當前價位：{_ma20_status}")
        else:
            lines.append(f"{code} {name}｜今價 {fmt2(price)}｜{chg_txt}｜MA10 {fmt2(ma10)}｜MA20 {fmt2(ma20)}")


    # ===== 輸出 / 推播（避免重複）=====
    summary = "\n".join(lines)
    sent_summary = line_send(summary)
    if not sent_summary:
        print(summary)

    if alerts:
        alert_msg = "📣 均線警報\n" + "\n".join(alerts)
        sent_alerts = line_send(alert_msg)
        if not sent_alerts:
            print(alert_msg)

   

if __name__ == "__main__":
    main()
