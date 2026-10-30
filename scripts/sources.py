"""출처 표기(fred:, yf:, ak: ...)별 수집 함수.

지표는 날짜 인덱스의 Series, 지수 일봉은 open/high/low/close/volume 열의 DataFrame으로 돌려준다.
"""
import io
import re
import time

import numpy as np
import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 30


def _get(url, headers=HEADERS, tries=3, **kw):
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def _series(dates, values):
    s = pd.Series(np.asarray(pd.to_numeric(pd.Series(list(values)), errors="coerce"), dtype=float),
                  index=pd.DatetimeIndex(pd.to_datetime(list(dates))))
    s = s[s.index.notna()]
    return s[~s.index.duplicated(keep="last")].sort_index().dropna()


def _ohlcv(df):
    out = df[["open", "high", "low", "close", "volume"]].astype(float)
    out.index = pd.DatetimeIndex(pd.to_datetime(df["date"]))
    return out.sort_index().dropna(subset=["close"])


def _cn_month(text):
    """'2026年09月份' → 2026-09-01"""
    m = re.match(r"(\d{4})年(\d{1,2})月", str(text))
    return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1)


def _cn_day(text):
    """'2025年05月15日' → 2025-05-15"""
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", str(text))
    return pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))


# --- 해외 공개 API ---------------------------------------------------------

def fred(code, start):
    # FRED는 브라우저 흉내를 낸 User-Agent를 차단하므로 requests 기본 헤더를 쓴다
    df = pd.read_csv(io.StringIO(_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={code}", headers=None).text))
    s = _series(df.iloc[:, 0], df.iloc[:, 1])
    return s[s.index >= start]


def yf_daily(ticker, start):
    import yfinance as yf

    d = yf.download(ticker, start=f"{start:%Y-%m-%d}", interval="1d", auto_adjust=False,
                    progress=False, multi_level_index=False)
    if d.empty:
        raise ValueError(f"Yahoo Finance에 {ticker} 데이터가 없음")
    d = d.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].astype(float)
    d.index = pd.DatetimeIndex(d.index)
    if d.index.tz is not None:
        d.index = d.index.tz_localize(None)
    return d.dropna(subset=["close"])


def _oecd(flow_key, start):
    url = f"https://sdmx.oecd.org/public/rest/data/{flow_key}?startPeriod={start:%Y-%m}&format=csvfile"
    df = pd.read_csv(io.StringIO(_get(url).text))
    return _series(df["TIME_PERIOD"], df["OBS_VALUE"])


def oecd_cli(area, start):
    return _oecd(f"OECD.SDD.STES,DSD_STES@DF_CLI,4.1/{area}.M.LI...AA...H", start)


def oecd_cpi(area, item, start):
    return _oecd(f"OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/{area}.M.N.CPI.PA.{item}.N.GY", start)


def ecb(flow_key, start):
    url = f"https://data-api.ecb.europa.eu/service/data/{flow_key}?startPeriod={start:%Y}&format=csvdata"
    df = pd.read_csv(io.StringIO(_get(url).text))
    dates = [pd.Period(p.replace("-Q", "Q"), freq="Q").start_time if "-Q" in p else pd.Timestamp(p)
             for p in df["TIME_PERIOD"]]
    return _series(dates, df["OBS_VALUE"])


def mof_jp(tenor, start):
    base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
    parts = []
    for path in ("historical/jgbcme_all.csv", "jgbcme.csv"):
        text = _get(base + path).content.decode("utf-8", "replace")
        df = pd.read_csv(io.StringIO(text), skiprows=1)
        parts.append(_series(pd.to_datetime(df["Date"], format="%Y/%m/%d", errors="coerce"), df[tenor]))
    s = pd.concat(parts)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s.index >= start]


def hkma_hibor3m(start):
    url = ("https://api.hkma.gov.hk/public/market-data-and-statistics/"
           "monthly-statistical-bulletin/er-ir/hk-interbank-ir-daily")
    rows, offset = [], 0
    while True:
        params = {"segment": "hibor.fixing", "pagesize": 1000, "offset": offset}  # 기본 정렬이 최신순
        recs = _get(url, params=params).json()["result"]["records"]
        rows += recs
        if not recs or recs[-1]["end_of_day"] < f"{start:%Y-%m-%d}":
            break
        offset += len(recs)
    df = pd.DataFrame(rows)
    s = _series(df["end_of_day"], df["ir_3m"])
    return s[s.index >= start]


# --- AKShare (중국·홍콩) ----------------------------------------------------

def _ak():
    import akshare

    return akshare


def _ak_csi300(start):
    try:
        d = _ak().stock_zh_index_daily_em(symbol="sh000300", start_date=f"{start:%Y%m%d}", end_date="20991231")
    except Exception:
        # 동방재부가 연결을 끊는 경우가 잦아 시나로 대신 받는다
        d = _ak().stock_zh_index_daily(symbol="sh000300")
    d = _ohlcv(d)
    return d[d.index >= start]


def _ak_hk_index(symbol):
    def fetch(start):
        d = _ohlcv(_ak().stock_hk_index_daily_sina(symbol=symbol))
        return d[d.index >= start]
    return fetch


def _ak_monthly(func, column):
    def fetch(start):
        d = getattr(_ak(), func)()
        s = _series([_cn_month(x) for x in d["月份"]], d[column])
        return s[s.index >= start]
    return fetch


def _ak_lpr(column):
    def fetch(start):
        d = _ak().macro_china_lpr()
        s = _series(d["TRADE_DATE"], d[column])
        return s[s.index >= start - pd.DateOffset(years=2)]
    return fetch


def _ak_rrr(start):
    d = _ak().macro_china_reserve_requirement_ratio()
    return _series([_cn_day(x) for x in d["生效时间"]], d["大型金融机构-调整后"])


def _ak_cn10y(start):
    d = _ak().bond_zh_us_rate(start_date=f"{start:%Y%m%d}")
    return _series(d["日期"], d["中国国债收益率10年"])


def _ak_csi300_pe(start):
    d = _ak().stock_index_pe_lg(symbol="沪深300")
    s = _series(d["日期"], d["滚动市盈率"])
    return s[s.index >= start]


def _ak_southbound(start):
    d = _ak().stock_hsgt_hist_em(symbol="南向资金")
    s = _series(d["日期"], d["当日成交净买额"])
    return s[s.index >= start]


def _ak_home_price_t1(start):
    d = _ak().macro_china_new_house_price(city_first="北京", city_second="上海")
    v = pd.to_numeric(d["新建商品住宅价格指数-同比"], errors="coerce") - 100
    s = v.groupby(pd.to_datetime(d["日期"])).mean()
    s = _series(s.index, s.values)
    return s[s.index >= start]


AK = {
    "csi300": _ak_csi300,
    "hstech": _ak_hk_index("HSTECH"),
    "ah_premium": _ak_hk_index("HSAHP"),
    "pmi": _ak_monthly("macro_china_pmi", "制造业-指数"),
    "cpi": _ak_monthly("macro_china_cpi", "全国-同比增长"),
    "ppi": _ak_monthly("macro_china_ppi", "当月同比增长"),
    "m1": _ak_monthly("macro_china_money_supply", "货币(M1)-同比增长"),
    "lpr1y": _ak_lpr("LPR1Y"),
    "lpr5y": _ak_lpr("LPR5Y"),
    "rrr": _ak_rrr,
    "cn10y": _ak_cn10y,
    "csi300_pe": _ak_csi300_pe,
    "southbound": _ak_southbound,
    "home_price_t1": _ak_home_price_t1,
}


# --- 진입점 ----------------------------------------------------------------

API_KINDS = ("fred", "yf", "oecd-cli", "oecd-cpi", "ecb", "mof-jp", "hkma", "ak")


def fetch(source, start, ohlcv=False):
    """출처 표기 하나를 받아 데이터를 돌려준다. ohlcv=True면 지수 일봉."""
    kind, _, arg = source.partition(":")
    if kind == "yf":
        d = yf_daily(arg, start)
        return d if ohlcv else d["close"]
    if kind == "ak":
        d = AK[arg](start)
        return d["close"] if isinstance(d, pd.DataFrame) and not ohlcv else d
    if kind == "fred":
        return fred(arg, start)
    if kind == "oecd-cli":
        return oecd_cli(arg, start)
    if kind == "oecd-cpi":
        area, item = arg.split(":")
        return oecd_cpi(area, item, start)
    if kind == "ecb":
        return ecb(arg, start)
    if kind == "mof-jp":
        return mof_jp(arg, start)
    if kind == "hkma" and arg == "hibor3m":
        return hkma_hibor3m(start)
    raise ValueError(f"알 수 없는 출처: {source}")
