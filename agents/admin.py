from django.contrib import admin

from .models import AgentJob, AgentRun, AgentSettings, Provider


@admin.register(AgentSettings)
class AgentSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "claude_model", "gemini_model", "has_keys", "updated_at")
    readonly_fields = ("has_keys", "updated_at")

    @admin.display(description="저장된 API 키")
    def has_keys(self, obj):
        # 키 값은 보여 주지 않는다. 입력은 사용자 페이지에서만 한다.
        saved = [label for value, label in Provider.choices if obj.get_api_key(value)]
        return ", ".join(saved) or "-"


@admin.register(AgentJob)
class AgentJobAdmin(admin.ModelAdmin):
    list_display = ("created_at", "application", "status", "step", "revisions", "finished_at")
    list_filter = ("status",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "role",
        "provider",
        "model",
        "status",
        "input_tokens",
        "output_tokens",
        "application",
    )
    list_filter = ("status", "role", "provider")
    search_fields = ("prompt", "output", "error")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # 실행 기록은 읽기 전용.
        return False
