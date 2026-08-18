from django.contrib import admin

from .models import AgentProfile, Skill, Team


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "presence", "max_concurrent", "accepts_auto_assignment")
    list_filter = ("workspace", "presence")


admin.site.register(Skill)
admin.site.register(Team)
