"""디스코드 알림. 웹훅 주소로 글 한 줄을 보낸다.

알림은 부가 기능이라, 보내지 못해도 원래 하던 일(분석 작업 등)을 막지 않는다.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.urls import reverse

from .models import AgentSettings

logger = logging.getLogger(__name__)

# 디스코드 웹훅 주소만 받는다. 다른 주소로 요청을 보내지 않게 한다.
WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)
MESSAGE_LIMIT = 2000  # 디스코드 글 한 개의 최대 길이
TIMEOUT_SECONDS = 10


def is_discord_webhook(url):
    return url.startswith(WEBHOOK_PREFIXES)


def send_discord(webhook_url, content):
    """글을 보내고 (성공 여부, 실패 이유)를 돌려준다."""
    if not is_discord_webhook(webhook_url):
        return False, "디스코드 웹훅 주소가 아닙니다."
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"content": content[:MESSAGE_LIMIT]}).encode(),
        # 기본 User-Agent(Python-urllib)는 디스코드가 거절한다.
        headers={"Content-Type": "application/json", "User-Agent": "get-hired/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            return True, ""
    except urllib.error.HTTPError as error:
        return False, f"디스코드가 요청을 거절했습니다({error.code}). 웹훅 주소를 확인하세요."
    except (urllib.error.URLError, TimeoutError):
        return False, "디스코드에 연결하지 못했습니다. 인터넷 연결을 확인하세요."


def notify_user(user, content):
    """사용자가 웹훅을 등록해 두었으면 알림을 보낸다. 보냈으면 True."""
    webhook_url = AgentSettings.for_user(user).get_discord_webhook()
    if not webhook_url:
        return False
    ok, reason = send_discord(webhook_url, content)
    if not ok:
        logger.warning("디스코드 알림 실패: %s", reason)
    return ok


def application_link(application):
    return settings.SITE_URL + reverse("applications:detail", args=[application.pk])
