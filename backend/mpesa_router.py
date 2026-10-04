"""
Till reconciliation.

The daily task a bursar actually does: download the paybill statement for the
day, paste or upload it, and find out which transactions are already recorded and
which are missing.

Matching runs in tiers, most confident first:

  1. Exact transaction reference (CheckoutCode / M-Pesa receipt number)
  2. Admission number embedded in the account reference or receipt number
  3. Amount + parent phone
  4. Amount + payer name

Anything still unmatched goes to a review queue rather than being silently
guessed at, because posting fees to the wrong parent is far worse than asking.
"""

import re
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from auth import get_current_school, scoped
from database import get_db
from models import Payment, PaymentIncoming, School, Student
from mpesa import get_config
from schemas import (
    MpesaStatusResponse,
    ReconcileRequest,
    ReconciliationRow,
    ReconciliationSummary,
    StkPushRequest,
    StkPushResponse,
)

router = APIRouter(prefix="/api/mpesa", tags=["mpesa"])


# ---------------------------------------------------------------------------
# STK Push
# ---------------------------------------------------------------------------

@router.get("/status", response_model=MpesaStatusResponse)
def mpesa_status(db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Tell the UI whether Daraja is usable, and for which environment."""
    config = get_config()
    return MpesaStatusResponse(
        configured=config.is_configured,
        sandbox=config.sandbox,
        shortcode=config.shortcode or None,
        environment="sandbox" if config.sandbox else "production",
        message=(
            "Daraja credentials are configured."
            if config.is_configured
            else "Add your Safaricom Daraja credentials to the environment to enable M-Pesa."
        ),
    )


@router.post("/stkpush", response_model=StkPushResponse)
def request_stk_push(payload: StkPushRequest, db: Session = Depends(get_db), school: School = Depends(get_current_school)):
    """Ask Safaricom to push a payment prompt to a parent's phone."""
    from mpesa import stk_push  # local import keeps module load order simple

    student = None
    if payload.student_id is not None:
        student = scoped(db, Student, school).filter(Student.id == payload.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

    phone = payload.phone_number or (student.parent_phone if student else None)
    if not phone:
        raise HTTPException(status_code=400, detail="A parent phone number is required")

    amount = int(payload.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be more than zero")

    # The account reference is what the payer sees and what we match on, so put
    # the admission number in it. That is what makes tier-2 matching possible.
    account_reference = student.admission_number if student else payload.account_reference or "FEES"

    try:
        result = stk_push(
            phone_number=phone,
            amount=amount,
            account_reference=account_reference[:20],
            description="School fees",
        )
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Record the request so the callback can be tied back to a student even if
    # the payer typed the wrong phone number.
    incoming = PaymentIncoming(
        gateway="mpesa",
        gateway_txn_id=result.get("CheckoutRequestID", f"stk-{int(datetime_now().timestamp())}"),
        external_reference=account_reference,
        phone_number=phone,
        payer_name=payload.payer_name,
        currency="KES",
        amount=Decimal(amount),
        school_id=school.id,
        payload=result,
        raw_payload=str(result),
        match_status="pending",
        confidence_score=0.0,
        matched_student_id=student.id if student else None,
    )
    db.add(incoming)
    db.commit()
    db.refresh(incoming)

    return StkPushResponse(
        success=True,
        checkout_request_id=incoming.gateway_txn_id,
        message="Payment prompt sent. The parent should confirm on their phone.",
        student_id=student.id if student else None,
    )


def datetime_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def normalise(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").strip().lower())


def find_match(db: Session, school: School, row: dict) -> tuple[Student | None, str, float]:
    """Try each matching tier in turn. Returns (student, reason, confidence)."""
    reference = (row.get("receipt") or row.get("transaction_reference") or "").strip()
    ref_key = normalise(reference)
    payer_name = row.get("payer_name") or ""
    phone = row.get("phone") or ""

    students = scoped(db, Student, school).all()

    # Tier 1: exact transaction reference against an existing payment.
    if reference:
        for student in students:
            for payment in student.payments or []:
                if normalise(payment.transaction_reference) == ref_key and ref_key:
                    return student, "Matched on transaction reference", 1.0

    # Tier 2: admission number inside the reference or account reference.
    for student in students:
        admission = normalise(student.admission_number)
        if admission and admission in ref_key:
            return student, f"Matched on admission number {student.admission_number}", 0.95

    # Tier 3: amount plus parent phone.
    try:
        amount = float(parse_amount(row.get("amount")))
    except Exception:
        amount = 0.0
    phone_key = normalise(phone)
    if amount and phone_key:
        for student in students:
            if normalise(student.parent_phone) == phone_key and abs(float(student.fee_balance or 0) - amount) < 0.01:
                return student, "Matched on amount and parent phone", 0.8

    # Tier 4: amount plus payer name similarity.
    if amount and payer_name:
        from difflib import SequenceMatcher

        payer_key = normalise(payer_name)
        best = None
        best_score = 0.0
        for student in students:
            student_key = normalise(f"{student.parent_name} {student.first_name} {student.last_name}")
            if not student_key or not payer_key:
                continue
            score = SequenceMatcher(None, payer_key, student_key).ratio()
            if score > best_score:
                best, best_score = student, score
        if best and best_score >= 0.75 and abs(float(best.fee_balance or 0) - amount) < 0.01:
            return best, f"Matched on amount and payer name ({int(best_score * 100)}% similar)", round(best_score, 2)

    return None, "No confident match", 0.0


@router.post("/reconcile", response_model=ReconciliationSummary)
async def reconcile_statement(
    request: ReconcileRequest,
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """Match statement lines supplied directly as JSON.

    `post_matched` must be explicitly true for money to move. A reconciliation
    pass should always be reviewed first, because posting fees to the wrong
    parent is far worse than asking.

    This is a separate route from /reconcile-file on purpose: FastAPI treats
    every non-File parameter in a route that also takes a File as form data, so
    mixing a JSON body and a file upload in one signature silently breaks JSON
    callers.
    """
    return await _reconcile(request.rows, request.post_matched, db, school, source="pasted rows")


@router.post("/reconcile-file", response_model=ReconciliationSummary)
async def reconcile_statement_file(
    file: UploadFile = File(...),
    post_matched: bool = Form(default=False),
    db: Session = Depends(get_db),
    school: School = Depends(get_current_school),
):
    """Match statement lines from an uploaded CSV export."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file was uploaded")

    import csv
    import io

    content = (await file.read()).decode("utf-8-sig", errors="replace")
    parsed = list(csv.DictReader(io.StringIO(content)))

    return await _reconcile(parsed, post_matched, db, school, source=file.filename)


async def _reconcile(parsed, post_matched, db, school, source: str):
    if not parsed:
        raise HTTPException(status_code=422, detail=f"No transaction rows were found in '{source}'")

    results: list[ReconciliationRow] = []
    posted = 0
    unmatched = 0
    skipped_duplicate = 0

    for index, raw in enumerate(parsed, start=1):
        row = {
            "receipt": str(
                raw.get("receipt") or raw.get("transaction_reference") or raw.get("Receipt Number") or ""
            ).strip(),
            # Coerced to a string because statements quote amounts inconsistently
            # ("5,000", "5000.00", 5000). Decimal() below does the real parsing.
            "amount": str(raw.get("amount") or raw.get("Amount") or "").strip(),
            "payer_name": str(raw.get("payer_name") or raw.get("Name") or raw.get("Payer") or "").strip(),
            "phone": str(raw.get("phone") or raw.get("Phone") or "").strip(),
            "date": str(raw.get("date") or raw.get("Date") or "").strip(),
        }

        student, reason, confidence = find_match(db, school, row)

        if student is None:
            unmatched += 1
            results.append(
                ReconciliationRow(
                    line=index, **row, student_id=None, student_name=None,
                    match_status="unmatched", reason=reason, confidence=confidence, posted=False,
                )
            )
            continue

        did_post = False
        note = reason

        if post_matched:
            try:
                amount = parse_amount(row["amount"])
            except Exception:
                amount = Decimal("0")

            if amount <= 0:
                note = f"{reason} - skipped, amount is not a number"
            elif row["receipt"]:
                existing = db.query(Payment).filter(Payment.transaction_reference == row["receipt"]).first()
                if existing:
                    skipped_duplicate += 1
                    note = f"{reason} - already recorded, not posted again"
                else:
                    db.add(
                        Payment(
                            school_id=school.id,
                            student_id=student.id,
                            amount=amount,
                            method="M-Pesa",
                            transaction_reference=row["receipt"],
                            payer_name=row["payer_name"] or student.parent_name,
                            payer_phone=row["phone"] or student.parent_phone,
                            date=parse_date(row["date"]),
                            status="Matched",
                            notes="Posted from M-Pesa statement reconciliation",
                        )
                    )
                    did_post = True
                    note = f"{reason} - posted"
            else:
                note = f"{reason} - skipped, no receipt number to use as a reference"

            posted += int(did_post)

        results.append(
            ReconciliationRow(
                line=index, **row,
                student_id=student.id,
                student_name=f"{student.first_name} {student.last_name}",
                match_status="matched",
                reason=note,
                confidence=confidence,
                posted=did_post,
            )
        )

    db.commit()

    parts = [f"{len(parsed) - unmatched} of {len(parsed)} transaction(s) matched."]
    if post_matched:
        parts.append(f"{posted} payment(s) posted.")
        if skipped_duplicate:
            parts.append(f"{skipped_duplicate} already existed and were not posted again.")
    else:
        parts.append("Nothing was posted - review the matches, then post them.")

    return ReconciliationSummary(
        total=len(parsed),
        matched=len(parsed) - unmatched,
        unmatched=unmatched,
        posted=posted,
        rows=results,
        message=" ".join(parts),
    )


def parse_amount(value) -> Decimal:
    """Parse an amount from a statement cell, tolerating commas and currency marks."""
    import re as _re

    cleaned = _re.sub(r"[^0-9.\-]", "", str(value or ""))
    if not cleaned or cleaned in {"-", ".", "-."}:
        raise ValueError("no amount")
    return Decimal(cleaned).quantize(Decimal("0.01"))


def parse_date(value: str | None):
    """Parse a statement date, falling back to today rather than failing the row."""
    from datetime import datetime

    if not value:
        return date.today()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return date.today()