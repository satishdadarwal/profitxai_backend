# apps/orders/views.py
# UPDATED VERSION - WITH TAG FILTERING AND JOURNAL FEATURES

import logging

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from datetime import datetime

logger = logging.getLogger(__name__)

from .models import Order, TradeJournalEntry
from .serializers import (
    OrderSerializer,
    TradeSerializer,
    TradeUpdateSerializer,
    TradeJournalEntrySerializer,
    TradeFilterSerializer,
)


class OrderViewSet(viewsets.ModelViewSet):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).select_related(
            'asset', 'strategy', 'broker_account'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['patch'])
    def update_journal(self, request, pk=None):
        """PATCH /api/v1/orders/orders/{id}/update_journal/ — notes/tags/emoji save"""
        order = self.get_object()

        if 'notes' in request.data:
            order.journal_notes = request.data['notes']
        if 'tags' in request.data:
            order.tags = request.data['tags']
        if 'emoji_reaction' in request.data:
            order.emoji_reaction = request.data['emoji_reaction']
        order.save(update_fields=['journal_notes', 'tags', 'emoji_reaction', 'updated_at'])
        return Response({"status": "ok", "id": str(order.id)})


_INDIAN_TYPES = frozenset(["options", "futures", "equity", "index", ""])


class TradeViewSet(viewsets.ModelViewSet):
    """
    Journal ViewSet backed by Order model.
    Keeps the same URL endpoints (orders/trades/) and response shape
    that Flutter's journal_provider expects.
    """
    serializer_class = TradeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = (
            Order.objects.filter(user=self.request.user)
            .select_related("asset")
            .order_by("-created_at")
        )

        filter_serializer = TradeFilterSerializer(data=self.request.query_params)
        if filter_serializer.is_valid():
            data = filter_serializer.validated_data

            market_type = data.get("market_type", "all")
            if market_type == "indian":
                queryset = queryset.filter(instrument_type__in=list(_INDIAN_TYPES))
            elif market_type == "crypto":
                queryset = queryset.filter(instrument_type="crypto")

            mode = data.get("mode", "all")
            if mode != "all":
                queryset = queryset.filter(mode=mode)

            tags = data.get("tags")
            if tags:
                tag_query = Q()
                for tag in tags:
                    tag_query |= Q(tags__contains=[tag])
                queryset = queryset.filter(tag_query)

            emoji = data.get("emoji")
            if emoji:
                queryset = queryset.filter(emoji_reaction=emoji)

            start_date = data.get("start_date")
            if start_date:
                queryset = queryset.filter(created_at__date__gte=start_date)

            end_date = data.get("end_date")
            if end_date:
                queryset = queryset.filter(created_at__date__lte=end_date)

            has_notes = data.get("has_notes")
            if has_notes is not None:
                if has_notes:
                    queryset = queryset.exclude(journal_notes="")
                else:
                    queryset = queryset.filter(journal_notes="")

        return queryset

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=["patch"])
    def update_journal(self, request, pk=None):
        order = self.get_object()
        serializer = TradeUpdateSerializer(order, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(TradeSerializer(order).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"])
    def all_tags(self, request):
        orders = Order.objects.filter(user=request.user).exclude(tags=[])
        all_tags: set = set()
        for order in orders:
            all_tags.update(order.tags)
        return Response({"tags": sorted(all_tags)})

    @action(detail=False, methods=["get"])
    def tag_stats(self, request):
        orders = Order.objects.filter(user=request.user).exclude(tags=[])
        tag_counts: dict = {}
        for order in orders:
            for tag in order.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        return Response(
            {"tag_stats": [{"tag": tag, "count": count} for tag, count in sorted_tags]}
        )


class TradeJournalEntryViewSet(viewsets.ModelViewSet):
    serializer_class = TradeJournalEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return (
            TradeJournalEntry.objects.filter(user=self.request.user)
            .select_related("order", "order__asset")
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
# apps/orders/views.py
# ADD THIS TO EXISTING FILE - Daily Performance Calendar View

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Q, Avg
from django.db.models.functions import TruncDate
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict

from .models import Order


class CalendarPerformanceView(APIView):
    """
    GET /api/trades/calendar-performance/?year=2025&month=4&market_type=all

    Returns daily aggregated performance for calendar visualization.
    Queries the centralised Order model (populated via migrate_to_orders).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        year = int(request.query_params.get('year', datetime.now().year))
        month = int(request.query_params.get('month', datetime.now().month))
        market_type = request.query_params.get('market_type', 'all')

        start_date = datetime(year, month, 1).date()
        if month == 12:
            end_date = datetime(year + 1, 1, 1).date()
        else:
            end_date = datetime(year, month + 1, 1).date()

        qs = Order.objects.filter(
            user=user,
            status='closed',
        ).filter(
            Q(exit_time__date__gte=start_date, exit_time__date__lt=end_date) |
            Q(exit_time__isnull=True, updated_at__date__gte=start_date, updated_at__date__lt=end_date)
        )
        if market_type == 'live':
            qs = qs.filter(mode='live')
        elif market_type == 'paper':
            qs = qs.filter(mode='paper')

        daily_data = defaultdict(lambda: {
            'date': None,
            'pnl': 0,
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'breakeven': 0,
            'win_rate': 0,
            'avg_pnl': 0,
        })

        for order in qs:
            date_key = (order.exit_time or order.updated_at).date().isoformat()
            pnl = float(order.realized_pnl or 0)
            daily_data[date_key]['date'] = date_key
            daily_data[date_key]['pnl'] += pnl
            daily_data[date_key]['trades'] += 1
            if pnl > 0:
                daily_data[date_key]['wins'] += 1
            elif pnl < 0:
                daily_data[date_key]['losses'] += 1
            else:
                daily_data[date_key]['breakeven'] += 1

        for data in daily_data.values():
            total = data['trades']
            if total > 0:
                data['win_rate'] = round((data['wins'] / total) * 100, 2)
                data['avg_pnl'] = round(data['pnl'] / total, 2)

        calendar_data = sorted(daily_data.values(), key=lambda x: x['date'])

        total_pnl = sum(day['pnl'] for day in calendar_data)
        total_trades = sum(day['trades'] for day in calendar_data)
        total_wins = sum(day['wins'] for day in calendar_data)
        total_losses = sum(day['losses'] for day in calendar_data)
        profitable_days = [d for d in calendar_data if d['pnl'] > 0]
        losing_days = [d for d in calendar_data if d['pnl'] < 0]
        best_day = max(calendar_data, key=lambda x: x['pnl']) if calendar_data else None
        worst_day = min(calendar_data, key=lambda x: x['pnl']) if calendar_data else None

        return Response({
            'year': year,
            'month': month,
            'market_type': market_type,
            'daily_data': calendar_data,
            'summary': {
                'total_pnl': round(total_pnl, 2),
                'total_trades': total_trades,
                'total_wins': total_wins,
                'total_losses': total_losses,
                'win_rate': round((total_wins / total_trades * 100) if total_trades > 0 else 0, 2),
                'profitable_days': len(profitable_days),
                'losing_days': len(losing_days),
                'breakeven_days': len([d for d in calendar_data if d['pnl'] == 0 and d['trades'] > 0]),
                'best_day': best_day,
                'worst_day': worst_day,
                'current_streak': self._calculate_streak(calendar_data),
            }
        })

    def _calculate_streak(self, calendar_data):
        if not calendar_data:
            return {'type': 'none', 'count': 0}
        sorted_days = sorted(calendar_data, key=lambda x: x['date'], reverse=True)
        streak_type = None
        streak_count = 0
        for day in sorted_days:
            if day['trades'] == 0:
                continue
            if day['pnl'] > 0:
                if streak_type is None:
                    streak_type = 'winning'
                    streak_count = 1
                elif streak_type == 'winning':
                    streak_count += 1
                else:
                    break
            elif day['pnl'] < 0:
                if streak_type is None:
                    streak_type = 'losing'
                    streak_count = 1
                elif streak_type == 'losing':
                    streak_count += 1
                else:
                    break
        return {'type': streak_type or 'none', 'count': streak_count}
# apps/orders/views.py
# ADD THIS - Export CSV/PDF Functionality

import csv
import io
from datetime import datetime
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Sum, Count, Q

from reportlab.lib.pagesizes import letter, A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.lib.units import inch


class ExportTradesCSVView(APIView):
    """
    GET /api/trades/export/csv/?start_date=2025-01-01&end_date=2025-12-31&market_type=all
    
    Exports trades to CSV with all journal fields (notes, tags, emoji)
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user = request.user
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        market_type = request.query_params.get("market_type", "all")

        orders = Order.objects.filter(user=user).select_related("asset").order_by("created_at")
        if start_date:
            orders = orders.filter(created_at__date__gte=start_date)
        if end_date:
            orders = orders.filter(created_at__date__lte=end_date)
        if market_type == "indian":
            orders = orders.filter(instrument_type__in=list(_INDIAN_TYPES))
        elif market_type == "crypto":
            orders = orders.filter(instrument_type="crypto")

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Date", "Market", "Symbol", "Side", "Type", "Quantity", "Entry Price",
            "Exit Price", "PnL", "Fee", "Net PnL", "Mode", "Strike", "Option Type",
            "Lots", "Leverage", "Notes", "Tags", "Emoji", "Status",
        ])

        for ord_ in orders:
            price = float(ord_.avg_fill_price or ord_.entry_price or ord_.limit_price or 0)
            pnl = float(ord_.realized_pnl or 0)
            tags_str = ", ".join(ord_.tags) if ord_.tags else ""
            sym = ord_.symbol_display or (ord_.asset.symbol if ord_.asset else "")
            ts = ord_.entry_time or ord_.created_at
            market_label = "Crypto Market" if ord_.instrument_type == "crypto" else "Indian Market"
            writer.writerow([
                ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "",
                market_label,
                sym,
                ord_.side.upper(),
                (ord_.instrument_type or "order").upper(),
                float(ord_.quantity),
                price,
                float(ord_.exit_price or 0) or "",
                pnl,
                0,
                pnl,
                ord_.mode.upper(),
                "",
                ord_.option_type or "",
                ord_.lots or "",
                "",
                ord_.journal_notes,
                tags_str,
                ord_.emoji_reaction,
                ord_.status.upper(),
            ])

        output.seek(0)
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        filename = f"profitx_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class ExportTradesPDFView(APIView):
    """
    GET /api/trades/export/pdf/?start_date=2025-01-01&end_date=2025-12-31&market_type=all
    
    Exports trades to PDF with equity curve chart and summary statistics
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user = request.user
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        market_type = request.query_params.get("market_type", "all")

        orders = Order.objects.filter(user=user).select_related("asset").order_by("created_at")
        if start_date:
            orders = orders.filter(created_at__date__gte=start_date)
        if end_date:
            orders = orders.filter(created_at__date__lte=end_date)
        if market_type == "indian":
            orders = orders.filter(instrument_type__in=list(_INDIAN_TYPES))
        elif market_type == "crypto":
            orders = orders.filter(instrument_type="crypto")

        stats = self._calculate_statistics(list(orders), market_type)
        
        # Create PDF
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        elements = []
        
        styles = getSampleStyleSheet()
        title_style = styles['Heading1']
        
        # Title
        elements.append(Paragraph('ProfitX AI - Trading Journal Report', title_style))
        elements.append(Spacer(1, 12))
        
        # Date range
        date_range = f"Period: {start_date or 'Beginning'} to {end_date or 'Now'}"
        elements.append(Paragraph(date_range, styles['Normal']))
        elements.append(Spacer(1, 12))
        
        # Summary statistics table
        summary_data = [
            ['Metric', 'Value'],
            ['Total Trades', str(stats['total_trades'])],
            ['Winning Trades', str(stats['wins'])],
            ['Losing Trades', str(stats['losses'])],
            ['Win Rate', f"{stats['win_rate']:.2f}%"],
            ['Total PnL', f"₹{stats['total_pnl']:.2f}"],
            ['Avg Win', f"₹{stats['avg_win']:.2f}"],
            ['Avg Loss', f"₹{stats['avg_loss']:.2f}"],
            ['Largest Win', f"₹{stats['largest_win']:.2f}"],
            ['Largest Loss', f"₹{stats['largest_loss']:.2f}"],
            ['Profit Factor', f"{stats['profit_factor']:.2f}"],
        ]
        
        summary_table = Table(summary_data, colWidths=[3*inch, 2*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        
        elements.append(summary_table)
        elements.append(Spacer(1, 24))
        
        # Equity curve chart
        if stats['equity_curve']:
            equity_chart = self._create_equity_curve(stats['equity_curve'])
            elements.append(Paragraph('Equity Curve', styles['Heading2']))
            elements.append(Spacer(1, 12))
            elements.append(equity_chart)
            elements.append(Spacer(1, 24))
        
        # Build PDF
        doc.build(elements)
        
        # Prepare response
        buffer.seek(0)
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        filename = f'profitx_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        
        return response
    
    def _calculate_statistics(self, orders, market_type):
        all_pnls = []
        for order in orders:
            if order.realized_pnl is not None:
                all_pnls.append(float(order.realized_pnl))
        
        if not all_pnls:
            return self._empty_stats()
        
        wins = [p for p in all_pnls if p > 0]
        losses = [p for p in all_pnls if p < 0]
        
        total_pnl = sum(all_pnls)
        total_wins_amount = sum(wins) if wins else 0
        total_losses_amount = abs(sum(losses)) if losses else 0
        
        # Calculate equity curve
        equity_curve = []
        cumulative = 0
        for i, pnl in enumerate(all_pnls):
            cumulative += pnl
            equity_curve.append((i + 1, cumulative))
        
        return {
            'total_trades': len(all_pnls),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': (len(wins) / len(all_pnls) * 100) if all_pnls else 0,
            'total_pnl': total_pnl,
            'avg_win': (total_wins_amount / len(wins)) if wins else 0,
            'avg_loss': (total_losses_amount / len(losses)) if losses else 0,
            'largest_win': max(wins) if wins else 0,
            'largest_loss': min(losses) if losses else 0,
            'profit_factor': (total_wins_amount / total_losses_amount) if total_losses_amount > 0 else 0,
            'equity_curve': equity_curve,
        }
    
    def _empty_stats(self):
        return {
            'total_trades': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0,
            'total_pnl': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'largest_win': 0,
            'largest_loss': 0,
            'profit_factor': 0,
            'equity_curve': [],
        }
    
    def _create_equity_curve(self, equity_data):
        """Create equity curve line chart"""
        drawing = Drawing(400, 200)
        
        lp = LinePlot()
        lp.x = 50
        lp.y = 50
        lp.height = 125
        lp.width = 300
        
        # Prepare data
        lp.data = [[(x, y) for x, y in equity_data]]
        
        lp.joinedLines = 1
        lp.lines[0].strokeColor = colors.blue
        lp.lines[0].strokeWidth = 2
        
        lp.xValueAxis.valueMin = 0
        lp.xValueAxis.valueMax = max(x for x, _ in equity_data) if equity_data else 1
        
        y_values = [y for _, y in equity_data]
        lp.yValueAxis.valueMin = min(y_values) if y_values else 0
        lp.yValueAxis.valueMax = max(y_values) if y_values else 1
        
        drawing.add(lp)
        
        return drawing

# Risk/Reward Calculator
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def calculate_risk_reward(request):
    """
    POST /api/trades/calculate-rr/
    Calculate position size and R:R ratio for a trade.
    """
    market_type = request.data.get('market_type')
    entry = float(request.data.get('entry_price'))
    sl = float(request.data.get('stop_loss'))
    tp = float(request.data.get('target_price'))
    account_size = float(request.data.get('account_size'))
    risk_percent = float(request.data.get('risk_percent', 2))

    # Calculate risk per unit
    risk_per_unit = abs(entry - sl)
    reward_per_unit = abs(tp - entry)
    rr_ratio = reward_per_unit / risk_per_unit if risk_per_unit > 0 else 0
    risk_amount = account_size * (risk_percent / 100)

    if market_type == 'indian':
        lot_size = int(request.data.get('lot_size', 75))
        risk_per_lot = risk_per_unit * lot_size
        lots = max(1, int(risk_amount / risk_per_lot))
        actual_quantity = lots * lot_size
        actual_risk = lots * risk_per_lot
        potential_reward = lots * lot_size * reward_per_unit

        return Response({
            'market_type': 'indian',
            'recommended_lots': lots,
            'quantity': actual_quantity,
            'risk_per_lot': round(risk_per_lot, 2),
            'total_risk': round(actual_risk, 2),
            'potential_reward': round(potential_reward, 2),
            'rr_ratio': round(rr_ratio, 2),
            'risk_percent_actual': round((actual_risk / account_size) * 100, 2),
        })

    elif market_type == 'crypto':
        leverage = float(request.data.get('leverage', 1))
        position_value = risk_amount / risk_per_unit
        margin_required = position_value / leverage
        quantity = position_value / entry
        potential_reward = quantity * reward_per_unit

        return Response({
            'market_type': 'crypto',
            'recommended_quantity': round(quantity, 8),
            'position_value': round(position_value, 2),
            'margin_required': round(margin_required, 2),
            'leverage': leverage,
            'total_risk': round(risk_amount, 2),
            'potential_reward': round(potential_reward, 2),
            'rr_ratio': round(rr_ratio, 2),
            'risk_percent_actual': risk_percent,
        })

    else:
        return Response({'error': 'Invalid market_type'}, status=400)


class TradeJournalListView(APIView):
    """
    GET /api/v1/orders/journal/?page=1&market_type=indian&mode=live&tags=fvg
    Order model is single source of truth — use ?mode=paper|live|all
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.core.paginator import Paginator
        market_type = request.query_params.get("market_type", "all")
        mode_filter = request.query_params.get("mode", "all")
        tags_filter = request.query_params.get("tags", "")
        page_num    = int(request.query_params.get("page", 1))
        results = []

        # Order model — single source of truth for paper and live
        order_qs = Order.objects.filter(
            user=request.user,
        ).exclude(side=None).exclude(status="rejected").select_related("asset").order_by("-created_at")
        if mode_filter != "all":
            order_qs = order_qs.filter(mode=mode_filter)
        if market_type == "indian":
            order_qs = order_qs.exclude(notes__icontains="USD")
        elif market_type == "crypto":
            order_qs = order_qs.filter(
                Q(notes__icontains="BTC") | Q(notes__icontains="ETH") |
                Q(notes__icontains="SOL") | Q(notes__icontains="USD")
            )
        for o in order_qs:
            sym = o.notes or (o.asset.symbol if o.asset else "")
            is_crypto = any(k in sym.upper() for k in ["USDT","BTC","ETH","SOL","PERP"])
            mtype = "crypto" if is_crypto else "indian"
            side = o.side or "buy"
            if side == "long": side = "buy"
            elif side == "short": side = "sell"
            price = float(o.avg_fill_price) if o.avg_fill_price and float(o.avg_fill_price) > 0 else float(o.limit_price or 0)
            # Use new journal fields if available
            opt_type = o.option_type or ("CE" if "CE" in sym else "PE" if "PE" in sym else "")
            qty = float(o.quantity or 0)
            lots = o.lots
            if lots is None:
                LOT_SIZES = {"NIFTY":65,"BANKNIFTY":30,"FINNIFTY":40,"MIDCPNIFTY":120,"SENSEX":10}
                underlying = next((k for k in LOT_SIZES if k in sym.upper()), None)
                lot_size = LOT_SIZES.get(underlying, 1)
                lots = int(qty // lot_size) if lot_size > 1 and qty > 0 else None

            results.append({
                "id": str(o.id), "order_id": str(o.id),
                "symbol": o.symbol_display or sym,
                "asset_name": o.asset.symbol if o.asset else sym,
                "market_type": mtype,
                "market_display": "Crypto Market" if is_crypto else "Indian Market",
                "side": side, "mode": o.mode or "live",
                "quantity": qty, "price": price,
                "amount": qty * price, "fee": 0.0,
                "realized_pnl": float(o.realized_pnl) if o.realized_pnl is not None else None,
                "net_pnl": float(o.realized_pnl) if o.realized_pnl is not None else None,
                "notes": o.journal_notes or "",
                "tags": o.tags or [],
                "emoji_reaction": o.emoji_reaction or "",
                "strike": None, "lots": lots, "option_type": opt_type,
                "leverage": None, "funding_fee": None,
                "broker": o.broker or ("fyers" if not is_crypto else "delta"),
                "instrument_type": o.instrument_type or ("options" if opt_type else "equity"),
                "created_at": o.created_at.isoformat(),
            })


        # Buy+Sell match karke net_pnl calculate karo
        from collections import defaultdict
        buys = defaultdict(list)
        for r in results:
            if r.get("side") == "buy" and r.get("price", 0) > 0:
                buys[r["symbol"]].append(r)
        for r in results:
            if r.get("side") == "sell":
                sym = r["symbol"]
                if buys.get(sym):
                    buy = buys[sym].pop(0)
                    # Use stored realized_pnl first — only calculate if missing
                    if r["realized_pnl"] is not None:
                        pass  # already populated from Order.realized_pnl
                    else:
                        entry = buy["price"]
                        exit_p = r["price"]
                        qty = r["quantity"]
                        if entry > 0 and exit_p > 0 and qty > 0:
                            if buy.get("side") in ("buy", "long"):
                                pnl = (exit_p - entry) * qty
                            else:
                                pnl = (entry - exit_p) * qty
                            pnl = round(pnl, 2)
                        else:
                            pnl = 0.0
                        r["realized_pnl"] = pnl
                        r["net_pnl"] = pnl

        results.sort(key=lambda x: x["created_at"], reverse=True)
        paginator = Paginator(results, 20)
        page = paginator.get_page(page_num)
        return Response({
            "count": paginator.count,
            "next": f"?page={page_num+1}" if page.has_next() else None,
            "previous": f"?page={page_num-1}" if page.has_previous() else None,
            "results": list(page.object_list),
        })



class DailyPnlView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.utils import timezone
        from .models import DailyPnlSnapshot, Position
        today = timezone.now().date()
        mode = request.query_params.get('mode', 'live')
        market_type = request.query_params.get('market_type', 'all')
        date_str = request.query_params.get('date', None)
        target_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today

        snap = DailyPnlSnapshot.objects.filter(
            user=request.user, date=target_date, mode=mode
        ).first()

        snap_realized = float(getattr(snap, 'realized_pnl', 0) or getattr(snap, 'realised_pnl', 0) or 0) if snap else 0
        snap_trades = (getattr(snap, 'trade_count', 0) or getattr(snap, 'total_trades', 0) or 0) if snap else 0
        if snap and snap_trades > 0 and snap_realized != 0:
            return Response({
                'date': str(snap.date),
                'mode': snap.mode,
                'realised': snap_realized,
                'unrealised': float(getattr(snap, 'unrealized_pnl', 0) or getattr(snap, 'unrealised_pnl', 0) or 0) if snap else 0,
                'total': snap_realized,
                'total_trades': snap_trades,
                'wins': (getattr(snap, 'win_count', 0) or getattr(snap, 'wins', 0) or 0) if snap else 0,
                'realized_pnl': snap_realized,
                'unrealized_pnl': 0.0,
                'total_pnl': snap_realized,
                'source': 'snapshot',
            })

        orders_qs = Order.objects.filter(
            user=request.user,
            status='closed',
        ).filter(
            Q(exit_time__date=target_date) |
            Q(exit_time__isnull=True, updated_at__date=target_date)
        )
        if mode != 'all':
            orders_qs = orders_qs.filter(mode=mode)

        realised = 0.0
        wins = 0
        losses = 0
        for o in orders_qs:
            pnl = float(o.realized_pnl or 0)
            realised += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        # Unrealized PnL — live from Fyers positions API
        unrealised = 0.0
        try:
            from apps.brokers.models import BrokerAccount
            from fyers_apiv3 import fyersModel
            account = BrokerAccount.objects.filter(
                user=request.user, broker='fyers', is_active=True
            ).first()
            if account:
                fyers = fyersModel.FyersModel(
                    client_id=account.app_id,
                    token=account.access_token,
                    is_async=False, log_path='',
                )
                pos_resp = fyers.positions()
                if pos_resp.get('s') == 'ok':
                    for pos in pos_resp.get('netPositions', []):
                        net_qty = float(pos.get('netQty', 0))
                        if net_qty != 0:
                            buy_avg = float(pos.get('buyAvg', 0))
                            sell_avg = float(pos.get('sellAvg', 0))
                            ltp = float(pos.get('ltp', 0))
                            qty = abs(net_qty)
                            if net_qty > 0:
                                unrealised += (ltp - buy_avg) * qty
                            else:
                                unrealised += (sell_avg - ltp) * qty
        except Exception as e:
            logger.error("Unrealized PnL fetch error | %s", e)

        return Response({
            'date': str(target_date),
            'mode': mode,
            'market_type': market_type,
            'realized_pnl': round(realised, 2),
            'unrealized_pnl': round(unrealised, 2),
            'total_pnl': round(realised + unrealised, 2),
            # keep legacy keys so existing Flutter code doesn't break
            'realised': round(realised, 2),
            'unrealised': round(unrealised, 2),
            'fees': 0.0,
            'total_trades': orders_qs.count(),
            'wins': wins,
            'losses': losses,
            'source': 'live',
        })
