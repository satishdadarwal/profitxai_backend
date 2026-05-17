from django.contrib import admin

from .models import PaymentLog, Plan, RazorpayWebhookEvent, Subscription


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "tier",
        "billing_cycle",
        "price_inr",
        "is_active",
        "created_at",
    )
    list_filter = ("tier", "billing_cycle", "is_active")
    search_fields = ("name", "razorpay_plan_id")
    ordering = ("tier", "billing_cycle")
    readonly_fields = ("created_at",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "plan",
        "status",
        "current_period_start",
        "current_period_end",
        "grace_until",
        "trial_end",
        "cancelled_at",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "plan__tier", "plan__billing_cycle")
    search_fields = ("user__username", "user__email", "razorpay_subscription_id")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    # Custom actions
    actions = ["mark_active", "mark_cancelled", "mark_expired"]

    def mark_active(self, request, queryset):
        queryset.update(status=Subscription.Status.ACTIVE)

    mark_active.short_description = "Mark selected subscriptions as Active"

    def mark_cancelled(self, request, queryset):
        queryset.update(status=Subscription.Status.CANCELLED)

    mark_cancelled.short_description = "Mark selected subscriptions as Cancelled"

    def mark_expired(self, request, queryset):
        queryset.update(status=Subscription.Status.EXPIRED)

    mark_expired.short_description = "Mark selected subscriptions as Expired"


@admin.register(PaymentLog)
class PaymentLogAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subscription",
        "user",
        "razorpay_payment_id",
        "amount_inr",
        "currency",
        "payment_status",
        "created_at",
    )
    list_filter = ("payment_status", "currency")
    search_fields = ("razorpay_order_id", "razorpay_payment_id", "user__username")
    ordering = ("-created_at",)


@admin.register(RazorpayWebhookEvent)
class RazorpayWebhookEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_type", "processed_at")
    list_filter = ("event_type",)
    search_fields = ("event_id", "event_type")
    ordering = ("-processed_at",)
