from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import Count, Sum
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, ListView, UpdateView

from applications.views import PageLinksMixin

from .forms import AgentSettingsForm
from .models import AgentRun, AgentSettings, Provider
from .notify import send_discord
from .pricing import cost_rows


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
        return context


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
