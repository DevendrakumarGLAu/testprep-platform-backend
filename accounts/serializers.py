import re

from django.contrib.auth import password_validation
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import ActivityLog, PlatformSettings, Role, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "phone", "daily_goal_questions", "preferred_language", "weekly_summary", "email_verified", "must_change_password", "role", "is_active", "date_joined"]
        read_only_fields = fields


class ProfileSerializer(serializers.ModelSerializer):
    """What a person may change about themselves. Username, email and role are not on this list."""

    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone", "daily_goal_questions", "preferred_language", "weekly_summary"]
        extra_kwargs = {k: {"required": False} for k in fields}

    def validate_first_name(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("First name cannot be empty.")
        return value

    def validate_last_name(self, value):
        return " ".join(value.split())

    def validate_daily_goal_questions(self, value):
        if not 5 <= value <= 500:
            raise serializers.ValidationError("Choose a daily goal between 5 and 500 questions.")
        return value

    def validate_phone(self, value):
        value = value.strip()
        if value and not re.fullmatch(r"\+?[0-9][0-9 ()\-]{6,18}[0-9]", value):
            raise serializers.ValidationError("Enter a valid phone number (digits, spaces, + ( ) - only).")
        return value


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
        user = self.context["request"].user
        if user.check_password(value):
            raise serializers.ValidationError("The new password must be different from the current one.")
        password_validation.validate_password(value, user)
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

    def to_representation(self, instance):
        from .emails import email_enabled

        data = super().to_representation(instance)
        data["email_enabled"] = email_enabled()  # the login page only offers "Forgot password?" when emails can be sent
        return data

    def validate_platform_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("The platform name cannot be empty.")
        return value
