"""N17-obs424: what the dataset console relies on — a refusal names WHERE and WHY in a
machine-readable ``detail`` (the console words it in the reader's language), and the full
scenario shape the per-scenario editor writes round-trips verbatim with its revision."""

import json


def _create(client, items, **over):
    return client.post("/api/eval/datasets", json={"name": "c", "items": items, **over})


def test_an_invalid_record_names_its_item_turn_and_reason(client):
    res = _create(client, [{"scenario_id": "a", "turns": [{"input": "x"}, {"input": " "}]}])
    assert res.status_code == 422
    assert res.json()["detail"] == {"reason": "turn_input_required", "item": 1, "turn": 2}
    dup = _create(client, [{"scenario_id": "a", "turns": [{"input": "x"}]},
                           {"scenario_id": "a", "turns": [{"input": "y"}]}])
    assert dup.json()["detail"] == {"reason": "scenario_id_duplicate", "item": 2,
                                    "scenario_id": "a"}
    split = _create(client, [{"scenario_id": "a", "turns": [{"input": "x"}], "split": "dev"}])
    assert split.json()["detail"] == {"reason": "split_invalid", "item": 1}


def test_a_jsonl_refusal_names_its_line(client):
    bad = client.post("/api/eval/datasets/upload", json={
        "name": "u", "jsonl": '{"prompt": "ok"}\n{not json}\n'})
    assert bad.status_code == 422
    assert (bad.json()["code"], bad.json()["detail"]) == ("dataset.invalid_jsonl", {"line": 2})
    arr = client.post("/api/eval/datasets/upload", json={"name": "u", "jsonl": "[1, 2]\n"})
    assert arr.json()["detail"] == {"reason": "not_an_object", "line": 1}


def test_the_editor_shape_round_trips_with_its_revision(client):
    item = {"scenario_id": "refund-1",
            "turns": [{"input": "where?", "expected_response": "today"}, {"input": "refund?"}],
            "assertions": ["mentions the order"], "expected_trajectory": [], "split": "held_out",
            "metadata": {"launchpad_assets": {"golden_test_id": "G1"}}}
    created = _create(client, [item]).json()
    assert created["items"] == [item] and created["revision"] == 1
    item2 = {**item, "turns": [*item["turns"], {"input": "bye"}]}
    updated = client.put(f"/api/eval/datasets/{created['id']}", json={"items": [item2]}).json()
    assert updated["items"] == [item2] and updated["revision"] == 2
    up = client.post("/api/eval/datasets/upload", json={
        "name": "u2", "jsonl": json.dumps(item) + "\n"}).json()
    assert up["items"] == [item] and up["item_splits"] == ["held_out"]
