"""견적서 PDF: 현재 방식(pypdf) vs pymupdf4llm 추출 결과 수동 비교 스크립트.

pytest가 자동 수집하지 않도록 일부러 파일명을 test_*.py로 짓지 않았다(실제
PDF 경로가 있어야 돌아가는 수동 실험용 스크립트라 CI/일반 pytest 실행에
끼면 안 됨).

비교 대상은 pypdf다 - fitz(PyMuPDF)가 아니다. 실제 운영 코드
(quotation_extractor.py)에서 fitz는 텍스트 추출용이 아니라, pypdf로 텍스트가
0자 나온 스캔 PDF를 페이지 이미지로 변환해 로컬 비전 모델에 넘기는 용도로만
쓰인다(_render_scanned_pdf). 디지털 텍스트가 있는 PDF에서 실제로 텍스트를
뽑는 건 pypdf.PdfReader().pages[i].extract_text()(_pdf_to_source)뿐이라,
이 스크립트도 그 경로와 동일한 방식으로 pypdf를 돌려서 비교한다.

quotation_extractor.py 주석에 적혀있듯 pypdf 방식은 표를 "통짜 평문화"해서
열 순서가 보장되지 않는데, pymupdf4llm이 표를 마크다운 표로 더 잘
구조화해주는지 눈으로 비교하는 게 목적.

사용법:
    python tests/manual_pdf_extraction_comparison.py <PDF 경로> [<PDF 경로2> ...]

    또는 아래 PDF_PATHS 리스트에 경로를 직접 넣고:
    python tests/manual_pdf_extraction_comparison.py

필요 패키지:
    - pypdf            (requirements.txt에 이미 있음)
    - pymupdf4llm       (별도 설치 필요: pip install pymupdf4llm)

파일을 따로 안 만들고 결과를 전부 터미널에 그대로 찍는다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# 여기에 직접 PDF 경로를 넣어도 됨(인자 없이 실행할 경우 이 목록을 씀).
PDF_PATHS: list[str] = [
    # r"C:\Users\박동관\Desktop\샘플견적서.pdf",
]


def extract_with_pypdf(path: Path) -> tuple[str, float, int]:
    """운영 코드(_pdf_to_source)의 디지털 텍스트 추출 경로와 동일한 방식."""
    from pypdf import PdfReader

    start = time.perf_counter()
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    flat_text = "\n\n".join(
        f"[page {idx}]\n{page}" for idx, page in enumerate(pages, 1) if page
    )
    elapsed = time.perf_counter() - start
    return flat_text, elapsed, len(reader.pages)


def extract_with_pymupdf4llm(path: Path) -> tuple[str, float]:
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise RuntimeError(
            "pymupdf4llm이 설치돼있지 않습니다. 가상환경에서 "
            "`pip install pymupdf4llm` 실행 후 다시 돌리세요."
        ) from exc

    start = time.perf_counter()
    markdown = pymupdf4llm.to_markdown(str(path))
    elapsed = time.perf_counter() - start
    return markdown, elapsed


def _print_block(title: str, elapsed: float, char_count: int, text: str) -> None:
    print(f"\n--- {title} ({elapsed:.3f}s, {char_count}자) " + "-" * 30)
    print(text if text.strip() else "(추출된 내용 없음)")


def compare_one(path: Path) -> None:
    if not path.exists():
        print(f"[건너뜀] 파일 없음: {path}")
        return

    print(f"\n{'=' * 70}\n{path.name}\n{'=' * 70}")

    try:
        pypdf_text, pypdf_elapsed, page_count = extract_with_pypdf(path)
    except Exception as exc:  # pragma: no cover - 수동 실험용
        print(f"  pypdf 추출 실패: {exc}")
        pypdf_text, pypdf_elapsed, page_count = "", 0.0, 0

    try:
        md_text, md_elapsed = extract_with_pymupdf4llm(path)
    except Exception as exc:  # pragma: no cover - 수동 실험용
        print(f"  pymupdf4llm 추출 실패: {exc}")
        md_text, md_elapsed = "", 0.0

    print(f"페이지 수: {page_count}")
    _print_block("pypdf (현재 운영 방식)", pypdf_elapsed, len(pypdf_text), pypdf_text)
    _print_block("pymupdf4llm", md_elapsed, len(md_text), md_text)


def main() -> None:
    paths = [Path(p) for p in (sys.argv[1:] or PDF_PATHS)]
    if not paths:
        print(
            "비교할 PDF 경로가 없습니다.\n"
            "  python tests/manual_pdf_extraction_comparison.py <PDF 경로>\n"
            "또는 이 파일 위쪽 PDF_PATHS 리스트에 경로를 채워서 실행하세요."
        )
        return

    for path in paths:
        compare_one(path)


if __name__ == "__main__":
    main()
