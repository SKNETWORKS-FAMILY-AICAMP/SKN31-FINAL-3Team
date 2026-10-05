import asyncio
from unittest.mock import Mock

import pytest

import main
from fastapi import FastAPI


def test_email_reminder_scheduler_is_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("EMAIL_REMINDER_SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "scheduler safety")
    assert not main._email_reminder_scheduler_enabled()
    monkeypatch.setenv("EMAIL_REMINDER_SCHEDULER_ENABLED", "true")
    assert main._email_reminder_scheduler_enabled()
    monkeypatch.setenv("EMAIL_REMINDER_SCHEDULER_ENABLED", "false")
    assert not main._email_reminder_scheduler_enabled()


def test_email_reminder_loop_runs_immediately_and_does_not_overlap(monkeypatch):
    from backend_logic2.nodes.rfq import remind_rfq
    from backend_logic2.pr import reminder_service

    rfq = Mock(return_value=[])
    pr = Mock(return_value={"results": []})
    monkeypatch.setattr(remind_rfq, "run_due_rfq_reminders", rfq)
    monkeypatch.setattr(reminder_service, "run_due_pr_reminders", pr)

    async def scenario():
        app = FastAPI()
        task = asyncio.create_task(main._run_email_reminders(app))
        for _ in range(20):
            if rfq.called and pr.called:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return app

    app = asyncio.run(scenario())

    rfq.assert_called_once_with()
    pr.assert_called_once_with()
    assert app.state.email_reminder_status["actions"] == {}
    assert app.state.email_reminder_status["error"] is None
