"""디지털 PDF(텍스트 레이어가 있는 PDF) 견적서에서 표를 좌표 기반으로 직접 읽는 추출기.

docx_table_extractor.py와 같은 철학이다 — PDF가 스캔 이미지가 아니라 진짜 텍스트
레이어를 가지고 있다면, OCR 없이 표의 실제 셀 경계 좌표만으로 표를 그대로
복원할 수 있다. quotation_extractor.py의 _pdf_to_source()가 지금 하고 있는
"페이지 전체를 한 줄 텍스트로 평문화"와 근본적으로 다르다 — 평문화는 열 순서를
보장하지 않지만, 이 추출기는 각 셀의 실제 경계 좌표로 행/열을 지킨다.

병합 셀 처리(중요):
    pdfplumber의 편의 함수 page.extract_tables()는 병합된 칸을 사각형 그리드에
    억지로 맞추면서 "이 칸이 rowspan인지 colspan인지" 정보를 잃고 그냥 None으로만
    표시한다. 이 모듈은 대신 page.find_tables()가 주는 실제 셀 경계 사각형
    (table.cells)을 직접 쓴다 — 병합된 칸은 처음부터 더 큰 사각형 하나로
    나오므로, 그 사각형이 그리드에서 몇 행×몇 열을 덮는지 좌표로 계산해서 해당
    칸 전체에 같은 텍스트를 채운다(docx_table_extractor.py가 병합 셀 텍스트를
    반복해서 채우는 것과 같은 규칙). 행/열 병합이 동시에 걸린 칸(2x2 이상)도
    같은 방식으로 정확히 처리된다.

글자 순서 처리(중요):
    page.extract_tables()가 기본으로 쓰는 좌표 정렬 방식은 두 가지 경우에
    글자 순서를 실제로 틀리게 만드는 것을 실측으로 확인했다:
        1. 세로쓰기(위→아래) 헤더 — "비고"가 "고 비"로 뒤집혀 나옴
        2. 좁은 칸에 줄바꿈된 굵은 글씨 — 렌더링 과정에서 글자 경계가 겹치면
           "추가비용"이 "추비용가"로 순서가 깨짐
    이 모듈은 각 셀을 crop한 뒤 extract_text(use_text_flow=True)로 다시
    읽는다 — 좌표 재정렬 대신 PDF 콘텐츠 스트림에 실제로 그려진 순서를
    그대로 따르므로 두 문제 모두 해결된다.

남아있는 한계:
    세로쓰기 글자가 옆 칸 경계를 살짝 침범해서 그려지는 경우(Word→PDF 변환
    자체의 렌더링 특성)는 이 모듈로 고칠 수 없다 — 셀 경계 자체가 아니라
    원본 PDF에 글자가 배치된 좌표가 틀어져 있기 때문이다. 세로쓰기 헤더가
    실제 문서에서 흔치 않다면 무시해도 되는 잔여 리스크다.

언제 이 추출기가 아니라 스캔 처리(비전 모델/PaddleOCR)로 가야 하는가:
    페이지에서 추출되는 문자 수가 0에 가까우면(스캔본) 표 좌표 자체가 없으므로
    이 추출기는 빈 결과만 반환한다. 호출자는 그 경우 기존 스캔 PDF 경로로
    폴백해야 한다 (has_digital_text_layer()로 미리 판단할 수 있다).

단독 실행 예:
    python pdf_table_extractor.py --pdf 견적서.pdf --output-html pdf_report.html
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

EDGE_TOLERANCE = 0.6  # 포인트(pt). 부동소수점 오차로 갈라진 같은 경계선을 하나로 합친다.


def _unique_edges(values: list[float], tol: float = EDGE_TOLERANCE) -> list[float]:
    """좌표값 목록에서 서로 tol 이내로 가까운 값을 하나의 경계선으로 합친다."""
    ordered = sorted(values)
    merged = [ordered[0]]
    for value in ordered[1:]:
        if value - merged[-1] > tol:
            merged.append(value)
    return merged


def _edge_index(edges: list[float], value: float, tol: float = EDGE_TOLERANCE) -> int:
    """value와 가장 가까운 경계선의 인덱스를 찾는다(그리드 행/열 위치로 변환)."""
    for index, edge in enumerate(edges):
        if abs(edge - value) <= tol:
            return index
    return min(range(len(edges)), key=lambda index: abs(edges[index] - value))


def _table_to_grid(table: Any, page: Any) -> list[list[str]]:
    """table.cells(실제 셀 경계 사각형)로부터 병합 정보를 보존한 텍스트 그리드를 만든다."""
    xs = [value for cell in table.cells for value in (cell[0], cell[2])]
    ys = [value for cell in table.cells for value in (cell[1], cell[3])]
    col_edges = _unique_edges(xs)
    row_edges = _unique_edges(ys)
    n_rows, n_cols = len(row_edges) - 1, len(col_edges) - 1
    grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]

    for bbox in table.cells:
        x0, top, x1, bottom = bbox
        # 좌표 재정렬 대신 실제 그려진 순서를 그대로 읽어 세로쓰기/좁은칸
        # 줄바꿈에서도 글자 순서가 틀어지지 않게 한다.
        text = (page.crop(bbox).extract_text(use_text_flow=True) or "").strip()
        col_start, col_end = _edge_index(col_edges, x0), _edge_index(col_edges, x1)
        row_start, row_end = _edge_index(row_edges, top), _edge_index(row_edges, bottom)
        for row in range(row_start, row_end):
            for col in range(col_start, col_end):
                grid[row][col] = text
    return grid


def _open_pdf(source: Any):
    """경로 문자열/Path와 이미 메모리에 있는 바이트 스트림(BytesIO 등)을 모두 받는다.

    quotation_extractor.py는 파일을 디스크에 다시 쓰지 않고 bytes로만 다루므로,
    여기서 str(path)로 강제 캐스팅하면 BytesIO가 깨진다. pdfplumber.open()은
    경로 문자열/Path/파일객체를 전부 받아들이므로 그대로 전달한다.
    """
    import pdfplumber

    if isinstance(source, (str, Path)):
        return pdfplumber.open(str(source))
    return pdfplumber.open(source)


def _bbox_contains(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float], tol: float = 2.0) -> bool:
    """outer 사각형이 inner 사각형을 (여백 tol 안에서) 완전히 포함하는지 본다."""
    return (
        outer[0] - tol <= inner[0]
        and outer[1] - tol <= inner[1]
        and outer[2] + tol >= inner[2]
        and outer[3] + tol >= inner[3]
    )


def extract_tables_from_pdf(source: str | Path | Any) -> list[list[list[str]]]:
    """PDF 안의 모든 표를 페이지 순서대로 행 x 열 텍스트 그리드로 읽는다.

    병합된 칸은 docx_table_extractor.py와 같은 규칙으로 병합 범위 전체에 같은
    텍스트를 채운다. 표가 하나도 감지되지 않는 페이지(스캔본이거나 표가 없는
    페이지)는 건너뛴다.

    "표 안의 표"(시각적으로 셀 안에 또 다른 표가 들어있는 문서) 중복 방지:
    작은 표의 경계 사각형이 다른(더 큰) 표의 셀 사각형 안에 통째로 들어가면,
    그 작은 표는 부모 표의 해당 셀 텍스트 안에 이미 포함되어 있으므로
    독립된 표로 다시 내보내지 않는다. docx_table_extractor.py의 중첩 표 처리
    방식(부모 셀 텍스트에 풀어서 합침)과 결과적으로 같은 그림이 된다.
    """
    tables: list[list[list[str]]] = []
    with _open_pdf(source) as pdf:
        for page in pdf.pages:
            found = page.find_tables()
            for table in found:
                other_cells = [cell for other in found if other is not table for cell in other.cells]
                if any(_bbox_contains(cell, table.bbox) for cell in other_cells):
                    continue  # 다른 표의 셀 안에 완전히 들어있는 중첩 표 -> 중복 제외
                grid = _table_to_grid(table, page)
                if any(any(cell for cell in row) for row in grid):
                    tables.append(grid)
    return tables


def extract_page_text_outside_tables(source: str | Path | Any) -> list[str]:
    """표 영역을 뺀 본문(제목, 안내문, 서명란 등)을 페이지별 평문으로 읽는다.

    docx_table_extractor.extract_paragraphs()와 대응된다. page.filter()로 표
    경계 사각형 안의 글자를 걸러낸 뒤 남은 글자만 추출하므로, 표 내용이
    document_text에 (구조가 깨진 채로) 두 번 들어가는 것을 막는다. 글자 순서
    문제를 피하기 위해 use_text_flow=True도 그대로 적용한다.
    """
    pages_text: list[str] = []
    with _open_pdf(source) as pdf:
        for page in pdf.pages:
            table_bboxes = [table.bbox for table in page.find_tables()]

            def _is_outside_tables(obj: dict[str, Any], bboxes: list[tuple[float, float, float, float]] = table_bboxes) -> bool:
                for x0, top, x1, bottom in bboxes:
                    if (
                        obj["x0"] >= x0 - 1
                        and obj["x1"] <= x1 + 1
                        and obj["top"] >= top - 1
                        and obj["bottom"] <= bottom + 1
                    ):
                        return False
                return True

            filtered_page = page.filter(_is_outside_tables) if table_bboxes else page
            text = (filtered_page.extract_text(use_text_flow=True) or "").strip()
            if text:
                pages_text.append(text)
    return pages_text


def has_digital_text_layer(source: str | Path | Any, min_chars: int = 20) -> bool:
    """스캔본(텍스트 레이어 없음) 여부를 판단해 호출자가 경로를 고를 수 있게 한다."""
    with _open_pdf(source) as pdf:
        total = sum(len((page.extract_text() or "")) for page in pdf.pages)
    return total >= min_chars


def build_html_report(tables: list[list[list[str]]], pages_text: list[str], source_label: str) -> str:
    pages_html = "".join(f"<pre>{text}</pre>" for text in pages_text)

    tables_html = []
    for index, grid in enumerate(tables, start=1):
        rows_html = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in grid
        )
        col_count = len(grid[0]) if grid else 0
        tables_html.append(f"""
        <section class="table-block">
          <h3>표 {index} <span class="badge">{len(grid)}행 x {col_count}열</span></h3>
          <div class="table-scroll"><table>{rows_html}</table></div>
        </section>
        """)

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>PDF 표 추출 결과 (pdfplumber)</title>
<style>
  body {{ font-family: "Malgun Gothic", sans-serif; margin: 24px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 20px; }}
  .notice {{ background: #e6f4ea; border: 1px solid #b7ddc3; padding: 10px 14px; border-radius: 6px; margin-bottom: 18px; }}
  .table-block {{ margin-top: 24px; }}
  .badge {{ display: inline-block; font-size: 12px; background: #e6f4ea; color: #1e7d32; border-radius: 10px; padding: 2px 10px; margin-left: 8px; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; margin-top: 8px; }}
  td {{ border: 1px solid #999; padding: 6px 10px; font-size: 13px; white-space: nowrap; }}
  pre {{ background: #fff; border: 1px solid #ddd; padding: 10px; font-size: 12px; white-space: pre-wrap; }}
</style>
</head>
<body>
  <h1>PDF 표 추출 결과 (pdfplumber, OCR 미사용)</h1>
  <div class="notice">디지털 텍스트 레이어의 실제 셀 좌표로 표를 복원했습니다(병합 칸은 반복 채움). 원본: {source_label}</div>
  {"".join(tables_html)}
  <h2>표 밖 페이지 전체 텍스트(참고용)</h2>
  {pages_html}
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="pdfplumber 기반 디지털 PDF 견적서 표 추출")
    parser.add_argument("--pdf", required=True, help=".pdf 파일 경로")
    parser.add_argument("--output-json", help="표 그리드 JSON 저장 경로")
    parser.add_argument("--output-html", help="HTML 리포트 저장 경로")
    args = parser.parse_args()

    if not has_digital_text_layer(args.pdf):
        print("경고: 텍스트 레이어가 거의 없습니다 (스캔본으로 추정). 비전/OCR 경로를 쓰세요.")

    tables = extract_tables_from_pdf(args.pdf)
    pages_text = extract_page_text_outside_tables(args.pdf)

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"표 그리드 JSON 저장: {args.output_json}")

    if args.output_html:
        html = build_html_report(tables, pages_text, source_label=args.pdf)
        Path(args.output_html).write_text(html, encoding="utf-8")
        print(f"HTML 리포트 저장: {args.output_html}")

    print(f"찾은 표 개수: {len(tables)}")
    for index, grid in enumerate(tables, start=1):
        print(f"  표 {index}: {len(grid)}행 x {len(grid[0]) if grid else 0}열")


if __name__ == "__main__":
    main()
