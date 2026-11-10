"""Refresh the personal evaluation datasets of recorded students from the shipped assets.

    python3 refresh_datasets.py                          # dry run (default): diff only
    python3 refresh_datasets.py --apply                  # every student in the manifest
    python3 refresh_datasets.py --student lab01 --apply

``student_kit.py`` creates ``itsm-exam-8-cn-<labNN>`` / ``itsm-held-out-cn-<labNN>`` once and
never touches them again. When the shipped assets change (``lab-assets/itsm-exam-8-cn.json``,
``itsm-held-out-cn.json`` or the read-only overrides in ``student_kit_lib.READ_ONLY_ITEMS``),
this script re-derives each recorded dataset with ``adapt_dataset_document`` and, with
``--apply``, PUTs the new ``description`` and ``items`` to ``PUT /api/eval/datasets/{id}`` as
the STUDENT (the dataset's creator; the route is creator-or-admin). Scenario ids, splits and
the item count are checked to stay the same, so completed evaluation runs that replayed the
old items remain comparable by case id. Nothing else is touched: no Agent, no run, no
knowledge base, no account. A dataset the manifest does not record for the student is
reported and skipped (never adopted); a recorded id the student's dataset list no longer has
is reported.
Every run writes ``<state_dir>/evidence/refresh-datasets-<ts>.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from student_kit_lib import SHARED_DATASETS, STUDENTS, adapt_dataset_document


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--student", choices=STUDENTS,
                   help="the learner whose datasets to refresh (default: every recorded one)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="read the live datasets and print what would change (default)")
    mode.add_argument("--apply", action="store_true", help="PUT the refreshed items")
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


def item_diff(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    """Scenario ids whose input / expected response / assertions differ."""
    changed = []
    for a, b in zip(old, new, strict=True):
        if (a.get("turns"), a.get("assertions")) != (b.get("turns"), b.get("assertions")):
            changed.append(str(b.get("scenario_id")))
    return changed


def check_shape(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> None:
    """The refresh must keep ids, splits and count so run comparisons by case id survive."""
    if len(old) != len(new):
        raise RuntimeError(f"item count changed {len(old)} -> {len(new)}; refusing")
    for a, b in zip(old, new, strict=True):
        if (a.get("scenario_id"), a.get("split")) != (b.get("scenario_id"), b.get("split")):
            raise RuntimeError(
                f"scenario id / split changed {a.get('scenario_id')}/{a.get('split')} -> "
                f"{b.get('scenario_id')}/{b.get('split')}; refusing")


def refresh(lab, manifest: dict[str, Any], student: str, *, apply: bool, log=print) -> list[dict]:
    row = (manifest.get("students") or {}).get(student)
    if row is None:
        raise SystemExit(f"{student} is not recorded in the manifest (exit 2)") from None
    recorded = row.get("datasets") or {}
    c = lab.session(student)
    listed = lab.api(c, "GET", "/api/eval/datasets")["datasets"]  # the list carries the items
    live = {d["name"]: d for d in listed}
    live_by_id = {d["id"]: d for d in listed}
    results = []
    for shared in SHARED_DATASETS:
        document = json.loads((lab.ROOT / "lab-assets" / f"{shared}.json").read_text())
        body = adapt_dataset_document(document, student)
        name = body["name"]
        entry: dict[str, Any] = {"student": student, "dataset": name}
        rec = recorded.get(name)
        if not rec or not rec.get("id"):
            entry["action"] = "SKIPPED (not recorded on the manifest row; run student_kit.py)"
            log(f"  [{name}] {entry['action']}")
            results.append(entry)
            continue
        current = live_by_id.get(rec["id"])
        if current is None:
            entry["action"] = f"MISSING (id {rec['id']} not on the console)"
            log(f"  [{name}] {entry['action']}")
            results.append(entry)
            continue
        if name in live and live[name]["id"] != rec["id"]:
            entry["action"] = (f"CONFLICT (console {name} is {live[name]['id']}, manifest "
                               f"{rec['id']}); not touched")
            log(f"  [{name}] {entry['action']}")
            results.append(entry)
            continue
        old_items = list(current.get("items") or [])
        check_shape(old_items, body["items"])
        changed = item_diff(old_items, body["items"])
        entry.update({"id": rec["id"], "changed_scenarios": changed})
        if not changed and current.get("description") == body["description"]:
            entry["action"] = "UNCHANGED"
            log(f"  [{name}] UNCHANGED ({len(old_items)} items)")
        elif apply:
            out = lab.api(c, "PUT", f"/api/eval/datasets/{rec['id']}",
                          {"description": body["description"], "items": body["items"]})
            rec["revision"] = out.get("revision", rec.get("revision"))
            rec["item_count"] = out.get("item_count", rec.get("item_count"))
            rec["refreshed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["action"] = f"REFRESHED scenarios {', '.join(changed) or '(description)'}"
            log(f"  [{name}] {entry['action']} -> revision {rec.get('revision')}")
        else:
            entry["action"] = f"WOULD REFRESH scenarios {', '.join(changed) or '(description)'}"
            log(f"  [{name}] {entry['action']}")
        results.append(entry)
    return results


def main(argv=None) -> int:
    import lab

    args = parse_args(argv)
    lab.sync_assets()  # N17-obs558: the shipped datasets follow the package
    manifest = lab.load()
    students = manifest.get("students") or {}
    if args.student:
        if args.student not in students:
            print(f"{args.student} is not recorded in the manifest", file=sys.stderr)
            return 2
        students = {args.student: students[args.student]}
    if not students:
        print("No students recorded in the manifest; nothing to refresh")
        return 0
    print("DRY RUN — nothing is written" if args.dry_run else "APPLY", flush=True)
    results: list[dict] = []
    for student in students:
        print(student, flush=True)
        results.extend(refresh(lab, manifest, student, apply=args.apply))
    if args.apply:
        lab.merge_save(manifest, ["students"])
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    lab.evidence(f"refresh-datasets-{ts}.json",
                 {"mode": "apply" if args.apply else "dry-run", "results": results})
    return 0


if __name__ == "__main__":
    sys.exit(main())
