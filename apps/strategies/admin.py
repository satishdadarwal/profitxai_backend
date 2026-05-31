# apps/strategies/admin.py

from django import forms
from django.contrib import admin

from .models import Strategy, StrategyPerformanceSnapshot, StrategySignal, UserStrategyPreference


# ─────────────────────────────────────────────────────────────────
#  Helper — DB se plan choices fetch karo
# ─────────────────────────────────────────────────────────────────
def _get_plan_choices():
    """
    DB se active plans fetch karo.
    Pylance warning fix: get_billing_cycle_display() Django auto-method hai —
    Plan model mein billing_cycle field pe choices hone chahiye.
    Agar nahi hain toh simple name use karo.
    """
    try:
        from apps.subscriptions.models import Plan
        plans = Plan.objects.filter(is_active=True).order_by("tier")
        choices = []
        for plan in plans:
            # ✅ FIX: get_billing_cycle_display() sirf tab kaam karta hai jab
            # billing_cycle field pe choices set ho. Safe fallback use karo.
            try:
                cycle_label = plan.get_billing_cycle_display()  # type: ignore[attr-defined]
            except AttributeError:
                cycle_label = getattr(plan, "billing_cycle", "")
            price = getattr(plan, "price_inr", "")
            label = f"{plan.name}"
            if cycle_label:
                label += f" ({cycle_label})"
            if price:
                label += f" — ₹{price}"
            choices.append((plan.name, label))
        return choices
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────
#  ✅ FIX: UserPreferenceInline — StrategyAdmin se PEHLE define karo
#  (StrategyAdmin mein inlines = [UserPreferenceInline] use hota hai)
# ─────────────────────────────────────────────────────────────────
class UserPreferenceInline(admin.TabularInline):
    """Strategy detail page pe inline — kitne users ne kya choose kiya."""
    model = UserStrategyPreference
    extra = 0
    readonly_fields = ("user", "preferred_mode", "is_running", "updated_at")
    fields = ("user", "preferred_mode", "is_running", "updated_at")
    can_delete = False
    show_change_link = True
    verbose_name = "User Preference"
    verbose_name_plural = "User Preferences (kitne users ne yeh strategy use ki)"


# ─────────────────────────────────────────────────────────────────
#  Strategy Admin Form
# ─────────────────────────────────────────────────────────────────
class StrategyAdminForm(forms.ModelForm):
    """
    Custom form — allowed_plans ke liye dynamic checkboxes.
    ✅ FIX: choices= field assignment Pylance warning —
    __init__ mein widget.choices set karo (not field.choices directly).
    """

    allowed_plans_selector = forms.MultipleChoiceField(
        choices=[],
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Allowed Plans (kisi ko select na karo = sab plans ko dikhe)",
        help_text=(
            "✅ Koi plan select na karo → sab plans ke users ko strategy dikhe.<br>"
            "🔒 Plans select karo → sirf un plan wale users ko dikhe.<br>"
            "<b>Example:</b> Sirf Pro aur Elite ko → Pro + Elite check karo."
        ),
    )

    class Meta:
        model = Strategy
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ✅ FIX: field.choices nahi — field ko directly reassign karo
        # Pylance: "Cannot assign to attribute choices for class Field"
        # Solution: naya field instance banao updated choices ke saath
        plan_choices = _get_plan_choices()
        self.fields["allowed_plans_selector"] = forms.MultipleChoiceField(
            choices=plan_choices,
            widget=forms.CheckboxSelectMultiple,
            required=False,
            label="Allowed Plans (kisi ko select na karo = sab plans ko dikhe)",
            help_text=(
                "✅ Koi plan select na karo → sab plans ke users ko strategy dikhe.<br>"
                "🔒 Plans select karo → sirf un plan wale users ko dikhe.<br>"
                "<b>Example:</b> Sirf Pro aur Elite ko → Pro + Elite check karo."
            ),
        )

        # Existing values pre-populate (edit form ke liye)
        if self.instance and self.instance.pk:
            self.fields["allowed_plans_selector"].initial = (
                self.instance.allowed_plans or []
            )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.allowed_plans = self.cleaned_data.get("allowed_plans_selector", [])
        if commit:
            instance.save()
        return instance


# ─────────────────────────────────────────────────────────────────
#  Strategy Admin
# ─────────────────────────────────────────────────────────────────
@admin.register(Strategy)
class StrategyAdmin(admin.ModelAdmin):
    form = StrategyAdminForm

    # ✅ UserPreferenceInline pehle define ho chuki hai — ab safe hai
    inlines = [UserPreferenceInline]

    list_display = (
        "id",
        "user",
        "broker",
        "name",
        "algo_name",
        "symbol",
        "mode",
        "state",
        "interval_seconds",
        "is_active",
        "is_global",
        "allowed_plans_display",
        "created_by_admin",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "mode", "state", "is_active",
        "is_global",
        "created_by_admin",
        "created_at",
    )
    search_fields = ("name", "algo_name", "symbol", "user__email")
    ordering = ("-created_at",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "started_at",
        "stopped_at",
        "error_msg",
    )

    fieldsets = (
        ("Basic Info", {
            "fields": ("user", "broker", "name", "algo_name", "symbol", "symbols", "instrument_type"),
        }),
        ("Trading Config", {
            "fields": ("mode", "state", "interval_seconds", "timeframe", "default_lots", "risk_config", "parameters"),
        }),
        ("🌐 Global Strategy Settings", {
            "fields": ("is_global", "allowed_plans_selector", "created_by_admin"),
            "description": (
                "<b>Step 1:</b> <code>is_global = True</code> karo → strategy sab eligible users ko dikhe.<br>"
                "<b>Step 2:</b> Niche se plans choose karo (empty = sab plans).<br>"
                "<b>Step 3:</b> <code>created_by_admin = True</code> mark karo.<br>"
                "<br>"
                "<b>Plan → Tier mapping:</b> Free=0, Basic=1, Pro=2, Elite=3"
            ),
        }),
        ("Status", {
            "fields": ("is_active", "error_msg", "started_at", "stopped_at", "created_at", "updated_at"),
        }),
    )

    actions = [
        "mark_idle", "mark_running", "mark_error",
        "make_global", "make_private",
        "clear_allowed_plans",
    ]

    @admin.display(description="Allowed Plans")
    def allowed_plans_display(self, obj):
        if not obj.allowed_plans:
            return "✅ Sab plans"
        return ", ".join(obj.allowed_plans)

    def mark_idle(self, request, queryset):
        queryset.update(state=Strategy.State.IDLE)
    mark_idle.short_description = "Mark selected strategies as Idle"

    def mark_running(self, request, queryset):
        queryset.update(state=Strategy.State.RUNNING)
    mark_running.short_description = "Mark selected strategies as Running"

    def mark_error(self, request, queryset):
        queryset.update(state=Strategy.State.ERROR)
    mark_error.short_description = "Mark selected strategies as Error"

    def make_global(self, request, queryset):
        updated = queryset.update(is_global=True, created_by_admin=True)
        self.message_user(request, f"{updated} strateg(ies) ab GLOBAL hain.")
    make_global.short_description = "✅ Make GLOBAL (sab users ko dikhe)"

    def make_private(self, request, queryset):
        updated = queryset.update(is_global=False)
        self.message_user(request, f"{updated} strateg(ies) ab PRIVATE hain.")
    make_private.short_description = "🔒 Make PRIVATE (sirf owner ko dikhe)"

    def clear_allowed_plans(self, request, queryset):
        for strategy in queryset:
            strategy.allowed_plans = []
            strategy.save(update_fields=["allowed_plans"])
        self.message_user(
            request,
            f"{queryset.count()} strateg(ies) ke allowed_plans clear — ab sab plans ko dikhenge.",
        )
    clear_allowed_plans.short_description = "🗑️ Clear Allowed Plans"


# ─────────────────────────────────────────────────────────────────
#  Strategy Signal Admin
# ─────────────────────────────────────────────────────────────────
@admin.register(StrategySignal)
class StrategySignalAdmin(admin.ModelAdmin):
    list_display = ("id", "strategy", "signal_type", "symbol", "price", "result", "order", "created_at")
    list_filter = ("signal_type", "result", "created_at")
    search_fields = ("symbol", "reason", "strategy__name")
    ordering = ("-created_at",)


# ─────────────────────────────────────────────────────────────────
#  Strategy Performance Snapshot Admin
# ─────────────────────────────────────────────────────────────────
@admin.register(StrategyPerformanceSnapshot)
class StrategyPerformanceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("id", "strategy", "granularity", "period_start", "total_trades", "win_rate", "total_pnl", "total_fees", "created_at")
    list_filter = ("granularity", "period_start")
    search_fields = ("strategy__name",)
    ordering = ("-period_start",)


# ─────────────────────────────────────────────────────────────────
#  UserStrategyPreference Admin
# ─────────────────────────────────────────────────────────────────
@admin.register(UserStrategyPreference)
class UserStrategyPreferenceAdmin(admin.ModelAdmin):
    list_display = (
        "user", "strategy_name", "preferred_mode",
        "is_running", "created_at", "updated_at",
    )
    list_filter   = ("preferred_mode", "is_running", "strategy__algo_name")
    search_fields = ("user__email", "strategy__name")
    ordering      = ("-updated_at",)
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Strategy")
    def strategy_name(self, obj):
        return f"{obj.strategy.name} ({obj.strategy.algo_name})"

    actions = ["set_paper", "set_live", "stop_all"]

    def set_paper(self, request, queryset):
        updated = queryset.update(preferred_mode="paper")
        self.message_user(request, f"{updated} preferences → Paper mode")
    set_paper.short_description = "📄 Set Paper mode"

    def set_live(self, request, queryset):
        updated = queryset.update(preferred_mode="live")
        self.message_user(request, f"{updated} preferences → Live mode")
    set_live.short_description = "⚡ Set Live mode"

    def stop_all(self, request, queryset):
        updated = queryset.update(is_running=False)
        self.message_user(request, f"{updated} preferences stopped")
    stop_all.short_description = "⏹ Stop All"