"""
Tenant isolation enforced centrally.

The risk with multi-tenant SaaS is missing a single `filter(... school_id ==)` in
one of hundreds of query sites and leaking one customer's data to another. Rather
than relying on every call site remembering to filter, isolation is applied once
here with a SQLAlchemy `do_orm_execute` hook.

How it works
------------
Each request gets its own Session from `get_db()`. `bind_tenant()` stamps that
session with the caller's school id. Before every ORM statement executes on that
session, this listener appends `school_id == <school>` to the WHERE clause for
any model that has a `school_id` column.

Consequences, by design:
  * A query with no tenant filter can only ever return the caller's own rows.
  * A deliberately broad query (`db.query(Student)`) is still scoped.
  * Cross-tenant access needs an explicit bypass via `unscoped_query()`.

Models WITHOUT a `school_id` column (FeeCategory, ExpenseCategory, and similar
reference/configuration tables) are intentionally left global so shared fee
structures can be reused across schools.
"""

from contextlib import contextmanager

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

TENANT_ATTR = "tenant_school_id"


def bind_tenant(db: Session, school_id: int) -> Session:
    """Scope this session to one school for the rest of its request."""
    setattr(db, TENANT_ATTR, school_id)
    return db


@contextmanager
def unscoped_query(db: Session):
    """Temporarily run queries without the tenant filter.

    Only for legitimate cross-tenant work such as platform-wide administration or
    a lookup by a globally unique key. Every use should be a deliberate,
    reviewable decision.
    """
    previous = getattr(db, TENANT_ATTR, None)
    try:
        setattr(db, TENANT_ATTR, None)
        yield db
    finally:
        setattr(db, TENANT_ATTR, previous)


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state) -> None:
    school_id = getattr(state.session, TENANT_ATTR, None)
    if school_id is None:
        return

    statement = state.statement
    descriptions = getattr(statement, "column_descriptions", None)
    if not descriptions:
        return

    # The same entity can appear more than once in a join; only scope it once.
    seen: set[str] = set()

    for column_description in descriptions:
        model = column_description.get("entity")
        if model is None:
            continue

        mapper = inspect(model)
        if "school_id" not in {column.key for column in mapper.columns}:
            continue

        key = getattr(model, "__name__", str(model))
        if key in seen:
            continue
        seen.add(key)

        state.statement = statement.where(model.school_id == school_id)