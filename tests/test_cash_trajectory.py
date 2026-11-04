import json

from v8_next.evaluation.trajectory import cash_trajectory


def test_prefix_replay_never_uses_future_capture_or_bridges_missing_cash(tmp_path, monkeypatch):
    manifests = []
    for index in range(1, 5):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps({"artifacts": [{"received_time_ns": index * 10}]}))
        manifests.append(path)
    seen = []

    def native(paths, config, *, observer):
        seen.append((len(paths), observer))
        assert paths == manifests[: len(paths)]
        return {"campaigns": []}

    def accounting(paths, campaigns, config, cutoff):
        assert cutoff == len(paths) * 10
        return {"count": len(paths)}

    monkeypatch.setattr("v8_next.evaluation.trajectory.replay_account", native)
    monkeypatch.setattr("v8_next.evaluation.trajectory.replay_frozen_campaigns", accounting)
    monkeypatch.setattr(
        "v8_next.evaluation.trajectory.terminal_cash_return",
        lambda account, initial: {"return": ["0.1", None, "0.3", "0.4"][account["count"] - 1]},
    )
    result = cash_trajectory(manifests, {"initial_balance": "10000"}, 0, 50)
    assert [row["squeeze"]["incremental_cash_return"] for row in result["rows"]] == [
        "0.1",
        None,
        None,
        "0.1",
    ]
    assert seen == [
        (i, observer) for i in range(1, 5) for observer in ("breakout_baseline", "squeeze")
    ]
    assert result["inference_eligible"] is False
