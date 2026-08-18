from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.core.views import healthz

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("", include(("apps.core.urls", "core"), namespace="core")),
    path("accounts/", include(("apps.accounts.urls", "accounts"), namespace="accounts")),
    path(
        "workspaces/",
        include(("apps.workspaces.urls", "workspaces"), namespace="workspaces"),
    ),
    path("inbox/", include(("apps.inbox.urls", "inbox"), namespace="inbox")),
    path("library/", include(("apps.library.urls", "library"), namespace="library")),
    path("agents/", include(("apps.agents.urls", "agents"), namespace="agents")),
    path("wa/", include(("apps.channels_wa.urls", "channels_wa"), namespace="channels_wa")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
