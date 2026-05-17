# apps/subscriptions/permissions.py

import logging

from rest_framework.permissions import BasePermission

from .models import Plan, Subscription

logger = logging.getLogger(__name__)

# Tier aliases for readable code
TIER_FREE = Plan.Tier.FREE
TIER_BASIC = Plan.Tier.BASIC
TIER_PRO = Plan.Tier.PRO
TIER_ELITE = Plan.Tier.ELITE


# ─────────────────────────────────────────────────────────────────
#  Internal helper — centralised subscription fetch
# ─────────────────────────────────────────────────────────────────
def _get_active_subscription(user) -> Subscription | None:
    """
    Returns the subscription if user currently has active access.
    Returns None if no subscription or access is not granted.
    """
    sub = getattr(user, "subscription", None)
    if sub is None:
        return None
    return sub if sub.is_access_granted else None


def _subscription_message(user) -> str:
    """Human-readable denial reason."""
    sub = getattr(user, "subscription", None)
    if sub is None:
        return "No active subscription found. Please subscribe to access this feature."
    if not sub.is_access_granted:
        return (
            f"Your {sub.plan.name} subscription is {sub.status}. "
            "Please renew to continue using this feature."
        )
    return "Your current plan does not include this feature."


# ─────────────────────────────────────────────────────────────────
#  1. CanAddBroker
#  Checks: plan.feature_limits["max_brokers"] > user's current count
# ─────────────────────────────────────────────────────────────────
class CanAddBroker(BasePermission):
    """
    User apne plan ki broker limit ke andar hi brokers add kar sakta hai.

    View mein broker count inject karna ho toh:
        request.broker_count = Broker.objects.filter(user=request.user).count()
    Otherwise permission class DB se fetch karega.
    """

    message = "You have reached the maximum number of brokers allowed on your plan."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        sub = _get_active_subscription(request.user)
        if sub is None:
            self.message = _subscription_message(request.user)
            return False

        max_brokers = sub.get_limit("max_brokers", default=0)

        # -1 = unlimited (ELITE plan convention)
        if max_brokers == -1:
            return True

        if max_brokers == 0:
            self.message = (
                f"Your {sub.plan.name} plan does not allow adding brokers. "
                "Please upgrade to a higher plan."
            )
            return False

        # Count existing brokers — lazy import avoids circular deps
        current_count = _get_broker_count(request.user)

        if current_count >= max_brokers:
            self.message = (
                f"Your {sub.plan.name} plan allows up to {max_brokers} broker(s). "
                f"You have {current_count}. Upgrade to add more."
            )
            return False

        return True


# ─────────────────────────────────────────────────────────────────
#  2. CanAddStrategy
#  Checks: plan.feature_limits["max_strategies"] > user's current count
# ─────────────────────────────────────────────────────────────────
class CanAddStrategy(BasePermission):
    """
    User apne plan ki strategy limit ke andar hi strategies add kar sakta hai.
    """

    message = "You have reached the maximum number of strategies allowed on your plan."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        sub = _get_active_subscription(request.user)
        if sub is None:
            self.message = _subscription_message(request.user)
            return False

        max_strategies = sub.get_limit("max_strategies", default=0)

        if max_strategies == -1:
            return True

        if max_strategies == 0:
            self.message = (
                f"Your {sub.plan.name} plan does not allow creating strategies. "
                "Please upgrade your plan."
            )
            return False

        current_count = _get_strategy_count(request.user)

        if current_count >= max_strategies:
            self.message = (
                f"Your {sub.plan.name} plan allows up to {max_strategies} strategy(ies). "
                f"You have {current_count}. Upgrade to add more."
            )
            return False

        return True


# ─────────────────────────────────────────────────────────────────
#  3. IsProOrAbove
#  Tier >= PRO (2)
# ─────────────────────────────────────────────────────────────────
class IsProOrAbove(BasePermission):
    """
    Sirf PRO aur ELITE tier users ko access deta hai.
    Use for: advanced analytics, AI signals, priority support features.
    """

    message = "This feature requires a Pro or Elite subscription."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        sub = _get_active_subscription(request.user)
        if sub is None:
            self.message = _subscription_message(request.user)
            return False

        if sub.tier < TIER_PRO:
            self.message = (
                f"This feature is available on Pro and Elite plans. "
                f"You are on {sub.plan.name}. Please upgrade."
            )
            return False

        return True


# ─────────────────────────────────────────────────────────────────
#  4. CanLiveTrade
#  plan.feature_limits["live_trading"] == True  AND  tier >= PRO
# ─────────────────────────────────────────────────────────────────
class CanLiveTrade(BasePermission):
    """
    Live trading sirf un plans pe allow hai jisme live_trading=true ho.
    Paper trading hamesha allowed hai (orders/services.py check karta hai mode=paper).

    Use on:
        class LiveOrderCreateView(APIView):
            permission_classes = [IsAuthenticated, CanLiveTrade]
    """

    message = "Live trading is not available on your current plan."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        sub = _get_active_subscription(request.user)
        if sub is None:
            self.message = _subscription_message(request.user)
            return False

        if not sub.plan.allows_live_trading:
            self.message = (
                f"Live trading is not available on the {sub.plan.name} plan. "
                "Upgrade to Pro or Elite to enable live trading."
            )
            return False

        return True


# ─────────────────────────────────────────────────────────────────
#  5. HasActiveSubscription  (base gating — any paid plan)
# ─────────────────────────────────────────────────────────────────
class HasActiveSubscription(BasePermission):
    """
    Sirf check karta hai ki subscription active hai ya nahi.
    Free plan pe bhi True hoga agar access_granted hai.
    """

    message = "An active subscription is required to access this feature."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        sub = _get_active_subscription(request.user)
        if sub is None:
            self.message = _subscription_message(request.user)
            return False

        return True


# ─────────────────────────────────────────────────────────────────
#  Lazy DB helpers (avoid circular imports at module load time)
# ─────────────────────────────────────────────────────────────────
def _get_broker_count(user) -> int:
    try:
        from apps.brokers.models import Broker

        return Broker.objects.filter(user=user, is_active=True).count()
    except ImportError:
        logger.warning("apps.brokers not found — broker count defaulting to 0")
        return 0


def _get_strategy_count(user) -> int:
    try:
        from apps.strategies.models import Strategy

        return Strategy.objects.filter(user=user, is_active=True).count()
    except ImportError:
        logger.warning("apps.strategies not found — strategy count defaulting to 0")
        return 0


class CanBacktest(BasePermission):
    """
    Backtesting sirf un plans pe allow hai jisme backtest=true ho.
    """

    message = "Backtesting is not available on your current plan."

    def has_permission(self, request, view) -> bool:
        if not request.user or not request.user.is_authenticated:
            return False

        sub = _get_active_subscription(request.user)
        if sub is None:
            self.message = _subscription_message(request.user)
            return False

        if not sub.plan.allows_backtest:
            self.message = (
                f"Backtesting is not available on the {sub.plan.name} plan. "
                "Upgrade to Pro or Elite to enable backtesting."
            )
            return False

        return True
