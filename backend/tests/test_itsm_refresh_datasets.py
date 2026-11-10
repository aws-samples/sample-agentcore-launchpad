"""samples/itsm-cn ``refresh_datasets.py`` (manual 1.9 D1): the personal exam datasets are
re-derived from the shipped assets and PUT as the student, against a stub ``lab``.

Also pins the dataset change itself: T02 names the student's CRM ticket and T02 / T03 accept
whatever state the tool returns, because the manual's scenario two (4.2.5 ``add_comment``)
moves that ticket from ``on_hold`` to ``in_progress`` before scenario three evaluates it.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples/itsm-cn"
SCRIPTS = SAMPLE / "scripts"


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def lib():
    sys.path.insert(0, str(SCRIPTS))
    return module(SCRIPTS / "student_kit_lib.py")


@pytest.fixture(scope="module")
def shared():
    return {
        name: json.loads((SAMPLE / "lab-assets" / f"{name}.json").read_text())
        for name in ("itsm-exam-8-cn", "itsm-held-out-cn")
    }


# ----- the dataset change ------------------------------------------------------------------


def test_t02_names_the_ticket_and_t02_t03_accept_either_state(lib, shared):
    """D1: a student who did 4.2.5 has ``in_progress``, one who skipped it ``on_hold``."""
    exam = shared["itsm-exam-8-cn"]["items"]
    for student, inc in (("lab01", "INC0020001"), ("lab07", "INC0020007")):
        by_id = {i["scenario_id"]: i for i in lib.adapt_dataset(exam, student)}
        t02 = by_id["T02"]["turns"][0]
        assert inc in t02["input"] and t02["input"].startswith(f"我是{student}")
        assert "挂起的" not in t02["input"], "the question must not presume the state"
        assert "只查询不要修改" in t02["input"]
        assert "on_hold" in t02["expected_response"] and "in_progress" in t02["expected_response"]
        expected = t02["expected_response"]
        assert "企业微信版本" in expected and "5个工作日" in expected
        assert any("不假定仍为挂起" in a for a in by_id["T02"]["assertions"])
        assert any(inc in a for a in by_id["T02"]["assertions"])
        t03 = by_id["T03"]["turns"][0]
        assert inc in t03["input"] and "CRM支持组" in t03["expected_response"]
        assert "挂起待补材料" not in t03["expected_response"]
        assert "按实际返回" in t03["expected_response"]
    # the shared asset itself (the organizer's shared dataset) carries the same wording
    shared_t02 = next(i for i in exam if i["scenario_id"] == "T02")["turns"][0]
    assert "挂起的" not in shared_t02["input"] and "INC0020001" in shared_t02["input"]
    assert "in_progress" in shared_t02["expected_response"]


def test_adaptation_stays_idempotent_and_shape_preserving(lib, shared):
    exam = shared["itsm-exam-8-cn"]["items"]
    adapted = lib.adapt_dataset(exam, "lab03")
    assert lib.adapt_dataset(adapted, "lab03") == adapted
    assert [(i["scenario_id"], i["split"]) for i in adapted] == [
        (i["scenario_id"], i["split"]) for i in exam]
    assert "lab01" not in json.dumps(adapted, ensure_ascii=False)


# ----- the refresh script against a stub lab ------------------------------------------------


def stale_items(lib, shared, student):
    """What the kit created before this change: the old T02 question and expectations."""
    items = lib.adapt_dataset(shared["itsm-exam-8-cn"]["items"], student)
    inc = lib.tickets_for(student)["inc"]
    t02 = next(i for i in items if i["scenario_id"] == "T02")
    t02["turns"][0]["input"] = f"我是{student}，帮我查那张挂起的CRM单，需要补充什么？"
    t02["turns"][0]["expected_response"] = (
        f"只读查询{inc}，说明需要补充企业微信版本、错误截图和发生时间；挂起超过5个工作日不补充自动关闭。"
        "不代为添加评论或修改工单。")
    t02["assertions"] = [f"实际查询本人挂起工单{inc}", "完整列出待补材料", "提醒5个工作日超时关闭",
                         "不执行写操作"]
    return items


def stub_lab(root, lib, shared, *, students):
    stub = types.ModuleType("lab")
    stub.ROOT = root
    stub.sync_assets = lambda: []  # N17-obs558
    (root / "lab-assets").mkdir()
    for name, document in shared.items():
        (root / "lab-assets" / f"{name}.json").write_text(json.dumps(document, ensure_ascii=False))
    stub.calls = []
    stub.datasets = {}  # id -> live dataset row (as the console lists it)
    rows = {}
    for n, student in students.items():
        exam_id, held_id = f"ds-exam-{n}", f"ds-held-{n}"
        stub.datasets[exam_id] = {
            "id": exam_id, "name": f"itsm-exam-8-cn-{student}", "revision": 1,
            "description": "old", "items": stale_items(lib, shared, student),
            "created_by_name": student}
        held = lib.adapt_dataset_document(shared["itsm-held-out-cn"], student)
        stub.datasets[held_id] = {
            "id": held_id, "name": held["name"], "revision": 1,
            "description": held["description"], "items": held["items"],
            "created_by_name": student}
        rows[student] = {
            "staff": {"username": f"itdesk{n:02}"},
            "datasets": {
                f"itsm-exam-8-cn-{student}": {"id": exam_id, "created_by": student},
                held["name"]: {"id": held_id, "created_by": student},
            },
        }
    manifest = {"model_id": "m", "workspace": "default", "students": rows}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    stub.load = lambda: json.loads((root / "manifest.json").read_text())

    def merge_save(state, keys):
        stub.calls.append(("merge_save", tuple(keys)))
        current = stub.load()
        for k in keys:
            current[k] = state[k]
        (root / "manifest.json").write_text(json.dumps(current, ensure_ascii=False))

    stub.merge_save = merge_save
    stub.evidence = lambda name, obj: ((root / "evidence").mkdir(exist_ok=True),
                                       (root / "evidence" / name).write_text(
                                           json.dumps(obj, ensure_ascii=False)))

    def session(user=None):
        stub.calls.append(("login", user))
        return user

    def api(c, method, path, body=None, **kw):
        stub.calls.append((c, method, path))
        if method == "GET" and path == "/api/eval/datasets":
            return {"datasets": [copy.deepcopy(d) for d in stub.datasets.values()
                                 if d["created_by_name"] == c]}
        if method == "PUT" and path.startswith("/api/eval/datasets/"):
            ds = stub.datasets[path.rsplit("/", 1)[1]]
            assert ds["created_by_name"] == c, "PUT must be issued as the creator"
            assert set(body) <= {"description", "items", "name"}
            ds.update(body)
            ds["revision"] += 1
            return copy.deepcopy(ds) | {"item_count": len(ds["items"])}
        raise AssertionError(f"unexpected API call {method} {path}")

    stub.session, stub.api = session, api
    return stub


@pytest.fixture
def world(tmp_path, monkeypatch, lib, shared):
    stub = stub_lab(tmp_path, lib, shared, students={1: "lab01", 3: "lab03"})
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "lab", stub)
    return module(SCRIPTS / "refresh_datasets.py"), stub


def test_dry_run_reports_t02_only_and_writes_nothing(world, capsys):
    rd, stub = world
    assert rd.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert out.count("WOULD REFRESH scenarios T02") == 2  # one exam set per student
    assert out.count("UNCHANGED") == 2  # the held-out sets did not change
    assert all(call[1] == "GET" for call in stub.calls if len(call) == 3)
    assert ("merge_save", ("students",)) not in stub.calls
    assert stub.datasets["ds-exam-1"]["revision"] == 1
    evidence = list((stub.ROOT / "evidence").iterdir())
    assert len(evidence) == 1 and "dry-run" in evidence[0].read_text()


def test_apply_puts_as_the_student_and_keeps_ids_and_splits(world, lib, capsys):
    rd, stub = world
    before = [(i["scenario_id"], i["split"]) for i in stub.datasets["ds-exam-1"]["items"]]
    assert rd.main(["--student", "lab01", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "REFRESHED scenarios T02 -> revision 2" in out
    puts = [c for c in stub.calls if len(c) == 3 and c[1] == "PUT"]
    assert puts == [("lab01", "PUT", "/api/eval/datasets/ds-exam-1")]
    live = stub.datasets["ds-exam-1"]
    assert [(i["scenario_id"], i["split"]) for i in live["items"]] == before
    t02 = next(i for i in live["items"] if i["scenario_id"] == "T02")["turns"][0]
    assert "INC0020001" in t02["input"] and "挂起的" not in t02["input"]
    assert "lab01 个人只读版" in live["description"]
    # lab03 untouched; the manifest row records the new revision
    assert stub.datasets["ds-exam-3"]["revision"] == 1
    manifest = stub.load()
    row = manifest["students"]["lab01"]["datasets"]["itsm-exam-8-cn-lab01"]
    assert row["revision"] == 2 and row["item_count"] == 8 and row["refreshed_at"]
    assert ("merge_save", ("students",)) in stub.calls
    # a second apply is a no-op
    assert rd.main(["--student", "lab01", "--apply"]) == 0
    assert "UNCHANGED" in capsys.readouterr().out and stub.datasets["ds-exam-1"]["revision"] == 2


def test_unrecorded_or_missing_datasets_are_reported_not_adopted(world, capsys):
    rd, stub = world
    manifest = stub.load()
    del manifest["students"]["lab03"]["datasets"]["itsm-exam-8-cn-lab03"]
    manifest["students"]["lab03"]["datasets"]["itsm-held-out-cn-lab03"]["id"] = "ds-gone"
    (stub.ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    assert rd.main(["--student", "lab03", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "SKIPPED (not recorded" in out and "MISSING (id ds-gone" in out
    assert not [c for c in stub.calls if len(c) == 3 and c[1] == "PUT"]


def test_unknown_student_exits_2(world, capsys):
    rd, _ = world
    assert rd.main(["--student", "lab09"]) == 2


def test_shape_guard_refuses_a_changed_scenario_list(world):
    rd, _ = world
    old = [{"scenario_id": "T01", "split": "tuning"}, {"scenario_id": "T02", "split": "tuning"}]
    with pytest.raises(RuntimeError, match="count changed"):
        rd.check_shape(old, old[:1])
    with pytest.raises(RuntimeError, match="scenario id / split changed"):
        rd.check_shape(old, [old[0], {"scenario_id": "T02", "split": "held_out"}])
