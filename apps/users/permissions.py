from django.utils import timezone

from rest_framework.permissions import BasePermission


class IsVerified(BasePermission):
    message = "Email verification required"

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.is_verified
        )


class HasActivePlan(BasePermission):
    message = "Active subscription required"

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_plan_active
        )


class IsPro(BasePermission):
    message = "Pro plan required"

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.plan in ("pro", "elite")
            and request.user.is_plan_active
        )
