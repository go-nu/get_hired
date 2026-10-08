from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from agents.notify import notify_user
from applications.services import deadline_alert_text


class Command(BaseCommand):
    help = (
        "마감 3일 이내인데 아직 지원하지 않은 건을 디스코드로 알린다. "
        "하루 한 번 돌도록 Windows 작업 스케줄러 등에 등록해 쓴다."
    )

    def handle(self, *args, **options):
        text = deadline_alert_text()
        if not text:
            self.stdout.write("마감이 임박한 미지원 건이 없습니다.")
            return
        # 혼자 쓰는 앱이라 지원 건에 주인이 없다. 웹훅을 등록한 사용자 모두에게 보낸다.
        sent = sum(notify_user(user, text) for user in get_user_model().objects.all())
        if sent:
            self.stdout.write(self.style.SUCCESS("마감 임박 알림을 보냈습니다."))
        else:
            self.stdout.write(
                self.style.WARNING("알림을 보내지 못했습니다. 사용자 페이지에서 디스코드 웹훅 주소를 확인하세요.")
            )
