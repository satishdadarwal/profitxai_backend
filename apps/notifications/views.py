from django.shortcuts import render
from django.utils import timezone

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Notification, NotificationPreference

# Create your views here.
# apps/notifications/views.py




# ── Serializers ──────────────────────────────────────────────────
class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "title",
            "body",
            "level",
            "category",
            "is_read",
            "metadata",
            "created_at",
            "read_at",
        ]


class PreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = ["email_enabled", "ws_enabled", "push_enabled", "disabled_categories"]


# ── Views ────────────────────────────────────────────────────────
class NotificationListView(APIView):
    """
    GET  /notifications/          → user ki saari notifications (unread first)
    POST /notifications/mark-all/ → sab read mark karo
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(user=request.user)

        if request.query_params.get("unread") == "1":
            qs = qs.filter(is_read=False)
        if cat := request.query_params.get("category"):
            qs = qs.filter(category=cat)

        unread_count = Notification.objects.filter(
            user=request.user, is_read=False
        ).count()
        data = NotificationSerializer(qs[:50], many=True).data

        return Response({"unread_count": unread_count, "results": data})


class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, notif_id=None):
        """
        POST /notifications/<id>/read/  → single notification mark read
        POST /notifications/read-all/   → sab mark read
        """
        now = timezone.now()

        if notif_id:
            updated = Notification.objects.filter(
                pk=notif_id, user=request.user, is_read=False
            ).update(is_read=True, read_at=now)
            return Response({"marked": updated})

        # Bulk mark all
        updated = Notification.objects.filter(user=request.user, is_read=False).update(
            is_read=True, read_at=now
        )
        return Response({"marked": updated})


class NotificationPreferenceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
        return Response(PreferenceSerializer(prefs).data)

    def patch(self, request):
        prefs, _ = NotificationPreference.objects.get_or_create(user=request.user)
        ser = PreferenceSerializer(prefs, data=request.data, partial=True)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        ser.save()
        return Response(ser.data)


# ── urls.py ──────────────────────────────────────────────────────
from django.urls import path

app_name = "notifications"

urlpatterns = [
    path("", NotificationListView.as_view(), name="list"),
    path("<uuid:notif_id>/read/", NotificationMarkReadView.as_view(), name="mark_read"),
    path("read-all/", NotificationMarkReadView.as_view(), name="mark_all_read"),
    path("preferences/", NotificationPreferenceView.as_view(), name="preferences"),
]
