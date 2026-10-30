"""수집한 데이터로 차트, 표, 보고서 마크다운을 만든다.

사용법: python scripts/build.py [--date YYYY-MM-DD] [--market kr]

한 줄 요약과 주요 요인은 보고서 파일의 summary/factors 표시 사이에 직접 쓴다.
스크립트를 다시 실행해도 그 부분은 그대로 남는다.
"""
import argparse
import json
import math
import re
import sys
import warnings
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

import spec  # noqa: E402
from fetch import STATUS, cache_key, cache_path  # noqa: E402

REPORTS = spec.ROOT / "reports"
MANUAL = spec.ROOT / "data" / "manual.csv"

# 색 (CLAUDE.md 차트 규칙)
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
LINE, UP, DOWN, MA5, MA20 = "#2a78d6", "#e34948", "#2a78d6", "#eda100", "#52514e"

STALE_DAYS = {"일": 7, "주": 21, "월": 75, "분기": 230, "연": 450}
SOURCE_NAMES = {"fred": "FRED", "yf": "Yahoo Finance", "oecd-cli": "OECD", "oecd-cpi": "OECD", "ecb": "ECB",
                "mof-jp": "일본 재무성", "hkma": "HKMA", "ak": "AKShare", "ecos": "한국은행 ECOS", "krx": "KRX",
                "estat": "e-Stat", "manual": "수동 입력"}

plt.rcParams.update({
    "font.family": "Malgun Gothic", "axes.unicode_minus": False, "font.size": 9,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.linewidth": 0.8, "axes.labelcolor": INK2,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "axes.spines.top": False, "axes.spines.right": False,
})


class Missing(Exception):
    """지표를 그릴 수 없을 때. 메시지는 차트와 데이터 메모에 그대로 쓴다."""


# --- 형식 -----------------------------------------------------------------

def fmt_value(v, unit=""):
    if v is None or pd.isna(v):
        return "—"
    if unit.startswith("%"):
        return f"{v:,.2f}"
    a = abs(v)
    if unit.startswith(("천", "억", "조")) or unit == "원":  # 건수·금액은 소수 한 자리면 충분하다
        return f"{v:,.0f}" if a >= 1000 else f"{v:,.1f}"
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:,.1f}"
    if a >= 1:
        return f"{v:,.2f}"
    return f"{v:.3f}"


def fmt_period(ts, freq):
    if freq == "월":
        return f"{ts:%Y-%m}"
    if freq == "분기":
        return f"{ts.year}-Q{ts.quarter}"
    if freq == "연":
        return f"{ts.year}"
    return f"{ts:%Y-%m-%d}"


def label(ind):
    """'근원 CPI' + '% (전년비)' → '근원 CPI (%, 전년비)'"""
    unit = ind.unit.replace(" (", ", ").rstrip(")") if "(" in ind.unit else ind.unit
    return f"{ind.name} ({unit})"


def fmt_pct(v):
    return "—" if v is None or pd.isna(v) else f"{v * 100:+.1f}%"


def kr_units(v, _=None):
    a = abs(v)
    for unit, div in (("조", 1e12), ("억", 1e8), ("만", 1e4)):
        if a >= div:
            return f"{v / div:,.1f}{unit}"
    return f"{v:,.0f}"


def source_names(sources):
    names = []
    for s in sources:
        kinds = re.findall(r"\{([a-z-]+):", s) if s.startswith("calc:") else [s.split(":")[0]]
        for k in kinds:
            if k in SOURCE_NAMES and SOURCE_NAMES[k] not in names:
                names.append(SOURCE_NAMES[k])
    return names


# --- 데이터 ---------------------------------------------------------------

def close_offset(close):
    """'16:00' → 16시간, '익일 06:30' → 30시간 30분. 봉 날짜에 더하면 종가 확정 시각(KST)."""
    days = 1 if close.startswith("익일") else 0
    hh, mm = close.split()[-1].split(":")
    return pd.Timedelta(days=days, hours=int(hh), minutes=int(mm))


class Store:
    def __init__(self, markets, pub, asof=None):
        self.pub = pub
        # 발행 시점(KST). 이미 발행한 보고서를 다시 만들면 그 보고서의 asof를 쓴다.
        # 처음 만들 때는 오늘이면 지금, 지난 날짜면 그날 23:59
        now = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).floor("min")
        if asof is None:
            asof = now if pub.normalize() == now.normalize() else pub + pd.Timedelta(hours=23, minutes=59)
        self.asof = asof
        self.status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
        self.indicators = {i.id: i for m in markets for a in m.axes for i in a.indicators}
        self.indices = {x.id: x for m in markets for x in m.indices}
        self.manual = self._load_manual()
        self._memo = {}

    @staticmethod
    def _load_manual():
        if not MANUAL.exists():
            return {}
        df = pd.read_csv(MANUAL, parse_dates=["date", "released"])
        df = df.dropna(subset=["value"]).sort_values("released").drop_duplicates(["id", "date"], keep="last")
        return {k: g.set_index("date")["value"].astype(float).sort_index() for k, g in df.groupby("id")}

    def _cached(self, source, ohlcv=False, close=None):
        """캐시를 읽고, 발행 시점에 아직 확정되지 않은 봉은 뺀다."""
        st = self.status.get(cache_key(source, ohlcv))
        path = cache_path(source, ohlcv)
        if not path.exists() or (st and not st["ok"]):
            raise Missing("수집 실패")
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.DatetimeIndex(df.index).astype("datetime64[ns]")
        df = df[df.index <= self.pub]
        if close:
            df = df[df.index + close_offset(close) <= self.asof]
        else:
            # 지표는 발행일 당일 날짜의 값을 미확정으로 본다 (환율·선물 장중 값, 장 마감 후 확정되는 금리 등)
            df = df[df.index < self.asof.normalize()]
        return df if ohlcv else df.iloc[:, 0].dropna()

    def ohlcv(self, index_id):
        idx = self.indices[index_id]
        d = self._cached(idx.source, ohlcv=True, close=idx.close)
        if idx.volume != "= 지수":
            d["volume"] = self._cached(idx.volume, ohlcv=True, close=idx.close)["volume"].reindex(d.index)
        return d

    def _resolve(self, ref):
        if ref in self.indices:
            return self.ohlcv(ref)["close"]
        if ref in self.indicators:
            return self.series(ref)
        return self._cached(ref)

    def _calc(self, expr):
        refs = list(dict.fromkeys(re.findall(r"\{([^}]+)\}", expr)))
        parts = []
        for ref in refs:
            try:
                parts.append(self._resolve(ref).sort_index())
            except Missing as e:
                if "(" in str(e):  # 이미 원인 지표가 적힌 사유는 그대로 올린다
                    raise
                name = self.indicators[ref].name if ref in self.indicators else ref
                raise Missing(f"{e} ({name})")
        base = parts[0]
        env = {"v0": base}
        for i, s in enumerate(parts[1:], 1):
            gap = s.index.to_series().diff().median()
            tol = max(pd.Timedelta(days=7), gap * 1.5 if pd.notna(gap) else pd.Timedelta(days=7))
            left = pd.DataFrame({"t": base.index})
            right = pd.DataFrame({"t": s.index, "v": s.to_numpy()})
            merged = pd.merge_asof(left, right, on="t", direction="backward", tolerance=tol)
            env[f"v{i}"] = pd.Series(merged["v"].to_numpy(), index=base.index)
        code = expr
        for i, ref in enumerate(refs):
            code = code.replace("{" + ref + "}", f"v{i}")
        out = eval(code, {"__builtins__": {}}, env)  # 식은 rules/indicators.md에서만 온다
        return out.replace([np.inf, -np.inf], np.nan).dropna()

    def series(self, ind_id):
        """변환까지 마친 전체 시계열 (발행일까지)."""
        if ind_id in self._memo:
            result = self._memo[ind_id]
            if isinstance(result, Missing):
                raise result
            return result
        try:
            result = self._build(self.indicators[ind_id])
        except Missing as e:
            self._memo[ind_id] = e
            raise
        self._memo[ind_id] = result
        return result

    def _build(self, ind):
        src = ind.source
        if src.startswith("none:"):
            raise Missing(src[5:])
        if src == "manual":
            s = self.manual.get(ind.id)
            if s is None or s.empty:
                raise Missing("수동 입력 전")
        elif src.startswith("calc:"):
            s = self._calc(src[5:])
        else:
            s = self._cached(src)
        if ind.transform == "yoy":
            prev = s.reindex(s.index - pd.DateOffset(years=1)).to_numpy()
            s = (s / prev - 1) * 100
        elif ind.transform == "diff":
            s = s.diff()
        s = s.replace([np.inf, -np.inf], np.nan).dropna()
        s = s[s.index <= self.pub]
        if s.empty:
            raise Missing("데이터 없음")
        if ind.freq == "수시" and s.index[-1] < self.pub:
            s = pd.concat([s, pd.Series([s.iloc[-1]], index=[self.pub])])
        return s

    def window(self, ind, start):
        """차트 기간으로 자른 시계열. 수시 지표는 시작 시점 값을 채우고, cumsum은 기간 누적으로 바꾼다."""
        s = self.series(ind.id)
        w = s[s.index >= start]
        if ind.freq == "수시":
            before = s[s.index < start]
            if len(before):
                w = pd.concat([pd.Series([before.iloc[-1]], index=[start]), w])
        if ind.transform == "cumsum":
            w = w.cumsum()
        if w.empty:
            raise Missing("최근 3년 데이터 없음")
        return w

    def is_stale(self, ind, s):
        limit = STALE_DAYS.get(ind.freq)
        return limit is not None and (self.pub - s.index[-1]).days > limit


# --- 차트 -----------------------------------------------------------------

def _panel_header(ax, title, desc):
    # 맑은 고딕 굵은체에는 수학 기호 '−'(U+2212) 글리프가 없어 일반 하이픈으로 바꾼다
    ax.set_title(title.replace("−", "-"), loc="left", fontsize=10.5, fontweight="bold", color=INK, pad=17)
    ax.text(0, 1.035, desc.replace("−", "-"), transform=ax.transAxes, fontsize=8.5, color=INK2, va="bottom")


def _date_axis(ax, start, end):
    ax.set_xlim(start, end)
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))


def axis_chart(store, market, axis, pub, path):
    """한 축의 지표를 지표당 패널 하나로 그린다. 돌려주는 값: 데이터 메모 목록."""
    inds = axis.indicators
    ncols = 1 if len(inds) <= 3 else 2
    nrows = math.ceil(len(inds) / ncols)
    fig, axs = plt.subplots(nrows, ncols, figsize=(12, 2.55 * nrows + 1.0), squeeze=False)
    start = pub - pd.DateOffset(years=3)
    end = pub + pd.Timedelta(days=120)
    memo = []
    for k, ind in enumerate(inds):
        ax = axs[k // ncols][k % ncols]
        _date_axis(ax, start, end)
        try:
            s = store.window(ind, start)
        except Missing as e:
            _panel_header(ax, label(ind), ind.desc)
            ax.text(0.5, 0.5, f"데이터 없음: {e}", transform=ax.transAxes, ha="center", va="center",
                    fontsize=10, color=MUTED)
            ax.set_yticks([])
            memo.append(f"{ind.name}: 데이터 없음 ({e})")
            continue
        last, v = s.index[-1], s.iloc[-1]
        title = label(ind)
        if store.is_stale(ind, s):
            title += f"  (최신 {fmt_period(last, ind.freq)})"
            memo.append(f"{ind.name}: 최신 데이터가 {fmt_period(last, ind.freq)}로 지연")
        _panel_header(ax, title, ind.desc)
        if ind.freq == "수시":
            ax.step(s.index, s.to_numpy(), where="post", color=LINE, lw=1.5)
        else:
            marker = "o" if ind.freq in ("월", "분기", "연") else None
            ax.plot(s.index, s.to_numpy(), color=LINE, lw=1.5, marker=marker, ms=2.5)
        if ind.ref is not None:
            ax.axhline(ind.ref, color=MUTED, lw=1, ls=(0, (4, 3)))
        ax.plot([last], [v], marker="o", ms=4.5, color=LINE)
        ax.annotate(f"{fmt_value(v, ind.unit)}\n{fmt_period(last, ind.freq)}", xy=(last, v), xytext=(7, 0),
                    textcoords="offset points", va="center", fontsize=8.5, color=INK)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.10g}"))
    for k in range(len(inds), nrows * ncols):
        axs[k // ncols][k % ncols].set_visible(False)
    fig.suptitle(f"{market.name} · {axis.label} · 기준일 {pub:%Y-%m-%d}", x=0.01, ha="left",
                 fontsize=14, fontweight="bold", color=INK)
    names = source_names([i.source for i in inds])
    fig.text(0.01, 0.005, "출처: " + ", ".join(names) if names else "", fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97), h_pad=2.2, w_pad=3)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return memo


def weekly(daily):
    w = daily.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                     "volume": "sum"}).dropna(subset=["close"])
    for n in (5, 20):
        w[f"ma{n}"] = w["close"].rolling(n).mean()
        w[f"vma{n}"] = w["volume"].rolling(n).mean()
    return w


def candle_chart(store, market, idx, pub, path):
    daily = store.ohlcv(idx.id)
    start = pub - pd.DateOffset(years=3)
    w = weekly(daily)
    w = w[w.index >= start]
    in_progress = daily.index[-1] < w.index[-1] and pub <= w.index[-1]
    x = mdates.date2num(w.index - pd.Timedelta(days=2))  # 캔들을 주 가운데(수요일)에 둔다
    colors = np.where(w["close"] >= w["open"], UP, DOWN)

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(12, 6.4), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1], "hspace": 0.06})
    ax.vlines(x, w["low"], w["high"], colors=colors, lw=0.8)
    body = (w["close"] - w["open"]).abs().to_numpy()
    body = np.maximum(body, (w["high"].max() - w["low"].min()) * 0.002)
    ax.bar(x, body, bottom=np.minimum(w["open"], w["close"]), width=4.2, color=colors, linewidth=0)
    ax.plot(x, w["ma5"], color=MA5, lw=1.5, label="5주 이평")
    ax.plot(x, w["ma20"], color=MA20, lw=1.5, label="20주 이평")
    ax.legend(loc="upper left", frameon=False, fontsize=9)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}" if abs(y) >= 100 else f"{y:,.2f}"))
    last = w["close"].iloc[-1]
    ax.annotate(f"{last:,.2f}", xy=(x[-1], last), xytext=(8, 0), textcoords="offset points",
                va="center", fontsize=9, color=INK, fontweight="bold")
    if in_progress:
        ax.annotate("진행 중", xy=(x[-1], w["high"].iloc[-1]), xytext=(0, 12), textcoords="offset points",
                    ha="center", fontsize=8, color=INK2)

    if w["volume"].fillna(0).sum() == 0:
        axv.text(0.5, 0.5, "데이터 없음: 출처에 거래량 없음", transform=axv.transAxes, ha="center",
                 va="center", fontsize=10, color=MUTED)
        axv.set_yticks([])
        vol_memo = f"{idx.name}: 거래량 데이터 없음"
    else:
        axv.bar(x, w["volume"], width=4.2, color=colors, alpha=0.5, linewidth=0)
        axv.plot(x, w["vma5"], color=MA5, lw=1.5)
        axv.plot(x, w["vma20"], color=MA20, lw=1.5)
        axv.yaxis.set_major_formatter(FuncFormatter(kr_units))
        axv.annotate(kr_units(w["volume"].iloc[-1]), xy=(x[-1], w["volume"].iloc[-1]), xytext=(8, 0),
                     textcoords="offset points", va="center", fontsize=8.5, color=INK)
        vol_memo = None
    axv.set_ylabel("거래량 (출처 단위)", color=INK2)
    axv.set_xlim(mdates.date2num(start - pd.Timedelta(days=7)), mdates.date2num(pub + pd.Timedelta(days=70)))
    axv.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 4, 7, 10)))
    axv.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    fig.suptitle(f"{idx.name} · 주봉 · 기준일 {pub:%Y-%m-%d}", x=0.01, ha="left", fontsize=14,
                 fontweight="bold", color=INK)
    fig.text(0.01, 0.005, "출처: " + ", ".join(source_names([idx.source])), fontsize=8, color=MUTED)
    fig.subplots_adjust(left=0.07, right=0.92, top=0.92, bottom=0.07)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return vol_memo


# --- 표 -------------------------------------------------------------------

def index_row(store, idx):
    d = store.ohlcv(idx.id)
    c = d["close"]
    last_dt, last = c.index[-1], c.iloc[-1]

    def change(offset):
        prev = c[c.index <= last_dt - offset]
        return last / prev.iloc[-1] - 1 if len(prev) else None

    prev_year = c[c.index.year < last_dt.year]
    ytd = last / prev_year.iloc[-1] - 1 if len(prev_year) else None
    high52 = d["high"][d.index > last_dt - pd.DateOffset(weeks=52)].max()
    return [idx.name, f"{last:,.2f}", f"{last_dt:%Y-%m-%d}", fmt_pct(change(pd.Timedelta(days=7))),
            fmt_pct(change(pd.DateOffset(months=1))), fmt_pct(change(pd.DateOffset(months=3))), fmt_pct(ytd),
            fmt_pct(change(pd.DateOffset(years=1))), fmt_pct(last / high52 - 1)]


def latest_row(store, ind, start):
    try:
        s = store.window(ind, start)
    except Missing:
        return [label(ind), "—", "—", "—", "—"]
    last = s.index[-1]

    def asof(offset):
        prev = s[s.index <= last - offset]
        return fmt_value(prev.iloc[-1], ind.unit) if len(prev) else "—"

    return [label(ind), fmt_value(s.iloc[-1], ind.unit), fmt_period(last, ind.freq),
            asof(pd.DateOffset(months=3)), asof(pd.DateOffset(years=1))]


def md_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return lines


# --- 보고서 ---------------------------------------------------------------

PLACEHOLDER = {
    "summary": "> (한 줄 요약: 지수 방향 + 가장 큰 요인 한 가지)",
    "factors": "1. **[축] 제목** — 사실 ([출처](URL)). 해석: 지수에 미친 영향.",
}


def preserved_blocks(path):
    blocks = dict(PLACEHOLDER)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        for key in blocks:
            m = re.search(rf"<!-- {key}:start -->\n(.*?)\n<!-- {key}:end -->", text, re.S)
            if m:
                blocks[key] = m.group(1)
    return blocks


def correction_section(path):
    """발행 후 덧붙인 '## 정정' 섹션. 다시 만들어도 그대로 붙인다."""
    if path.exists():
        text = path.read_text(encoding="utf-8")
        i = text.find("\n## 정정\n")
        if i >= 0:
            return text[i:].strip("\n")
    return None


def previous_report(market, pub):
    found = []
    for p in REPORTS.glob(f"*/*/{market.order}-{market.code}.md"):
        try:
            d = pd.Timestamp(p.parent.name)
        except ValueError:
            continue
        if d < pub:
            found.append((d, p))
    return max(found) if found else None


def build_market(store, market, pub):
    out = REPORTS / f"{pub:%Y}" / f"{pub:%Y-%m-%d}"
    charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    path = out / f"{market.order}-{market.code}.md"
    blocks = preserved_blocks(path)
    correction = correction_section(path)
    prev = previous_report(market, pub)
    period_start = prev[0] + pd.Timedelta(days=1) if prev else pub - pd.DateOffset(months=1)
    prev_rel = prev[1].relative_to(spec.ROOT).as_posix() if prev else "none"
    start = pub - pd.DateOffset(years=3)

    lines = ["---", f"market: {market.code}", f"title: {market.title}", f"date: {pub:%Y-%m-%d}",
             f"asof: {store.asof:%Y-%m-%d %H:%M}",
             f"period: {period_start:%Y-%m-%d} ~ {pub:%Y-%m-%d}", f"prev: {prev_rel}", "---", "",
             f"# {market.title} · {pub:%Y-%m-%d}", "", "## 1. 시장 요약", "",
             "<!-- summary:start -->", blocks["summary"], "<!-- summary:end -->", "",
             "### 지수 현황", "", f"기준: 발행 시점({store.asof:%Y-%m-%d %H:%M} KST)에 확정된 각 지수의 직전 종가", ""]
    rows, images, idx_memo = [], [], []
    for idx in market.indices:
        try:
            rows.append(index_row(store, idx))
            vol_memo = candle_chart(store, market, idx, pub, charts / f"{market.code}-0-index-{idx.id}.png")
            images.append(f"![{idx.name} 주봉](charts/{market.code}-0-index-{idx.id}.png)")
            if vol_memo:
                idx_memo.append(vol_memo)
        except Missing as e:
            rows.append([idx.name] + ["—"] * 8)
            idx_memo.append(f"{idx.name}: 데이터 없음 ({e})")
    lines += md_table(["지수", "종가", "종가일", "1주", "1개월", "3개월", "연초 대비", "1년", "52주 고점 대비"], rows)
    lines.append("")
    for img in images:
        lines += [img, ""]
    if idx_memo:
        lines += ["데이터 메모:", ""] + [f"- {m}" for m in idx_memo] + [""]
    lines += ["### 주요 요인", "", "<!-- factors:start -->", blocks["factors"], "<!-- factors:end -->", "",
              "## 2. 지표 (최근 3년)", ""]

    all_memo = list(idx_memo)
    for axis in market.axes:
        name = f"{market.code}-{axis.order}-{axis.code}.png"
        memo = axis_chart(store, market, axis, pub, charts / name)
        all_memo += memo
        lines += [f"### 2.{axis.order} {axis.label}", "", f"![{axis.label}](charts/{name})", ""]
        lines += md_table(["지표", "최신값", "기준 시점", "3개월 전", "1년 전"],
                          [latest_row(store, ind, start) for ind in axis.indicators])
        lines.append("")
        if memo:
            lines += ["데이터 메모:", ""] + [f"- {m}" for m in memo] + [""]
    if correction:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", correction]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path, all_memo


def existing_asof(pub):
    """같은 발행일 보고서가 이미 있으면 그 발행 시점을 돌려준다 (다시 만들어도 수치가 바뀌지 않게)."""
    for p in sorted((REPORTS / f"{pub:%Y}" / f"{pub:%Y-%m-%d}").glob("*.md")):
        m = re.search(r"^asof: (\d{4}-\d\d-\d\d \d\d:\d\d)$", p.read_text(encoding="utf-8"), re.M)
        if m:
            return pd.Timestamp(m.group(1))
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    p.add_argument("--market")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")

    pub = pd.Timestamp(args.date)
    markets = spec.load()
    store = Store(markets, pub, existing_asof(pub))
    for m in markets:
        if args.market and m.code != args.market:
            continue
        path, memo = build_market(store, m, pub)
        print(f"{path.relative_to(spec.ROOT).as_posix()}  (데이터 메모 {len(memo)}건)")
        for line in memo:
            print(f"  - {line}")


if __name__ == "__main__":
    main()
