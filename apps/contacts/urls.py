from django.urls import path

from . import views

urlpatterns = [
    path("", views.contacts, name="contacts"),
    path("export/", views.contacts_export, name="contacts_export"),
    path("<int:pk>/", views.contact_detail, name="contact_detail"),
    path("<int:pk>/audiences/", views.contact_audiences, name="contact_audiences"),
    path("<int:pk>/export/<slug:fmt>/", views.contact_export, name="contact_export"),
    path("audiences/", views.audiences, name="audiences"),
    path("audiences/import/", views.audience_import, name="audience_import"),
    path("audiences/import/map/", views.audience_import_map, name="audience_import_map"),
    path("audiences/<int:pk>/", views.audience_detail, name="audience_detail"),
    path("audiences/<int:pk>/export/", views.audience_export, name="audience_export"),
    path("audiences/<int:pk>/add/", views.audience_add, name="audience_add"),
    path("audiences/<int:pk>/add-new/", views.audience_add_new, name="audience_add_new"),
    path("audiences/<int:pk>/remove/", views.audience_remove, name="audience_remove"),
    path("audiences/<int:pk>/delete/", views.audience_delete, name="audience_delete"),
]
