"""견적 제출 시각 기록 - 자동화 v2의 5단계 b.

**마감이 지났는지 판정하는 기준은 협력사가 낸 시각이다.** ERPNext에 Supplier
Quotation이 만들어진 시각이 아니다.

두 경로가 다르다. 포털은 협력사가 쓰는 순간 SQ가 생기니 두 시각이 같다.
이메일 회신은 첨부를 RunPod으로 읽어 등록하기까지 몇 분이 걸릴 수 있어서,
SQ 생성 시각을 쓰면 마감 1분 전에 낸 견적이 마감 4분 후 제출로 보인다.
그 견적을 버리면 멀쩡한 회신을 버리는 것이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend_logic2.repositories import quotation_submissions
from backend_logic2.services import quotation_service

KST = ZoneInfo("Asia/Seoul")


# ---------------------------------------------------------------------------
# ERPNext 시각 읽기
# ---------------------------------------------------------------------------


def test_an_erp_timestamp_is_read_as_korean_time() -> None:
    """⚠️ naive를 돌려주면 마감 시각(+09:00)과 비교하는 순간 TypeError다."""
    parsed = quotation_submissions.parse_erp_datetime("2026-09-27 17:59:30")

    assert parsed == datetime(2026, 9, 27, 17, 59, 30, tzinfo=KST)
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("value", [
    "2026-09-27 17:59:30.123456",
    # ⚠️ 소수 자리가 6자리가 아닌 형태. fromisoformat이 3.10에서 거부하므로
    # strptime 쪽 경로를 타는데, 거기서도 표준시가 붙어야 한다.
    "2026-09-27 17:59:30.1",
    "2026-09-27 17:59:30",
])
def test_the_usual_erp_formats_are_read_as_korean_time(value: str) -> None:
    parsed = quotation_submissions.parse_erp_datetime(value)

    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=9)


def test_a_date_without_a_time_is_read_as_midnight() -> None:
    assert quotation_submissions.parse_erp_datetime("2026-09-27") == datetime(
        2026, 9, 27, tzinfo=KST
    )


def test_an_offset_that_is_already_there_is_kept() -> None:
    parsed = quotation_submissions.parse_erp_datetime("2026-09-27T09:00:00+00:00")

    assert parsed == datetime(2026, 9, 27, 18, 0, tzinfo=KST)


@pytest.mark.parametrize("value", [None, "", "   ", "곧", "2026-13-45"])
def test_an_unreadable_time_is_unknown_rather_than_now(value) -> None:
    """모르는 값을 지금 시각으로 채우면 멀쩡한 견적이 늦은 것으로 바뀐다."""
    assert quotation_submissions.parse_erp_datetime(value) is None


# ---------------------------------------------------------------------------
# 기록
# ---------------------------------------------------------------------------


class _FakeConnection:
    """진짜 연결처럼 **딕셔너리** 행을 돌려준다.

    ⚠️ get_connection은 row_factory=dict_row다. v1에서 가짜 연결이 튜플을
    돌려준 탓에, 실제로는 KeyError(0)으로 죽는 fetchone()[0] 코드가 테스트를
    통과했고 마감 스캔이 한 번도 돌지 못했다.
    """

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []
        self.opened = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return SimpleNamespace(
            fetchone=lambda: (self.rows[0] if self.rows else None),
            fetchall=lambda: list(self.rows),
        )


@pytest.fixture
def connection(monkeypatch):
    """연결을 몇 개 열었는지까지 센다.

    ⚠️ 이 프로젝트에는 커넥션 풀이 없다. get_connection은 부를 때마다 새
    Postgres 연결을 연다. 그래서 "몇 번 불렀나"가 곧 부하다.
    """
    def install(rows=None):
        fake = _FakeConnection(rows=rows)

        class _Ctx:
            def __enter__(self_inner):
                fake.opened += 1
                return fake

            def __exit__(self_inner, *_exc):
                return False

        monkeypatch.setattr(
            quotation_submissions, "get_connection", lambda **_kw: _Ctx()
        )
        return fake

    return install


def _row(name="SQ-1", **overrides):
    row = {
        "quotation_name": name,
        "rfq_name": "RFQ-1",
        "source": "portal",
        "submitted_at": "2026-09-27 17:59:00",
    }
    row.update(overrides)
    return row


def test_many_submissions_take_one_connection_and_one_statement(connection) -> None:
    """⚠️ 이게 이번 수정의 핵심이다. 이 함수는 10초마다 도는 폴러 안에서
    불린다. 건당 연결을 열면 그게 그대로 화면 지연이 된다."""
    fake = connection(rows=[
        {"quotation_name": f"SQ-{index}", "submitted_at": datetime.now(KST)}
        for index in range(1, 6)
    ])

    written = quotation_submissions.record_submissions(
        [_row(f"SQ-{index}") for index in range(1, 6)]
    )

    assert written == 5
    assert fake.opened == 1, f"연결을 {fake.opened}개 열었다"
    assert len(fake.calls) == 1, f"문장을 {len(fake.calls)}개 실행했다"


def test_the_batch_is_read_back_by_column_name(connection) -> None:
    """⚠️ dict_row 연결이다. row[0]은 KeyError(0)이 된다."""
    connection(rows=[{"quotation_name": "SQ-1", "submitted_at": datetime.now(KST)}])

    assert quotation_submissions.record_submission(
        quotation_name="SQ-1", rfq_name="RFQ-1",
        source="email", submitted_at="2026-09-27 17:59:00",
    ) == 1


def test_a_recorded_time_is_never_pushed_later(connection) -> None:
    """같은 견적에 SQ 변경 웹훅이 여러 번 온다. 그때마다 SQ 생성 시각으로
    덮어쓰면 애써 기록한 메일 시각이 날아간다. 그래서 LEAST다."""
    fake = connection(rows=[{"quotation_name": "SQ-1", "submitted_at": datetime.now(KST)}])

    quotation_submissions.record_submissions([_row(submitted_at="2026-09-27 18:04:00")])

    assert "LEAST(" in fake.calls[0][0], "기록된 제출 시각을 덮어쓰면 안 된다"


def test_email_beats_portal_as_the_source(connection) -> None:
    """이메일로 들어온 건은 SQ 생성 시각보다 메일 시각이 항상 진실에 가깝다."""
    fake = connection(rows=[{"quotation_name": "SQ-1", "submitted_at": datetime.now(KST)}])

    quotation_submissions.record_submissions([_row()])

    assert (
        "WHEN quotation_submission.source = 'email' THEN 'email'" in fake.calls[0][0]
    )


def test_the_same_quotation_twice_is_merged_before_the_statement(connection) -> None:
    """⚠️ 한 문장 안에 같은 견적이 두 번 들어가면 Postgres가 거부한다
    (ON CONFLICT는 한 행을 두 번 못 고친다). 폴링과 이메일 처리가 겹치면
    실제로 그런 입력이 만들어진다."""
    fake = connection(rows=[{"quotation_name": "SQ-1", "submitted_at": datetime.now(KST)}])

    quotation_submissions.record_submissions([
        _row(submitted_at="2026-09-27 18:04:00"),
        _row(source="email", submitted_at="2026-09-27 17:59:00",
             communication_name="COMM-1"),
    ])

    params = fake.calls[0][1]
    assert params["quotation_names"] == ["SQ-1"], "같은 견적이 두 번 들어갔다"
    assert params["sources"] == ["email"], "email이 이겨야 한다"
    assert params["submitted_ats"][0] == datetime(2026, 9, 27, 17, 59, tzinfo=KST)
    assert params["communication_names"] == ["COMM-1"]


@pytest.mark.parametrize("row", [
    _row(name=""),
    _row(rfq_name=""),
    _row(source="carrier-pigeon"),
    _row(submitted_at="곧"),
    _row(submitted_at=None),
    "이상한 값",
    None,
])
def test_an_incomplete_record_never_reaches_the_database(connection, row) -> None:
    """읽지 못한 시각을 아무 값으로 채워 넣으면 판정이 거짓말을 한다."""
    fake = connection(rows=[])

    assert quotation_submissions.record_submissions([row]) == 0
    assert fake.calls == [], "쓸 게 없는데 연결을 열었다"


def test_a_good_row_survives_a_bad_one_beside_it(connection) -> None:
    fake = connection(rows=[{"quotation_name": "SQ-2", "submitted_at": datetime.now(KST)}])

    assert quotation_submissions.record_submissions([_row("", ), _row("SQ-2")]) == 1
    assert fake.calls[0][1]["quotation_names"] == ["SQ-2"]


def test_reading_back_skips_quotations_without_a_record(connection) -> None:
    moment = datetime(2026, 9, 27, 17, 0, tzinfo=KST)
    connection(rows=[
        {"quotation_name": "SQ-1", "submitted_at": moment},
        {"quotation_name": "SQ-2", "submitted_at": None},
    ])

    found = quotation_submissions.submitted_at_by_quotation(["RFQ-1"])

    assert found == {"SQ-1": moment}
    assert "SQ-2" not in found, "모르는 건 없는 것으로 - 아는 척하면 안 된다"


def test_reading_back_with_no_rfq_asks_nothing(connection) -> None:
    fake = connection(rows=[])

    assert quotation_submissions.submitted_at_by_quotation(["", "  "]) == {}
    assert fake.calls == []


# ---------------------------------------------------------------------------
# 포털 경로 - 아직 기록이 없는 것만
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    state = {"written": [], "batches": 0, "looked_up": 0}

    def look_up(names):
        state["looked_up"] += 1
        return {}

    def write(rows):
        state["batches"] += 1
        state["written"].extend(rows)
        # 진짜 저장소와 같은 규칙으로 센다 - 가짜가 더 관대하면 테스트가
        # 거짓으로 통과한다.
        return len(quotation_submissions.merge_rows(rows))

    monkeypatch.setattr(
        quotation_service.submission_repository, "submitted_at_by_quotation", look_up
    )
    monkeypatch.setattr(
        quotation_service.submission_repository, "record_submissions", write
    )
    return state


def _quotation(name="SQ-1", creation="2026-09-27 17:59:00"):
    return {
        "name": name,
        "creation": creation,
        "supplier": "SUP-1",
        "supplier_name": "동관컴퍼니",
    }


def test_a_portal_quotation_is_recorded_from_its_creation_time(recorded) -> None:
    quotation_service.record_portal_submissions("RFQ-1", [_quotation()])

    assert len(recorded["written"]) == 1
    written = recorded["written"][0]
    assert written["source"] == "portal"
    assert written["submitted_at"] == "2026-09-27 17:59:00"
    assert written["supplier_name"] == "동관컴퍼니"


def test_the_portal_path_never_asks_what_is_already_recorded(recorded) -> None:
    """⚠️ 폴링은 10초마다 돈다. 미리 조회하면 그만큼 연결이 더 열린다.
    다시 넣어도 안전하다 - 기록된 시각은 LEAST로 유지되고 메일 시각은
    email이 이겨서 덮이지 않는다. 그래서 물어볼 필요가 없다."""
    quotation_service.record_portal_submissions(
        "RFQ-1", [_quotation("SQ-1"), _quotation("SQ-2")]
    )

    assert recorded["looked_up"] == 0, "쓸데없이 미리 조회했다"
    assert recorded["batches"] == 1, "건수와 무관하게 한 번에 써야 한다"


def test_a_quotation_without_a_creation_time_is_not_invented(recorded) -> None:
    quotation_service.record_portal_submissions("RFQ-1", [_quotation(creation=None)])

    assert recorded["written"][0]["submitted_at"] is None


def test_recording_never_breaks_receiving_a_quotation(monkeypatch) -> None:
    """⚠️ 제출 시각 기록은 견적을 받는 일보다 덜 중요하다. 여기서 예외가
    새면 회신 자체가 실패한다."""
    monkeypatch.setattr(
        quotation_service.submission_repository, "record_submissions",
        lambda rows: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    assert quotation_service.record_portal_submissions("RFQ-1", [_quotation()]) == 0


# ---------------------------------------------------------------------------
# 이메일 경로 - 메일 시각이 제출 시각이다
# ---------------------------------------------------------------------------


def test_an_email_quotation_is_recorded_from_the_mail_time(recorded) -> None:
    quotation_service.record_email_submissions(
        "RFQ-1",
        [{"status": "created", "name": "SQ-9"}],
        submitted_at="2026-09-27 17:59:00",
        supplier_id="SUP-1",
        supplier_name="세희상사",
        communication_name="COMM-1",
    )

    written = recorded["written"][0]
    assert written["quotation_name"] == "SQ-9"
    assert written["source"] == "email"
    assert written["submitted_at"] == "2026-09-27 17:59:00"
    assert written["communication_name"] == "COMM-1"


def test_an_already_registered_quotation_still_gets_its_mail_time(recorded) -> None:
    """같은 메일이 다시 처리돼 'already_exists'로 돌아와도 시각은 남아야 한다."""
    quotation_service.record_email_submissions(
        "RFQ-1",
        [{"status": "already_exists", "name": "SQ-9"}],
        submitted_at="2026-09-27 17:59:00",
    )

    assert recorded["written"][0]["quotation_name"] == "SQ-9"


def test_a_registration_without_a_document_name_is_skipped(recorded) -> None:
    written = quotation_service.record_email_submissions(
        "RFQ-1",
        [{"status": "dry_run"}, "이상한 값", None],
        submitted_at="2026-09-27 17:59:00",
    )

    assert written == 0
    assert all(not row["quotation_name"] for row in recorded["written"])


def test_email_recording_never_breaks_the_reply_handler(monkeypatch) -> None:
    monkeypatch.setattr(
        quotation_service.submission_repository, "record_submissions",
        lambda rows: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    assert quotation_service.record_email_submissions(
        "RFQ-1", [{"name": "SQ-9"}], submitted_at="2026-09-27 17:59:00",
    ) == 0


def test_the_mail_time_is_written_before_the_creation_time_can_win() -> None:
    """이메일 회신 처리는 반드시 이 순서여야 한다.

    refresh_case_quotations는 기록이 없는 견적을 SQ 생성 시각으로 채운다.
    메일 시각을 먼저 박아두지 않으면, 첨부를 읽는 데 걸린 몇 분이 그대로
    제출 시각이 된다.
    """
    import inspect

    # 주석에도 두 이름이 나오므로, 실제 코드 줄만 남겨 순서를 본다.
    source = "\n".join(
        line for line in inspect.getsource(
            quotation_service.register_quotation_email_event
        ).splitlines()
        if not line.lstrip().startswith("#")
    )
    mail_first = source.index("record_email_submissions(")
    refresh_after = source.index("refresh_case_quotations(")

    assert mail_first < refresh_after, "메일 시각을 먼저 기록해야 한다"


# ---------------------------------------------------------------------------
# RunPod 비동기 경로 - 몇 분 걸려도 제출 시각은 메일이 온 그때다
# ---------------------------------------------------------------------------


def test_the_mail_time_travels_with_the_runpod_job(monkeypatch) -> None:
    """SQ는 이 작업이 끝난 뒤에야 만들어진다. 작업에 실어 보내지 않으면
    제출 시각을 되찾을 방법이 없다."""
    from backend_logic2.services import runpod_quotation_jobs as runpod

    captured: dict = {}

    class _Parser:
        config = SimpleNamespace(endpoint_id="ep-1", pipeline_version="v1")

        def build_worker_input(self, *_a):
            return {
                "request_id": "req-1",
                "prompt_sha256": "sha",
                "prompt_version": "pv",
            }

        def submit_job(self, *_a):
            return {"id": "runpod-1"}

    monkeypatch.setattr(runpod, "callback_url", lambda: "https://callback")
    monkeypatch.setattr(runpod, "_parser", lambda endpoint_id=None: _Parser())
    monkeypatch.setattr(
        runpod, "prepare_source_bytes",
        lambda data, filename: SimpleNamespace(
            kind=SimpleNamespace(value="pdf"), text="", evidence=[]
        ),
    )
    monkeypatch.setattr(runpod, "prepare_rfq_specifications", lambda *_a: None)
    monkeypatch.setattr(runpod, "extract_document_fallbacks", lambda _text: {})

    def create_job(request_id, endpoint_id, context, *_a):
        captured["context"] = context
        return {"job_id": "job-1"}, True

    monkeypatch.setattr(runpod.jobs, "create_job", create_job)
    monkeypatch.setattr(runpod.jobs, "mark_submitted", lambda *_a: None)
    monkeypatch.setattr(
        runpod.jobs, "get_job", lambda _id: {"status": "SUBMITTED"}
    )

    runpod.enqueue(
        b"pdf", "quote.pdf", "RFQ-1",
        supplier_id="SUP-1", supplier_name="세희상사",
        fallback_quotation_id="EMAIL-1", rfq_requirements={},
        message_id="COMM-1", submitted_at="2026-09-27 17:59:00",
    )

    assert captured["context"]["submitted_at"] == "2026-09-27 17:59:00"


def test_the_finished_job_records_the_mail_time_not_the_registration_time(
    monkeypatch, recorded
) -> None:
    from backend_logic2.services import runpod_quotation_jobs

    mail_time = "2026-09-27 17:59:00"
    job = {
        "context": {
            "rfq_name": "RFQ-1", "supplier_id": "SUP-1", "supplier_name": "세희상사",
            "source_filename": "quote.pdf", "fallback_quotation_id": "EMAIL-1",
            "message_id": "COMM-1", "content_type": "application/pdf",
            "submitted_at": mail_time,
            "source_kind": "pdf", "evidence": [], "document_fallbacks": {},
            "pipeline_version": "v1",
        },
        "result_json": {"extraction": {}},
    }
    seen = {}

    def fake_extract(prepared, **kwargs):
        # ⚠️ context는 그대로 **kwargs로 들어온다. submitted_at을 빼지 않으면
        # 여기서 TypeError가 난다.
        seen["kwargs"] = kwargs
        return {"quotation": True}

    monkeypatch.setattr(
        runpod_quotation_jobs, "_extract_prepared_quotation", fake_extract
    )
    monkeypatch.setattr(
        runpod_quotation_jobs.PreparedSource, "__init__",
        lambda self, kind, text, evidence: None,
    )
    monkeypatch.setattr(
        quotation_service, "register_supplier_quotation",
        lambda quotation: {"status": "created", "name": "SQ-9"},
    )

    runpod_quotation_jobs._register(job)

    assert "submitted_at" not in seen["kwargs"], "context에서 빼지 않으면 터진다"
    written = recorded["written"][0]
    assert written["quotation_name"] == "SQ-9"
    assert written["submitted_at"] == mail_time
    assert written["source"] == "email"


def test_a_job_queued_before_this_change_still_registers(monkeypatch, recorded) -> None:
    """배포 전에 큐에 들어간 작업은 context에 제출 시각이 없다. 그것 때문에
    등록이 실패하면 안 된다(기록만 못 남긴다)."""
    from backend_logic2.services import runpod_quotation_jobs

    job = {
        "context": {
            "rfq_name": "RFQ-1", "supplier_id": "SUP-1", "supplier_name": "세희상사",
            "source_filename": "quote.pdf", "fallback_quotation_id": "EMAIL-1",
            "message_id": "COMM-1", "content_type": "application/pdf",
            "source_kind": "pdf", "evidence": [], "document_fallbacks": {},
        },
        "result_json": {"extraction": {}},
    }
    monkeypatch.setattr(
        runpod_quotation_jobs, "_extract_prepared_quotation",
        lambda prepared, **kw: {"quotation": True},
    )
    monkeypatch.setattr(
        runpod_quotation_jobs.PreparedSource, "__init__",
        lambda self, kind, text, evidence: None,
    )
    monkeypatch.setattr(
        quotation_service, "register_supplier_quotation",
        lambda quotation: {"status": "created", "name": "SQ-9"},
    )

    assert runpod_quotation_jobs._register(job)["name"] == "SQ-9"
    assert recorded["written"][0]["submitted_at"] is None


# ---------------------------------------------------------------------------
# 판정이 기댈 값이 실제로 실려 오는가
# ---------------------------------------------------------------------------


def test_the_quotation_read_model_carries_the_creation_time() -> None:
    """⚠️ get_quotations_for_rfq가 creation을 싣지 않으면 포털 제출 시각을
    기록할 근거가 사라진다."""
    from backend_logic2.nodes.quotation.quotation_filter import (
        get_supplier_quotations as reader,
    )

    detail = {
        "name": "SQ-1", "supplier": "SUP-1", "supplier_name": "동관컴퍼니",
        "docstatus": 0, "creation": "2026-09-27 17:59:00", "grand_total": 1000,
        "items": [{
            "request_for_quotation": "RFQ-1", "item_code": "ITEM-1",
            "item_name": "품목", "qty": 1, "rate": 1000, "amount": 1000,
        }],
    }
    rows = reader.get_quotations_for_rfq(
        "RFQ-1",
        get_many=lambda *a, **kw: [{"name": "SQ-1"}],
        get_one=lambda *a, **kw: detail,
    )

    assert rows[0]["creation"] == "2026-09-27 17:59:00"


def test_a_late_processed_reply_is_still_an_on_time_submission() -> None:
    """이 단계가 존재하는 이유 그 자체.

    마감 17:59에 낸 견적을 18:04에 등록했다. 등록 시각으로 판정하면 버려진다.
    """
    deadline = datetime(2026, 9, 27, 18, 0, tzinfo=KST)
    mail_arrived = quotation_submissions.parse_erp_datetime("2026-09-27 17:59:00")
    registered = mail_arrived + timedelta(minutes=5)

    assert mail_arrived <= deadline, "마감 전에 낸 견적이다"
    assert registered > deadline, "등록은 마감 뒤에 끝났다"
