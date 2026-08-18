from django.contrib import admin

from .models import AuditLog, FeatureToggle, UiCopy


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "workspace", "actor", "action", "target_type")
    list_filter = ("action", "workspace")
    search_fields = ("action", "target_id")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(UiCopy)
class UiCopyAdmin(admin.ModelAdmin):
    list_display = ("key", "workspace", "text")
    list_filter = ("workspace",)
    search_fields = ("key", "text")


@admin.register(FeatureToggle)
class FeatureToggleAdmin(admin.ModelAdmin):
    list_display = ("key", "is_enabled", "description", "updated_at")
    list_editable = ("is_enabled",)
