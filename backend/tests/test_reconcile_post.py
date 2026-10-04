import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from settings import GV_EMAIL, GV_PASSWORD, ACME_EMAIL, ACME_PASSWORD, PLATFORM_EMAIL, PLATFORM_PASSWORD

"""Posts the same statement twice to prove duplicate protection works via multipart."""
import http.cookiejar
import json
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
BOUNDARY = "----SchoolPay99"


def client():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    req = urllib.request.Request(
        f"{BASE}/api/auth/login",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    op.open(req, timeout=15).read()
    return op


def multipart(op, csv_text: str, post_matched: bool):
    parts = []
    parts.append(
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="paybill.csv"\r\n'
        f'Content-Type: text/csv\r\n\r\n{csv_text}\r\n'
    )
    parts.append(
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="post_matched"\r\n\r\n'
        f'{"true" if post_matched else "false"}\r\n'
    )
    parts.append(f"--{BOUNDARY}--\r\n")
    body = "".join(parts).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE}/api/mpesa/reconcile-file",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        method="POST",
    )
    with op.open(req, timeout=30) as response:
        return json.loads(response.read().decode())


def count_payments(op):
    req = urllib.request.Request(f"{BASE}/payments")
    with op.open(req, timeout=15) as response:
        return len(json.loads(response.read().decode()))


op = client()
ref = f"MPX-34-{uuid.uuid4().hex[:5]}"
csv_text = f"receipt,amount,payer_name,phone,date\n{ref},3200,Some Parent,,2026-10-04\n"

before = count_payments(op)
print(f"payments before: {before}\n")

print("DRY RUN (post_matched=false)")
dry = multipart(op, csv_text, False)
print(f"  {dry['message']}")
for r in dry["rows"]:
    print(f"    {r['match_status']:9} posted={r['posted']}  {r['reason']}")
after_dry = count_payments(op)
print(f"  payments after dry run: {after_dry}  {'OK unchanged' if after_dry == before else '!! CHANGED'}\n")

print("POST (post_matched=true) - run 1")
first = multipart(op, csv_text, True)
print(f"  {first['message']}")
for r in first["rows"]:
    print(f"    {r['match_status']:9} posted={r['posted']}  {r['reason']}")
after_first = count_payments(op)
print(f"  payments now: {after_first}\n")

print("POST the SAME statement again - run 2 (must not double-post)")
second = multipart(op, csv_text, True)
print(f"  {second['message']}")
for r in second["rows"]:
    print(f"    {r['match_status']:9} posted={r['posted']}  {r['reason']}")
after_second = count_payments(op)
print(f"  payments now: {after_second}  {'OK unchanged' if after_second == after_first else '!! DUPLICATED'}")