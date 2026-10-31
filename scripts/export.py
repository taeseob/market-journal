"""한 발행 폴더의 시장 보고서를 A4 PDF와 HTML 한 파일씩으로 내보낸다. 시장마다 새 페이지에서 시작한다.

사용법: python scripts/export.py [--date YYYY-MM-DD] [--edition daily|monthly] [--format pdf html]
  → reports/YYYY/YYYY-MM-DD/market-journal-YYYY-MM-DD.pdf, .html (월간은 YYYY-MM)

마크다운을 HTML로 바꾼 뒤
  pdf   Chrome(없으면 Edge) 헤드리스로 인쇄한다. 차트는 로컬 파일을 쓴다.
  html  그대로 저장한다. 차트는 GitHub 저장소에서 불러오므로 파일 하나만 있으면 어디서든 열린다.
        차트 주소는 차트를 마지막으로 바꾼 커밋에 고정한다. 차트를 먼저 커밋하고 GitHub에 올려야 한다.
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
@media screen {{
  body {{ max-width: 960px; margin: 0 auto; padding: 32px 16px 64px; font-size: 15px; }}
  .meta, table {{ font-size: 13px; }}
  .table-wrap {{ overflow-x: auto; }}
  section.market + section.market {{ margin-top: 56px; padding-top: 32px; border-top: 2px solid {MUTED}; }}
}}
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


def git(*args):
    return subprocess.run(["git", *args], cwd=spec.ROOT, capture_output=True, text=True, check=True).stdout.strip()


def github_base(folder):
    """차트를 마지막으로 바꾼 커밋의 GitHub 원본 주소. 차트가 아직 GitHub에 없으면 멈춘다."""
    charts = (folder / "charts").relative_to(spec.ROOT).as_posix()
    if git("status", "--porcelain", "--", charts):
        raise SystemExit(f"{charts}에 커밋하지 않은 변경이 있다. 차트를 커밋하고 GitHub에 올린 뒤 다시 실행한다.")
    commit = git("log", "-1", "--format=%H", "--", charts)
    if not commit or not git("branch", "-r", "--contains", commit):
        raise SystemExit(f"{charts}가 아직 GitHub에 없다. 커밋하고 올린 뒤 다시 실행한다.")
    repo = re.search(r"github\.com[:/](.+?)(?:\.git)?$", git("remote", "get-url", "origin")).group(1)
    return f"https://raw.githubusercontent.com/{repo}/{commit}/{folder.relative_to(spec.ROOT).as_posix()}/"


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
    out = out.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    # 지표 축(2.1~2.6)은 제목·차트·표·메모를 한 페이지에 둔다
    parts = re.split(r"(?=<h[23][ >])", out)
    out = "".join(f'<div class="axis">{p}</div>' if re.match(r"<h3>\d\.\d ", p) else p for p in parts)
    return f'<section class="market">\n{out}\n</section>'


def document(reports, title, base):
    """시장 보고서들을 HTML 한 문서로 묶는다. 차트(charts/...)는 base 기준으로 불러온다."""
    return (f'<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{html.escape(title)}</title>\n<base href="{base}">\n'
            f"<style>{CSS.replace('{footer}', title)}</style>\n</head>\n<body>\n"
            + "\n".join(market_html(r) for r in reports) + "\n</body>\n</html>\n")


def print_pdf(doc, pdf):
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    p.add_argument("--edition", choices=EDITIONS, default="daily")
    p.add_argument("--format", nargs="+", choices=["pdf", "html"], default=["pdf", "html"])
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    ed = EDITIONS[args.edition]
    folder = out_dir(pd.Timestamp(args.date), ed)
    reports = sorted(folder.glob("[0-9][0-9]-*.md"))
    if not reports:
        raise SystemExit(f"보고서가 없다: {folder}")
    stem = folder / f"market-journal-{folder.name}"
    title = f"Market Journal · {EDITION_NAMES[ed.key]} 보고서 {folder.name}"

    outputs = []
    if "html" in args.format:
        path = stem.with_suffix(".html")
        path.write_text(document(reports, title, github_base(folder)), encoding="utf-8")
        outputs.append(path)
    if "pdf" in args.format:
        path = stem.with_suffix(".pdf")
        print_pdf(document(reports, title, f"{folder.as_uri()}/"), path)
        outputs.append(path)
    for path in outputs:
        print(f"{path.relative_to(spec.ROOT).as_posix()}  (시장 {len(reports)}개, {path.stat().st_size / 1e6:.1f}MB)")


if __name__ == "__main__":
    main()
