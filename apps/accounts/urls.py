from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("login/", views.login_start, name="login"),
    path("login/code/", views.login_code, name="login_code"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("users/", views.users, name="users"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/", views.user_edit, name="user_edit"),
    path("users/<int:pk>/resend/", views.user_resend_welcome, name="user_resend_welcome"),
    path("users/<int:pk>/toggle/", views.user_toggle_active, name="user_toggle_active"),
]
