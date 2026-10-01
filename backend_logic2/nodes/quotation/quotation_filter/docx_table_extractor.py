"""Word(.docx) 견적서에서 표를 직접 읽는 추출기.

이미지/PDF용 추출기(table_structure_extractor.py 등)와 근본적으로 다르다.
docx는 표 데이터가 이미지가 아니라 문서 안에 진짜 텍스트로 들어있어서 OCR이
전혀 필요 없다. python-docx로 표 구조(document.tables)를 그대로 읽는다.
단, DOCX 안에 삽입된 이미지의 글자는 quotation_extractor가 별도 비전
입력으로 전달한다.

지원 형식: .docx만 지원한다. 구형 이진 형식 .doc는 python-docx가 읽지 못한다.
.doc 파일은 Word/한글/LibreOffice에서 열어 "다른 이름으로 저장 > Word 문서
(.docx)"로 변환한 뒤 사용하면 된다.

병합 셀 처리: python-docx는 병합된 셀을 rowspan/colspan으로 알려주지 않고,
"같은 셀 텍스트가 이웃 칸에도 그대로 반복"되는 형태로 보여준다. 이 모듈은
그 반복을 그대로 둔다 — 값이 사라지거나 엉뚱한 칸으로 새는 이미지 추출기의
문제와는 다른 종류의(더 안전한) 결과다.

"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TypeAlias


DocxSource: TypeAlias = str | Path | bytes


def _open_document(source: DocxSource):
    """경로 또는 이메일 첨부 바이트에서 Word 문서를 연다."""
    from docx import Document

    if isinstance(source, bytes):
        return Document(io.BytesIO(source))

    suffix = Path(source).suffix.lower()
    if suffix != ".docx":
        raise ValueError(
            f"지원하지 않는 확장자입니다: {suffix or '(없음)'}. "
            ".doc(구형 이진 형식)는 Word/한글/LibreOffice에서 .docx로 변환한 뒤 사용하세요."
        )
    return Document(str(source))


def _cell_text(cell) -> str:
    """셀 자체 텍스트에 더해, 셀 안에 중첩된 표(표 안의 표)가 있으면 같이 풀어서 붙인다.

    docx는 이미지와 달리 "표 안의 표"가 문서 모델에 그대로 들어있어서 OCR처럼
    추측할 필요가 없다. cell.text만 읽으면 중첩 표 내용이 통째로 빠지므로
    cell.tables를 따로 순회해서 합친다. 2칸짜리 행(키: 값 형태)은 "키: 값"으로,
    그 외에는 칸을 띄어서 이어 붙인다.
    """
    parts = [cell.text.strip()] if cell.text.strip() else []
    for nested_table in cell.tables:
        for row in nested_table.rows:
            values = [c.text.strip() for c in row.cells]
            if len(values) == 2 and values[0] and values[1]:
                parts.append(f"{values[0]}: {values[1]}")
            else:
                parts.append(" ".join(v for v in values if v))
    return "; ".join(p for p in parts if p)


def extract_tables_from_docx(path: DocxSource) -> list[list[list[str]]]:
    """docx 안의 모든 표를 행 x 열 텍스트 그리드로 읽는다."""
    document = _open_document(path)
    tables: list[list[list[str]]] = []
    for table in document.tables:
        grid = [[_cell_text(cell) for cell in row.cells] for row in table.rows]
        tables.append(grid)
    return tables


def extract_paragraphs(path: DocxSource) -> list[str]:
    """표 밖의 본문 문단(회사명, 비고 등)을 읽는다."""
    document = _open_document(path)
    return [p.text.strip() for p in document.paragraphs if p.text.strip()]
