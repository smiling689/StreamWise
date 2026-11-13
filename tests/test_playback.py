import asyncio

import pytest

from streamwise.client.playback import PlaybackModel, PlayerState

D = 0.1  # seconds per segment


async def test_startup_then_smooth_playback() -> None:
    model = PlaybackModel([D] * 3, startup_buffer_s=D)
    played: list[int] = []
    task = asyncio.create_task(model.run(on_segment_played=played.append))
    await asyncio.sleep(0.05)
    assert model.state is PlayerState.STARTUP
    for seq in range(3):
        model.segment_downloaded(seq)
    await task
    assert played == [0, 1, 2]
    assert model.stalls == []
    assert model.startup_delay == pytest.approx(0.05, abs=0.02)
    assert model.ended_at - model.playback_started_at == pytest.approx(3 * D, abs=0.01)


async def test_stall_is_measured_from_playhead_to_arrival() -> None:
    model = PlaybackModel([D] * 2, startup_buffer_s=D)
    events: list[tuple[int, bool]] = []
    task = asyncio.create_task(model.run(on_stall=lambda s, started: events.append((s, started))))
    model.segment_downloaded(0)
    await asyncio.sleep(D + 0.15)  # segment 0 ends at ~D, segment 1 arrives 0.15 s later
    assert model.state is PlayerState.STALLED
    assert model.buffer_level() == 0
    model.segment_downloaded(1)
    await task
    assert events == [(1, True), (1, False)]
    assert len(model.stalls) == 1
    assert model.stalls[0].duration == pytest.approx(0.15, abs=0.02)


async def test_buffer_level_and_wait_for_space() -> None:
    model = PlaybackModel([D] * 5, startup_buffer_s=D)
    task = asyncio.create_task(model.run())
    for seq in range(4):
        model.segment_downloaded(seq)
    await asyncio.sleep(0.01)
    assert model.buffer_level() == pytest.approx(4 * D - 0.01, abs=0.01)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await model.wait_for_buffer_below(2 * D)
    assert loop.time() - t0 == pytest.approx(2 * D - 0.01, abs=0.02)
    assert model.buffer_level() <= 2 * D + 1e-6
    model.segment_downloaded(4)
    await task


async def test_out_of_order_rejected() -> None:
    model = PlaybackModel([D] * 2, startup_buffer_s=D)
    with pytest.raises(ValueError):
        model.segment_downloaded(1)
