"""rules/indicators.md를 읽어 시장·지수·축·지표 정의를 만든다."""
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "rules" / "indicators.md"

# 순서 번호 → (시장 코드, 차트에 쓰는 이름, 보고서 제목)
MARKETS = {
    "01": ("global", "글로벌", "글로벌 시장 보고서 (미국 중심)"),
    "02": ("kr", "한국", "한국 시장 보고서"),
    "03": ("cnhk", "중국·홍콩", "중국·홍콩 시장 보고서"),
    "04": ("jp", "일본", "일본 시장 보고서"),
    "05": ("eu", "유럽", "유럽 시장 보고서"),
}
AXIS_CODES = {1: "growth", 2: "inflation", 3: "policy", 4: "credit", 5: "earnings", 6: "external"}


@dataclass
class Index:
    id: str
    name: str
    source: str
    volume: str
    close: str  # 마감 시각 (KST). '익일 06:00'처럼 다음 날일 수 있다
    note: str


@dataclass
class Indicator:
    id: str
    name: str
    desc: str
    unit: str
    freq: str
    transform: str
    ref: float | None
    source: str
    note: str


@dataclass
class Axis:
    order: int
    code: str
    label: str
    indicators: list[Indicator] = field(default_factory=list)


@dataclass
class Market:
    order: str
    code: str
    name: str
    title: str
    indices: list[Index] = field(default_factory=list)
    axes: list[Axis] = field(default_factory=list)


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def load(path=SPEC_PATH):
    markets, market, section = [], None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            market, section = None, None
            m = re.match(r"## (\d\d) ", line)
            if m and m.group(1) in MARKETS:
                code, name, title = MARKETS[m.group(1)]
                market = Market(m.group(1), code, name, title)
                markets.append(market)
        elif line.startswith("### ") and market:
            m = re.match(r"### (\d)\. (.+)", line)
            if m:
                order = int(m.group(1))
                section = Axis(order, AXIS_CODES[order], m.group(2).strip())
                market.axes.append(section)
            else:
                section = "index" if line.strip() == "### 지수" else None
        elif line.startswith("|") and market and section:
            cells = _cells(line)
            if cells[0] == "id" or set(cells[0]) <= set("-:"):
                continue
            if section == "index":
                market.indices.append(Index(*cells[:6]))
            else:
                ref = None if cells[6] in ("—", "-", "") else float(cells[6])
                section.indicators.append(Indicator(*cells[:6], ref, cells[7], cells[8]))
    return markets
