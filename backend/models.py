from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from database import Base


class School(Base):
    """A paying customer. Every other table hangs off this so that one school can
    never see another school's data. This is what makes the product multi-tenant."""

    __tablename__ = "schools"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    code = Column(String, unique=True, nullable=False, index=True)  # e.g. "GVHS", used in the subdomain
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)

    # --- Subscription ---
    plan = Column(String, default="trial", nullable=False)  # trial, starter, pro, enterprise
    subscription_status = Column(String, default="active", nullable=False, index=True)  # active, past_due, cancelled, expired
    subscription_started_at = Column(DateTime, default=datetime.utcnow)
    subscription_renews_at = Column(DateTime, nullable=True)
    max_students = Column(Integer, default=500)  # seat limit for the current plan

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    users = relationship("User", back_populates="school")
    students = relationship("Student", back_populates="school")

    @property
    def student_count(self) -> int:
        return len(self.students)

    @property
    def at_capacity(self) -> bool:
        return self.max_students is not None and self.student_count >= self.max_students


class User(Base):
    """A login belonging to one school. `role` separates the school administrator
    from staff who can only record payments, and `is_platform_admin` marks the
    operator account used to create and manage schools."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)  # NULL for platform admins
    email = Column(String, unique=True, nullable=False, index=True)
    full_name = Column(String, nullable=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="staff", nullable=False)  # admin, staff, bursar
    is_platform_admin = Column(Boolean, default=False, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    school = relationship("School", back_populates="users")


# ==============================================================================
# SUBSCRIPTION BILLING (what schools pay YOU)
# ==============================================================================

class SubscriptionInvoice(Base):
    """A monthly charge to a school for using SchoolPay.

    Separate from student fee invoices: this is the school's own subscription,
    while `Invoice` is what a school bills its parents.
    """
    __tablename__ = "subscription_invoices"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False, index=True)
    invoice_number = Column(String, unique=True, nullable=False, index=True)  # SUB-2026-10-GVHS
    plan = Column(String, nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    amount_due = Column(Numeric(12, 2), nullable=False)
    amount_paid = Column(Numeric(12, 2), default=0.0)
    status = Column(String, default="unpaid", nullable=False, index=True)  # unpaid, paid, overdue, waived
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)

    school = relationship("School")
    payments = relationship("SubscriptionPayment", back_populates="invoice")

    @property
    def balance(self):
        return round(float(self.amount_due or 0) - float(self.amount_paid or 0), 2)


class SubscriptionPayment(Base):
    """One payment against a school's subscription invoice.

    Kept as its own table (rather than only a column on the invoice) so that
    partial payments and a payment history both survive, and so revenue can be
    audited line by line.
    """
    __tablename__ = "subscription_payments"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False, index=True)
    invoice_id = Column(Integer, ForeignKey("subscription_invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(String, default="M-Pesa")  # M-Pesa, Bank Transfer, Cheque
    reference = Column(String, nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)
    recorded_by = Column(String, nullable=True)

    invoice = relationship("SubscriptionInvoice", back_populates="payments")


# ==============================================================================
# TIMETABLING
# ==============================================================================

class Subject(Base):
    """A teachable subject, e.g. Mathematics, Kiswahili, Biology."""
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    code = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)

    school = relationship("School")


class SchoolClass(Base):
    """A class/stream, e.g. "Form 3 Blue".

    Students keep their class as free text on the student row, so this table is
    seeded from the distinct values already in use. That means timetabling works
    immediately on existing data without having to rewrite every student record.
    """
    __tablename__ = "school_classes"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    name = Column(String, nullable=False, index=True)
    level = Column(String, nullable=True)        # "Junior Secondary", "Senior Secondary"
    stream = Column(String, nullable=True)        # "Blue", "North"
    is_active = Column(Boolean, default=True)

    school = relationship("School")


class TimetableSlot(Base):
    """One period in the day, e.g. "08:00-08:45". Shared by every class."""
    __tablename__ = "timetable_slots"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    name = Column(String, nullable=False)          # "Period 1", "Break", "Lunch"
    start_time = Column(String, nullable=False)    # "08:00"
    end_time = Column(String, nullable=False)      # "08:45"
    sort_order = Column(Integer, default=0, index=True)
    is_teaching = Column(Boolean, default=True)    # False for break/lunch


class TimetableEntry(Base):
    """A subject taught to a class in one period on one day.

    A week is (day x period), so the day has to be part of the row. The unique
    constraint is what lets the API reject a double-booked class rather than
    silently overwriting the first lesson.
    """
    __tablename__ = "timetable_entries"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    class_id = Column(Integer, ForeignKey("school_classes.id"), nullable=False, index=True)
    slot_id = Column(Integer, ForeignKey("timetable_slots.id"), nullable=False, index=True)
    day = Column(String, nullable=False, default="Monday", index=True)  # Monday..Friday
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False, index=True)
    teacher_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    room = Column(String, nullable=True)
    note = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("class_id", "slot_id", "day", name="uq_timetable_class_slot_day"),
    )

    school_class = relationship("SchoolClass")
    slot = relationship("TimetableSlot")
    subject = relationship("Subject")


class Student(Base):

    __tablename__ = "students"

    id = Column(Integer, primary_key=True, index=True)

    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)

    admission_number = Column(
        String,
        unique=True,
        index=True,
        nullable=False
    )

    first_name = Column(
        String,
        nullable=False
    )

    last_name = Column(
        String,
        nullable=False
    )

    gender = Column(
        String,
        nullable=True
    )

    class_name = Column(
        String,
        nullable=False
    )

    parent_name = Column(
        String,
        nullable=True
    )

    parent_phone = Column(
        String,
        nullable=True
    )

    parent_email = Column(
        String,
        nullable=True
    )

    grade_level = Column(
        String,
        nullable=True,
        index=True
    )  # e.g., "Grade 1", "Grade 7 (Junior Primary)", "Form 3 (Senior Secondary)"

    student_tag = Column(
        String,
        default="Day",
        nullable=False,
        index=True
    )  # e.g., "Day", "Boarding", "Transport-Route-A", "Special-Scholarship"

    status = Column(
        String,
        default="Active",
        nullable=False
    )  # Active, Inactive, Graduated, Suspended

    fee_balance = Column(
        Float,
        default=0
    )

    photo_url = Column(String, nullable=True)  # relative path under /uploads/students

    school = relationship("School", back_populates="students")

    account = relationship("StudentAccount", back_populates="student", uselist=False, cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="student")
    ledger_entries = relationship("PaymentLedger", back_populates="student")
    incoming_payments = relationship("PaymentIncoming", back_populates="student")
    invoices = relationship("Invoice", back_populates="student", cascade="all, delete-orphan")
    fee_assignments = relationship("StudentFeeAssignment", back_populates="student", cascade="all, delete-orphan")
    notifications = relationship("NotificationLog", back_populates="student", cascade="all, delete-orphan")


class StudentAccount(Base):
    __tablename__ = "student_accounts"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), unique=True, nullable=False, index=True)
    total_fees = Column(Float, default=0.0)
    total_paid = Column(Float, default=0.0)
    outstanding_balance = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow)

    student = relationship("Student", back_populates="account")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(String, default="M-Pesa")
    transaction_reference = Column(String, unique=True, index=True, nullable=True)
    payer_name = Column(String, nullable=True)
    payer_phone = Column(String, nullable=True)
    date = Column(Date, default=date.today)
    status = Column(String, default="Unmatched")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    student = relationship("Student", back_populates="payments")


class PaymentIncoming(Base):
    __tablename__ = "payment_incoming"

    id = Column(Integer, primary_key=True, index=True)
    gateway = Column(String, nullable=False)
    gateway_txn_id = Column(String, unique=True, nullable=False, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    external_reference = Column(String, nullable=True, index=True)
    phone_number = Column(String, nullable=True, index=True)
    payer_name = Column(String, nullable=True)
    currency = Column(String, default="KES")
    amount = Column(Float, nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow)
    payload = Column(JSON, nullable=False)
    raw_payload = Column(Text, nullable=True)
    match_status = Column(String, default="pending")
    confidence_score = Column(Float, default=0.0)
    matched_student_id = Column(Integer, ForeignKey("students.id"), nullable=True, index=True)

    student = relationship("Student", back_populates="incoming_payments")
    ledger = relationship("PaymentLedger", back_populates="payment_incoming", uselist=False)
    exception = relationship("PaymentExceptionQueue", back_populates="payment_incoming", uselist=False)
    audits = relationship("AuditLog", back_populates="payment_incoming")


class PaymentLedger(Base):
    __tablename__ = "payment_ledger"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    payment_incoming_id = Column(Integer, ForeignKey("payment_incoming.id"), unique=True, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    posted_by = Column(String, nullable=True)
    posted_at = Column(DateTime, default=datetime.utcnow)
    balance_before = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    status = Column(String, default="posted")
    notes = Column(Text, nullable=True)

    student = relationship("Student", back_populates="ledger_entries")
    payment_incoming = relationship("PaymentIncoming", back_populates="ledger")


class PaymentExceptionQueue(Base):
    __tablename__ = "payment_exception_queue"

    id = Column(Integer, primary_key=True, index=True)
    payment_incoming_id = Column(Integer, ForeignKey("payment_incoming.id"), unique=True, nullable=False, index=True)
    reason = Column(String, nullable=False)
    severity = Column(String, default="medium")
    assigned_to = Column(String, nullable=True)
    status = Column(String, default="open")
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    payment_incoming = relationship("PaymentIncoming", back_populates="exception")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    entity_type = Column(String, nullable=False)
    entity_id = Column(Integer, nullable=False, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    action = Column(String, nullable=False)
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    performed_by = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    payment_incoming_id = Column(Integer, ForeignKey("payment_incoming.id"), nullable=True, index=True)

    payment_incoming = relationship("PaymentIncoming", back_populates="audits")


class DocumentStaging(Base):
    __tablename__ = "document_staging"

    id = Column(Integer, primary_key=True, index=True)
    document_name = Column(String, nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    source_type = Column(String, nullable=False)
    row_number = Column(Integer, nullable=False)
    raw_data = Column(JSON, nullable=False)
    normalized_data = Column(JSON, nullable=False)
    confidence_score = Column(Float, default=0.0)
    error_flags = Column(JSON, nullable=False, default=list)
    review_status = Column(String, nullable=False, default="pending", index=True)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# ==============================================================================
# MODULE 1: DYNAMIC FEE STRUCTURING & MULTI-TIER BILLING ENGINE
# ==============================================================================

class FeeCategory(Base):
    """
    Core Category for school fee lines.
    Examples: Tuition, Boarding, Transport, Activity, Lab Fees, ICT, Uniform, Exam Fees.
    """
    __tablename__ = "fee_categories"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, nullable=False, index=True)  # e.g., "TUIT", "BRD", "TRN", "LAB"
    name = Column(String, nullable=False)  # e.g., "Tuition Fee", "Boarding & Accommodation"
    description = Column(Text, nullable=True)
    is_optional = Column(Boolean, default=False)
    school_level = Column(String, default="All")  # "Junior Primary", "Senior Secondary", "All"
    created_at = Column(DateTime, default=datetime.utcnow)

    template_items = relationship("FeeTemplateItem", back_populates="category")
    invoice_items = relationship("InvoiceItem", back_populates="category")


class FeeTemplate(Base):
    """
    Multi-Tier Billing template combining category items for term-based billing.
    Configurable by grade level (Junior Primary vs Senior Secondary) and student tags (Boarding vs Day).
    """
    __tablename__ = "fee_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)  # e.g., "Senior Secondary - Grade 10 Boarding Term 1"
    academic_year = Column(String, nullable=False)  # e.g., "2026"
    term = Column(String, nullable=False)  # e.g., "Term 1", "Term 2", "Term 3"
    school_level = Column(String, nullable=False)  # "Junior Primary", "Senior Secondary", "All"
    grade_level = Column(String, nullable=True, index=True)  # e.g., "Grade 1-3", "Form 3", "All"
    student_tag = Column(String, nullable=True, index=True)  # e.g., "Boarding", "Day", "Transport"
    total_amount = Column(Float, default=0.0)
    currency = Column(String, default="KES")
    due_date = Column(Date, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("FeeTemplateItem", back_populates="template", cascade="all, delete-orphan")
    assignments = relationship("StudentFeeAssignment", back_populates="template")
    invoices = relationship("Invoice", back_populates="fee_template")


class FeeTemplateItem(Base):
    """
    Line items within a specific fee structure template.
    """
    __tablename__ = "fee_template_items"

    id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, ForeignKey("fee_templates.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("fee_categories.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    is_mandatory = Column(Boolean, default=True)
    description = Column(String, nullable=True)

    template = relationship("FeeTemplate", back_populates="items")
    category = relationship("FeeCategory", back_populates="template_items")


class StudentFeeAssignment(Base):
    """
    Association linking a student to an active FeeTemplate for an academic period.
    Supports individual student overrides and custom discounts/scholarships.
    """
    __tablename__ = "student_fee_assignments"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True)
    template_id = Column(Integer, ForeignKey("fee_templates.id"), nullable=False, index=True)
    academic_year = Column(String, nullable=False)
    term = Column(String, nullable=False)
    discount_amount = Column(Float, default=0.0)
    discount_reason = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    assigned_at = Column(DateTime, default=datetime.utcnow)

    student = relationship("Student", back_populates="fee_assignments")
    template = relationship("FeeTemplate", back_populates="assignments")


class Invoice(Base):
    """
    Termly opening balance/invoice generated for a student ledger.
    """
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    invoice_number = Column(String, unique=True, nullable=False, index=True)  # e.g., "INV-2026-T1-0001"
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True)
    fee_template_id = Column(Integer, ForeignKey("fee_templates.id"), nullable=True, index=True)
    academic_year = Column(String, nullable=False)
    term = Column(String, nullable=False)
    subtotal_amount = Column(Float, default=0.0)
    discount_amount = Column(Float, default=0.0)
    carried_forward_arrears = Column(Float, default=0.0)
    total_billed = Column(Float, default=0.0)  # subtotal - discount + carried_forward_arrears
    amount_paid = Column(Float, default=0.0)
    balance_due = Column(Float, default=0.0)
    issue_date = Column(Date, default=date.today)
    due_date = Column(Date, nullable=False)
    status = Column(String, default="ISSUED", index=True)  # ISSUED, PARTIALLY_PAID, PAID, OVERDUE, CANCELLED
    created_at = Column(DateTime, default=datetime.utcnow)

    student = relationship("Student", back_populates="invoices")
    fee_template = relationship("FeeTemplate", back_populates="invoices")
    items = relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")


class InvoiceItem(Base):
    """
    Detailed breakdown line item for each invoice.
    """
    __tablename__ = "invoice_items"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("fee_categories.id"), nullable=False, index=True)
    description = Column(String, nullable=False)
    amount = Column(Float, nullable=False)

    invoice = relationship("Invoice", back_populates="items")
    category = relationship("FeeCategory", back_populates="invoice_items")


# ==============================================================================
# MODULE 2: AUTOMATED ARREARS TRACKING & SCHEDULED REMINDERS
# ==============================================================================

class NotificationLog(Base):
    """
    Outbound communication log for automated SMS, Email receipts, and Arrears Reminders.
    """
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    recipient_type = Column(String, default="parent")  # parent, guardian, admin, student
    recipient_identifier = Column(String, nullable=False)  # Phone number or Email address
    channel = Column(String, nullable=False, index=True)  # "SMS", "EMAIL", "WHATSAPP"
    notification_type = Column(String, nullable=False, index=True)  # "PAYMENT_RECEIPT", "ARREARS_REMINDER", "OVERDUE_ALERT", "TERM_INVOICE"
    subject = Column(String, nullable=True)
    message_body = Column(Text, nullable=False)
    status = Column(String, default="QUEUED", index=True)  # "QUEUED", "SENT", "DELIVERED", "FAILED"
    gateway_reference = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    student = relationship("Student", back_populates="notifications")


class NotificationTemplate(Base):
    """
    Customizable system templates for SMS and Email notifications.
    """
    __tablename__ = "notification_templates"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, nullable=False, index=True)  # e.g., "SMS_RECEIPT", "EMAIL_REMINDER", "SMS_OVERDUE"
    channel = Column(String, nullable=False)  # "SMS" or "EMAIL"
    title = Column(String, nullable=False)
    subject = Column(String, nullable=True)
    template_body = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)


# ==============================================================================
# MODULE 3: EXPENSE TRACKING & FINANCIAL SUMMARY MODULE
# ==============================================================================

class ExpenseCategory(Base):
    """
    Classification for school operating expenses (Petty cash, Utilities, Lab reagents, Food, Repairs).
    """
    __tablename__ = "expense_categories"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, nullable=False, index=True)  # e.g., "FOOD", "UTIL", "REPAIR", "LAB_SUPPLIES"
    name = Column(String, nullable=False)  # e.g., "Food & Boarding Supplies"
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)

    expenses = relationship("Expense", back_populates="category")


class Expense(Base):
    """
    Operational expenses, supplier invoice disbursements, and petty cash vouchers.
    """
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    expense_number = Column(String, unique=True, nullable=False, index=True)  # e.g., "EXP-2026-0012"
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True, index=True)
    expense_type = Column(String, nullable=False, index=True)  # "OPERATIONAL", "SUPPLIER_INVOICE", "PETTY_CASH", "PAYROLL", "CAPEX"
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=False, index=True)

    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="KES")

    academic_year = Column(String, nullable=False, index=True)  # e.g., "2026"
    term = Column(String, nullable=False, index=True)  # e.g., "Term 1"

    payment_method = Column(String, default="Bank Transfer")  # "M-Pesa", "Bank Transfer", "Cheque", "Cash"
    payment_reference = Column(String, nullable=True)

    vendor_name = Column(String, nullable=True)
    vendor_contact = Column(String, nullable=True)
    vendor_invoice_number = Column(String, nullable=True)

    disbursed_to = Column(String, nullable=True)  # Staff or custodian for petty cash
    disbursed_by = Column(String, nullable=True)  # Bursar / Approver
    status = Column(String, default="APPROVED", index=True)  # "DRAFT", "PENDING_APPROVAL", "APPROVED", "PAID", "REJECTED"

    expense_date = Column(Date, default=date.today)
    created_at = Column(DateTime, default=datetime.utcnow)

    category = relationship("ExpenseCategory", back_populates="expenses")

