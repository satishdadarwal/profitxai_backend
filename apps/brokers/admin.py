# apps/brokers/admin.py

import hashlib
import pyotp
import requests

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.conf import settings

from .models import BrokerAccount

FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"


@admin.register(BrokerAccount)
class BrokerAccountAdmin(admin.ModelAdmin):
    list_display  = (
        "id", "user", "broker", "label",
        "is_active", "is_verified",
        "token_status",
        "fyers_login_button",
        "updated_at",
    )
    list_filter   = ("broker", "is_active", "is_verified")
    search_fields = ("user__email", "broker", "label")
    ordering      = ("-updated_at",)
    readonly_fields = (
        "created_at", "updated_at",
        "token_preview", "fyers_login_link",
    )

    fieldsets = (
        ("Account Info", {
            "fields": ("user", "broker", "label", "is_active", "is_verified"),
        }),
        ("Fyers Credentials", {
            "fields": ("app_id", "secret_key", "redirect_uri"),
        }),
        ("Tokens", {
            "fields": ("token_preview", "refresh_token", "token_expiry"),
            "classes": ("collapse",),
        }),
        ("Auto-Refresh Auth", {
            "fields": ("fyers_pin", "totp_secret"),
            "description": (
                "TOTP secret ya PIN save karo — "
                "daily auto-refresh ke liye (jab SEBI allow kare)"
            ),
        }),
        ("Quick Actions", {
            "fields": ("fyers_login_link",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    actions = ["mark_active", "mark_inactive", "generate_fyers_login_url"]

    # ── List display helpers ──────────────────────────────────

    def token_status(self, obj):
        if obj.access_token:
            return format_html(
                '<span style="color:green;font-weight:bold">✅ Token Set</span>'
            )
        return format_html(
            '<span style="color:red;font-weight:bold">❌ No Token</span>'
        )
    token_status.short_description = "Token"

    def fyers_login_button(self, obj):
        if obj.broker != "fyers":
            return "-"
        url = reverse("admin:brokers_fyers_login", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" '
            'style="background:#417690;color:white;padding:4px 10px;'
            'border-radius:4px;text-decoration:none;font-size:12px;">'
            '🔐 Login / Refresh Token</a>',
            url
        )
    fyers_login_button.short_description = "Fyers Action"
    fyers_login_button.allow_tags = True

    # ── Detail view helpers ───────────────────────────────────

    def token_preview(self, obj):
        if obj.access_token:
            preview = obj.access_token[:20] + "..." + obj.access_token[-10:]
            return format_html(
                '<code style="font-size:11px">{}</code>'
                '<span style="color:green;margin-left:10px">✅ Active</span>',
                preview
            )
        return format_html(
            '<span style="color:red">❌ No access token — Login karein</span>'
        )
    token_preview.short_description = "Access Token"

    def fyers_login_link(self, obj):
        if obj.broker != "fyers":
            return "N/A"
        url = reverse("admin:brokers_fyers_login", args=[obj.pk])
        totp_available = bool(getattr(obj, "totp_secret", ""))
        totp_note = "✅ TOTP configured" if totp_available else "⚠️ TOTP not set"
        return format_html(
            '<a class="button" href="{}" '
            'style="background:#28a745;color:white;padding:8px 20px;'
            'border-radius:4px;text-decoration:none;font-size:14px;">'
            '🔐 Open Fyers Login Page</a>'
            '<br><small style="color:#666;margin-top:5px;display:block">'
            '{} — Login ke baad token automatically save hoga</small>',
            url, totp_note
        )
    fyers_login_link.short_description = "Fyers Login"

    # ── Custom URLs ───────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:pk>/fyers-login/",
                self.admin_site.admin_view(self.fyers_login_view),
                name="brokers_fyers_login",
            ),
        ]
        return custom + urls

    # ── Fyers Login View — browser mein login page kholo ─────

    def fyers_login_view(self, request, pk):
        try:
            account = BrokerAccount.objects.get(pk=pk)
        except BrokerAccount.DoesNotExist:
            messages.error(request, "Account not found")
            return HttpResponseRedirect(
                reverse("admin:brokers_brokeraccount_changelist")
            )

        if account.broker != "fyers":
            messages.error(request, "Ye sirf Fyers accounts ke liye hai")
            return HttpResponseRedirect(
                reverse("admin:brokers_brokeraccount_change", args=[pk])
            )

        # TOTP generate karo agar available ho
        totp_code = ""
        totp_secret = getattr(account, "totp_secret", "") or ""
        if totp_secret:
            try:
                totp_code = pyotp.TOTP(totp_secret).now()
            except Exception:
                totp_code = ""

        # Auth URL banao
        redirect_uri = account.redirect_uri or settings.FYERS_REDIRECT_URI
        from urllib.parse import quote
        auth_url = (
            f"https://api-t1.fyers.in/api/v3/generate-authcode"
            f"?client_id={account.app_id}"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
            f"&response_type=code"
            f"&state=master_setup"
        )

        # HTML page return karo
        totp_section = ""
        if totp_code:
            totp_section = f"""
                <div style="background:#d4edda;border:1px solid #c3e6cb;
                            border-radius:8px;padding:20px;margin:20px 0;
                            text-align:center;">
                    <h3 style="margin:0 0 10px;color:#155724">
                        🔑 TOTP Code (Auto-Generated)
                    </h3>
                    <div style="font-size:42px;font-weight:bold;
                                letter-spacing:8px;color:#155724;
                                font-family:monospace;">
                        {totp_code}
                    </div>
                    <p style="color:#155724;margin:10px 0 0">
                        ⏱ Ye code 30 sec mein expire hoga —
                        jaldi login karo
                    </p>
                    <button onclick="navigator.clipboard.writeText('{totp_code}')"
                            style="margin-top:10px;padding:8px 20px;
                                   background:#155724;color:white;
                                   border:none;border-radius:4px;
                                   cursor:pointer;font-size:14px;">
                        📋 Copy TOTP
                    </button>
                </div>
            """
        else:
            totp_section = """
                <div style="background:#fff3cd;border:1px solid #ffc107;
                            border-radius:8px;padding:15px;margin:20px 0;">
                    <strong>⚠️ TOTP not configured</strong><br>
                    <small>Account mein TOTP secret save karo for auto-generation.
                    Abhi manually Authenticator app se copy karo.</small>
                </div>
            """

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Fyers Daily Login — ProfitxAI Admin</title>
            <meta charset="utf-8">
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont,
                                 'Segoe UI', sans-serif;
                    max-width: 600px;
                    margin: 40px auto;
                    padding: 20px;
                    background: #f8f9fa;
                    color: #333;
                }}
                .card {{
                    background: white;
                    border-radius: 12px;
                    padding: 30px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                }}
                h1 {{ color: #417690; margin-top: 0; }}
                .info-row {{
                    display: flex;
                    justify-content: space-between;
                    padding: 8px 0;
                    border-bottom: 1px solid #eee;
                }}
                .info-label {{ color: #666; font-size: 14px; }}
                .info-value {{ font-weight: bold; font-size: 14px; }}
                .btn-login {{
                    display: block;
                    width: 100%;
                    padding: 15px;
                    background: #28a745;
                    color: white;
                    text-align: center;
                    text-decoration: none;
                    border-radius: 8px;
                    font-size: 18px;
                    font-weight: bold;
                    margin: 20px 0;
                    box-sizing: border-box;
                }}
                .btn-login:hover {{ background: #218838; }}
                .btn-back {{
                    color: #417690;
                    text-decoration: none;
                    font-size: 14px;
                }}
                .steps {{
                    background: #e8f4f8;
                    border-radius: 8px;
                    padding: 15px 20px;
                    margin: 15px 0;
                }}
                .steps ol {{ margin: 5px 0; padding-left: 20px; }}
                .steps li {{ margin: 5px 0; font-size: 14px; }}
            </style>
            <script>
                // Auto-refresh page every 25 sec for new TOTP
                setTimeout(function() {{
                    if (confirm('TOTP expire hone wala hai. Naya TOTP lene ke liye page refresh karein?')) {{
                        location.reload();
                    }}
                }}, 25000);
            </script>
        </head>
        <body>
            <div class="card">
                <h1>🔐 Fyers Daily Login</h1>

                <div class="info-row">
                    <span class="info-label">Account</span>
                    <span class="info-value">{account.label} (#{account.id})</span>
                </div>
                <div class="info-row">
                    <span class="info-label">App ID</span>
                    <span class="info-value">{account.app_id}</span>
                </div>
                <div class="info-row">
                    <span class="info-label">Token Status</span>
                    <span class="info-value" style="color:{'green' if account.access_token else 'red'}">
                        {'✅ Active' if account.access_token else '❌ Not Set'}
                    </span>
                </div>

                {totp_section}

                <div class="steps">
                    <strong>📋 Steps:</strong>
                    <ol>
                        <li>Neeche "Open Fyers Login" button click karo</li>
                        <li>Fyers mein ID + PIN + TOTP daalo</li>
                        <li>Login ke baad green page aayega ✅</li>
                        <li>Wapas aao aur server restart karo</li>
                    </ol>
                </div>

                <a href="{auth_url}" target="_blank" class="btn-login">
                    🚀 Open Fyers Login Page
                </a>

                <div style="text-align:center;margin-top:10px">
                    <small style="color:#666">
                        Login ke baad token automatically DB mein save hoga
                    </small>
                </div>

                <hr style="margin:20px 0;border:none;border-top:1px solid #eee">

                <a href="{reverse('admin:brokers_brokeraccount_changelist')}"
                   class="btn-back">
                    ← Back to Broker Accounts
                </a>
            </div>
        </body>
        </html>
        """

        from django.http import HttpResponse
        return HttpResponse(html)

    # ── Bulk Actions ──────────────────────────────────────────

    def mark_active(self, request, queryset):
        queryset.update(is_active=True)
        self.message_user(request, f"{queryset.count()} accounts marked active")
    mark_active.short_description = "✅ Mark Active"

    def mark_inactive(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, f"{queryset.count()} accounts marked inactive")
    mark_inactive.short_description = "❌ Mark Inactive"

    def generate_fyers_login_url(self, request, queryset):
        fyers_accounts = queryset.filter(broker="fyers")
        if not fyers_accounts.exists():
            self.message_user(request, "Koi Fyers account select nahi hua", messages.WARNING)
            return
        account = fyers_accounts.first()
        return HttpResponseRedirect(
            reverse("admin:brokers_fyers_login", args=[account.pk])
        )
    generate_fyers_login_url.short_description = "🔐 Fyers Login Page Kholo"