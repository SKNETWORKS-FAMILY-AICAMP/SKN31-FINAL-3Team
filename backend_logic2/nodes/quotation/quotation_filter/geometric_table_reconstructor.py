"""표 구조 인식 모델(SLANeXt/셀 탐지) 대신, 순수 OCR로 읽은 글자 위치만으로
행/열을 직접 계산해서 표를 다시 만드는 대안 추출기.

왜 필요한가:
    table_structure_verifier.py로 실측 확인한 내용 — 행 간격이 좁은 문서에서는
    PaddleOCR의 셀 경계 탐지(RT-DETR)가 여러 행을 하나의 칸으로 뭉쳐버리는
    경우가 있었다. 반면 순수 OCR(글자 위치만 인식, 행/열 추론 없음)은 같은
    문서에서 글자 자체는 정확히, 줄 단위로 구분해서 읽어냈다.

    순수 OCR은 "이 위치에 이 글자가 있다"까지만 알려주고 표 모양으로 정리하지는
    않는다. 이 스크립트는 그 정리를 학습된 모델이 아니라 좌표 계산으로 직접
    한다:
        1. 헤더 행(맨 위 줄)에 있는 글자들의 x좌표로 열 경계를 정한다.
        2. 나머지 글자들을 y좌표로 묶어서 행을 나눈다(줄 높이를 기준으로 묶음
           허용 오차를 자동으로 정한다).
        3. 각 글자를 x좌표가 속한 열, y좌표가 속한 행에 배치한다.

    이 방식은 학습된 셀 탐지 모델의 오탐(행을 뭉개는 등)에 영향받지 않는다.
    다만 진짜로 셀이 병합된 경우(예: 세부규격 칸 안에 사이즈/색상/재질이 함께
    있는 경우)는 각각 별도의 행으로 나뉘어 나온다 — 표 구조 인식 결과처럼
    한 셀로 합쳐주지는 않지만, 값이 엉뚱한 칸으로 밀리지도 않는다.

단독 실행 예:
    python -m backend_logic2.nodes.quotation.quotation_filter.geometric_table_reconstructor `
      --image page1.png --output-html geometric_table.html
"""

from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

try:
    from .table_structure_verifier import get_plain_ocr_engine
except ImportError:  # nodes 폴더에서 직접 실행할 때
    from backend_logic2.nodes.quotation.quotation_filter.table_structure_verifier import get_plain_ocr_engine


def run_ocr_lines(image: Image.Image, ocr_engine: Any) -> list[dict]:
    """이미지 전체(또는 잘라낸 영역)에서 순수 OCR로 글자 줄 목록을 얻는다."""
    import numpy as np

    bgr = np.array(image.convert("RGB"))[:, :, ::-1].copy()
    results = ocr_engine.predict(bgr)
    if not results:
        return []
    page = results[0]
    lines = []
    for box, text, score in zip(page.get("rec_boxes", []), page.get("rec_texts", []), page.get("rec_scores", [])):
        text = str(text).strip()
        if not text:
            continue
        x1, y1, x2, y2 = (float(v) for v in box)
        lines.append({"bbox": [x1, y1, x2, y2], "text": text, "score": float(score)})
    return lines


def _y_center(line: dict) -> float:
    return (line["bbox"][1] + line["bbox"][3]) / 2


def _x_center(line: dict) -> float:
    return (line["bbox"][0] + line["bbox"][2]) / 2


def cluster_rows(lines: list[dict]) -> list[list[dict]]:
    """y좌표가 비슷한 줄들을 하나의 행으로 묶는다. 허용 오차는 줄 높이의 중간값으로 자동 계산한다."""
    if not lines:
        return []
    heights = [line["bbox"][3] - line["bbox"][1] for line in lines]
    heights.sort()
    median_height = heights[len(heights) // 2]
    tolerance = max(6.0, median_height * 0.6)

    ordered = sorted(lines, key=_y_center)
    rows: list[list[dict]] = [[ordered[0]]]
    row_y = [_y_center(ordered[0])]
    for line in ordered[1:]:
        y = _y_center(line)
        if abs(y - row_y[-1]) <= tolerance:
            rows[-1].append(line)
            row_y[-1] = sum(_y_center(item) for item in rows[-1]) / len(rows[-1])
        else:
            rows.append([line])
            row_y.append(y)
    for row in rows:
        row.sort(key=_x_center)
    return rows


def derive_columns(header_row: list[dict], table_left: float, table_right: float) -> list[tuple[float, float]]:
    """헤더 행 글자들의 x중심 사이 중간 지점을 열 경계로 삼는다."""
    centers = sorted(_x_center(line) for line in header_row)
    boundaries = [table_left]
    for a, b in zip(centers, centers[1:]):
        boundaries.append((a + b) / 2)
    boundaries.append(table_right)
    return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]


def _column_index(x_center: float, columns: list[tuple[float, float]]) -> int:
    for index, (left, right) in enumerate(columns):
        if left <= x_center < right:
            return index
    return len(columns) - 1 if x_center >= columns[-1][1] else 0


def build_grid(lines: list[dict]) -> tuple[list[list[str]], list[list[dict]], list[tuple[float, float]]]:
    """순수 OCR 줄 목록을 행 x 열 텍스트 표로 재구성한다."""
    rows = cluster_rows(lines)
    if not rows:
        return [], [], []

    xs = [v for line in lines for v in (line["bbox"][0], line["bbox"][2])]
    table_left, table_right = min(xs), max(xs)
    columns = derive_columns(rows[0], table_left, table_right)

    grid: list[list[str]] = []
    grid_lines: list[list[dict]] = []
    for row in rows:
        cells: list[list[str]] = [[] for _ in columns]
        cell_lines: list[list[dict]] = [[] for _ in columns]
        for line in row:
            col = _column_index(_x_center(line), columns)
            cells[col].append(line["text"])
            cell_lines[col].append(line)
        grid.append([" ".join(parts) for parts in cells])
        grid_lines.append(cell_lines)
    return grid, grid_lines, columns


def _image_to_data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


def _draw_grid_overlay(image: Image.Image, grid_lines: list[list[dict]]) -> Image.Image:
    annotated = image.convert("RGB").copy()
    draw = ImageDraw.Draw(annotated)
    for row in grid_lines:
        for cell in row:
            for line in cell:
                x1, y1, x2, y2 = (round(v) for v in line["bbox"])
                draw.rectangle([x1, y1, x2, y2], outline=(0, 140, 200), width=2)
    return annotated


def build_html_report(image: Image.Image, grid: list[list[str]], grid_lines: list[list[dict]], source_label: str) -> str:
    annotated = _draw_grid_overlay(image, grid_lines)
    image_data_uri = _image_to_data_uri(annotated)

    rows_html = "".join(
        "<tr>" + "".join(f"<td>{cell or ''}</td>" for cell in row) + "</tr>"
        for row in grid
    )

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>좌표 기반 표 재구성 결과</title>
<style>
  body {{ font-family: "Malgun Gothic", sans-serif; margin: 24px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 20px; }}
  .notice {{ background: #e8f0fe; border: 1px solid #b6d0fb; padding: 10px 14px; border-radius: 6px; margin-bottom: 18px; }}
  img.annotated {{ max-width: 100%; border: 1px solid #ccc; margin-bottom: 20px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ border: 1px solid #999; padding: 6px 10px; font-size: 13px; }}
</style>
</head>
<body>
  <h1>좌표 기반 표 재구성 결과 (표 구조 모델 미사용)</h1>
  <div class="notice">
    학습된 셀 탐지 모델을 쓰지 않고, 순수 OCR로 읽은 글자 위치만으로 행/열을 계산했습니다.
    파란 박스는 이 계산에 쓰인 글자 줄입니다. 이 페이지는 로컬 결과이며 ERPNext 등
    외부 시스템에 저장되지 않았습니다.
  </div>
  <img class="annotated" src="{image_data_uri}" alt="글자 줄 표시 이미지">
  <table>{rows_html}</table>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="좌표 기반 표 재구성(표 구조 모델 없이)")
    parser.add_argument("--image", required=True, help="원본 이미지 경로")
    parser.add_argument("--crop", nargs=4, type=float, metavar=("X1", "Y1", "X2", "Y2"), help="표 영역만 자를 좌표(생략하면 전체 이미지 사용)")
    parser.add_argument("--output-json", help="행x열 텍스트 그리드 JSON 저장 경로")
    parser.add_argument("--output-html", default="geometric_table.html", help="HTML 리포트 저장 경로")
    args = parser.parse_args()

    image = Image.open(args.image)
    if args.crop:
        x1, y1, x2, y2 = args.crop
        image = image.crop((x1, y1, x2, y2))

    engine = get_plain_ocr_engine()
    lines = run_ocr_lines(image, engine)
    grid, grid_lines, _ = build_grid(lines)

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(grid, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"그리드 JSON 저장: {args.output_json}")

    html = build_html_report(image, grid, grid_lines, source_label=args.image)
    Path(args.output_html).write_text(html, encoding="utf-8")
    print(f"HTML 리포트 저장: {args.output_html}")
    print(f"인식된 행 수: {len(grid)}, 열 수: {len(grid[0]) if grid else 0}")


if __name__ == "__main__":
    main()
