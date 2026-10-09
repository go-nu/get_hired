from django import forms

from profiles.forms import INPUT_CLASS
from profiles.models import GuidelineVersion

from .models import (
    Analysis,
    Application,
    Choice,
    ChoiceKind,
    Company,
    choice_options,
)
from .services import ANALYSIS_FIELDS

# 지원 건의 칸 → 관리자 페이지에서 관리하는 선택지 종류
CHOICE_FIELDS = {
    "source": ChoiceKind.SOURCE,
    "stage": ChoiceKind.STAGE,
    "result": ChoiceKind.RESULT,
}


class StyledModelForm(forms.ModelForm):
    """모든 위젯에 공통 Tailwind 클래스를 붙인다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            widget.attrs["class"] = f"{INPUT_CLASS} {widget.attrs.get('class', '')}".strip()


class ChoiceFieldsMixin:
    """단계·결과·지원 경로 칸을 선택지(Choice)로 채운다. 숨긴 선택지는 지금 저장된 값일 때만 남긴다."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, kind in CHOICE_FIELDS.items():
            if name not in self.fields:
                continue
            field = forms.ChoiceField(
                label=self.fields[name].label,
                choices=choice_options(kind, include=getattr(self.instance, name)),
            )
            field.widget.attrs["class"] = INPUT_CLASS
            self.fields[name] = field


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class ApplicationForm(ChoiceFieldsMixin, StyledModelForm):
    company_name = forms.CharField(label="기업명", max_length=100)

    class Meta:
        model = Application
        fields = [
            "position",
            "posting_url",
            "source",
            "deadline",
            "applied_at",
            "stage",
            "result",
            "main_tasks",
            "requirements",
            "preferred",
            "guideline_version",
            "memo",
        ]
        widgets = {
            "deadline": DateInput(),
            "applied_at": DateInput(),
            "main_tasks": forms.Textarea(attrs={"rows": 5}),
            "requirements": forms.Textarea(attrs={"rows": 5}),
            "preferred": forms.Textarea(attrs={"rows": 5}),
            "memo": forms.Textarea(attrs={"rows": 3}),
        }

    field_order = ["company_name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["company_name"].initial = self.instance.company.name
        else:
            self.fields["guideline_version"].initial = GuidelineVersion.get_active()

    def clean_company_name(self):
        name = self.cleaned_data["company_name"].strip()
        if Company.objects.deleted().filter(name=name).exists():
            raise forms.ValidationError(
                "삭제된 기업입니다. 기업 메뉴의 삭제된 기업 목록에서 복원한 뒤 사용하세요."
            )
        return name

    def save(self, commit=True):
        # 처음 보는 기업명이면 기업을 새로 만든다.
        self.instance.company, _ = Company.objects.get_or_create(
            name=self.cleaned_data["company_name"]
        )
        return super().save(commit)


class StageForm(ChoiceFieldsMixin, StyledModelForm):
    """상세 화면에서 단계·결과만 빠르게 바꾸는 폼."""

    class Meta:
        model = Application
        fields = ["stage", "result"]


class GuidelineChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, guideline):
        return f"{guideline} (활성)" if guideline.is_active else str(guideline)


class AnalysisRunForm(forms.Form):
    """상세 화면의 [에이전트로 분석] 옆에서 분석에 쓸 지침 버전을 고른다. 기본은 활성 버전."""

    guideline_version = GuidelineChoiceField(
        label="지침 버전",
        required=False,
        queryset=GuidelineVersion.objects.order_by("-version"),
        empty_label="지침 없음 (기업 조사만)",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["guideline_version"]
        field.initial = GuidelineVersion.get_active()
        field.widget.attrs.update(
            {
                "class": "rounded-md border border-gray-300 bg-white px-2 py-1 text-sm",
                "aria-label": field.label,
            }
        )


class AnalysisForm(StyledModelForm):
    class Meta:
        model = Analysis
        fields = list(ANALYSIS_FIELDS)
        widgets = {
            name: forms.Textarea(attrs={"rows": 10, "class": "font-mono"})
            for name in ANALYSIS_FIELDS
        }

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(name, "").strip() for name in ANALYSIS_FIELDS):
            raise forms.ValidationError("분석 결과를 한 항목 이상 입력하세요.")
        return cleaned


class CompanyForm(StyledModelForm):
    class Meta:
        model = Company
        fields = ["name", "industry", "size", "website", "memo"]
        widgets = {"memo": forms.Textarea(attrs={"rows": 3})}


class ChoiceForm(StyledModelForm):
    """관리자 페이지의 선택지 추가·수정. 종류는 주소에서 받고 값은 자동으로 정한다."""

    class Meta:
        model = Choice
        fields = ["label", "is_hidden", "is_open", "is_no_response"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choice = self.instance
        if choice.kind != ChoiceKind.RESULT:
            del self.fields["is_open"], self.fields["is_no_response"]
        if choice.is_system or choice._state.adding:  # 시스템 값은 숨길 수 없다
            del self.fields["is_hidden"]
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "rounded border-gray-300 text-indigo-600"

    def clean_label(self):
        label = self.cleaned_data["label"].strip()
        same = Choice.objects.filter(kind=self.instance.kind, label=label)
        if same.exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("같은 이름의 선택지가 이미 있습니다.")
        return label

    def clean_is_hidden(self):
        hidden = self.cleaned_data["is_hidden"]
        if hidden and self.instance.is_last_visible:
            raise forms.ValidationError("폼에서 고를 수 있는 마지막 선택지는 숨길 수 없습니다.")
        return hidden


class DashboardFilterForm(forms.Form):
    """대시보드 필터. 숨긴 선택지도 쓰는 지원 건이 있을 수 있어 모두 보여 준다."""

    stage = forms.ChoiceField(label="단계", required=False)
    result = forms.ChoiceField(label="결과", required=False)
    source = forms.ChoiceField(label="지원 경로", required=False)

    EMPTY_LABELS = {"stage": "전체 단계", "result": "전체 결과", "source": "전체 경로"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.choices = [
                ("", self.EMPTY_LABELS[name]),
                *((c.value, c.label) for c in Choice.of_kind(CHOICE_FIELDS[name])),
            ]
            field.widget.attrs["class"] = (
                "rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm"
            )
