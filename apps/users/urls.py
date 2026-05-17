from django.urls import path

from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("register/", views.RegisterView.as_view()),
    path("login/", views.LoginView.as_view()),
    path("token/refresh/", TokenRefreshView.as_view()),
    path("otp/verify/", views.OTPVerifyView.as_view()),
    path("otp/resend/", views.OTPResendView.as_view()),
    path("forgot-password/", views.ForgotPasswordView.as_view()),
    path("reset-password/", views.ResetPasswordView.as_view()),
    path("profile/", views.ProfileView.as_view()),
    path("brokers/", views.BrokerListView.as_view()),
    path("brokers/connected/", views.UserBrokerView.as_view()),
    path("brokers/connected/<int:pk>/", views.BrokerDeleteView.as_view()),
    path(
        "brokers/<int:pk>/remove/", views.BrokerDeleteView.as_view()
    ),  # ← frontend's URL
    path("brokers/<str:broker_slug>/verify/", views.BrokerVerifyView.as_view()),
]
