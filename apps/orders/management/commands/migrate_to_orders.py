"""
Management command to migrate OptionTrade and PaperTrade records into the
centralised Order model.

Usage:
    python manage.py migrate_to_orders [--dry-run]
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.orders.models import Order
from apps.options.models import OptionTrade
from apps.paper_trading.models import PaperTrade


def _get_or_create_asset(symbol: str, asset_type: str = "options"):
    """Return an Asset for the given symbol, creating one if necessary."""
    from apps.market.models import Asset

    asset, _ = Asset.objects.get_or_create(
        symbol=symbol,
        defaults={
            "name": symbol,
            "asset_type": asset_type,
            "exchange": "NSE",
            "currency": "INR",
            "is_active": True,
        },
    )
    return asset


class Command(BaseCommand):
    help = "Migrate OptionTrade and PaperTrade records into the Order model"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate migration without writing to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be saved"))

        migrated_option = 0
        migrated_paper = 0
        skipped = 0

        # ── OptionTrade → Order ───────────────────────────────────────────────
        for trade in OptionTrade.objects.select_related(
            "user", "symbol", "contract"
        ).order_by("entry_time"):
            symbol_str = f"{trade.symbol.name}{int(trade.contract.strike)}{trade.contract.option_type}"
            key = f"optiontrade_{trade.id}"

            # Deduplicate by client_order_id (idempotent)
            if Order.objects.filter(client_order_id=key).exists():
                skipped += 1
                continue

            try:
                asset = _get_or_create_asset(symbol_str, "options")

                order_kwargs = dict(
                    user=trade.user,
                    asset=asset,
                    client_order_id=key,
                    side=trade.action,
                    order_type="market",
                    status=trade.status,          # 'open' or 'closed'
                    mode=trade.mode,              # 'live' or 'paper'
                    quantity=Decimal(str(trade.quantity)),
                    filled_qty=Decimal(str(trade.quantity)),
                    # Trade tracking fields
                    entry_price=Decimal(str(trade.entry_price)),
                    exit_price=Decimal(str(trade.exit_price)) if trade.exit_price else None,
                    realized_pnl=Decimal(str(trade.pnl)) if trade.pnl is not None else None,
                    entry_time=trade.entry_time,
                    exit_time=trade.exit_time,
                    exit_reason=trade.exit_reason or "",
                    position_size=trade.quantity,
                    # Display helpers
                    symbol_display=symbol_str,
                    option_type=trade.contract.option_type,
                    lots=trade.lots,
                    instrument_type="options",
                )

                if not dry_run:
                    with transaction.atomic():
                        Order.objects.create(**order_kwargs)

                migrated_option += 1

            except Exception as exc:
                self.stderr.write(
                    f"OptionTrade {trade.id} skipped: {exc}"
                )
                skipped += 1

        # ── PaperTrade → Order ────────────────────────────────────────────────
        for trade in PaperTrade.objects.select_related("account__user").order_by(
            "opened_at"
        ):
            key = f"papertrade_{trade.id}"

            if Order.objects.filter(client_order_id=key).exists():
                skipped += 1
                continue

            try:
                user = trade.account.user
                asset = _get_or_create_asset(trade.symbol, trade.asset_type)

                side = trade.side
                if side == "long":
                    side = "buy"
                elif side == "short":
                    side = "sell"

                order_kwargs = dict(
                    user=user,
                    asset=asset,
                    client_order_id=key,
                    side=side,
                    order_type="market",
                    status=trade.status,          # 'open' or 'closed'
                    mode="paper",
                    quantity=Decimal(str(trade.quantity)),
                    filled_qty=Decimal(str(trade.quantity)),
                    # Trade tracking fields
                    entry_price=trade.entry_price,
                    exit_price=trade.exit_price,
                    realized_pnl=trade.pnl if trade.pnl else None,
                    entry_time=trade.opened_at,
                    exit_time=trade.closed_at,
                    exit_reason=trade.exit_reason or "",
                    position_size=int(trade.quantity),
                    # Display helpers
                    symbol_display=trade.display_name or trade.symbol,
                    option_type=trade.option_type or "",
                    lots=None,
                    instrument_type=trade.asset_type,
                )

                if not dry_run:
                    with transaction.atomic():
                        Order.objects.create(**order_kwargs)

                migrated_paper += 1

            except Exception as exc:
                self.stderr.write(
                    f"PaperTrade {trade.id} skipped: {exc}"
                )
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done — Migrated {migrated_option} OptionTrades, "
                f"{migrated_paper} PaperTrades, {skipped} skipped"
            )
        )
