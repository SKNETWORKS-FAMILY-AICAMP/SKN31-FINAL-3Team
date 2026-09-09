"""table_structure_extractor.py를 실제 견적서 없이 검증하기 위한 합성 표 이미지 생성기.

병합 헤더(colspan), 인쇄체 항목 행, 그리고 저품질(흐림+저대비)로 렌더링한 셀을
하나씩 넣어서 PaddleOCR의 인식 점수가 낮게 나오는 상황(=비전 모델 재확인 대상)
까지 재현한다. 실제 거래 정보가 없는 순수 테스트용 더미 데이터만 사용한다.

단독 실행 예:
    python -m backend_logic2.nodes.quotation.quotation_filter.make_sample_table_image `
      --output sample_quotation_table.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

DEFAULT_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\malgun.ttf",
    r"C:\Windows\Fonts\malgunbd.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in DEFAULT_FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def build_sample_table_image() -> Image.Image:
    width, height = 760, 360
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _load_font(18)
    title_font = _load_font(20)

    col_edges = [40, 340, 460, 580, 720]
    row_edges = [40, 90, 140, 190, 240, 290]

    def cell_box(row: int, col_start: int, col_end: int) -> tuple[int, int, int, int]:
        return (col_edges[col_start], row_edges[row], col_edges[col_end], row_edges[row + 1])

    # 병합 헤더(colspan=4): 표 구조 인식이 rowspan/colspan을 얼마나 잘 잡는지 확인용
    draw.rectangle(cell_box(0, 0, 4), outline="black", width=2)
    draw.text((col_edges[0] + 10, row_edges[0] + 12), "견적서 - 품목 상세 (테스트용 더미 데이터)", font=title_font, fill="black")

    headers = ["품목명", "규격", "수량", "단가"]
    for col, text in enumerate(headers):
        box = cell_box(1, col, col + 1)
        draw.rectangle(box, outline="black", width=2)
        draw.text((box[0] + 8, box[1] + 12), text, font=font, fill="black")

    rows = [
        ["스테인리스 볼트", "SUS304 10mm", "100", "1,200"],
        ["알루미늄 브라켓", "AL6061 20T", "50", "3,500"],
        ["고무 개스킷", "NBR 5mm", "200", "450"],
    ]
    for row_index, values in enumerate(rows, start=2):
        for col, text in enumerate(values):
            box = cell_box(row_index, col, col + 1)
            draw.rectangle(box, outline="black", width=2)
            draw.text((box[0] + 8, box[1] + 12), text, font=font, fill="black")

    # 마지막 행의 수량 셀만 흐리고 저대비로 렌더링해 PaddleOCR 인식 점수를
    # 일부러 낮춘다 -> table_structure_extractor의 저신뢰 셀(needs_review) 경로 재현.
    blurry_box = cell_box(4, 2, 3)
    blur_layer = Image.new("RGB", image.size, "white")
    blur_draw = ImageDraw.Draw(blur_layer)
    blur_draw.text((blurry_box[0] + 8, blurry_box[1] + 12), "37", font=font, fill=(210, 210, 210))
    blur_layer = blur_layer.filter(ImageFilter.GaussianBlur(2.2))
    image.paste(blur_layer.crop(blurry_box), blurry_box)

    return image


def main() -> None:
    parser = argparse.ArgumentParser(description="table_structure_extractor.py 테스트용 합성 견적서 표 이미지 생성")
    parser.add_argument("--output", default="sample_quotation_table.png", help="저장할 PNG 경로")
    args = parser.parse_args()

    image = build_sample_table_image()
    image.save(args.output)
    print(f"샘플 표 이미지 저장 완료: {args.output}")


if __name__ == "__main__":
    main()
