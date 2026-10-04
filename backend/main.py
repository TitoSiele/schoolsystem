import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine, get_db
from models import (
    AuditLog,
    DocumentStaging,
    Expense,
    ExpenseCategory,
    FeeCategory,
    FeeTemplate,
    FeeTemplateItem,
    Invoice,
    InvoiceItem,
    NotificationLog,
    NotificationTemplate,
    Payment,
    PaymentExceptionQueue,
    PaymentIncoming,
    PaymentLedger,
    School,
    Student,
    StudentAccount,
    StudentFeeAssignment,
    User,
)
from document_intake import extract_document
from billing_service import BillingService
from notification_service import ArrearsNotificationService, BackgroundScheduler
from expense_service import ExpenseService
from analytics_service import AnalyticsService
from admin_router import router as admin_router
from mpesa_router import router as mpesa_router
from subscription_router import router as subscription_router
from timetable_router import router as timetable_router
from auth import (
    PLAN_LIMITS,
    SESSION_COOKIE,
    create_session_token,
    get_current_school,
    get_current_user,
    hash_password,
    require_admin,
    scoped,
    verify_password,
)
from fastapi.responses import FileResponse, Response
from schemas import (
    ArrearsReportResponse,
    AuditLogResponse,
    AuditReportSummary,
    DocumentCommitRequest,
    DocumentCommitResponse,
    DocumentStagingResponse,
    ExceptionQueueResponse,
    ExpenseCategoryCreate,
    ExpenseCategoryResponse,
    ExpenseCreate,
    ExpenseResponse,
    FeeCategoryCreate,
    FeeCategoryResponse,
    FeeTemplateCreate,
    FeeTemplateResponse,
    GenerateTermBillingRequest,
    InstitutionalTrialBalanceReport,
    InvoiceResponse,
    LoginRequest,
    NotificationBatchResult,
    NotificationLogResponse,
    PaymentCreate,
    PaymentIncomingResponse,
    PaymentResponse,
    PaymentUpdate,
    PaymentWebhookPayload,
    ReconciliationResult,
    SchoolCreate,
    SchoolResponse,
    SchoolUpdate,
    SendReceiptRequest,
    StudentCreate,
    StudentFeeAssignmentCreate,
    StudentFeeAssignmentResponse,
    StudentResponse,
    StudentUpdate,
    StagingRowUpdate,
    TermBillingBatchResult,
    TermFinancialHealthSummary,
    TermlyCollectionSummaryReport,
    TriggerReminderRequest,
    UserResponse,
)


Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="School Fee Management System",
    description="API for managing students, fees and payments",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.strip().lower())


def fuzzy_score(a: str | None, b: str | None) -> float:
    left = normalize_text(a)
    right = normalize_text(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def append_audit(db: Session, entity_type: str, entity_id: int, action: str, old_value: Any, new_value: Any, performed_by: str | None, ip_address: str | None = None) -> None:
    db.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            old_value=old_value,
            new_value=new_value,
            performed_by=performed_by,
            ip_address=ip_address,
            user_agent="reconciliation-engine",
        )
    )


def resolve_student_match(db: Session, incoming: PaymentIncoming) -> tuple[Student | None, float, str]:
    best_student = None
    best_score = 0.0
    best_reason = "No sufficient match"

    students = db.query(Student).all()
    for student in students:
        ref_score = fuzzy_score(incoming.external_reference, student.admission_number)
        name_score = fuzzy_score(incoming.payer_name, f"{student.first_name} {student.last_name}")
        phone_score = 1.0 if incoming.phone_number and student.parent_phone and normalize_text(incoming.phone_number) == normalize_text(student.parent_phone) else 0.0
        combined = (0.55 * ref_score) + (0.30 * name_score) + (0.15 * phone_score)

        if combined > best_score:
            best_score = combined
            best_student = student
            best_reason = "Best fuzzy match across reference, name, and phone"

    if best_student and best_score >= 0.9:
        return best_student, round(best_score, 3), "matched"
    if best_student and best_score >= 0.7:
        return best_student, round(best_score, 3), "low_confidence"
    return None, round(best_score, 3), "unmatched"


def post_reconciled_payment(db: Session, payment: PaymentIncoming, student: Student) -> None:
    account = db.query(StudentAccount).filter(StudentAccount.student_id == student.id).first()
    if not account:
        account = StudentAccount(
            student_id=student.id,
            total_fees=0.0,
            total_paid=0.0,
            outstanding_balance=float(student.fee_balance or 0.0),
        )
        db.add(account)

    balance_before = float(account.outstanding_balance)
    amount = float(payment.amount)
    balance_after = round(balance_before - amount, 2)

    account.total_paid = round((account.total_paid or 0.0) + amount, 2)
    account.outstanding_balance = balance_after
    account.updated_at = datetime.now(timezone.utc)
    student.fee_balance = balance_after

    ledger = PaymentLedger(
        student_id=student.id,
        payment_incoming_id=payment.id,
        amount=amount,
        posted_by="system",
        posted_at=datetime.now(timezone.utc),
        balance_before=balance_before,
        balance_after=balance_after,
        status="posted",
        notes="Auto-reconciled by fuzzy matching",
    )
    db.add(ledger)

    append_audit(
        db,
        "payment_incoming",
        payment.id,
        "reconciliation_posted",
        {"balance": balance_before},
        {"balance": balance_after, "student_id": student.id},
        "system",
        "internal",
    )


@app.get("/api")
def home():
    return {
        "message": "School Fee Management System API",
        "status": "running",
    }


# ==============================================================================
# AUTH & SCHOOL MANAGEMENT
# ==============================================================================

def _user_response(db: Session, user: User) -> UserResponse:
    school_name = None
    if user.school_id:
        school = db.query(School).filter(School.id == user.school_id).first()
        school_name = school.name if school else None
    return UserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_platform_admin=user.is_platform_admin,
        school_id=user.school_id,
        school_name=school_name,
    )


@app.post("/api/auth/register-school", response_model=SchoolResponse, status_code=201)
def register_school(payload: SchoolCreate, db: Session = Depends(get_db)):
    """Public self-serve signup: creates a school plus its first admin user.
    This is the entry point a new paying customer goes through."""
    code = payload.code.strip().upper()
    if db.query(School).filter(School.code == code).first():
        raise HTTPException(status_code=400, detail=f"School code '{code}' is already taken")

    if db.query(User).filter(User.email == payload.admin_email.strip().lower()).first():
        raise HTTPException(status_code=400, detail="That email is already registered")

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
    db.flush()  # assign school.id

    admin = User(
        school_id=school.id,
        email=payload.admin_email.strip().lower(),
        full_name=payload.admin_name,
        password_hash=hash_password(payload.admin_password),
        role="admin",
    )
    db.add(admin)
    db.commit()
    db.refresh(school)
    return school


@app.post("/api/auth/login", response_model=UserResponse)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.strip().lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been disabled")

    token = create_session_token(user.id)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
    )
    return _user_response(db, user)


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"message": "Signed out"}


@app.get("/api/auth/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _user_response(db, user)


@app.get("/api/school", response_model=SchoolResponse)
def my_school(school: School = Depends(get_current_school)):
    return school


# ==============================================================================
# STUDENTS (scoped to the signed-in school)
# ==============================================================================

@app.post("/students", response_model=StudentResponse, status_code=201)
def create_student(
    student: StudentCreate,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    if scoped(db, Student, school).filter(Student.admission_number == student.admission_number).first():
        raise HTTPException(status_code=400, detail="Admission number already exists in your school")

    if school.at_capacity:
        raise HTTPException(
            status_code=402,
            detail=f"Your {school.plan} plan allows {school.max_students} students. Upgrade to add more.",
        )

    new_student = Student(
        school_id=school.id,
        admission_number=student.admission_number,
        first_name=student.first_name,
        last_name=student.last_name,
        gender=student.gender,
        class_name=student.class_name,
        parent_name=student.parent_name,
        parent_phone=student.parent_phone,
        parent_email=student.parent_email,
        grade_level=student.grade_level,
        student_tag=student.student_tag or "Day",
        status=student.status or "Active",
        fee_balance=student.fee_balance,
        photo_url=student.photo_url,
    )

    db.add(new_student)
    db.commit()
    db.refresh(new_student)

    return new_student


@app.get("/students", response_model=list[StudentResponse])
def get_students(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    return scoped(db, Student, school).all()


@app.get("/students/{student_id}", response_model=StudentResponse)
def get_student(student_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    student = scoped(db, Student, school).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


@app.put("/students/{student_id}", response_model=StudentResponse)
def update_student(
    student_id: int,
    updates: StudentUpdate,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    student = scoped(db, Student, school).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    update_data = updates.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(student, field, value)

    db.commit()
    db.refresh(student)

    return student


@app.delete("/students/{student_id}")
def delete_student(student_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    student = scoped(db, Student, school).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    db.delete(student)
    db.commit()

    return {"message": "Student deleted successfully"}


@app.post("/students/{student_id}/photo")
async def upload_student_photo(
    student_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """Attach a photo to a student. Files are stored under uploads/students/ and
    referenced by a relative path, so the app still works when hosted on a
    different machine."""
    student = scoped(db, Student, school).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded photo is empty")

    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail="Photo must be a JPG, PNG, WEBP or GIF image")

    photo_dir = Path(__file__).parent / "uploads" / "students"
    photo_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{school.code}_{student.id}_{secrets.token_hex(6)}{suffix}"
    (photo_dir / filename).write_bytes(content)

    student.photo_url = f"/uploads/students/{filename}"
    db.commit()
    db.refresh(student)

    return {"photo_url": student.photo_url}


# ==============================================================================
# PAYMENTS (scoped to the signed-in school)
# ==============================================================================

@app.post("/payments", response_model=PaymentResponse, status_code=201)
def create_payment(
    payment: PaymentCreate,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    if payment.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than zero")

    if payment.student_id is not None:
        student = scoped(db, Student, school).filter(Student.id == payment.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

    ref = (payment.transaction_reference or "").strip() or None
    if ref:
        dup = db.query(Payment).filter(Payment.transaction_reference == ref).first()
        if dup:
            raise HTTPException(status_code=400, detail=f"A payment with transaction reference '{ref}' already exists")

    status_value = "Matched" if payment.student_id is not None else payment.status

    new_payment = Payment(
        school_id=school.id,
        student_id=payment.student_id,
        amount=payment.amount,
        method=payment.method,
        transaction_reference=ref,
        payer_name=payment.payer_name,
        payer_phone=payment.payer_phone,
        date=payment.date or date.today(),
        status=status_value,
        notes=payment.notes,
    )
    db.add(new_payment)
    db.commit()
    db.refresh(new_payment)
    return new_payment


@app.get("/payments", response_model=list[PaymentResponse])
def list_payments(
    student_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    query = scoped(db, Payment, school)

    if student_id is not None:
        query = query.filter(Payment.student_id == student_id)
    if start_date is not None:
        query = query.filter(Payment.date >= start_date)
    if end_date is not None:
        query = query.filter(Payment.date <= end_date)

    return query.order_by(Payment.date.desc(), Payment.id.desc()).all()


@app.get("/payments/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    payment = scoped(db, Payment, school).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@app.put("/payments/{payment_id}", response_model=PaymentResponse)
def update_payment(
    payment_id: int,
    updates: PaymentUpdate,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    payment = scoped(db, Payment, school).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    update_data = updates.model_dump(exclude_unset=True)

    if "transaction_reference" in update_data:
        ref = (update_data["transaction_reference"] or "").strip() or None
        if ref:
            dup = (
                db.query(Payment)
                .filter(Payment.transaction_reference == ref, Payment.id != payment_id)
                .first()
            )
            if dup:
                raise HTTPException(
                    status_code=400,
                    detail=f"A different payment already uses transaction reference '{ref}'",
                )
        update_data["transaction_reference"] = ref

    for field, value in update_data.items():
        setattr(payment, field, value)

    db.commit()
    db.refresh(payment)
    return payment


@app.delete("/payments/{payment_id}")
def delete_payment(payment_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    payment = scoped(db, Payment, school).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    db.delete(payment)
    db.commit()
    return {"message": "Payment deleted successfully"}


@app.get("/students/{student_id}/payments", response_model=list[PaymentResponse])
def student_payments(student_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    student = scoped(db, Student, school).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    return (
        scoped(db, Payment, school)
        .filter(Payment.student_id == student_id)
        .order_by(Payment.date.desc(), Payment.id.desc())
        .all()
    )


@app.post("/api/v1/payments/webhook", response_model=ReconciliationResult)
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    secret = os.getenv("PAYMENT_WEBHOOK_SECRET", "dev-secret")
    signature = request.headers.get("X-Signature") or request.headers.get("x-signature")

    if signature:
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    data = PaymentWebhookPayload(**payload)
    existing = db.query(PaymentIncoming).filter(PaymentIncoming.gateway_txn_id == data.gateway_txn_id).first()
    if existing:
        return ReconciliationResult(status=existing.match_status, score=float(existing.confidence_score or 0.0), student_id=existing.matched_student_id, payment_id=existing.id, reason="Duplicate transaction received")

    incoming = PaymentIncoming(
        gateway=data.gateway,
        gateway_txn_id=data.gateway_txn_id,
        external_reference=data.reference,
        phone_number=data.phone_number,
        payer_name=data.payer_name,
        currency=data.currency,
        amount=float(data.amount),
        received_at=datetime.fromisoformat(data.received_at.replace("Z", "+00:00")),
        payload=payload,
        raw_payload=raw_body.decode("utf-8"),
        match_status="pending",
        confidence_score=0.0,
    )
    db.add(incoming)
    db.commit()
    db.refresh(incoming)

    student, score, decision = resolve_student_match(db, incoming)
    incoming.confidence_score = score
    incoming.match_status = decision if decision in {"matched", "unmatched", "low_confidence"} else "pending"

    if decision == "matched" and student is not None:
        incoming.matched_student_id = student.id
        incoming.match_status = "matched"
        post_reconciled_payment(db, incoming, student)
        db.commit()
        # Trigger automated receipt
        try:
            ArrearsNotificationService.dispatch_payment_receipt(db, incoming.id, channels=["SMS", "EMAIL"])
        except Exception:
            pass
        return ReconciliationResult(status="matched", score=score, student_id=student.id, payment_id=incoming.id, reason="Matched using fuzzy reconciliation")

    if decision == "low_confidence":
        incoming.match_status = "low_confidence"
        exception = PaymentExceptionQueue(
            payment_incoming_id=incoming.id,
            reason="Payment matched with low confidence; manual review required",
            severity="medium",
            status="open",
        )
        db.add(exception)
        db.commit()
        return ReconciliationResult(status="low_confidence", score=score, payment_id=incoming.id, reason="Manual review required")

    incoming.match_status = "unmatched"
    db.add(
        PaymentExceptionQueue(
            payment_incoming_id=incoming.id,
            reason="No confident student match found for payment",
            severity="high",
            status="open",
        )
    )
    db.commit()
    return ReconciliationResult(status="unmatched", score=score, payment_id=incoming.id, reason="No confident match found")


@app.get("/api/v1/payments", response_model=list[PaymentIncomingResponse])
def list_incoming_payments(db: Session = Depends(get_db)):
    return db.query(PaymentIncoming).order_by(PaymentIncoming.id.desc()).all()


@app.get("/api/v1/payments/exceptions", response_model=list[ExceptionQueueResponse])
def list_payment_exceptions(db: Session = Depends(get_db)):
    return db.query(PaymentExceptionQueue).order_by(PaymentExceptionQueue.id.desc()).all()


@app.get("/api/v1/audit-log", response_model=list[AuditLogResponse])
def list_audit_log(db: Session = Depends(get_db)):
    return db.query(AuditLog).order_by(AuditLog.id.desc()).all()


@app.post("/api/v1/documents/universal-upload", response_model=list[DocumentStagingResponse])
async def universal_upload(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded document is empty")
    try:
        records, source_type = extract_document(content, file.filename or "upload", file.content_type)
    except (ValueError, ImportError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not records:
        raise HTTPException(status_code=422, detail="No student rows could be extracted from the document")

    staged = []
    for row_number, normalized in enumerate(records, start=1):
        raw_data = {key: value for key, value in normalized.items() if key not in {"source", "error_flags", "confidence_score"}}
        row = DocumentStaging(
            document_name=file.filename or "upload",
            school_id=school.id,
            source_type=source_type,
            row_number=row_number,
            raw_data=raw_data,
            normalized_data=raw_data.copy(),
            confidence_score=normalized["confidence_score"],
            error_flags=normalized["error_flags"],
        )
        db.add(row)
        staged.append(row)
    db.commit()
    for row in staged:
        db.refresh(row)
    return staged


@app.get("/api/v1/documents/staging", response_model=list[DocumentStagingResponse])
def list_staging_rows(status: str = "pending", db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    return (
        db.query(DocumentStaging)
        .filter(DocumentStaging.review_status == status)
        .order_by(DocumentStaging.id)
        .all()
    )


@app.patch("/api/v1/documents/staging/{staging_id}", response_model=DocumentStagingResponse)
def review_staging_row(staging_id: int, update: StagingRowUpdate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    row = db.query(DocumentStaging).filter(DocumentStaging.id == staging_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Staging row not found")
    row.normalized_data = update.normalized_data
    row.review_status = update.review_status
    row.reviewed_by = update.reviewed_by
    row.reviewed_at = datetime.now(timezone.utc)
    row.error_flags = [field for field in ("admission_number", "first_name", "last_name", "class_name") if not update.normalized_data.get(field)]
    db.commit()
    db.refresh(row)
    return row


@app.post("/api/v1/documents/staging/commit", response_model=DocumentCommitResponse)
def commit_staging_rows(request: DocumentCommitRequest, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    rows = db.query(DocumentStaging).filter(DocumentStaging.id.in_(request.staging_ids)).all()
    committed = 0
    errors = []
    for row in rows:
        data = row.normalized_data or {}
        missing = [field for field in ("admission_number", "first_name", "last_name", "class_name") if not data.get(field)]
        # A row that a human explicitly rejected must never be imported, but a
        # row that is merely "pending" is fine: the UI only lists complete rows
        # for confirmation, so the user's click to "Import" IS the approval.
        if row.review_status == "rejected" or missing:
            errors.append({"staging_id": row.id, "error": "Row is missing required fields" if missing else "Row was rejected", "fields": missing})
            continue
        try:
            student = scoped(db, Student, school).filter(Student.admission_number == data["admission_number"]).first()
            if student:
                for field in ("first_name", "last_name", "gender", "class_name", "parent_name", "parent_phone", "fee_balance"):
                    if data.get(field) is not None:
                        setattr(student, field, data[field])
            else:
                db.add(Student(school_id=school.id, **{field: data.get(field) for field in ("admission_number", "first_name", "last_name", "gender", "class_name", "parent_name", "parent_phone", "fee_balance")}))
            row.review_status = "committed"
            row.reviewed_by = request.reviewed_by or row.reviewed_by
            row.reviewed_at = datetime.now(timezone.utc)
            committed += 1
        except Exception as exc:
            db.rollback()
            errors.append({"staging_id": row.id, "error": str(exc)})
    db.commit()
    return DocumentCommitResponse(committed=committed, skipped=len(errors), errors=errors)

# ==============================================================================
# MODULE 1: DYNAMIC FEE STRUCTURING & MULTI-TIER BILLING ROUTES
# ==============================================================================

@app.post("/api/v1/billing/categories", response_model=FeeCategoryResponse, status_code=201)
def create_fee_category(category: FeeCategoryCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Creates a new fee category (e.g. Tuition, Boarding, Transport, Activity, Lab)."""
    return BillingService.create_fee_category(db, category)


@app.get("/api/v1/billing/categories", response_model=list[FeeCategoryResponse])
def list_fee_categories(school_level: str | None = None, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Lists all configured fee categories with optional school level filter."""
    return BillingService.list_fee_categories(db, school_level)


@app.post("/api/v1/billing/templates", response_model=FeeTemplateResponse, status_code=201)
def create_fee_template(template: FeeTemplateCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Creates a multi-tier fee structure template for Junior Primary / Senior Secondary / Boarding / Day."""
    return BillingService.create_fee_template(db, template)


@app.get("/api/v1/billing/templates", response_model=list[FeeTemplateResponse])
def list_fee_templates(
    academic_year: str | None = None,
    term: str | None = None,
    school_level: str | None = None,
    grade_level: str | None = None,
    student_tag: str | None = None,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """Lists fee structure templates by academic year, term, grade levels, and student tags."""
    return BillingService.list_fee_templates(
        db,
        academic_year=academic_year,
        term=term,
        school_level=school_level,
        grade_level=grade_level,
        student_tag=student_tag,
    )


@app.post("/api/v1/billing/assignments", response_model=StudentFeeAssignmentResponse, status_code=201)
def assign_student_fee_structure(assignment: StudentFeeAssignmentCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Assigns a custom fee template or scholarship discount override to a specific student."""
    return BillingService.assign_student_fee_template(db, assignment)


@app.post("/api/v1/billing/generate-invoices", response_model=TermBillingBatchResult)
def generate_term_invoices(request: GenerateTermBillingRequest, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """
    Automated Billing Engine: Generates opening balance invoices across student ledgers
    based on fee templates, applying discounts and carrying forward unpaid arrears.
    """
    return BillingService.generate_term_invoices(db, request)


@app.get("/api/v1/billing/students/{student_id}/invoices", response_model=list[InvoiceResponse])
def get_student_invoices(student_id: int, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Retrieves all term invoices and breakdowns for a given student."""
    return BillingService.get_student_invoices(db, student_id)


# ==============================================================================
# MODULE 2: AUTOMATED ARREARS TRACKING & SCHEDULED REMINDERS ROUTES
# ==============================================================================

@app.get("/api/v1/arrears/report", response_model=ArrearsReportResponse)
def get_arrears_report(
    min_balance: float = 1.0,
    min_days_overdue: int = 0,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """
    Arrears Tracking: Scans accounts against due dates and categorizes urgency (MILD, MODERATE, CRITICAL).
    """
    return ArrearsNotificationService.analyze_arrears(
        db, min_balance=min_balance, min_days_overdue=min_days_overdue
    )


@app.post("/api/v1/arrears/dispatch-reminders", response_model=NotificationBatchResult)
def dispatch_arrears_reminders(request: TriggerReminderRequest, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """
    Notification Dispatcher: Sends automated SMS and Email payment reminders to parents of overdue students.
    """
    return ArrearsNotificationService.dispatch_arrears_reminders(db, request)


@app.post("/api/v1/notifications/send-receipt", response_model=list[NotificationLogResponse])
def send_payment_receipt(request: SendReceiptRequest, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """
    Automated Receipting: Generates and dispatches digital SMS/Email payment receipt to parent.
    """
    return ArrearsNotificationService.dispatch_payment_receipt(
        db, payment_id=request.payment_id, channels=request.channels
    )


@app.get("/api/v1/notifications/logs", response_model=list[NotificationLogResponse])
def list_notification_logs(channel: str | None = None, status: str | None = None, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Lists audit logs of all sent SMS and Email notifications."""
    query = db.query(NotificationLog)
    if channel:
        query = query.filter(NotificationLog.channel == channel)
    if status:
        query = query.filter(NotificationLog.status == status)
    return query.order_by(NotificationLog.id.desc()).all()


# ==============================================================================
# MODULE 3: EXPENSE TRACKING & INSTITUTIONAL FINANCIAL SUMMARY ROUTES
# ==============================================================================

@app.post("/api/v1/expenses/categories", response_model=ExpenseCategoryResponse, status_code=201)
def create_expense_category(cat_in: ExpenseCategoryCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Creates an expense category (e.g. Food Supplies, Utilities, Laboratory Reagents, Repairs)."""
    return ExpenseService.create_expense_category(db, cat_in)


@app.get("/api/v1/expenses/categories", response_model=list[ExpenseCategoryResponse])
def list_expense_categories(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Lists all operational expense categories."""
    return ExpenseService.list_expense_categories(db)


@app.post("/api/v1/expenses", response_model=ExpenseResponse, status_code=201)
def record_expense(expense: ExpenseCreate, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """
    Records operational expense, supplier invoice disbursement, or petty cash disbursement voucher.
    """
    return ExpenseService.record_expense(db, expense)


@app.get("/api/v1/expenses", response_model=list[ExpenseResponse])
def list_expenses(
    academic_year: str | None = None,
    term: str | None = None,
    expense_type: str | None = None,
    category_id: int | None = None,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """Lists recorded expenses with optional filtering."""
    return ExpenseService.list_expenses(
        db,
        academic_year=academic_year,
        term=term,
        expense_type=expense_type,
        category_id=category_id,
    )


@app.get("/api/v1/finance/summary-ledger", response_model=TermFinancialHealthSummary)
def get_term_financial_summary(academic_year: str = "2026", term: str = "Term 1", db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """
    Executive Financial Summary Ledger:
    Calculates institutional financial health by aggregating total fee collections against total recorded expenses.
    Yields Net Operating Cash Flow, Accrual Surplus/Deficit, and Health Rating.
    """
    return ExpenseService.get_term_financial_summary(db, academic_year=academic_year, term=term)


# ==============================================================================
# MODULE 4: FINANCIAL REPORTING & ANALYTICS ROUTES
# ==============================================================================

@app.get("/api/v1/reports/collection-summary", response_model=TermlyCollectionSummaryReport)
def get_termly_collection_summary(
    academic_year: str = "2026",
    term: str = "Term 1",
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """
    Termly Fee Collection Summary:
    Aggregates total expected vs. total collected fees broken down by grade level (class),
    stream, and payment channels (Mobile Money / Bank / Cash).
    """
    return AnalyticsService.get_termly_collection_summary(
        db, academic_year=academic_year, term=term
    )


@app.get("/api/v1/reports/audit-trail", response_model=AuditReportSummary)
def get_audit_trail_summary(
    action_filter: str | None = None,
    entity_filter: str | None = None,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """
    Automated Audit Report Summary:
    Compiles a tamper-proof audit trail of manual overrides, fee waivers/discounts,
    and payment suspense account clearings.
    """
    return AnalyticsService.get_audit_report_data(
        db, action_filter=action_filter, entity_filter=entity_filter
    )


@app.get("/api/v1/reports/audit-trail/download")
def download_audit_trail_csv(
    action_filter: str | None = None,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """
    Downloadable Tamper-Proof Audit Report (CSV):
    Exports all manual overrides, waivers, and suspense modifications to an official audit CSV file.
    """
    csv_content = AnalyticsService.generate_audit_report_csv(db, action_filter=action_filter)
    filename = f"audit_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/v1/reports/trial-balance", response_model=InstitutionalTrialBalanceReport)
def get_institutional_trial_balance(
    academic_year: str = "2026",
    term: str = "Term 1",
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """
    Institutional Trial Balance / Statement:
    Ledger aggregation providing double-entry debits/credits of cash, accounts receivable,
    operating/supplier expenses, and fee revenues with net operating surplus/deficit for directors.
    """
    return AnalyticsService.get_institutional_trial_balance(
        db, academic_year=academic_year, term=term
    )


# ==============================================================================
# APP ENTRYPOINT
# ==============================================================================
# The frontend StaticFiles mount is registered here, LAST, on purpose.
#
# `app.mount("/", ...)` is a catch-all: once it is registered it matches every
# path, so any @app.route added AFTER this point becomes unreachable and returns
# 404 with no error logged. Mounting at the end guarantees the API routes above
# win, and keeps the single-server "one origin, no CORS" setup working locally.
# Student photos are served as static files. This must be registered before the
# catch-all frontend mount below, otherwise "/" would swallow /uploads/...
uploads_dir = Path(__file__).parent / "uploads"
if uploads_dir.is_dir():
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")


# Platform administration (cross-tenant). Registered as a router rather than
# inline routes so it stays reviewable as one unit.
app.include_router(admin_router)

# Timetabling
app.include_router(timetable_router)

# M-Pesa Daraja (STK Push) and till reconciliation
app.include_router(mpesa_router)

# School subscription billing (what schools pay you, not parents)
app.include_router(subscription_router)


if os.path.isdir(frontend_dir := os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)