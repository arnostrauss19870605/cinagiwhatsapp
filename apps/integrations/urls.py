from django.urls import path

from . import views

urlpatterns = [
    path("api-keys/", views.api_keys, name="api_keys"),
    path("api-keys/<int:pk>/revoke/", views.api_key_revoke, name="api_key_revoke"),
]
