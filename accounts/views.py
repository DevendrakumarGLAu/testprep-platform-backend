from rest_framework import generics, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import ActivityLog, PlatformSettings, Role, User, log_activity
from .permissions import IsAdminOrSuperAdmin, IsSuperAdmin
from .serializers import (
    ActivityLogSerializer, ChangePasswordSerializer, LoginSerializer,
    ManagedUserSerializer, PlatformSettingsSerializer, RegisterSerializer, UserSerializer,
)


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    throttle_scope = "auth"


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def create(self, request, *args, **kwargs):
        if not PlatformSettings.load().allow_registration:
            return Response({"detail": "Registration is closed. Ask your administrator for an account."}, status=403)
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user

    def update(self, request, *args, **kwargs):
        # Only profile fields are self-editable; role/status never.
        user = self.get_object()
        for k in ("first_name", "last_name", "phone"):
            if k in request.data:
                setattr(user, k, request.data[k])
        user.save()
        return Response(UserSerializer(user).data)


class ChangePasswordView(generics.GenericAPIView):
    serializer_class = ChangePasswordSerializer

    def post(self, request):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        request.user.set_password(ser.validated_data["new_password"])
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
