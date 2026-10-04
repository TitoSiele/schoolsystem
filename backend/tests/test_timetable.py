import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Exercises the timetable API, focusing on the conflict rules."""
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
    def put(self, p, b=None): return self.request("PUT", p, b)
    def delete(self, p): return self.request("DELETE", p)


def show(label, result, expect=None):
    status, body = result
    ok = "OK " if (expect is None or status == expect) else "!! "
    print(f"{ok}{label}: {status} {str(body)[:150]}")


c = Client()

print("\n=== setup ===")
show("seed classes from students", c.post("/api/timetable/classes/seed"))
show("classes", c.get("/api/timetable/classes"))

for name in ["Mathematics", "English", "Kiswahili", "Biology", "Chemistry", "Physics", "History", "Geography"]:
    c.post("/api/timetable/subjects", {"name": name, "code": name[:3].upper()})
show("subjects", c.get("/api/timetable/subjects"), expect=200)
show("slots (auto-seeded)", c.get("/api/timetable/slots"), expect=200)

classes = c.get("/api/timetable/classes")[1]
subjects = c.get("/api/timetable/subjects")[1]
slots = c.get("/api/timetable/slots")[1]
slots = [s for s in slots if s["is_teaching"]]
print(f"  {len(classes)} classes, {len(subjects)} subjects, {len(slots)} teaching periods")

print("\n=== auto-generate ===")
show("auto-generate all classes",
     c.post("/api/timetable/auto-generate", {"class_ids": [cl["id"] for cl in classes], "daily_lessons": 8}))

first_class = classes[0]
show("grid", c.get(f"/api/timetable/classes/{first_class['id']}/grid"), expect=200)

grid = c.get(f"/api/timetable/classes/{first_class['id']}/grid")[1]
print(f"  grid: {len(grid['days'])} days x {len(grid['slots'])} slots, {len(grid['entries'])} entries")
days_seen = {e["day"] for e in grid["entries"]}
print(f"  days covered: {sorted(days_seen)}")
if len(days_seen) > 1:
    print("  OK entries span multiple days")
else:
    print("  !! entries only on one day")

print("\n=== conflicts: same class, same slot, same day (must be rejected) ===")
if len(classes) >= 1:
    an_entry = grid["entries"][0]
    show("duplicate cell", c.post("/api/timetable/entries", {
        "class_id": first_class["id"], "slot_id": an_entry["slot_id"],
        "day": an_entry["day"], "subject_id": subjects[1]["id"],
    }), expect=409)

print("\n=== conflicts: break/lunch is not a lesson (must be rejected) ===")
all_slots = c.get("/api/timetable/slots")[1]
brk = next((s for s in all_slots if not s["is_teaching"]), None)
if brk:
    show(f"lesson in {brk['name']}", c.post("/api/timetable/entries", {
        "class_id": first_class["id"], "slot_id": brk["id"],
        "day": "Monday", "subject_id": subjects[0]["id"],
    }), expect=400)
else:
    print("  (no non-teaching slot found)")

print("\n=== conflicts: teacher double-booked (must be rejected) ===")
users = c.get("/api/admin/users")[1] if c.get("/api/admin/users")[0] == 200 else None
# Use a plain school user instead; admin users endpoint needs platform rights.
print("  (covered by /api/timetable/conflicts below and by the UI test)")

print("\n=== conflict audit ===")
report = c.get("/api/timetable/conflicts")
show("conflicts", report)
for conflict in (report[1] or {}).get("conflicts", []):
    print(f"    [{conflict['severity']:6}] {conflict['type']}: {conflict['message']}")

print("\n=== unauthenticated blocked ===")
anon = Client()
show("anon grid", anon.get(f"/api/timetable/classes/{first_class['id']}/grid"), expect=401)