from django.urls import path

from . import builder, views

urlpatterns = [
    path("<int:pk>/builder/", builder.builder, name="builder"),
    path("<int:pk>/builder/data/", builder.builder_data, name="builder_data"),
    path("<int:pk>/builder/op/", builder.builder_op, name="builder_op"),
    path("<int:pk>/builder/simulate/", builder.builder_simulate, name="builder_simulate"),
    path("<int:pk>/builder/phone-test/", builder.builder_phone_test, name="builder_phone_test"),
    path("<int:pk>/builder/steps/<str:step_id>/", builder.builder_panel, name="builder_panel"),
    path("", views.workflow_list, name="list"),
    path("import/", views.workflow_import, name="import"),
    path("webhooks/", views.connections, name="connections"),
    path("webhooks/<int:pk>/", views.connection_update, name="connection_update"),
    path("files/", views.files, name="files"),
    path("files/<int:pk>/", views.file_download, name="file_download"),
    path("files/<int:pk>/delete/", views.file_delete, name="file_delete"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("chats/<uuid:conversation_pk>/stop/", views.stop_conversation, name="stop_conversation"),
    path("<int:pk>/", views.workflow_detail, name="detail"),
    path("<int:pk>/settings/", views.workflow_settings, name="settings"),
    path("<int:pk>/publish/", views.workflow_publish, name="publish"),
    path("<int:pk>/discard/", views.workflow_discard, name="discard"),
    path("<int:pk>/switch/", views.workflow_toggle, name="toggle"),
    path("<int:pk>/delete/", views.workflow_delete, name="delete"),
    path("<int:pk>/steps/add/", views.step_add, name="step_add"),
    path("<int:pk>/steps/<str:step_id>/", views.step_edit, name="step_edit"),
    path("<int:pk>/steps/<str:step_id>/delete/", views.step_delete, name="step_delete"),
]
