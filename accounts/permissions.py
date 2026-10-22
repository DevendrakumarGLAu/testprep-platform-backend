from rest_framework.permissions import BasePermission

from .models import Role


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == Role.SUPER_ADMIN)


class IsAdminOrSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role in (Role.ADMIN, Role.SUPER_ADMIN))


class IsStudent(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == Role.STUDENT)
