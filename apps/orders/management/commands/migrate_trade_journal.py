"""
Copy journal data from Trade fills → parent Order records.
Also back-fills TradeJournalEntry.order where it was only linked via trade.

Run once before dropping the Trade table:
    python manage.py migrate_trade_journal
"""
from django.core.management.base import BaseCommand

from apps.orders.models import Trade, TradeJournalEntry


class Command(BaseCommand):
    help = "Copy Trade.notes/tags/emoji_reaction → parent Order; fix TradeJournalEntry.order"

    def handle(self, *args, **options):
        self._migrate_trade_fields()
        self._backfill_journal_entries()

    def _migrate_trade_fields(self):
        trades = Trade.objects.select_related("order").all()
        updated = 0
        for trade in trades:
            order = trade.order
            changed = []

            if trade.notes and not order.journal_notes:
                order.journal_notes = trade.notes
                changed.append("journal_notes")

            if trade.tags and not order.tags:
                order.tags = trade.tags
                changed.append("tags")

            if trade.emoji_reaction and not order.emoji_reaction:
                order.emoji_reaction = trade.emoji_reaction
                changed.append("emoji_reaction")

            # Back-fill avg_fill_price from Trade.price if Order has neither
            if trade.price and not order.avg_fill_price and not order.entry_price:
                order.avg_fill_price = trade.price
                changed.append("avg_fill_price")

            # Back-fill realized_pnl
            if trade.realized_pnl is not None and order.realized_pnl is None:
                order.realized_pnl = trade.realized_pnl
                changed.append("realized_pnl")

            # Back-fill instrument_type from market_type
            if not order.instrument_type:
                if trade.market_type == "crypto":
                    order.instrument_type = "crypto"
                elif trade.option_type in ("CE", "PE"):
                    order.instrument_type = "options"
                else:
                    order.instrument_type = "equity"
                changed.append("instrument_type")

            if changed:
                order.save(update_fields=changed)
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(f"Updated {updated} / {trades.count()} Orders from Trade records")
        )

    def _backfill_journal_entries(self):
        entries = TradeJournalEntry.objects.filter(trade__isnull=False, order__isnull=True).select_related("trade__order")
        updated = 0
        for entry in entries:
            if entry.trade and entry.trade.order_id:
                entry.order_id = entry.trade.order_id
                entry.save(update_fields=["order"])
                updated += 1
        self.stdout.write(
            self.style.SUCCESS(f"Back-filled order on {updated} TradeJournalEntry records")
        )
