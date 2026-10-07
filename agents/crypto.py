"""API 키를 DB에 넣기 전에 암호화한다.

암호화 키는 .env 의 SECRET_KEY 에서 만든다. SECRET_KEY 를 바꾸면 저장된 API 키는
풀 수 없게 되므로 사용자 페이지에서 다시 입력해야 한다.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode() if text else ""


def decrypt(token):
    """풀 수 없는 값(SECRET_KEY 변경 등)은 키가 없는 것으로 본다."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
