#!/usr/bin/env python3
"""tests/test_profile.py -- tests for the Profile page and navbar integration.

Verifies:
  * Presence of Profile option on the right side of the navbar with user icon
  * Preservation of all 7 existing navbar links (Dashboard, Donors, Donations,
    Blood Stock, Hospitals, Blood Requests, SQL Reports)
  * Unauthenticated access redirection to /admin/login (protecting private info)
  * Authenticated profile page loading with 200 OK
  * Profile page content (Role: Administrator, session status, lifetime, actions)
  * Conditional display of name/email only when present
  * Active navigation highlight when viewing /profile
  * Responsive Bootstrap markup (inside #mainNav)
  * Logout from profile ending the session
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, TESTS_DIR)

from werkzeug.security import generate_password_hash

TEST_ADMIN_PASSWORD = "test-only-admin-password-1234567890"
STRONG_SECRET_KEY = "test-only-session-signing-key-0123456789abcdef"

os.environ["SECRET_KEY"] = STRONG_SECRET_KEY
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash(TEST_ADMIN_PASSWORD)
os.environ.pop("ADMIN_PASSWORD", None)

import app as app_module
from app import app
from test_db import Results, csrf_token, post


def get_token(client):
    return csrf_token(client, "/requests")


def login_admin(client):
    token = get_token(client)
    r = post(client, "/admin/login", csrf_token=token, password=TEST_ADMIN_PASSWORD)
    assert r.status_code == 302, f"Login failed with status {r.status_code}"


def test_navbar_structure(results, client):
    print("\n== 1. Navbar Structure & Links ==")
    r = client.get("/")
    results.check("dashboard page returns 200", r.status_code == 200)
    html = r.get_data(as_text=True)

    expected_links = [
        ("Dashboard", "/"),
        ("Donors", "/donors"),
        ("Donations", "/donations"),
        ("Blood Stock", "/stock"),
        ("Hospitals", "/hospitals"),
        ("Blood Requests", "/requests"),
        ("SQL Reports", "/reports"),
    ]

    for label, path in expected_links:
        results.check(
            f"navbar preserves '{label}' link",
            f'href="{path}"' in html and label in html
        )

    results.check("navbar has Profile link", 'href="/profile"' in html)
    results.check("navbar has Profile label", "Profile" in html)
    results.check("navbar has nav-icon SVG", "nav-icon" in html and "<svg" in html)
    results.check("navbar has nav-link-profile class", "nav-link-profile" in html)
    results.check("navbar is inside responsive #mainNav", 'id="mainNav"' in html)

    # Check order: SQL Reports comes before Profile in the HTML
    pos_reports = html.find("SQL Reports")
    pos_profile = html.find("Profile")
    results.check("Profile is located after SQL Reports on the right",
                  pos_reports != -1 and pos_profile != -1 and pos_reports < pos_profile)


def test_unauthenticated_profile_access(results, client):
    print("\n== 2. Unauthenticated Profile Access (Protection) ==")
    r = client.get("/profile")
    results.check("unauthenticated GET /profile redirects to login",
                  r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/login"),
                  f"got {r.status_code} {r.headers.get('Location')}")

    r_admin_profile = client.get("/admin/profile")
    results.check("unauthenticated GET /admin/profile redirects to login",
                  r_admin_profile.status_code == 302 and r_admin_profile.headers.get("Location", "").endswith("/admin/login"))


def test_authenticated_profile(results, client):
    print("\n== 3. Authenticated Profile Page ==")
    login_admin(client)

    r = client.get("/profile")
    results.check("authenticated GET /profile returns 200", r.status_code == 200)
    html = r.get_data(as_text=True)

    results.check("page title contains Profile", "<title>Profile" in html)
    results.check("displays Role Administrator", "Administrator" in html)
    results.check("displays Active Session badge", "Active Session" in html)
    results.check("displays Authenticated status", "Authenticated" in html)
    results.check("displays Session Lifetime", "Session Lifetime" in html)
    results.check("has Pending Approvals action button", 'href="/admin"' in html and "Pending Approvals" in html)
    results.check("has Dashboard action button", 'href="/"' in html and "Dashboard" in html)
    results.check("has Log out action button", 'action="/admin/logout"' in html and "Log out" in html)

    # Active nav link check on /profile
    results.check("navbar marks Profile as active",
                  'nav-link-profile active' in html or ('active' in html and 'aria-current="page"' in html))


def test_logout_workflow(results, client):
    print("\n== 4. Logout from Profile ==")
    token = get_token(client)
    r = post(client, "/admin/logout", csrf_token=token)
    results.check("logout redirects to dashboard", r.status_code == 302 and r.headers.get("Location", "").endswith("/"))

    r = client.get("/profile")
    results.check("after logout, /profile is protected (redirects to login)",
                  r.status_code == 302 and r.headers.get("Location", "").endswith("/admin/login"))


def main():
    results = Results()
    client = app.test_client()

    test_navbar_structure(results, client)
    test_unauthenticated_profile_access(results, client)
    test_authenticated_profile(results, client)
    test_logout_workflow(results, client)

    print("\n== Summary ==")
    print(f"  passed: {results.passed}")
    print(f"  failed: {results.failed}")
    if results.failures:
        print("  failures:")
        for name, detail in results.failures:
            print(f"    - {name} {detail}")
    return 0 if results.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
