from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from . import captcha, emails
from .models import ActivityLog, PlatformSettings, Role, User, log_activity
from .permissions import IsAdminOrSuperAdmin, IsSuperAdmin
from .serializers import (
    ActivityLogSerializer, ChangePasswordSerializer, LoginSerializer,
    ManagedUserSerializer, PlatformSettingsSerializer, ProfileSerializer, RegisterSerializer, UserSerializer,
)


GENERIC_RESET_REPLY = "If that email belongs to an account, a link to reset the password has been sent to it."
NO_EMAIL_REPLY = "Emails are not set up on this site. Ask your administrator to reset your password."


class ForgotPasswordView(APIView):
    """POST {email, captcha}: emails a reset link. The reply is the same whether or not the address has an account."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        captcha.require(request)
        email = str(request.data.get("email") or "").strip().lower()
        if not email:
            return Response({"email": ["Enter your email address."]}, status=400)
        if not emails.email_enabled():
            return Response({"detail": NO_EMAIL_REPLY}, status=503)
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user:
            try:
                emails.send_password_reset(user)
            except Exception:  # noqa: BLE001 - never tell a stranger whether the address exists, or that sending failed
                pass
        return Response({"detail": GENERIC_RESET_REPLY})


class ResetPasswordView(APIView):
    """POST {uid, token, new_password}: sets a new password from an emailed link."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        user = emails.user_from_reset(str(request.data.get("uid") or ""), str(request.data.get("token") or ""))
        if user is None:
            return Response({"detail": "This link is invalid or has expired. Ask for a new one."}, status=400)
        password = str(request.data.get("new_password") or "")
        try:
            password_validation.validate_password(password, user)
        except DjangoValidationError as e:
            return Response({"new_password": list(e.messages)}, status=400)
        user.set_password(password)
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        emails.end_all_sessions(user)  # anyone holding the old password is signed out
        log_activity(user, "user.password_reset", user, "by email link")
        return Response({"detail": "Your password was changed. You can sign in now."})


class SendVerificationView(APIView):
    """POST: emails the signed-in person a link to confirm their address."""

    throttle_scope = "auth"

    def post(self, request):
        user = request.user
        if user.email_verified:
            return Response({"detail": "Your email is already confirmed.", "already": True})
        if not emails.email_enabled():
            return Response({"detail": NO_EMAIL_REPLY}, status=503)
        try:
            emails.send_verification(user)
        except Exception:  # noqa: BLE001
            return Response({"detail": "The email could not be sent. Please try again later."}, status=502)
        return Response({"detail": f"We sent a link to {user.email}."})


class VerifyEmailView(APIView):
    """POST {token}: confirms an address from the emailed link. No sign-in needed, so it works from any device."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        user = emails.user_from_verification(str(request.data.get("token") or ""))
        if user is None:
            return Response({"detail": "This link is invalid or has expired. Sign in and ask for a new one."}, status=400)
        if not user.email_verified:
            user.email_verified = True
            user.save(update_fields=["email_verified"])
        return Response({"detail": "Your email is confirmed. Thank you."})


class CaptchaView(APIView):
    """A fresh captcha picture. {"enabled": false} when the captcha is switched off (CAPTCHA_ENABLED=False)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "captcha"

    def get(self, request):
        from django.conf import settings

        if not getattr(settings, "CAPTCHA_ENABLED", True):
            return Response({"enabled": False})
        return Response(captcha.new_challenge())


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        captcha.require(request)
        return super().post(request, *args, **kwargs)


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def create(self, request, *args, **kwargs):
        if not PlatformSettings.load().allow_registration:
            return Response({"detail": "Registration is closed. Ask your administrator for an account."}, status=403)
        captcha.require(request)
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        if emails.email_enabled():
            try:
                emails.send_verification(user)  # best effort: sign-up must not fail because the email did not go
            except Exception:  # noqa: BLE001
                pass
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user

    def update(self, request, *args, **kwargs):
        # Only name and phone are self-editable. Role, status, username and email are ignored if sent.
        user = self.get_object()
        ser = ProfileSerializer(user, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(UserSerializer(user).data)


class ChangePasswordView(generics.GenericAPIView):
    serializer_class = ChangePasswordSerializer

    def post(self, request):
        captcha.require(request)
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        request.user.set_password(ser.validated_data["new_password"])
        request.user.must_change_password = False
        request.user.save()
        return Response({"detail": "Password updated."})


class UserViewSet(viewsets.ModelViewSet):
    """
    Super Admin: manages all users (admins and students).
    Admin: manages students only.
    """

    serializer_class = ManagedUserSerializer
    permission_classes = [IsAdminOrSuperAdmin]
    filterset_fields = ["role", "is_active"]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering_fields = ["date_joined", "username", "email"]
    ordering = ["-date_joined"]

    def get_queryset(self):
        qs = User.objects.all()
        if self.request.user.role == Role.ADMIN:
            qs = qs.filter(role=Role.STUDENT)
        return qs

    def _check_role(self, role):
        if self.request.user.role == Role.ADMIN and role != Role.STUDENT:
            raise PermissionDenied("Admins can only manage student accounts.")

    def perform_create(self, serializer):
        self._check_role(serializer.validated_data.get("role", Role.STUDENT))
        user = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "user.create", user, f"role={user.role}")

    def perform_update(self, serializer):
        self._check_role(serializer.validated_data.get("role", serializer.instance.role))
        if serializer.instance.pk == self.request.user.pk and serializer.validated_data.get("is_active") is False:
            raise PermissionDenied("You cannot deactivate your own account.")
        user = serializer.save()
        log_activity(self.request.user, "user.update", user)

    def perform_destroy(self, instance):
        if instance.pk == self.request.user.pk:
            raise PermissionDenied("You cannot delete your own account.")
        log_activity(self.request.user, "user.deactivate", instance, "soft delete")
        instance.is_active = False  # preserve attempt history; never hard-delete
        instance.save(update_fields=["is_active"])

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """Gives the person a temporary password (shown once) and signs them out. They must choose their own at next sign-in."""
        user = self.get_object()
        if user.pk == request.user.pk:
            raise PermissionDenied("Use Change password in your profile for your own account.")
        self._check_role(user.role)
        temporary = emails.new_temporary_password()
        user.set_password(temporary)
        user.must_change_password = True
        user.save(update_fields=["password", "must_change_password"])
        emails.end_all_sessions(user)
        log_activity(request.user, "user.reset_password", user)
        return Response({"temporary_password": temporary, "username": user.username})

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        user.is_active = True
        user.save(update_fields=["is_active"])
        log_activity(request.user, "user.activate", user)
        return Response(UserSerializer(user).data)


class ActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ActivityLog.objects.select_related("actor")
    serializer_class = ActivityLogSerializer
    permission_classes = [IsSuperAdmin]
    filterset_fields = ["action", "actor"]
    search_fields = ["action", "detail"]


class LogoutView(generics.GenericAPIView):
    """Retires the refresh token so it cannot be used again. Always succeeds from the client's point of view."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        try:
            RefreshToken(request.data.get("refresh", "")).blacklist()
        except TokenError:
            pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlatformView(generics.GenericAPIView):
    """Public read (the login page needs the name and whether sign-up is open); Super Admin write."""

    serializer_class = PlatformSettingsSerializer

    def get_permissions(self):
        return [AllowAny()] if self.request.method == "GET" else [IsSuperAdmin()]

    def get_authenticators(self):
        # Reading is anonymous-friendly; a bad or expired token must not break the login page.
        return [] if self.request.method == "GET" else super().get_authenticators()

    def get(self, request):
        return Response(self.get_serializer(PlatformSettings.load()).data)

    def patch(self, request):
        obj = PlatformSettings.load()
        ser = self.get_serializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_activity(request.user, "platform.update", obj, str(dict(ser.validated_data)))
        return Response(ser.data)

    put = patch
