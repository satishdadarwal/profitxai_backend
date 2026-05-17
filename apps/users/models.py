# apps/users/models.py

import uuid

from django.contrib.auth.models import (
    AbstractBaseUser,
    BaseUserManager,
    PermissionsMixin,
)
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=15, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_verified = models.BooleanField(default=False)  # email verified
    date_joined = models.DateTimeField(default=timezone.now)

    # Subscription
    plan = models.CharField(
        max_length=20,
        choices=[
            ("free", "Free"),
            ("basic", "Basic"),
            ("pro", "Pro"),
            ("elite", "Elite"),
        ],
        default="free",
    )
    plan_expires = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    class Meta:
        db_table = "users"

    def __str__(self):
        return self.email

    @property
    def is_plan_active(self):
        if self.plan == "free":
            return True
        return self.plan_expires and self.plan_expires > timezone.now()


class OTPCode(models.Model):
    """Email/SMS OTP for verification and password reset."""

    PURPOSE_CHOICES = [
        ("verify", "Email Verify"),
        ("reset", "Password Reset"),
        ("login", "2FA Login"),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="otps")
    code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=10, choices=PURPOSE_CHOICES)
    is_used = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "otp_codes"
        ordering = ["-created_at"]

    def is_valid(self):
        return not self.is_used and self.expires_at > timezone.now()


class BrokerCredential(models.Model):
    """
    Encrypted broker credentials per user.
    One user can have multiple brokers.
    New brokers can be added without schema changes.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="broker_credentials"
    )
    broker_slug = models.CharField(max_length=30)  # 'fyers', 'delta', 'zerodha', etc.
    label = models.CharField(max_length=50, blank=True)  # e.g. "My Zerodha"
    # Credentials stored as encrypted JSON — see core/security.py
    credentials = models.TextField()  # EncryptedTextField in prod
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    last_used = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "broker_credentials"
        unique_together = [("user", "broker_slug", "label")]

    def __str__(self):
        return f"{self.user.email} — {self.broker_slug}"
