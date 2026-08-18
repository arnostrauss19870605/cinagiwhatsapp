from django.contrib import admin

from .models import BusinessHours, Holiday, Workspace, WorkspaceMembership


class MembershipInline(admin.TabularInline):
    model = WorkspaceMembership
    extra = 0


class BusinessHoursInline(admin.TabularInline):
    model = BusinessHours
    extra = 0


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active", "auto_assign_enabled")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline, BusinessHoursInline]


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("name", "date", "workspace", "recurring_annually")
