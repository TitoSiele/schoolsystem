import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Tests subscription billing: plans, invoice generation, payment, guards, revenue."""
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
    print(f"{ok}{label}: {status} {str(body)[:140]}")


def login(email, pw):
    c = Client()
    return c, c.post("/api/auth/login", {"email": email, "password": pw})


pa, s = login(PLATFORM_EMAIL, PLATFORM_PASSWORD)
print(f"platform admin login: {s}")

gv, s = login(GV_EMAIL, GV_PASSWORD)
print(f"school login: {s}")

print("\n=== plans catalogue ===")
status, plans = pa.get("/api/subscriptions/plans")
print(f"  {status}")
for p in plans or []:
    price = "free" if p["monthly_price"] is None else f"KSh {p['monthly_price']:,}/mo"
    cap = "unlimited" if p["max_students"] is None else str(p["max_students"])
    print(f"    {p['code']:11} {p['name']:11} {price:16} {cap:10} students, {len(p['features'])} features")

print("\n=== school's own subscription view ===")
show("GET /current", gv.get("/api/subscriptions/current"), expect=200)
print(f"    {gv.get('/api/subscriptions/current')[1]}")

print("\n=== generate invoices for this month ===")
status, created = pa.post("/api/subscriptions/generate", {})
print(f"  created {len(created or [])} invoice(s)")
for inv in created or []:
    print(f"    {inv['invoice_number']:24} {str(inv['school_name']):22} {inv['plan']:9} KSh {float(inv['amount_due']):>9,.2f}  {inv['status']}")

print("\n=== idempotency: run again (must create nothing) ===")
status, again = pa.post("/api/subscriptions/generate", {})
print(f"  second run created {len(again or [])} invoice(s)  {'OK' if not again else '!! DUPLICATED'}")

print("\n=== bad period rejected ===")
show("period=nonsense", pa.post("/api/subscriptions/generate?period=nonsense"), expect=400)

print("\n=== school CANNOT do platform billing ===")
show("school generates", gv.post("/api/subscriptions/generate", {}), expect=403)
show("school sees all invoices", gv.get("/api/subscriptions/all"), expect=403)
show("school sees revenue", gv.get("/api/subscriptions/revenue"), expect=403)

print("\n=== partial payment, then settle ===")
status, all_inv = pa.get("/api/subscriptions/all")
target = next((i for i in all_inv or [] if float(i["balance"]) > 0), None)

if target:
    half = round(float(target["balance"]) / 2, 2)
    show(f"partial KSh {half:,.2f}",
         pa.post(f"/api/subscriptions/invoices/{target['id']}/pay",
                 {"amount": half, "method": "M-Pesa", "reference": "MPX-SUB-PART1"}), expect=200)

    inv = next(i for i in pa.get("/api/subscriptions/all")[1] if i["id"] == target["id"])
    print(f"    after partial: status={inv['status']} paid={float(inv['amount_paid']):,.2f} balance={float(inv['balance']):,.2f} payments={len(inv['payments'])}")

    rest = round(float(inv["balance"]), 2)
    show(f"remainder KSh {rest:,.2f}",
         pa.post(f"/api/subscriptions/invoices/{target['id']}/pay",
                 {"amount": rest, "method": "Bank Transfer"}), expect=200)

    inv = next(i for i in pa.get("/api/subscriptions/all")[1] if i["id"] == target["id"])
    print(f"    after full:    status={inv['status']} balance={float(inv['balance']):,.2f} payments={len(inv['payments'])}")

    print("\n=== guards ===")
    show("overpay", pa.post(f"/api/subscriptions/invoices/{inv['id']}/pay", {"amount": 500}), expect=400)
    show("pay a settled invoice", pa.post(f"/api/subscriptions/invoices/{inv['id']}/pay", {"amount": 1}), expect=400)
    show("zero amount", pa.post(f"/api/subscriptions/invoices/{inv['id']}/pay", {"amount": 0}), expect=422)

print("\n=== waive ===")
status, unpaid = pa.get("/api/subscriptions/all?status=unpaid")
if unpaid:
    inv = unpaid[0]
    show(f"waive {inv['invoice_number']}",
         pa.post(f"/api/subscriptions/invoices/{inv['id']}/waive?reason=goodwill"), expect=200)
    after = next(i for i in pa.get("/api/subscriptions/all")[1] if i["id"] == inv["id"])
    print(f"    status={after['status']} notes={after['notes']}")

print("\n=== revenue (platform only) ===")
status, rev = pa.get("/api/subscriptions/revenue")
print(f"  {status}")
if isinstance(rev, dict):
    print(f"    MRR                   KSh {rev['mrr']:,.2f}")
    print(f"    collected this month  KSh {rev['collected_this_month']:,.2f}")
    print(f"    outstanding           KSh {rev['outstanding_total']:,.2f}")
    print(f"    overdue               KSh {rev['overdue_total']:,.2f} ({rev['overdue_count']})")
    print(f"    active subs {rev['active_subscriptions']}, past due {rev['past_due_count']}")
    for plan, f in (rev["by_plan"] or {}).items():
        print(f"      {plan:11} billed {f['billed']:>10,.2f}  collected {f['collected']:>10,.2f}  outstanding {f['outstanding']:>10,.2f}")

print("\n=== unauthenticated blocked ===")
anon = Client()
show("anon plans", anon.get("/api/subscriptions/plans"), expect=401)
show("anon current", anon.get("/api/subscriptions/current"), expect=401)