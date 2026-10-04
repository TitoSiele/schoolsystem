"""
School subscription billing - what schools pay for using SchoolPay.

Deliberately separate from the fee-billing code already in `billing_service.py`:
that bills PARENTS for school fees, this bills the SCHOOL for its monthly
platform subscription.

Billing rules encoded here:

* An invoice is raised per school per month and is idempotent - running the
  generator twice for the same month never double-charges.
* Money is Decimal/Numeric throughout. Never float.
* A school becomes `past_due` when an invoice passes its due date unpaid, and
  `expired` only after a grace period. It is never locked out immediately:
  cutting a school off mid-term over one invoice loses the customer instead of
  collecting from them.
* Free plans (trial) still get a zero-value invoice, so a trial upgrade has
  nothing special to handle.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from admin_router import require_platform_admin
from auth import get_current_school
from database import get_db
from models import School, SubscriptionInvoice, SubscriptionPayment, User
from schemas import (
    PlanInfo,
    RevenueSummary,
    SubscriptionInvoiceResponse,
    SubscriptionPaymentCreate,
    SubscriptionPaymentResponse,
    SubscriptionSummary,
)

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

# Whole shillings per month. `None` means "talk to us".
PLANS: dict[str, dict] = {
    "trial": {
        "name": "Trial",
        "monthly_price": None,
        "max_students": 50,
        "features": ["Up to 50 students", "Fee collection", "Reports", "SMS receipts"],
    },
    "starter": {
        "name": "Starter",
        "monthly_price": Decimal("3500.00"),
        "max_students": 250,
        "features": ["Up to 250 students", "Everything in Trial", "M-Pesa STK Push", "Till reconciliation", "Timetable"],
    },
    "pro": {
        "name": "Pro",
        "monthly_price": Decimal("9000.00"),
        "max_students": 1000,
        "features": ["Up to 1000 students", "Everything in Starter", "Parent portal", "Attendance", "Exam results"],
    },
    "enterprise": {
        "name": "Enterprise",
        "monthly_price": None,
        "max_students": None,
        "features": ["Unlimited students", "Custom pricing", "Dedicated support", "On-site training"],
    },
}

# How long a school keeps working after its invoice goes unpaid.
GRACE_DAYS = 14


def monthly_price(plan: str) -> Decimal:
    price = PLANS.get(plan, {}).get("monthly_price")
    return price if price is not None else Decimal("0.00")


# ---------------------------------------------------------------------------
# Plan catalogue
# ---------------------------------------------------------------------------

@router.get("/plans", response_model=list[PlanInfo])
def list_plans():
    return [
        PlanInfo(
            code=code,
            name=config["name"],
            monthly_price=float(config["monthly_price"]) if config["monthly_price"] is not None else None,
            max_students=config["max_students"],
            features=config["features"],
        )
        for code, config in PLANS.items()
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def invoice_number(school_code: str, period: date) -> str:
    return f"SUB-{period.year}-{period.month:02d}-{school_code}"


def month_bounds(period: date) -> tuple[date, date]:
    start = period.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end - timedelta(days=1)


def refresh_status(db: Session, school: School) -> None:
    """Move a school to past_due/expired based on its unpaid invoices.

    Called whenever subscription data is read or written, so a lapse is noticed
    without needing a background job.
    """
    if school.subscription_status == "cancelled":
        return

    today = date.today()
    open_invoices = (
        db.query(SubscriptionInvoice)
        .filter(
            SubscriptionInvoice.school_id == school.id,
            SubscriptionInvoice.status.in_(["unpaid", "overdue"]),
        )
        .all()
    )

    for invoice in open_invoices:
        if invoice.due_date < today:
            invoice.status = "overdue"

    overdue = [i for i in open_invoices if i.due_date < today]

    if not overdue:
        if school.subscription_status != "active":
            school.subscription_status = "active"
        db.commit()
        return

    # Only expire once the oldest overdue invoice is past the grace period.
    oldest = min(i.due_date for i in overdue)
    if today > oldest + timedelta(days=GRACE_DAYS):
        school.subscription_status = "expired"
    else:
        school.subscription_status = "past_due"


def to_response(db: Session, invoice: SubscriptionInvoice, school_name: str | None = None) -> SubscriptionInvoiceResponse:
    return SubscriptionInvoiceResponse(
        id=invoice.id,
        school_id=invoice.school_id,
        school_name=school_name,
        invoice_number=invoice.invoice_number,
        plan=invoice.plan,
        period_start=invoice.period_start,
        period_end=invoice.period_end,
        due_date=invoice.due_date,
        amount_due=invoice.amount_due,
        amount_paid=invoice.amount_paid,
        balance=invoice.balance,
        status=invoice.status,
        notes=invoice.notes,
        created_at=invoice.created_at,
        paid_at=invoice.paid_at,
        payments=[SubscriptionPaymentResponse.model_validate(p) for p in invoice.payments],
    )


# ---------------------------------------------------------------------------
# The school's own view
# ---------------------------------------------------------------------------

@router.get("/current", response_model=SubscriptionSummary)
def current_subscription(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    refresh_status(db, school)
    db.refresh(school)

    outstanding = 0.0
    next_due = None
    invoices = (
        db.query(SubscriptionInvoice)
        .filter(SubscriptionInvoice.school_id == school.id, SubscriptionInvoice.status.in_(["unpaid", "overdue"]))
        .order_by(SubscriptionInvoice.due_date)
        .all()
    )
    for invoice in invoices:
        outstanding += invoice.balance
        if next_due is None:
            next_due = invoice.due_date

    return SubscriptionSummary(
        school_id=school.id,
        school_name=school.name,
        plan=school.plan,
        subscription_status=school.subscription_status,
        student_count=school.student_count,
        max_students=school.max_students,
        at_capacity=school.at_capacity,
        monthly_price=float(monthly_price(school.plan)),
        outstanding_balance=round(outstanding, 2),
        next_due_date=next_due,
    )


@router.get("/invoices", response_model=list[SubscriptionInvoiceResponse])
def my_invoices(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    refresh_status(db, school)
    invoices = (
        db.query(SubscriptionInvoice)
        .filter(SubscriptionInvoice.school_id == school.id)
        .order_by(SubscriptionInvoice.due_date.desc())
        .all()
    )
    return [to_response(db, i, school.name) for i in invoices]


# ---------------------------------------------------------------------------
# Self-serve plan changes
# ---------------------------------------------------------------------------

SELF_SERVE_PLANS = {"trial", "starter", "pro"}


@router.post("/change-plan", response_model=SubscriptionSummary)
def change_plan(
    plan: str,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """Let a school move between self-serve plans.

    Enterprise is deliberately excluded: it has no fixed price, so it has to go
    through a human. The seat limit is moved with the plan in the same
    transaction, otherwise a customer could be "pro" but capped at 250.
    """
    plan = plan.strip().lower()
    if plan not in SELF_SERVE_PLANS:
        raise HTTPException(
            status_code=400,
            detail="Contact us to move to the Enterprise plan",
        )

    school.plan = plan
    school.max_students = PLANS[plan]["max_students"]
    school.subscription_status = "active"
    db.commit()
    db.refresh(school)

    return current_subscription(db=db, school=school)


# ---------------------------------------------------------------------------
# Platform admin: generate, collect, waive
# ---------------------------------------------------------------------------

@router.post("/generate", response_model=list[SubscriptionInvoiceResponse])
def generate_invoices(
    period: str | None = None,
    school_id: int | None = None,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Raise subscription invoices for a month.

    `period` is YYYY-MM and defaults to this month. Idempotent: a school that
    already has an invoice for that month is skipped, so this can be run as
    often as you like without double-charging anyone.
    """
    if period:
        try:
            year, month = (int(part) for part in period.split("-"))
            target = date(year, month, 1)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Period must look like 2026-10")
    else:
        target = date.today().replace(day=1)

    start, end = month_bounds(target)
    due_date = start + timedelta(days=14)

    query = db.query(School).filter(School.subscription_status != "cancelled")
    if school_id is not None:
        query = query.filter(School.id == school_id)
    schools = query.all()

    created: list[tuple[School, SubscriptionInvoice]] = []
    for school in schools:
        number = invoice_number(school.code, start)
        if db.query(SubscriptionInvoice).filter(SubscriptionInvoice.invoice_number == number).first():
            continue

        amount = monthly_price(school.plan)
        invoice = SubscriptionInvoice(
            school_id=school.id,
            invoice_number=number,
            plan=school.plan,
            period_start=start,
            period_end=end,
            due_date=due_date,
            amount_due=amount,
            amount_paid=Decimal("0.00"),
            # Free plans still get an invoice, at zero, so the trail is uniform.
            status="paid" if amount == 0 else "unpaid",
            paid_at=datetime.now(timezone.utc) if amount == 0 else None,
            notes=f"{PLANS.get(school.plan, {}).get('name', school.plan)} plan, {start:%B %Y}",
        )
        db.add(invoice)
        created.append((school, invoice))

    db.commit()

    result = []
    for school, invoice in created:
        db.refresh(invoice)
        result.append(to_response(db, invoice, school.name))
    return result


@router.get("/all", response_model=list[SubscriptionInvoiceResponse])
def all_invoices(
    school_id: int | None = None,
    status: str | None = None,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    query = db.query(SubscriptionInvoice, School.name).join(School, SubscriptionInvoice.school_id == School.id)
    if school_id is not None:
        query = query.filter(SubscriptionInvoice.school_id == school_id)
    if status:
        query = query.filter(SubscriptionInvoice.status == status)

    rows = query.order_by(SubscriptionInvoice.due_date.desc()).all()
    return [to_response(db, invoice, name) for invoice, name in rows]


@router.post("/invoices/{invoice_id}/pay", response_model=SubscriptionInvoiceResponse)
def pay_invoice(
    invoice_id: int,
    payload: SubscriptionPaymentCreate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Record a payment against a school's subscription.

    Supports partial payments: the invoice only becomes `paid` once the balance
    reaches zero, at which point the school returns to `active`.
    """
    invoice = db.query(SubscriptionInvoice).filter(SubscriptionInvoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Subscription invoice not found")
    if invoice.status == "waived":
        raise HTTPException(status_code=400, detail="This invoice was waived")
    if invoice.balance <= 0:
        raise HTTPException(status_code=400, detail="This invoice is already settled")

    amount = payload.amount
    if amount > invoice.balance:
        raise HTTPException(
            status_code=400,
            detail=f"That is more than the outstanding balance of KSh {invoice.balance:,.2f}",
        )

    db.add(
        SubscriptionPayment(
            school_id=invoice.school_id,
            invoice_id=invoice.id,
            amount=amount,
            method=payload.method,
            reference=payload.reference,
            paid_at=payload.paid_at or datetime.now(timezone.utc),
            recorded_by=admin.email,
        )
    )

    invoice.amount_paid = Decimal(str(invoice.amount_paid or 0)) + amount
    if invoice.balance <= 0:
        invoice.status = "paid"
        invoice.paid_at = datetime.now(timezone.utc)

    school = db.query(School).filter(School.id == invoice.school_id).first()
    if school:
        refresh_status(db, school)
        if school.subscription_status == "active":
            # Rolling renewal: next invoice falls due a month after this period.
            school.subscription_renews_at = datetime.now(timezone.utc) + timedelta(days=30)

    db.commit()
    db.refresh(invoice)
    return to_response(db, invoice)


@router.post("/invoices/{invoice_id}/waive", response_model=SubscriptionInvoiceResponse)
def waive_invoice(
    invoice_id: int,
    reason: str = "",
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
):
    """Write an invoice off - goodwill, or a genuine billing mistake."""
    invoice = db.query(SubscriptionInvoice).filter(SubscriptionInvoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Subscription invoice not found")

    invoice.status = "waived"
    invoice.notes = f"Waived by {admin.email}" + (f": {reason}" if reason else "")
    db.commit()

    school = db.query(School).filter(School.id == invoice.school_id).first()
    if school:
        refresh_status(db, school)

    db.refresh(invoice)
    return to_response(db, invoice)


@router.get("/revenue", response_model=RevenueSummary)
def revenue_summary(admin: User = Depends(require_platform_admin), db: Session = Depends(get_db)):
    """Money view for the operator: MRR, collected, outstanding, overdue."""
    schools = db.query(School).all()
    for school in schools:
        refresh_status(db, school)
    db.commit()

    invoices = db.query(SubscriptionInvoice).all()
    this_month = date.today().strftime("%Y-%m")

    mrr = sum(float(monthly_price(s.plan)) for s in schools if s.subscription_status in {"active", "past_due"})

    collected = 0.0
    outstanding = 0.0
    overdue_total = 0.0
    overdue_count = 0
    by_plan: dict[str, dict[str, float]] = {}

    for invoice in invoices:
        bucket = by_plan.setdefault(invoice.plan, {"billed": 0.0, "collected": 0.0, "outstanding": 0.0})
        bucket["billed"] += float(invoice.amount_due or 0)
        bucket["collected"] += float(invoice.amount_paid or 0)

        if invoice.status in {"unpaid", "overdue"}:
            outstanding += invoice.balance
            bucket["outstanding"] += invoice.balance
        if invoice.status == "overdue":
            overdue_total += invoice.balance
            overdue_count += 1
        if invoice.period_start.strftime("%Y-%m") == this_month:
            collected += float(invoice.amount_paid or 0)

    return RevenueSummary(
        mrr=round(mrr, 2),
        collected_this_month=round(collected, 2),
        outstanding_total=round(outstanding, 2),
        overdue_total=round(overdue_total, 2),
        overdue_count=overdue_count,
        active_subscriptions=sum(1 for s in schools if s.subscription_status == "active"),
        past_due_count=sum(1 for s in schools if s.subscription_status == "past_due"),
        by_plan=by_plan,
    )