"""지원 건 분석 그래프 (LangGraph).

    기업 조사 ──(지침 없음)──▶ 끝
        │
        ▼
    적합도 평가 ─▶ 지원동기 작성 ─▶ 검수 ──(통과)──▶ 끝
                        ▲              │
                        ├─(지적 있음)──┤
                        │              ▼ (재작성 한도까지 써도 지적이 남음)
                        └─(다시 쓰기)─ 사람 확인 ──(이대로 저장)──▶ 끝

- 각 노드는 agents.llm.ask() 로 AI를 한 번 부르고, 호출마다 AgentRun 기록이 남는다.
- 사람 확인 노드는 interrupt() 로 그래프를 멈춘다. 멈춘 상태는 체크포인터가 DB에 저장하고,
  사용자가 상세 화면에서 답하면 Command(resume=...) 로 그 자리에서 이어 간다.
- 상태(AnalysisState)는 체크포인트에 저장되므로 글자·숫자만 담는다. 모델 객체는 실행할 때마다
  context(AnalysisContext)로 넘긴다.
"""

import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, TypedDict

from django.conf import settings
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from psycopg.conninfo import make_conninfo
from pydantic import BaseModel, Field

from agents import llm
from agents.models import Role

from . import prompts
from .models import Application, Company
from .scoring import EXTRA_MAX, VERDICT_RATIOS, score_evaluation

# 검수에서 지적받았을 때 AI끼리 다시 쓰게 하는 최대 횟수. 넘으면 사람에게 묻는다.
MAX_REVISIONS = 2

# 사람 확인에서 고를 수 있는 답
ACCEPT = "accept"  # 이대로 저장
REVISE = "revise"  # 지시를 주고 다시 쓰기

Verdict = Literal[tuple(VERDICT_RATIOS)]
SIZE_BY_LABEL = {label: value for value, label in Company.Size.choices}

# 기업 조사 답변 끝의 "[기업 정보] / 업종: … / 규모: …" 부분
COMPANY_INFO_PATTERN = re.compile(
    re.escape(prompts.COMPANY_INFO_HEADER)
    + r"\s*업종\s*:\s*(?P<industry>[^\n]*)\s*규모\s*:\s*(?P<size>[^\n]*)\s*$"
)


class Judgement(BaseModel):
    item: str = Field(description="공고에 적힌 항목 하나 (원문을 짧게 옮김)")
    verdict: Verdict = Field(description="지원자 프로필에 비춘 판정")
    reason: str = Field(description="판정 근거가 되는 프로필 내용 한 문장")


class Evaluation(BaseModel):
    """평가 에이전트의 답. 점수와 등급은 여기에 없고 scoring.py 가 판정으로 계산한다."""

    requirements: list[Judgement] = Field(description="자격 요건 항목별 판정")
    preferred: list[Judgement] = Field(description="우대 사항 항목별 판정")
    tasks: list[Judgement] = Field(description="주요 업무 항목별로 관련 경험이 있는지 판정")
    extra_score: int = Field(description=f"그 밖의 적합도, 0~{EXTRA_MAX} 정수")
    extra_reason: str = Field(description="extra_score 의 근거 한두 문장")
    summary: str = Field(description="강점과 보완할 점 종합")


class Draft(BaseModel):
    motivation_draft: str = Field(description="지원동기 초안")
    title_candidates: list[str] = Field(description="지원동기 제목 후보 3~5개")


class Review(BaseModel):
    approved: bool = Field(description="문제가 하나도 없으면 true")
    issues: list[str] = Field(description="지적 사항. 없으면 빈 목록")


@dataclass
class AnalysisContext:
    """실행할 때마다 넘기는 객체들. 체크포인트에는 저장되지 않는다."""

    user: object
    application: Application
    guideline: object = None  # GuidelineVersion 또는 None
    job: object = None  # AgentJob 또는 None


class AnalysisState(TypedDict, total=False):
    has_guideline: bool  # 입력. 없으면 기업 조사만 한다.
    company_analysis: str
    industry: str
    size: str
    fit_evaluation: str
    fit_score: int
    fit_grade: str
    motivation_draft: str
    title_candidates: str
    approved: bool
    review_issues: list[str]
    revisions: int
    human_decision: str  # ACCEPT / REVISE
    human_instructions: list[str]  # 사람이 준 지시. 작성과 검수에 계속 반영한다.


def _ask(runtime, role, revisions=0, **kwargs):
    """현재 단계를 작업에 적어 두고(상세 화면 진행 표시용) AI를 부른다."""
    context = runtime.context
    if context.job:
        context.job.step = role
        context.job.revisions = revisions
        context.job.save(update_fields=["step", "revisions", "updated_at"])
    return llm.ask(
        context.user, role, job=context.job, application=context.application, **kwargs
    )


def split_company_info(text):
    """기업 조사 답변을 (기업 분석 본문, 업종, 규모 값)으로 나눈다."""
    match = COMPANY_INFO_PATTERN.search(text)
    if not match:
        return text.strip(), "", ""
    industry = match["industry"].strip()
    if industry == prompts.UNKNOWN:
        industry = ""
    # "대기업 (직원 1,000명 이상)"처럼 설명을 덧붙여도 읽을 수 있게 라벨이 들어 있는지만 본다.
    size = next(
        (value for label, value in SIZE_BY_LABEL.items() if label in match["size"]), ""
    )
    return text[: match.start()].strip(), industry[:100], size


def _shared_blocks(state, context):
    """평가·작성·검수가 함께 쓰는 앞부분: 지침 → 기업(기업 분석 포함) → 공고."""
    application = context.application
    return [
        prompts.guideline_block(context.guideline),
        prompts.company_block(application.company, state["company_analysis"]),
        prompts.posting_block(application),
    ]


def _draft_block(state):
    return (
        "# 지원동기 초안\n\n"
        + state["motivation_draft"]
        + "\n\n# 제목 후보\n\n"
        + state["title_candidates"]
    )


def _human_block(state):
    instructions = state.get("human_instructions")
    if not instructions:
        return ""
    return "# 사용자 지시 (다른 규칙보다 먼저 따른다)\n\n" + "\n".join(
        f"- {instruction}" for instruction in instructions
    )


def research(state: AnalysisState, runtime: Runtime[AnalysisContext]):
    application = runtime.context.application
    answer = _ask(
        runtime,
        Role.RESEARCH,
        system=prompts.RESEARCH_SYSTEM.format(max_searches=llm.MAX_WEB_SEARCHES),
        text=prompts.build_prompt(
            prompts.company_block(application.company),
            prompts.posting_block(application),
        ),
        web_search=True,
    )
    company_analysis, industry, size = split_company_info(answer)
    return {"company_analysis": company_analysis, "industry": industry, "size": size}


def evaluate(state: AnalysisState, runtime: Runtime[AnalysisContext]):
    result = _ask(
        runtime,
        Role.EVALUATE,
        system=prompts.EVALUATE_SYSTEM,
        text=prompts.build_prompt(*_shared_blocks(state, runtime.context)),
        schema=Evaluation,
    )
    score, grade, text = score_evaluation(result)
    return {"fit_evaluation": text, "fit_score": score, "fit_grade": grade}


def write(state: AnalysisState, runtime: Runtime[AnalysisContext]):
    blocks = [
        *_shared_blocks(state, runtime.context),
        "# 적합도 평가\n\n" + state["fit_evaluation"],
    ]
    revisions = state.get("revisions", 0)
    if state.get("motivation_draft"):  # 검수나 사람 확인에서 되돌아온 경우
        revisions += 1
        blocks.append(_draft_block(state))
        if state.get("review_issues"):
            blocks.append(
                "# 검수 지적 사항\n\n"
                + "\n".join(f"- {issue}" for issue in state["review_issues"])
            )
    blocks.append(_human_block(state))
    result = _ask(
        runtime,
        Role.WRITE,
        revisions,
        system=prompts.WRITE_SYSTEM,
        text=prompts.build_prompt(*blocks),
        schema=Draft,
    )
    return {
        "revisions": revisions,
        "motivation_draft": result.motivation_draft.strip(),
        "title_candidates": "\n".join(
            title.strip() for title in result.title_candidates if title.strip()
        ),
    }


def review(state: AnalysisState, runtime: Runtime[AnalysisContext]):
    result = _ask(
        runtime,
        Role.REVIEW,
        state.get("revisions", 0),
        system=prompts.REVIEW_SYSTEM,
        text=prompts.build_prompt(
            *_shared_blocks(state, runtime.context),
            _draft_block(state),
            _human_block(state),
        ),
        schema=Review,
    )
    issues = [issue.strip() for issue in result.issues if issue.strip()]
    # 지적이 없는데 불합격이라고 하면 되돌려도 고칠 것이 없으므로 통과로 본다.
    return {"approved": result.approved or not issues, "review_issues": issues}


def human_review(state: AnalysisState):
    """AI끼리 해결하지 못한 지적을 사람에게 보여 주고 답을 기다린다.

    interrupt() 에서 그래프가 멈추고, 재개하면 사용자의 답이 반환값으로 들어온다.
    답: {"action": ACCEPT} 또는 {"action": REVISE, "instruction": "…"}
    """
    answer = interrupt(
        {
            "question": "검수 지적이 남았습니다. 이대로 저장할까요, 지시를 주고 다시 쓸까요?",
            "issues": state["review_issues"],
            "revisions": state.get("revisions", 0),
        }
    )
    if answer.get("action") != REVISE:
        return {"human_decision": ACCEPT}
    instructions = list(state.get("human_instructions", []))
    if answer.get("instruction", "").strip():
        instructions.append(answer["instruction"].strip())
    return {"human_decision": REVISE, "human_instructions": instructions}


def after_research(state: AnalysisState):
    # 지침 버전이 없으면 평가 기준이 없으므로 기업 조사만 하고 끝낸다.
    return "evaluate" if state.get("has_guideline") else END


def after_review(state: AnalysisState):
    if state["approved"]:
        return END
    if state.get("revisions", 0) < MAX_REVISIONS:
        return "write"
    return "human_review"


def after_human_review(state: AnalysisState):
    return "write" if state["human_decision"] == REVISE else END


def build_graph():
    graph = StateGraph(AnalysisState, context_schema=AnalysisContext)
    graph.add_node("research", research)
    graph.add_node("evaluate", evaluate)
    graph.add_node("write", write)
    graph.add_node("review", review)
    graph.add_node("human_review", human_review)
    graph.add_edge(START, "research")
    graph.add_conditional_edges("research", after_research, ["evaluate", END])
    graph.add_edge("evaluate", "write")
    graph.add_edge("write", "review")
    graph.add_conditional_edges("review", after_review, ["write", "human_review", END])
    graph.add_conditional_edges("human_review", after_human_review, ["write", END])
    return graph


graph_builder = build_graph()

_setup_lock = threading.Lock()
_setup_done = False


@contextmanager
def open_checkpointer():
    """멈춘 그래프의 상태를 PostgreSQL에 저장하는 체크포인터. 표는 처음 쓸 때 만든다."""
    global _setup_done
    database = settings.DATABASES["default"]
    conninfo = make_conninfo(
        dbname=database["NAME"],
        user=database["USER"],
        password=database["PASSWORD"],
        host=database["HOST"],
        port=database["PORT"],
    )
    with PostgresSaver.from_conn_string(conninfo) as saver:
        with _setup_lock:
            if not _setup_done:
                saver.setup()
                _setup_done = True
        yield saver


@contextmanager
def open_graph():
    """체크포인터를 붙여 컴파일한 그래프. 실행하거나 재개할 때마다 연다."""
    with open_checkpointer() as saver:
        yield graph_builder.compile(checkpointer=saver)
