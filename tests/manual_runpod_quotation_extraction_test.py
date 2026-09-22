"""실제 운영 RunPod 견적 추출 파이프라인을 그대로 호출하는 수동 테스트.

quotation_extractor.py 안의 사설(private) 함수와
backend_logic2.integrations.quotation_extraction.runpod.RunPodQuotationParser를
직접 재사용한다 - 즉 이 스크립트가 만드는 요청은 웹훅이 실제 SQ 이메일을
받았을 때 백엔드가 RunPod에 보내는 것과 100% 동일한 모양이다(같은
system_prompt/user_prompt, 같은 pipeline_version="document-text-v1",
같은 RunPodQuotationParser.__call__ 흐름).

pytest가 자동 수집하지 않도록 test_*.py로 짓지 않았다(실제 RunPod 엔드포인트를
호출해서 GPU 비용이 드는 수동 실험용 스크립트라 CI에 끼면 안 됨).

pymupdf4llm은 모델이 아니라 순수 로컬 라이브러리다(PyMuPDF/fitz 기반 텍스트·
레이아웃 파싱, GPU/모델 가중치 없음, CPU에서 즉시 실행). 그래서 --extractor로
골라도 "가벼운 전처리 방식을 바꾸는 것"일 뿐, 실제 무거운 추론(Qwen3.5)은
여전히 RunPod 쪽에서만 일어난다.

필요:
    - .env(레포 루트)에 RUNPOD_QUOTATION_ENDPOINT_ID, RUNPOD_API_KEY (운영과 동일 변수)
    - pypdf (이미 requirements.txt에 있음)
    - --extractor pymupdf4llm 쓰려면 pip install pymupdf4llm 별도 필요

사용법:
    python tests/manual_runpod_quotation_extraction_test.py <PDF경로>
    python tests/manual_runpod_quotation_extraction_test.py <PDF경로> --extractor pymupdf4llm
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=REPO_ROOT / ".env")


def _extract_text_pypdf(data: bytes, filename: str) -> str:
    """운영 코드(_pdf_to_source)와 완전히 동일한 경로. 스캔 PDF면 빈 문자열이 아니라
    예외를 던지는 대신 그대로 빈 텍스트를 반환하니(비전 fallback은 이 스크립트
    범위 밖), 0자 나오면 이 PDF는 지금 RunPod document_text 경로로는 테스트가
    안 되는 스캔본이라는 뜻이다."""
    from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
        _pdf_to_source,
    )

    text, vision_inputs, evidence = _pdf_to_source(data, filename)
    if vision_inputs:
        print(
            "  [주의] pypdf가 텍스트를 못 읽어서 운영 코드라면 스캔 PDF로 판단해 "
            "비전 모델 경로로 갔을 문서입니다. 이 스크립트는 document_text 경로만 "
            "테스트하므로 빈 텍스트로 진행됩니다."
        )
    return text


def _extract_text_pymupdf4llm(data: bytes, filename: str) -> str:
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise RuntimeError(
            "pymupdf4llm이 설치돼있지 않습니다. `pip install pymupdf4llm` 실행 후 다시 돌리세요."
        ) from exc

    tmp_path = Path(filename)
    if not tmp_path.exists():
        # pymupdf4llm은 경로만 받으므로, 바이트로 받은 경우 임시 파일로 씀.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(data)
            tmp_path = Path(handle.name)
    return pymupdf4llm.to_markdown(str(tmp_path))


def run(pdf_path: Path, extractor: str) -> None:
    _load_env()

    from backend_logic2.nodes.quotation.quotation_filter.quotation_extractor import (
        PreparedSource,
        SourceKind,
    )
    from backend_logic2.integrations.quotation_extraction.runpod import (
        RunPodQuotationParser,
        RunPodQuotationParserError,
    )

    data = pdf_path.read_bytes()

    print(f"[1/3] 텍스트 추출 중 ({extractor}) ...")
    extract_start = time.perf_counter()
    if extractor == "pypdf":
        text = _extract_text_pypdf(data, pdf_path.name)
    else:
        text = _extract_text_pymupdf4llm(data, pdf_path.name)
    extract_elapsed = time.perf_counter() - extract_start
    print(f"  {len(text)}자 추출 ({extract_elapsed:.3f}s)")
    if not text.strip():
        print("추출된 텍스트가 없어서 RunPod 호출을 건너뜁니다.")
        return

    prepared = PreparedSource(
        kind=SourceKind.PDF,
        text=text,
        vision_inputs=[],
        document_inputs=[],
        evidence=[f"수동 테스트: {extractor}로 추출"],
        specification_keys=[],
    )

    print("[2/3] RunPod 엔드포인트에 제출 중 (운영과 동일한 RunPodQuotationParser 사용) ...")
    try:
        parser = RunPodQuotationParser.from_env()
    except ValueError as exc:
        print(f"환경변수 설정 오류: {exc}")
        print(".env에 RUNPOD_QUOTATION_ENDPOINT_ID / RUNPOD_API_KEY가 있는지 확인하세요.")
        return

    call_start = time.perf_counter()
    try:
        extraction = parser(prepared, rfq_name="MANUAL-TEST", supplier_name=None, reflection_errors=[])
    except RunPodQuotationParserError as exc:
        call_elapsed = time.perf_counter() - call_start
        print(f"  RunPod 호출 실패 ({call_elapsed:.1f}s): {exc}")
        return
    call_elapsed = time.perf_counter() - call_start

    print(f"[3/3] 완료 ({call_elapsed:.1f}s)")
    print("\n" + "=" * 70)
    print(f"입력 텍스트 추출 방식: {extractor} ({len(text)}자, {extract_elapsed:.3f}s)")
    print(f"RunPod 왕복 시간     : {call_elapsed:.1f}s")
    print("=" * 70)
    import json

    print(json.dumps(extraction, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument(
        "--extractor",
        choices=["pypdf", "pymupdf4llm"],
        default="pypdf",
        help="텍스트 추출 방식 (기본값 pypdf = 현재 운영 방식)",
    )
    args = parser.parse_args()

    if not args.pdf_path.exists():
        print(f"파일 없음: {args.pdf_path}")
        return

    run(args.pdf_path, args.extractor)


if __name__ == "__main__":
    main()
