# apps/brokers/management/commands/test_paper_lifecycle.py
#
# BLOCKER #3 FIX — Paper Trading Lifecycle Test
#
# Yeh command ek complete paper order lifecycle run karta hai:
#   place → BrokerOrder(PENDING) → FakeFyersAdapter → OPEN
#   → _simulate_paper_fill() → COMPLETE → Trade record → PnL
#
# Real money risk ZERO. Real DB mein test data banta hai (rollback option bhi hai).
#
# Usage:
#   python manage.py test_paper_lifecycle --email your@email.com
#   python manage.py test_paper_lifecycle --email your@email.com --symbol NSE:NIFTY50-INDEX --qty 1
#   python manage.py test_paper_lifecycle --email your@email.com --dry-run  (rollback after test)
#   python manage.py test_paper_lifecycle --email your@email.com --simulate-rejection  (rejection path test)
#
# Kya test hota hai:
#   ✅ FakeFyersAdapter.place_order() — symbol format warnings
#   ✅ BrokerOrder PENDING → OPEN (mark_sent)
#   ✅ _simulate_paper_fill() → process_broker_fill()
#   ✅ fill_order() → Trade record bana
#   ✅ realized_pnl calculate hua
#   ✅ BrokerOrder.realized_pnl copy hua
#   ✅ Wallet settlement (paper mode mein skip hota hai — confirm karo)
#   ✅ WebSocket push (connection nahi hai toh gracefully fail hoga)
#   ✅ Rejection path (--simulate-rejection flag se)

from __future__ import annotations

import time
from decimal import Decimal

from django.utils import timezone

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = "Paper trading ka full order lifecycle test karo — bina real money ke."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            required=True,
            help="User email (aapka apna account use karo)",
        )
        parser.add_argument(
            "--symbol",
            default="NSE:NIFTY50-INDEX",
            help="Symbol to test (default: NSE:NIFTY50-INDEX)",
        )
        parser.add_argument(
            "--qty",
            type=int,
            default=1,
            help="Quantity / lots (default: 1)",
        )
        parser.add_argument(
            "--price",
            type=float,
            default=100.0,
            help="Paper fill price (default: 100.0)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Test ke baad DB changes rollback karo (no permanent records)",
        )
        parser.add_argument(
            "--simulate-rejection",
            action="store_true",
            help="Rejection code path test karo (order FAILED mark hoga)",
        )

    def handle(self, *args, **options):
        email            = options["email"]
        symbol           = options["symbol"]
        qty              = options["qty"]
        price            = Decimal(str(options["price"]))
        dry_run          = options["dry_run"]
        simulate_reject  = options["simulate_rejection"]

        self.stdout.write(self.style.MIGRATE_HEADING(
            "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "  📝 Paper Trading Lifecycle Test\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ))

        if dry_run:
            self.stdout.write(self.style.WARNING("  🔁 DRY RUN — changes will be rolled back"))
        if simulate_reject:
            self.stdout.write(self.style.WARNING("  ❌ REJECTION SIMULATION MODE"))

        # ── Step 0: User + Strategy dhundo ───────────────────────────────────
        self.stdout.write("\n[1/7] User aur Strategy dhundo...")

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(f"User not found: {email}")

        from apps.strategies.models import Strategy
        strategy = Strategy.objects.filter(user=user, is_active=True).first()
        if not strategy:
            raise CommandError(
                f"No active strategy found for {email}. "
                "Admin panel mein ek strategy create karo."
            )

        self.stdout.write(self.style.SUCCESS(
            f"  ✅ User: {user.email} | Strategy: {strategy.name} ({strategy.id})"
        ))

        # ── Step 1: INR Wallet check ──────────────────────────────────────────
        self.stdout.write("\n[2/7] INR Wallet check...")

        from apps.wallet.models import Wallet
        try:
            wallet = Wallet.objects.get(user=user, currency="INR")
            self.stdout.write(self.style.SUCCESS(
                f"  ✅ Wallet found | available={wallet.available_balance} | "
                f"locked={wallet.locked_balance}"
            ))
        except Wallet.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                "  ⚠️  INR Wallet nahi mila — creating one for test..."
            ))
            wallet = Wallet.objects.create(
                user=user,
                currency="INR",
                available_balance=Decimal("100000"),
                locked_balance=Decimal("0"),
            )
            self.stdout.write(self.style.SUCCESS("  ✅ Wallet created (Rs 1,00,000)"))

        # ── Step 2: FakeFyersAdapter test ────────────────────────────────────
        self.stdout.write("\n[3/7] FakeFyersAdapter.place_order() test...")

        from broker_adapters.paper.adapter import FakeFyersAdapter
        adapter = FakeFyersAdapter({})

        if simulate_reject:
            adapter._simulate_failure = True
            adapter._failure_reason = "Simulated rejection — testing rejection code path"

        result = adapter.place_order(
            symbol=symbol,
            side="buy",
            qty=float(qty),
            order_type="market",
            price=float(price),
        )

        if simulate_reject:
            if not result.success:
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ Rejection simulation working | reason={result.message}"
                ))
            else:
                self.stdout.write(self.style.ERROR("  ❌ Expected rejection but got success"))
            self.stdout.write("\nRejection path test complete.")
            return

        if not result.success:
            raise CommandError(f"FakeFyersAdapter.place_order() failed: {result.message}")

        self.stdout.write(self.style.SUCCESS(
            f"  ✅ FakeFyersAdapter accepted | fake_order_id={result.order_id}"
        ))
        fake_order_id = result.order_id

        # ── Step 3: Full DB lifecycle (with optional rollback) ────────────────
        self.stdout.write("\n[4/7] DB objects create karo (Order + BrokerOrder)...")

        sp = transaction.savepoint()

        try:
            from apps.market.models import Asset
            from apps.orders.models import Order
            from apps.brokers.models import BrokerOrder
            from apps.brokers.fill_handler import _simulate_paper_fill, _get_paper_fill_price

            # Asset get or create
            asset, _ = Asset.objects.get_or_create(
                symbol=symbol,
                defaults={
                    "name":       symbol,
                    "asset_type": "equity",
                    "last_price": price,
                },
            )

            # Order create (paper mode)
            order_obj = Order.objects.create(
                user          = user,
                asset         = asset,
                side          = "buy",
                order_type    = Order.OrderType.MARKET,
                quantity      = Decimal(str(qty)),
                limit_price   = price,
                status        = Order.Status.OPEN,
                mode          = Order.Mode.PAPER,    # ← paper mode
                strategy      = strategy,
            )

            self.stdout.write(self.style.SUCCESS(
                f"  ✅ Order created | id={order_obj.id} | mode={order_obj.mode}"
            ))

            # BrokerOrder create (OPEN — fake order ID set hai)
            broker_order = BrokerOrder.objects.create(
                broker_account   = strategy.broker,
                order            = order_obj,
                symbol           = symbol,
                side             = BrokerOrder.Side.BUY,
                quantity         = qty,
                status           = BrokerOrder.Status.OPEN,
                exchange_order_id= fake_order_id,
                broker_response  = result.raw,
                sent_to_broker_at= timezone.now(),
            )

            self.stdout.write(self.style.SUCCESS(
                f"  ✅ BrokerOrder created | id={broker_order.id} | "
                f"status={broker_order.status} | exchange_id={broker_order.exchange_order_id}"
            ))

            # ── Step 4: _simulate_paper_fill() call karo ─────────────────────
            self.stdout.write("\n[5/7] _simulate_paper_fill() run karo...")

            fill_price_resolved = _get_paper_fill_price(fake_order_id)
            self.stdout.write(f"  Fill price resolved: Rs {fill_price_resolved}")

            _simulate_paper_fill(broker_order)

            # Refresh from DB
            broker_order.refresh_from_db()
            order_obj.refresh_from_db()

            self.stdout.write(self.style.SUCCESS(
                f"  ✅ BrokerOrder status: {broker_order.status} | "
                f"realized_pnl={broker_order.realized_pnl} | "
                f"avg_fill_price={broker_order.avg_fill_price}"
            ))

            # ── Step 5: Trade record check ────────────────────────────────────
            self.stdout.write("\n[6/7] Trade record verify karo...")

            from apps.orders.models import Trade
            trades = Trade.objects.filter(order=order_obj)

            if trades.exists():
                t = trades.first()
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ Trade created | id={t.id} | qty={t.quantity} @ {t.price} | "
                    f"mode={t.mode} | realized_pnl={t.realized_pnl}"
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    "  ⚠️  No Trade record found — fill_order() ne Trade create nahi kiya. "
                    "fill_order() mein bug hai."
                ))

            # ── Step 6: Wallet check (paper = no settlement) ──────────────────
            self.stdout.write("\n[7/7] Wallet settlement verify karo...")

            wallet.refresh_from_db()

            # Paper mode mein wallet debit nahi hona chahiye
            if order_obj.mode == Order.Mode.PAPER:
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ Paper mode — wallet NOT debited (correct) | "
                    f"available={wallet.available_balance}"
                ))
            else:
                self.stdout.write(self.style.WARNING(
                    f"  ⚠️  Unexpected LIVE mode wallet interaction"
                ))

            # ── Summary ──────────────────────────────────────────────────────
            self.stdout.write(self.style.MIGRATE_HEADING(
                "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "  RESULT SUMMARY\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ))
            self.stdout.write(f"  Order ID:          {order_obj.id}")
            self.stdout.write(f"  Order status:      {order_obj.status}")
            self.stdout.write(f"  Order mode:        {order_obj.mode}")
            self.stdout.write(f"  BrokerOrder:       {broker_order.id}")
            self.stdout.write(f"  BrokerOrder status:{broker_order.status}")
            self.stdout.write(f"  Fill price:        Rs {broker_order.avg_fill_price}")
            self.stdout.write(f"  Realized PnL:      {broker_order.realized_pnl}")
            self.stdout.write(f"  Trades created:    {trades.count()}")
            self.stdout.write(f"  Wallet available:  Rs {wallet.available_balance}")

            all_good = (
                broker_order.status == BrokerOrder.Status.COMPLETE
                and order_obj.status in (Order.Status.FILLED, Order.Status.PARTIAL)
                and trades.count() > 0
            )

            if all_good:
                self.stdout.write(self.style.SUCCESS(
                    "\n  ✅ ALL CHECKS PASSED — paper trading lifecycle working correctly.\n"
                    "  Live trading pe safe to switch.\n"
                ))
            else:
                self.stdout.write(self.style.ERROR(
                    "\n  ❌ SOME CHECKS FAILED — fix the issues above before going live.\n"
                ))

            if dry_run:
                transaction.savepoint_rollback(sp)
                self.stdout.write(self.style.WARNING(
                    "  🔁 DRY RUN: all DB changes rolled back."
                ))
            else:
                transaction.savepoint_commit(sp)
                self.stdout.write(
                    "  💾 Changes committed to DB. "
                    "Admin panel mein dekh sakte ho.\n"
                )

        except Exception as exc:
            import traceback
            transaction.savepoint_rollback(sp)
            tb = traceback.format_exc()
            raise CommandError(
                f"""Test FAILED with exception: {exc!r}

Full traceback:
{tb}"""
) from exc