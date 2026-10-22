from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import ActivityLog, PlatformSettings, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "is_active")
    list_filter = ("role", "is_active")
    fieldsets = UserAdmin.fieldsets + (("Role", {"fields": ("role", "phone", "created_by")}),)


admin.site.register([ActivityLog, PlatformSettings])
