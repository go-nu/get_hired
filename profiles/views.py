from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, TemplateView

from .diff import compare_versions
from .forms import GuidelineVersionForm
from .models import GuidelineVersion
from .services import check_privacy, sections_of


class ActiveVersionView(LoginRequiredMixin, TemplateView):
    template_name = "profiles/version_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["version"] = GuidelineVersion.get_active()
        context["is_active_page"] = True
        return context


class VersionListView(LoginRequiredMixin, ListView):
    model = GuidelineVersion
    template_name = "profiles/version_list.html"
    context_object_name = "versions"


class VersionDetailView(LoginRequiredMixin, DetailView):
    model = GuidelineVersion
    template_name = "profiles/version_detail.html"
    context_object_name = "version"
    slug_field = "version"
    slug_url_kwarg = "version"


class VersionCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    """기존 버전 내용을 불러와 고친 뒤 새 버전으로 저장한다."""

    model = GuidelineVersion
    form_class = GuidelineVersionForm
    template_name = "profiles/version_form.html"
    success_url = reverse_lazy("profiles:active")
    success_message = "v%(version)s 을(를) 저장했습니다."

    def get_base_version(self):
        base = self.request.GET.get("base")
        if base and base.isdigit():
            return GuidelineVersion.objects.filter(version=base).first()
        return GuidelineVersion.get_active()

    def get_initial(self):
        initial = super().get_initial()
        base = self.get_base_version()
        if base:
            for name in GuidelineVersion.SECTION_FIELDS:
                initial[name] = getattr(base, name)
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["base_version"] = self.get_base_version()
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user  # 개인정보 점검의 AI 호출 기록에 남는다
        return kwargs

    def form_valid(self, form):
        form.instance.is_active = True
        response = super().form_valid(form)
        if form.privacy_warning:
            messages.warning(
                self.request,
                f"개인정보 점검을 하지 못한 채 저장했습니다. ({form.privacy_warning})",
            )
        return response

    def get_success_message(self, cleaned_data):
        return self.success_message % {"version": self.object.version}


class VersionActivateView(LoginRequiredMixin, View):
    def post(self, request, version):
        target = get_object_or_404(GuidelineVersion, version=version)
        # 점검을 거치지 않은 버전(이 기능 이전에 만든 것 등)은 활성으로 지정할 때 점검한다.
        if not target.privacy_checked:
            result = check_privacy(request.user, sections_of(target))
            if result.resident_fields or result.findings:
                # 어느 부분인지는 새로 작성 화면에서 저장할 때 칸마다 보여 준다.
                messages.error(
                    request,
                    f"{target} 에 개인정보로 보이는 내용이 있어 활성으로 지정하지 않았습니다. "
                    "[이 내용으로 새로 작성]에서 고쳐 저장하세요.",
                )
                return redirect("profiles:detail", version=target.version)
            if result.error:
                messages.warning(
                    request, f"개인정보 점검을 하지 못한 채 지정했습니다. ({result.error})"
                )
            else:
                target.privacy_checked = True
                target.save(update_fields=["privacy_checked"])
        target.activate()
        messages.success(request, f"{target} 을(를) 활성 버전으로 지정했습니다.")
        return redirect("profiles:list")


class VersionCompareView(LoginRequiredMixin, TemplateView):
    template_name = "profiles/version_compare.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        versions = list(GuidelineVersion.objects.all())  # 최신순
        context["versions"] = versions
        if len(versions) < 2:
            return context

        by_number = {str(v.version): v for v in versions}
        # 기본값: 최신 버전과 그 바로 이전 버전
        new = by_number.get(self.request.GET.get("new"), versions[0])
        index = versions.index(new)
        previous = versions[index + 1] if index + 1 < len(versions) else versions[index - 1]
        old = by_number.get(self.request.GET.get("old"), previous)
        context.update(old=old, new=new, sections=compare_versions(old, new))
        return context
