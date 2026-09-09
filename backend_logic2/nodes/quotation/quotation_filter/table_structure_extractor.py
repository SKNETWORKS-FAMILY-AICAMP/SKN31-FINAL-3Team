"""PP-StructureV3(표 구조 인식) + 로컬 VLM을 결합한 하이브리드 표 추출기.

quotation_extractor.py의 비전 파이프라인은 페이지 이미지 전체를 한 번에 VLM에
넣어 "보이는 대로 옮겨 적으라"고 지시한다. 표가 단순하면 잘 동작하지만, 병합
셀이나 표 안에 또 다른 표가 있는 문서에서는 셀 경계 자체를 모델이 잘못 해석해
행/열이 밀리는 문제가 있다.

이 모듈은 그 문제를 구조 인식과 텍스트 인식으로 분리해서 해결한다.

    1. PaddleOCR의 ``TableRecognitionPipelineV2``(PP-StructureV3와 같은 표 인식
       엔진)로 페이지에서 표 영역을 찾고, 각 표의 셀 좌표(``cell_box_list``)와
       병합 셀 구조가 반영된 HTML(``pred_html``)을 얻는다. 표 안에 시각적으로
       또 다른 표가 있는 문서는 레이아웃 탐지 단계에서 별도 표 영역으로 잡혀
       ``table_res_list``에 항목이 여러 개로 나뉘어 나온다. 즉 "중첩 표"는
       하나의 재귀적 HTML이 아니라 여러 개의 개별 표로 표현된다.
    2. 각 셀에 대해 PaddleOCR이 함께 반환한 인식 점수(``rec_scores``)를 확인해,
       점수가 낮거나 아예 텍스트가 잡히지 않은 셀만 그 부분만 크롭해서 로컬
       VLM(quotation_extractor.LocalHuggingFaceQuotationParser의 비전 모델)에게
       다시 읽힌다. 인쇄체가 대부분인 표에서 비전 모델 호출 횟수를 셀 전체가
       아니라 애매한 셀 몇 개로 줄이는 것이 목적이다.
    3. 비전 모델이 다시 읽은 셀 텍스트만 ``pred_html``에 치환해 ``refined_html``
       을 만든다. 병합 셀의 rowspan/colspan 정보는 PaddleOCR 구조 인식 결과를
       그대로 쓰므로 별도로 복원할 필요가 없다.

보안 원칙(quotation_extractor.py와 동일):
    - 표 이미지·셀 이미지를 외부 API로 전송하지 않는다.
    - 텍스트 재인식에는 quotation_extractor.py가 이미 로컬 전용으로 로드하는
      Hugging Face 비전 모델만 재사용한다(별도 원격 모델을 새로 부르지 않음).
    - PaddleOCR/PaddleX는 지정한 model_dir이 없으면 최초 실행 시 공개 모델
      가중치를 자사 서버에서 자동 내려받는다. 이 과정에서 문서 내용이 전송되지
      않지만, 완전 폐쇄망 운영이 필요하면 PPOCR_TABLE_PIPELINE_OVERRIDES
      환경변수로 사내에 미리 내려받아 둔 모델 디렉터리를 지정해야 한다.

주의(실 테스트로 확인한 함정):
    TableRecognitionPipelineV2는 기본값으로 중국어 인식 모델을 쓴다. 언어를
    지정하지 않으면 한글이 비슷하게 생긴 한자로 잘못 인식되거나(예: "품목"이
    "苦号"로 인식) 아예 빈 텍스트로 나온다. 그래서 이 모듈은 기본적으로
    text_recognition_model_name="korean_PP-OCRv5_mobile_rec"를 지정한다.

설치(이 저장소 requirements.txt에는 아직 포함되어 있지 않다):
    pip install paddleocr paddlepaddle   # GPU 환경이면 paddlepaddle-gpu

단독 실행 예:
    python -m backend_logic2.nodes.quotation.quotation_filter.table_structure_extractor `
      "C:/quotes/vendor-a.png" --output tables.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
from html import escape as escape_html
from typing import Any, Callable

from PIL import Image
from pydantic import BaseModel, ConfigDict

try:
    from .quotation_extractor import LocalHuggingFaceQuotationParser, _move_inputs_to_device
    from .quotation_models import dump_json
except ImportError:  # nodes 폴더에서 직접 실행할 때
    from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
        LocalHuggingFaceQuotationParser,
        _move_inputs_to_device,
    )
    from backend_logic2.nodes.quotation.quotation_filter.quotation_models import dump_json


_TD_PATTERN = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.90
DEFAULT_CONTAINMENT_THRESHOLD = 0.5


class TableCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bbox: list[float]
    text: str
    source: str  # "paddle_ocr" | "local_vision_model" | "empty"
    ocr_score: float | None
    needs_review: bool


class ExtractedTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_region_id: int | None
    neighbor_text: str | None
    cell_count: int
    vision_reviewed_count: int
    cells: list[TableCell]
    structure_html: str
    refined_html: str


def _to_bgr_array(image: Image.Image):
    import numpy as np

    return np.array(image.convert("RGB"))[:, :, ::-1].copy()


def _box_area(box: list[float]) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def _containment(cell_box: list[float], ocr_box: list[float]) -> float:
    """ocr_box 면적 중 cell_box 안에 들어오는 비율."""
    x1 = max(cell_box[0], ocr_box[0])
    y1 = max(cell_box[1], ocr_box[1])
    x2 = min(cell_box[2], ocr_box[2])
    y2 = min(cell_box[3], ocr_box[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ocr_area = _box_area(ocr_box)
    return inter / ocr_area if ocr_area > 0 else 0.0


def _match_cell_ocr(
    cell_box: list[float],
    rec_boxes: list[Any],
    rec_texts: list[str],
    rec_scores: list[float],
    containment_threshold: float = DEFAULT_CONTAINMENT_THRESHOLD,
) -> tuple[str, float | None]:
    """셀 영역 안에 들어오는 PaddleOCR 라인들을 좌->우로 이어 붙이고, 최저 점수를 셀 점수로 쓴다."""
    matched: list[tuple[float, str, float]] = []
    for box, text, score in zip(rec_boxes, rec_texts, rec_scores):
        box = [float(value) for value in box]
        if _containment(cell_box, box) >= containment_threshold:
            matched.append((box[0], str(text), float(score)))
    if not matched:
        return "", None
    matched.sort(key=lambda item: item[0])
    text = " ".join(part for _, part, _ in matched if part).strip()
    score = min(item[2] for item in matched)
    return text, score


def _crop_with_margin(
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    margin_ratio: float = 0.3,
    min_margin: int = 10,
) -> Image.Image:
    """셀 경계를 딱 맞춰 자르지 않고 약간의 여백을 두고 크롭한다.

    실측(Qwen2-VL-2B-Instruct, CPU)으로 확인한 내용: 셀 경계에 정확히 맞춘
    아주 작은 크롭(예: 132x55px)을 넣으면 모델이 "이미지를 읽을 수 없다"는
    정형화된 문구로 거절하는 경우가 많았다. 주변 픽셀을 약간 포함해서 여백을
    주면 실제로 글자를 옮겨 적었다. 그래서 셀 재확인 크롭에는 항상 이 여백을
    적용한다.
    """
    x1, y1, x2, y2 = bbox
    margin_x = max(min_margin, int((x2 - x1) * margin_ratio))
    margin_y = max(min_margin, int((y2 - y1) * margin_ratio))
    left = max(0, x1 - margin_x)
    top = max(0, y1 - margin_y)
    right = min(image.width, x2 + margin_x)
    bottom = min(image.height, y2 + margin_y)
    return image.crop((left, top, right, bottom))


# 셀 하나에 들어갈 값(품목명/규격/숫자 등)치고 비정상적으로 긴 응답은 표 셀 값이
# 아니라 딴소리(장문 설명 등)일 가능성이 높아 거절 여부와 무관하게 버린다.
_MAX_PLAUSIBLE_CELL_LENGTH = 80
_REFUSAL_MARKERS = (
    "죄송", "미안", "할 수 없", "지원하지", "처리할 수", "sorry", "cannot", "can't", "i can not",
    "申し訳", "aiでき", "できません",
)


def _looks_like_refusal_or_junk(text: str) -> bool:
    """실측(Qwen2-VL-2B-Instruct, CPU)으로 확인한 문제: 비전 모델이 셀 값 대신

    "죄송합니다, 저는 이미지를 처리할 수 없습니다" 같은 정형화된 거절 문장을
    내놓는 경우가 잦았다(13번 중 10번). 이런 응답을 검증 없이 그대로 받아
    pred_html에 꽂으면, 원래 있던(비록 틀렸어도) OCR 추정값보다 더 나쁜
    엉뚱한 문장이 최종 결과에 섞여 들어간다. 셀 값치고 너무 길거나 거절
    문구 특유의 표현이 섞여 있으면 무시하고 기존 OCR 결과를 유지한다.
    """
    if len(text) > _MAX_PLAUSIBLE_CELL_LENGTH:
        return True
    lowered = text.lower()
    return any(marker in text or marker in lowered for marker in _REFUSAL_MARKERS)


def _rebuild_html(pred_html: str, overrides: dict[int, str]) -> str:
    """비전 모델이 재확인한 셀만 골라 pred_html의 <td> 내용을 치환한다.

    PaddleOCR 구조 인식 결과의 <td> 등장 순서는 cell_box_list와 같은 순서로
    정렬되어 있으므로(둘 다 위->아래, 왼쪽->오른쪽 행 우선 정렬), 등장 순서
    인덱스로 안전하게 짝지을 수 있다.
    """
    counter = {"index": 0}

    def _substitute(match: re.Match) -> str:
        index = counter["index"]
        counter["index"] += 1
        override_text = overrides.get(index)
        if override_text is None:
            return match.group(0)
        return f"<td{match.group(1)}>{escape_html(override_text)}</td>"

    return _TD_PATTERN.sub(_substitute, pred_html)


def _transcribe_cell(parser: LocalHuggingFaceQuotationParser, cell_image: Image.Image) -> str:
    """quotation_extractor의 로컬 비전 모델을 재사용해 셀 하나만 다시 읽는다."""
    parser._load_vision_model()
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "image": cell_image},
            {"type": "text", "text": (
                "이 표 셀 이미지 안의 글자만 그대로 옮겨 적으세요. 인쇄체든 손글씨든 "
                "보이는 문자, 숫자, 기호를 있는 그대로 전사하고 해석하거나 고치지 마세요. "
                "셀이 비어 있으면 아무것도 출력하지 마세요."
            )},
        ],
    }]
    prompt = parser._vision_processor.apply_chat_template(messages, add_generation_prompt=True)
    inputs = parser._vision_processor(text=prompt, images=[cell_image], return_tensors="pt")
    inputs = _move_inputs_to_device(inputs, parser._device or "cpu")
    input_length = inputs["input_ids"].shape[-1]
    outputs = parser._vision_model.generate(
        **inputs,
        max_new_tokens=64,
        do_sample=False,
    )
    generated = outputs[:, input_length:]
    return parser._vision_processor.batch_decode(generated, skip_special_tokens=True)[0].strip()


# TableRecognitionPipelineV2에는 PPStructureV3와 달리 lang= 단축 옵션이 없어서
# 인식 모델 이름을 직접 지정해야 한다. 미지정 시 기본값은 중국어 모델이라
# 한글이 한자로 오인식되거나 통째로 빈 텍스트가 된다(실제 합성 표 이미지로
# 확인함: "품목" -> "苦号").
DEFAULT_TEXT_DETECTION_MODEL = "PP-OCRv5_server_det"
DEFAULT_TEXT_RECOGNITION_MODEL = "korean_PP-OCRv5_mobile_rec"


def _pipeline_overrides() -> dict[str, Any]:
    """PPOCR_TABLE_PIPELINE_OVERRIDES에 지정한 값만 파이프라인 생성자에 그대로 넘긴다.

    TableRecognitionPipelineV2 생성자가 받는 어떤 키워드도(예: 특정 서브모델의
    model_dir을 사내 로컬 경로로 고정) 이 JSON으로 지정할 수 있다. 지정하지
    않으면 PaddleX가 최초 실행 시 공식 모델 저장소에서 공개 가중치를 자동으로
    내려받는다(문서 내용 전송 없음). 완전 폐쇄망 환경에서는 미리 내려받은 모델
    디렉터리를 이 환경변수로 지정해야 한다.
    """
    raw = os.getenv("PPOCR_TABLE_PIPELINE_OVERRIDES")
    if not raw:
        return {}
    overrides = json.loads(raw)
    if not isinstance(overrides, dict):
        raise ValueError("PPOCR_TABLE_PIPELINE_OVERRIDES는 JSON 객체여야 합니다.")
    return overrides


_TABLE_PIPELINE: Any = None


def get_table_pipeline() -> Any:
    global _TABLE_PIPELINE
    if _TABLE_PIPELINE is None:
        try:
            from paddleocr import TableRecognitionPipelineV2
        except ImportError as exc:
            raise RuntimeError(
                "표 구조 인식에는 paddleocr와 paddlepaddle이 필요합니다. "
                "`pip install paddleocr paddlepaddle`(GPU는 paddlepaddle-gpu)로 설치하세요."
            ) from exc
        overrides = _pipeline_overrides()
        overrides.setdefault("text_detection_model_name", DEFAULT_TEXT_DETECTION_MODEL)
        overrides.setdefault("text_recognition_model_name", DEFAULT_TEXT_RECOGNITION_MODEL)
        _TABLE_PIPELINE = TableRecognitionPipelineV2(**overrides)
    return _TABLE_PIPELINE


def extract_tables(
    image: Image.Image,
    *,
    vision_transcribe: Callable[[Image.Image], str] | None = None,
    low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
) -> list[ExtractedTable]:
    """이미지 한 장에서 표를 모두 찾아 구조(HTML)와 셀 텍스트를 추출한다.

    vision_transcribe를 넘기지 않으면 PaddleOCR 인식 결과만 사용한다(빠르지만
    손글씨나 저해상도 셀은 부정확할 수 있음). 넘기면 저신뢰/빈 셀만 크롭해
    해당 콜백으로 다시 읽혀 refined_html에 반영한다.
    """
    pipeline = get_table_pipeline()
    results = pipeline.predict(_to_bgr_array(image))
    if not results:
        return []

    page_result = results[0]
    tables: list[ExtractedTable] = []
    for table_res in page_result["table_res_list"]:
        cell_boxes = [[float(v) for v in box] for box in table_res["cell_box_list"]]
        pred_html = table_res["pred_html"]
        ocr_pred = table_res["table_ocr_pred"] or {}
        rec_boxes = list(ocr_pred.get("rec_boxes", []))
        rec_texts = list(ocr_pred.get("rec_texts", []))
        rec_scores = list(ocr_pred.get("rec_scores", []))

        cells: list[TableCell] = []
        overrides: dict[int, str] = {}
        for index, bbox in enumerate(cell_boxes):
            ocr_text, ocr_score = _match_cell_ocr(bbox, rec_boxes, rec_texts, rec_scores)
            needs_review = ocr_score is None or ocr_score < low_confidence_threshold or not ocr_text.strip()
            text, source = (ocr_text, "paddle_ocr") if ocr_text.strip() else ("", "empty")

            # ocr_score가 None이면 PaddleOCR의 글자 "탐지" 모델(인식이 아니라 탐지 단계)이
            # 이 칸에서 아예 아무 글자 영역도 찾지 못했다는 뜻이다. 픽셀 밝기로 빈 칸을
            # 걸러보려 했으나(격자선/스캔 노이즈 때문에 실측 실패, 진짜 빈 칸도 10~20%
            # "잉크"로 잡혀 기준을 세울 수 없었음) 폐기하고, 대신 이미 훈련된 탐지
            # 모델의 판단을 신뢰한다 — 탐지 자체가 실패한 칸까지 전부 비전 모델로
            # 재확인하면(빈 칸이 많은 서식에서) CPU 기준 30분 넘게 걸리는 것을 실측했다.
            # 단점: 아주 희미한 손글씨는 탐지 단계에서부터 놓치면 이 최적화로 인해
            # 비전 모델 재확인 기회조차 못 받는다 — 속도와 재현율의 트레이드오프다.
            if needs_review and vision_transcribe is not None and ocr_score is not None:
                x1, y1, x2, y2 = (int(round(v)) for v in bbox)
                if x2 > x1 and y2 > y1:
                    vision_text = vision_transcribe(_crop_with_margin(image, (x1, y1, x2, y2))).strip()
                    if vision_text and not _looks_like_refusal_or_junk(vision_text):
                        text, source = vision_text, "local_vision_model"
                        overrides[index] = vision_text

            cells.append(TableCell(
                bbox=bbox,
                text=text,
                source=source,
                ocr_score=ocr_score,
                needs_review=needs_review,
            ))

        tables.append(ExtractedTable(
            table_region_id=table_res.get("table_region_id"),
            neighbor_text=(table_res.get("neighbor_texts") or "").strip() or None,
            cell_count=len(cells),
            vision_reviewed_count=len(overrides),
            cells=cells,
            structure_html=pred_html,
            refined_html=_rebuild_html(pred_html, overrides) if overrides else pred_html,
        ))
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PP-Structure 표 구조 인식 + 로컬 VLM 셀 재확인 하이브리드 추출기"
    )
    parser.add_argument("image", help="표가 포함된 견적서 이미지 경로(PNG/JPG 등)")
    parser.add_argument("--vision-model", help="셀 재확인에 쓸 로컬 비전 모델 경로(기본: HF_QUOTATION_VISION_MODEL)")
    parser.add_argument("--no-vision-review", action="store_true", help="PaddleOCR 결과만 쓰고 비전 모델 재확인을 건너뜀")
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        help="이 점수보다 낮은 PaddleOCR 인식 결과만 비전 모델로 재확인",
    )
    parser.add_argument("--output", help="결과 JSON 경로. 생략하면 stdout")
    args = parser.parse_args()

    image = Image.open(args.image).convert("RGB")

    vision_transcribe = None
    if not args.no_vision_review:
        vision_parser = LocalHuggingFaceQuotationParser(vision_model=args.vision_model)
        vision_transcribe = lambda cell_image: _transcribe_cell(vision_parser, cell_image)

    tables = extract_tables(
        image,
        vision_transcribe=vision_transcribe,
        low_confidence_threshold=args.confidence_threshold,
    )
    rendered = dump_json(tables, args.output)
    if not args.output:
        print(rendered)


if __name__ == "__main__":
    main()
