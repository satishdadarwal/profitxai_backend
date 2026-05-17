from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import BrokerCredential, OTPCode, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # Fields shown in list view
    list_display = (
        "id",
        "email",
        "full_name",
        "phone",
        "is_active",
        "is_staff",
        "is_verified",
        "plan",
        "plan_expires",
        "date_joined",
    )
    list_filter = ("is_active", "is_staff", "is_verified", "plan")
    search_fields = ("email", "full_name", "phone")
    ordering = ("-date_joined",)

    # Fieldsets for detail view
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("full_name", "phone")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "is_verified")},
        ),
        ("Plan", {"fields": ("plan", "plan_expires")}),
        ("Important dates", {"fields": ("date_joined",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "password1",
                    "password2",
                    "is_staff",
                    "is_superuser",
                ),
            },
        ),
    )
    readonly_fields = ("date_joined",)


@admin.register(OTPCode)
class OTPCodeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "code",
        "purpose",
        "is_used",
        "expires_at",
        "created_at",
    )
    list_filter = ("purpose", "is_used")
    search_fields = ("user__email", "code")
    ordering = ("-created_at",)


@admin.register(BrokerCredential)
class BrokerCredentialAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "broker_slug",
        "label",
        "is_active",
        "is_verified",
        "last_used",
        "created_at",
        "updated_at",
    )
    list_filter = ("broker_slug", "is_active", "is_verified")
    search_fields = ("user__email", "broker_slug", "label")
    ordering = ("-created_at",)
