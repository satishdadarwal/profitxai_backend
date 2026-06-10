"""
Post-save signal: automatically sync OptionTrade records into the
centralised Order model so Flutter's /orders/ endpoint always has fresh data.
"""

import logging
from decimal import Decimal

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.options.models import OptionTrade

logger = logging.getLogger(__name__)


def _get_or_create_asset(symbol: str):
    """Return a market.Asset for the given symbol, creating one if needed."""
    from apps.market.models import Asset

    asset, _ = Asset.objects.get_or_create(
        symbol=symbol,
        defaults={
            "name": symbol,
            "asset_type": "options",
            "exchange": "NSE",
            "currency": "INR",
            "is_active": True,
        },
    )
    return asset


@receiver(post_save, sender=OptionTrade)
def sync_option_trade_to_order(sender, instance: OptionTrade, created: bool, **kwargs):
    """
    Keep Order model in sync with OptionTrade.

    - On create  → create Order(status='open')
    - On update  → update existing Order fields; if status changed to
                   'closed', populate exit_price / realized_pnl / exit_time.
    """
    try:
        from apps.orders.models import Order

        symbol_str = (
            f"{instance.symbol.name}"
            f"{int(instance.contract.strike)}"
            f"{instance.contract.option_type}"
        )
        client_key = f"optiontrade_{instance.id}"

        asset = _get_or_create_asset(symbol_str)

        common = dict(
            side=instance.action,
            order_type="market",
            status=instance.status,
            mode=instance.mode,
            quantity=Decimal(str(instance.quantity)),
            filled_qty=Decimal(str(instance.quantity)),
            entry_price=Decimal(str(instance.entry_price)),
            exit_price=Decimal(str(instance.exit_price)) if instance.exit_price else None,
            realized_pnl=Decimal(str(instance.pnl)) if instance.pnl is not None else None,
            current_price=Decimal(str(instance.current_price)) if instance.current_price else None,
            entry_time=instance.entry_time,
            exit_time=instance.exit_time,
            exit_reason=instance.exit_reason or "",
            position_size=instance.quantity,
            symbol_display=symbol_str,
            option_type=instance.contract.option_type,
            lots=instance.lots,
            instrument_type="options",
        )

        order, order_created = Order.objects.get_or_create(
            client_order_id=client_key,
            defaults=dict(user=instance.user, asset=asset, **common),
        )

        if not order_created:
            for field, value in common.items():
                setattr(order, field, value)
            order.save(update_fields=list(common.keys()) + ["updated_at"])

    except Exception as exc:
        logger.error(
            "sync_option_trade_to_order failed for OptionTrade %s: %s",
            instance.id,
            exc,
            exc_info=True,
        )
