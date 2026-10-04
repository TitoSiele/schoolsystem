"""
Shared credentials for the scripts in this folder.

These are DEVELOPMENT accounts for a local database only. They are read from
the environment so that no real password is stored in the repository.

    set SCHOOLPAY_TEST_PASSWORD=...
    set PLATFORM_ADMIN_PASSWORD=...

If they are not set the scripts fall back to placeholders that will not match
any real account, so a copied repository cannot be used to sign in anywhere.
"""

import os

BASE = os.getenv("SCHOOLPAY_BASE_URL", "http://127.0.0.1:8000")

# Local development school
GV_EMAIL = "admin@greenvalley.sc.ke"
GV_PASSWORD = os.getenv("SCHOOLPAY_TEST_PASSWORD", "local-dev-only")

# Second tenant used to prove isolation
ACME_EMAIL = "admin@acme.sc.ke"
ACME_PASSWORD = os.getenv("SCHOOLPAY_TEST_ACME_PASSWORD", "local-dev-only")

# Platform operator
PLATFORM_EMAIL = "admin@schoolpay.app"
PLATFORM_PASSWORD = os.getenv("PLATFORM_ADMIN_PASSWORD", "local-dev-only")
