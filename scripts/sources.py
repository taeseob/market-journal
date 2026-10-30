"""출처 표기(fred:, yf:, ak: ...)별 수집 함수.

지표는 날짜 인덱스의 Series, 지수 일봉은 open/high/low/close/volume 열의 DataFrame으로 돌려준다.
"""
import contextlib
import io
import os
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 30
ROOT = Path(__file__).resolve().parent.parent


def _load_env(path=ROOT / ".env"):
    """.env의 KEY=VALUE를 환경 변수로 읽는다. pykrx는 import할 때 KRX_ID/KRX_PW를 읽으므로 먼저 실행한다."""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env()


def _secret(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f".env에 {name}가 없음")
    return value


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


# --- 키·계정이 필요한 출처 (.env) --------------------------------------------
# 요청 URL에 키가 들어가므로, 요청 오류는 URL 없이 다시 올린다.

def ecos(spec, start):
    """한국은행 ECOS. spec: 통계표/주기/항목, 예: 817Y002/D/010200000"""
    stat, cycle, item = spec.split("/")
    fmt = {"D": "%Y%m%d", "M": "%Y%m"}[cycle]
    end = pd.Timestamp.today()
    url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{_secret('ECOS_API_KEY')}/json/kr/1/100000/"
           f"{stat}/{cycle}/{start:{fmt}}/{end:{fmt}}/{item}")
    try:
        data = _get(url, headers=None).json()
    except requests.RequestException as e:
        raise RuntimeError(f"ECOS 요청 실패 ({type(e).__name__})") from None
    rows = data.get("StatisticSearch", {}).get("row")
    if not rows:
        raise ValueError(f"ECOS 응답 없음: {data.get('RESULT', {}).get('MESSAGE', '')}")
    return _series([pd.to_datetime(r["TIME"], format=fmt) for r in rows], [r["DATA_VALUE"] for r in rows])


def krx(name, start):
    """KRX 정보데이터시스템 (pykrx, 회원 계정 로그인). name: kospi_pbr | kospi_foreign"""
    _secret("KRX_ID"), _secret("KRX_PW")
    end = pd.Timestamp.today()
    with contextlib.redirect_stdout(io.StringIO()):  # pykrx는 로그인할 때 아이디를 출력한다
        from pykrx import stock

        if name == "kospi_pbr":
            s = stock.get_index_fundamental(f"{start:%Y%m%d}", f"{end:%Y%m%d}", "1001")["PBR"]
            s = s[s > 0]
        elif name == "kospi_foreign":
            s = stock.get_market_trading_value_by_date(f"{start:%Y%m%d}", f"{end:%Y%m%d}", "KOSPI")["외국인합계"]
            s = s / 1e12  # 원 → 조 원
        else:
            raise ValueError(f"알 수 없는 KRX 항목: {name}")
    return _series(s.index, s.to_numpy())


def estat(spec, start):
    """일본 e-Stat. spec: 통계표ID/품목(cat01)/지역(area)/표장항목(tab), 예: 0004052037/0161/00000/3"""
    sid, cat01, area, tab = spec.split("/")
    params = {"appId": _secret("ESTAT_APP_ID"), "statsDataId": sid, "cdCat01": cat01, "cdArea": area, "cdTab": tab}
    try:
        data = _get("https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData", headers=None, params=params).json()
    except requests.RequestException as e:
        raise RuntimeError(f"e-Stat 요청 실패 ({type(e).__name__})") from None
    body = data["GET_STATS_DATA"]
    values = body.get("STATISTICAL_DATA", {}).get("DATA_INF", {}).get("VALUE", [])
    if not values:
        raise ValueError(f"e-Stat 응답 없음: {body['RESULT']['ERROR_MSG']}")
    # 시간 코드 '2026001010' = 2026년 10월. 월 자리가 '00'이면 연간 값이라 뺀다
    rows = [(v["@time"], v["$"]) for v in values if v["@time"][6:8] != "00"]
    s = _series([pd.Timestamp(int(t[:4]), int(t[6:8]), 1) for t, _ in rows], [x for _, x in rows])
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

API_KINDS = ("fred", "yf", "oecd-cli", "oecd-cpi", "ecb", "mof-jp", "hkma", "ak", "ecos", "krx", "estat")


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
    if kind == "ecos":
        return ecos(arg, start)
    if kind == "krx":
        return krx(arg, start)
    if kind == "estat":
        return estat(arg, start)
    raise ValueError(f"알 수 없는 출처: {source}")
