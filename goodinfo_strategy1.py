import sys, io, os, time, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import pandas as pd
import urllib3
from datetime import datetime, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 策略條件 ──────────────────────────────────────────
PRICE_MAX      = 300        # 條件2：收盤價 <= 300
VOL_RATIO_MIN  = 2.0        # 條件1：今日量 / 昨日量 >= 2
INST_RATIO_MIN = 30.0       # 條件4：法人成交佔比 >= 30%
# 條件3：本益比 > 0（EPS 正值）
PE_MAX         = 30.0       # 條件6：本益比 <= 30
DAILY_DROP_MIN = -7.0       # 條件7：漲跌幅 > -7%
# 條件5：排除產業關鍵字
EXCLUDE_INDUSTRIES = ["建材營造", "不動產", "建設", "土地", "租賃", "建築", "金融保險", "運動休閒"]
# ──────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0"}

def twse_get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=15, verify=False)
    r.raise_for_status()
    return r.json()

def prev_date(date_str, skip=1):
    d = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=skip)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

def get_prev_trading_days(from_date_str, count=25):
    d = datetime.strptime(from_date_str, "%Y%m%d")
    days = []
    while len(days) < count:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
    return days

def fetch_ma20(today, codes, log=print):
    trade_dates = get_prev_trading_days(today, 25)
    price_history = {}
    log(f"抓取 MA20 歷史資料（{trade_dates[-1]} ~ {trade_dates[0]}，共 {len(trade_dates)} 日）...")
    for i, date in enumerate(reversed(trade_dates)):
        df_day = fetch_daily(date)
        if df_day.empty:
            time.sleep(1)
            continue
        for _, row in df_day.iterrows():
            code = row["證券代號"]
            if code not in codes:
                continue
            price = clean(row["收盤價"])
            if price:
                price_history.setdefault(code, []).append(price)
        time.sleep(1)
    ma20_map = {}
    for code, prices in price_history.items():
        if len(prices) >= 20:
            ma20_map[code] = round(sum(prices[-20:]) / 20, 2)
    return ma20_map

def get_latest_quarter():
    """回傳（今年季度, 去年同季度）的 (西元年, 季, ROC年) tuple"""
    now = datetime.now()
    y, m = now.year, now.month
    if m >= 11:
        q = 3
    elif m >= 8:
        q = 2
    elif m >= 5:
        q = 1
    else:
        y -= 1
        q = 4
    return y, q, y - 1911, y - 1, y - 1912

def fetch_eps(code, roc_year, season):
    """從 MOPS 取得單一公司特定季度的基本每股盈餘"""
    from bs4 import BeautifulSoup
    try:
        r = requests.post(
            "https://mops.twse.com.tw/mops/web/ajax_t163sb04",
            data={"step": "1", "firstin": "1", "off": "1",
                  "co_id": code, "year": str(roc_year), "season": f"{season:02d}"},
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15, verify=False
        )
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "lxml")
        for row in soup.find_all("tr"):
            tds = row.find_all("td")
            if not tds:
                continue
            label = tds[0].get_text(strip=True)
            if "基本每股盈餘" in label:
                for td in tds[1:]:
                    txt = td.get_text(strip=True).replace(",", "")
                    if txt and txt not in ("--", "-", ""):
                        try:
                            return float(txt)
                        except ValueError:
                            pass
    except Exception:
        pass
    return None

def fetch_vol_max_4days(today):
    """取得今日前3個交易日各股最大成交量，用於判斷4日新高"""
    trade_dates = get_prev_trading_days(today, 3)
    max_vol = {}
    for date in trade_dates:
        df = fetch_daily_raw(date)
        if df.empty:
            time.sleep(1)
            continue
        for _, row in df.iterrows():
            code = row["證券代號"]
            vol = clean(row["成交股數"])
            if vol and vol > max_vol.get(code, 0):
                max_vol[code] = vol
        time.sleep(1)
    return max_vol

def fetch_daily_raw(date_str):
    data = twse_get(
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        {"response": "json", "date": date_str, "type": "ALLBUT0999"}
    )
    for block in data.get("tables", []):
        fields = block.get("fields", [])
        if "證券代號" in fields and "成交股數" in fields:
            return pd.DataFrame(block["data"], columns=fields)
    return pd.DataFrame()

def fetch_daily(date_str):
    return fetch_daily_raw(date_str)

def fetch_industry():
    """從 TWSE ISIN 系統取得上市股票的產業分類"""
    r = requests.get(
        "https://isin.twse.com.tw/isin/C_public.jsp",
        params={"strMode": "2"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15, verify=False
    )
    r.encoding = "ms950"
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "lxml")
    rows = soup.find_all("tr")
    data = []
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 5:
            code_name = cols[0].get_text(strip=True)
            industry  = cols[4].get_text(strip=True)
            if "　" in code_name:
                code = code_name.split("　")[0].strip()
                if len(code) == 4 and code.isdigit():
                    data.append({"證券代號": code, "產業": industry})
    return pd.DataFrame(data)

def fetch_inst(date_str):
    """回傳三大法人各類買賣股數（T86），去重保留每支股票一筆"""
    data = twse_get(
        "https://www.twse.com.tw/rwd/zh/fund/T86",
        {"response": "json", "date": date_str, "selectType": "ALL"}
    )
    fields = data.get("fields", [])
    rows   = data.get("data", [])
    if not fields or not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=fields)
    df = df[df["證券代號"].str.match(r"^\d{4}$", na=False)]
    df = df.drop_duplicates(subset="證券代號", keep="first")
    return df

def clean(s):
    try:
        return float(str(s).replace(",", "").replace("+", "").replace("--", "").strip())
    except Exception:
        return None

def run(target_date=None, quiet=False):
    log = (lambda *a, **kw: None) if quiet else print
    today = target_date if target_date else datetime.now().strftime("%Y%m%d")

    log(f"抓取今日（{today}）交易資料...")
    df_today = fetch_daily(today)
    if df_today.empty:
        today = prev_date(today, 1)
        log(f"  今日無資料，改用 {today}")
        df_today = fetch_daily(today)
    log(f"  → {len(df_today)} 筆")

    for skip in range(1, 6):
        yest = prev_date(today, skip)
        log(f"抓取前一交易日（{yest}）資料...")
        df_prev = fetch_daily(yest)
        if not df_prev.empty:
            break
    log(f"  → {len(df_prev)} 筆")

    log("抓取三大法人資料...")
    df_inst = fetch_inst(today)
    log(f"  → {len(df_inst)} 筆")
    if df_inst.empty:
        log("  ⚠ 法人資料尚未公布，結束執行。")
        return pd.DataFrame(), today

    log("抓取產業分類資料...")
    df_ind = fetch_industry()
    log(f"  → {len(df_ind)} 筆")

    log("抓取前3個交易日成交量（判斷4日新高）...")
    vol_max_4days = fetch_vol_max_4days(today)
    log(f"  → 完成")

    df = df_today[["證券代號", "證券名稱", "成交股數", "收盤價", "本益比"]].copy()
    df["收盤_n"] = df["收盤價"].apply(clean)
    df["今量_n"] = df["成交股數"].apply(clean)
    df["pe_n"]   = df["本益比"].apply(clean)

    df_prev_vol = df_prev[["證券代號", "成交股數", "收盤價"]].copy()
    df_prev_vol.columns = ["證券代號", "昨量", "昨收"]
    df_prev_vol["昨量_n"] = df_prev_vol["昨量"].apply(clean)
    df_prev_vol["昨收_n"] = df_prev_vol["昨收"].apply(clean)
    df = df.merge(df_prev_vol[["證券代號", "昨量_n", "昨收_n"]], on="證券代號", how="left")

    def col(name): return df_inst[name].apply(clean)

    df_inst["外資_量"] = col("外陸資買進股數(不含外資自營商)") + col("外陸資賣出股數(不含外資自營商)") \
                       + col("外資自營商買進股數") + col("外資自營商賣出股數")
    df_inst["外資_超"] = col("外陸資買賣超股數(不含外資自營商)") + col("外資自營商買賣超股數")

    df_inst["投信_量"] = col("投信買進股數") + col("投信賣出股數")
    df_inst["投信_超"] = col("投信買賣超股數")

    df_inst["自營_量"] = col("自營商買進股數(自行買賣)") + col("自營商賣出股數(自行買賣)") \
                       + col("自營商買進股數(避險)") + col("自營商賣出股數(避險)")
    df_inst["自營_超"] = col("自營商買賣超股數")

    df_inst["法人量"] = df_inst["外資_量"] + df_inst["投信_量"] + df_inst["自營_量"]

    inst_cols = ["證券代號", "法人量", "外資_量", "外資_超", "投信_量", "投信_超", "自營_量", "自營_超"]
    df = df.merge(df_inst[inst_cols], on="證券代號", how="left")

    df = df.merge(df_ind, on="證券代號", how="left")
    df["產業"] = df["產業"].fillna("")

    df["5日前高量"] = df["證券代號"].map(vol_max_4days)
    df["量比"]    = df["今量_n"] / df["昨量_n"].replace(0, float("nan"))
    df["漲跌幅"]  = (df["收盤_n"] - df["昨收_n"]) / df["昨收_n"] * 100
    df["法人%"]   = df["法人量"] / df["今量_n"] * 100
    df["外資%"]   = df["外資_量"] / df["今量_n"] * 100
    df["投信%"]   = df["投信_量"] / df["今量_n"] * 100
    df["自營%"]   = df["自營_量"] / df["今量_n"] * 100
    df["外資超(張)"] = (df["外資_超"] / 1000).round(0)
    df["投信超(張)"] = (df["投信_超"] / 1000).round(0)
    df["自營超(張)"] = (df["自營_超"] / 1000).round(0)

    exclude_mask = df["產業"].apply(
        lambda ind: any(kw in ind for kw in EXCLUDE_INDUSTRIES)
    )

    mask = (
        (df["量比"]   >= VOL_RATIO_MIN)    &
        (df["收盤_n"] <= PRICE_MAX)         &
        (df["pe_n"]   >  0)                &
        (df["法人%"]  >= INST_RATIO_MIN)    &
        (~exclude_mask)                     &
        (df["pe_n"]   <= PE_MAX)            &
        (df["漲跌幅"]  >  DAILY_DROP_MIN)   &
        (df["今量_n"] >= df["5日前高量"])
    )
    result = df[mask].copy().sort_values("量比", ascending=False)

    if not result.empty:
        cur_yr, cur_q, roc_cur, prev_yr, roc_prev = get_latest_quarter()
        log(f"查詢 EPS YoY（{cur_yr}Q{cur_q} vs {prev_yr}Q{cur_q}）...")
        def eps_yoy_pass(code):
            eps_cur  = fetch_eps(code, roc_cur,  cur_q)
            time.sleep(0.3)
            eps_prev = fetch_eps(code, roc_prev, cur_q)
            time.sleep(0.3)
            if eps_cur is None or eps_prev is None:
                return True  # 無法取得資料時保留
            return eps_cur >= eps_prev
        before = len(result)
        result = result[result["證券代號"].apply(eps_yoy_pass)].copy()
        log(f"  EPS YoY 篩選：{before} → {len(result)} 檔（剔除 {before - len(result)} 檔）")

    if not result.empty:
        log("計算 20日移動均線（MA20）...")
        codes = set(result["證券代號"].tolist())
        ma20_map = fetch_ma20(today, codes, log)
        result["MA20"] = result["證券代號"].map(ma20_map)
        before = len(result)
        result = result[result["收盤_n"] >= result["MA20"].fillna(float("-inf"))]
        result = result.sort_values("量比", ascending=False)
        log(f"  MA20 篩選：{before} → {len(result)} 檔（剔除 {before - len(result)} 檔）")

    log(f"\n{'='*55}")
    log(f"策略1 選股結果（{today}）：共 {len(result)} 檔")
    log(f"EPS 判斷：本益比 > 0，且最近一季 EPS ≥ 去年同期")
    log(f"成交量條件：4日內新高")
    log(f"{'='*55}")

    if not result.empty:
        out = result[[
            "證券代號", "證券名稱", "產業", "收盤_n", "MA20", "量比", "pe_n",
            "法人%", "外資%", "外資超(張)", "投信%", "投信超(張)", "自營%", "自營超(張)"
        ]].copy()
        out.columns = [
            "代號", "名稱", "產業", "收盤價", "MA20", "量比(今/昨)", "本益比",
            "法人合計%", "外資%", "外資超(張)", "投信%", "投信超(張)", "自營%", "自營超(張)"
        ]
        pct_cols = ["法人合計%", "外資%", "投信%", "自營%"]
        for c in pct_cols:
            out[c] = out[c].map(lambda x: f"{x:.1f}%" if pd.notna(x) else "-")
        out["量比(今/昨)"] = out["量比(今/昨)"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
        for c in ["外資超(張)", "投信超(張)", "自營超(張)"]:
            out[c] = out[c].map(lambda x: f"{x:+.0f}" if pd.notna(x) else "-")
        log(out.to_string(index=False))

        if not quiet:
            base_dir  = r"D:\AI agent"
            fname     = os.path.join(base_dir, f"策略1_選股_{today}.xlsx")
            fname_csv = os.path.join(base_dir, f"策略1_選股_{today}.csv")
            try:
                out.to_excel(fname, index=False)
                log(f"\n已儲存：{fname}")
            except PermissionError:
                log(f"\n⚠ Excel 檔案已開啟，請關閉後重試：{fname}")
            out.to_csv(fname_csv, index=False, encoding="utf-8-sig")
            log(f"已儲存：{fname_csv}")
    else:
        log("無")

    return result, today

if __name__ == "__main__":
    run()
