from django import forms

from .models import GuidelineVersion

INPUT_CLASS = (
    "w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm "
    "focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
)


class GuidelineVersionForm(forms.ModelForm):
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
