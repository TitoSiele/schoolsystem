"""
Expense Tracking & Institutional Financial Summary Module.
Handles:
- Operational Expenses, Supplier Invoices, and Petty Cash Disbursements
- Category-level expense allocation and auditing
- Financial Summary Ledger endpoint aggregating term revenue vs recorded expenses
- Bottom-line Institutional Net Financial Health calculation (Operating Cash Flow, Accrual Surplus/Deficit, Operating Margin)
"""

from datetime import date, datetime
from typing import Optional
from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import HTTPException

from models import Expense, ExpenseCategory, Invoice, InvoiceItem, Payment, PaymentLedger, Student
from schemas import (
    CategoryFinancialBreakdown,
    ExpenseCategoryCreate,
    ExpenseCreate,
    TermFinancialHealthSummary,
)


class ExpenseService:

    @staticmethod
    def create_expense_category(db: Session, cat_in: ExpenseCategoryCreate) -> ExpenseCategory:
        existing = db.query(ExpenseCategory).filter(ExpenseCategory.code == cat_in.code.upper()).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Expense category code '{cat_in.code}' already exists")

        category = ExpenseCategory(
            code=cat_in.code.upper(),
            name=cat_in.name,
            description=cat_in.description,
            is_active=cat_in.is_active,
        )
        db.add(category)
        db.commit()
        db.refresh(category)
        return category

    @staticmethod
    def list_expense_categories(db: Session) -> list[ExpenseCategory]:
        return db.query(ExpenseCategory).filter(ExpenseCategory.is_active == True).order_by(ExpenseCategory.name).all()

    @staticmethod
    def record_expense(db: Session, expense_in: ExpenseCreate) -> Expense:
        category = db.query(ExpenseCategory).filter(ExpenseCategory.id == expense_in.category_id).first()
        if not category:
            raise HTTPException(status_code=404, detail=f"Expense category id {expense_in.category_id} not found")

        # Generate sequence expense number
        count = db.query(Expense).count() + 1
        exp_number = f"EXP-{expense_in.academic_year}-{count:04d}"

        expense = Expense(
            expense_number=exp_number,
            expense_type=expense_in.expense_type,
            category_id=category.id,
            title=expense_in.title,
            description=expense_in.description,
            amount=round(expense_in.amount, 2),
            currency=expense_in.currency,
            academic_year=expense_in.academic_year,
            term=expense_in.term,
            payment_method=expense_in.payment_method,
            payment_reference=expense_in.payment_reference,
            vendor_name=expense_in.vendor_name,
            vendor_contact=expense_in.vendor_contact,
            vendor_invoice_number=expense_in.vendor_invoice_number,
            disbursed_to=expense_in.disbursed_to,
            disbursed_by=expense_in.disbursed_by,
            status=expense_in.status,
            expense_date=expense_in.expense_date or date.today(),
        )
        db.add(expense)
        db.commit()
        db.refresh(expense)
        return expense

    @staticmethod
    def list_expenses(
        db: Session,
        academic_year: Optional[str] = None,
        term: Optional[str] = None,
        expense_type: Optional[str] = None,
        category_id: Optional[int] = None,
    ) -> list[Expense]:
        query = db.query(Expense)
        if academic_year:
            query = query.filter(Expense.academic_year == academic_year)
        if term:
            query = query.filter(Expense.term == term)
        if expense_type:
            query = query.filter(Expense.expense_type == expense_type)
        if category_id:
            query = query.filter(Expense.category_id == category_id)
        return query.order_by(Expense.expense_date.desc(), Expense.id.desc()).all()

    @classmethod
    def get_term_financial_summary(
        cls, db: Session, academic_year: str, term: str
    ) -> TermFinancialHealthSummary:
        """
        Summary Ledger endpoint calculating net institutional financial health:
        1. Aggregates term fee invoicing (accrual billing) & total collected payments (cash receipts).
        2. Aggregates operational expenses, supplier invoices, and petty cash disbursements.
        3. Computes net operating cash flow, accrual surplus/deficit, and operating margin.
        4. Calculates category-level revenue and expense breakdowns.
        """
        # --- REVENUE / INFLOWS ---
        invoices = db.query(Invoice).filter(
            Invoice.academic_year == academic_year,
            Invoice.term == term
        ).all()

        total_invoiced = sum(float(inv.total_billed or 0.0) for inv in invoices)

        # Total payments received for this academic term/year
        # Query Payment table
        payments = db.query(Payment).all()
        # For realistic simulation, sum matched and received payments
        total_collected = sum(float(p.amount) for p in payments if p.status in {"Matched", "Cleared", "posted"})

        # If payment ledger entries exist, factor posted ledger
        ledger_total = db.query(func.sum(PaymentLedger.amount)).filter(PaymentLedger.status == "posted").scalar()
        if ledger_total and ledger_total > total_collected:
            total_collected = float(ledger_total)

        total_arrears = sum(float(inv.balance_due or 0.0) for inv in invoices)
        if total_arrears == 0.0 and invoices:
            total_arrears = max(0.0, total_invoiced - total_collected)

        collection_rate = (
            round((total_collected / total_invoiced * 100), 2)
            if total_invoiced > 0
            else (100.0 if total_collected > 0 else 0.0)
        )

        # Revenue Breakdown by Fee Category
        revenue_by_cat_map: dict[str, dict] = {}
        for inv in invoices:
            for item in inv.items:
                cat_name = item.category.name if item.category else "General Tuition"
                cat_code = item.category.code if item.category else "GEN"
                if cat_name not in revenue_by_cat_map:
                    revenue_by_cat_map[cat_name] = {"code": cat_code, "amount": 0.0}
                revenue_by_cat_map[cat_name]["amount"] += float(item.amount)

        revenue_breakdown: list[CategoryFinancialBreakdown] = []
        for cat_name, data in revenue_by_cat_map.items():
            pct = round((data["amount"] / total_invoiced * 100), 2) if total_invoiced > 0 else 0.0
            revenue_breakdown.append(
                CategoryFinancialBreakdown(
                    category_name=cat_name,
                    category_code=data["code"],
                    amount=round(data["amount"], 2),
                    percentage_of_total=pct,
                )
            )

        # --- EXPENDITURES / OUTFLOWS ---
        expenses = db.query(Expense).filter(
            Expense.academic_year == academic_year,
            Expense.term == term,
            Expense.status.in_(["APPROVED", "PAID"]),
        ).all()

        total_ops = sum(float(e.amount) for e in expenses if e.expense_type == "OPERATIONAL")
        total_supplier = sum(float(e.amount) for e in expenses if e.expense_type == "SUPPLIER_INVOICE")
        total_petty = sum(float(e.amount) for e in expenses if e.expense_type == "PETTY_CASH")
        total_all_expenses = sum(float(e.amount) for e in expenses)

        # Expense Breakdown by Category
        exp_by_cat_map: dict[str, dict] = {}
        for exp in expenses:
            cat_name = exp.category.name if exp.category else "General Operational"
            cat_code = exp.category.code if exp.category else "GEN_EXP"
            if cat_name not in exp_by_cat_map:
                exp_by_cat_map[cat_name] = {"code": cat_code, "amount": 0.0}
            exp_by_cat_map[cat_name]["amount"] += float(exp.amount)

        expenses_breakdown: list[CategoryFinancialBreakdown] = []
        for cat_name, data in exp_by_cat_map.items():
            pct = round((data["amount"] / total_all_expenses * 100), 2) if total_all_expenses > 0 else 0.0
            expenses_breakdown.append(
                CategoryFinancialBreakdown(
                    category_name=cat_name,
                    category_code=data["code"],
                    amount=round(data["amount"], 2),
                    percentage_of_total=pct,
                )
            )

        # --- NET FINANCIAL HEALTH ---
        # 1. Net Operating Cash Flow (Cash received - Cash expenses)
        net_cash_flow = round(total_collected - total_all_expenses, 2)
        
        # 2. Net Accrual Surplus/Deficit (Total Invoiced - Total Expenses)
        net_accrual = round(total_invoiced - total_all_expenses, 2)

        # 3. Operating Margin %
        operating_margin = (
            round((net_cash_flow / total_collected * 100), 2)
            if total_collected > 0
            else 0.0
        )

        # 4. Rating logic
        if net_cash_flow > 0 and collection_rate >= 80:
            health_rating = "EXCELLENT"
        elif net_cash_flow >= 0 and collection_rate >= 60:
            health_rating = "HEALTHY"
        elif net_cash_flow < 0 and net_accrual > 0:
            health_rating = "CAUTION"  # Accrual profitable but cash flow tight due to arrears
        else:
            health_rating = "DEFICIT"

        return TermFinancialHealthSummary(
            academic_year=academic_year,
            term=term,
            total_invoiced_amount=round(total_invoiced, 2),
            total_collected_revenue=round(total_collected, 2),
            total_uncollected_arrears=round(total_arrears, 2),
            collection_rate_percentage=collection_rate,
            total_operating_expenses=round(total_ops, 2),
            total_supplier_invoices=round(total_supplier, 2),
            total_petty_cash=round(total_petty, 2),
            total_all_expenses=round(total_all_expenses, 2),
            net_operating_cash_flow=net_cash_flow,
            net_accrual_surplus_deficit=net_accrual,
            operating_margin_percentage=operating_margin,
            financial_health_rating=health_rating,
            revenue_by_category=revenue_breakdown,
            expenses_by_category=expenses_breakdown,
        )
