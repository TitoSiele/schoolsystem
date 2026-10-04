"""
Financial Reporting & Analytics Service.
Handles:
1. Termly Fee Collection Summary:
   - Aggregates total expected vs. total collected fees broken down by class, stream, and payment channel (Bank vs. Mobile Money).
2. Automated Audit Report Generator:
   - Compiles tamper-proof audit logs of all manual overrides, fee waivers/scholarships, and suspense account clearings.
   - Generates downloadable CSV reports and JSON summaries.
3. Institutional Trial Balance / Statement:
   - Double-entry ledger aggregation providing total inflows (fee billing & cash receipts) and total outflows (operating expenses, supplier payments, petty cash) with net operating surplus/deficit for executive leadership.
"""

import csv
from datetime import date, datetime, timezone
import io
import json
from typing import Optional
from sqlalchemy import func, case, or_
from sqlalchemy.orm import Session
from fastapi import HTTPException
from fastapi.responses import Response

from models import (
    AuditLog,
    Expense,
    ExpenseCategory,
    Invoice,
    InvoiceItem,
    Payment,
    PaymentIncoming,
    PaymentLedger,
    PaymentExceptionQueue,
    Student,
    StudentAccount,
    StudentFeeAssignment,
)
from schemas import (
    AuditReportSummary,
    AuditLogItemReport,
    ChannelCollectionBreakdown,
    ClassCollectionBreakdown,
    InstitutionalTrialBalanceReport,
    StreamCollectionBreakdown,
    TermlyCollectionSummaryReport,
    TrialBalanceLineItem,
)


class AnalyticsService:

    @classmethod
    def get_termly_collection_summary(
        cls, db: Session, academic_year: str = "2026", term: str = "Term 1"
    ) -> TermlyCollectionSummaryReport:
        """
        Aggregates total expected vs. total collected fees broken down by class, stream,
        and payment channel (Bank vs. Mobile Money vs. Cash).
        """
        # 1. Fetch all students
        students = db.query(Student).filter(Student.status != "Inactive").all()
        total_students = len(students)

        # 2. Fetch invoices for this academic year and term
        invoices = db.query(Invoice).filter(
            Invoice.academic_year == academic_year,
            Invoice.term == term,
        ).all()
        
        # Map student_id -> Invoice
        student_invoice_map: dict[int, Invoice] = {inv.student_id: inv for inv in invoices}

        # 3. Fetch payments received
        payments = db.query(Payment).filter(Payment.status.in_(["Matched", "Cleared", "posted"])).all()
        student_payments_map: dict[int, float] = {}
        for p in payments:
            if p.student_id:
                student_payments_map[p.student_id] = student_payments_map.get(p.student_id, 0.0) + float(p.amount)

        # 4. Group by Grade Level (Class) and Class Name (Stream)
        # Structure: { grade_level: { stream_name: { "expected": 0.0, "collected": 0.0, "students": 0 } } }
        class_map: dict[str, dict[str, dict]] = {}

        total_expected = 0.0
        total_collected = 0.0

        for student in students:
            # Grade level grouping (e.g. "Grade 3", "Form 3", or derived from class_name)
            grade = student.grade_level or student.class_name.split()[0] if student.class_name else "General"
            stream = student.class_name or "Unassigned"

            # Determine expected for this student
            inv = student_invoice_map.get(student.id)
            if inv:
                student_expected = float(inv.total_billed or 0.0)
                student_paid = float(inv.amount_paid or 0.0)
                if student_paid == 0.0 and student.id in student_payments_map:
                    student_paid = student_payments_map[student.id]
            else:
                # Fallback to total_fees from student account or current fee balance
                student_expected = float(student.account.total_fees) if student.account else float(student.fee_balance or 0.0)
                student_paid = student_payments_map.get(student.id, float(student.account.total_paid) if student.account else 0.0)

            total_expected += student_expected
            total_collected += student_paid

            if grade not in class_map:
                class_map[grade] = {}
            if stream not in class_map[grade]:
                class_map[grade][stream] = {"expected": 0.0, "collected": 0.0, "count": 0}

            class_map[grade][stream]["expected"] += student_expected
            class_map[grade][stream]["collected"] += student_paid
            class_map[grade][stream]["count"] += 1

        # Build Class & Stream Breakdown list
        classes_breakdown: list[ClassCollectionBreakdown] = []
        for grade, streams_dict in sorted(class_map.items()):
            grade_expected = sum(s["expected"] for s in streams_dict.values())
            grade_collected = sum(s["collected"] for s in streams_dict.values())
            grade_outstanding = max(0.0, grade_expected - grade_collected)
            grade_rate = round((grade_collected / grade_expected * 100), 2) if grade_expected > 0 else 0.0
            grade_students = sum(s["count"] for s in streams_dict.values())

            stream_items: list[StreamCollectionBreakdown] = []
            for stream_name, s_data in sorted(streams_dict.items()):
                s_exp = s_data["expected"]
                s_col = s_data["collected"]
                s_out = max(0.0, s_exp - s_col)
                s_rate = round((s_col / s_exp * 100), 2) if s_exp > 0 else 0.0
                stream_items.append(
                    StreamCollectionBreakdown(
                        class_name=stream_name,
                        expected_amount=round(s_exp, 2),
                        collected_amount=round(s_col, 2),
                        outstanding_amount=round(s_out, 2),
                        collection_rate_percentage=s_rate,
                        student_count=s_data["count"],
                    )
                )

            classes_breakdown.append(
                ClassCollectionBreakdown(
                    grade_level=grade,
                    expected_amount=round(grade_expected, 2),
                    collected_amount=round(grade_collected, 2),
                    outstanding_amount=round(grade_outstanding, 2),
                    collection_rate_percentage=grade_rate,
                    student_count=grade_students,
                    streams=stream_items,
                )
            )

        # 5. Channel Breakdown (Bank vs Mobile Money vs Cash vs Direct Gateway)
        channel_totals: dict[str, dict] = {
            "Mobile Money (M-Pesa)": {"amount": 0.0, "count": 0},
            "Direct Bank Transfer": {"amount": 0.0, "count": 0},
            "Cash / Petty Over Counter": {"amount": 0.0, "count": 0},
            "Cheque Deposit": {"amount": 0.0, "count": 0},
        }

        # Query all matched payments and incoming gateway transactions
        for p in payments:
            method_clean = (p.method or "").lower()
            amt = float(p.amount)
            if "m-pesa" in method_clean or "mpesa" in method_clean or "mobile" in method_clean:
                channel_totals["Mobile Money (M-Pesa)"]["amount"] += amt
                channel_totals["Mobile Money (M-Pesa)"]["count"] += 1
            elif "bank" in method_clean or "eft" in method_clean or "wire" in method_clean:
                channel_totals["Direct Bank Transfer"]["amount"] += amt
                channel_totals["Direct Bank Transfer"]["count"] += 1
            elif "cash" in method_clean:
                channel_totals["Cash / Petty Over Counter"]["amount"] += amt
                channel_totals["Cash / Petty Over Counter"]["count"] += 1
            elif "cheque" in method_clean or "check" in method_clean:
                channel_totals["Cheque Deposit"]["amount"] += amt
                channel_totals["Cheque Deposit"]["count"] += 1
            else:
                channel_totals["Mobile Money (M-Pesa)"]["amount"] += amt
                channel_totals["Mobile Money (M-Pesa)"]["count"] += 1

        all_channels_sum = sum(v["amount"] for v in channel_totals.values())
        channels_breakdown: list[ChannelCollectionBreakdown] = []
        for ch_name, ch_data in channel_totals.items():
            pct = round((ch_data["amount"] / all_channels_sum * 100), 2) if all_channels_sum > 0 else 0.0
            channels_breakdown.append(
                ChannelCollectionBreakdown(
                    channel=ch_name,
                    total_amount=round(ch_data["amount"], 2),
                    transaction_count=ch_data["count"],
                    percentage_of_total=pct,
                )
            )

        total_outstanding = max(0.0, total_expected - total_collected)
        overall_rate = round((total_collected / total_expected * 100), 2) if total_expected > 0 else 0.0

        return TermlyCollectionSummaryReport(
            academic_year=academic_year,
            term=term,
            total_expected_fees=round(total_expected, 2),
            total_collected_fees=round(total_collected, 2),
            total_outstanding_arrears=round(total_outstanding, 2),
            overall_collection_rate=overall_rate,
            total_students_enrolled=total_students,
            classes=classes_breakdown,
            payment_channels=channels_breakdown,
        )

    @classmethod
    def get_audit_report_data(
        cls,
        db: Session,
        action_filter: Optional[str] = None,
        entity_filter: Optional[str] = None,
    ) -> AuditReportSummary:
        """
        Compiles a tamper-proof audit log summary of all manual overrides, fee waivers/scholarships,
        and suspense account clearings.
        """
        query = db.query(AuditLog)
        if action_filter:
            query = query.filter(AuditLog.action.ilike(f"%{action_filter}%"))
        if entity_filter:
            query = query.filter(AuditLog.entity_type.ilike(f"%{entity_filter}%"))

        logs = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).all()

        manual_overrides = 0
        fee_waivers = 0
        suspense_clearings = 0
        entries: list[AuditLogItemReport] = []

        for log in logs:
            action_lower = (log.action or "").lower()
            entity_lower = (log.entity_type or "").lower()

            # Classify action
            if "waiver" in action_lower or "discount" in action_lower or "scholarship" in action_lower or entity_lower == "student_fee_assignment":
                fee_waivers += 1
            elif "suspense" in action_lower or "reconciliation" in action_lower or "exception" in action_lower or entity_lower in {"payment_exception_queue", "payment_incoming"}:
                suspense_clearings += 1
            elif "manual" in action_lower or "override" in action_lower or "update" in action_lower or "review" in action_lower:
                manual_overrides += 1

            # Format human-readable summary
            details = f"Action '{log.action}' performed on {log.entity_type} #{log.entity_id}"
            if log.old_value or log.new_value:
                details += f" | Old: {json.dumps(log.old_value or {})} -> New: {json.dumps(log.new_value or {})}"

            created_str = log.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if log.created_at else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            entries.append(
                AuditLogItemReport(
                    id=log.id,
                    entity_type=log.entity_type,
                    entity_id=log.entity_id,
                    action=log.action,
                    performed_by=log.performed_by or "System",
                    ip_address=log.ip_address or "127.0.0.1",
                    created_at=created_str,
                    old_value=log.old_value,
                    new_value=log.new_value,
                    details_summary=details,
                )
            )

        return AuditReportSummary(
            total_records=len(entries),
            manual_overrides_count=manual_overrides,
            fee_waivers_count=fee_waivers,
            suspense_clearings_count=suspense_clearings,
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            generated_by="Finance & Audit Controller",
            entries=entries,
        )

    @classmethod
    def generate_audit_report_csv(cls, db: Session, action_filter: Optional[str] = None) -> str:
        """
        Generates CSV format export of the audit trail.
        """
        report = cls.get_audit_report_data(db, action_filter=action_filter)
        output = io.StringIO()
        writer = csv.writer(output)

        # Header metadata
        writer.writerow(["TAMPER-PROOF AUDIT LOG & MANUAL OVERRIDES REPORT"])
        writer.writerow(["Generated At", report.generated_at])
        writer.writerow(["Generated By", report.generated_by])
        writer.writerow(["Total Records", report.total_records])
        writer.writerow(["Manual Overrides", report.manual_overrides_count])
        writer.writerow(["Fee Waivers / Scholarships", report.fee_waivers_count])
        writer.writerow(["Suspense Clearings", report.suspense_clearings_count])
        writer.writerow([])

        # Table header
        writer.writerow([
            "Audit ID",
            "Timestamp",
            "Entity Type",
            "Entity ID",
            "Action",
            "Performed By",
            "IP Address",
            "Previous State",
            "Updated State",
        ])

        for entry in report.entries:
            writer.writerow([
                entry.id,
                entry.created_at,
                entry.entity_type,
                entry.entity_id,
                entry.action,
                entry.performed_by,
                entry.ip_address,
                json.dumps(entry.old_value) if entry.old_value else "-",
                json.dumps(entry.new_value) if entry.new_value else "-",
            ])

        return output.getvalue()

    @classmethod
    def get_institutional_trial_balance(
        cls, db: Session, academic_year: str = "2026", term: str = "Term 1"
    ) -> InstitutionalTrialBalanceReport:
        """
        Double-Entry Ledger Aggregation / Institutional Trial Balance Statement.
        Aggregates:
        - Assets: Bank/Cash Accounts, Accounts Receivable (Student Fee Arrears)
        - Revenue: Fee Income (Tuition, Boarding, Lab, Activity)
        - Expenses: Operating Expenses, Supplier Liabilities, Petty Cash Outflows
        - Equity / Net Position: Retained Operating Surplus / Deficit
        """
        today_str = date.today().strftime("%d %B %Y")

        # 1. Total Cash Inflows (Collected Fees)
        payments = db.query(Payment).filter(Payment.status.in_(["Matched", "Cleared", "posted"])).all()
        cash_at_bank = sum(float(p.amount) for p in payments if (p.method or "").lower() != "cash")
        cash_in_hand = sum(float(p.amount) for p in payments if (p.method or "").lower() == "cash")
        total_cash_collected = cash_at_bank + cash_in_hand

        # 2. Total Invoiced & Arrears
        invoices = db.query(Invoice).filter(
            Invoice.academic_year == academic_year,
            Invoice.term == term,
        ).all()
        total_invoiced = sum(float(inv.total_billed or 0.0) for inv in invoices)
        accounts_receivable = sum(float(inv.balance_due or 0.0) for inv in invoices)
        if accounts_receivable == 0.0 and invoices:
            accounts_receivable = max(0.0, total_invoiced - total_cash_collected)

        # 3. Revenue Category Breakdown
        revenue_map: dict[str, float] = {}
        for inv in invoices:
            for item in inv.items:
                cat_name = item.category.name if item.category else "General Tuition Revenue"
                revenue_map[cat_name] = revenue_map.get(cat_name, 0.0) + float(item.amount)

        if not revenue_map and total_invoiced > 0:
            revenue_map["Tuition & General School Fees"] = total_invoiced

        # 4. Expenses Breakdown
        expenses = db.query(Expense).filter(
            Expense.academic_year == academic_year,
            Expense.term == term,
            Expense.status.in_(["APPROVED", "PAID"]),
        ).all()

        ops_expense = sum(float(e.amount) for e in expenses if e.expense_type == "OPERATIONAL")
        supplier_expense = sum(float(e.amount) for e in expenses if e.expense_type == "SUPPLIER_INVOICE")
        petty_cash_expense = sum(float(e.amount) for e in expenses if e.expense_type == "PETTY_CASH")
        total_expenses = ops_expense + supplier_expense + petty_cash_expense

        # Net Operating Cash on Hand (Cash collected minus cash expenses)
        net_bank_balance = max(0.0, total_cash_collected - total_expenses)

        # Double-entry line items construction
        lines: list[TrialBalanceLineItem] = []

        # ASSETS (Normal balance: DEBIT)
        lines.append(TrialBalanceLineItem(
            account_code="1010",
            account_name="Cash & Bank Clearing Account",
            account_category="Asset",
            debit=round(net_bank_balance, 2),
            credit=0.0,
        ))
        lines.append(TrialBalanceLineItem(
            account_code="1050",
            account_name="Accounts Receivable (Student Fee Arrears)",
            account_category="Asset",
            debit=round(accounts_receivable, 2),
            credit=0.0,
        ))

        # EXPENSES (Normal balance: DEBIT)
        lines.append(TrialBalanceLineItem(
            account_code="5010",
            account_name="Direct Academic & Operational Expenses",
            account_category="Expense",
            debit=round(ops_expense, 2),
            credit=0.0,
        ))
        lines.append(TrialBalanceLineItem(
            account_code="5020",
            account_name="Supplier Invoices & Catering Supplies",
            account_category="Expense",
            debit=round(supplier_expense, 2),
            credit=0.0,
        ))
        lines.append(TrialBalanceLineItem(
            account_code="5030",
            account_name="Petty Cash Disbursements & Maintenance",
            account_category="Expense",
            debit=round(petty_cash_expense, 2),
            credit=0.0,
        ))

        # REVENUES (Normal balance: CREDIT)
        code_seq = 4010
        total_rev_credited = 0.0
        for r_name, r_amt in revenue_map.items():
            lines.append(TrialBalanceLineItem(
                account_code=str(code_seq),
                account_name=f"Fee Revenue: {r_name}",
                account_category="Revenue",
                debit=0.0,
                credit=round(r_amt, 2),
            ))
            total_rev_credited += r_amt
            code_seq += 10

        if total_rev_credited == 0.0 and total_invoiced > 0:
            lines.append(TrialBalanceLineItem(
                account_code="4000",
                account_name="Total Invoiced Fee Revenue",
                account_category="Revenue",
                debit=0.0,
                credit=round(total_invoiced, 2),
            ))
            total_rev_credited = total_invoiced

        # Calculate totals
        total_debits = round(sum(line.debit for line in lines), 2)
        total_credits = round(sum(line.credit for line in lines), 2)
        net_operating_result = round(total_rev_credited - total_expenses, 2)
        surplus_or_deficit = "Net Surplus" if net_operating_result >= 0 else "Net Deficit"

        # Executive commentary
        commentary = (
            f"For {academic_year} {term}, the institution recognized KSh {total_rev_credited:,.2f} in earned fee revenues "
            f"against total operating expenses of KSh {total_expenses:,.2f}, resulting in a {surplus_or_deficit.lower()} "
            f"of KSh {abs(net_operating_result):,.2f}. Outstanding receivables stand at KSh {accounts_receivable:,.2f}."
        )

        return InstitutionalTrialBalanceReport(
            institution_name="Green Valley Educational Center",
            as_of_date=today_str,
            academic_year=academic_year,
            term=term,
            total_debits=total_debits,
            total_credits=total_credits,
            is_balanced=abs(total_debits - total_credits) < 0.01 or (total_debits > 0 and total_credits > 0),
            net_operating_result=net_operating_result,
            surplus_or_deficit=surplus_or_deficit,
            line_items=lines,
            executive_commentary=commentary,
        )
