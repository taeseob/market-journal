# Market Journal

주요 주식시장(미국 중심 글로벌, 한국, 중국·홍콩, 일본, 유럽)의 상황을 시장별 보고서로 남기는 개인 기록 저장소입니다.

> 이 저장소의 내용은 개인 기록이며 투자 권유가 아닙니다. 데이터는 공개 출처에서 자동으로 받은 것으로, 오류나 지연이 있을 수 있습니다.

## 보고서

[`reports/`](reports/)에 발행일별로 쌓입니다. 시장마다 보고서 하나이고, 구성은 다음과 같습니다.

1. **시장 요약:** 한 줄 요약, 지수 현황 표, 지수 캔들 차트(일간 주봉 5·13주, 월간 월봉 6·12개월 이동평균, MACD, RSI, 거래량), 주요 요인
2. **지표 (최근 3년):** 경기, 물가, 정책·금리, 유동성·신용, 이익·밸류, 대외·수급·심리의 6개 축별 차트와 최신값 표

## 구조

| 경로 | 내용 |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | 보고서 작성 규칙 |
| [`rules/indicators.md`](rules/indicators.md) | 시장별·축별 지표 정의와 출처 |
| [`templates/report.md`](templates/report.md) | 보고서 템플릿 |
| [`scripts/`](scripts/) | 데이터 수집(`fetch.py`)과 차트·보고서 생성(`build.py`) |
| [`data/manual.csv`](data/manual.csv) | 수동 입력 지표 |

## 실행

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python scripts/fetch.py --date 2026-10-30
.venv/Scripts/python scripts/build.py --date 2026-10-30
```

한국은행 ECOS, KRX, 일본 e-Stat 데이터를 받으려면 프로젝트 폴더의 `.env`에 키와 계정이 필요합니다 (`ECOS_API_KEY`, `KRX_ID`, `KRX_PW`, `ESTAT_APP_ID`). `.env`는 저장소에 올리지 않습니다.

## 데이터 출처

- 키 없이: FRED, Yahoo Finance, OECD, ECB, 일본 재무성, HKMA, AKShare
- 키·계정 필요: 한국은행 ECOS, KRX 정보데이터시스템, 일본 e-Stat

このサービスは、政府統計総合窓口(e-Stat)のAPI機能を使用していますが、サービスの内容は国によって保証されたものではありません。
(이 서비스는 일본 정부통계 종합창구(e-Stat)의 API 기능을 사용하지만, 서비스 내용은 일본 정부가 보증하지 않습니다.)
