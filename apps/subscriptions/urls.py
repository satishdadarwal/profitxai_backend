from django.urls import include, path

from .views import SubscriptionStatusView  # ← ye add karo
from .views import (  # RazorpayWebhookView,
    CancelSubscriptionView,
    ChangePlanView,
    CreateOrderView,
    CurrentSubscriptionView,
    PaymentHistoryView,
    PlanListView,
    VerifyPaymentView,
)

urlpatterns = [
    path("plans/", PlanListView.as_view(), name="plan_list"),
    path("me/", CurrentSubscriptionView.as_view(), name="current"),
    path("status/", SubscriptionStatusView.as_view(), name="status"),  # ← ye add karo
    path("orders/", CreateOrderView.as_view(), name="create_order"),
    path("verify/", VerifyPaymentView.as_view(), name="verify_payment"),
    path("change-plan/", ChangePlanView.as_view(), name="change_plan"),
    path("cancel/", CancelSubscriptionView.as_view(), name="cancel"),
    path("payments/", PaymentHistoryView.as_view(), name="payment_history"),
    # path("webhook/razorpay/", RazorpayWebhookView.as_view(),   name="razorpay_webhook"),
]
