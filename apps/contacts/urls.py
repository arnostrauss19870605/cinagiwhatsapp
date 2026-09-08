from django.urls import path

from . import views

urlpatterns = [
    path("audiences/", views.audiences, name="audiences"),
    path("audiences/<int:pk>/", views.audience_detail, name="audience_detail"),
    path("audiences/<int:pk>/add/", views.audience_add, name="audience_add"),
    path("audiences/<int:pk>/remove/", views.audience_remove, name="audience_remove"),
    path("audiences/<int:pk>/delete/", views.audience_delete, name="audience_delete"),
    path("audiences/<int:pk>/import/", views.audience_import, name="audience_import"),
]
