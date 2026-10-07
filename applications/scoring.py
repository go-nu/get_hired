"""적합도 점수와 등급 계산.

AI는 항목별 판정만 하고, 점수 합산과 등급은 여기서 계산한다. 그래서 같은 판정이면
항상 같은 점수·등급이 나온다. 배점이나 등급 구간을 바꿀 때는 이 파일만 고친다.
"""

from .models import Application

# 판정 → 인정 비율
VERDICT_RATIOS = {"충족": 1.0, "부분 충족": 0.5, "미충족": 0.0}

# 자격 요건이 기본 점수이고 나머지는 가점이다. 자격 요건을 모두 충족하면 60점(중하)에서
# 시작해, 가점을 받는 만큼 등급이 오른다. 가점은 우대 사항을 가장 크게 둔다.
# (판정 목록의 이름, 표시 이름, 배점)
SECTIONS = (
    ("requirements", "자격 요건 (기본)", 60),
    ("preferred", "우대 사항 (가점)", 20),
    ("tasks", "주요 업무 관련성 (가점)", 15),
)
EXTRA_LABEL = "기타 (가점)"
EXTRA_MAX = 5

# 이 점수 이상이면 해당 등급. 위에서부터 본다. 60점 미만은 "하".
GRADE_CUTOFFS = (
    (90, Application.FitGrade.HIGH),
    (80, Application.FitGrade.MID_HIGH),
    (70, Application.FitGrade.MID),
    (60, Application.FitGrade.MID_LOW),
)


def grade_for(score):
    for cutoff, grade in GRADE_CUTOFFS:
        if score >= cutoff:
            return grade
    return Application.FitGrade.LOW


def score_evaluation(evaluation):
    """항목별 판정(Evaluation)으로 (점수, 등급, 평가 글)을 만든다.

    공고에 없는 영역(예: 우대 사항이 없는 공고)은 배점에서 빼고 나머지를 100점으로 환산한다.
    """
    lines = []
    earned = 0.0
    possible = 0
    for name, label, weight in SECTIONS:
        items = getattr(evaluation, name)
        if not items:
            lines.append(f"[{label}] 공고에 없음 (배점 제외)")
            continue
        ratio = sum(VERDICT_RATIOS[item.verdict] for item in items) / len(items)
        earned += ratio * weight
        possible += weight
        lines.append(f"[{label}] {ratio * weight:.1f} / {weight}")
        lines.extend(
            f"- {item.verdict} · {item.item.strip()}: {item.reason.strip()}"
            for item in items
        )
    extra = max(0, min(EXTRA_MAX, evaluation.extra_score))
    earned += extra
    possible += EXTRA_MAX
    lines.append(f"[{EXTRA_LABEL}] {extra} / {EXTRA_MAX}: {evaluation.extra_reason.strip()}")

    score = round(earned / possible * 100)
    grade = grade_for(score)
    header = f"총점 {score}점 · 등급 {Application.FitGrade(grade).label}"
    if possible != 100:
        header += f" (배점 {possible}점 만점을 100점으로 환산)"
    text = "\n".join([header, "", *lines, "", "[종합]", evaluation.summary.strip()])
    return score, grade, text
