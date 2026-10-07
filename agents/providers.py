"""Claude·Gemini 호출을 한 모양으로 감싼다. 다른 코드는 complete() 만 쓴다."""

import base64
from dataclasses import dataclass

import anthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from .models import ClaudeModel, Provider

MAX_OUTPUT_TOKENS = 16000

# 안전 분류기가 요청을 거절하면 API가 다른 모델로 다시 실행해 준다. (지원 모델만)
CLAUDE_FALLBACK_MODELS = (ClaudeModel.OPUS, ClaudeModel.SONNET)
CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ProviderError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 한국어 오류 메시지를 담는다."""


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    parsed: object = None  # 구조화 출력을 파싱한 객체 (agents/llm.py 에서 채운다)


def friendly_error(error):
    """SDK 오류를 사용자에게 보여 줄 한국어 문장으로 바꾼다. 모르는 오류면 None."""
    if isinstance(error, anthropic.APIError):
        if isinstance(error, anthropic.AuthenticationError):
            return "Claude API 키가 올바르지 않습니다."
        if isinstance(error, anthropic.PermissionDeniedError):
            return "이 Claude API 키로는 해당 모델을 쓸 수 없습니다."
        if isinstance(error, anthropic.NotFoundError):
            return "Claude 모델 이름을 찾을 수 없습니다. 사용자 페이지에서 확인하세요."
        if isinstance(error, anthropic.RateLimitError):
            return "Claude 사용량 한도에 걸렸습니다. 잠시 뒤 다시 시도하세요."
        if isinstance(error, anthropic.APIStatusError):
            return f"Claude 오류({error.status_code}): {error.message}"
        return "Claude 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요."
    if isinstance(error, genai_errors.APIError):
        if error.code in (401, 403) or "API key" in (error.message or ""):
            return "Gemini API 키가 올바르지 않습니다."
        if error.code == 404:
            return "Gemini 모델 이름을 찾을 수 없습니다. 사용자 페이지에서 확인하세요."
        if error.code == 429:
            return "Gemini 사용량 한도에 걸렸습니다. 잠시 뒤 다시 시도하세요."
        return f"Gemini 오류({error.code}): {error.message}"
    return None


def complete(agent_settings, *, system, text, images=(), schema=None):
    """선택한 AI에 한 번 요청한다.

    images: (바이트, MIME 타입) 목록. schema: 주면 그 JSON 스키마에 맞는 JSON 문자열로 답한다.
    """
    provider = agent_settings.provider
    api_key = agent_settings.get_api_key()
    if not api_key:
        raise ProviderError(
            f"{agent_settings.get_provider_display()} API 키가 없습니다. "
            "사용자 페이지에서 먼저 입력하세요."
        )
    call = _complete_claude if provider == Provider.CLAUDE else _complete_gemini
    return call(api_key, agent_settings.model, system, text, images, schema)


def _complete_claude(api_key, model, system, text, images, schema):
    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": base64.standard_b64encode(data).decode(),
            },
        }
        for data, mime_type in images
    ]
    content.append({"type": "text", "text": text})
    request = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    if schema:
        request["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    client = anthropic.Anthropic(api_key=api_key)
    try:
        if model in CLAUDE_FALLBACK_MODELS:
            response = client.beta.messages.create(
                betas=[CLAUDE_FALLBACK_BETA], fallbacks="default", **request
            )
        else:
            response = client.messages.create(**request)
    except anthropic.APIError as error:
        raise ProviderError(friendly_error(error)) from error

    if response.stop_reason == "refusal":
        raise ProviderError("Claude가 이 요청을 처리하지 않았습니다.")
    if response.stop_reason == "max_tokens":
        raise ProviderError("답변이 너무 길어 중간에 끊겼습니다.")
    usage = response.usage
    return Completion(
        text="".join(block.text for block in response.content if block.type == "text"),
        input_tokens=(
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        ),
        output_tokens=usage.output_tokens,
    )


def _complete_gemini(api_key, model, system, text, images, schema):
    contents = [
        genai_types.Part.from_bytes(data=data, mime_type=mime_type)
        for data, mime_type in images
    ]
    contents.append(text)
    config = {"system_instruction": system, "max_output_tokens": MAX_OUTPUT_TOKENS}
    if schema:
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = schema

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=genai_types.GenerateContentConfig(**config),
        )
    except genai_errors.APIError as error:
        raise ProviderError(friendly_error(error)) from error
    except OSError as error:  # 연결 실패
        raise ProviderError("Gemini 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.") from error

    if not response.text:
        raise ProviderError("Gemini가 답변을 돌려주지 않았습니다.")
    usage = response.usage_metadata
    return Completion(
        text=response.text,
        input_tokens=usage.prompt_token_count or 0,
        # 생각(thinking)에 쓴 토큰도 출력 요금으로 계산된다.
        output_tokens=(usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
    )
