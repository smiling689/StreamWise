import asyncio
import time

import pytest

from streamwise.server.shaping import MBPS, BandwidthTrace, TokenBucket


def test_parse_trace() -> None:
    trace = BandwidthTrace.parse("# comment\n0 8\n10, 1   # drop\n\n20 4\n")
    assert trace.times == (0.0, 10.0, 20.0)
    assert trace.rates_bps == (8 * MBPS, 1 * MBPS, 4 * MBPS)
    assert trace.rate_at(9.99) == 8 * MBPS
    assert trace.rate_at(10) == 1 * MBPS
    assert trace.rate_at(1e6) == 4 * MBPS


@pytest.mark.parametrize("text", ["", "1 8", "0 8\n0 4", "0 8\n5 -1", "0 8\n5 0", "0 8 9", "0 abc"])
def test_invalid_traces(text: str) -> None:
    with pytest.raises(ValueError):
        BandwidthTrace.parse(text)


def test_zero_rate_outage_allowed_in_the_middle() -> None:
    trace = BandwidthTrace.parse("0 8\n1 0\n2 8")
    # 1 MB/s for 1 s, nothing for 1 s, then 1 MB/s again.
    assert trace.bytes_between(0, 3) == pytest.approx(2e6)
    assert trace.time_to_send(0.5, 1e6) == pytest.approx(2.0)  # finishes at t = 2.5


def test_integration_across_pieces() -> None:
    trace = BandwidthTrace.parse("0 8\n10 1")  # 1 MB/s then 125 kB/s
    assert trace.bytes_between(0, 10) == pytest.approx(10e6)
    assert trace.bytes_between(9, 11) == pytest.approx(1e6 + 125e3)
    assert trace.bytes_between(5, 5) == 0
    assert trace.time_to_send(9, 1e6 + 125e3) == pytest.approx(2.0)
    for t0, n in [(0, 123456), (9.5, 7e5), (12, 1e5)]:
        assert trace.bytes_between(t0, t0 + trace.time_to_send(t0, n)) == pytest.approx(n)


async def test_token_bucket_paces_to_rate() -> None:
    rate_mbps = 16.0  # 2 MB/s
    bucket = TokenBucket(BandwidthTrace.constant(rate_mbps), burst_bytes=16 * 1024)
    total = 600_000
    started = time.monotonic()
    for _ in range(total // 10_000):
        await bucket.consume(10_000)
    elapsed = time.monotonic() - started
    expected = (total - 16 * 1024) / (rate_mbps * MBPS / 8)
    assert elapsed == pytest.approx(expected, rel=0.1)


async def test_shared_bucket_splits_bandwidth() -> None:
    bucket = TokenBucket(BandwidthTrace.constant(16.0), burst_bytes=16 * 1024)
    done: dict[str, float] = {}
    started = time.monotonic()

    async def sender(name: str) -> None:
        for _ in range(30):
            await bucket.consume(8 * 1024)
        done[name] = time.monotonic() - started

    await asyncio.gather(sender("a"), sender("b"))
    # 2 x 240 KiB at 2 MB/s (minus the initial burst); the FIFO lock interleaves the two
    # senders, so both finish near the end.
    expected = (2 * 30 * 8192 - 16 * 1024) / 2e6
    assert max(done.values()) == pytest.approx(expected, rel=0.1)
    assert min(done.values()) > 0.8 * max(done.values())
