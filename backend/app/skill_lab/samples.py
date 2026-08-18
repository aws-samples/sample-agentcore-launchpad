"""Built-in Skill Lab demo samples, seeded from the vendored SkillOpt tree.

Upstream Studio materializes demo skills and task sets at startup
(``skillopt_studio/samples.py``); the Launchpad equivalents are native
resources instead of a parallel surface: demo skills become Registry
AGENT_SKILLS records (every Skill Lab wizard picks registry skills) and demo
task sets become Skill Lab tasksets flagged ``sample`` (read-only, runnable).

Only the self-authored demo class is vendored — no paper checkpoints, no
benchmark subsets (see vendor/skillopt/LAUNCHPAD_DEVIATIONS.md for why).

Seeding runs inside the ``skill-lab`` bootstrap stage: idempotent by name per
workspace, per-item degrade (one broken sample must not cost the stage), and
NO validator subprocess — the venv may not exist yet, and the vendored files
are covered by a hermetic test that runs the real validator over them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.errors import AppError
from app.skill_lab.models import SkillLabTaskset
from app.skill_lab.worker_build import VENDOR_ROOT

logger = logging.getLogger(__name__)

DEMO_ROOT = VENDOR_ROOT / "data" / "skilleval_demo"


@dataclass(frozen=True)
class SampleSkill:
    name: str  # registry record name (also the S3 skills/<name>/ prefix)
    source: str  # relative to DEMO_ROOT; dir with SKILL.md, or a lone .md file
    description: str


@dataclass(frozen=True)
class SampleTaskset:
    name: str
    description: str
    mode: str  # single | split
    sources: dict[str, str]  # split -> file relative to DEMO_ROOT


SAMPLE_SKILLS: tuple[SampleSkill, ...] = (
    SampleSkill(
        name="sample-logtriage",
        source="logtriage_skill",
        description=(
            "Demo: log-triage helper (weak baseline) with a bundled parser script "
            "and format doc. Pairs with the logtriage sample task set for "
            "evaluation, and makes a good training starting point."
        ),
    ),
    SampleSkill(
        name="sample-logtriage-v2",
        source="logtriage_skill_v2",
        description=(
            "Demo: multi-document training variant — references/report-template.md "
            "is deliberately wrong so training can fix it. Pairs with the "
            "logtriage sample task set."
        ),
    ),
    SampleSkill(
        name="sample-report",
        source="report_skill/initial.md",
        description=(
            "Demo: minimal CSV-report weak baseline. Pairs with the report or "
            "xlsx sample task sets to show evaluation and training lift."
        ),
    ),
)

SAMPLE_TASKSETS: tuple[SampleTaskset, ...] = (
    SampleTaskset(
        name="Log triage demo tasks",
        description="Built-in sample · pairs with the sample-logtriage skills",
        mode="split",
        sources={
            "train": "logtriage_tasks/train/items.json",
            "val": "logtriage_tasks/val/items.json",
            "test": "logtriage_tasks/test/items.json",
        },
    ),
    SampleTaskset(
        name="CSV report demo tasks",
        description="Built-in sample · pairs with the sample-report skill",
        mode="split",
        sources={
            "train": "report_tasks/train/items.json",
            "val": "report_tasks/val/items.json",
            "test": "report_tasks/test/items.json",
        },
    ),
    SampleTaskset(
        name="Excel handling demo tasks",
        description="Built-in sample · 3 spreadsheet tasks (single mode)",
        mode="single",
        sources={"tasks": "xlsx_tasks.json"},
    ),
)


def _with_frontmatter(skill_md: str, name: str, description: str) -> str:
    """AgentCore Registry refuses SKILL.md without leading `---` frontmatter
    (live ValidationException 2026-08-18); the upstream demo skills carry none
    (Studio's own storage never required it). The vendored files stay
    byte-identical to upstream — the frontmatter is synthesized at seed time.
    json.dumps produces a valid YAML scalar for the colon-carrying description."""
    if skill_md.lstrip("﻿").startswith("---"):
        return skill_md
    return (
        f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n{skill_md}"
    )


def _seed_skills(workspace: Any, log: Callable[[str], None]) -> int:
    import shutil
    from tempfile import TemporaryDirectory

    from app.services import registry_console
    from app.services.skill_ingest import bundle_from_dir, bundle_from_inline

    seeded = 0
    for sample in SAMPLE_SKILLS:
        source = DEMO_ROOT / sample.source
        try:
            stack = TemporaryDirectory(prefix="skill-sample-")
            if source.is_dir():
                staged = Path(stack.name) / source.name
                shutil.copytree(source, staged)
                skill_md_path = staged / "SKILL.md"
                skill_md_path.write_text(
                    _with_frontmatter(
                        skill_md_path.read_text(encoding="utf-8"),
                        sample.name,
                        sample.description,
                    ),
                    encoding="utf-8",
                )
                bundle = bundle_from_dir(staged)
            else:
                bundle = bundle_from_inline(
                    _with_frontmatter(
                        source.read_text(encoding="utf-8"), sample.name, sample.description
                    )
                )
            try:
                registry_console.register_skill_bundle(
                    bundle,
                    workspace,
                    name_override=sample.name,
                    description_override=sample.description,
                )
            finally:
                bundle.close()
                stack.cleanup()
            seeded += 1
            log(f"sample skill registered · {sample.name}")
        except AppError as exc:
            if exc.code == "registry.name_exists":
                log(f"sample skill exists · {sample.name}")
            else:
                logger.warning("sample skill %s skipped: %s", sample.name, exc)
                log(f"sample skill {sample.name} skipped · {exc.code}")
        except Exception as exc:  # noqa: BLE001 — per-item degrade by design
            logger.warning("sample skill %s skipped: %s", sample.name, exc)
            log(f"sample skill {sample.name} skipped · {type(exc).__name__}")
    return seeded


def _seed_tasksets(db: Any, workspace_id: str, log: Callable[[str], None]) -> int:
    from app.skill_lab import tasksets as taskset_svc

    seeded = 0
    for sample in SAMPLE_TASKSETS:
        try:
            exists = db.scalars(
                select(SkillLabTaskset).where(
                    SkillLabTaskset.workspace_id == workspace_id,
                    SkillLabTaskset.name == sample.name,
                )
            ).first()
            if exists is not None:
                log(f"sample task set exists · {sample.name}")
                continue
            tasks_by_split = {
                split: json.loads((DEMO_ROOT / rel).read_text(encoding="utf-8"))
                for split, rel in sample.sources.items()
            }
            taskset_svc.create_taskset(
                db,
                workspace_id,
                name=sample.name,
                description=sample.description,
                mode=sample.mode,
                tasks_by_split=tasks_by_split,
                sample=True,
                run_validator=False,
            )
            seeded += 1
            log(f"sample task set created · {sample.name}")
        except Exception as exc:  # noqa: BLE001 — per-item degrade by design
            logger.warning("sample task set %r skipped: %s", sample.name, exc)
            log(f"sample task set {sample.name!r} skipped · {type(exc).__name__}")
    return seeded


def ensure_skill_lab_samples(
    db: Any, workspace_id: str, workspace: Any, log: Callable[[str], None] = lambda _m: None
) -> str:
    """Seed the demo samples for one workspace; returns a short stage detail."""
    if not DEMO_ROOT.is_dir():
        log("vendored demo samples missing — skipping sample seeding")
        return "samples unavailable"
    skills = _seed_skills(workspace, log)
    tasksets = _seed_tasksets(db, workspace_id, log)
    return f"{skills} sample skills · {tasksets} sample task sets seeded"
