# apps/notifications/models.py

import uuid

from django.contrib.auth import get_user_model
from django.db import models

User = get_user_model()


class NotificationPreference(models.Model):
    """Per-user notification channel preferences."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="notification_prefs"
    )
    email_enabled = models.BooleanField(default=True)
    ws_enabled = models.BooleanField(default=True)
    push_enabled = models.BooleanField(default=False)  # FCM / APNs

    # Granular category toggles (stored as JSON set)
    disabled_categories = models.JSONField(default=list, blank=True)

    def is_category_enabled(self, category: str) -> bool:
        return category not in (self.disabled_categories or [])

    def __str__(self):
        return f"NotifPrefs({self.user})"


class Notification(models.Model):
    """Persistent notification record — shown in the in-app notification centre."""

    class Level(models.TextChoices):
        INFO = "info", "Info"
        SUCCESS = "success", "Success"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notifications"
    )
    title = models.CharField(max_length=255)
    body = models.TextField()
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.INFO)
    category = models.CharField(max_length=50, default="general")
    is_read = models.BooleanField(default=False, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read"])]

    def __str__(self):
        return f"[{self.level}] {self.title} → {self.user}"
