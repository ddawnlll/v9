import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from v8_next.adapters.binance_capture import capture


@pytest.mark.parametrize("saturated", [False, True])
def test_explicit_funding_window_does_not_accept_saturated_response(
    tmp_path, monkeypatch, saturated
):
    urls = []

    def request(url, timeout):
        urls.append(url)
        if "exchangeInfo" in url:
            payload = {"symbols": [{"symbol": "BTCUSDT"}]}
        elif "fundingRate" in url:
            payload = [{"symbol": "BTCUSDT", "fundingTime": 10}] * (1000 if saturated else 1)
        else:
            payload = []
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("v8_next.adapters.binance_capture.urlopen", request)
    monkeypatch.setattr("v8_next.adapters.funding_history.urlopen", request)
    monkeypatch.setattr("v8_next.adapters.binance_capture.time.time_ns", lambda: 20_000_000)
    target = tmp_path / "capture"
    if saturated:
        with pytest.raises(ValueError, match="chronology"):
            capture(target, funding_start_ms=5)
        assert not (target / "manifest.json").exists()
    else:
        manifest = capture(target, funding_start_ms=5)
        assert manifest.is_file()
        from v8_next.adapters.settlements import funding_query_windows

        assert funding_query_windows([manifest], 19_000_000) == []
        windows = funding_query_windows([manifest], 25_000_000)
        assert len(windows) == 1
        assert windows[0]["start_inclusive_ns"] == 5_000_000
        assert windows[0]["end_inclusive_ns"] == 20_999_999
        assert windows[0]["received_ns"] == 20_000_000
        assert windows[0]["status"] == "BOUNDED_RESPONSE_NOT_FINALITY_CERTIFICATE"
    funding_url = next(url for url in urls if "fundingRate" in url)
    query = parse_qs(urlsplit(funding_url).query)
    assert query == {
        "symbol": ["BTCUSDT"],
        "startTime": ["5"],
        "endTime": ["20"],
        "limit": ["1000"],
    }


def test_paginated_history_keeps_raw_pages_and_receipts(tmp_path, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from v8_next.adapters.binance_capture import validate_capture
    from v8_next.adapters.captured_market import load_settled_funding
    from v8_next.adapters.settlements import (
        final_funding,
        funding_query_windows,
        position_funding_query_coverage,
    )

    urls = []

    def request(url, timeout):
        urls.append(url)
        if "exchangeInfo" in url:
            payload = {"symbols": [{"symbol": "BTCUSDT"}]}
        elif "fundingRate" in url:
            cursor = int(parse_qs(urlsplit(url).query)["startTime"][0])
            payload = [
                dict(symbol="BTCUSDT", fundingTime=t, fundingRate=".0001", markPrice="100")
                for t in range(cursor, min(cursor + 1000, 1007))
            ]
        else:
            payload = []
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("v8_next.adapters.binance_capture.urlopen", request)
    monkeypatch.setattr("v8_next.adapters.funding_history.urlopen", request)
    monkeypatch.setattr("v8_next.adapters.binance_capture.time.time_ns", lambda: 2000_000_000)
    manifest = capture(tmp_path / "pages", funding_start_ms=5)
    validate_capture(manifest)
    assert len([u for u in urls if "fundingRate" in u]) == 2
    assert len(json.loads((manifest.parent / "funding.json").read_text())) == 1000
    assert len(json.loads((manifest.parent / "funding-page-001.json").read_text())) == 2
    assert len(load_settled_funding(manifest, max_age_ns=3000_000_000)) == 1002
    records = final_funding([manifest], 0, 2000_000_000, 2000_000_000)
    assert len(records) == 1002
    windows = funding_query_windows([manifest], 2000_000_000)
    assert len(windows) == 2
    assert windows[0]["end_inclusive_ns"] + 1 == windows[1]["start_inclusive_ns"]
    position = dict(
        instrument_id="BTCUSDT-PERP.BINANCE",
        opened_ns=5_000_000,
        closed_ns=1500_000_000,
        is_closed=True,
    )
    assert (
        position_funding_query_coverage([position], windows, 2000_000_000)[0]["query_status"]
        == "BOUNDED_RESPONSE_COVERS_EXPOSURE"
    )
    assert (
        position_funding_query_coverage([position], windows[:1], 2000_000_000)[0]["query_status"]
        == "EXPOSURE_NOT_FULLY_QUERIED"
    )


@pytest.mark.parametrize("change", ["gap", "overlap", "end", "clock", "terminal"])
def test_funding_page_chain_rejects_discontinuity(tmp_path, change):
    from v8_next.adapters.funding_history import validate_page_chain

    pages = []
    for i, (start, stop) in enumerate(((0, 1000), (1000, 1002))):
        name = "funding.json" if i == 0 else "funding-page-001.json"
        (tmp_path / name).write_text(
            json.dumps([dict(symbol="BTCUSDT", fundingTime=t) for t in range(start, stop)])
        )
        pages.append(
            dict(
                path=name,
                source_url=f"https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime={start}&endTime=2000&limit=1000",
                request_time_ns=3000_000_000,
                received_time_ns=3000_000_000,
            )
        )
    metadata = dict(symbol="BTCUSDT", artifacts=pages)
    validate_page_chain(tmp_path, metadata)
    if change in {"gap", "overlap"}:
        pages[1]["source_url"] = pages[1]["source_url"].replace(
            "startTime=1000", f"startTime={1001 if change == 'gap' else 999}"
        )
    elif change == "end":
        pages[1]["source_url"] = pages[1]["source_url"].replace("endTime=2000", "endTime=2001")
    elif change == "clock":
        pages[1]["request_time_ns"] -= 1
    else:
        (tmp_path / "funding.json").write_text(
            json.dumps([dict(symbol="BTCUSDT", fundingTime=999)])
        )
    with pytest.raises(ValueError):
        validate_page_chain(tmp_path, metadata)
