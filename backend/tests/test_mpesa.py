import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Tests M-Pesa status, STK push guardrails and the reconciliation tiers."""
import http.cookiejar
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=30) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw

    def get(self, p): return self.request("GET", p)
    def post(self, p, b=None): return self.request("POST", p, b)


def show(label, result, expect=None):
    status, body = result
    ok = "OK " if (expect is None or status == expect) else "!! "
    print(f"{ok}{label}: {status} {str(body)[:170]}")


c = Client()

print("\n=== M-Pesa status (no Daraja credentials configured) ===")
show("GET /api/mpesa/status", c.get("/api/mpesa/status"), expect=200)

print("\n=== STK push must fail clearly, not crash ===")
status, students = c.get("/students")
student = students[0] if students else None
show("stkpush (unconfigured)", c.post("/api/mpesa/stkpush", {
    "student_id": student["id"] if student else None,
    "phone_number": "0712345678",
    "amount": 5000,
}), expect=502)

print("\n=== STK push validation ===")
show("amount zero", c.post("/api/mpesa/stkpush", {"amount": 0, "phone_number": "0712345678"}), expect=422)
show("no phone and no student", c.post("/api/mpesa/stkpush", {"amount": 500}), expect=400)
show("bad student id", c.post("/api/mpesa/stkpush", {"amount": 500, "student_id": 99999}), expect=404)

print("\n=== reconciliation: dry run (should NOT post) ===")
rows = [
    # Tier 1: exact transaction reference already recorded
    {"receipt": "QJG7X2K9LM", "amount": 1000, "payer_name": "Someone", "phone": "0700000000", "date": "2026-10-01"},
    # Tier 2: admission number inside the reference
    {"receipt": f"MPX-{student['admission_number']}-99", "amount": 2500,
     "payer_name": "Parent Of Student", "phone": "", "date": "2026-10-01"},
    # Tier 3: amount + parent phone matching the outstanding balance
    {"receipt": "RCP-PHONE-1", "amount": float(student["fee_balance"] or 0),
     "payer_name": "Anything", "phone": student["parent_phone"], "date": "2026-10-01"},
    # Should NOT match - nothing in common
    {"receipt": "RCP-UNKNOWN-1", "amount": 7777, "payer_name": "Zzzz Stranger", "phone": "0700009999", "date": "2026-10-01"},
]
before = len(c.get("/payments")[1] or [])
show("POST /api/mpesa/reconcile (dry run)", c.post("/api/mpesa/reconcile", {"rows": rows}), expect=200)

status, summary = c.post("/api/mpesa/reconcile", {"rows": rows})
print(f"\n  summary: total={summary['total']} matched={summary['matched']} unmatched={summary['unmatched']} posted={summary['posted']}")
print(f"  message: {summary['message']}")
for r in summary["rows"]:
    print(f"    line {r['line']}: {r['match_status']:9} {str(r['student_name']):22} {r['reason']}")

after = len(c.get("/payments")[1] or [])
print(f"\n  payments before={before} after={after} (dry run must not change it)")
print("  OK" if before == after else "  !! DRY RUN POSTED SOMETHING")

print("\n=== reconciliation: post only tier-2 (safe, unique reference) ===")
post_rows = [rows[1]]
status, posted = c.post("/api/mpesa/reconcile", {"rows": post_rows, "post_matched": True})
show("post matched", (status, posted), expect=200)
print(f"  {posted['message']}")

print("\n=== re-running the same statement must NOT double-post ===")
status, again = c.post("/api/mpesa/reconcile", {"rows": post_rows, "post_matched": True})
print(f"  {again['message']}")
for r in again["rows"]:
    print(f"    line {r['line']}: {r['reason']}")

print("\n=== bad input ===")
show("no rows and no file", c.post("/api/mpesa/reconcile", {}), expect=400)

print("\n=== unauthenticated blocked ===")
anon = Client()
show("anon status", anon.get("/api/mpesa/status"), expect=401)
show("anon reconcile", anon.post("/api/mpesa/reconcile", {"rows": rows}), expect=401)