from datetime import date as date_type, datetime as datetime_type
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StudentCreate(BaseModel):
    admission_number: str
    first_name: str
    last_name: str
    gender: Optional[str] = None
    class_name: str
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    parent_email: Optional[str] = None
    grade_level: Optional[str] = None
    student_tag: Optional[str] = "Day"
    status: Optional[str] = "Active"
    fee_balance: float = 0
    photo_url: Optional[str] = None

    @field_validator("admission_number", "first_name", "last_name", "class_name", "parent_name", "parent_phone", "parent_email", "grade_level", "student_tag")
    @classmethod
    def clean_values(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return " ".join(value.strip().split())


class StudentResponse(StudentCreate):
    id: int
    model_config = ConfigDict(from_attributes=True)


class StudentUpdate(BaseModel):
    admission_number: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    gender: Optional[str] = None
    class_name: Optional[str] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    parent_email: Optional[str] = None
    grade_level: Optional[str] = None
    student_tag: Optional[str] = None
    status: Optional[str] = None
    fee_balance: Optional[float] = None
    photo_url: Optional[str] = None


# ==============================================================================
# AUTH & MULTI-TENANCY SCHEMAS
# ==============================================================================

class LoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None
    role: str
    is_platform_admin: bool
    school_id: Optional[int] = None
    school_name: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class SchoolCreate(BaseModel):
    name: str
    code: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    plan: Optional[str] = "trial"
    admin_email: str
    admin_password: str
    admin_name: Optional[str] = None


class SchoolUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    plan: Optional[str] = None
    subscription_status: Optional[str] = None


class SchoolResponse(BaseModel):
    id: int
    name: str
    code: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    plan: str
    subscription_status: str
    max_students: Optional[int] = None
    student_count: int = 0
    at_capacity: bool = False
    subscription_renews_at: Optional[datetime_type] = None
    model_config = ConfigDict(from_attributes=True)


# ==============================================================================
# TIMETABLE SCHEMAS
# ==============================================================================

class SubjectCreate(BaseModel):
    name: str
    code: Optional[str] = None


class SubjectResponse(BaseModel):
    id: int
    name: str
    code: Optional[str] = None
    is_active: bool = True
    model_config = ConfigDict(from_attributes=True)


class SchoolClassCreate(BaseModel):
    name: str
    level: Optional[str] = None
    stream: Optional[str] = None


class SchoolClassResponse(BaseModel):
    id: int
    name: str
    level: Optional[str] = None
    stream: Optional[str] = None
    is_active: bool = True
    student_count: int = 0
    model_config = ConfigDict(from_attributes=True)


class TimetableSlotCreate(BaseModel):
    name: str
    start_time: str
    end_time: str
    sort_order: int = 0
    is_teaching: bool = True


class TimetableSlotResponse(TimetableSlotCreate):
    id: int
    model_config = ConfigDict(from_attributes=True)


class TimetableEntryCreate(BaseModel):
    class_id: int
    slot_id: int
    day: str = Field(default="Monday", pattern="^(Monday|Tuesday|Wednesday|Thursday|Friday)$")
    subject_id: int
    teacher_id: Optional[int] = None
    room: Optional[str] = None
    note: Optional[str] = None


class TimetableEntryResponse(BaseModel):
    id: int
    class_id: int
    slot_id: int
    day: str = "Monday"
    subject_id: int
    teacher_id: Optional[int] = None
    room: Optional[str] = None
    note: Optional[str] = None
    # Denormalised for the grid so the frontend needs a single request
    class_name: Optional[str] = None
    subject_name: Optional[str] = None
    teacher_name: Optional[str] = None
    slot_name: Optional[str] = None


class TimetableGrid(BaseModel):
    """A whole class's week, ready to render without extra lookups."""
    class_id: int
    class_name: str
    days: list[str]
    slots: list[TimetableSlotResponse]
    entries: list[TimetableEntryResponse]


class ConflictReport(BaseModel):
    conflicts: list[dict[str, Any]]
    count: int


class AutoGenerateRequest(BaseModel):
    class_ids: list[int]
    daily_lessons: int = Field(default=8, ge=1, le=20)


# ==============================================================================
# SUBSCRIPTION BILLING SCHEMAS
# ==============================================================================

class PlanInfo(BaseModel):
    code: str
    name: str
    monthly_price: Optional[float] = None
    max_students: Optional[int] = None
    features: list[str] = Field(default_factory=list)


class SubscriptionPaymentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    method: str = "M-Pesa"
    reference: Optional[str] = None
    paid_at: Optional[datetime_type] = None


class SubscriptionPaymentResponse(BaseModel):
    id: int
    amount: Decimal
    method: str
    reference: Optional[str] = None
    paid_at: Optional[datetime_type] = None
    recorded_by: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class SubscriptionInvoiceResponse(BaseModel):
    id: int
    school_id: int
    school_name: Optional[str] = None
    invoice_number: str
    plan: str
    period_start: date_type
    period_end: date_type
    due_date: date_type
    amount_due: Decimal
    amount_paid: Decimal
    balance: float
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime_type] = None
    paid_at: Optional[datetime_type] = None
    payments: list[SubscriptionPaymentResponse] = Field(default_factory=list)


class SubscriptionSummary(BaseModel):
    """What the school sees about its own subscription."""
    school_id: int
    school_name: str
    plan: str
    subscription_status: str
    student_count: int
    max_students: Optional[int] = None
    at_capacity: bool
    monthly_price: Optional[float] = None
    outstanding_balance: float
    next_due_date: Optional[date_type] = None


class RevenueSummary(BaseModel):
    """Platform-wide money view for the operator."""
    mrr: float
    collected_this_month: float
    outstanding_total: float
    overdue_total: float
    overdue_count: int
    active_subscriptions: int
    past_due_count: int
    by_plan: dict[str, dict[str, float]]


# ==============================================================================
# M-PESA / RECONCILIATION SCHEMAS
# ==============================================================================

class MpesaStatusResponse(BaseModel):
    configured: bool
    sandbox: bool
    environment: str
    shortcode: Optional[str] = None
    message: str


class StkPushRequest(BaseModel):
    phone_number: Optional[str] = None
    amount: float = Field(..., gt=0)
    student_id: Optional[int] = None
    payer_name: Optional[str] = None
    account_reference: Optional[str] = None


class StkPushResponse(BaseModel):
    success: bool
    checkout_request_id: str
    message: str
    student_id: Optional[int] = None


class ReconciliationRow(BaseModel):
    line: int
    receipt: Optional[str] = None
    amount: Optional[str] = None
    payer_name: Optional[str] = None
    phone: Optional[str] = None
    date: Optional[str] = None
    student_id: Optional[int] = None
    student_name: Optional[str] = None
    match_status: str
    reason: str
    confidence: float = 0.0
    posted: bool = False


class ReconcileRequest(BaseModel):
    """Body for the reconciliation endpoint.

    Sent as a single JSON object rather than separate `rows` / `post_matched`
    form fields: FastAPI treats every non-File parameter in a route that also
    takes a File as form data, which would silently break JSON callers.
    """
    rows: list[dict[str, Any]] = Field(default_factory=list)
    post_matched: bool = False


class ReconciliationSummary(BaseModel):
    total: int
    matched: int
    unmatched: int
    posted: int
    rows: list[ReconciliationRow] = Field(default_factory=list)
    message: str


# ==============================================================================
# PLATFORM ADMIN SCHEMAS
# ==============================================================================

class PlatformStats(BaseModel):
    """Headline numbers shown at the top of the admin dashboard."""
    total_schools: int
    active_subscriptions: int
    trialing: int
    cancelled: int
    total_students: int
    total_payments: int
    total_payments_value: Decimal
    schools_at_capacity: int
    all_plans: dict[str, int]
    all_statuses: dict[str, int]


class AdminUserCreate(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    full_name: Optional[str] = None
    school_id: Optional[int] = None
    role: str = Field(default="staff", pattern="^(admin|staff|bursar)$")
    is_platform_admin: bool = False


class AdminUserUpdate(BaseModel):
    full_name: Optional[str] = None
    school_id: Optional[int] = None
    role: Optional[str] = Field(default=None, pattern="^(admin|staff|bursar)$")
    is_active: Optional[bool] = None
    is_platform_admin: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8)


class AdminUserResponse(BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None
    role: str
    is_platform_admin: bool
    is_active: bool
    school_id: Optional[int] = None
    school_name: Optional[str] = None
    created_at: Optional[datetime_type] = None
    model_config = ConfigDict(from_attributes=True)


class AdminStudentResponse(BaseModel):
    id: int
    school_id: Optional[int] = None
    school_name: Optional[str] = None
    admission_number: str
    first_name: str
    last_name: str
    class_name: str
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    fee_balance: float = 0
    status: Optional[str] = None
    photo_url: Optional[str] = None


class AdminPaymentResponse(BaseModel):
    id: int
    school_id: Optional[int] = None
    school_name: Optional[str] = None
    student_id: Optional[int] = None
    amount: Decimal
    method: Optional[str] = None
    transaction_reference: Optional[str] = None
    payer_name: Optional[str] = None
    date: Optional[date_type] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime_type] = None


class PaymentBase(BaseModel):
    student_id: Optional[int] = None
    amount: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    method: str = "M-Pesa"
    transaction_reference: Optional[str] = None
    payer_name: Optional[str] = None
    payer_phone: Optional[str] = None
    date: Optional[date_type] = None
    status: str = "Unmatched"
    notes: Optional[str] = None

    @field_validator("method", "transaction_reference", "payer_name", "payer_phone", "status", "notes")
    @classmethod
    def clean_values(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return " ".join(value.strip().split())


class PaymentCreate(PaymentBase):
    pass


class PaymentUpdate(BaseModel):
    student_id: Optional[int] = None
    amount: Optional[Decimal] = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    method: Optional[str] = None
    transaction_reference: Optional[str] = None
    payer_name: Optional[str] = None
    payer_phone: Optional[str] = None
    date: Optional[date_type] = None
    status: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("method", "transaction_reference", "payer_name", "payer_phone", "status", "notes")
    @classmethod
    def clean_values(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return " ".join(value.strip().split())


class PaymentResponse(BaseModel):
    id: int
    student_id: Optional[int] = None
    amount: Decimal
    method: Optional[str] = None
    transaction_reference: Optional[str] = None
    payer_name: Optional[str] = None
    payer_phone: Optional[str] = None
    date: Optional[date_type] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime_type] = None
    model_config = ConfigDict(from_attributes=True)


class PaymentWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway: str
    gateway_txn_id: str
    reference: Optional[str] = None
    phone_number: Optional[str] = None
    payer_name: Optional[str] = None
    amount: Decimal
    currency: str = "KES"
    received_at: str
    signature: Optional[str] = None

    @field_validator("gateway_txn_id", "reference", "phone_number", "payer_name")
    @classmethod
    def clean_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        return value.strip()


class PaymentIncomingResponse(BaseModel):
    id: int
    gateway: str
    gateway_txn_id: str
    external_reference: Optional[str] = None
    phone_number: Optional[str] = None
    payer_name: Optional[str] = None
    amount: float
    currency: str
    match_status: str
    confidence_score: float
    matched_student_id: Optional[int] = None
    payload: dict[str, Any]
    model_config = ConfigDict(from_attributes=True)


class ReconciliationResult(BaseModel):
    status: str
    score: float
    student_id: Optional[int] = None
    payment_id: Optional[int] = None
    reason: Optional[str] = None


class ExceptionQueueResponse(BaseModel):
    id: int
    payment_incoming_id: int
    reason: str
    severity: str
    status: str
    created_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class AuditLogResponse(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    action: str
    old_value: Optional[dict[str, Any]] = None
    new_value: Optional[dict[str, Any]] = None
    performed_by: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class StagingRowUpdate(BaseModel):
    normalized_data: dict[str, Any]
    review_status: str = Field(default="approved", pattern="^(pending|approved|rejected)$")
    reviewed_by: Optional[str] = None


class DocumentStagingResponse(BaseModel):
    id: int
    document_name: str
    source_type: str
    row_number: int
    raw_data: dict[str, Any]
    normalized_data: dict[str, Any]
    confidence_score: float
    error_flags: list[str]
    review_status: str
    reviewed_by: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class DocumentCommitRequest(BaseModel):
    staging_ids: list[int] = Field(min_length=1)
    reviewed_by: Optional[str] = None


class DocumentCommitResponse(BaseModel):
    committed: int
    skipped: int
    errors: list[dict[str, Any]]


# ==============================================================================
# MODULE 1: DYNAMIC FEE STRUCTURING & MULTI-TIER BILLING SCHEMAS
# ==============================================================================

class FeeCategoryBase(BaseModel):
    code: str = Field(..., description="Unique category code, e.g., TUIT, BRD, TRN, LAB")
    name: str = Field(..., description="Fee category name")
    description: Optional[str] = None
    is_optional: bool = False
    school_level: str = Field(default="All", description="Junior Primary, Senior Secondary, or All")


class FeeCategoryCreate(FeeCategoryBase):
    pass


class FeeCategoryResponse(FeeCategoryBase):
    id: int
    created_at: Optional[datetime_type] = None
    model_config = ConfigDict(from_attributes=True)


class FeeTemplateItemCreate(BaseModel):
    category_id: int
    amount: float = Field(..., gt=0)
    is_mandatory: bool = True
    description: Optional[str] = None


class FeeTemplateItemResponse(FeeTemplateItemCreate):
    id: int
    template_id: int
    category: Optional[FeeCategoryResponse] = None
    model_config = ConfigDict(from_attributes=True)


class FeeTemplateCreate(BaseModel):
    name: str
    academic_year: str = Field(default="2026")
    term: str = Field(default="Term 1")
    school_level: str = Field(default="All", description="Junior Primary, Senior Secondary, or All")
    grade_level: Optional[str] = Field(default="All", description="e.g. Grade 1-3, Form 3, All")
    student_tag: Optional[str] = Field(default="Day", description="Day, Boarding, Transport-Route-A, etc.")
    currency: str = Field(default="KES")
    due_date: date_type
    items: list[FeeTemplateItemCreate] = Field(default_factory=list)


class FeeTemplateResponse(BaseModel):
    id: int
    name: str
    academic_year: str
    term: str
    school_level: str
    grade_level: Optional[str] = None
    student_tag: Optional[str] = None
    total_amount: float
    currency: str
    due_date: date_type
    is_active: bool
    created_at: Optional[datetime_type] = None
    items: list[FeeTemplateItemResponse] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class StudentFeeAssignmentCreate(BaseModel):
    student_id: int
    template_id: int
    academic_year: str
    term: str
    discount_amount: float = Field(default=0.0, ge=0.0)
    discount_reason: Optional[str] = None


class StudentFeeAssignmentResponse(BaseModel):
    id: int
    student_id: int
    template_id: int
    academic_year: str
    term: str
    discount_amount: float
    discount_reason: Optional[str] = None
    is_active: bool
    assigned_at: Optional[datetime_type] = None
    template: Optional[FeeTemplateResponse] = None
    model_config = ConfigDict(from_attributes=True)


class GenerateTermBillingRequest(BaseModel):
    academic_year: str = Field(..., description="e.g. 2026")
    term: str = Field(..., description="e.g. Term 1, Term 2, Term 3")
    school_level: Optional[str] = Field(default=None, description="Filter: Junior Primary or Senior Secondary")
    grade_level: Optional[str] = Field(default=None, description="Filter: Specific class/grade or None for all")
    student_tag: Optional[str] = Field(default=None, description="Filter: Day, Boarding, or None for all")
    due_date: Optional[date_type] = None
    apply_arrears_carry_forward: bool = Field(default=True, description="Add previous unpaid balance to opening balance")


class InvoiceItemResponse(BaseModel):
    id: int
    category_id: int
    description: str
    amount: float
    model_config = ConfigDict(from_attributes=True)


class InvoiceResponse(BaseModel):
    id: int
    invoice_number: str
    student_id: int
    fee_template_id: Optional[int] = None
    academic_year: str
    term: str
    subtotal_amount: float
    discount_amount: float
    carried_forward_arrears: float
    total_billed: float
    amount_paid: float
    balance_due: float
    issue_date: date_type
    due_date: date_type
    status: str
    created_at: Optional[datetime_type] = None
    items: list[InvoiceItemResponse] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class TermBillingBatchResult(BaseModel):
    academic_year: str
    term: str
    students_processed: int
    invoices_created: int
    invoices_skipped: int
    total_billed_amount: float
    details: list[dict[str, Any]] = Field(default_factory=list)


# ==============================================================================
# MODULE 2: AUTOMATED ARREARS TRACKING & SCHEDULED REMINDERS SCHEMAS
# ==============================================================================

class ArrearsStudentSummary(BaseModel):
    student_id: int
    admission_number: str
    student_name: str
    class_name: str
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    parent_email: Optional[str] = None
    total_balance: float
    days_overdue: int
    overdue_invoices_count: int
    last_payment_date: Optional[date_type] = None
    urgency_level: str  # "MILD", "MODERATE", "CRITICAL"


class ArrearsReportResponse(BaseModel):
    total_overdue_students: int
    total_arrears_amount: float
    critical_count: int
    moderate_count: int
    mild_count: int
    students: list[ArrearsStudentSummary]


class TriggerReminderRequest(BaseModel):
    student_ids: Optional[list[int]] = Field(default=None, description="Specific student IDs or None for all overdue")
    channels: list[str] = Field(default=["SMS", "EMAIL"], description="Channels to dispatch: SMS, EMAIL")
    min_balance: float = Field(default=1.0, description="Minimum balance threshold to notify")
    min_days_overdue: int = Field(default=0, description="Minimum days overdue")
    custom_message: Optional[str] = None


class SendReceiptRequest(BaseModel):
    payment_id: int
    channels: list[str] = Field(default=["SMS", "EMAIL"])


class NotificationLogResponse(BaseModel):
    id: int
    student_id: Optional[int] = None
    recipient_type: str
    recipient_identifier: str
    channel: str
    notification_type: str
    subject: Optional[str] = None
    message_body: str
    status: str
    gateway_reference: Optional[str] = None
    error_message: Optional[str] = None
    sent_at: Optional[datetime_type] = None
    created_at: Optional[datetime_type] = None
    model_config = ConfigDict(from_attributes=True)


class NotificationBatchResult(BaseModel):
    dispatched_count: int
    sms_count: int
    email_count: int
    failed_count: int
    logs: list[NotificationLogResponse] = Field(default_factory=list)


# ==============================================================================
# MODULE 3: EXPENSE TRACKING & FINANCIAL SUMMARY SCHEMAS
# ==============================================================================

class ExpenseCategoryCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool = True


class ExpenseCategoryResponse(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    is_active: bool
    model_config = ConfigDict(from_attributes=True)


class ExpenseCreate(BaseModel):
    expense_type: str = Field(default="OPERATIONAL", description="OPERATIONAL, SUPPLIER_INVOICE, PETTY_CASH, PAYROLL, CAPEX")
    category_id: int
    title: str
    description: Optional[str] = None
    amount: float = Field(..., gt=0)
    currency: str = "KES"
    academic_year: str = Field(default="2026")
    term: str = Field(default="Term 1")
    payment_method: str = Field(default="Bank Transfer", description="M-Pesa, Bank Transfer, Cheque, Cash")
    payment_reference: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_contact: Optional[str] = None
    vendor_invoice_number: Optional[str] = None
    disbursed_to: Optional[str] = None
    disbursed_by: Optional[str] = None
    status: str = Field(default="APPROVED", description="DRAFT, PENDING_APPROVAL, APPROVED, PAID, REJECTED")
    expense_date: Optional[date_type] = None


class ExpenseResponse(BaseModel):
    id: int
    expense_number: str
    expense_type: str
    category_id: int
    title: str
    description: Optional[str] = None
    amount: float
    currency: str
    academic_year: str
    term: str
    payment_method: str
    payment_reference: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_contact: Optional[str] = None
    vendor_invoice_number: Optional[str] = None
    disbursed_to: Optional[str] = None
    disbursed_by: Optional[str] = None
    status: str
    expense_date: date_type
    created_at: Optional[datetime_type] = None
    category: Optional[ExpenseCategoryResponse] = None
    model_config = ConfigDict(from_attributes=True)


class CategoryFinancialBreakdown(BaseModel):
    category_name: str
    category_code: str
    amount: float
    percentage_of_total: float


class TermFinancialHealthSummary(BaseModel):
    academic_year: str
    term: str

    # Revenue / Inflows
    total_invoiced_amount: float
    total_collected_revenue: float
    total_uncollected_arrears: float
    collection_rate_percentage: float

    # Expenditures / Outflows
    total_operating_expenses: float
    total_supplier_invoices: float
    total_petty_cash: float
    total_all_expenses: float

    # Bottom-line Net Financial Health
    net_operating_cash_flow: float  # total_collected_revenue - total_all_expenses
    net_accrual_surplus_deficit: float  # total_invoiced_amount - total_all_expenses
    operating_margin_percentage: float
    financial_health_rating: str  # "EXCELLENT", "HEALTHY", "CAUTION", "DEFICIT"

    # Detailed Categorical Breakdowns
    revenue_by_category: list[CategoryFinancialBreakdown] = Field(default_factory=list)
    expenses_by_category: list[CategoryFinancialBreakdown] = Field(default_factory=list)


# ==============================================================================
# MODULE 4: FINANCIAL REPORTING & ANALYTICS SCHEMAS
# ==============================================================================

class ChannelCollectionBreakdown(BaseModel):
    channel: str  # "Mobile Money", "Bank", "Cash", "Cheque", etc.
    total_amount: float
    transaction_count: int
    percentage_of_total: float


class StreamCollectionBreakdown(BaseModel):
    class_name: str  # e.g., "Grade 1A", "Grade 3 North", "Form 3 Blue"
    expected_amount: float
    collected_amount: float
    outstanding_amount: float
    collection_rate_percentage: float
    student_count: int


class ClassCollectionBreakdown(BaseModel):
    grade_level: str  # e.g., "Grade 3", "Form 3", "Junior Primary", "Senior Secondary"
    expected_amount: float
    collected_amount: float
    outstanding_amount: float
    collection_rate_percentage: float
    student_count: int
    streams: list[StreamCollectionBreakdown] = Field(default_factory=list)


class TermlyCollectionSummaryReport(BaseModel):
    academic_year: str
    term: str
    total_expected_fees: float
    total_collected_fees: float
    total_outstanding_arrears: float
    overall_collection_rate: float
    total_students_enrolled: int
    classes: list[ClassCollectionBreakdown] = Field(default_factory=list)
    payment_channels: list[ChannelCollectionBreakdown] = Field(default_factory=list)


class AuditLogItemReport(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    action: str
    performed_by: Optional[str] = "System"
    ip_address: Optional[str] = "127.0.0.1"
    created_at: Optional[str] = None
    old_value: Optional[dict[str, Any]] = None
    new_value: Optional[dict[str, Any]] = None
    details_summary: str


class AuditReportSummary(BaseModel):
    total_records: int
    manual_overrides_count: int
    fee_waivers_count: int
    suspense_clearings_count: int
    generated_at: str
    generated_by: str
    entries: list[AuditLogItemReport] = Field(default_factory=list)


class TrialBalanceLineItem(BaseModel):
    account_code: str
    account_name: str
    account_category: str  # "Asset", "Liability", "Equity", "Revenue", "Expense"
    debit: float
    credit: float


class InstitutionalTrialBalanceReport(BaseModel):
    institution_name: str
    as_of_date: str
    academic_year: str
    term: str
    total_debits: float
    total_credits: float
    is_balanced: bool
    net_operating_result: float  # Total Revenue - Total Expenses
    surplus_or_deficit: str  # "Net Surplus" or "Net Deficit"
    line_items: list[TrialBalanceLineItem] = Field(default_factory=list)
    executive_commentary: str
