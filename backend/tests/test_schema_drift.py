"""SCRUM-67: the suite's schema comes from `alembic upgrade head` (see
tests/conftest.py), not Base.metadata.create_all() -- so this is what keeps
app/models.py honest: any model change without a matching migration (or a
hand-edited migration that no longer matches its model) fails here, the
same diff `alembic revision --autogenerate` would have produced.
"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.database import Base, engine


def test_orm_models_match_migrated_schema():
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)

    assert diff == []
