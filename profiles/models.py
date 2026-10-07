from django.db import models, transaction
from django.db.models import Max, Q


class GuidelineVersion(models.Model):
    """프로필·지침의 한 버전. 수정은 덮어쓰지 않고 새 버전을 만든다."""

    version = models.PositiveIntegerField("버전", unique=True, editable=False)
    note = models.CharField("변경 메모", max_length=200, blank=True)
    is_active = models.BooleanField("활성", default=False)
    created_at = models.DateTimeField("생성일시", auto_now_add=True)
    # 개인정보 점검을 통과했거나, 지적을 사용자가 확인하고 저장한 버전이면 True.
    # 점검하지 못한 채 저장한 버전과 이 기능 이전의 버전은 False 이고, 활성으로 지정할 때 점검한다.
    privacy_checked = models.BooleanField("개인정보 점검 완료", default=False, editable=False)

    profile = models.TextField("고정 정보", blank=True)
    writing_rules = models.TextField("작성 규칙", blank=True)
    output_format = models.TextField("요청 사항/출력 형식", blank=True)
    extra = models.TextField("기타", blank=True)

    # 버전 비교·프롬프트 조립에서 섹션을 순회할 때 쓰는 필드 순서
    SECTION_FIELDS = ("profile", "writing_rules", "output_format", "extra")

    class Meta:
        verbose_name = "지침 버전"
        verbose_name_plural = "지침 버전"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["is_active"],
                condition=Q(is_active=True),
                name="unique_active_guideline_version",
            ),
        ]

    def __str__(self):
        return f"v{self.version}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if self._state.adding and not self.version:
                last = GuidelineVersion.objects.aggregate(last=Max("version"))["last"]
                self.version = (last or 0) + 1
            if self.is_active:
                GuidelineVersion.objects.filter(is_active=True).exclude(
                    pk=self.pk
                ).update(is_active=False)
            super().save(*args, **kwargs)

    def sections(self):
        """(섹션 라벨, 내용) 목록. 템플릿에서 섹션을 순서대로 그릴 때 쓴다."""
        return [
            (self._meta.get_field(name).verbose_name, getattr(self, name))
            for name in self.SECTION_FIELDS
        ]

    def activate(self):
        self.is_active = True
        self.save(update_fields=["is_active"])

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first()
