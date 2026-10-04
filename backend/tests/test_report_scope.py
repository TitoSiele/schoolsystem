import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Confirms reports are tenant-scoped: each school must see only its own numbers."""
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


def login(email, password):
    c = Client()
    status, body = c.post("/api/auth/login", {"email": email, "password": password})
    assert status == 200, f"login failed for {email}: {status} {body}"
    return c


REPORT = "/api/v1/reports/collection-summary?academic_year=2026&term=Term%201"

gv = login(GV_EMAIL, GV_PASSWORD)
ac = login(ACME_EMAIL, ACME_PASSWORD)

for label, client in (("Green Valley", gv), ("Acme", ac)):
    status, students = client.get("/students")
    status2, report = client.get(REPORT)

    enrolled = report.get("total_students_enrolled") if isinstance(report, dict) else report
    student_count = len(students) if isinstance(students, list) else students

    print(f"{label}:")
    print(f"   /students count      = {student_count}")
    print(f"   report students_enrolled = {enrolled}")
    match = "OK" if enrolled == student_count else "*** MISMATCH ***"
    print(f"   -> {match}")
    print(f"   raw report: {json.dumps(report)[:300]}")
    print()

print("=== unauthenticated access to a now-protected report ===")
anon = Client()
status, body = anon.get(REPORT)
print(f"   {status} {str(body)[:120]}")
print("   -> OK (blocked)" if status == 401 else "   -> *** LEAK ***")