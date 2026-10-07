"""분석 그래프 노드의 프롬프트 조립. 역할별 지시문은 skills/<이름>/SKILL.md 에 있다.

조립 순서: 시스템 지시 → 지침(고정 정보 → 작성 규칙 → 요청 사항/출력 형식 → 기타)
→ 기업 → 공고 → 앞 단계 결과. 기업 조사에는 지침을 넣지 않는다.
이 순서를 바꿀 때는 먼저 확인을 받는다. (CLAUDE.md)
"""

UNKNOWN = "모름"
COMPANY_INFO_HEADER = "[기업 정보]"


def _section(title, body):
    return f"## {title}\n{body.strip() or '(없음)'}"


def guideline_block(guideline):
    return "# 지침\n\n" + "\n\n".join(
        _section(label, body) for label, body in guideline.sections()
    )


def company_block(company, company_analysis=""):
    lines = [f"기업명: {company.name}"]
    if company.industry:
        lines.append(f"업종: {company.industry}")
    if company.size:
        lines.append(f"규모: {company.get_size_display()}")
    if company.website:
        lines.append(f"웹사이트: {company.website}")
    block = "# 기업\n\n" + "\n".join(lines)
    if company_analysis:
        block += "\n\n" + _section("기업 분석", company_analysis)
    return block


def posting_block(application):
    parts = [f"직무명: {application.position}"]
    if application.posting_url:
        parts.append(f"공고 URL: {application.posting_url}")
    return "# 공고\n\n" + "\n\n".join(
        [
            "\n".join(parts),
            _section("주요 업무", application.main_tasks),
            _section("자격 요건", application.requirements),
            _section("우대 사항", application.preferred),
        ]
    )


def build_prompt(*blocks):
    return "\n\n".join(block for block in blocks if block)
