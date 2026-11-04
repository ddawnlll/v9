import hashlib
import json

import pytest

from v8_next.adapters.binance_capture import verify


def test_substitution_is_rejected(tmp_path):
    artifact = tmp_path / "raw.json"
    artifact.write_bytes(b"original")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"artifacts": [{"path": "raw.json", "sha256": hashlib.sha256(b"original").hexdigest()}]}
        )
    )
    verify(manifest)
    artifact.write_bytes(b"substituted")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify(manifest)


def test_external_artifact_is_rejected(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": [{"path": "../outside", "sha256": ""}]}))
    with pytest.raises(ValueError, match="escapes"):
        verify(manifest)


def test_complete_capture_rejects_omission_clock_and_source_changes(tmp_path, monkeypatch):
    import io

    from v8_next.adapters import binance_capture

    def response(url, **kwargs):
        payload = {"symbols": [{"symbol": "BTCUSDT"}]} if "exchangeInfo" in url else []
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(binance_capture, "urlopen", response)
    path = binance_capture.capture(tmp_path / "test-only")
    binance_capture.validate_capture(path)
    original = json.loads(path.read_text())
    for mutation in ("missing", "duplicate", "clock", "source", "interval", "claim"):
        data = json.loads(json.dumps(original))
        if mutation == "missing":
            data["artifacts"] = data["artifacts"][1:]
        elif mutation == "duplicate":
            data["artifacts"].append(data["artifacts"][0])
        elif mutation == "clock":
            data["artifacts"][0]["received_time_ns"] = 0
        elif mutation == "source":
            data["artifacts"][0]["source_url"] = "https://example.org/fapi/v1/exchangeInfo"
        elif mutation == "interval":
            data["artifacts"][1]["source_url"] = data["artifacts"][1]["source_url"].replace(
                "1h", "1m"
            )
        else:
            data["claim_status"] = "SUPPORTED_EDGE"
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            binance_capture.validate_capture(path)
