# SchoolPay

Multi-tenant school fee management for Kenyan schools. FastAPI + SQLAlchemy + SQLite, with a plain HTML/CSS/JS frontend and no build step.

One command runs everything at `http://127.0.0.1:8000/`.

---

## Quick start

```bash
cd backend
python -m venv ../.venv
../.venv/Scripts/pip install -r requirements.txt      # Windows
../.venv/bin/pip install -r requirements.txt          # macOS / Linux

# REQUIRED before first run - the server will not start without it.
cp ../.env.example ../.env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SCHOOLPAY_SECRET

python -m uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> and click **Create a school account** to sign up your school.

> **Run uvicorn from inside `backend/`.** The app uses flat imports
> (`from models import ...`), which only resolve when `backend/` is on the
> Python path.

The database is created automatically on first start. To start over, delete
`backend/school.db`.

---

## Features

**Fees and money**
- Students, fee templates, term invoices, arrears tracking
- Payments with unique transaction references (duplicates rejected)
- Money stored as `Numeric(12,2)` / `Decimal` - never floats

**M-Pesa**
- STK Push - parents pay from their own phone (works on feature phones)
- Till reconciliation from the Safaricom CSV, matched in four tiers:
  reference -> admission number -> amount + phone -> amount + name
- Dry run by default; posting is opt-in and idempotent (re-running a
  statement never double-posts)

**Timetable**
- Weekly grid by class, click to edit
- Conflict detection: a class cannot be double-booked, and neither can a teacher
- Auto-generator produces a valid, balanced starting timetable

**Platform**
- Multi-tenant: every school sees only its own data, enforced centrally in
  `backend/tenancy.py` rather than at each query site
- Plans with seat limits (trial / starter / pro / enterprise)
- Monthly subscription invoices, partial payments, revenue dashboard

**Data entry**
- Bulk import from CSV, XLSX, DOCX and PDF with a review step
- Student photos

**Reports**
- Fee collection, student balances, payments, arrears, trial balance
- Audit trail with CSV export

---

## Project layout

```
backend/
  main.py             FastAPI app; routes mounted here
  models.py           SQLAlchemy models
  schemas.py          Pydantic request/response models
  auth.py             Password hashing, sessions, tenant enforcement
  tenancy.py          Central multi-tenant query scoping
  database.py         Engine and session factory
  admin_router.py     Platform admin API (cross-tenant)
  subscription_router.py   School subscription billing
  mpesa.py            Safaricom Daraja client (stdlib only)
  mpesa_router.py     STK Push + till reconciliation
  timetable_router.py Timetable API
  document_intake.py  CSV / XLSX / DOCX / PDF parsing
  services/           Billing, arrears, expenses, analytics
  tests/              Development scripts (need a local server)
frontend/
  index.html          All pages, single-file structure
  app.js              Vanilla JS, no framework
  style.css           Styling
  config.js           API base URL - the only place it is set
```

---

## Security notes

- `SCHOOLPAY_SECRET` is **required**. The server refuses to start without it,
  because a default signing key would let anyone forge an admin session cookie.
- All database files are gitignored: they contain real student names, parent
  phone numbers and fee balances.
- Multi-tenant isolation is applied centrally rather than per query, so a
  forgotten filter cannot leak one school's data to another.

---

## Before deploying for real

- [ ] Set `SCHOOLPAY_SECRET` to a strong random value
- [ ] Set `MPESA_SANDBOX=false` and add real Daraja credentials
- [ ] Move from SQLite to PostgreSQL
- [ ] Add pagination - fine at 500 students, slow at 50,000
- [ ] Install the Tesseract engine if you need OCR on scanned documents
- [ ] Serve over HTTPS so the session cookie is marked `Secure`
