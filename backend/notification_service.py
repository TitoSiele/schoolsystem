"""
Automated Arrears Tracking & Scheduled Notification Dispatcher.
Handles:
- Scheduled background balance checks against due dates
- Identification of overdue student accounts and categorizing severity (MILD, MODERATE, CRITICAL)
- Automated SMS and Email notification dispatcher (Receipts, Invoices, Payment Reminders)
- Pluggable Mock/Production SMS & Email Gateway Adapters
- Async background task runner integration for periodic scheduled execution
"""

import asyncio
from datetime import date, datetime, timezone
import logging
from typing import Optional
import uuid

from sqlalchemy.orm import Session

from models import Invoice, NotificationLog, NotificationTemplate, Payment, PaymentIncoming, Student, StudentAccount
from schemas import (
    ArrearsReportResponse,
    ArrearsStudentSummary,
    NotificationBatchResult,
    NotificationLogResponse,
    TriggerReminderRequest,
)

logger = logging.getLogger("notification_engine")


class GatewayAdapter:
    """
    Enterprise Notification Gateway Provider Adapter.
    In a live production deployment, this connects to providers like Africa's Talking / Twilio for SMS
    and SendGrid / AWS SES for Email.
    """

    @staticmethod
    def send_sms(phone: str, message: str) -> tuple[bool, str, Optional[str]]:
        """
        Simulates SMS Gateway delivery.
        Returns: (success: bool, gateway_reference: str, error_message: str | None)
        """
        ref = f"SMS-{uuid.uuid4().hex[:10].upper()}"
        if not phone or len(phone.strip()) < 5:
            return False, ref, "Invalid phone number provided"
        # Gateway simulation: success
        logger.info(f"[SMS DISPATCH] Sent to {phone} | Ref: {ref} | Msg: {message[:40]}...")
        return True, ref, None

    @staticmethod
    def send_email(email: str, subject: str, body: str) -> tuple[bool, str, Optional[str]]:
        """
        Simulates Email Gateway delivery.
        Returns: (success: bool, gateway_reference: str, error_message: str | None)
        """
        ref = f"EML-{uuid.uuid4().hex[:10].upper()}"
        if not email or "@" not in email:
            return False, ref, "Invalid email address format"
        # Gateway simulation: success
        logger.info(f"[EMAIL DISPATCH] Sent to {email} | Subject: {subject} | Ref: {ref}")
        return True, ref, None


class ArrearsNotificationService:

    @classmethod
    def analyze_arrears(cls, db: Session, min_balance: float = 1.0, min_days_overdue: int = 0) -> ArrearsReportResponse:
        """
        Scans all student fee balances and overdue invoices against current date.
        Categorizes urgency based on days overdue and outstanding balance size.
        """
        today = date.today()
        students = db.query(Student).filter(Student.fee_balance >= min_balance).all()

        overdue_students: list[ArrearsStudentSummary] = []
        critical_count = 0
        moderate_count = 0
        mild_count = 0
        total_arrears = 0.0

        for student in students:
            balance = float(student.fee_balance or 0.0)
            total_arrears += balance

            # Check unpaid invoices for this student
            unpaid_invoices = (
                db.query(Invoice)
                .filter(Invoice.student_id == student.id, Invoice.balance_due > 0)
                .order_by(Invoice.due_date.asc())
                .all()
            )

            days_overdue = 0
            if unpaid_invoices:
                earliest_due = unpaid_invoices[0].due_date
                if earliest_due and earliest_due < today:
                    days_overdue = (today - earliest_due).days

            # Filter by minimum days overdue if requested
            if days_overdue < min_days_overdue:
                continue

            # Urgency Classification
            if days_overdue > 60 or balance >= 50000:
                urgency = "CRITICAL"
                critical_count += 1
            elif days_overdue > 30 or balance >= 25000:
                urgency = "MODERATE"
                moderate_count += 1
            else:
                urgency = "MILD"
                mild_count += 1

            # Find latest payment date
            latest_payment = (
                db.query(Payment)
                .filter(Payment.student_id == student.id)
                .order_by(Payment.date.desc())
                .first()
            )
            last_pay_date = latest_payment.date if latest_payment else None

            overdue_students.append(
                ArrearsStudentSummary(
                    student_id=student.id,
                    admission_number=student.admission_number,
                    student_name=f"{student.first_name} {student.last_name}",
                    class_name=student.class_name,
                    parent_name=student.parent_name,
                    parent_phone=student.parent_phone,
                    parent_email=student.parent_email,
                    total_balance=round(balance, 2),
                    days_overdue=days_overdue,
                    overdue_invoices_count=len(unpaid_invoices),
                    last_payment_date=last_pay_date,
                    urgency_level=urgency,
                )
            )

        # Sort with critical first, then highest balance
        urgency_rank = {"CRITICAL": 3, "MODERATE": 2, "MILD": 1}
        overdue_students.sort(
            key=lambda s: (urgency_rank.get(s.urgency_level, 0), s.total_balance),
            reverse=True,
        )

        return ArrearsReportResponse(
            total_overdue_students=len(overdue_students),
            total_arrears_amount=round(total_arrears, 2),
            critical_count=critical_count,
            moderate_count=moderate_count,
            mild_count=mild_count,
            students=overdue_students,
        )

    @classmethod
    def dispatch_arrears_reminders(
        cls, db: Session, request: TriggerReminderRequest
    ) -> NotificationBatchResult:
        """
        Dispatches targeted SMS / Email reminders to parents with overdue balances.
        """
        report = cls.analyze_arrears(
            db, min_balance=request.min_balance, min_days_overdue=request.min_days_overdue
        )

        target_students = report.students
        if request.student_ids:
            target_ids_set = set(request.student_ids)
            target_students = [s for s in target_students if s.student_id in target_ids_set]

        dispatched = 0
        sms_count = 0
        email_count = 0
        failed_count = 0
        logs_created: list[NotificationLog] = []

        for student_data in target_students:
            parent_name = student_data.parent_name or "Parent/Guardian"
            bal_str = f"KSh {student_data.total_balance:,.2f}"

            # 1. SMS Dispatch
            if "SMS" in request.channels and student_data.parent_phone:
                if request.custom_message:
                    msg = request.custom_message
                else:
                    msg = (
                        f"Dear {parent_name}, this is a gentle reminder that {student_data.student_name} "
                        f"({student_data.admission_number}) has an outstanding school fee balance of {bal_str}. "
                        f"Kindly clear via School M-Pesa Paybill 247247. Thank you."
                    )

                success, ref, err = GatewayAdapter.send_sms(student_data.parent_phone, msg)
                log = NotificationLog(
                    student_id=student_data.student_id,
                    recipient_type="parent",
                    recipient_identifier=student_data.parent_phone,
                    channel="SMS",
                    notification_type="ARREARS_REMINDER",
                    subject="Fee Balance Reminder",
                    message_body=msg,
                    status="SENT" if success else "FAILED",
                    gateway_reference=ref,
                    error_message=err,
                    sent_at=datetime.now(timezone.utc) if success else None,
                )
                db.add(log)
                logs_created.append(log)
                if success:
                    dispatched += 1
                    sms_count += 1
                else:
                    failed_count += 1

            # 2. Email Dispatch
            if "EMAIL" in request.channels and student_data.parent_email:
                subject = f"Official Fee Arrears Notice - {student_data.student_name} ({student_data.admission_number})"
                body = (
                    f"Dear {parent_name},\n\n"
                    f"This is an automated notification from the School Bursary Department regarding "
                    f"the fee account of {student_data.student_name} (Class: {student_data.class_name}).\n\n"
                    f"Outstanding Balance: {bal_str}\n"
                    f"Days Overdue: {student_data.days_overdue} days\n\n"
                    f"Please remit payment at your earliest convenience to maintain uninterrupted school services.\n\n"
                    f"Best regards,\nGreen Valley School Finance Office"
                )

                success, ref, err = GatewayAdapter.send_email(student_data.parent_email, subject, body)
                log = NotificationLog(
                    student_id=student_data.student_id,
                    recipient_type="parent",
                    recipient_identifier=student_data.parent_email,
                    channel="EMAIL",
                    notification_type="ARREARS_REMINDER",
                    subject=subject,
                    message_body=body,
                    status="SENT" if success else "FAILED",
                    gateway_reference=ref,
                    error_message=err,
                    sent_at=datetime.now(timezone.utc) if success else None,
                )
                db.add(log)
                logs_created.append(log)
                if success:
                    dispatched += 1
                    email_count += 1
                else:
                    failed_count += 1

        db.commit()
        for l in logs_created:
            db.refresh(l)

        return NotificationBatchResult(
            dispatched_count=dispatched,
            sms_count=sms_count,
            email_count=email_count,
            failed_count=failed_count,
            logs=[NotificationLogResponse.model_validate(l) for l in logs_created],
        )

    @classmethod
    def dispatch_payment_receipt(
        cls, db: Session, payment_id: int, channels: list[str] = ["SMS", "EMAIL"]
    ) -> list[NotificationLogResponse]:
        """
        Sends automated digital receipts immediately when payments clear.
        """
        payment = db.query(Payment).filter(Payment.id == payment_id).first()
        if not payment or not payment.student_id:
            # Check if it's an incoming payment
            incoming = db.query(PaymentIncoming).filter(PaymentIncoming.id == payment_id).first()
            if incoming and incoming.matched_student_id:
                student = db.query(Student).filter(Student.id == incoming.matched_student_id).first()
                amount = float(incoming.amount)
                ref = incoming.gateway_txn_id
                pay_date = incoming.received_at.strftime("%d-%b-%Y")
                payer_phone = incoming.phone_number or (student.parent_phone if student else None)
            else:
                return []
        else:
            student = db.query(Student).filter(Student.id == payment.student_id).first()
            amount = float(payment.amount)
            ref = payment.transaction_reference or f"TXN-{payment.id}"
            pay_date = payment.date.strftime("%d-%b-%Y") if payment.date else date.today().strftime("%d-%b-%Y")
            payer_phone = payment.payer_phone or (student.parent_phone if student else None)

        if not student:
            return []

        remaining_balance = float(student.fee_balance or 0.0)
        parent_name = student.parent_name or "Parent/Guardian"
        created_logs: list[NotificationLog] = []

        # 1. SMS Digital Receipt
        if "SMS" in channels and payer_phone:
            msg = (
                f"Receipt Confirmed! We received KSh {amount:,.2f} for {student.first_name} {student.last_name} "
                f"({student.admission_number}). Ref: {ref} on {pay_date}. "
                f"New Balance: KSh {remaining_balance:,.2f}. Thank you for your payment."
            )
            success, g_ref, err = GatewayAdapter.send_sms(payer_phone, msg)
            log = NotificationLog(
                student_id=student.id,
                recipient_type="parent",
                recipient_identifier=payer_phone,
                channel="SMS",
                notification_type="PAYMENT_RECEIPT",
                subject="Payment Receipt Confirmation",
                message_body=msg,
                status="SENT" if success else "FAILED",
                gateway_reference=g_ref,
                error_message=err,
                sent_at=datetime.now(timezone.utc) if success else None,
            )
            db.add(log)
            created_logs.append(log)

        # 2. Email Digital Receipt
        if "EMAIL" in channels and student.parent_email:
            subject = f"Official Fee Payment Receipt - Ref: {ref}"
            body = (
                f"Dear {parent_name},\n\n"
                f"We acknowledge receipt of your school fee payment for {student.first_name} {student.last_name} "
                f"({student.admission_number}).\n\n"
                f"--- PAYMENT DETAILS ---\n"
                f"Transaction Reference: {ref}\n"
                f"Amount Paid: KSh {amount:,.2f}\n"
                f"Date Received: {pay_date}\n"
                f"Current Outstanding Balance: KSh {remaining_balance:,.2f}\n\n"
                f"Thank you for your continued support.\n\n"
                f"Green Valley School Accounts Office"
            )
            success, g_ref, err = GatewayAdapter.send_email(student.parent_email, subject, body)
            log = NotificationLog(
                student_id=student.id,
                recipient_type="parent",
                recipient_identifier=student.parent_email,
                channel="EMAIL",
                notification_type="PAYMENT_RECEIPT",
                subject=subject,
                message_body=body,
                status="SENT" if success else "FAILED",
                gateway_reference=g_ref,
                error_message=err,
                sent_at=datetime.now(timezone.utc) if success else None,
            )
            db.add(log)
            created_logs.append(log)

        db.commit()
        for l in created_logs:
            db.refresh(l)

        return [NotificationLogResponse.model_validate(l) for l in created_logs]


class BackgroundScheduler:
    """
    Asynchronous scheduled background worker for periodically checking arrears and triggering alerts.
    Can run continuously in background or execute via Celery/Cron triggers.
    """
    _running = False
    _task: Optional[asyncio.Task] = None

    @classmethod
    async def run_periodic_arrears_check(cls, session_factory, interval_seconds: int = 86400):
        """
        Background task that executes daily (default: 86400s) to monitor overdue student ledgers.
        """
        cls._running = True
        logger.info(f"Background Arrears Scheduler started (interval: {interval_seconds}s)")
        while cls._running:
            try:
                db: Session = session_factory()
                try:
                    logger.info("Executing periodic arrears scan & reminder dispatch...")
                    req = TriggerReminderRequest(
                        channels=["SMS", "EMAIL"],
                        min_balance=100.0,
                        min_days_overdue=7,
                    )
                    result = ArrearsNotificationService.dispatch_arrears_reminders(db, req)
                    logger.info(f"Periodic reminder run complete: {result.dispatched_count} notifications dispatched.")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"Error in background arrears scheduler loop: {e}")

            await asyncio.sleep(interval_seconds)

    @classmethod
    def start_scheduler(cls, session_factory, interval_seconds: int = 86400):
        if not cls._running:
            loop = asyncio.get_event_loop()
            cls._task = loop.create_task(cls.run_periodic_arrears_check(session_factory, interval_seconds))

    @classmethod
    def stop_scheduler(cls):
        cls._running = False
        if cls._task:
            cls._task.cancel()
