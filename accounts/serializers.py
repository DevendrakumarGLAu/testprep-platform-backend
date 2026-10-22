from django.contrib.auth import password_validation
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import ActivityLog, PlatformSettings, Role, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "phone", "role", "is_active", "date_joined"]
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    """Public self-registration. Always creates a student."""

    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["username", "email", "password", "first_name", "last_name", "phone"]
        extra_kwargs = {"first_name": {"required": True}}

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate(self, attrs):
        probe = User(username=attrs.get("username"), email=attrs.get("email"), first_name=attrs.get("first_name", ""))
        password_validation.validate_password(attrs["password"], probe)
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(role=Role.STUDENT, password=password, **validated_data)


class ManagedUserSerializer(serializers.ModelSerializer):
    """Used by admins to manage accounts. Role limits are enforced in the view."""

    password = serializers.CharField(write_only=True, required=False, style={"input_type": "password"})

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "first_name", "last_name", "phone",
            "role", "is_active", "date_joined", "password",
        ]
        read_only_fields = ["id", "date_joined"]

    def validate_email(self, value):
        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate(self, attrs):
        if not self.instance and not attrs.get("password"):
            raise serializers.ValidationError({"password": "Password is required."})
        if attrs.get("password"):
            password_validation.validate_password(attrs["password"], self.instance)
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        return User.objects.create_user(password=password, **validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value


class LoginSerializer(TokenObtainPairSerializer):
    """Accepts username or email; returns tokens plus the user profile."""

    def validate(self, attrs):
        ident = attrs.get(self.username_field, "")
        if "@" in ident:
            match = User.objects.filter(email__iexact=ident).first()
            if match:
                attrs[self.username_field] = match.get_username()
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        return token


class ActivityLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.username", default=None, read_only=True)

    class Meta:
        model = ActivityLog
        fields = ["id", "actor", "actor_name", "action", "target_type", "target_id", "detail", "created_at"]


class PlatformSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformSettings
        fields = ["platform_name", "allow_registration", "support_email"]

    def validate_platform_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("The platform name cannot be empty.")
        return value
