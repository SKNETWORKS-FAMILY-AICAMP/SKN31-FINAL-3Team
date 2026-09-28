"""Opt-in, context-local counters: never record URLs, credentials or document bodies."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic


class OperationBudgetExceeded(RuntimeError):
    pass


@dataclass
class OperationMetrics:
    seconds: float = 30.0
    max_erp_calls: int = 100
    started: float = field(default_factory=monotonic)
    erp_calls: int = 0
    db_connections: int = 0

    def before_erp(self) -> float:
        remaining = self.seconds - (monotonic() - self.started)
        if remaining <= 0 or self.erp_calls >= self.max_erp_calls:
            raise OperationBudgetExceeded('준비 검사 예산 초과; 자동 반복 대신 재시도 한도를 적용합니다.')
        self.erp_calls += 1
        return remaining

    def snapshot(self):
        return {'elapsed_ms': round((monotonic() - self.started) * 1000),
                'erp_calls': self.erp_calls, 'db_connections': self.db_connections}


current: ContextVar[OperationMetrics | None] = ContextVar('operation_metrics', default=None)


@contextmanager
def measure(**kwargs):
    metrics = OperationMetrics(**kwargs)
    token = current.set(metrics)
    try:
        yield metrics
    finally:
        current.reset(token)
