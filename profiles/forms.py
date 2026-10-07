from django import forms

from .models import GuidelineVersion
from .services import check_privacy, confirm_token, is_confirmed, sections_of

INPUT_CLASS = (
    "w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm "
    "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
)

RESIDENT_NUMBER_ERROR = "주민등록번호로 보이는 숫자가 있습니다. 지워야 저장할 수 있습니다."


class GuidelineVersionForm(forms.ModelForm):
    """저장하기 전에 개인정보 점검을 한다. (profiles/services.py)

    - 주민등록번호가 있으면 저장하지 않는다.
    - AI가 지적하면 저장하지 않고 지적을 보여 준다. 사용자가 [그래도 저장]을 누르면
      privacy_confirm 값이 함께 와서, 같은 내용일 때만 점검 없이 저장한다.
    - AI 점검을 하지 못하면 저장하고 privacy_warning 에 이유를 남긴다.
    """

    class Meta:
        model = GuidelineVersion
        fields = ["note", *GuidelineVersion.SECTION_FIELDS]
        widgets = {
            "note": forms.TextInput(
                attrs={"class": INPUT_CLASS, "placeholder": "이번 버전에서 바뀐 점"}
            ),
            **{
                name: forms.Textarea(
                    attrs={"class": f"{INPUT_CLASS} font-mono", "rows": 10}
                )
                for name in GuidelineVersion.SECTION_FIELDS
            },
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.privacy_findings = []
        self.privacy_confirm = ""  # 지적이 있을 때 [그래도 저장]이 보낼 값
        self.privacy_warning = ""

    def clean(self):
        cleaned_data = super().clean()
        sections = sections_of(cleaned_data)
        if is_confirmed(sections, self.data.get("privacy_confirm")):
            self.instance.privacy_checked = True
            return cleaned_data

        result = check_privacy(self.user, sections)
        for name in result.resident_fields:
            self.add_error(name, RESIDENT_NUMBER_ERROR)
        for finding in result.findings:
            self.add_error(finding.field or None, str(finding))
        if result.findings:
            self.privacy_findings = result.findings
            self.privacy_confirm = confirm_token(sections)
        if result.error:
            self.privacy_warning = result.error
        self.instance.privacy_checked = result.checked
        return cleaned_data
