"""한 발행 폴더의 시장 보고서를 A4 PDF 한 파일로 묶는다. 시장마다 새 페이지에서 시작한다.

사용법: python scripts/pdf.py [--date YYYY-MM-DD] [--edition daily|monthly]
  → reports/YYYY/YYYY-MM-DD/market-journal-YYYY-MM-DD.pdf (월간은 YYYY-MM)

마크다운을 HTML로 바꾼 뒤 Chrome(없으면 Edge) 헤드리스로 인쇄한다.
"""
import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import markdown
import pandas as pd

import spec
from build import EDITIONS, INK, INK2, LINE, MUTED, GRID, out_dir

EDITION_NAMES = {"daily": "일간", "monthly": "월간"}

BROWSERS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
]

CSS = f"""
@page {{
  size: A4;
  margin: 15mm 14mm 16mm;
  @bottom-left {{ content: "{{footer}}"; font: 7.5pt "Malgun Gothic"; color: {MUTED}; }}
  @bottom-right {{ content: counter(page) " / " counter(pages); font: 7.5pt "Malgun Gothic"; color: {MUTED}; }}
}}
html {{ background: #fff; }}
body {{ margin: 0; font: 9.5pt/1.6 "Malgun Gothic", sans-serif; color: {INK}; }}
section.market + section.market {{ break-before: page; }}
h1 {{ font-size: 16pt; line-height: 1.3; margin: 0 0 1.5mm; }}
.meta {{ margin: 0 0 5mm; padding-bottom: 2.5mm; border-bottom: 1px solid {MUTED}; color: {INK2}; font-size: 8.5pt; }}
h2 {{ font-size: 12.5pt; margin: 7mm 0 3mm; padding-bottom: 1mm; border-bottom: 1px solid {GRID}; break-after: avoid; }}
h3 {{ font-size: 10.5pt; margin: 5mm 0 2mm; break-after: avoid; }}
p, ul, ol {{ margin: 0 0 2.5mm; }}
ul, ol {{ padding-left: 5mm; }}
li {{ margin-bottom: 1.5mm; }}
li > ul {{ margin: 1mm 0 0; }}
blockquote {{ margin: 0 0 4mm; padding: 2.5mm 4mm; border-left: 3px solid {LINE}; background: #f3f2ee; }}
blockquote p {{ margin: 0; }}
a {{ color: {LINE}; text-decoration: none; }}
img {{ display: block; width: 100%; margin: 0 0 3mm; break-inside: avoid; }}
table {{ width: 100%; border-collapse: collapse; margin: 0 0 3mm; font-size: 8.5pt; font-variant-numeric: tabular-nums; break-inside: avoid; }}
th, td {{ padding: 1mm 1.5mm; border-bottom: 1px solid {GRID}; text-align: right; white-space: nowrap; }}
th {{ color: {INK2}; font-weight: normal; border-bottom-color: {MUTED}; }}
th:first-child, td:first-child {{ text-align: left; white-space: normal; }}
.axis {{ break-inside: avoid; }}
p:has(+ ul) {{ break-after: avoid; }}
"""


def find_browser():
    for p in BROWSERS:
        path = Path(os.path.expandvars(p))
        if path.exists():
            return path
    for name in ("chrome", "msedge", "chromium"):
        if found := shutil.which(name):
            return Path(found)
    raise SystemExit("Chrome 또는 Edge를 찾지 못했다.")


def front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    meta = dict(line.split(": ", 1) for line in m.group(1).splitlines() if ": " in line)
    return meta, text[m.end():]


def market_html(path):
    meta, body = front_matter(path.read_text(encoding="utf-8"))
    body = re.sub(r"<!--.*?-->\n?", "", body, flags=re.S)
    out = markdown.markdown(body, extensions=["tables"], tab_length=2)  # 보고서의 하위 목록은 2칸 들여쓰기
    note = f"발행 시점 {meta['asof']} KST · 요약 대상 기간 {meta['period']}"
    out = out.replace("</h1>", f'</h1>\n<p class="meta">{html.escape(note)}</p>', 1)
    # 지표 축(2.1~2.6)은 제목·차트·표·메모를 한 페이지에 둔다
    parts = re.split(r"(?=<h[23][ >])", out)
    out = "".join(f'<div class="axis">{p}</div>' if re.match(r"<h3>\d\.\d ", p) else p for p in parts)
    return f'<section class="market">\n{out}\n</section>'


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    p.add_argument("--edition", choices=EDITIONS, default="daily")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    ed = EDITIONS[args.edition]
    folder = out_dir(pd.Timestamp(args.date), ed)
    reports = sorted(folder.glob("[0-9][0-9]-*.md"))
    if not reports:
        raise SystemExit(f"보고서가 없다: {folder}")
    pdf = folder / f"market-journal-{folder.name}.pdf"
    footer = f"Market Journal · {EDITION_NAMES[ed.key]} 보고서 {folder.name}"
    doc = (f'<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
           f'<title>{html.escape(footer)}</title>\n<base href="{folder.as_uri()}/">\n'
           f"<style>{CSS.replace('{footer}', footer)}</style>\n</head>\n<body>\n"
           + "\n".join(market_html(r) for r in reports) + "\n</body>\n</html>\n")

    pdf.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "report.html"
        page.write_text(doc, encoding="utf-8")
        subprocess.run([str(find_browser()), "--headless", "--disable-gpu", f"--user-data-dir={tmp}",
                        "--no-pdf-header-footer", "--generate-pdf-document-outline",
                        f"--print-to-pdf={pdf}", page.as_uri()],
                       check=True, capture_output=True, timeout=180)
    if not pdf.exists():
        raise SystemExit("PDF가 만들어지지 않았다.")
    print(f"{pdf.relative_to(spec.ROOT).as_posix()}  (시장 {len(reports)}개, {pdf.stat().st_size / 1e6:.1f}MB)")


if __name__ == "__main__":
    main()
