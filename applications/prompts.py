"""분석 그래프 노드의 프롬프트.

조립 순서: 시스템 지시 → 지침(고정 정보 → 작성 규칙 → 요청 사항/출력 형식 → 기타)
→ 기업 → 공고 → 앞 단계 결과. 기업 조사에는 지침을 넣지 않는다.
이 순서를 바꿀 때는 먼저 확인을 받는다. (CLAUDE.md)
"""

from .models import Company

UNKNOWN = "모름"
COMPANY_INFO_HEADER = "[기업 정보]"

RESEARCH_SYSTEM = f"""\
채용 공고를 낸 기업을 조사해, 지원자가 지원 여부를 판단하고 지원동기를 쓸 때 참고할 기업 분석을 쓴다.

- 웹 검색은 최대 {{max_searches}}번만 쓴다. 기업 공식 홈페이지, 최근 기사, 채용 페이지를 우선 본다.
- 다룰 내용: 사업 내용과 주력 제품·서비스, 업계에서의 위치, 최근 소식, 이 직무가 회사에서 맡는 역할.
- 검색으로 확인한 사실만 쓴다. 확인하지 못한 것은 추측하지 말고 "확인하지 못함"이라고 적는다.
- 이름이 같은 다른 회사와 헷갈리지 않게 공고 내용과 맞는 회사인지 확인한다.
- 한국어로, 소제목과 짧은 목록으로 정리한다.

답변의 맨 마지막에는 아래 세 줄을 형식 그대로 붙인다.

{COMPANY_INFO_HEADER}
업종: (한 줄로. 모르면 {UNKNOWN})
규모: ({", ".join(Company.Size.labels)}, {UNKNOWN} 중 하나)
"""

EVALUATE_SYSTEM = """\
지원자의 프로필과 채용 공고를 견주어 적합도를 평가한다.

- fit_evaluation: 평가 근거. 자격 요건과 우대 사항을 항목별로 지원자 프로필과 대조해 충족, 부분 충족, 미충족을 밝히고, 강점과 보완할 점을 정리한다.
- fit_score: 0~100 정수. 필수 자격 요건 충족 여부를 가장 크게 반영한다.
- fit_grade: 점수와 별개로 종합 판단한 등급.
- 프로필에 적힌 사실만 근거로 삼는다. 프로필에 없는 경험이나 역량을 있다고 가정하지 않는다.
- 지침의 작성 규칙과 요청 사항 중 평가에 해당하는 내용을 따른다.
"""

WRITE_SYSTEM = """\
지원자의 프로필, 기업 분석, 적합도 평가를 바탕으로 지원동기 초안과 제목 후보를 쓴다.

- motivation_draft: 지원동기 초안. 지침의 작성 규칙과 요청 사항/출력 형식을 그대로 따른다.
- title_candidates: 지원동기 제목 후보 3~5개.
- 프로필에 적힌 경험과 사실만 쓴다. 없는 경력, 수치, 성과를 지어내지 않는다.
- 기업 분석에서 확인된 내용만 회사 이야기로 쓴다.
- 검수 지적 사항이 주어지면 이전 초안을 그 지적에 맞게 고쳐 쓴다. 지적받지 않은 부분은 유지한다.
"""

REVIEW_SYSTEM = """\
지원동기 초안과 제목 후보를 검수한다. 직접 고쳐 쓰지 않고 문제만 지적한다.

점검할 것:
1. 지침의 작성 규칙과 요청 사항/출력 형식을 어긴 곳
2. 지원자 프로필에 없는 경험, 경력, 수치, 성과를 쓴 곳
3. 기업 분석이나 공고에 없는 회사 정보를 사실처럼 쓴 곳

- approved: 위 세 가지에 해당하는 문제가 하나도 없을 때만 true.
- issues: 문제를 한 항목에 하나씩, 초안의 어느 부분이 어떤 규칙에 어긋나는지 구체적으로 적는다. 문제가 없으면 빈 목록.
- 문체 취향이나 더 나은 표현 제안은 문제로 세지 않는다.
"""


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
