"""respond.io-shaped API paths, mounted at /api/v2/."""

from django.urls import path

from . import api

urlpatterns = [
    path("contact/create_or_update/<str:identifier>", api.contact_create_or_update, name="contact_create_or_update"),
    path("contact/<str:identifier>/message", api.contact_message, name="contact_message"),
    path("contact/<str:identifier>", api.contact_get, name="contact_get"),
]
