from django import forms

from profiles.forms import INPUT_CLASS

from .models import AgentSettings, Provider
from .notify import is_discord_webhook


class AgentSettingsForm(forms.ModelForm):
    """API 키 칸은 비워 두면 저장된 키를 그대로 둔다. 저장된 키는 화면에 다시 내보내지 않는다."""

    claude_api_key = forms.CharField(
        label="Claude API 키", required=False, widget=forms.PasswordInput()
    )
    gemini_api_key = forms.CharField(
        label="Gemini API 키", required=False, widget=forms.PasswordInput()
    )

    discord_webhook = forms.CharField(
        label="디스코드 웹훅 주소", required=False, widget=forms.PasswordInput()
    )
    clear_discord_webhook = forms.BooleanField(label="디스코드 알림 끄기", required=False)

    class Meta:
        model = AgentSettings
        fields = ["provider", "claude_model", "gemini_model"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        has_webhook = bool(self.instance.get_discord_webhook())
        self.fields["discord_webhook"].widget.attrs.update(
            autocomplete="off",
            placeholder="저장됨 · 바꿀 때만 입력" if has_webhook else "아직 없음",
        )
        if not has_webhook:
            del self.fields["clear_discord_webhook"]
        for name, field in self.fields.items():
            if name == "clear_discord_webhook":
                continue  # 체크박스는 입력 칸 모양을 쓰지 않는다
            field.widget.attrs["class"] = INPUT_CLASS
            if name.endswith("_api_key"):
                masked = self.instance.masked_api_key(name.removesuffix("_api_key"))
                field.widget.attrs["autocomplete"] = "off"
                field.widget.attrs["placeholder"] = (
                    f"저장됨 ({masked}) · 바꿀 때만 입력" if masked else "아직 없음"
                )

    def clean_discord_webhook(self):
        url = self.cleaned_data["discord_webhook"].strip()
        if url and not is_discord_webhook(url):
            raise forms.ValidationError(
                "디스코드 웹훅 주소가 아닙니다. https://discord.com/api/webhooks/ 로 시작해야 합니다."
            )
        return url

    def save(self, commit=True):
        if self.cleaned_data.get("clear_discord_webhook"):
            self.instance.discord_webhook = ""
        if self.cleaned_data["discord_webhook"]:
            self.instance.set_discord_webhook(self.cleaned_data["discord_webhook"])
        for provider in Provider.values:
            new_key = self.cleaned_data[f"{provider}_api_key"]
            if new_key:
                self.instance.set_api_key(provider, new_key)
        return super().save(commit)
