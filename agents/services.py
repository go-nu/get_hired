"""에이전트 실행. 모든 AI 호출은 record_run() 을 거쳐 AgentRun 기록을 남긴다."""

from django.utils import timezone

from .models import AgentRun, AgentSettings
from .providers import ProviderError, complete, friendly_error


def record_run(
    user, role, prompt, call, *, image_count=0, job=None, application=None, company=None
):
    """call(agent_settings) 을 실행하고 (AgentRun, Completion) 을 돌려준다.

    실패하면 기록을 "실패"로 남긴 뒤 ProviderError 를 던진다.
    """
    agent_settings = AgentSettings.for_user(user)
    run = AgentRun.objects.create(
        user=user,
        role=role,
        provider=agent_settings.provider,
        model=agent_settings.model,
        job=job,
        application=application,
        company=company,
        prompt=prompt,
        image_count=image_count,
    )
    try:
        completion = call(agent_settings)
    except ProviderError as error:
        run.status = AgentRun.Status.FAILED
        run.error = str(error)
        raise
    except Exception as error:
        # 예상하지 못한 오류도 기록에 남겨 관리자 페이지에서 볼 수 있게 한다.
        run.status = AgentRun.Status.FAILED
        run.error = f"{type(error).__name__}: {error}"
        raise ProviderError(
            friendly_error(error)
            or "에이전트 실행 중 오류가 났습니다. 관리자 페이지에서 확인하세요."
        ) from error
    else:
        run.status = AgentRun.Status.SUCCEEDED
        run.output = completion.text
        run.input_tokens = completion.input_tokens
        run.output_tokens = completion.output_tokens
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run, completion


def run_agent(
    user, role, *, system, text, images=(), schema=None, application=None, company=None
):
    """선택한 AI를 한 번 호출하고(단발 호출용) 결과가 담긴 AgentRun 을 돌려준다."""
    run, _ = record_run(
        user,
        role,
        f"{system}\n\n---\n\n{text}",
        lambda agent_settings: complete(
            agent_settings, system=system, text=text, images=images, schema=schema
        ),
        image_count=len(images),
        application=application,
        company=company,
    )
    return run
