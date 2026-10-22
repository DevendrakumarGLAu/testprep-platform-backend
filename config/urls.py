from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/auth/", include("accounts.auth_urls")),
    path("api/", include("accounts.urls")),
    path("api/", include("questionbank.urls")),
    path("api/", include("attempts.admin_urls")),
    path("api/", include("imports.urls")),
    path("api/", include("reports.urls")),
    path("api/", include("exams.urls")),
    path("api/student/", include("attempts.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
