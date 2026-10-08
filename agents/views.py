from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, ListView, UpdateView

from applications.services import (
    deadline_alert_text,
    latest_job_result_text,
    waiting_alert_text,
)
from applications.views import PageLinksMixin

from .forms import AgentSettingsForm
from .models import AgentRun, AgentSettings, Provider
from .notify import send_discord
from .pricing import KRW_PER_USD, cost_rows, to_krw


def usage_rows(user):
    """사용자 페이지의 토큰 사용량 표: AI별로 이번 달과 전체 합계."""
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    runs = AgentRun.objects.filter(user=user)
    periods = {
        "month": runs.filter(created_at__gte=month_start),
        "total": runs,
    }
    totals = {
        period: {
            row["provider"]: row
            for row in queryset.order_by()
            .values("provider")
            .annotate(
                runs=Count("id"), input=Sum("input_tokens"), output=Sum("output_tokens")
            )
        }
        for period, queryset in periods.items()
    }
    empty = {"runs": 0, "input": 0, "output": 0}
    return [
        {
            "label": label,
            "month": totals["month"].get(value, empty),
            "total": totals["total"].get(value, empty),
        }
        for value, label in Provider.choices
    ]


class AgentSettingsView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    """사용자 페이지: 사용할 AI, API 키, 토큰 사용량."""

    form_class = AgentSettingsForm
    template_name = "agents/settings.html"
    success_url = reverse_lazy("agents:settings")
    success_message = "설정을 저장했습니다."

    def get_object(self, queryset=None):
        return AgentSettings.for_user(self.request.user)

    def form_valid(self, form):
        response = super().form_valid(form)
        # 웹훅 주소를 새로 넣었으면 바로 시험 알림을 보내 연결을 확인한다.
        if form.cleaned_data["discord_webhook"]:
            ok, reason = send_discord(
                form.cleaned_data["discord_webhook"], "취업 활동 관리와 연결되었습니다. 이 채널로 알림을 보냅니다."
            )
            if ok:
                messages.info(self.request, "디스코드로 시험 알림을 보냈습니다.")
            else:
                messages.warning(self.request, f"디스코드 시험 알림 실패: {reason}")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["usage_rows"] = usage_rows(self.request.user)
        context["has_discord_webhook"] = bool(self.object.get_discord_webhook())
        context["discord_alerts"] = [
            {"kind": kind, **alert} for kind, alert in DISCORD_ALERTS.items()
        ]
        return context


# 사용자 페이지에서 직접 보낼 수 있는 알림. text 는 보낼 글을 만들고, 보낼 것이 없으면 빈 문자열을 준다.
DISCORD_ALERTS = {
    "test": {
        "label": "연결 시험",
        "help": "웹훅 주소가 맞는지 확인하는 글을 보냅니다.",
        "text": lambda user: "취업 활동 관리에서 보낸 시험 알림입니다.",
        "empty": "",
    },
    "analysis": {
        "label": "최근 분석 결과",
        "help": "가장 최근에 끝났거나 멈춘 분석 작업의 결과(완료·실패·확인 대기)를 다시 보냅니다.",
        "text": latest_job_result_text,
        "empty": "아직 끝난 분석 작업이 없습니다.",
    },
    "waiting": {
        "label": "확인 대기 목록",
        "help": "에이전트가 답을 기다리는 건을 모아 보냅니다.",
        "text": waiting_alert_text,
        "empty": "확인 대기인 건이 없습니다.",
    },
    "deadline": {
        "label": "마감 임박",
        "help": "마감이 3일 이내인데 아직 지원하지 않은 건을 모아 보냅니다. (매일 자동 알림과 같은 내용)",
        "text": lambda user: deadline_alert_text(),
        "empty": "마감이 임박한 미지원 건이 없습니다.",
    },
}


class DiscordSendView(LoginRequiredMixin, View):
    """사용자 페이지의 알림 [보내기] 버튼."""

    def post(self, request, kind):
        alert = DISCORD_ALERTS.get(kind)
        if alert is None:
            raise Http404
        webhook_url = AgentSettings.for_user(request.user).get_discord_webhook()
        text = alert["text"](request.user)
        if not webhook_url:
            messages.warning(request, "디스코드 웹훅 주소를 먼저 저장하세요.")
        elif not text:
            messages.info(request, alert["empty"])
        else:
            ok, reason = send_discord(webhook_url, text)
            if ok:
                messages.success(request, f"디스코드로 보냈습니다: {alert['label']}")
            else:
                messages.warning(request, f"디스코드 알림 실패: {reason}")
        return redirect("agents:settings")


class AgentRunListView(LoginRequiredMixin, PageLinksMixin, ListView):
    """관리자 페이지: 에이전트 실행 기록."""

    template_name = "agents/run_list.html"
    context_object_name = "runs"

    def get_queryset(self):
        queryset = AgentRun.objects.select_related("application__company", "company")
        status = self.request.GET.get("status")
        role = self.request.GET.get("role")
        if status in AgentRun.Status.values:
            queryset = queryset.filter(status=status)
        if role in AgentRun.Role.values:
            queryset = queryset.filter(role=role)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # 예상 비용은 페이지가 아니라 지금 필터에 걸린 전체 기록으로 계산한다.
        context["cost_rows"], context["cost_total"] = cost_rows(self.object_list)
        context["cost_total_krw"] = to_krw(context["cost_total"])
        context["krw_per_usd"] = KRW_PER_USD
        context.update(
            statuses=AgentRun.Status.choices,
            roles=AgentRun.Role.choices,
            current_status=self.request.GET.get("status", ""),
            current_role=self.request.GET.get("role", ""),
            failed_count=AgentRun.objects.filter(status=AgentRun.Status.FAILED).count(),
        )
        return context


class AgentRunDetailView(LoginRequiredMixin, DetailView):
    model = AgentRun
    template_name = "agents/run_detail.html"
    context_object_name = "run"
