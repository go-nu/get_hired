"""skill 불러오기.

skill 은 에이전트 한 역할의 지시문과 체크리스트를 담은 파일(skills/<이름>/SKILL.md)이다.
파일 앞머리(--- 사이)에 이름·설명·쓸 도구를 적고, 그 아래 본문이 그대로 시스템 지시가 된다.
부를 때마다 파일을 새로 읽으므로, 코드를 고치거나 서버를 다시 켜지 않고 내용을 바꿀 수 있다.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SKILLS_DIR = settings.BASE_DIR / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tools: tuple
    instructions: str  # 본문. {이름} 자리에 실행할 때 값을 채운다.

    def render(self, **values):
        return self.instructions.format(**values)


def load_skill(name):
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        raise ImproperlyConfigured(f"skill 파일이 없습니다: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        _, front, body = text.split("---", 2)
    except ValueError:
        raise ImproperlyConfigured(f"skill 파일의 앞머리(--- 사이) 형식이 맞지 않습니다: {path}")
    meta = dict(
        (key.strip(), value.strip())
        for key, _, value in (line.partition(":") for line in front.strip().splitlines())
    )
    return Skill(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        tools=tuple(tool.strip() for tool in meta.get("tools", "").split(",") if tool.strip()),
        instructions=body.strip() + "\n",
    )


def list_skills():
    return [load_skill(path.parent.name) for path in sorted(SKILLS_DIR.glob("*/SKILL.md"))]
