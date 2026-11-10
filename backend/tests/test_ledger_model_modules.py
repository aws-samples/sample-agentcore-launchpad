"""`init_db` imports every ORM model module itself (N11-r5-bug3): `create_all` only
builds the tables of models that are imported, and the bootstrap CLI never imports the
API app. This pins `app.core.db.MODEL_MODULES` to the modules that actually declare a
`Base` table, so a new model module cannot be forgotten."""

import re
from pathlib import Path

from app.core import db

APP = Path(db.__file__).resolve().parents[1]
_BASE_CLASS = re.compile(r"^class \w+\(Base\):", re.M)


def _modules_declaring_base_tables() -> set[str]:
    found = set()
    for path in APP.rglob("*.py"):
        if _BASE_CLASS.search(path.read_text(encoding="utf-8")):
            found.add("app." + ".".join(path.relative_to(APP).with_suffix("").parts))
    return found


def test_model_modules_lists_exactly_the_modules_that_declare_base_tables():
    assert set(db.MODEL_MODULES) == _modules_declaring_base_tables()


def test_import_model_modules_makes_the_metadata_complete():
    db.import_model_modules()
    tables = set(db.Base.metadata.tables)
    for expected in ("workspaces", "registry_records", "skill_lab_tasksets", "skill_lab_jobs",
                     "eval_results", "experiments", "assistant_conversations", "announcements"):
        assert expected in tables, expected
