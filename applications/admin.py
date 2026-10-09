from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .models import Analysis, Application, Choice, Company, StageHistory


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "industry", "size", "website", "deleted_at")
    list_filter = ("size", ("deleted_at", admin.EmptyFieldListFilter))
    search_fields = ("name", "industry")
    readonly_fields = ("deleted_at",)
    actions = ["soft_delete_selected", "restore_selected"]

    def has_delete_permission(self, request, obj=None):
        # 행을 지우지 않는다. 아래 "삭제 처리" 액션으로만 삭제한다.
        return False

    @admin.action(description="선택한 기업을 삭제 처리 (지원 전 기업만)")
    def soft_delete_selected(self, request, queryset):
        for company in queryset.active():
            try:
                company.soft_delete()
            except ValidationError as error:
                self.message_user(
                    request, f"{company}: {error.message}", messages.ERROR
                )

    @admin.action(description="선택한 기업을 복원")
    def restore_selected(self, request, queryset):
        for company in queryset.deleted():
            company.restore()


class AnalysisInline(admin.StackedInline):
    model = Analysis
    extra = 0
    readonly_fields = ("created_at",)


class StageHistoryInline(admin.TabularInline):
    model = StageHistory
    extra = 0
    can_delete = False
    readonly_fields = ("from_stage", "to_stage", "changed_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "position",
        "fit_score",
        "fit_grade",
        "source",
        "deadline",
        "applied_at",
        "stage",
        "result",
    )
    list_filter = ("stage", "result", "source", "fit_grade")
    search_fields = ("company__name", "position")
    autocomplete_fields = ("company",)
    date_hierarchy = "deadline"
    readonly_fields = ("created_at", "updated_at")
    inlines = [AnalysisInline, StageHistoryInline]
    fieldsets = (
        (None, {"fields": ("company", "position", "posting_url", "source")}),
        (
            "공고 정보",
            {
                "fields": (
                    "main_tasks",
                    "requirements",
                    "preferred",
                    "essay_char_limit",
                    "extra_notes",
                )
            },
        ),
        ("진행", {"fields": ("deadline", "applied_at", "stage", "result")}),
        ("적합도", {"fields": ("fit_score", "fit_grade", "guideline_version")}),
        ("기타", {"fields": ("memo", "created_at", "updated_at")}),
    )


@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    list_display = ("application", "source", "created_at")
    list_filter = ("source",)
    search_fields = ("application__company__name", "application__position")
    readonly_fields = ("created_at",)


@admin.register(Choice)
class ChoiceAdmin(admin.ModelAdmin):
    list_display = ("kind", "label", "value", "order", "is_hidden", "is_system")
    list_filter = ("kind", "is_hidden")
    readonly_fields = ("kind", "value", "order", "is_system")

    def has_add_permission(self, request):
        # 추가·삭제·순서 변경은 관리자 페이지의 선택지 관리에서 한다. (사용 중 삭제 방지)
        return False

    def has_delete_permission(self, request, obj=None):
        return False
