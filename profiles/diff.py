import difflib

from .models import GuidelineVersion

_TAGS = {"- ": "removed", "+ ": "added", "  ": "same"}


def diff_lines(old, new):
    """두 텍스트를 줄 단위로 비교해 {"tag", "text"} 목록으로 돌려준다."""
    lines = []
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        tag = _TAGS.get(line[:2])
        if tag is None:  # "? " 로 시작하는 ndiff 힌트 줄은 버린다
            continue
        lines.append({"tag": tag, "text": line[2:]})
    return lines


def compare_versions(old, new):
    """두 GuidelineVersion을 섹션별로 비교한다."""
    sections = []
    for name in GuidelineVersion.SECTION_FIELDS:
        lines = diff_lines(getattr(old, name), getattr(new, name))
        sections.append(
            {
                "label": GuidelineVersion._meta.get_field(name).verbose_name,
                "lines": lines,
                "changed": any(line["tag"] != "same" for line in lines),
            }
        )
    return sections
