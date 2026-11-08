from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from .health import HealthView

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("api/health/", HealthView.as_view()),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("trades.urls")),
]

if settings.DEBUG:
    urlpatterns.append(path("api-auth/", include("rest_framework.urls")))  # the browsable API's login page