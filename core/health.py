from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@require_safe
@never_cache
def health(request):
    """
    A cheap check for uptime monitors and the hosting platform: the app is up and its database answers.
    Needs no sign-in and returns no data about the system. Pinging it every few minutes also keeps a free Render service awake.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any database failure means "not healthy"
        return JsonResponse({"status": "error", "database": "down"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})
