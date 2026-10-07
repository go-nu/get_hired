from django.contrib import admin, messages

from .models import GuidelineVersion


@admin.register(GuidelineVersion)
class GuidelineVersionAdmin(admin.ModelAdmin):
    list_display = ("version", "note", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("note",)
    readonly_fields = ("version", "created_at")
    actions = ["make_active"]
    fieldsets = (
        (None, {"fields": ("version", "note", "is_active", "created_at")}),
        ("섹션", {"fields": GuidelineVersion.SECTION_FIELDS}),
    )

    def has_change_permission(self, request, obj=None):
        # 과거 버전은 읽기 전용. 수정하려면 새 버전을 추가한다.
        return False

    @admin.action(description="선택한 버전을 활성 버전으로 지정")
    def make_active(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request, "활성 버전은 하나만 선택해야 합니다.", messages.ERROR
            )
            return
        version = queryset.get()
        version.activate()
        self.message_user(request, f"{version} 을(를) 활성 버전으로 지정했습니다.")
