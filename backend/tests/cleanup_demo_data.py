import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Remove test/demo data created during development."""
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
        req = urllib.request.Request(
            f"{BASE}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw

    def get(self, p):
        return self.request("GET", p)

    def post(self, p, b=None):
        return self.request("POST", p, b)

    def delete(self, p):
        return self.request("DELETE", p)


def login(email, password):
    c = Client()
    status, body = c.post("/api/auth/login", {"email": email, "password": password})
    if status != 200:
        print(f"  could not log in as {email}: {status} {body}")
    return c


gv = login(GV_EMAIL, GV_PASSWORD)

# Remove students created by the import/regression tests.
status, students = gv.get("/students")
removed = 0
for s in students or []:
    if s["admission_number"].startswith(("IMP-", "E2E-", "ACME-", "T-")):
        gv.delete(f"/students/{s['id']}")
        print(f"  removed student {s['admission_number']}")
        removed += 1
print(f"Green Valley: removed {removed} test student(s)")

# Remove test payments.
status, payments = gv.get("/payments")
for p in payments or []:
    ref = p.get("transaction_reference") or ""
    if ref.startswith(("E2E-", "SHOT-", "IMP-")):
        gv.delete(f"/payments/{p['id']}")
        print(f"  removed payment {ref}")

# Acme keeps its school (it demonstrates two tenants) but loses the test student.
ac = login(ACME_EMAIL, ACME_PASSWORD)
status, ac_students = ac.get("/students")
for s in ac_students or []:
    if s["admission_number"].startswith("ACME-"):
        ac.delete(f"/students/{s['id']}")
        print(f"  removed Acme student {s['admission_number']}")

print("\nFinal state:")
for label, client in (("Green Valley", gv), ("Acme", ac)):
    _, students = client.get("/students")
    _, payments = client.get("/payments")
    print(f"  {label}: {len(students or [])} students, {len(payments or [])} payments")
    for s in students or []:
        print(f"      {s['admission_number']}  {s['first_name']} {s['last_name']}  ({s['class_name']})")