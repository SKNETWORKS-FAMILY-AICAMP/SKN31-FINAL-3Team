"""table_structure_extractor.py의 표 구조 인식 결과를, 구조를 전혀 모르는 순수
OCR로 한 번 더 대조해서 검증하는 2차 확인 스크립트.

왜 필요한가:
    실측으로 확인한 문제 — 표 구조 인식 모델(SLANeXt)은 셀 안에 미니 표가 또
    들어있는 것처럼 아주 불규칙한 레이아웃을 만나면 rowspan/colspan을 잘못
    예측한다. 이때 글자 인식 자체는 맞았는데도 값이 엉뚱한 행의 칸으로 붙어버려
    표가 틀어진다(예: "1", "산업용작업장갑"이 병합 칸이 아니라 그 아래 새 행에
    끼워짐). table_structure_extractor.py 코드가 셀 순서를 잘못 매칭한 게
    아니라, PaddleOCR이 만들어준 표 구조 자체가 이미 틀린 경우다.

어떻게 검증하는가:
    표 구조를 전혀 추론하지 않는 PaddleOCR의 기본 OCR 파이프라인(PaddleOCR
    클래스, 행/열 병합 추론 없이 글자 위치만 인식)을 같은 표 영역에 한 번 더
    돌린다. 이 순수 OCR이 찾은 글자 한 줄 한 줄의 실제 좌표가, 구조 인식
    결과에서 그 좌표에 해당하는 셀의 텍스트와 실제로 일치하는지 대조한다.
    일치하지 않으면(그 위치의 글자가 구조 인식 결과 어디에도 그대로 없으면)
    "불일치"로 표시해 사람 검토 대상으로 남긴다.

    참고: 두 인식 모두 같은 PaddleOCR 패키지를 쓰지만, 하나는 표 구조까지
    추론하는 무거운 파이프라인(TableRecognitionPipelineV2)이고 다른 하나는
    구조 추론이 전혀 없는 순수 OCR(PaddleOCR)이라 서로 독립적인 실패 양상을
    보인다 — 구조 추론이 틀려도 순수 OCR의 글자 위치 자체는 영향받지 않는다.

단독 실행 예:
    python -m backend_logic2.nodes.quotation.quotation_filter.table_structure_verifier `
      --image page1.png --result result.json --output verification.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict

try:
    from .table_structure_extractor import _containment
except ImportError:  # nodes 폴더에서 직접 실행할 때
    from backend_logic2.nodes.quotation.quotation_filter.table_structure_extractor import _containment


MIN_CONTAINMENT = 0.5
CROP_MARGIN = 15


class CellMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_bbox: list[float]
    structured_text: str
    independent_text: str


class TableVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_region_id: int | None
    checked_lines: int
    mismatches: list[CellMismatch]


_PLAIN_OCR_ENGINE: Any = None


def get_plain_ocr_engine() -> Any:
    """구조 추론이 전혀 없는 순수 OCR 엔진을 지연 로딩한다."""
    global _PLAIN_OCR_ENGINE
    if _PLAIN_OCR_ENGINE is None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise RuntimeError(
                "검증에는 paddleocr와 paddlepaddle이 필요합니다. "
                "`pip install paddleocr paddlepaddle`로 설치하세요."
            ) from exc
        _PLAIN_OCR_ENGINE = PaddleOCR(
            lang="korean",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _PLAIN_OCR_ENGINE


def _table_envelope(table: dict, margin: int = CROP_MARGIN) -> tuple[int, int, int, int]:
    """표에 속한 모든 셀 좌표를 감싸는 사각형을 여백을 두고 계산한다."""
    xs = [v for cell in table["cells"] for v in (cell["bbox"][0], cell["bbox"][2])]
    ys = [v for cell in table["cells"] for v in (cell["bbox"][1], cell["bbox"][3])]
    return (
        max(0, int(min(xs)) - margin),
        max(0, int(min(ys)) - margin),
        int(max(xs)) + margin,
        int(max(ys)) + margin,
    )


def verify_table(image: Image.Image, table: dict, ocr_engine: Any) -> TableVerification:
    import numpy as np

    left, top, right, bottom = _table_envelope(table)
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    bgr = np.array(crop)[:, :, ::-1].copy()

    results = ocr_engine.predict(bgr)
    if not results:
        return TableVerification(table_region_id=table.get("table_region_id"), checked_lines=0, mismatches=[])

    page = results[0]
    rec_boxes = list(page.get("rec_boxes", []))
    rec_texts = list(page.get("rec_texts", []))

    mismatches: list[CellMismatch] = []
    checked = 0
    for box, raw_text in zip(rec_boxes, rec_texts):
        text = str(raw_text).strip()
        if not text:
            continue
        # 크롭 기준 좌표를 원본 이미지 좌표로 되돌린다.
        line_box = [float(box[0]) + left, float(box[1]) + top, float(box[2]) + left, float(box[3]) + top]
        checked += 1

        best_cell, best_overlap = None, 0.0
        for cell in table["cells"]:
            overlap = _containment(cell["bbox"], line_box)
            if overlap > best_overlap:
                best_overlap, best_cell = overlap, cell
        if best_cell is None or best_overlap < MIN_CONTAINMENT:
            continue  # 표 테두리/캡션 등 셀 밖 글자는 대조 대상이 아님

        if text not in (best_cell.get("text") or ""):
            mismatches.append(CellMismatch(
                cell_bbox=best_cell["bbox"],
                structured_text=best_cell.get("text") or "",
                independent_text=text,
            ))

    return TableVerification(
        table_region_id=table.get("table_region_id"),
        checked_lines=checked,
        mismatches=mismatches,
    )


def verify_tables(image: Image.Image, tables: list[dict]) -> list[TableVerification]:
    engine = get_plain_ocr_engine()
    return [verify_table(image, table, engine) for table in tables]


def main() -> None:
    parser = argparse.ArgumentParser(description="표 구조 인식 결과를 순수 OCR로 재검증")
    parser.add_argument("--image", required=True, help="table_structure_extractor.py에 넣었던 원본 이미지")
    parser.add_argument("--result", required=True, help="table_structure_extractor.py가 만든 결과 JSON")
    parser.add_argument("--output", help="검증 결과 JSON 저장 경로. 생략하면 stdout")
    args = parser.parse_args()

    image = Image.open(args.image)
    tables = json.loads(Path(args.result).read_text(encoding="utf-8"))
    verifications = verify_tables(image, tables)

    payload = [v.model_dump() for v in verifications]
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"검증 결과 저장: {args.output}")
    else:
        print(rendered)

    total_mismatches = sum(len(v.mismatches) for v in verifications)
    total_checked = sum(v.checked_lines for v in verifications)
    if total_mismatches:
        print(f"\n총 {total_checked}줄 대조 중 불일치 {total_mismatches}건 발견 -> 사람 검토가 필요합니다.")
    else:
        print(f"\n총 {total_checked}줄 대조, 불일치 없음 -> 구조 인식 결과가 순수 OCR과 일치합니다.")


if __name__ == "__main__":
    main()
