"""분석 결과 생성·저장 로직. 뷰는 여기 함수만 호출한다.

- save_manual_analysis: 붙여넣은 결과를 저장 (source=manual)
- start_analysis → generate_analysis: 에이전트 그래프로 생성해 저장 (source=api)
"""

import json
import logging
import threading
from datetime import timedelta
import uuid

from django.db import connection, transaction
from django.utils import timezone
from langgraph.types import Command

from agents.models import AgentJob, AgentRun, AgentSettings, Status
from agents.notify import application_link, notify_user
from agents.providers import ProviderError
from agents.services import run_agent

from .analysis_graph import ACCEPT, REVISE, AnalysisContext, open_graph
from .models import URGENT_DAYS, Analysis, Application, Company

logger = logging.getLogger(__name__)

# 공고 캡처에서 읽어 채우는 폼 칸
POSTING_FIELDS = ("main_tasks", "requirements", "preferred")

POSTING_OCR_SYSTEM = """\
채용 공고 화면을 캡처한 이미지에서 글자를 읽어 세 항목으로 나눠 옮겨 적는다.

- main_tasks: 주요 업무 (담당 업무, 하는 일 등)
- requirements: 자격 요건 (필수 요건, 지원 자격 등)
- preferred: 우대 사항

규칙:
- 이미지에 적힌 문장을 그대로 옮긴다. 요약하거나 고쳐 쓰거나 내용을 보태지 않는다.
- 목록의 각 줄은 "- "로 시작하고 한 줄에 하나씩 적는다.
- 이미지에 없는 항목은 빈 문자열로 둔다.
- 세 항목에 속하지 않는 내용(복지, 전형 절차, 회사 소개 등)은 옮기지 않는다.
- 이미지가 여러 장이면 순서대로 이어진 하나의 공고로 본다.
"""

POSTING_OCR_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in POSTING_FIELDS},
    "required": list(POSTING_FIELDS),
    "additionalProperties": False,
}

MEMO_OCR_SYSTEM = """\
이미지에 적힌 글자를 그대로 옮겨 적는다.

- 요약하거나 고쳐 쓰거나 설명을 덧붙이지 않는다. 읽은 글자만 답한다.
- 줄바꿈과 목록 구조는 이미지와 같게 유지한다.
- 이미지가 여러 장이면 순서대로 이어 적는다.
- 글자가 없으면 아무것도 적지 않는다.
"""

ANALYSIS_FIELDS = (
    "company_analysis",
    "fit_evaluation",
    "motivation_draft",
    "title_candidates",
)


def read_posting_images(user, images):
    """공고 캡처 이미지((바이트, MIME 타입) 목록)를 읽어 폼 칸별 글로 돌려준다."""
    run = run_agent(
        user,
        AgentRun.Role.OCR,
        system=POSTING_OCR_SYSTEM,
        text="이 채용 공고 캡처를 읽어 주세요.",
        images=images,
        schema=POSTING_OCR_SCHEMA,
    )
    try:
        data = json.loads(run.output)
    except json.JSONDecodeError as error:
        raise ProviderError("읽은 결과를 해석하지 못했습니다. 다시 시도하세요.") from error
    return {name: str(data.get(name, "")).strip() for name in POSTING_FIELDS}


def read_memo_images(user, images):
    """메모 칸에 붙여넣은 이미지((바이트, MIME 타입) 목록)의 글자를 그대로 읽어 돌려준다."""
    run = run_agent(
        user,
        AgentRun.Role.OCR,
        system=MEMO_OCR_SYSTEM,
        text="이 이미지의 글자를 옮겨 적어 주세요.",
        images=images,
    )
    return run.output.strip()


def generate_analysis(application, guideline_version, *, user, job=None, resume=None):
    """분석 그래프를 돌려 결과를 Analysis(source=api)로 저장한다.

    (Analysis, 오류 문장, 사람 확인을 기다리는지)를 돌려준다.
    - 지침 버전이 없으면 기업 조사만 한다.
    - 중간에 실패해도 그때까지 나온 결과는 저장한다.
    - resume: 사람 확인에서 멈춘 작업을 이어 갈 때 사용자의 답. (job 이 있어야 한다)
    """
    thread_id = f"job-{job.pk}" if job else f"direct-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}
    context = AnalysisContext(
        user=user, application=application, guideline=guideline_version, job=job
    )
    if resume:
        graph_input = Command(resume=resume)
    else:
        graph_input = {"has_guideline": guideline_version is not None}

    error = ""
    with open_graph() as graph:
        try:
            graph.invoke(graph_input, config, context=context)
        except ProviderError as failure:
            error = str(failure)
        # 실패했어도 체크포인트에는 마지막으로 끝난 단계까지의 상태가 남아 있다.
        snapshot = graph.get_state(config)
        state = snapshot.values
        waiting = bool(snapshot.interrupts) and not error
        if not waiting:
            graph.checkpointer.delete_thread(thread_id)

    if resume and not error and state.get("human_decision") == ACCEPT:
        # "이대로 저장": 멈출 때 저장해 둔 결과를 그대로 둔다.
        return None, error, waiting
    return _save_analysis_state(application, state), error, waiting


def _save_analysis_state(application, state):
    company = application.company
    updates = {
        name: state[name]
        for name in ("industry", "size")
        # 직접 적어 둔 값은 덮어쓰지 않는다.
        if state.get(name) and not getattr(company, name)
    }
    if updates:
        Company.objects.filter(pk=company.pk).update(**updates)

    if state.get("fit_score") is not None:
        # 실행 중에 사용자가 지원 건을 고쳤을 수 있으므로 적합도 칸만 바꾼다.
        Application.objects.filter(pk=application.pk).update(
            fit_score=state["fit_score"], fit_grade=state["fit_grade"]
        )

    fields = {name: state.get(name, "") for name in ANALYSIS_FIELDS}
    if not any(fields.values()):
        return None
    issues = [] if state.get("approved", True) else state.get("review_issues", [])
    return Analysis.objects.create(
        application=application,
        source=Analysis.Source.API,
        review_notes="\n".join(f"- {issue}" for issue in issues),
        **fields,
    )


def _run_in_background(job, resume=None):
    # 요청의 DB 저장이 끝난 뒤에 시작해야 스레드가 바뀐 내용을 읽을 수 있다.
    transaction.on_commit(
        lambda: threading.Thread(
            target=run_analysis_job, args=(job.pk, resume), daemon=True
        ).start()
    )


def start_analysis(application, user):
    """분석을 백그라운드로 시작하고 (AgentJob, 시작하지 못한 이유)를 돌려준다."""
    if not AgentSettings.for_user(user).get_api_key():
        return None, "API 키가 없어 에이전트 분석을 시작하지 못했습니다. 사용자 페이지에서 입력하세요."
    AgentJob.expire_stale()
    active = application.agent_jobs.filter(status__in=(Status.RUNNING, Status.WAITING)).first()
    if active:
        if active.status == Status.WAITING:
            return None, "에이전트가 확인을 기다리고 있습니다. 먼저 답해 주세요."
        return None, "이 지원 건은 이미 에이전트가 분석하고 있습니다."
    job = AgentJob.objects.create(user=user, application=application)
    _run_in_background(job)
    return job, ""


def resume_analysis(application, action, instruction=""):
    """사람 확인에서 멈춘 분석에 답해 이어 가게 한다. (AgentJob, 못 한 이유)를 돌려준다."""
    job = application.agent_jobs.filter(status=Status.WAITING).first()
    if job is None:
        return None, "확인을 기다리는 분석이 없습니다."
    if action not in (ACCEPT, REVISE):
        return None, "알 수 없는 선택입니다."
    instruction = instruction.strip()
    if action == REVISE and not instruction:
        return None, "다시 쓰게 하려면 지시 사항을 적어 주세요."
    job.status = Status.RUNNING
    job.save(update_fields=["status", "updated_at"])
    _run_in_background(job, {"action": action, "instruction": instruction})
    return job, ""


def run_analysis_job(job_id, resume=None):
    """백그라운드 스레드의 본체."""
    try:
        job = AgentJob.objects.select_related(
            "user", "application__company", "application__guideline_version"
        ).get(pk=job_id)
        waiting = False
        try:
            _, error, waiting = generate_analysis(
                job.application,
                job.application.guideline_version,
                user=job.user,
                job=job,
                resume=resume,
            )
        except Exception as failure:  # 그래프 밖의 예상하지 못한 오류
            error = f"{type(failure).__name__}: {failure}"
        if waiting:
            job.status = Status.WAITING
            job.save(update_fields=["status", "revisions", "updated_at"])
        else:
            job.finish(error)
        notify_job_result(job)
    finally:
        connection.close()


def notify_job_result(job):
    """분석 작업이 멈추거나 끝났을 때 디스코드로 알린다. 알림이 실패해도 작업 결과는 그대로다."""
    try:
        notify_user(job.user, job_result_text(job))
    except Exception:  # 알림 때문에 스레드가 죽지 않게 한다
        logger.exception("디스코드 알림 중 오류")


# 아래 *_text 함수는 알림에 보낼 글을 만든다. 자동 알림과 사용자 페이지의 [보내기] 버튼이 함께 쓴다.


def job_result_text(job):
    """분석 작업의 결과(완료·실패·확인 대기) 알림 글."""
    application = Application.objects.select_related("company").get(pk=job.application_id)
    if job.is_waiting:
        headline = "[확인 대기] 검수 지적이 남아 에이전트가 답을 기다립니다."
    elif job.error:
        headline = f"[분석 실패] {job.error}"
    elif application.fit_score is not None:
        headline = (
            f"[분석 완료] 적합도 {application.fit_score}점"
            f" ({application.get_fit_grade_display()})"
        )
    else:
        headline = "[분석 완료] 기업 조사를 마쳤습니다."
    return f"{headline}\n{application}\n{application_link(application)}"


def latest_job_result_text(user):
    """가장 최근에 끝났거나 멈춘 분석 작업의 알림 글. 없으면 빈 문자열."""
    job = (
        AgentJob.objects.filter(user=user, application__in=Application.objects.visible())
        .exclude(status=Status.RUNNING)
        .order_by("-updated_at")
        .first()
    )
    return job_result_text(job) if job else ""


def waiting_alert_text(user):
    """에이전트가 사람의 답을 기다리는 건 목록. 없으면 빈 문자열."""
    jobs = (
        AgentJob.objects.filter(
            user=user, status=Status.WAITING, application__in=Application.objects.visible()
        )
        .select_related("application__company")
        .order_by("updated_at")
    )
    if not jobs:
        return ""
    lines = [f"[확인 대기] 에이전트가 답을 기다리는 {len(jobs)}건"]
    lines += [f"- {job.application} · {application_link(job.application)}" for job in jobs]
    return "\n".join(lines)


def deadline_alert_text():
    """마감이 임박한 미지원 건 목록. 없으면 빈 문자열."""
    applications = urgent_applications()
    if not applications:
        return ""
    lines = [f"[마감 임박] 아직 지원하지 않은 {len(applications)}건"]
    lines += [
        f"- {application.d_day_label} · {application} · {application_link(application)}"
        for application in applications
    ]
    return "\n".join(lines)


def urgent_applications():
    """마감 3일 이내인데 아직 지원하지 않은 건. (마감 알림 대상)"""
    today = timezone.localdate()
    candidates = (
        Application.objects.visible()
        .select_related("company")
        .filter(deadline__range=(today, today + timedelta(days=URGENT_DAYS)))
        .order_by("deadline")
    )
    return [application for application in candidates if application.is_urgent]


def save_manual_analysis(application, data):
    """Claude 앱에서 붙여넣은 분석 결과를 새 Analysis로 저장한다."""
    fields = {name: data.get(name, "") for name in ANALYSIS_FIELDS}
    return Analysis.objects.create(
        application=application, source=Analysis.Source.MANUAL, **fields
    )
