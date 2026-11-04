import asyncio

import pytest

from v8_next.app.stream_run import run_observation
from v8_next.evaluation.store import canonical


@pytest.mark.parametrize("mode", ["recover", "exhaust", "error"])
def test_only_sealed_quote_silence_restarts_with_backfill(tmp_path, monkeypatch, mode):
    calls = []

    async def capture(path, duration, **kwargs):
        calls.append(kwargs)
        if mode == "error":
            raise ValueError("invalid source")
        path.mkdir()
        result = dict(
            status="OBSERVED" if mode == "recover" and len(calls) == 2 else "HALTED_QUOTE_SILENCE"
        )
        (path / "result.json").write_text(canonical(result))
        return result

    monkeypatch.setattr("v8_next.app.stream_run.capture_stream", capture)
    job = run_observation(
        tmp_path / "run",
        manifests=(tmp_path / "warmup",),
        duration_seconds=100,
        max_restarts=1,
        max_quote_silence_ns=10,
        positioning_refresh_seconds=30,
    )
    if mode == "error":
        with pytest.raises(ValueError, match="invalid source"):
            asyncio.run(job)
        assert len(calls) == 1
        return
    result = asyncio.run(job)
    assert len(calls) == 2
    assert all(call["positioning_refresh_seconds"] == 30 for call in calls)
    assert result["positioning_refresh_seconds"] == 30
    assert calls[0]["resume_from"] is None
    assert calls[1]["resume_from"] == tmp_path / "run" / "session-000"
    assert calls[1]["refresh_on_resume"] is True
    assert calls[1]["manifests"] == ()
    assert result["status"] == ("OBSERVED" if mode == "recover" else "HALTED_RESTART_LIMIT")
