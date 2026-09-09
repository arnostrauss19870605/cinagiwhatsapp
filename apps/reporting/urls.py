from django.urls import path

from . import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("bulk-sends/<int:pk>/", views.bulk_send, name="bulk_send"),
    path("bulk-sends/<int:pk>/export/<slug:fmt>/", views.bulk_send_export, name="bulk_send_export"),
    path("bulk-sends/<int:pk>/resend/", views.bulk_send_resend, name="bulk_send_resend"),
]
