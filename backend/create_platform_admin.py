"""Create (or reset) the platform/system administrator account.

This account has no school attached and is the ONLY account allowed to see data
across every school. It is for you, not for customers.

Usage (from the backend/ folder):
    python create_platform_admin.py
"""
import getpass
import sys

from database import Base, SessionLocal, engine
from models import User
from auth import hash_password

EMAIL = "admin@schoolpay.app"


def main() -> int:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == EMAIL).first()

        if existing:
            password = getpass.getpass(f"New password for {EMAIL}: ")
            if len(password) < 8:
                print("Password must be at least 8 characters.")
                return 1
            existing.password_hash = hash_password(password)
            existing.is_platform_admin = True
            existing.is_active = True
            db.commit()
            print(f"Reset platform admin: {EMAIL}")
            return 0

        password = getpass.getpass(f"Password for {EMAIL}: ")
        if len(password) < 8:
            print("Password must be at least 8 characters.")
            return 1

        db.add(
            User(
                email=EMAIL,
                full_name="System Administrator",
                password_hash=hash_password(password),
                role="admin",
                is_platform_admin=True,
                school_id=None,  # platform admins belong to no school
            )
        )
        db.commit()
        print(f"Created platform admin: {EMAIL}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())