"""The retired configuration-bundle experiment (N13-obs2).

Its routes, stage machine and console surfaces are gone; ``routing`` ignores its rows.
The ``experiments`` table is kept unmigrated so an existing ledger keeps loading — this
module only counts what is left so the startup log says so.
"""

from app.core.db import SessionLocal
from app.optimization.models import Experiment


def count_legacy_experiment_rows() -> int:
    db = SessionLocal()
    try:
        return int(db.query(Experiment).count())
    finally:
        db.close()
