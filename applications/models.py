from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


# 단계·결과·지원 경로의 실제 선택지는 Choice 표에 있고 관리자 페이지에서 고친다.
# 아래 Stage, Result는 처음 넣은 기본값이자, 코드가 뜻을 알고 쓰는 값의 이름이다.
class Stage(models.TextChoices):
    INTEREST = "interest", "관심"
    APPLIED = "applied", "지원 완료"
    DOCUMENT_PASSED = "document_passed", "서류 합격"
    TEST = "test", "코딩테스트/과제"
    INTERVIEW = "interview", "면접"
    FINAL = "final", "최종"  # 면접을 마치고 결과를 기다리는 상태


class Result(models.TextChoices):
    UNREAD = "unread", "미열람"
    READ = "read", "열람"
    IN_PROGRESS = "in_progress", "진행 중"
    PASSED = "passed", "합격"
    FAILED = "failed", "불합격"
    WITHDRAWN = "withdrawn", "포기"


class ChoiceKind(models.TextChoices):
    SOURCE = "source", "지원 경로"
    STAGE = "stage", "진행 단계"
    RESULT = "result", "결과"


_choice_cache = {}


def clear_choice_cache():
    _choice_cache.clear()


class Choice(models.Model):
    """지원 건의 단계·결과·지원 경로 선택지 하나. 지원 건에는 value를 저장한다."""

    kind = models.CharField("종류", max_length=10, choices=ChoiceKind.choices)
    # 지원 건에 저장되는 값. 새로 만들 때 자동으로 정하고 그 뒤로는 바꾸지 않는다.
    value = models.CharField("값", max_length=20, blank=True, editable=False)
    label = models.CharField("이름", max_length=30)
    order = models.PositiveIntegerField("순서", default=0, editable=False)
    is_hidden = models.BooleanField(
        "숨김",
        default=False,
        help_text="등록·수정 폼의 선택지에서 뺍니다. 이미 쓰고 있는 지원 건과 필터에는 남습니다.",
    )
    # 코드가 뜻을 알고 쓰는 값: 지울 수도 숨길 수도 없고 이름만 바꿀 수 있다.
    # 관심(아직 지원하지 않은 단계, 등록 기본값), 미열람(결과 기본값), 불합격(미응답 처리가 바꾸는 값).
    is_system = models.BooleanField("시스템 값", default=False, editable=False)
    # 결과 선택지에만 쓰는 속성
    is_open = models.BooleanField(
        "아직 끝나지 않은 결과",
        default=False,
        help_text="대시보드의 진행 중 건수와 마감 임박 알림에서 끝나지 않은 건으로 셉니다.",
    )
    is_no_response = models.BooleanField(
        "미응답 처리 대상",
        default=False,
        help_text="회사 쪽 반응이 아직 없는 결과입니다. 미응답 처리 모달에 나옵니다.",
    )

    class Meta:
        verbose_name = "선택지"
        verbose_name_plural = "선택지"
        ordering = ["kind", "order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "value"], name="unique_choice_value"),
            models.UniqueConstraint(fields=["kind", "label"], name="unique_choice_label"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.label}"

    @classmethod
    def of_kind(cls, kind):
        """한 종류의 선택지 전체(순서대로). 화면마다 여러 번 읽으므로 바뀔 때까지 기억해 둔다."""
        if kind not in _choice_cache:
            _choice_cache[kind] = list(cls.objects.filter(kind=kind))
        return _choice_cache[kind]

    def save(self, *args, **kwargs):
        if self._state.adding and not self.order:
            last = Choice.objects.filter(kind=self.kind).aggregate(models.Max("order"))
            self.order = (last["order__max"] or 0) + 1
        with transaction.atomic():
            super().save(*args, **kwargs)
            if not self.value:
                self.value = f"c{self.pk}"
                super().save(update_fields=["value"])
        clear_choice_cache()

    def delete(self, *args, **kwargs):
        reason = self.delete_blocker
        if reason:
            raise ValidationError(reason)
        result = super().delete(*args, **kwargs)
        clear_choice_cache()
        return result

    @property
    def usage_count(self):
        """이 선택지를 지금 쓰고 있는 지원 건 수 (삭제된 기업의 건 포함)."""
        return Application.objects.filter(**{self.kind: self.value}).count()

    @property
    def is_last_visible(self):
        """같은 종류에서 폼에 나오는 선택지가 이것 하나뿐인가."""
        others = Choice.objects.filter(kind=self.kind, is_hidden=False).exclude(pk=self.pk)
        return not self.is_hidden and not others.exists()

    @property
    def delete_blocker(self):
        """삭제할 수 없는 이유. 삭제할 수 있으면 빈 문자열."""
        if self.is_system:
            return "시스템이 쓰는 값이라 삭제할 수 없습니다. 이름은 바꿀 수 있습니다."
        count = self.usage_count
        if count:
            return f"지원 건 {count}건이 쓰고 있어 삭제할 수 없습니다. 대신 숨길 수 있습니다."
        if self.kind == ChoiceKind.STAGE and (
            StageHistory.objects.filter(
                Q(from_stage=self.value) | Q(to_stage=self.value)
            ).exists()
        ):
            return "단계 변경 이력에 남아 있어 삭제할 수 없습니다. 대신 숨길 수 있습니다."
        if self.is_last_visible:
            return "폼에서 고를 수 있는 마지막 선택지라 삭제할 수 없습니다."
        return ""


def choice_options(kind, include=""):
    """폼에 넣을 (값, 이름) 목록. 숨긴 선택지는 빼되 include(지금 저장된 값)는 남긴다."""
    return [
        (choice.value, choice.label)
        for choice in Choice.of_kind(kind)
        if not choice.is_hidden or choice.value == include
    ]


def choice_values(kind):
    return [choice.value for choice in Choice.of_kind(kind)]


def choice_label(kind, value):
    """값의 이름. 선택지에 없는 값이면 값 그대로."""
    return next(
        (choice.label for choice in Choice.of_kind(kind) if choice.value == value), value
    )


def default_source():
    options = choice_options(ChoiceKind.SOURCE)
    return options[0][0] if options else ""


def open_results():
    """아직 끝나지 않은(합격·불합격·포기가 아닌) 결과 값."""
    return [c.value for c in Choice.of_kind(ChoiceKind.RESULT) if c.is_open]


def no_response_results():
    """회사 쪽 반응이 아직 없는 결과 값 ("응답 없음" 판정 대상)."""
    return [c.value for c in Choice.of_kind(ChoiceKind.RESULT) if c.is_no_response]


# 마감이 이 일수 이내이고 아직 지원하지 않았으면 강조 표시한다.
URGENT_DAYS = 3
# 마감이 이 일수 이내이면 D-day 글자에 색을 입힌다.
SOON_DAYS = 7
# 미응답 처리 모달에서 고를 수 있는 기준 일수. 지원했는데 마감 후(상시지원은 지원 후)
# 이 일수가 지나도록 미열람·열람인 건이 대상이다. 첫 값이 기본 선택.
NO_RESPONSE_DAY_OPTIONS = (15, 30)


class CompanyQuerySet(models.QuerySet):
    def active(self):
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        return self.filter(deleted_at__isnull=False)


class Company(models.Model):
    class Size(models.TextChoices):
        STARTUP = "startup", "스타트업"
        SMALL = "small", "중소"
        MID = "mid", "중견"
        LARGE = "large", "대기업"

    name = models.CharField("기업명", max_length=100, unique=True)
    industry = models.CharField("업종", max_length=100, blank=True)
    size = models.CharField("규모", max_length=20, choices=Size.choices, blank=True)
    website = models.URLField("웹사이트", blank=True)
    memo = models.TextField("메모", blank=True)
    # 삭제는 행을 지우지 않고 삭제 시각만 기록한다. (삭제된 기업 목록·복원용)
    deleted_at = models.DateTimeField("삭제일시", null=True, blank=True, editable=False)

    objects = CompanyQuerySet.as_manager()

    class Meta:
        verbose_name = "기업"
        verbose_name_plural = "기업"
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    @property
    def can_delete(self):
        """실제로 지원한(관심 단계를 넘어간) 건이 없을 때만 삭제할 수 있다."""
        return not self.applications.exclude(stage=Stage.INTEREST).exists()

    def soft_delete(self):
        if not self.can_delete:
            raise ValidationError("이미 지원한 기업은 삭제할 수 없습니다.")
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])


class ApplicationQuerySet(models.QuerySet):
    def visible(self):
        """삭제된 기업의 지원 건을 뺀 목록."""
        return self.filter(company__deleted_at__isnull=True)

    def no_response(self, days=min(NO_RESPONSE_DAY_OPTIONS)):
        """미응답 처리 대상: 지원한 뒤 기준일부터 days일 이상 지났는데 미열람·열람인 건."""
        cutoff = timezone.localdate() - timedelta(days=days)
        return (
            self.visible()
            .exclude(stage=Stage.INTEREST)
            .filter(result__in=no_response_results())
            .filter(
                Q(deadline__lte=cutoff)
                | Q(deadline__isnull=True, applied_at__lte=cutoff)
            )
        )


class Application(models.Model):
    class FitGrade(models.TextChoices):
        HIGH = "high", "상"
        MID_HIGH = "mid_high", "중상"
        MID = "mid", "중"
        MID_LOW = "mid_low", "중하"
        LOW = "low", "하"

    company = models.ForeignKey(
        Company,
        verbose_name="기업",
        on_delete=models.PROTECT,
        related_name="applications",
    )
    position = models.CharField("직무명", max_length=200)
    posting_url = models.URLField("공고 URL", max_length=500, blank=True)
    # source, stage, result에는 Choice.value를 저장한다. 선택지는 폼에서 채운다.
    source = models.CharField("지원 경로", max_length=20, default=default_source)

    # 공고 정보
    main_tasks = models.TextField("주요 업무", blank=True)
    requirements = models.TextField("자격 요건", blank=True)
    preferred = models.TextField("우대 사항", blank=True)
    essay_char_limit = models.PositiveIntegerField("자소서 글자 수 제한", null=True, blank=True)
    extra_notes = models.TextField("기타 공고 정보", blank=True)

    deadline = models.DateField("마감일", null=True, blank=True)
    applied_at = models.DateField("지원 완료일", null=True, blank=True)
    stage = models.CharField("진행 단계", max_length=20, default=Stage.INTEREST)
    result = models.CharField("결과", max_length=20, default=Result.UNREAD)

    fit_score = models.PositiveSmallIntegerField(
        "적합도 점수",
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    fit_grade = models.CharField(
        "적합도 등급", max_length=10, choices=FitGrade.choices, null=True, blank=True
    )
    guideline_version = models.ForeignKey(
        "profiles.GuidelineVersion",
        verbose_name="분석에 사용한 지침 버전",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )

    memo = models.TextField("메모", blank=True)
    created_at = models.DateTimeField("등록일시", auto_now_add=True)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    objects = ApplicationQuerySet.as_manager()

    class Meta:
        verbose_name = "지원 건"
        verbose_name_plural = "지원 건"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company} · {self.position}"

    def save(self, *args, **kwargs):
        # 지원 완료 이후 단계인데 지원일이 비어 있으면 오늘로 채운다.
        if self.stage != Stage.INTEREST and self.applied_at is None:
            self.applied_at = timezone.localdate()
        # 단계가 바뀌면(최초 등록 포함) StageHistory를 자동으로 남긴다.
        if self._state.adding:
            previous = ""
        else:
            previous = (
                Application.objects.filter(pk=self.pk)
                .values_list("stage", flat=True)
                .first()
            )
        with transaction.atomic():
            super().save(*args, **kwargs)
            if previous != self.stage:
                StageHistory.objects.create(
                    application=self, from_stage=previous or "", to_stage=self.stage
                )

    def get_source_display(self):
        return choice_label(ChoiceKind.SOURCE, self.source)

    def get_stage_display(self):
        return choice_label(ChoiceKind.STAGE, self.stage)

    def get_result_display(self):
        return choice_label(ChoiceKind.RESULT, self.result)

    @property
    def latest_analysis(self):
        return self.analyses.first()

    @property
    def is_applied(self):
        return self.stage != Stage.INTEREST

    @property
    def days_left(self):
        """마감까지 남은 일수. 마감일이 없으면 None, 지났으면 음수."""
        if self.deadline is None:
            return None
        return (self.deadline - timezone.localdate()).days

    @property
    def d_day_label(self):
        days = self.days_left
        if days is None:
            return ""
        if days == 0:
            return "D-DAY"
        return f"D-{days}" if days > 0 else f"D+{-days}"

    @property
    def days_without_response(self):
        """미응답 기준일(마감일, 상시지원은 지원일)부터 지난 일수."""
        base = self.deadline or self.applied_at
        return (timezone.localdate() - base).days if base else None

    @property
    def deadline_level(self):
        """D-day 글자 색을 정하는 구분: today(오늘 마감) / soon(7일 이내) / ""."""
        days = self.days_left
        if days == 0:
            return "today"
        if days is not None and 0 < days <= SOON_DAYS:
            return "soon"
        return ""

    @property
    def is_urgent(self):
        """마감 3일 이내인데 아직 지원하지 않은 건."""
        days = self.days_left
        return (
            days is not None
            and 0 <= days <= URGENT_DAYS
            and not self.is_applied
            and self.result in open_results()
        )


class Analysis(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "붙여넣기"
        API = "api", "에이전트"

    application = models.ForeignKey(
        Application,
        verbose_name="지원 건",
        on_delete=models.CASCADE,
        related_name="analyses",
    )
    company_analysis = models.TextField("기업 분석", blank=True)
    fit_evaluation = models.TextField("적합도 평가", blank=True)
    motivation_draft = models.TextField("지원동기 초안", blank=True)
    title_candidates = models.TextField("제목 후보", blank=True)
    # 검수 에이전트가 끝까지 남긴 지적. 통과했거나 붙여넣은 결과면 비어 있다.
    review_notes = models.TextField("검수 의견", blank=True)
    source = models.CharField(
        "생성 방식", max_length=10, choices=Source.choices, default=Source.MANUAL
    )
    created_at = models.DateTimeField("생성일시", auto_now_add=True)

    class Meta:
        verbose_name = "분석 결과"
        verbose_name_plural = "분석 결과"
        ordering = ["-created_at", "-id"]  # 가장 최근 것이 대표

    def __str__(self):
        return f"{self.application} 분석 ({self.created_at:%Y-%m-%d %H:%M})"


class StageHistory(models.Model):
    application = models.ForeignKey(
        Application,
        verbose_name="지원 건",
        on_delete=models.CASCADE,
        related_name="stage_history",
    )
    from_stage = models.CharField("이전 단계", max_length=20, blank=True)
    to_stage = models.CharField("변경 단계", max_length=20)
    changed_at = models.DateTimeField("변경일시", auto_now_add=True)

    class Meta:
        verbose_name = "단계 변경 이력"
        verbose_name_plural = "단계 변경 이력"
        ordering = ["-changed_at", "-id"]

    def get_from_stage_display(self):
        return choice_label(ChoiceKind.STAGE, self.from_stage)

    def get_to_stage_display(self):
        return choice_label(ChoiceKind.STAGE, self.to_stage)

    def __str__(self):
        before = self.get_from_stage_display() or "등록"
        return f"{before} → {self.get_to_stage_display()}"
