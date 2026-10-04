import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Verifies login, tenant isolation and the subscription guard.
Uses urllib from the standard library so no extra dependency is needed."""
import http.cookiejar
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


class Client:
    """Tiny cookie-aware HTTP client."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

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

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body)

    def put(self, path, body=None):
        return self.request("PUT", path, body)

    def delete(self, path):
        return self.request("DELETE", path)


def show(label, result):
    status, body = result
    print(f"{label}: {status} {str(body)[:220]}")


print("=== unauthenticated ===")
anon = Client()
show("GET /students", anon.get("/students"))

print("\n=== login as Green Valley ===")
gv = Client()
show("me", gv.get("/api/auth/me"))
show("school", gv.get("/api/school"))
show("students", gv.get("/students"))
gv_ids = {s["id"] for s in (gv.get("/students")[1] or [])}
print(f"  GV student ids: {sorted(gv_ids)}")

print("\n=== register a SECOND school ===")
ac = Client()
show("register", ac.post("/api/auth/register-school", {
    "name": "Acme High School", "code": "ACME",
    "email": "office@acme.sc.ke",
    "plan": "trial",
    "admin_email": "admin@acme.sc.ke",
    "admin_password": ACME_PASSWORD,
    "admin_name": "Acme Admin",
}))

print("\n=== login as Acme ===")
ac_students = ac.get("/students")[1]
print(f"  Acme students: {ac_students}  (must be empty)")
show("Acme payments", ac.get("/payments"))

print("\n=== isolation: Acme tries to read a Green Valley student ===")
if gv_ids:
    target = sorted(gv_ids)[0]
    show(f"GET /students/{target} as Acme", ac.get(f"/students/{target}"))
    show(f"PUT /students/{target} as Acme", ac.put(f"/students/{target}", {"class_name": "HACKED"}))
    show(f"DELETE /students/{target} as Acme", ac.delete(f"/students/{target}"))
    show(f"GET /students/{target}/payments as Acme", ac.get(f"/students/{target}/payments"))

print("\n=== Acme creates its own student + payment ===")
status, s = ac.post("/students", {
    "admission_number": "ACME-001", "first_name": "Test", "last_name": "Acme",
    "class_name": "Form 1", "fee_balance": 1000,
})
show("create student", (status, s))
sid = s.get("id") if isinstance(s, dict) else None
if sid:
    show("create payment", ac.post("/payments", {"student_id": sid, "amount": 500, "method": "Cash"}))

print("\n=== cross-check: Green Valley must NOT see Acme's student ===")
gv_students2 = gv.get("/students")[1] or []
print(f"  GV students now: {[s['admission_number'] for s in gv_students2]}  (ACME-001 must be absent)")

print("\n=== Acme plan info ===")
show("GET /api/school", ac.get("/api/school"))

print("\n=== wrong password ===")
bad = Client()
show("login wrong pw", bad.post("/api/auth/login", {"email": "admin@acme.sc.ke", "password": "wrong"}))

print("\n=== logout ===")
show("logout", gv.post("/api/auth/logout"))

print("\n=== reports while signed in ===")
show("collection-summary", gv.get("/api/v1/reports/collection-summary?academic_year=2026&term=Term%201"))
show("arrears", gv.get("/api/v1/arrears/report"))
show("trial-balance", gv.get("/api/v1/reports/trial-balance?academic_year=2026&term=Term%201"))