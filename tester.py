# tester.py
# Run: python tester.py
# ─────────────────────────────────────────────────────────────

import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings
settings.ALLOWED_HOSTS.append('testserver')


from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()

# ─── Colors for terminal output ──────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):  print(f"{GREEN}  ✅ {msg}{RESET}")
def err(msg): print(f"{RED}  ❌ {msg}{RESET}")
def info(msg): print(f"{CYAN}  ℹ  {msg}{RESET}")
def head(msg): print(f"\n{BOLD}{YELLOW}{'─'*50}\n  {msg}\n{'─'*50}{RESET}")


def check(label, resp, expected_status=200, check_keys=None):
    """Response validate karo aur result print karo."""
    data = resp.json() if hasattr(resp, 'json') else {}
    passed = resp.status_code == expected_status

    if passed:
        ok(f"{label} → {resp.status_code}")
    else:
        err(f"{label} → got {resp.status_code}, expected {expected_status}")
        err(f"    Response: {data}")

    if check_keys:
        for key in check_keys:
            if key in data:
                ok(f"  Key '{key}' present: {data[key]}")
            else:
                err(f"  Key '{key}' MISSING in response")

    return data, passed


# ═══════════════════════════════════════════════════════════════
#  MAIN TEST RUNNER
# ═══════════════════════════════════════════════════════════════

def run_all_tests():
    client = APIClient()

    # ── Auth ──────────────────────────────────────────────────
    head("SETUP: User authenticate karo")
    user, created = User.objects.get_or_create(
        email="satishdadarwal@gmail.com",
        defaults={"username": "testuser"},
    )
    if created:
        user.set_password("testpass123")
        user.save()
        info("Naya user banaya")
    else:
        info("Existing user mila")

    client.force_authenticate(user=user)
    ok(f"Authenticated as: {user.email}")

    session_id = None

    # ─────────────────────────────────────────────────────────
    #  TEST 1: Session Start — AUTO mode
    # ─────────────────────────────────────────────────────────
    head("TEST 1: Session Start (AUTO mode)")
    resp = client.post("/api/live-trading/session/start/", {
        "strategy_id": "breakout_alpha_001",
        "mode": "auto",
    }, format="json")
    data, ok1 = check("Start Session AUTO", resp, expected_status=200,
                      check_keys=["session_id", "mode", "started_at"])
    if ok1:
        session_id = data.get("session_id")
        info(f"session_id = {session_id}")

    # ─────────────────────────────────────────────────────────
    #  TEST 2: Session Start — SEMI_AUTO mode (duplicate — pehli band hogi)
    # ─────────────────────────────────────────────────────────
    head("TEST 2: Session Start (SEMI_AUTO mode — pehli auto-close hogi)")
    resp = client.post("/api/live-trading/session/start/", {
        "strategy_id": "ict_swing_002",
        "mode": "semi_auto",
    }, format="json")
    data, ok2 = check("Start Session SEMI_AUTO", resp, expected_status=200,
                      check_keys=["session_id", "mode"])
    if ok2:
        session_id = data.get("session_id")
        info(f"New session_id = {session_id}")

    # ─────────────────────────────────────────────────────────
    #  TEST 3: Session Start — Invalid mode
    # ─────────────────────────────────────────────────────────
    head("TEST 3: Session Start — Invalid mode (error expected)")
    resp = client.post("/api/live-trading/session/start/", {
        "strategy_id": "test_strategy",
        "mode": "invalid_xyz",
    }, format="json")
    check("Invalid Mode Rejected", resp, expected_status=400,
          check_keys=["error"])

    # ─────────────────────────────────────────────────────────
    #  TEST 4: Activity Log
    # ─────────────────────────────────────────────────────────
    head("TEST 4: Activity Log")
    resp = client.get(f"/api/live-trading/activity/?session_id={session_id}")
    check("Activity Log Fetch", resp, expected_status=200,
          check_keys=["activity"])

    # ─────────────────────────────────────────────────────────
    #  TEST 5: Manual Order Placement
    # ─────────────────────────────────────────────────────────
    head("TEST 5: Manual Order (MANUAL mode)")
    resp = client.post("/api/live-trading/manual-order/", {
        "session_id":  session_id,
        "symbol":      "NIFTY50",
        "direction":   "buy",
        "order_type":  "MARKET",
        "lots":        1,
        "price":       19500.0,
        "stop_loss":   19400.0,
        "take_profit": 19700.0,
    }, format="json")
    check("Manual Order Place", resp, expected_status=201,
          check_keys=["manual_order_id", "rr_ratio", "margin_required", "message"])

    # ─────────────────────────────────────────────────────────
    #  TEST 6: Manual Order — No active session (error expected)
    # ─────────────────────────────────────────────────────────
    head("TEST 6: Manual Order — Invalid session_id (error expected)")
    resp = client.post("/api/live-trading/manual-order/", {
        "session_id": 99999,
        "symbol":     "BANKNIFTY",
        "direction":  "sell",
        "lots":       2,
    }, format="json")
    check("Invalid Session Rejected", resp, expected_status=404,
          check_keys=["error"])

    # ─────────────────────────────────────────────────────────
    #  TEST 7: Signal Confirm — non-existent signal (error expected)
    # ─────────────────────────────────────────────────────────
    head("TEST 7: Signal Confirm — non-existent signal (404 expected)")
    resp = client.post("/api/live-trading/signals/99999/confirm/", {}, format="json")
    check("Signal Confirm 404", resp, expected_status=404,
          check_keys=["error"])

    # ─────────────────────────────────────────────────────────
    #  TEST 8: Signal Ignore — non-existent signal (error expected)
    # ─────────────────────────────────────────────────────────
    head("TEST 8: Signal Ignore — non-existent signal (404 expected)")
    resp = client.post("/api/live-trading/signals/99999/ignore/", {}, format="json")
    check("Signal Ignore 404", resp, expected_status=404,
          check_keys=["error"])

    # ─────────────────────────────────────────────────────────
    #  TEST 9: Session Summary
    # ─────────────────────────────────────────────────────────
    head("TEST 9: Session Summary")
    if session_id:
        resp = client.get(f"/api/live-trading/session/{session_id}/summary/")
        check("Session Summary Fetch", resp, expected_status=200,
              check_keys=["session_id", "strategy_id", "summary", "activity_log"])
    else:
        err("session_id nahi mila — summary test skip")

    # ─────────────────────────────────────────────────────────
    #  TEST 10: Session Stop
    # ─────────────────────────────────────────────────────────
    head("TEST 10: Session Stop")
    if session_id:
        resp = client.post("/api/live-trading/session/stop/", {
            "session_id": session_id,
        }, format="json")
        check("Stop Session", resp, expected_status=200,
              check_keys=["message"])
    else:
        err("session_id nahi mila — stop test skip")

    # ─────────────────────────────────────────────────────────
    #  TEST 11: Stop already-stopped session (404 expected)
    # ─────────────────────────────────────────────────────────
    head("TEST 11: Stop already-stopped session (404 expected)")
    if session_id:
        resp = client.post("/api/live-trading/session/stop/", {
            "session_id": session_id,
        }, format="json")
        check("Double Stop Rejected", resp, expected_status=404,
              check_keys=["error"])

    # ─────────────────────────────────────────────────────────
    #  SUMMARY
    # ─────────────────────────────────────────────────────────
    print(f"\n{BOLD}{GREEN}{'═'*50}")
    print("  ALL TESTS COMPLETE")
    print(f"{'═'*50}{RESET}\n")


if __name__ == "__main__":
    run_all_tests()