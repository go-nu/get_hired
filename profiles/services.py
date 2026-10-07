"""프로필·지침의 개인정보 점검. 폼과 뷰는 여기 함수만 호출한다.

지원 서류에 적으면 안 되는 개인정보가 프로필에 있으면 에이전트가 그대로 읽고 쓰게 되므로,
저장하기 전에 막는다.

- 주민등록번호: 정규식으로 찾는다. AI를 부르지 않고, 있으면 무조건 저장하지 않는다.
- 그 밖의 항목(가족·신체·출신 지역·혼인·종교 등): AI가 찾는다. 찾을 항목은 코드가 아니라
  skills/privacy-check/SKILL.md 에 있다. 잘못 짚을 수 있으므로 사용자가 확인하면 저장할 수 있다.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from django.core import signing

from agents.models import Role
from agents.providers import ProviderError
from agents.services import run_agent
from agents.skills import load_skill

from .models import GuidelineVersion

PRIVACY_SKILL = "privacy-check"

# 앞 6자리는 생년월일(월·일 범위까지 확인), 뒤 7자리는 1~8로 시작한다. 하이픈은 없어도 잡는다.
RESIDENT_NUMBER_PATTERN = re.compile(
    r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s*-?\s*[1-8]\d{6}(?!\d)"
)

SECTION_LABELS = {
    name: str(GuidelineVersion._meta.get_field(name).verbose_name)
    for name in GuidelineVersion.SECTION_FIELDS
}
FIELD_BY_LABEL = {label: name for name, label in SECTION_LABELS.items()}

PRIVACY_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": list(FIELD_BY_LABEL)},
                    "category": {"type": "string"},
                    "quote": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["section", "category", "quote", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# "그래도 저장"은 지적을 보여 준 바로 그 내용에만 통한다. 내용을 고치면 다시 점검한다.
CONFIRM_SALT = "profiles.privacy-confirm"
CONFIRM_MAX_AGE = 60 * 60  # 초


@dataclass
class Finding:
    field: str  # 섹션 필드 이름. AI가 섹션을 잘못 적었으면 빈 문자열.
    category: str
    quote: str
    reason: str

    def __str__(self):
        return f"[{self.category}] “{self.quote}” — {self.reason}"


@dataclass
class PrivacyResult:
    resident_fields: list = field(default_factory=list)  # 주민등록번호가 있는 섹션
    findings: list = field(default_factory=list)  # AI가 찾은 곳
    error: str = ""  # AI 점검을 하지 못한 이유

    @property
    def checked(self):
        """점검을 마쳤고 걸린 것이 없다."""
        return not (self.resident_fields or self.findings or self.error)


def sections_of(source):
    """GuidelineVersion 이나 폼의 cleaned_data 에서 섹션 글만 뽑는다."""
    if isinstance(source, dict):
        return {name: source.get(name) or "" for name in GuidelineVersion.SECTION_FIELDS}
    return {name: getattr(source, name) for name in GuidelineVersion.SECTION_FIELDS}


def find_resident_numbers(sections):
    """주민등록번호로 보이는 숫자가 있는 섹션의 필드 이름 목록."""
    return [name for name, text in sections.items() if RESIDENT_NUMBER_PATTERN.search(text)]


def find_private_info(user, sections):
    """AI로 섹션 글을 점검해 Finding 목록을 돌려준다. 실패하면 ProviderError."""
    blocks = [
        f"## {SECTION_LABELS[name]}\n{text.strip()}"
        for name, text in sections.items()
        if text.strip()
    ]
    if not blocks:
        return []
    run = run_agent(
        user,
        Role.PRIVACY,
        system=load_skill(PRIVACY_SKILL).render(),
        text="# 점검할 글\n\n" + "\n\n".join(blocks),
        schema=PRIVACY_SCHEMA,
    )
    try:
        items = json.loads(run.output)["findings"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ProviderError("점검 결과를 해석하지 못했습니다.") from error
    return [
        Finding(
            field=FIELD_BY_LABEL.get(str(item.get("section", "")).strip(), ""),
            category=str(item.get("category", "")).strip(),
            quote=str(item.get("quote", "")).strip(),
            reason=str(item.get("reason", "")).strip(),
        )
        for item in items
    ]


def check_privacy(user, sections):
    """섹션 글을 점검한다. 주민등록번호가 있으면 AI는 부르지 않는다."""
    resident_fields = find_resident_numbers(sections)
    if resident_fields:
        return PrivacyResult(resident_fields=resident_fields)
    try:
        return PrivacyResult(findings=find_private_info(user, sections))
    except ProviderError as error:
        return PrivacyResult(error=str(error))


def _digest(sections):
    # 브라우저가 줄바꿈을 \r\n 으로 보내도 같은 내용이면 같은 값이 나오게 한다.
    text = "\x1f".join("\n".join(sections[name].splitlines()) for name in sorted(sections))
    return hashlib.sha256(text.encode()).hexdigest()


def confirm_token(sections):
    """지적을 보여 준 내용에 묶인 확인 값. [그래도 저장]을 누르면 폼이 함께 보낸다."""
    return signing.dumps(_digest(sections), salt=CONFIRM_SALT)


def is_confirmed(sections, token):
    if not token:
        return False
    try:
        digest = signing.loads(token, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE)
    except signing.BadSignature:
        return False
    return digest == _digest(sections)
