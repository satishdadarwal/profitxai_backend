import datetime

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import DetailView, ListView

from apps.market.models import Asset
from apps.orders.models import Order
from apps.wallet.models import Transaction, Wallet

User = get_user_model()

# ─────────────────────────────────────────────────────────────────
#  Mixin — har admin view pe staff check enforce karega
# ─────────────────────────────────────────────────────────────────

class AdminRequiredMixin(View):
    @method_decorator(staff_member_required(login_url="/admin/login/"))
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)
    
# ─────────────────────────────────────────────────────────────────
#  1. Dashboard
# ─────────────────────────────────────────────────────────────────

class AdminDashboardView(AdminRequiredMixin, View):
    """
    Platform ka high-level snapshot:
      - User counts
      - Trade volume (last 30 days)
      - Open orders
      - Recent activity feed
    """

    template_name = "admin_panel/dashboard.html"

    def get(self, request):
        today = timezone.now().date()
        last_30 = today - datetime.timedelta(days=30)

        # ── User stats ──────────────────────────────────────────
        total_users = User.objects.count()
        new_users_30d = User.objects.filter(date_joined__date__gte=last_30).count()
        active_users = User.objects.filter(is_active=True).count()

        # ── Order stats ─────────────────────────────────────────
        orders_today = Order.objects.filter(created_at__date=today)
        open_orders = Order.objects.filter(status="open").count()
        filled_today = Order.objects.filter(
            status="filled", updated_at__date=today
        ).count()

        context = {
            "total_users": total_users,
            "new_users_30d": new_users_30d,
            "active_users": active_users,
            "total_trades": Order.objects.count(),
            "trades_today": orders_today.count(),
            "trade_volume_30d": 0,
            "open_orders": open_orders,
            "filled_today": filled_today,
            "recent_trades": (
                Order.objects.select_related("user", "asset").order_by("-created_at")[:10]
            ),
            "recent_users": User.objects.order_by("-date_joined")[:5],
        }
        return render(request, self.template_name, context)


# ─────────────────────────────────────────────────────────────────
#  2. User Management
# ─────────────────────────────────────────────────────────────────

class AdminUserListView(AdminRequiredMixin, ListView):
    """
    Searchable + filterable user list.
    Query params:
      ?q=<email/username>
      ?status=active|inactive|staff
    """

    model = User
    template_name = "admin_panel/users/list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        qs = User.objects.all().order_by("-date_joined")
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")

        if q:
            qs = qs.filter(Q(email__icontains=q) | Q(username__icontains=q))

        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        elif status == "staff":
            qs = qs.filter(is_staff=True)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "")
        ctx["status"] = self.request.GET.get("status", "")
        return ctx


class AdminUserDetailView(AdminRequiredMixin, DetailView):
    """
    Ek user ka full profile:
      - Wallet balance
      - Trade history
      - Open orders
    """

    model = User
    template_name = "admin_panel/users/detail.html"
    context_object_name = "target_user"
    pk_url_kwarg = "user_id"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.get_object()

        ctx["wallet"] = Wallet.objects.filter(user=user).first()
        ctx["trades"] = (
            Order.objects.filter(user=user)
            .select_related("asset")
            .order_by("-created_at")[:20]
        )
        ctx["orders"] = Order.objects.filter(user=user).order_by("-created_at")[:20]
        ctx["transactions"] = Transaction.objects.filter(wallet__user=user).order_by(
            "-created_at"
        )[:20]
        return ctx


class AdminUserToggleActiveView(AdminRequiredMixin, View):
    """
    User ko activate / deactivate karo — POST only.
    """

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        user.is_active = not user.is_active
        user.save(update_fields=["is_active"])
        state = "activated" if user.is_active else "deactivated"
        messages.success(request, f"User {user} has been {state}.")
        return redirect("admin_panel:user_detail", user_id=user_id)


# ─────────────────────────────────────────────────────────────────
#  3. Trade Management
# ─────────────────────────────────────────────────────────────────

class AdminTradeListView(AdminRequiredMixin, ListView):
    model = Order
    template_name = "admin_panel/trades/list.html"
    context_object_name = "trades"
    paginate_by = 30

    def get_queryset(self):
        qs = Order.objects.select_related("user", "asset").order_by("-created_at")

        if asset := self.request.GET.get("asset"):
            qs = qs.filter(asset__symbol__iexact=asset)

        if side := self.request.GET.get("side"):
            qs = qs.filter(side=side)

        if from_date := self.request.GET.get("from_date"):
            qs = qs.filter(created_at__date__gte=from_date)

        if to_date := self.request.GET.get("to_date"):
            qs = qs.filter(created_at__date__lte=to_date)

        if user_id := self.request.GET.get("user_id"):
            qs = qs.filter(user_id=user_id)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            {
                "asset": self.request.GET.get("asset", ""),
                "side": self.request.GET.get("side", ""),
                "from_date": self.request.GET.get("from_date", ""),
                "to_date": self.request.GET.get("to_date", ""),
            }
        )
        ctx["total_volume"] = 0
        return ctx


class AdminTradeDetailView(AdminRequiredMixin, DetailView):
    model = Order
    template_name = "admin_panel/trades/detail.html"
    context_object_name = "trade"
    pk_url_kwarg = "trade_id"


# ─────────────────────────────────────────────────────────────────
#  4. Order Management
# ─────────────────────────────────────────────────────────────────

class AdminOrderListView(AdminRequiredMixin, ListView):
    """
    All orders — filter by status, asset.
    """

    model = Order
    template_name = "admin_panel/orders/list.html"
    context_object_name = "orders"
    paginate_by = 30

    def get_queryset(self):
        qs = Order.objects.select_related("user", "asset").order_by("-created_at")

        if status := self.request.GET.get("status"):
            qs = qs.filter(status=status)

        if asset := self.request.GET.get("asset"):
            qs = qs.filter(asset__symbol__iexact=asset)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["status_choices"] = Order.STATUS_CHOICES  # e.g. [("open","Open"), ...]
        ctx["status_filter"] = self.request.GET.get("status", "")
        ctx["asset_filter"] = self.request.GET.get("asset", "")
        return ctx

class AdminOrderCancelView(AdminRequiredMixin, View):
    """
    Admin kisi bhi open order ko force-cancel kar sakta hai.
    """

    def post(self, request, order_id):
        order = get_object_or_404(Order, pk=order_id)

        if order.status != "open":
            messages.error(request, "Only open orders can be cancelled.")
            return redirect("admin_panel:order_list")

        order.status = "cancelled"
        order.save(update_fields=["status"])
        messages.success(request, f"Order #{order.id} cancelled.")
        return redirect("admin_panel:order_list")


# ─────────────────────────────────────────────────────────────────
#  5. Asset / Market Management
# ─────────────────────────────────────────────────────────────────

class AdminAssetListView(AdminRequiredMixin, ListView):
    model = Asset
    template_name = "admin_panel/assets/list.html"
    context_object_name = "assets"

    def get_queryset(self):
        return Asset.objects.annotate(
            trade_count=Count("trade"),
            total_volume=Sum("trade__amount"),
        ).order_by("-total_volume")

class AdminAssetToggleView(AdminRequiredMixin, View):
    """
    Asset ko enable / disable karo (trading suspend).
    """

    def post(self, request, asset_id):
        asset = get_object_or_404(Asset, pk=asset_id)
        asset.is_active = not asset.is_active
        asset.save(update_fields=["is_active"])
        state = "enabled" if asset.is_active else "disabled"
        messages.success(request, f"{asset.symbol} trading {state}.")
        return redirect("admin_panel:asset_list")


# ─────────────────────────────────────────────────────────────────
#  6. Wallet / Transaction Oversight
# ─────────────────────────────────────────────────────────────────

class AdminWalletListView(AdminRequiredMixin, ListView):
    model = Wallet
    template_name = "admin_panel/wallets/list.html"
    context_object_name = "wallets"
    paginate_by = 30

    def get_queryset(self):
        qs = Wallet.objects.select_related("user").order_by("-balance")
        if q := self.request.GET.get("q", "").strip():
            qs = qs.filter(Q(user__email__icontains=q) | Q(user__username__icontains=q))
        return qs

class AdminTransactionListView(AdminRequiredMixin, ListView):
    """
    Deposits / withdrawals — filter by type and date.
    """

    model = Transaction
    template_name = "admin_panel/wallets/transactions.html"
    context_object_name = "transactions"
    paginate_by = 30

    def get_queryset(self):
        qs = Transaction.objects.select_related("wallet__user").order_by("-created_at")

        if tx_type := self.request.GET.get("type"):
            qs = qs.filter(transaction_type=tx_type)

        if from_date := self.request.GET.get("from_date"):
            qs = qs.filter(created_at__date__gte=from_date)

        if to_date := self.request.GET.get("to_date"):
            qs = qs.filter(created_at__date__lte=to_date)

        return qs
