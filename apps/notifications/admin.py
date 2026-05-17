from django.contrib import admin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "user", "created_at", "is_read")
    list_filter = ("is_read", "created_at")
    search_fields = ("title", "message", "user__username")
    ordering = ("-created_at",)

    # Optional: read-only fields
    readonly_fields = ("created_at",)
