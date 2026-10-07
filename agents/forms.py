from django import forms

from profiles.forms import INPUT_CLASS

from .models import AgentSettings, Provider


class AgentSettingsForm(forms.ModelForm):
    """API 키 칸은 비워 두면 저장된 키를 그대로 둔다. 저장된 키는 화면에 다시 내보내지 않는다."""

    claude_api_key = forms.CharField(
        label="Claude API 키", required=False, widget=forms.PasswordInput()
    )
    gemini_api_key = forms.CharField(
        label="Gemini API 키", required=False, widget=forms.PasswordInput()
    )

    class Meta:
        model = AgentSettings
        fields = ["provider", "claude_model", "gemini_model"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs["class"] = INPUT_CLASS
            if name.endswith("_api_key"):
                masked = self.instance.masked_api_key(name.removesuffix("_api_key"))
                field.widget.attrs["autocomplete"] = "off"
                field.widget.attrs["placeholder"] = (
                    f"저장됨 ({masked}) · 바꿀 때만 입력" if masked else "아직 없음"
                )

    def save(self, commit=True):
        for provider in Provider.values:
            new_key = self.cleaned_data[f"{provider}_api_key"]
            if new_key:
                self.instance.set_api_key(provider, new_key)
        return super().save(commit)
