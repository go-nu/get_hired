from django.conf import settings
from django.core.management.base import BaseCommand

from applications.analysis_graph import graph_builder


class Command(BaseCommand):
    help = "분석 그래프를 PNG 그림으로 static/images/analysis_graph.png 에 저장한다."

    def handle(self, *args, **options):
        path = settings.BASE_DIR / "static" / "images" / "analysis_graph.png"
        # 그림은 mermaid.ink 웹 서비스가 그려 준다. (인터넷 연결 필요, 그래프 구조가 전송됨)
        png = graph_builder.compile().get_graph().draw_mermaid_png()
        path.write_bytes(png)
        self.stdout.write(self.style.SUCCESS(f"저장했습니다: {path}"))
