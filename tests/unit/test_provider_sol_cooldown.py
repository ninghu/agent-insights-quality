import asyncio
from collections import Counter
from datetime import UTC, datetime
from email.utils import format_datetime
import json

import pytest

from agent_insights_quality.assessment import _retain_error
from agent_insights_quality.contracts import Environment
from agent_insights_quality.errors import QualityError
from agent_insights_quality.providers import AzureSol, HttpResponse, SolResponseError

SCHEMA = {
    "type": "object", "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"], "additionalProperties": False,
}
ENVIRONMENT = Environment(
    "staging", "synthetic", "project", "https://example.invalid/api/projects/project",
    "/synthetic/telemetry", "storage", "registry", "swedencentral", "SwedenCentral",
)
WALL_TIME = datetime(2026, 9, 5, tzinfo=UTC).timestamp()


def limited(headers=None):
    return HttpResponse(
        429, headers or {}, b'{"error":{"code":"rate_limit_exceeded","message":"Synthetic private detail"}}',
    )


def success():
    return HttpResponse(200, body=json.dumps({
        "status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"ok":true}'},
        ]}],
    }).encode())


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    async def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class ManualClock(Clock):
    def __init__(self):
        super().__init__()
        self.sleepers = []
        self.started = asyncio.Queue()

    async def sleep(self, delay):
        self.waits.append(delay)
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + delay, future))
        self.started.put_nowait(delay)
        await future

    def advance(self, delay):
        self.now += delay
        for deadline, future in self.sleepers:
            if deadline <= self.now and not future.done():
                future.set_result(None)


class Transport:
    def __init__(self, clock, *responses):
        self.clock = clock
        self.responses = list(responses)
        self.requests = []

    async def send(self, request):
        self.requests.append((self.clock.now, request))
        assert self.responses, "Unexpected POST"
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def provider(clock, transport, **kwargs):
    return AzureSol(
        ENVIRONMENT, transport=transport, sleep=clock.sleep,
        monotonic=lambda: clock.now, wall_clock=lambda: WALL_TIME + clock.now, **kwargs,
    )


async def complete(sol, name="synthetic"):
    return await sol.complete_json(instructions="Synthetic", payload={"name": name}, schema=SCHEMA)


async def ready_tasks():
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.mark.parametrize("headers,expected", [
    ({}, 60),
    ({"Retry-After": "60"}, 60),
    ({"Retry-After": "300"}, 300),
    ({"Retry-After": "1.5"}, 1.5),
    ({"retry-after-ms": "1500"}, 1.5),
    ({"Retry-After": "60", "retry-after-ms": "90000"}, 90),
    ({"Retry-After": "120", "retry-after-ms": "60000"}, 120),
    ({"Retry-After": "invalid", "retry-after-ms": "1000"}, 1),
    ({"retry-after-ms": "invalid"}, 60),
    ({"Retry-After": "0"}, 60),
    ({"Retry-After": "-2"}, 60),
    ({"Retry-After": "nan"}, 60),
    ({"Retry-After": "inf"}, 60),
    ({"Retry-After": "Synthetic private malformed header"}, 60),
    ({"Retry-After": format_datetime(datetime.fromtimestamp(WALL_TIME + 120, UTC), usegmt=True)}, 120),
])
def test_server_reset_or_conservative_fallback_precedes_retry(headers, expected):
    clock = Clock()
    transport = Transport(clock, limited(headers), success())
    assert asyncio.run(complete(provider(clock, transport))) == {"ok": True}
    assert clock.waits == [expected]
    assert [sent for sent, _ in transport.requests] == [0, expected]
    assert transport.requests[0][1].body == transport.requests[1][1].body


@pytest.mark.parametrize("headers", [
    {"Retry-After": "301"}, {"retry-after-ms": "300001"},
    {"Retry-After": "9" * 1000},
])
def test_excessive_server_wait_blocks_retries_and_fresh_calls_without_sleep(headers):
    clock = Clock()
    transport = Transport(clock, limited(headers))
    sol = provider(clock, transport)
    for name in ("initial", "fresh"):
        with pytest.raises(SolResponseError, match="sol_rate_limit_wait_exhausted") as error:
            asyncio.run(complete(sol, name))
        assert not error.value.retryable and error.value.request_accepted is False
        assert error.value.private_detail["wait_reason"] == "server_wait_exceeds_limit"
    assert not clock.waits and len(transport.requests) == 1


def test_total_wait_and_attempts_are_bounded():
    clock = Clock()
    transport = Transport(clock, *[limited({"Retry-After": "250"}) for _ in range(3)])
    with pytest.raises(SolResponseError, match="sol_rate_limit_wait_exhausted") as error:
        asyncio.run(complete(provider(clock, transport, attempts=5)))
    assert error.value.private_detail["wait_reason"] == "total_wait_budget"
    assert error.value.private_detail["attempts_sent"] == 3
    assert clock.waits == [250, 250]
    assert len(transport.requests) == 3


def test_missing_headers_retries_only_the_configured_number_of_attempts():
    clock = Clock()
    transport = Transport(clock, *[limited() for _ in range(5)])
    with pytest.raises(SolResponseError, match="sol_http_error"):
        asyncio.run(complete(provider(clock, transport, attempts=5)))
    assert clock.waits == [60] * 4
    assert [sent for sent, _ in transport.requests] == [0, 60, 120, 180, 240]


def test_cooldown_is_scoped_to_one_sol_instance():
    clock = Clock()
    first = provider(clock, Transport(clock, limited()), attempts=1)
    with pytest.raises(SolResponseError):
        asyncio.run(complete(first))
    transport = Transport(clock, success())
    assert asyncio.run(complete(provider(clock, transport))) == {"ok": True}
    assert not clock.waits and transport.requests[0][0] == 0


def test_last_rejected_attempt_still_sets_shared_cooldown_for_fresh_calls():
    clock = Clock()
    transport = Transport(clock, limited(), success())
    sol = provider(clock, transport, attempts=1)
    with pytest.raises(SolResponseError, match="sol_http_error"):
        asyncio.run(complete(sol, "first"))
    assert asyncio.run(complete(sol, "second")) == {"ok": True}
    assert clock.waits == [60]
    assert [sent for sent, _ in transport.requests] == [0, 60]


def test_retries_keep_their_window_and_fresh_calls_are_fifo():
    async def exercise():
        clock = ManualClock()
        requests, counts = [], Counter()

        class Requests:
            async def send(self, request):
                name = json.loads(json.loads(request.body)["input"])["name"]
                requests.append((name, clock.now))
                counts[name] += 1
                if counts[name] <= {"first": 2, "second": 1, "third": 0}[name]:
                    return limited({"Retry-After": "60"})
                return success()

        sol = provider(clock, Requests())
        first = asyncio.create_task(complete(sol, "first"))
        assert await clock.started.get() == 60
        second = asyncio.create_task(complete(sol, "second"))
        third = asyncio.create_task(complete(sol, "third"))
        await ready_tasks()
        assert requests == [("first", 0)]
        clock.advance(60)
        assert await clock.started.get() == 60
        assert requests == [("first", 0), ("first", 60)]
        clock.advance(60)
        assert await clock.started.get() == 60
        assert requests[-2:] == [("first", 120), ("second", 120)]
        clock.advance(60)
        assert await asyncio.gather(first, second, third) == [{"ok": True}] * 3
        assert requests == [
            ("first", 0), ("first", 60), ("first", 120),
            ("second", 120), ("second", 180), ("third", 180),
        ]
        assert sol._cooldown_response is None

    asyncio.run(exercise())


def test_inflight_rejection_extends_cooldown_without_an_early_post():
    async def exercise():
        clock = ManualClock()
        first_release, second_release, both_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        requests, counts = [], Counter()

        class Requests:
            async def send(self, request):
                name = json.loads(json.loads(request.body)["input"])["name"]
                requests.append((name, clock.now))
                counts[name] += 1
                if len(requests) == 2:
                    both_entered.set()
                if counts[name] == 1:
                    await (first_release if name == "first" else second_release).wait()
                    return limited({"Retry-After": "60" if name == "first" else "120"})
                return success()

        sol = provider(clock, Requests())
        first = asyncio.create_task(complete(sol, "first"))
        second = asyncio.create_task(complete(sol, "second"))
        await both_entered.wait()
        first_release.set()
        assert await clock.started.get() == 60
        clock.advance(20)
        second_release.set()
        await ready_tasks()
        clock.advance(40)
        assert await clock.started.get() == 80
        assert len(requests) == 2
        clock.advance(80)
        assert await asyncio.gather(first, second) == [{"ok": True}] * 2
        assert requests[-2:] == [("first", 140), ("second", 140)]

    asyncio.run(exercise())


def test_healthy_calls_remain_concurrent():
    async def exercise():
        clock = Clock()
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        class Requests:
            async def send(self, request):
                nonlocal count
                count += 1
                if count == 4:
                    entered.set()
                await release.wait()
                return success()

        sol = provider(clock, Requests())
        tasks = [asyncio.create_task(complete(sol, str(index))) for index in range(4)]
        await entered.wait()
        assert count == 4 and not clock.waits
        release.set()
        assert await asyncio.gather(*tasks) == [{"ok": True}] * 4

    asyncio.run(exercise())


def test_cancelled_queue_waiter_does_not_steal_or_deadlock_recovery():
    async def exercise():
        clock = ManualClock()
        transport = Transport(clock, limited(), success(), success())
        sol = provider(clock, transport)
        first = asyncio.create_task(complete(sol))
        await clock.started.get()
        cancelled = asyncio.create_task(complete(sol, "cancelled"))
        await ready_tasks()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        third = asyncio.create_task(complete(sol, "third"))
        clock.advance(60)
        assert await asyncio.gather(first, third) == [{"ok": True}] * 2
        assert len(transport.requests) == 3

    asyncio.run(exercise())


def test_queue_wait_time_counts_toward_total_budget():
    async def exercise():
        clock = ManualClock()
        transport = Transport(clock, limited())
        sol = provider(clock, transport)
        first = asyncio.create_task(complete(sol, "first"))
        await clock.started.get()
        second = asyncio.create_task(complete(sol, "second"))
        await ready_tasks()
        clock.advance(601)
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(value, SolResponseError) for value in outcomes)
        assert all(value.code == "sol_rate_limit_wait_exhausted" for value in outcomes)
        assert len(transport.requests) == 1

    asyncio.run(exercise())


def test_no_progress_sleep_fails_instead_of_spinning():
    clock = Clock()

    async def stalled(delay):
        clock.waits.append(delay)

    clock.sleep = stalled
    transport = Transport(clock, limited())
    with pytest.raises(SolResponseError, match="sol_rate_limit_wait_exhausted") as error:
        asyncio.run(complete(provider(clock, transport)))
    assert error.value.private_detail["wait_reason"] == "clock_did_not_advance"
    assert clock.waits == [60] and len(transport.requests) == 1


@pytest.mark.parametrize("reply", [
    TimeoutError("Synthetic private timeout"),
    HttpResponse(503, body=b'{"error":{"message":"Synthetic private server error"}}'),
])
def test_unknown_or_server_failure_is_not_blindly_replayed(reply):
    clock = Clock()
    transport = Transport(clock, reply)
    with pytest.raises(QualityError):
        asyncio.run(complete(provider(clock, transport)))
    assert not clock.waits and len(transport.requests) == 1


def test_error_retains_only_valid_selected_rate_headers_privately(capsys):
    clock = Clock()
    headers = {
        "Retry-After": "60", "retry-after-ms": "60000",
        "x-ratelimit-limit-tokens": "1000000", "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "1m30s", "x-ratelimit-remaining-requests": "private text",
        "Authorization": "synthetic-secret", "Set-Cookie": "synthetic-cookie",
        "x-synthetic-diagnostic": "not retained",
    }
    transport = Transport(clock, limited(headers))
    with pytest.raises(SolResponseError) as error:
        asyncio.run(complete(provider(clock, transport, attempts=1)))
    expected = {
        "retry-after": "60", "retry-after-ms": "60000",
        "x-ratelimit-limit-tokens": "1000000", "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "1m30s",
    }
    assert error.value.private_detail == {"rate_limit": {"headers": expected}}
    _retain_error(error.value, {"synthetic": "assessment detail"})
    assert error.value.private_detail["failure"]["detail"]["rate_limit"]["headers"] == expected
    assert str(error.value) == "sol_http_error"
    assert "synthetic-secret" not in json.dumps(error.value.private_detail)
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
