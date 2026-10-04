"""
One-off: create the default "Green Valley School" tenant and attach the existing
single-school data to it, so upgrading an existing install does not orphan rows.

Every student/payment created before multi-tenancy existed has school_id = NULL.
This script moves them under one default school so they stay visible to the
school that already owns them. It is safe to run twice.

Usage (from the backend/ folder):
    python migrate_tenancy.py
"""

import sys

from database import Base, SessionLocal, engine
from models import Payment, School, Student, User
from auth import PLAN_LIMITS, hash_password

DEFAULT_SCHOOL_NAME = "Green Valley School"
DEFAULT_SCHOOL_CODE = "GVHS"
DEFAULT_ADMIN_EMAIL = "admin@greenvalley.sc.ke"


def main() -> None:
    # Make sure schools/users tables exist before touching them.
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        school = db.query(School).filter(School.code == DEFAULT_SCHOOL_CODE).first()

        if school is None:
            school = School(
                name=DEFAULT_SCHOOL_NAME,
                code=DEFAULT_SCHOOL_CODE,
                plan="pro",
                # Must match the plan, otherwise the school is capped at the
                # model default (500) while showing as "pro" (1000).
                max_students=PLAN_LIMITS["pro"],
                subscription_status="active",
            )
            db.add(school)
            db.flush()
            print(f"Created default school: {school.name} (#{school.id})")
        else:
            print(f"Default school already exists: {school.name} (#{school.id})")

        orphan_students = db.query(Student).filter(Student.school_id.is_(None)).count()
        if orphan_students:
            db.query(Student).filter(Student.school_id.is_(None)).update(
                {Student.school_id: school.id}, synchronize_session=False
            )
            print(f"Attached {orphan_students} existing student(s) to {school.name}")

        orphan_payments = db.query(Payment).filter(Payment.school_id.is_(None)).count()
        if orphan_payments:
            db.query(Payment).filter(Payment.school_id.is_(None)).update(
                {Payment.school_id: school.id}, synchronize_session=False
            )
            print(f"Attached {orphan_payments} existing payment(s) to {school.name}")

        # Give the school an admin login if it does not have one yet, so the
        # existing install is reachable after the auth layer goes in.
        if not db.query(User).filter(User.school_id == school.id).first():
            # Read from the environment. A real password must never be written
            # into source control, so there is no default.
            import os

            password = os.getenv("SCHOOLPAY_BOOTSTRAP_PASSWORD")
            if not password:
                print(
                    "\nA school already exists but has no login.\n"
                    "Set SCHOOLPAY_BOOTSTRAP_PASSWORD and re-run to create one:\n"
                    "  set SCHOOLPAY_BOOTSTRAP_PASSWORD=your-password\n"
                )
                print("Done.")
                return
            if len(password) < 8:
                print("SCHOOLPAY_BOOTSTRAP_PASSWORD must be at least 8 characters.")
                return

            db.add(
                User(
                    school_id=school.id,
                    email=DEFAULT_ADMIN_EMAIL,
                    full_name="Administrator",
                    password_hash=hash_password(password),
                    role="admin",
                )
            )
            print(f"Created admin login: {DEFAULT_ADMIN_EMAIL} / {password}")

        # Repair any school whose stored seat limit does not match its plan.
        # A mismatch means the school is silently capped at the wrong number,
        # which looks like a bug to the customer ("pro but only 500 students").
        repaired = 0
        for candidate in db.query(School).all():
            expected = PLAN_LIMITS.get(candidate.plan)
            if expected is not None and candidate.max_students != expected:
                print(f"  fixed seat limit for {candidate.name}: {candidate.plan} "
                      f"{candidate.max_students} -> {expected}")
                candidate.max_students = expected
                repaired += 1
        if repaired:
            print(f"Repaired {repaired} school seat limit(s) to match their plan.")

        db.commit()
        print("\nDone. Sign in with the credentials above, or register a new school at /api/auth/register-school.")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())