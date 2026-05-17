# apps/subscriptions/management/commands/seed_plans.py
#
#  Usage:
#    python manage.py seed_plans
#    python manage.py seed_plans --force   ← existing plans overwrite karo

from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.subscriptions.models import Plan

PLANS = [
    # ── FREE ─────────────────────────────────────────────────────
    {
        "name": "Free",
        "tier": Plan.Tier.FREE,
        "billing_cycle": Plan.BillingCycle.MONTHLY,
        "price_inr": Decimal("0.00"),
        "feature_limits": {
            "max_brokers": 0,
            "max_strategies": 0,
            "live_trading": False,
            "paper_trading": True,
            "backtest": False,
            "ai_signals": False,
        },
    },
    # ── BASIC Monthly ────────────────────────────────────────────
    {
        "name": "Basic",
        "tier": Plan.Tier.BASIC,
        "billing_cycle": Plan.BillingCycle.MONTHLY,
        "price_inr": Decimal("499.00"),
        "feature_limits": {
            "max_brokers": 1,
            "max_strategies": 2,
            "live_trading": False,
            "paper_trading": True,
            "backtest": True,
            "ai_signals": False,
        },
    },
    # ── BASIC Yearly (2 months free) ─────────────────────────────
    {
        "name": "Basic Annual",
        "tier": Plan.Tier.BASIC,
        "billing_cycle": Plan.BillingCycle.YEARLY,
        "price_inr": Decimal("4990.00"),
        "feature_limits": {
            "max_brokers": 1,
            "max_strategies": 2,
            "live_trading": False,
            "paper_trading": True,
            "backtest": True,
            "ai_signals": False,
        },
    },
    # ── PRO Monthly ──────────────────────────────────────────────
    {
        "name": "Pro",
        "tier": Plan.Tier.PRO,
        "billing_cycle": Plan.BillingCycle.MONTHLY,
        "price_inr": Decimal("1499.00"),
        "feature_limits": {
            "max_brokers": 3,
            "max_strategies": 10,
            "live_trading": True,
            "paper_trading": True,
            "backtest": True,
            "ai_signals": True,
        },
    },
    # ── PRO Yearly ───────────────────────────────────────────────
    {
        "name": "Pro Annual",
        "tier": Plan.Tier.PRO,
        "billing_cycle": Plan.BillingCycle.YEARLY,
        "price_inr": Decimal("14990.00"),
        "feature_limits": {
            "max_brokers": 3,
            "max_strategies": 10,
            "live_trading": True,
            "paper_trading": True,
            "backtest": True,
            "ai_signals": True,
        },
    },
    # ── ELITE Monthly ────────────────────────────────────────────
    {
        "name": "Elite",
        "tier": Plan.Tier.ELITE,
        "billing_cycle": Plan.BillingCycle.MONTHLY,
        "price_inr": Decimal("2999.00"),
        "feature_limits": {
            "max_brokers": -1,  # -1 = unlimited
            "max_strategies": -1,
            "live_trading": True,
            "paper_trading": True,
            "backtest": True,
            "ai_signals": True,
            "priority_support": True,
        },
    },
    # ── ELITE Yearly ─────────────────────────────────────────────
    {
        "name": "Elite Annual",
        "tier": Plan.Tier.ELITE,
        "billing_cycle": Plan.BillingCycle.YEARLY,
        "price_inr": Decimal("29990.00"),
        "feature_limits": {
            "max_brokers": -1,
            "max_strategies": -1,
            "live_trading": True,
            "paper_trading": True,
            "backtest": True,
            "ai_signals": True,
            "priority_support": True,
        },
    },
]


class Command(BaseCommand):
    help = "Seed default subscription plans into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite existing plans with same tier+billing_cycle.",
        )

    def handle(self, *args, **options):
        force = options["force"]
        created = 0
        updated = 0
        skipped = 0

        for plan_data in PLANS:
            lookup = {
                "tier": plan_data["tier"],
                "billing_cycle": plan_data["billing_cycle"],
            }
            existing = Plan.objects.filter(**lookup).first()

            if existing:
                if force:
                    for key, val in plan_data.items():
                        setattr(existing, key, val)
                    existing.save()
                    updated += 1
                    self.stdout.write(
                        self.style.WARNING(f"  Updated : {plan_data['name']}")
                    )
                else:
                    skipped += 1
                    self.stdout.write(
                        f"  Skipped : {plan_data['name']} (use --force to overwrite)"
                    )
            else:
                Plan.objects.create(**plan_data)
                created += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  Created : {plan_data['name']}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone — created={created} updated={updated} skipped={skipped}"
            )
        )
