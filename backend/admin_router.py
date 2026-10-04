"""
Platform administration API (cross-tenant).

Everything here is guarded by `require_platform_admin`, so a school admin calling
one of these routes gets 403 rather than accidentally seeing another school's
data.

Deliberately uses `unscoped_query()`: these routes are exactly the one place
where cross-tenant reads are intended. Every such call is a conscious decision,
and they are all in this file so they are easy to review.
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from auth import PLAN_LIMITS, hash_password, require_platform_admin
from database import get_db
from models import Expense, Invoice, Payment, School, Student, User
from schemas import (
    AdminPaymentResponse,
    AdminStudentResponse,
    AdminUserCreate,
    AdminUserResponse,
    AdminUserUpdate,
    PlatformStats,
    SchoolCreate,
    SchoolResponse,
    SchoolUpdate,
)
from tenancy import unscoped_query

router = APIRouter(prefix="/api/admin", tags=["platform-admin"])


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=PlatformStats)
def platform_stats(
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        schools = db.query(School).all()
        total_students = db.query(Student).count()
        total_payments = db.query(Payment).count()

        value = db.query(Payment).with_entities(Payment.amount).all()
        total_value = sum((Decimal(row[0]) for row in value), Decimal("0"))

        plans: dict[str, int] = {}
        statuses: dict[str, int] = {}
        for school in schools:
            plans[school.plan] = plans.get(school.plan, 0) + 1
            statuses[school.subscription_status] = statuses.get(school.subscription_status, 0) + 1

        at_capacity = sum(1 for s in schools if s.max_students is not None and s.student_count >= s.max_students)

        return PlatformStats(
            total_schools=len(schools),
            active_subscriptions=statuses.get("active", 0),
            trialing=sum(1 for s in schools if s.plan == "trial"),
            cancelled=statuses.get("cancelled", 0) + statuses.get("expired", 0),
            total_students=total_students,
            total_payments=total_payments,
            total_payments_value=total_value,
            schools_at_capacity=at_capacity,
            all_plans=plans,
            all_statuses=statuses,
        )


# ---------------------------------------------------------------------------
# Schools - full CRUD
# ---------------------------------------------------------------------------

@router.get("/schools", response_model=list[SchoolResponse])
def list_all_schools(
    search: str | None = None,
    plan: str | None = None,
    status: str | None = None,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        query = db.query(School)
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(School.name.ilike(term) | School.code.ilike(term) | School.email.ilike(term))
        if plan:
            query = query.filter(School.plan == plan)
        if status:
            query = query.filter(School.subscription_status == status)
        return query.order_by(School.name).all()


@router.post("/schools", response_model=SchoolResponse, status_code=201)
def create_school(
    payload: SchoolCreate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    code = payload.code.strip().upper()
    with unscoped_query(db):
        if db.query(School).filter(School.code == code).first():
            raise HTTPException(status_code=400, detail=f"School code '{code}' is already taken")
        if db.query(User).filter(User.email == payload.admin_email.strip().lower()).first():
            raise HTTPException(status_code=400, detail="That admin email is already registered")

    plan = payload.plan if payload.plan in PLAN_LIMITS else "trial"
    school = School(
        name=payload.name.strip(),
        code=code,
        email=(payload.email or "").strip() or None,
        phone=payload.phone,
        address=payload.address,
        plan=plan,
        max_students=PLAN_LIMITS.get(plan, 50),
        subscription_status="active",
    )
    db.add(school)
    db.flush()
    db.add(
        User(
            school_id=school.id,
            email=payload.admin_email.strip().lower(),
            full_name=payload.admin_name,
            password_hash=hash_password(payload.admin_password),
            role="admin",
        )
    )
    db.commit()
    db.refresh(school)
    return school


@router.get("/schools/{school_id}", response_model=SchoolResponse)
def get_school(school_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    with unscoped_query(db):
        school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    return school


@router.put("/schools/{school_id}", response_model=SchoolResponse)
def update_school(
    school_id: int,
    payload: SchoolUpdate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        school = db.query(School).filter(School.id == school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail="School not found")

        data = payload.model_dump(exclude_unset=True)

        # Changing the plan has to move the seat limit with it, otherwise a
        # school upgraded to "pro" would still be capped at the trial number.
        if "plan" in data and data["plan"] in PLAN_LIMITS:
            school.plan = data["plan"]
            school.max_students = PLAN_LIMITS[school.plan]

        for field, value in data.items():
            if field == "plan":
                continue
            setattr(school, field, value)

        db.commit()
        db.refresh(school)
    return school


@router.delete("/schools/{school_id}")
def delete_school(school_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    """Delete a school and everything it owns.

    Students and payments are removed too, because leaving a school's financial
    records behind after the customer has been deleted is worse than a clean
    delete - this is a genuine "cancel and remove" action, not a soft delete.
    """
    with unscoped_query(db):
        school = db.query(School).filter(School.id == school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail="School not found")

        name = school.name
        student_ids = [row[0] for row in db.query(Student.id).filter(Student.school_id == school_id).all()]

        db.query(Payment).filter(Payment.school_id == school_id).delete(synchronize_session=False)
        if student_ids:
            db.query(Invoice).filter(Invoice.student_id.in_(student_ids)).delete(synchronize_session=False)
        db.query(Student).filter(Student.school_id == school_id).delete(synchronize_session=False)
        db.query(Expense).filter(Expense.school_id == school_id).delete(synchronize_session=False)
        db.query(User).filter(User.school_id == school_id).delete(synchronize_session=False)
        db.delete(school)
        db.commit()

    return {"message": f"School '{name}' and all of its data were deleted"}


# ---------------------------------------------------------------------------
# Users - full CRUD across all schools
# ---------------------------------------------------------------------------

def _user_row(db: Session, user: User) -> AdminUserResponse:
    school_name = None
    if user.school_id:
        with unscoped_query(db):
            school = db.query(School).filter(School.id == user.school_id).first()
        school_name = school.name if school else None
    return AdminUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
        is_active=user.is_active,
        school_id=user.school_id,
        school_name=school_name,
        created_at=user.created_at,
    )


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(
    search: str | None = None,
    school_id: int | None = None,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        query = db.query(User)
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(User.email.ilike(term) | User.full_name.ilike(term))
        if school_id is not None:
            query = query.filter(User.school_id == school_id)
        users = query.order_by(User.email).all()
        return [_user_row(db, u) for u in users]


@router.post("/users", response_model=AdminUserResponse, status_code=201)
def create_user(payload: AdminUserCreate, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    with unscoped_query(db):
        if db.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=400, detail="That email is already registered")
        if payload.school_id is not None and not db.query(School).filter(School.id == payload.school_id).first():
            raise HTTPException(status_code=400, detail="That school does not exist")

        user = User(
            email=email,
            full_name=payload.full_name,
            password_hash=hash_password(payload.password),
            school_id=payload.school_id,
            role=payload.role,
            is_platform_admin=payload.is_platform_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return _user_row(db, user)


@router.put("/users/{user_id}", response_model=AdminUserResponse)
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Guard against locking yourself out of the platform.
        if user.id == admin.id and payload.is_platform_admin is False:
            raise HTTPException(status_code=400, detail="You cannot remove your own platform admin rights")

        data = payload.model_dump(exclude_unset=True)
        password = data.pop("password", None)
        if password:
            user.password_hash = hash_password(password)
        for field, value in data.items():
            setattr(user, field, value)

        db.commit()
        db.refresh(user)
        return _user_row(db, user)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    with unscoped_query(db):
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user.id == admin.id:
            raise HTTPException(status_code=400, detail="You cannot delete your own account")

        email = user.email
        db.delete(user)
        db.commit()

    return {"message": f"User '{email}' deleted"}


# ---------------------------------------------------------------------------
# Students - view and manage across every school
# ---------------------------------------------------------------------------

@router.get("/students", response_model=list[AdminStudentResponse])
def list_all_students(
    school_id: int | None = None,
    search: str | None = None,
    limit: int = Query(default=200, le=1000),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        query = db.query(Student, School.name).outerjoin(School, Student.school_id == School.id)
        if school_id is not None:
            query = query.filter(Student.school_id == school_id)
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                Student.admission_number.ilike(term)
                | Student.first_name.ilike(term)
                | Student.last_name.ilike(term)
            )
        rows = query.order_by(Student.id.desc()).limit(limit).all()

        return [
            AdminStudentResponse(
                id=s.id,
                school_id=s.school_id,
                school_name=school_name,
                admission_number=s.admission_number,
                first_name=s.first_name,
                last_name=s.last_name,
                class_name=s.class_name,
                parent_name=s.parent_name,
                parent_phone=s.parent_phone,
                fee_balance=s.fee_balance or 0,
                status=s.status,
                photo_url=s.photo_url,
            )
            for s, school_name in rows
        ]


@router.delete("/students/{student_id}")
def delete_any_student(student_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    with unscoped_query(db):
        student = db.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        name = f"{student.first_name} {student.last_name}"
        db.query(Payment).filter(Payment.student_id == student_id).delete(synchronize_session=False)
        db.delete(student)
        db.commit()
    return {"message": f"Student '{name}' deleted"}


# ---------------------------------------------------------------------------
# Payments - view across every school
# ---------------------------------------------------------------------------

@router.get("/payments", response_model=list[AdminPaymentResponse])
def list_all_payments(
    school_id: int | None = None,
    search: str | None = None,
    limit: int = Query(default=200, le=1000),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    with unscoped_query(db):
        query = db.query(Payment, School.name).outerjoin(School, Payment.school_id == School.id)
        if school_id is not None:
            query = query.filter(Payment.school_id == school_id)
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                Payment.transaction_reference.ilike(term) | Payment.payer_name.ilike(term)
            )
        rows = query.order_by(Payment.id.desc()).limit(limit).all()

        return [
            AdminPaymentResponse(
                id=p.id,
                school_id=p.school_id,
                school_name=school_name,
                student_id=p.student_id,
                amount=p.amount,
                method=p.method,
                transaction_reference=p.transaction_reference,
                payer_name=p.payer_name,
                date=p.date,
                status=p.status,
                notes=p.notes,
                created_at=p.created_at,
            )
            for p, school_name in rows
        ]


@router.delete("/payments/{payment_id}")
def delete_any_payment(payment_id: int, admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    with unscoped_query(db):
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        db.delete(payment)
        db.commit()
    return {"message": "Payment deleted"}