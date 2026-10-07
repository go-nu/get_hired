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

    def form_valid(self, form):
        form.instance.is_active = True
        return super().form_valid(form)

    def get_success_message(self, cleaned_data):
        return self.success_message % {"version": self.object.version}


class VersionActivateView(LoginRequiredMixin, View):
    def post(self, request, version):
        target = get_object_or_404(GuidelineVersion, version=version)
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
