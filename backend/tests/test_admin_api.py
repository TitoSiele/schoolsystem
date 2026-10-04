import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Verifies the platform-admin API and, critically, that school admins are blocked."""
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

    def put(self, p, b=None):
        return self.request("PUT", p, b)

    def delete(self, p):
        return self.request("DELETE", p)


def login(email, password):
    c = Client()
    return c, c.post("/api/auth/login", {"email": email, "password": password})


def line(label, result):
    status, body = result
    flag = "" if status < 400 else "   <-- expected error" if status in (400, 401, 403, 404) else "   *** UNEXPECTED ***"
    print(f"  {label}: {status} {str(body)[:150]}{flag}")


print("=== school admin MUST be blocked from the admin API ===")
gv, status = login(GV_EMAIL, GV_PASSWORD)
print(f"  school login: {status}")
for path in ("/api/admin/stats", "/api/admin/schools", "/api/admin/users",
             "/api/admin/students", "/api/admin/payments"):
    line(f"GET {path}", gv.get(path))

print("\n=== platform admin ===")
pa, status = login(PLATFORM_EMAIL, PLATFORM_PASSWORD)
print(f"  platform login: {status}")
line("GET /api/auth/me", pa.get("/api/auth/me"))

print("\n=== stats ===")
s, stats = pa.get("/api/admin/stats")
print(f"  {s} {json.dumps(stats, indent=2) if isinstance(stats, dict) else stats}")

print("\n=== schools (cross-tenant) ===")
s, schools = pa.get("/api/admin/schools")
print(f"  {s} {len(schools) if isinstance(schools, list) else schools} schools")
for sc in schools or []:
    print(f"    #{sc['id']} {sc['name']:22} {sc['plan']:8} {sc['subscription_status']:9} "
          f"{sc['student_count']}/{sc['max_students']} students")

print("\n=== students across all schools ===")
s, students = pa.get("/api/admin/students")
print(f"  {s} {len(students) if isinstance(students, list) else students} students")
for st in students or []:
    print(f"    #{st['id']} {st['school_name']:22} {st['admission_number']:12} {st['first_name']} {st['last_name']}")

print("\n=== payments across all schools ===")
s, payments = pa.get("/api/admin/payments")
print(f"  {s} {len(payments) if isinstance(payments, list) else payments} payments")
for p in payments or []:
    print(f"    #{p['id']} {p['school_name']:22} {p['amount']:>10} {p['method']}")

print("\n=== users across all schools ===")
s, users = pa.get("/api/admin/users")
print(f"  {s} {len(users) if isinstance(users, list) else users} users")
for u in users or []:
    print(f"    #{u['id']} {u['email']:32} role={u['role']:7} platform={str(u['is_platform_admin']):5} school={u['school_name']}")

print("\n=== CRUD: create a school ===")
s, created = pa.post("/api/admin/schools", {
    "name": "Riverside Academy", "code": "RIV",
    "email": "office@riverside.sc.ke", "plan": "starter",
    "admin_email": "admin@riverside.sc.ke",
    "admin_password": "test-only-password", "admin_name": "Riverside Admin",
})
print(f"  {s} {created}")
new_school_id = created.get("id") if isinstance(created, dict) else None

if new_school_id:
    print("\n=== CRUD: update that school (upgrade plan) ===")
    line("PUT plan=pro", pa.put(f"/api/admin/schools/{new_school_id}", {"plan": "pro"}))
    s, updated = pa.get(f"/api/admin/schools/{new_school_id}")
    print(f"    now: plan={updated['plan']} max_students={updated['max_students']}")

    print("\n=== CRUD: create a user in that school ===")
    s, nu = pa.post("/api/admin/users", {
        "email": "bursar@riverside.sc.ke", "password": "test-only-password",
        "full_name": "Riverside Bursar", "school_id": new_school_id, "role": "bursar",
    })
    print(f"  {s} {nu}")
    new_user_id = nu.get("id") if isinstance(nu, dict) else None

    print("\n=== CRUD: update that user ===")
    line("PUT role=admin", pa.put(f"/api/admin/users/{new_user_id}", {"role": "admin"}))

    print("\n=== guard: cannot demote yourself ===")
    line("PUT is_platform_admin=false on self", pa.put(f"/api/admin/users/{pa.get('/api/auth/me')[1]['id']}", {"is_platform_admin": False}))

    print("\n=== CRUD: delete that user, then the school ===")
    line("DELETE user", pa.delete(f"/api/admin/users/{new_user_id}"))
    line("DELETE school", pa.delete(f"/api/admin/schools/{new_school_id}"))

print("\n=== final cleanup check ===")
s, schools = pa.get("/api/admin/schools")
print(f"  {s} schools now: {[sc['name'] for sc in schools or []]}")