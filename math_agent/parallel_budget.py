"""Job-wide admission control for serial and parallel model requests.

Use ONE instance for source, every child agent and final review. A request lease
counts immediately on admission, even if the HTTP request fails. This module
never retries or interrupts an in-flight transport: callers must pass the lease's
remaining deadline to HTTP and propagate cancellation to their worker/transport.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import math
import threading
import time
from typing import Callable


class ParallelBudgetError(RuntimeError):
    """A model request was refused before admission."""


class CallsExhausted(ParallelBudgetError):
    pass


class DeadlineExpired(ParallelBudgetError):
    pass


class BudgetCancelled(ParallelBudgetError):
    pass


class RequestLease:
    """An admitted call's immutable identity and job-wide remaining deadline."""

    def __init__(self, budget: ParallelBudget, call_id: int, stage: str):
        self._budget = budget
        self.call_id = call_id
        self.stage = stage

    def remaining_seconds(self) -> float | None:
        return self._budget.remaining_seconds()

    def check(self) -> None:
        """Reject cancellation/deadline after admission, before transport work."""
        with self._budget._condition:
            self._budget._check_stopped()
            if self.call_id not in self._budget._active:
                raise RuntimeError("Request lease is already closed")

    def record_usage(self, usage: dict | None) -> None:
        """Attach returned usage once. Missing/unusable usage stays unknown."""
        with self._budget._condition:
            if self.call_id not in self._budget._active:
                raise RuntimeError("Cannot attach usage to a closed request lease")
            record = self._budget._records[self.call_id - 1]
            if record['usage_recorded'] or record.get('not_sent'):
                raise RuntimeError("Usage already recorded for this request")
            if usage is not None and not isinstance(usage, dict):
                raise TypeError("Usage must be a dict or None")
            record['usage'] = deepcopy(usage)
            record['usage_recorded'] = True

    def record_not_sent(self,reason):
        """A local guard refused transport; no synthetic API usage is invented."""
        with self._budget._condition:
            if self.call_id not in self._budget._active:raise RuntimeError('Request lease is already closed')
            record=self._budget._records[self.call_id-1]
            if record['usage_recorded'] or record.get('not_sent'):raise RuntimeError('Request fate already recorded')
            record['not_sent']=reason


def _token(value):
    return type(value) is int and value >= 0


def _counts(usage):
    if not isinstance(usage, dict):
        return None
    # Chat Completions and Responses use different names. Reasoning tokens are
    # already part of output tokens and must not be added a second time.
    prompt = usage.get('prompt_tokens', usage.get('input_tokens'))
    output = usage.get('completion_tokens', usage.get('output_tokens'))
    if not _token(prompt) or not _token(output):
        return None
    return prompt, output


class ParallelBudget:
    """Thread-safe shared call limit, monotonic deadline and at most two slots.

    No total-token cap is imposed: each call keeps its configured output budget.
    ``clock`` is injectable for deterministic tests, and must be monotonic.
    ``deadline`` is absolute on that clock, not wall-clock/Unix time.
    """

    def __init__(self, max_calls: int | None, *, timeout_seconds: float | None = None,
                 deadline: float | None = None, max_concurrency: int = 2,
                 clock: Callable[[], float] = time.monotonic):
        if max_calls is not None and (type(max_calls) is not int or max_calls <= 0):
            raise ValueError("max_calls must be a positive integer")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 2:
            raise ValueError("max_concurrency must be 1 or 2")
        if timeout_seconds is not None and deadline is not None:
            raise ValueError("Supply timeout_seconds or deadline, not both")
        for name, value in (('timeout_seconds', timeout_seconds), ('deadline', deadline)):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError(f"{name} must be finite")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.max_calls = max_calls
        self.max_concurrency = max_concurrency
        self._clock = clock
        self.deadline = clock() + timeout_seconds if timeout_seconds is not None else deadline
        self._condition = threading.Condition()
        self._records: list[dict] = []
        self._active: set[int] = set()
        self._peak_concurrency = 0
        self._cancel_reason: str | None = None

    @property
    def calls(self) -> int:
        with self._condition:
            return len(self._records)

    @property
    def remaining_calls(self) -> int | None:
        with self._condition:
            return None if self.max_calls is None else self.max_calls - len(self._records)

    def remaining_seconds(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - self._clock())

    def _check_stopped(self):
        if self._cancel_reason is not None:
            raise BudgetCancelled(self._cancel_reason)
        remaining = self.remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise DeadlineExpired("Job deadline expired; no additional model request")

    def check(self) -> None:
        """Check job cancellation/deadline before dispatch, merge or publication.

        This does not reserve a model call or require remaining call capacity:
        a fully reviewed result can still be published after the final call.
        """
        with self._condition:
            self._check_stopped()

    def cancel(self, reason: str = "Job cancelled") -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("Cancellation reason must be a nonempty string")
        with self._condition:
            if self._cancel_reason is None:
                self._cancel_reason = reason
            self._condition.notify_all()

    @contextmanager
    def request(self, stage: str = ''):
        """Wait for a slot, atomically reserve one call, release slot on any exit.

        Waiting does not consume calls. Deadline/cancellation and the shared
        count are checked again after a slot becomes available. Failed admitted
        requests are never refunded; any caller retry requires another lease.
        """
        if not isinstance(stage, str):
            raise TypeError("stage must be a string")
        with self._condition:
            while True:
                self._check_stopped()
                if self.max_calls is not None and len(self._records) >= self.max_calls:
                    raise CallsExhausted("Job model-call budget exhausted")
                if len(self._active) < self.max_concurrency:
                    call_id = len(self._records) + 1
                    self._records.append({'call_id': call_id, 'stage': stage,
                                          'usage_recorded': False, 'usage': None,
                                          'finished': False, 'failed': False})
                    self._active.add(call_id)
                    self._peak_concurrency = max(self._peak_concurrency, len(self._active))
                    break
                remaining = self.remaining_seconds()
                self._condition.wait(timeout=remaining)
        failed = True
        try:
            yield RequestLease(self, call_id, stage)
            failed = False
        finally:
            # Includes BaseException cancellation/interrupt, not just Exception.
            with self._condition:
                record = self._records[call_id - 1]
                record['finished'] = True
                record['failed'] = failed
                self._active.remove(call_id)
                self._condition.notify_all()

    def snapshot(self) -> dict:
        with self._condition:
            records = deepcopy(self._records)
            counts = [_counts(record['usage']) for record in records]
            known = [count for count in counts if count is not None]
            not_sent=sum(bool(record.get('not_sent')) for record in records)
            return {'calls': len(records), 'remaining_calls': self.remaining_calls,
                    'active': len(self._active), 'max_concurrency': self.max_concurrency,
                    'peak_concurrency': self._peak_concurrency,
                    'remaining_seconds': self.remaining_seconds(),
                    'cancelled': self._cancel_reason is not None,
                    'cancel_reason': self._cancel_reason, 'records': records,
                    'usage': {'reported_calls': len(known),
                              'not_sent_calls':not_sent,
                              'unknown_calls': len(records) - len(known)-not_sent,
                              'complete': len(known)+not_sent == len(records),
                              'known_prompt_tokens': sum(count[0] for count in known),
                              'known_completion_tokens': sum(count[1] for count in known)}}
