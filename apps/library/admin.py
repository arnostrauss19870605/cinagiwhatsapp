from django.contrib import admin

from .models import KnowledgeArticle, MessageTemplate, QuickSnippet


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "language", "category", "status", "workspace")
    list_filter = ("workspace", "status", "category")
    search_fields = ("name",)


@admin.register(QuickSnippet)
class QuickSnippetAdmin(admin.ModelAdmin):
    list_display = ("title", "shortcut", "category", "workspace", "owner", "usage_count")
    list_filter = ("workspace", "category")


admin.site.register(KnowledgeArticle)
