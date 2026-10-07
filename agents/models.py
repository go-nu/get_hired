from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from .crypto import decrypt, encrypt


class Provider(models.TextChoices):
    CLAUDE = "claude", "Claude"
    GEMINI = "gemini", "Gemini"


class ClaudeModel(models.TextChoices):
    OPUS = "claude-opus-5-5", "Claude Opus 5.5 (가장 정확)"
    SONNET = "claude-sonnet-5-5", "Claude Sonnet 5.5 (절반 가격)"
    HAIKU = "claude-haiku-4-5", "Claude Haiku 4.5 (가장 저렴)"


# Gemini 모델 이름은 자주 바뀌어 선택지로 묶지 않고 사용자 페이지에서 직접 입력한다.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


class AgentSettings(models.Model):
    """사용자별 에이전트 설정. API 키는 암호화해서 저장한다."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="사용자",
        on_delete=models.CASCADE,
        related_name="agent_settings",
    )
    provider = models.CharField(
        "사용할 AI", max_length=10, choices=Provider.choices, default=Provider.CLAUDE
    )
    claude_model = models.CharField(
        "Claude 모델", max_length=50, choices=ClaudeModel.choices, default=ClaudeModel.OPUS
    )
    gemini_model = models.CharField(
        "Gemini 모델", max_length=50, default=DEFAULT_GEMINI_MODEL
    )
    claude_api_key = models.TextField("Claude API 키(암호화)", blank=True, editable=False)
    gemini_api_key = models.TextField("Gemini API 키(암호화)", blank=True, editable=False)
    discord_webhook = models.TextField("디스코드 웹훅 주소(암호화)", blank=True, editable=False)
    updated_at = models.DateTimeField("수정일시", auto_now=True)

    class Meta:
        verbose_name = "에이전트 설정"
        verbose_name_plural = "에이전트 설정"

    def __str__(self):
        return f"{self.user} · {self.get_provider_display()}"

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]

    @property
    def model(self):
        """지금 선택한 AI의 모델 이름."""
        return getattr(self, f"{self.provider}_model")

    def set_api_key(self, provider, raw_key):
        setattr(self, f"{provider}_api_key", encrypt(raw_key.strip()))

    def get_api_key(self, provider=None):
        return decrypt(getattr(self, f"{provider or self.provider}_api_key"))

    def set_discord_webhook(self, url):
        self.discord_webhook = encrypt(url.strip())

    def get_discord_webhook(self):
        return decrypt(self.discord_webhook)

    def masked_api_key(self, provider):
        """화면 표시용. 끝 4자리만 보여 준다."""
        key = self.get_api_key(provider)
        return f"····{key[-4:]}" if key else ""


class Role(models.TextChoices):
    OCR = "ocr", "공고 캡처 읽기"
    # 아래 넷은 분석 그래프(applications/analysis_graph.py)의 노드
    RESEARCH = "research", "기업 조사"
    EVALUATE = "evaluate", "적합도 평가"
    WRITE = "write", "지원동기 작성"
    REVIEW = "review", "검수"


class Status(models.TextChoices):
    RUNNING = "running", "실행 중"
    WAITING = "waiting", "확인 대기"  # 분석 작업이 사람의 답을 기다리는 중 (AgentJob 전용)
    SUCCEEDED = "succeeded", "완료"
    FAILED = "failed", "실패"


# 이 시간 동안 진행이 없는 "실행 중" 작업은 서버가 꺼져 멈춘 것으로 본다.
STALE_JOB_MINUTES = 15


class AgentJob(models.Model):
    """지원 건 하나에 대한 분석 그래프 실행 한 번. 노드별 호출은 AgentRun 으로 딸린다."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="사용자",
        on_delete=models.CASCADE,
        related_name="agent_jobs",
    )
    application = models.ForeignKey(
        "applications.Application",
        verbose_name="지원 건",
        on_delete=models.CASCADE,
        related_name="agent_jobs",
    )
    status = models.CharField(
        "상태", max_length=10, choices=Status.choices, default=Status.RUNNING
    )
    step = models.CharField("현재 단계", max_length=20, choices=Role.choices, blank=True)
    revisions = models.PositiveSmallIntegerField("재작성 횟수", default=0)
    error = models.TextField("오류", blank=True)
    created_at = models.DateTimeField("시작일시", auto_now_add=True)
    updated_at = models.DateTimeField("마지막 진행일시", auto_now=True)
    finished_at = models.DateTimeField("종료일시", null=True, blank=True)

    class Meta:
        verbose_name = "분석 작업"
        verbose_name_plural = "분석 작업"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.application} · {self.get_status_display()} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def is_running(self):
        return self.status == Status.RUNNING

    @property
    def is_waiting(self):
        return self.status == Status.WAITING

    @classmethod
    def expire_stale(cls):
        cutoff = timezone.now() - timedelta(minutes=STALE_JOB_MINUTES)
        cls.objects.filter(status=Status.RUNNING, updated_at__lt=cutoff).update(
            status=Status.FAILED,
            error="서버가 중간에 꺼져 작업이 멈췄습니다. 다시 실행하세요.",
            finished_at=timezone.now(),
        )

    def finish(self, error=""):
        self.status = Status.FAILED if error else Status.SUCCEEDED
        self.error = error
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "error", "finished_at", "revisions", "updated_at"])


class AgentRun(models.Model):
    """AI 호출 한 번의 기록. 관리자 페이지에서 입출력과 토큰 사용량을 확인한다."""

    Role = Role
    Status = Status

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="사용자",
        on_delete=models.CASCADE,
        related_name="agent_runs",
    )
    role = models.CharField("역할", max_length=20, choices=Role.choices)
    provider = models.CharField("AI", max_length=10, choices=Provider.choices)
    model = models.CharField("모델", max_length=50)
    status = models.CharField(
        "상태", max_length=10, choices=Status.choices, default=Status.RUNNING
    )
    job = models.ForeignKey(
        AgentJob,
        verbose_name="분석 작업",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="runs",
    )
    application = models.ForeignKey(
        "applications.Application",
        verbose_name="지원 건",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_runs",
    )
    company = models.ForeignKey(
        "applications.Company",
        verbose_name="기업",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_runs",
    )
    prompt = models.TextField("입력", blank=True)
    image_count = models.PositiveSmallIntegerField("이미지 수", default=0)
    output = models.TextField("출력", blank=True)
    error = models.TextField("오류", blank=True)
    input_tokens = models.PositiveIntegerField("입력 토큰", default=0)
    output_tokens = models.PositiveIntegerField("출력 토큰", default=0)
    created_at = models.DateTimeField("시작일시", auto_now_add=True)
    finished_at = models.DateTimeField("종료일시", null=True, blank=True)

    class Meta:
        verbose_name = "에이전트 실행 기록"
        verbose_name_plural = "에이전트 실행 기록"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_role_display()} · {self.get_status_display()} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    @property
    def duration_seconds(self):
        if self.finished_at is None:
            return None
        return (self.finished_at - self.created_at).total_seconds()
