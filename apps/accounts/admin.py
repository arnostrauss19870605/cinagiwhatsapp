from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class AppUserAdmin(UserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "is_staff", "is_platform_admin")
    fieldsets = UserAdmin.fieldsets + (
        ("Platform", {"fields": ("phone", "is_platform_admin")}),
    )
