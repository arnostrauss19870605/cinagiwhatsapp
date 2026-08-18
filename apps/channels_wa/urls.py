from django.urls import path

from . import views
from .webhook import webhook

urlpatterns = [
    path("webhook/", webhook, name="webhook"),
    path("numbers/", views.channel_list, name="list"),
    path("numbers/connect/", views.connect, name="connect"),
    path("numbers/<int:pk>/edit/", views.edit, name="edit"),
    path("numbers/<int:pk>/check/", views.verify, name="verify"),
    path("numbers/<int:pk>/sync-templates/", views.sync_templates_now, name="sync_templates"),
    path("numbers/<int:pk>/toggle/", views.toggle_active, name="toggle"),
]
