from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.core.exceptions import ValidationError
from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, When
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    UpdateView,
)

from agents.models import AgentJob, Status
from agents.providers import ProviderError

from .forms import (
    AnalysisForm,
    AnalysisRunForm,
    ApplicationForm,
    CompanyForm,
    DashboardFilterForm,
    StageForm,
)
from .models import (
    NO_RESPONSE_DAY_OPTIONS,
    OPEN_RESULTS,
    Application,
    Company,
    Result,
    Stage,
)
from .analysis_graph import ACCEPT, REVISE
from .services import (
    ANALYSIS_FIELDS,
    read_memo_images,
    read_posting_images,
    resume_analysis,
    save_manual_analysis,
    start_analysis,
)

# 대시보드 정렬: 키 → (열 제목, 처음 눌렀을 때의 방향)
SORTS = {
    "index": ("번호", "asc"),
    "company": ("기업명", "asc"),
    "deadline": ("마감일", "desc"),
    "stage": ("단계", "asc"),
    "result": ("결과", "asc"),
}
DEFAULT_SORT = "deadline"
# 선택지에 정의된 순서로 정렬하는 열: 키 → (필드, 선택지 값)
CHOICE_SORTS = {
    "stage": ("stage", Stage.values),
    "result": ("result", Result.values),
}


def sort_ordering(sort, direction):
    descending = direction == "desc"
    if sort == "index":  # 번호는 등록 순서
        return ["-created_at", "-id"] if descending else ["created_at", "id"]
    if sort == "company":
        name = F("company__name")
        return [name.desc() if descending else name.asc(), "-created_at"]
    if sort in CHOICE_SORTS:  # Stage, Result 선택지에 정의된 순서
        field, values = CHOICE_SORTS[sort]
        rank = Case(
            *[When(**{field: value}, then=position) for position, value in enumerate(values)],
            output_field=IntegerField(),
        )
        return [rank.desc() if descending else rank.asc(), "-created_at"]
    # 마감일(기본, 내림차순): 마감이 먼 건이 위, 과거일수록 아래.
    # 상시지원(마감일 없음)은 다가오는 마감과 지난 마감 사이에 둔다.
    today = timezone.localdate()
    group = Case(
        When(deadline__gte=today, then=0),
        When(deadline__isnull=True, then=1),
        default=2,
        output_field=IntegerField(),
    )
    if descending:
        return [group.asc(), F("deadline").desc(), "-created_at"]
    return [group.desc(), F("deadline").asc(), "created_at"]


def this_week_range(today=None):
    """오늘이 속한 주의 (월요일, 일요일)."""
    today = today or timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def dashboard_stats():
    """대시보드 상단 카드 숫자. 삭제된 기업의 지원 건은 세지 않는다."""
    visible = Application.objects.visible()
    applied = visible.exclude(stage=Stage.INTEREST)
    monday, sunday = this_week_range()
    by_stage = dict(
        visible.order_by().values_list("stage").annotate(count=Count("id"))
    )
    return {
        "total_applied": applied.count(),
        "in_progress": applied.filter(result__in=OPEN_RESULTS).count(),
        # 이번 주(월~일)에 마감인데 아직 지원하지 않은 건
        "due_this_week": visible.filter(
            stage=Stage.INTEREST,
            result__in=OPEN_RESULTS,
            deadline__range=(monday, sunday),
        ).count(),
        "stages": [(label, by_stage.get(value, 0)) for value, label in Stage.choices],
        # 에이전트가 사람의 답을 기다리는 건
        "waiting": visible.filter(agent_jobs__status=Status.WAITING).distinct().count(),
    }


PAGE_SIZE = 15


class PageLinksMixin:
    """_pagination.html 이 쓰는 값. 페이지 링크에 현재 검색·필터·정렬을 실어 보낸다."""

    paginate_by = PAGE_SIZE

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)
        page = context["page_obj"]
        context["page_query"] = params.urlencode()
        context["page_range"] = page.paginator.get_elided_page_range(
            page.number, on_each_side=2, on_ends=1
        )
        return context


class DashboardView(LoginRequiredMixin, PageLinksMixin, ListView):
    template_name = "applications/dashboard.html"
    context_object_name = "applications"

    def get_filter_form(self):
        return DashboardFilterForm(self.request.GET or None)

    def get_sort(self):
        sort = self.request.GET.get("sort")
        if sort not in SORTS:
            sort = DEFAULT_SORT
        direction = self.request.GET.get("dir")
        if direction not in ("asc", "desc"):
            direction = SORTS[sort][1]
        return sort, direction

    def get_queryset(self):
        queryset = (
            Application.objects.visible()
            .select_related("company")
            .annotate(
                is_waiting=Exists(
                    AgentJob.objects.filter(
                        application=OuterRef("pk"), status=Status.WAITING
                    )
                )
            )
        )
        if self.request.GET.get("waiting"):  # 확인 대기 카드를 눌렀을 때
            queryset = queryset.filter(is_waiting=True)
        form = self.get_filter_form()
        filters = form.cleaned_data if form.is_valid() else {}
        for name in ("stage", "result", "source"):
            if filters.get(name):
                queryset = queryset.filter(**{name: filters[name]})
        return queryset.order_by(*sort_ordering(*self.get_sort()))

    def get_sort_headers(self):
        """정렬 가능한 열 제목. 같은 열을 다시 누르면 방향이 뒤집힌다."""
        current_sort, current_direction = self.get_sort()
        headers = {}
        for key, (label, default_direction) in SORTS.items():
            is_current = key == current_sort
            if is_current:
                direction = "asc" if current_direction == "desc" else "desc"
            else:
                direction = default_direction
            params = self.request.GET.copy()
            params.pop("page", None)  # 정렬을 바꾸면 첫 페이지로
            params["sort"], params["dir"] = key, direction
            headers[key] = {
                "label": label,
                "url": f"?{params.urlencode()}",
                "direction": current_direction if is_current else "",
            }
        return headers

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # 번호는 등록 순서(1부터)로 고정이라 필터·정렬을 바꿔도 같은 건은 같은 번호다.
        numbers = {
            pk: number
            for number, pk in enumerate(
                Application.objects.visible()
                .order_by("created_at", "id")
                .values_list("pk", flat=True),
                start=1,
            )
        }
        for application in context["applications"]:
            application.number = numbers[application.pk]

        sort, direction = self.get_sort()
        context.update(
            filter_form=self.get_filter_form(),
            stats=dashboard_stats(),
            is_filtered=any(
                self.request.GET.get(name)
                for name in ("stage", "result", "source", "waiting")
            ),
            waiting_only=bool(self.request.GET.get("waiting")),
            # 미응답 처리 모달: 가장 짧은 기준의 대상을 모두 내려 주고, 일수 선택은 화면에서 거른다.
            # 가장 오래 응답이 없는 건이 위로 온다.
            no_response_candidates=sorted(
                Application.objects.no_response().select_related("company"),
                key=lambda application: application.days_without_response,
                reverse=True,
            ),
            no_response_day_options=NO_RESPONSE_DAY_OPTIONS,
            sort=sort,
            direction=direction,
            sort_headers=self.get_sort_headers(),
        )
        return context


class ApplicationCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Application
    form_class = ApplicationForm
    template_name = "applications/application_form.html"
    success_message = "지원 건을 등록했습니다."

    def get_success_url(self):
        return reverse("applications:detail", args=[self.object.pk])

    def form_valid(self, form):
        response = super().form_valid(form)
        # 등록하면 에이전트가 바로 기업 조사와 평가를 시작한다.
        notify_analysis_start(self.request, self.object)
        return response


def notify_analysis_start(request, application, **kwargs):
    job, reason = start_analysis(application, request.user, **kwargs)
    if job:
        messages.info(request, "에이전트가 분석을 시작했습니다. 끝나면 이 화면에 결과가 나옵니다.")
    else:
        messages.warning(request, reason)


class AnalysisResumeView(LoginRequiredMixin, View):
    """사람 확인에서 멈춘 분석에 답한다: 이대로 저장하거나, 지시를 주고 다시 쓰게 한다."""

    def post(self, request, pk):
        application = get_object_or_404(Application, pk=pk)
        job, reason = resume_analysis(
            application, request.POST.get("action"), request.POST.get("instruction", "")
        )
        if job is None:
            messages.warning(request, reason)
        elif request.POST.get("action") == REVISE:
            messages.info(request, "지시를 반영해 다시 쓰고 있습니다.")
        else:
            messages.success(request, "지금 결과로 확정했습니다.")
        return redirect("applications:detail", pk=pk)


class AnalysisRunView(LoginRequiredMixin, View):
    """상세 화면의 [에이전트로 분석]: 고른 지침 버전으로 분석 그래프를 다시 돌린다."""

    def post(self, request, pk):
        application = get_object_or_404(Application, pk=pk)
        form = AnalysisRunForm(request.POST)
        if "guideline_version" not in request.POST:  # 고르지 않고 보낸 요청은 적힌 버전 그대로
            notify_analysis_start(request, application)
        elif not form.is_valid():
            messages.warning(request, "지침 버전을 다시 골라 주세요.")
        else:
            notify_analysis_start(
                request, application, guideline_version=form.cleaned_data["guideline_version"]
            )
        return redirect("applications:detail", pk=pk)


class ApplicationUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Application
    form_class = ApplicationForm
    template_name = "applications/application_form.html"
    success_message = "지원 건을 수정했습니다."

    def get_success_url(self):
        return reverse("applications:detail", args=[self.object.pk])


# 공고 캡처 읽기에 올릴 수 있는 이미지 제한
OCR_MAX_IMAGES = 5
OCR_MAX_BYTES = 5 * 1024 * 1024
OCR_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


class PostingOcrView(LoginRequiredMixin, View):
    """등록·수정 폼에서 공고 캡처를 올리면 칸별로 읽은 글을 JSON으로 돌려준다."""

    def read(self, user, images):
        return {"fields": read_posting_images(user, images)}

    def post(self, request):
        files = request.FILES.getlist("images")
        if not files:
            return self.error("이미지를 넣어 주세요.")
        if len(files) > OCR_MAX_IMAGES:
            return self.error(f"이미지는 한 번에 {OCR_MAX_IMAGES}장까지 넣을 수 있습니다.")
        for file in files:
            if file.content_type not in OCR_IMAGE_TYPES:
                return self.error("PNG, JPG, WEBP, GIF 이미지만 넣을 수 있습니다.")
            if file.size > OCR_MAX_BYTES:
                return self.error("이미지 한 장은 5MB 이하여야 합니다.")
        try:
            result = self.read(
                request.user, [(file.read(), file.content_type) for file in files]
            )
        except ProviderError as error:
            return self.error(str(error), status=502)
        return JsonResponse(result)

    @staticmethod
    def error(message, status=400):
        return JsonResponse({"error": message}, status=status)


class MemoOcrView(PostingOcrView):
    """메모 칸에 붙여넣은 이미지의 글자를 그대로 읽어 돌려준다."""

    def read(self, user, images):
        return {"text": read_memo_images(user, images)}


class ApplicationDetailView(LoginRequiredMixin, DetailView):
    model = Application
    template_name = "applications/application_detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return Application.objects.select_related("company", "guideline_version")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        AgentJob.expire_stale()
        context["agent_job"] = self.object.agent_jobs.first()  # 가장 최근 분석 작업
        context["human_actions"] = {"accept": ACCEPT, "revise": REVISE}
        context["analysis_run_form"] = AnalysisRunForm()
        analyses = list(self.object.analyses.all())  # 최신순
        selected_id = self.request.GET.get("analysis")
        analysis = next(
            (a for a in analyses if str(a.pk) == selected_id),
            analyses[0] if analyses else None,
        )
        context.update(
            analyses=analyses,
            analysis=analysis,
            analysis_tabs=[
                (name, analysis._meta.get_field(name).verbose_name, getattr(analysis, name))
                for name in ANALYSIS_FIELDS
            ]
            if analysis
            else [],
            stage_form=StageForm(instance=self.object),
            stage_history=self.object.stage_history.all(),
        )
        return context


class ApplicationStageUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(Application, pk=pk)
        form = StageForm(request.POST, instance=application)
        if form.is_valid():
            form.save()
            messages.success(request, "단계와 결과를 저장했습니다.")
        else:
            messages.error(request, "단계 변경에 실패했습니다.")
        return redirect("applications:detail", pk=pk)


class NoResponseFailView(LoginRequiredMixin, View):
    """미응답 처리 모달에서 체크한 건만 불합격으로 바꾼다."""

    def post(self, request):
        ids = [value for value in request.POST.getlist("ids") if value.isdigit()]
        # 모달에 나올 수 있는 대상(가장 짧은 기준 일수) 안에서만 처리한다.
        applications = list(Application.objects.no_response().filter(pk__in=ids))
        for application in applications:
            application.result = Result.FAILED
            application.save(update_fields=["result", "updated_at"])
        if applications:
            messages.success(
                request, f"미응답 {len(applications)}건을 불합격으로 전환했습니다."
            )
        else:
            messages.info(request, "불합격으로 전환한 건이 없습니다.")
        return redirect("home")


class AnalysisCreateView(LoginRequiredMixin, FormView):
    form_class = AnalysisForm
    template_name = "applications/analysis_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.application = get_object_or_404(Application, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["application"] = self.application
        return context

    def form_valid(self, form):
        # 저장은 form.save() 가 아니라 services 를 거친다. (2차 대비 규칙)
        save_manual_analysis(self.application, form.cleaned_data)
        messages.success(self.request, "분석 결과를 저장했습니다.")
        return redirect("applications:detail", pk=self.application.pk)


SEARCH_RESULT_LIMIT = 10


class CompanyListView(LoginRequiredMixin, PageLinksMixin, ListView):
    template_name = "applications/company_list.html"
    context_object_name = "companies"

    def get_queryset(self):
        return (
            Company.objects.active()
            .annotate(
                application_count=Count("applications"),
                # 실제 지원(관심 단계를 넘어간) 횟수
                applied_count=Count(
                    "applications", filter=~Q(applications__stage=Stage.INTEREST)
                ),
            )
            .order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["deleted_count"] = Company.objects.deleted().count()
        # 지원 이력 검색: 목록은 그대로 두고 결과만 따로 보여 준다.
        query = self.request.GET.get("q", "").strip()
        context["query"] = query
        if query:
            context["search_results"] = self.get_queryset().filter(
                name__icontains=query
            )[:SEARCH_RESULT_LIMIT]
        return context


class DeletedCompanyListView(LoginRequiredMixin, ListView):
    template_name = "applications/company_deleted_list.html"
    context_object_name = "companies"

    def get_queryset(self):
        return (
            Company.objects.deleted()
            .annotate(application_count=Count("applications"))
            .order_by("-deleted_at")
        )


class CompanyUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    form_class = CompanyForm
    template_name = "applications/company_form.html"
    success_url = reverse_lazy("applications:company_list")
    success_message = "기업 정보를 수정했습니다."

    def get_queryset(self):
        return Company.objects.active()


class CompanyDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        company = get_object_or_404(Company.objects.active(), pk=pk)
        try:
            company.soft_delete()
        except ValidationError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, f"{company} 을(를) 삭제했습니다.")
        return redirect("applications:company_list")


class CompanyRestoreView(LoginRequiredMixin, View):
    def post(self, request, pk):
        company = get_object_or_404(Company.objects.deleted(), pk=pk)
        company.restore()
        messages.success(request, f"{company} 을(를) 복원했습니다.")
        return redirect("applications:company_deleted_list")
