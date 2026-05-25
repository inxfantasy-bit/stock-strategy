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
EXCLUDE_INDUSTRIES = ["建材營造", "不動產", "建設", "土地", "租賃", "建築", "金融保險"]
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

def fetch_daily(date_str):
    """回傳當日全股票：代號、名稱、成交股數、收盤價、本益比"""
    data = twse_get(
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        {"response": "json", "date": date_str, "type": "ALLBUT0999"}
    )
    for block in data.get("tables", []):
        fields = block.get("fields", [])
        if "證券代號" in fields and "成交股數" in fields:
            return pd.DataFrame(block["data"], columns=fields)
    return pd.DataFrame()

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
            industry  = cols[4].get_text(strip=True)   # 第5欄才是產業別
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
    # 只保留 4 碼數字的股票代號，並去重
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

    # ── 今日資料 ──────────────────────────────────────
    log(f"抓取今日（{today}）交易資料...")
    df_today = fetch_daily(today)
    if df_today.empty:
        today = prev_date(today, 1)
        log(f"  今日無資料，改用 {today}")
        df_today = fetch_daily(today)
    log(f"  → {len(df_today)} 筆")

    # ── 前一交易日資料（量比用）──────────────────────
    for skip in range(1, 6):
        yest = prev_date(today, skip)
        log(f"抓取前一交易日（{yest}）資料...")
        df_prev = fetch_daily(yest)
        if not df_prev.empty:
            break
    log(f"  → {len(df_prev)} 筆")

    # ── 三大法人 ──────────────────────────────────────
    log("抓取三大法人資料...")
    df_inst = fetch_inst(today)
    log(f"  → {len(df_inst)} 筆")

    # ── 產業分類（條件5）────────────────────────────
    log("抓取產業分類資料...")
    df_ind = fetch_industry()
    log(f"  → {len(df_ind)} 筆")

    # ── 整理今日主表 ──────────────────────────────────
    df = df_today[["證券代號", "證券名稱", "成交股數", "收盤價", "本益比"]].copy()
    df["收盤_n"] = df["收盤價"].apply(clean)
    df["今量_n"] = df["成交股數"].apply(clean)
    df["pe_n"]   = df["本益比"].apply(clean)

    # 前日量
    df_prev_vol = df_prev[["證券代號", "成交股數", "收盤價"]].copy()
    df_prev_vol.columns = ["證券代號", "昨量", "昨收"]
    df_prev_vol["昨量_n"] = df_prev_vol["昨量"].apply(clean)
    df_prev_vol["昨收_n"] = df_prev_vol["昨收"].apply(clean)
    df = df.merge(df_prev_vol[["證券代號", "昨量_n", "昨收_n"]], on="證券代號", how="left")

    # ── 拆分三大法人 ──────────────────────────────────
    def col(name): return df_inst[name].apply(clean)

    # 外資（外陸資 + 外資自營商）
    df_inst["外資_量"] = col("外陸資買進股數(不含外資自營商)") + col("外陸資賣出股數(不含外資自營商)") \
                       + col("外資自營商買進股數") + col("外資自營商賣出股數")
    df_inst["外資_超"] = col("外陸資買賣超股數(不含外資自營商)") + col("外資自營商買賣超股數")

    # 投信
    df_inst["投信_量"] = col("投信買進股數") + col("投信賣出股數")
    df_inst["投信_超"] = col("投信買賣超股數")

    # 自營商（自行買賣 + 避險）
    df_inst["自營_量"] = col("自營商買進股數(自行買賣)") + col("自營商賣出股數(自行買賣)") \
                       + col("自營商買進股數(避險)") + col("自營商賣出股數(避險)")
    df_inst["自營_超"] = col("自營商買賣超股數")

    # 合計量（用於過濾條件）
    df_inst["法人量"] = df_inst["外資_量"] + df_inst["投信_量"] + df_inst["自營_量"]

    inst_cols = ["證券代號", "法人量", "外資_量", "外資_超", "投信_量", "投信_超", "自營_量", "自營_超"]
    df = df.merge(df_inst[inst_cols], on="證券代號", how="left")

    # 合併產業分類
    df = df.merge(df_ind, on="證券代號", how="left")
    df["產業"] = df["產業"].fillna("")

    # 衍生指標
    df["量比"]    = df["今量_n"] / df["昨量_n"].replace(0, float("nan"))
    df["漲跌幅"]  = (df["收盤_n"] - df["昨收_n"]) / df["昨收_n"] * 100
    df["法人%"]   = df["法人量"] / df["今量_n"] * 100
    df["外資%"]   = df["外資_量"] / df["今量_n"] * 100
    df["投信%"]   = df["投信_量"] / df["今量_n"] * 100
    df["自營%"]   = df["自營_量"] / df["今量_n"] * 100
    # 買超張數（股 / 1000）
    df["外資超(張)"] = (df["外資_超"] / 1000).round(0)
    df["投信超(張)"] = (df["投信_超"] / 1000).round(0)
    df["自營超(張)"] = (df["自營_超"] / 1000).round(0)

    # 條件5：排除指定產業
    exclude_mask = df["產業"].apply(
        lambda ind: any(kw in ind for kw in EXCLUDE_INDUSTRIES)
    )

    # ── 套用策略條件 ──────────────────────────────────
    mask = (
        (df["量比"]   >= VOL_RATIO_MIN)    &   # 條件1
        (df["收盤_n"] <= PRICE_MAX)         &   # 條件2
        (df["pe_n"]   >  0)                &   # 條件3：本益比>0 → EPS>0
        (df["法人%"]  >= INST_RATIO_MIN)    &   # 條件4
        (~exclude_mask)                     &   # 條件5：排除產業
        (df["pe_n"]   <= PE_MAX)            &   # 條件6：本益比 <= 30
        (df["漲跌幅"]  >  DAILY_DROP_MIN)       # 條件7：漲跌幅 > -7%
    )
    result = df[mask].copy().sort_values("量比", ascending=False)

    # ── 條件8：收盤 >= MA20 ───────────────────────────
    if not result.empty:
        log("計算 20日移動均線（MA20）...")
        codes = set(result["證券代號"].tolist())
        ma20_map = fetch_ma20(today, codes, log)
        result["MA20"] = result["證券代號"].map(ma20_map)
        before = len(result)
        result = result[result["收盤_n"] >= result["MA20"].fillna(float("-inf"))]
        result = result.sort_values("量比", ascending=False)
        log(f"  MA20 篩選：{before} → {len(result)} 檔（剔除 {before - len(result)} 檔）")

    # ── 輸出 ──────────────────────────────────────────
    log(f"\n{'='*55}")
    log(f"策略1 選股結果（{today}）：共 {len(result)} 檔")
    log(f"EPS 判斷：本益比 > 0（TWSE 每季更新）")
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
           base_dir  = os.path.dirname(os.path.abspath(__file__))
           fname     = os.path.join(base_dir, f"策略1_選股_{today}.xlsx")
           fname_csv = os.path.join(base_dir, f"策略1_選股_{today}.csv")
                out.to_excel(fname, index=False)
                log(f"\n已儲存：{fname}")
            except PermissionError:
                log(f"\n⚠ Excel 檔案已開啟，請關閉後重試：{fname}")
            out.to_csv(fname_csv, index=False, encoding="utf-8-sig")
            log(f"已儲存：{fname_csv}")
    else:
        log("今日無符合所有條件的股票。")

    return result, today

if __name__ == "__main__":
    run()
