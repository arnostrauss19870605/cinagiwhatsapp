from django.contrib import admin

from .models import RunEvent, WebhookConnection, Workflow, WorkflowFile, WorkflowRun, WorkflowVersion


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "trigger", "is_active", "published")
    list_filter = ("workspace", "is_active")


@admin.register(WorkflowVersion)
class WorkflowVersionAdmin(admin.ModelAdmin):
    list_display = ("workflow", "number", "published_at", "created_at")


@admin.register(WorkflowRun)
class WorkflowRunAdmin(admin.ModelAdmin):
    list_display = ("workflow", "contact", "status", "current_step", "created_at", "ended_at")
    list_filter = ("status", "workspace")


admin.site.register(RunEvent)
admin.site.register(WorkflowFile)


@admin.register(WebhookConnection)
class WebhookConnectionAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "url", "is_active")
    exclude = ("token",)
