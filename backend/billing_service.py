"""
Dynamic Fee Structuring & Multi-Tier Billing Engine Service.
Handles:
- Dynamic Fee Categories (Tuition, Boarding, Transport, Activity, Lab Fees, etc.)
- Multi-tier Fee Templates by Grade Level (Junior Primary vs Senior Secondary) & Student Tags (Day vs Boarding)
- Student Fee Assignments & custom discounts/scholarships
- Termly Opening Balances & Automated Invoice Generation across Student Ledgers with Arrears Carryover
"""

from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session
from fastapi import HTTPException

from models import (
    FeeCategory,
    FeeTemplate,
    FeeTemplateItem,
    StudentFeeAssignment,
    Invoice,
    InvoiceItem,
    Student,
    StudentAccount,
    PaymentLedger,
    AuditLog,
)
from schemas import (
    FeeCategoryCreate,
    FeeTemplateCreate,
    StudentFeeAssignmentCreate,
    GenerateTermBillingRequest,
    TermBillingBatchResult,
)


class BillingService:

    @staticmethod
    def create_fee_category(db: Session, category_in: FeeCategoryCreate) -> FeeCategory:
        existing = db.query(FeeCategory).filter(FeeCategory.code == category_in.code.upper()).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Fee category code '{category_in.code}' already exists")

        category = FeeCategory(
            code=category_in.code.upper(),
            name=category_in.name,
            description=category_in.description,
            is_optional=category_in.is_optional,
            school_level=category_in.school_level,
        )
        db.add(category)
        db.commit()
        db.refresh(category)
        return category

    @staticmethod
    def list_fee_categories(db: Session, school_level: Optional[str] = None) -> list[FeeCategory]:
        query = db.query(FeeCategory)
        if school_level and school_level != "All":
            query = query.filter((FeeCategory.school_level == school_level) | (FeeCategory.school_level == "All"))
        return query.order_by(FeeCategory.code).all()

    @staticmethod
    def create_fee_template(db: Session, template_in: FeeTemplateCreate) -> FeeTemplate:
        total_amount = sum(item.amount for item in template_in.items)

        template = FeeTemplate(
            name=template_in.name,
            academic_year=template_in.academic_year,
            term=template_in.term,
            school_level=template_in.school_level,
            grade_level=template_in.grade_level or "All",
            student_tag=template_in.student_tag or "Day",
            total_amount=round(total_amount, 2),
            currency=template_in.currency,
            due_date=template_in.due_date,
            is_active=True,
        )
        db.add(template)
        db.flush()  # populate template.id

        for item_in in template_in.items:
            category = db.query(FeeCategory).filter(FeeCategory.id == item_in.category_id).first()
            if not category:
                raise HTTPException(status_code=404, detail=f"Category id {item_in.category_id} not found")

            item = FeeTemplateItem(
                template_id=template.id,
                category_id=category.id,
                amount=round(item_in.amount, 2),
                is_mandatory=item_in.is_mandatory,
                description=item_in.description or category.name,
            )
            db.add(item)

        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def list_fee_templates(
        db: Session,
        academic_year: Optional[str] = None,
        term: Optional[str] = None,
        school_level: Optional[str] = None,
        grade_level: Optional[str] = None,
        student_tag: Optional[str] = None,
    ) -> list[FeeTemplate]:
        query = db.query(FeeTemplate).filter(FeeTemplate.is_active == True)
        if academic_year:
            query = query.filter(FeeTemplate.academic_year == academic_year)
        if term:
            query = query.filter(FeeTemplate.term == term)
        if school_level and school_level != "All":
            query = query.filter((FeeTemplate.school_level == school_level) | (FeeTemplate.school_level == "All"))
        if grade_level and grade_level != "All":
            query = query.filter((FeeTemplate.grade_level == grade_level) | (FeeTemplate.grade_level == "All"))
        if student_tag and student_tag != "All":
            query = query.filter((FeeTemplate.student_tag == student_tag) | (FeeTemplate.student_tag == "All"))
        return query.order_by(FeeTemplate.id.desc()).all()

    @staticmethod
    def assign_student_fee_template(db: Session, assignment_in: StudentFeeAssignmentCreate) -> StudentFeeAssignment:
        student = db.query(Student).filter(Student.id == assignment_in.student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=f"Student id {assignment_in.student_id} not found")

        template = db.query(FeeTemplate).filter(FeeTemplate.id == assignment_in.template_id).first()
        if not template:
            raise HTTPException(status_code=404, detail=f"Fee template id {assignment_in.template_id} not found")

        # Deactivate previous active assignment for same year/term
        db.query(StudentFeeAssignment).filter(
            StudentFeeAssignment.student_id == student.id,
            StudentFeeAssignment.academic_year == assignment_in.academic_year,
            StudentFeeAssignment.term == assignment_in.term,
        ).update({"is_active": False})

        assignment = StudentFeeAssignment(
            student_id=student.id,
            template_id=template.id,
            academic_year=assignment_in.academic_year,
            term=assignment_in.term,
            discount_amount=round(assignment_in.discount_amount, 2),
            discount_reason=assignment_in.discount_reason,
            is_active=True,
        )
        db.add(assignment)
        db.commit()
        db.refresh(assignment)
        return assignment

    @classmethod
    def match_best_template_for_student(
        cls, db: Session, student: Student, academic_year: str, term: str
    ) -> Optional[FeeTemplate]:
        """
        Determines the most accurate fee structure template for a student:
        1. Check explicit active StudentFeeAssignment.
        2. Match by exact grade_level + student_tag (e.g. "Form 3" + "Boarding").
        3. Match by class_name + student_tag.
        4. Fallback to school_level + student_tag or default template for that academic year & term.
        """
        # 1. Explicit Assignment
        assignment = (
            db.query(StudentFeeAssignment)
            .filter(
                StudentFeeAssignment.student_id == student.id,
                StudentFeeAssignment.academic_year == academic_year,
                StudentFeeAssignment.term == term,
                StudentFeeAssignment.is_active == True,
            )
            .first()
        )
        if assignment and assignment.template:
            return assignment.template

        tag = getattr(student, "student_tag", "Day") or "Day"
        grade = getattr(student, "grade_level", None) or getattr(student, "class_name", "")

        # 2. Match by exact Grade Level and Student Tag
        matched = (
            db.query(FeeTemplate)
            .filter(
                FeeTemplate.academic_year == academic_year,
                FeeTemplate.term == term,
                FeeTemplate.is_active == True,
                FeeTemplate.grade_level == grade,
                FeeTemplate.student_tag == tag,
            )
            .first()
        )
        if matched:
            return matched

        # 3. Match by partial grade/class or Tag
        matched = (
            db.query(FeeTemplate)
            .filter(
                FeeTemplate.academic_year == academic_year,
                FeeTemplate.term == term,
                FeeTemplate.is_active == True,
                (FeeTemplate.grade_level == "All") | (FeeTemplate.grade_level == grade),
                FeeTemplate.student_tag == tag,
            )
            .first()
        )
        if matched:
            return matched

        # 4. Fallback to any active template for that term
        return (
            db.query(FeeTemplate)
            .filter(
                FeeTemplate.academic_year == academic_year,
                FeeTemplate.term == term,
                FeeTemplate.is_active == True,
            )
            .first()
        )

    @classmethod
    def generate_term_invoices(cls, db: Session, request: GenerateTermBillingRequest) -> TermBillingBatchResult:
        """
        Enterprise billing engine:
        1. Iterates students eligible for billing.
        2. Resolves template & discounts.
        3. Computes subtotal, discount, carried forward arrears.
        4. Generates unique Invoice records + Invoice breakdown items.
        5. Updates StudentAccount opening ledger & total fees.
        """
        query = db.query(Student).filter(Student.status != "Inactive")
        if request.grade_level and request.grade_level != "All":
            query = query.filter((Student.class_name == request.grade_level) | (Student.grade_level == request.grade_level))
        if request.student_tag and request.student_tag != "All":
            query = query.filter(Student.student_tag == request.student_tag)

        students = query.all()
        processed_count = 0
        invoices_created = 0
        invoices_skipped = 0
        total_billed = 0.0
        details = []

        # Find default invoice count for naming sequence
        invoice_counter = db.query(Invoice).filter(
            Invoice.academic_year == request.academic_year,
            Invoice.term == request.term
        ).count() + 1

        for student in students:
            processed_count += 1

            # Prevent duplicate invoices for the same term
            existing_invoice = db.query(Invoice).filter(
                Invoice.student_id == student.id,
                Invoice.academic_year == request.academic_year,
                Invoice.term == request.term,
            ).first()

            if existing_invoice:
                invoices_skipped += 1
                details.append({
                    "student_id": student.id,
                    "admission_number": student.admission_number,
                    "status": "skipped",
                    "reason": f"Invoice {existing_invoice.invoice_number} already exists for {request.academic_year} {request.term}",
                })
                continue

            template = cls.match_best_template_for_student(db, student, request.academic_year, request.term)
            if not template or not template.items:
                invoices_skipped += 1
                details.append({
                    "student_id": student.id,
                    "admission_number": student.admission_number,
                    "status": "skipped",
                    "reason": "No fee template configured for this student profile",
                })
                continue

            # Check individual student discount
            assignment = db.query(StudentFeeAssignment).filter(
                StudentFeeAssignment.student_id == student.id,
                StudentFeeAssignment.academic_year == request.academic_year,
                StudentFeeAssignment.term == request.term,
                StudentFeeAssignment.is_active == True,
            ).first()
            discount = float(assignment.discount_amount) if assignment else 0.0

            subtotal = float(template.total_amount)
            net_term_fees = max(0.0, subtotal - discount)

            # Previous arrears balance
            carried_forward = float(student.fee_balance or 0.0) if request.apply_arrears_carry_forward else 0.0
            new_total_balance = round(carried_forward + net_term_fees, 2)
            due_date = request.due_date or template.due_date or date.today()

            term_clean = request.term.replace(" ", "")
            inv_num = f"INV-{request.academic_year}-{term_clean}-{invoice_counter:04d}"
            invoice_counter += 1

            invoice = Invoice(
                invoice_number=inv_num,
                student_id=student.id,
                fee_template_id=template.id,
                academic_year=request.academic_year,
                term=request.term,
                subtotal_amount=subtotal,
                discount_amount=discount,
                carried_forward_arrears=carried_forward,
                total_billed=new_total_balance,
                amount_paid=0.0,
                balance_due=new_total_balance,
                issue_date=date.today(),
                due_date=due_date,
                status="ISSUED" if new_total_balance > 0 else "PAID",
            )
            db.add(invoice)
            db.flush()

            # Create line items
            for item in template.items:
                inv_item = InvoiceItem(
                    invoice_id=invoice.id,
                    category_id=item.category_id,
                    description=item.description or item.category.name,
                    amount=float(item.amount),
                )
                db.add(inv_item)

            # Update Student balance & StudentAccount
            student.fee_balance = new_total_balance

            account = db.query(StudentAccount).filter(StudentAccount.student_id == student.id).first()
            if not account:
                account = StudentAccount(
                    student_id=student.id,
                    total_fees=net_term_fees,
                    total_paid=0.0,
                    outstanding_balance=new_total_balance,
                    updated_at=datetime.now(timezone.utc),
                )
                db.add(account)
            else:
                account.total_fees = round((account.total_fees or 0.0) + net_term_fees, 2)
                account.outstanding_balance = new_total_balance
                account.updated_at = datetime.now(timezone.utc)

            # Audit Log
            db.add(
                AuditLog(
                    entity_type="invoice",
                    entity_id=invoice.id,
                    action="invoice_generated",
                    old_value={"carried_forward": carried_forward},
                    new_value={"invoice_number": inv_num, "total_billed": new_total_balance},
                    performed_by="billing_engine",
                    ip_address="internal",
                    user_agent="billing-service",
                )
            )

            invoices_created += 1
            total_billed += new_total_balance
            details.append({
                "student_id": student.id,
                "admission_number": student.admission_number,
                "invoice_number": inv_num,
                "template_name": template.name,
                "term_fee": net_term_fees,
                "carried_forward_arrears": carried_forward,
                "total_billed": new_total_balance,
                "status": "created",
            })

        db.commit()

        return TermBillingBatchResult(
            academic_year=request.academic_year,
            term=request.term,
            students_processed=processed_count,
            invoices_created=invoices_created,
            invoices_skipped=invoices_skipped,
            total_billed_amount=round(total_billed, 2),
            details=details,
        )

    @staticmethod
    def get_student_invoices(db: Session, student_id: int) -> list[Invoice]:
        return db.query(Invoice).filter(Invoice.student_id == student_id).order_by(Invoice.id.desc()).all()
