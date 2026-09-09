"""table_structure_verifier.py가 찾아낸 불일치를 원본 이미지 위에 노란 박스로
표시해서 눈으로 바로 확인할 수 있게 만드는 시각화 스크립트.

table_structure_extractor.py(구조 인식 결과)와 table_structure_verifier.py(순수
OCR 재검증 결과)를 각각 실행한 뒤, 이 스크립트에 원본 이미지와 검증 결과 JSON을
넣으면 된다. 순수 로컬 파일 처리만 하며 외부로 아무것도 전송하지 않는다.

단독 실행 예:
    python -m backend_logic2.nodes.quotation.quotation_filter.visualize_verification_mismatches `
      --image page1.png --verification verification.json --output-html mismatches.html
"""

from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw

MISMATCH_COLOR = (230, 180, 0)  # 노란색


def _group_by_cell(mismatches: list[dict]) -> dict[tuple[int, int, int, int], dict]:
    """같은 셀을 가리키는 불일치 여러 건을 하나로 묶는다(셀 하나에 OCR 줄이 여러 개 겹칠 수 있음)."""
    groups: dict[tuple[int, int, int, int], dict] = {}
    for mismatch in mismatches:
        key = tuple(round(v) for v in mismatch["cell_bbox"])
        group = groups.setdefault(key, {
            "bbox": mismatch["cell_bbox"],
            "structured_text": mismatch["structured_text"],
            "independent_texts": [],
        })
        if mismatch["independent_text"] not in group["independent_texts"]:
            group["independent_texts"].append(mismatch["independent_text"])
    return groups


def draw_mismatches(image: Image.Image, verifications: list[dict]) -> tuple[Image.Image, list[dict]]:
    annotated = image.convert("RGB").copy()
    draw = ImageDraw.Draw(annotated)
    flat_groups: list[dict] = []

    for verification in verifications:
        groups = _group_by_cell(verification.get("mismatches", []))
        for group in groups.values():
            x1, y1, x2, y2 = (round(v) for v in group["bbox"])
            draw.rectangle([x1, y1, x2, y2], outline=MISMATCH_COLOR, width=3)
            flat_groups.append({
                "table_region_id": verification.get("table_region_id"),
                **group,
            })
    return annotated, flat_groups


def _image_to_data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_html_report(image: Image.Image, verifications: list[dict], source_label: str) -> str:
    annotated, groups = draw_mismatches(image, verifications)
    image_data_uri = _image_to_data_uri(annotated)

    total_checked = sum(v.get("checked_lines", 0) for v in verifications)
    total_mismatches = len(groups)

    rows_html = "".join(f"""
      <tr>
        <td>{g.get("table_region_id")}</td>
        <td>구조 인식 결과: <b>{g["structured_text"] or "(빈 칸)"}</b></td>
        <td>순수 OCR로 다시 읽은 값: <b>{", ".join(g["independent_texts"])}</b></td>
      </tr>
    """ for g in groups)

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>불일치 셀 확인</title>
<style>
  body {{ font-family: "Malgun Gothic", sans-serif; margin: 24px; background: #fafafa; color: #222; }}
  h1 {{ font-size: 20px; }}
  .notice {{ background: #fff3cd; border: 1px solid #ffe08a; padding: 10px 14px; border-radius: 6px; margin-bottom: 18px; }}
  .summary {{ margin-bottom: 16px; font-size: 14px; }}
  img.annotated {{ max-width: 100%; border: 1px solid #ccc; }}
  table {{ border-collapse: collapse; margin-top: 20px; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 8px 10px; font-size: 13px; text-align: left; }}
  th {{ background: #f0f0f0; }}
</style>
</head>
<body>
  <h1>구조 인식 vs 순수 OCR 재검증 - 불일치 셀</h1>
  <div class="notice">
    노란 박스로 표시된 칸은 표 구조 인식 결과와 순수 OCR 재검증 결과가 서로 다른 곳입니다.
    사람이 원본과 대조해서 확인해야 합니다. 이 페이지는 로컬 결과이며 ERPNext 등 외부 시스템에
    저장되지 않았습니다.
  </div>
  <div class="summary">원본: {source_label} · 대조한 줄 {total_checked}개 중 불일치 {total_mismatches}개 셀</div>
  <img class="annotated" src="{image_data_uri}" alt="불일치 셀 표시 이미지">
  <table>
    <thead><tr><th>표 영역</th><th colspan="2">불일치 내용</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="table_structure_verifier.py 결과의 불일치 셀 시각화")
    parser.add_argument("--image", required=True, help="원본 이미지 경로")
    parser.add_argument("--verification", required=True, help="table_structure_verifier.py가 만든 결과 JSON")
    parser.add_argument("--output-image", help="노란 박스만 그린 PNG 저장 경로(생략 가능)")
    parser.add_argument("--output-html", default="mismatch_report.html", help="HTML 리포트 저장 경로")
    args = parser.parse_args()

    image = Image.open(args.image)
    verifications = json.loads(Path(args.verification).read_text(encoding="utf-8"))

    if args.output_image:
        annotated, _ = draw_mismatches(image, verifications)
        annotated.save(args.output_image)
        print(f"불일치 표시 이미지 저장: {args.output_image}")

    html = build_html_report(image, verifications, source_label=args.image)
    Path(args.output_html).write_text(html, encoding="utf-8")
    print(f"HTML 리포트 저장: {args.output_html}")


if __name__ == "__main__":
    main()
