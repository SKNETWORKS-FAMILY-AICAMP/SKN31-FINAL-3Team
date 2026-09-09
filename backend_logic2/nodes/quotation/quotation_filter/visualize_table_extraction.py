"""table_structure_extractor.py 결과를 눈으로 확인할 수 있게 만드는 시각화 스크립트.

이 스크립트는 순수 로컬 파일 처리만 한다. ERPNext나 다른 외부 시스템에 아무것도
읽거나 쓰지 않는다 — table_structure_extractor.py 자체도 아직
quotation_extractor.py/quotation_registrar.py의 ERPNext 등록 파이프라인에
연결되어 있지 않으므로, 지금까지 만든 모든 결과는 로컬 이미지/JSON 파일로만
존재하고 실제 ERPNext Supplier Quotation으로 등록된 적이 없다.

만드는 결과:
    1. 원본 이미지 위에 인식된 셀 경계를 색깔별로 그린 PNG
       (초록=PaddleOCR가 바로 인식, 파랑=비전 모델이 재확인, 빨강=끝까지 못 읽음)
    2. 그 이미지와 표 내용(HTML)을 한 화면에 담은 자체완결형 HTML 리포트.
       인터넷이나 서버 없이 더블클릭만으로 브라우저에서 바로 열린다.

단독 실행 예:
    python -m backend_logic2.nodes.quotation.quotation_filter.visualize_table_extraction `
      --image page1.png --result result.json --output-html report.html
"""

from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLOR_BY_SOURCE = {
    "paddle_ocr": (34, 139, 34),          # 초록: PaddleOCR가 바로 인식
    "local_vision_model": (30, 100, 220),  # 파랑: 비전 모델이 재확인해서 채움
    "empty": (200, 30, 30),               # 빨강: 끝까지 텍스트를 못 찾음
}
LEGEND = [
    ("paddle_ocr", "PaddleOCR가 바로 인식"),
    ("local_vision_model", "비전 모델이 재확인해서 채움"),
    ("empty", "끝까지 텍스트를 못 찾음"),
]


def _load_font(size: int) -> ImageFont.ImageFont:
    for candidate in (r"C:\Windows\Fonts\malgun.ttf", r"C:\Windows\Fonts\malgunbd.ttf"):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def draw_cell_boxes(image: Image.Image, tables: list[dict]) -> Image.Image:
    """원본 이미지 위에 셀 경계만 색깔별로 그린다(원본 글자를 가리지 않도록 텍스트는 넣지 않음)."""
    annotated = image.convert("RGB").copy()
    draw = ImageDraw.Draw(annotated)
    for table in tables:
        for cell in table["cells"]:
            x1, y1, x2, y2 = (round(v) for v in cell["bbox"])
            color = COLOR_BY_SOURCE.get(cell["source"], (128, 128, 128))
            draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
    return annotated


def _image_to_data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_html_report(image: Image.Image, tables: list[dict], source_label: str) -> str:
    annotated = draw_cell_boxes(image, tables)
    image_data_uri = _image_to_data_uri(annotated)

    legend_html = "".join(
        f'<span class="legend-item"><span class="swatch" style="background: rgb{COLOR_BY_SOURCE[key]}"></span>{label}</span>'
        for key, label in LEGEND
    )

    tables_html = []
    for table in tables:
        html_to_show = table.get("refined_html") or table.get("structure_html") or ""
        tables_html.append(f"""
        <section class="table-block">
          <h3>표 영역 #{table.get("table_region_id")}
            <span class="badge">셀 {table.get("cell_count")}개</span>
            <span class="badge badge-blue">비전 재확인 {table.get("vision_reviewed_count")}개</span>
          </h3>
          {f'<p class="neighbor">주변 텍스트: {table["neighbor_text"]}</p>' if table.get("neighbor_text") else ""}
          <div class="table-scroll">{html_to_show}</div>
        </section>
        """)

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>표 추출 결과 확인</title>
<style>
  body {{ font-family: "Malgun Gothic", sans-serif; margin: 24px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 20px; }}
  .notice {{ background: #fff3cd; border: 1px solid #ffe08a; padding: 10px 14px; border-radius: 6px; margin-bottom: 18px; }}
  .legend {{ margin: 12px 0 20px; }}
  .legend-item {{ display: inline-flex; align-items: center; margin-right: 18px; font-size: 13px; }}
  .swatch {{ display: inline-block; width: 14px; height: 14px; margin-right: 6px; border-radius: 3px; }}
  .source-label {{ color: #666; font-size: 13px; margin-bottom: 6px; }}
  img.annotated {{ max-width: 100%; border: 1px solid #ccc; }}
  .table-block {{ margin-top: 28px; }}
  .badge {{ display: inline-block; font-size: 12px; background: #e6f4ea; color: #1e7d32; border-radius: 10px; padding: 2px 10px; margin-left: 8px; }}
  .badge-blue {{ background: #e8f0fe; color: #1a56c4; }}
  .neighbor {{ color: #666; font-size: 13px; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; margin-top: 8px; }}
  table td {{ border: 1px solid #999; padding: 6px 10px; font-size: 13px; white-space: nowrap; }}
</style>
</head>
<body>
  <h1>표 추출 결과 확인</h1>
  <div class="notice">
    이 페이지는 로컬에서 생성한 결과이며, ERPNext 등 외부 시스템에 저장되지 않았습니다.
  </div>
  <div class="source-label">원본: {source_label}</div>
  <div class="legend">{legend_html}</div>
  <img class="annotated" src="{image_data_uri}" alt="셀 경계 표시 이미지">
  {"".join(tables_html)}
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="table_structure_extractor.py 결과 시각화")
    parser.add_argument("--image", required=True, help="table_structure_extractor.py에 넣었던 원본 이미지 경로")
    parser.add_argument("--result", required=True, help="table_structure_extractor.py가 만든 결과 JSON 경로")
    parser.add_argument("--output-image", help="셀 경계만 그린 PNG 저장 경로(생략 가능)")
    parser.add_argument("--output-html", default="table_extraction_report.html", help="HTML 리포트 저장 경로")
    args = parser.parse_args()

    image = Image.open(args.image)
    tables = json.loads(Path(args.result).read_text(encoding="utf-8"))

    if args.output_image:
        annotated = draw_cell_boxes(image, tables)
        annotated.save(args.output_image)
        print(f"셀 경계 이미지 저장: {args.output_image}")

    html = build_html_report(image, tables, source_label=args.image)
    Path(args.output_html).write_text(html, encoding="utf-8")
    print(f"HTML 리포트 저장: {args.output_html} (더블클릭해서 브라우저로 열면 됩니다)")


if __name__ == "__main__":
    main()
