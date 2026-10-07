"""분석 그래프의 노드가 쓰는 LangChain 모델 호출. 노드는 ask() 만 쓴다."""

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from .models import ClaudeModel, Provider
from .providers import MAX_OUTPUT_TOKENS, Completion, ProviderError
from .services import record_run

# 기업 조사 한 번에 허용하는 웹 검색 횟수
MAX_WEB_SEARCHES = 3
# Claude 가 검색 도중 응답을 끊으면(pause_turn) 이어서 요청하는 최대 횟수
MAX_CONTINUATIONS = 3


def chat_model(agent_settings):
    api_key = agent_settings.get_api_key()
    if not api_key:
        raise ProviderError(
            f"{agent_settings.get_provider_display()} API 키가 없습니다. "
            "사용자 페이지에서 먼저 입력하세요."
        )
    if agent_settings.provider == Provider.CLAUDE:
        return ChatAnthropic(
            model=agent_settings.model,
            anthropic_api_key=api_key,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
    return ChatGoogleGenerativeAI(model=agent_settings.model, google_api_key=api_key)


def web_search_tool(agent_settings):
    """AI 회사가 제공하는 웹 검색 도구. 검색은 AI 쪽 서버에서 실행된다."""
    if agent_settings.provider == Provider.CLAUDE:
        # Haiku 4.5 는 예전 버전의 검색 도구만 지원한다.
        version = "20250305" if agent_settings.model == ClaudeModel.HAIKU else "20260209"
        return {
            "type": f"web_search_{version}",
            "name": "web_search",
            "max_uses": MAX_WEB_SEARCHES,
        }
    # Gemini 는 횟수 제한 옵션이 없어 프롬프트로만 제한한다.
    return {"google_search": {}}


def _invoke(agent_settings, system, text, schema, web_search):
    model = chat_model(agent_settings)
    if web_search:
        model = model.bind_tools([web_search_tool(agent_settings)])
    messages = [SystemMessage(system), HumanMessage(text)]

    if schema:
        result = model.with_structured_output(
            schema, method="json_schema", include_raw=True
        ).invoke(messages)
        replies, parsed = [result["raw"]], result["parsed"]
        if parsed is None:
            raise ProviderError("AI의 답변이 정해진 형식과 달라 읽지 못했습니다.")
    else:
        replies, parsed = [model.invoke(messages)], None
        while (
            replies[-1].response_metadata.get("stop_reason") == "pause_turn"
            and len(replies) <= MAX_CONTINUATIONS
        ):
            messages.append(replies[-1])
            replies.append(model.invoke(messages))

    stop_reason = replies[-1].response_metadata.get("stop_reason")
    if stop_reason == "refusal":
        raise ProviderError("AI가 이 요청을 처리하지 않았습니다.")
    if stop_reason == "max_tokens":
        raise ProviderError("답변이 너무 길어 중간에 끊겼습니다.")
    usages = [reply.usage_metadata or {} for reply in replies]
    return Completion(
        text="".join(reply.text for reply in replies).strip(),
        input_tokens=sum(usage.get("input_tokens", 0) for usage in usages),
        output_tokens=sum(usage.get("output_tokens", 0) for usage in usages),
        parsed=parsed,
    )


def ask(
    user, role, *, system, text, schema=None, web_search=False, job=None, application=None
):
    """선택한 AI에 묻고 답을 돌려준다. schema(Pydantic 모델)를 주면 그 객체, 아니면 글."""
    _, completion = record_run(
        user,
        role,
        f"{system}\n\n---\n\n{text}",
        lambda agent_settings: _invoke(agent_settings, system, text, schema, web_search),
        job=job,
        application=application,
        company=application.company if application else None,
    )
    return completion.parsed if schema else completion.text
