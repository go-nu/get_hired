from contextlib import contextmanager
from unittest import mock

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from langgraph.checkpoint.memory import InMemorySaver

from agents.models import AgentJob, AgentSettings, Provider, Role, Status
from agents.providers import ProviderError
from profiles.models import GuidelineVersion

from . import prompts
from agents.skills import list_skills, load_skill

from .analysis_graph import (
    SKILL_BY_ROLE,
    skill_values,
    ACCEPT,
    REVISE,
    Draft,
    Evaluation,
    Judgement,
    Review,
    graph_builder,
    split_company_info,
)
from .models import Analysis, Application
from .scoring import grade_for, score_evaluation
from .services import (
    generate_analysis,
    resume_analysis,
    run_analysis_job,
    start_analysis,
    urgent_applications,
)
from .test_views import LoggedInTestCase

RESEARCH_ANSWER = "## 사업\n- 결제 서비스\n\n[기업 정보]\n업종: 핀테크\n규모: 스타트업\n"
APPROVE = Review(approved=True, issues=[])
# 자격 요건 (1 + 0.5) / 2 × 60 = 45, 우대 없음(배점 제외), 업무 1 × 15 = 15, 기타 20 → 5로 자름
# (45 + 15 + 5) / 80 × 100 = 81.25 → 81점, 중상
EVALUATION = Evaluation(
    requirements=[
        Judgement(item="Python 3년", verdict="충족", reason="Python 4년"),
        Judgement(item="AWS 운영", verdict="부분 충족", reason="개인 프로젝트 배포"),
    ],
    preferred=[],
    tasks=[Judgement(item="API 개발", verdict="충족", reason="REST API 개발")],
    extra_score=20,
    extra_reason="핀테크 도메인 경험",
    summary="요건 대부분 충족",
)
REJECT = Review(approved=False, issues=["규칙 위반"])


class FakeAgents:
    """agents.llm.ask 대역. 역할별로 정해 둔 답을 돌려주고 호출 순서를 기록한다."""

    def __init__(self, reviews=(APPROVE,), fail_on=None):
        self.reviews = list(reviews)  # 순서대로 쓰고, 마지막 것은 계속 쓴다
        self.fail_on = fail_on
        self.calls = []  # (역할, 프롬프트 본문)
        self.systems = {}  # 역할 → 시스템 지시(skill 본문)
        self.web_search = {}  # 역할 → 웹 검색 도구를 줬는지

    def __call__(self, user, role, *, system, text, **kwargs):
        self.calls.append((role, text))
        self.systems[role] = system
        self.web_search[role] = kwargs.get("web_search", False)
        if role == self.fail_on:
            raise ProviderError("한도 초과")
        if role == Role.RESEARCH:
            return RESEARCH_ANSWER
        if role == Role.EVALUATE:
            return EVALUATION
        if role == Role.WRITE:
            number = self.roles.count(Role.WRITE)
            return Draft(motivation_draft=f"초안 {number}", title_candidates=["제목 A", " 제목 B "])
        return self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]

    @property
    def roles(self):
        return [role for role, _ in self.calls]


class AnalysisGraphTests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        self.guideline = GuidelineVersion.objects.create(
            profile="프로필 본문", writing_rules="규칙 본문", is_active=True
        )
        self.application = self.make_application(
            "핀테크사", requirements="Python 3년", guideline_version=self.guideline
        )
        self.job = AgentJob.objects.create(user=self.user, application=self.application)
        # 테스트에서는 멈춘 상태를 DB 대신 메모리에 저장한다.
        self.saver = InMemorySaver()

        @contextmanager
        def open_test_graph():
            yield graph_builder.compile(checkpointer=self.saver)

        patcher = mock.patch("applications.services.open_graph", open_test_graph)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_graph(self, fake, guideline="default", resume=None):
        if guideline == "default":
            guideline = self.guideline
        with mock.patch("agents.llm.ask", fake):
            return generate_analysis(
                self.application, guideline, user=self.user, job=self.job, resume=resume
            )

    def test_full_flow_saves_analysis_fit_and_company_info(self):
        fake = FakeAgents()
        analysis, error, waiting = self.run_graph(fake)
        self.assertEqual((error, waiting), ("", False))
        self.assertEqual(fake.roles, [Role.RESEARCH, Role.EVALUATE, Role.WRITE, Role.REVIEW])
        self.assertEqual(analysis.source, Analysis.Source.API)
        self.assertEqual(analysis.company_analysis, "## 사업\n- 결제 서비스")
        self.assertEqual(analysis.motivation_draft, "초안 1")
        self.assertEqual(analysis.title_candidates, "제목 A\n제목 B")
        self.assertEqual(analysis.review_notes, "")
        self.application.refresh_from_db()
        # 점수와 등급은 AI가 아니라 scoring.py 가 판정으로 계산한다.
        self.assertEqual((self.application.fit_score, self.application.fit_grade), (81, "mid_high"))
        for expected in (
            "총점 81점 · 등급 중상 (배점 80점 만점을 100점으로 환산)",
            "[자격 요건 (기본)] 45.0 / 60",
            "- 부분 충족 · AWS 운영: 개인 프로젝트 배포",
            "[우대 사항 (가점)] 공고에 없음 (배점 제외)",
            "[기타 (가점)] 5 / 5: 핀테크 도메인 경험",
            "[종합]\n요건 대부분 충족",
        ):
            self.assertIn(expected, analysis.fit_evaluation)
        company = self.application.company
        company.refresh_from_db()
        self.assertEqual((company.industry, company.size), ("핀테크", "startup"))
        self.job.refresh_from_db()
        self.assertEqual(self.job.step, Role.REVIEW)

    def test_review_issues_send_draft_back_to_writer(self):
        fake = FakeAgents(reviews=[Review(approved=False, issues=["없는 경력을 씀"]), APPROVE])
        analysis, _, waiting = self.run_graph(fake)
        self.assertEqual(
            fake.roles,
            [Role.RESEARCH, Role.EVALUATE, Role.WRITE, Role.REVIEW, Role.WRITE, Role.REVIEW],
        )
        rewrite_prompt = fake.calls[4][1]
        self.assertIn("없는 경력을 씀", rewrite_prompt)
        self.assertIn("초안 1", rewrite_prompt)
        self.assertEqual((analysis.motivation_draft, analysis.review_notes), ("초안 2", ""))
        self.assertFalse(waiting)

    def test_without_guideline_only_researches(self):
        fake = FakeAgents()
        analysis, _, _ = self.run_graph(fake, guideline=None)
        self.assertEqual(fake.roles, [Role.RESEARCH])
        self.assertEqual((analysis.company_analysis != "", analysis.fit_evaluation), (True, ""))
        self.application.refresh_from_db()
        self.assertIsNone(self.application.fit_score)

    def test_failure_keeps_results_so_far(self):
        analysis, error, waiting = self.run_graph(FakeAgents(fail_on=Role.WRITE))
        self.assertEqual((error, waiting), ("한도 초과", False))
        self.assertIn("요건 대부분 충족", analysis.fit_evaluation)
        self.assertEqual(analysis.motivation_draft, "")
        self.application.refresh_from_db()
        self.assertEqual(self.application.fit_score, 81)

    def test_failure_at_first_step_saves_nothing(self):
        analysis, error, _ = self.run_graph(FakeAgents(fail_on=Role.RESEARCH))
        self.assertEqual((analysis, error), (None, "한도 초과"))

    def test_company_info_entered_by_hand_is_kept(self):
        company = self.application.company
        company.industry = "금융"
        company.save()
        self.run_graph(FakeAgents())
        company.refresh_from_db()
        self.assertEqual((company.industry, company.size), ("금융", "startup"))

    def test_prompt_order_is_guideline_company_posting(self):
        fake = FakeAgents()
        self.run_graph(fake)
        research_prompt, evaluate_prompt = fake.calls[0][1], fake.calls[1][1]
        self.assertNotIn("프로필 본문", research_prompt)  # 기업 조사에는 지침을 넣지 않는다
        positions = [
            evaluate_prompt.index(marker)
            for marker in ("프로필 본문", "규칙 본문", "기업명: 핀테크사", "결제 서비스", "Python 3년")
        ]
        self.assertEqual(positions, sorted(positions))

    def test_nodes_take_instructions_and_tools_from_skill_files(self):
        fake = FakeAgents()
        self.run_graph(fake)
        self.assertEqual(fake.systems[Role.RESEARCH], load_skill("company-research").render(**skill_values()))
        self.assertIn("웹 검색은 최대 3번만", fake.systems[Role.RESEARCH])
        self.assertIn("규모: (스타트업, 중소, 중견, 대기업, 모름 중 하나)", fake.systems[Role.RESEARCH])
        self.assertIn("0부터 5까지의 정수", fake.systems[Role.EVALUATE])
        # 웹 검색 도구는 skill 파일에 tools 로 적은 역할에만 준다.
        self.assertEqual(
            fake.web_search,
            {Role.RESEARCH: True, Role.EVALUATE: False, Role.WRITE: False, Role.REVIEW: False},
        )

    def test_every_skill_file_loads_and_renders(self):
        skills = list_skills()
        self.assertEqual(
            sorted(skill.name for skill in skills), sorted(SKILL_BY_ROLE.values())
        )
        for skill in skills:
            self.assertTrue(skill.description)
            self.assertNotIn("{", skill.render(**skill_values()))

    def test_grade_cutoffs(self):
        grades = {score: grade_for(score) for score in (100, 90, 89, 80, 79, 70, 69, 60, 59, 0)}
        self.assertEqual(
            grades,
            {100: "high", 90: "high", 89: "mid_high", 80: "mid_high", 79: "mid",
             70: "mid", 69: "mid_low", 60: "mid_low", 59: "low", 0: "low"},
        )

    def test_score_uses_all_sections_when_present(self):
        judge = lambda verdict: Judgement(item="항목", verdict=verdict, reason="근거")  # noqa: E731
        evaluation = Evaluation(
            requirements=[judge("충족"), judge("미충족")],  # 30 / 60
            preferred=[judge("부분 충족")],  # 10 / 20
            tasks=[judge("충족"), judge("충족")],  # 15 / 15
            extra_score=-3,  # 0으로 자름
            extra_reason="근거 없음",
            summary="종합",
        )
        score, grade, text = score_evaluation(evaluation)
        self.assertEqual((score, grade), (55, "low"))
        self.assertTrue(text.startswith("총점 55점 · 등급 하\n"))

    def test_meeting_all_requirements_alone_is_the_base_grade(self):
        judge = lambda verdict: Judgement(item="항목", verdict=verdict, reason="근거")  # noqa: E731
        evaluation = Evaluation(
            requirements=[judge("충족")],
            preferred=[judge("미충족")],
            tasks=[judge("미충족")],
            extra_score=0,
            extra_reason="근거 없음",
            summary="종합",
        )
        score, grade, _ = score_evaluation(evaluation)
        self.assertEqual((score, grade), (60, "mid_low"))  # 가점 없이 기본 점수만

    def test_split_company_info(self):
        self.assertEqual(split_company_info(RESEARCH_ANSWER), ("## 사업\n- 결제 서비스", "핀테크", "startup"))
        unknown = f"본문\n{prompts.COMPANY_INFO_HEADER}\n업종: 모름\n규모: 모름"
        self.assertEqual(split_company_info(unknown), ("본문", "", ""))
        self.assertEqual(split_company_info("형식 없는 답"), ("형식 없는 답", "", ""))
        wordy = f"본문\n{prompts.COMPANY_INFO_HEADER}\n업종: 금융\n규모: 대기업 (직원 1,000명 이상)"
        self.assertEqual(split_company_info(wordy), ("본문", "금융", "large"))

    # --- 사람 확인 (휴먼 인 더 루프) ---

    def pause_at_human_review(self):
        fake = FakeAgents(reviews=[REJECT])
        analysis, error, waiting = self.run_graph(fake)
        self.assertEqual((error, waiting), ("", True))
        return fake, analysis

    def test_graph_pauses_for_human_when_rewrites_run_out(self):
        fake, analysis = self.pause_at_human_review()
        self.assertEqual(fake.roles.count(Role.WRITE), 3)  # 첫 작성 + 재작성 2회
        self.assertEqual(fake.roles.count(Role.REVIEW), 3)
        # 멈춘 시점의 초안과 남은 지적을 사람이 볼 수 있게 저장해 둔다.
        self.assertEqual((analysis.motivation_draft, analysis.review_notes), ("초안 3", "- 규칙 위반"))

    def test_human_accept_finishes_without_new_calls_or_analysis(self):
        self.pause_at_human_review()
        fake = FakeAgents()
        analysis, error, waiting = self.run_graph(fake, resume={"action": ACCEPT})
        self.assertEqual((analysis, error, waiting), (None, "", False))
        self.assertEqual(fake.calls, [])
        self.assertEqual(Analysis.objects.count(), 1)

    def test_human_instruction_reaches_writer_and_reviewer(self):
        self.pause_at_human_review()
        fake = FakeAgents()  # 이번에는 검수 통과
        analysis, error, waiting = self.run_graph(
            fake, resume={"action": REVISE, "instruction": "인턴 경험은 빼기"}
        )
        self.assertEqual(fake.roles, [Role.WRITE, Role.REVIEW])
        for _, prompt in fake.calls:
            self.assertIn("인턴 경험은 빼기", prompt)
        self.assertIn("규칙 위반", fake.calls[0][1])  # 남은 검수 지적도 함께 전달
        self.assertEqual((error, waiting, analysis.review_notes), ("", False, ""))
        self.assertEqual(Analysis.objects.count(), 2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.revisions, 3)

    def test_graph_pauses_again_if_review_still_fails_after_instruction(self):
        self.pause_at_human_review()
        fake = FakeAgents(reviews=[REJECT])
        _, _, waiting = self.run_graph(fake, resume={"action": REVISE, "instruction": "더 짧게"})
        self.assertEqual(fake.roles, [Role.WRITE, Role.REVIEW])
        self.assertTrue(waiting)
        # 다시 멈춘 뒤 이대로 저장해도 지시는 남아 있고, 추가 호출은 없다.
        analysis, _, waiting = self.run_graph(FakeAgents(), resume={"action": ACCEPT})
        self.assertEqual((analysis, waiting), (None, False))


class AnalysisJobTests(LoggedInTestCase):
    def give_api_key(self):
        settings = AgentSettings.for_user(self.user)
        settings.set_api_key(Provider.CLAUDE, "sk-test")
        settings.save()

    def test_start_needs_api_key(self):
        job, reason = start_analysis(self.make_application(), self.user)
        self.assertIsNone(job)
        self.assertIn("API 키가 없어", reason)

    @mock.patch("applications.services.threading.Thread")
    def test_start_runs_thread_after_commit_and_blocks_duplicates(self, thread):
        self.give_api_key()
        application = self.make_application()
        with self.captureOnCommitCallbacks(execute=True):
            job, reason = start_analysis(application, self.user)
        self.assertEqual((job.status, reason), (Status.RUNNING, ""))
        self.assertEqual(thread.call_args.kwargs["args"], (job.pk, None))
        thread.return_value.start.assert_called_once()
        duplicate, reason = start_analysis(application, self.user)
        self.assertIsNone(duplicate)
        self.assertIn("이미", reason)
        AgentJob.objects.update(status=Status.WAITING)
        duplicate, reason = start_analysis(application, self.user)
        self.assertIn("확인을 기다리고", reason)

    @mock.patch("applications.services.connection")
    @mock.patch("applications.services.generate_analysis")
    def test_job_records_success_failure_and_waiting(self, generate, connection):
        application = self.make_application()
        for result, status in (
            ((None, "", False), Status.SUCCEEDED),
            ((None, "한도 초과", False), Status.FAILED),
            ((None, "", True), Status.WAITING),
        ):
            generate.return_value = result
            job = AgentJob.objects.create(user=self.user, application=application)
            run_analysis_job(job.pk)
            job.refresh_from_db()
            self.assertEqual((job.status, job.error), (status, result[1]))
            self.assertEqual(job.finished_at is None, status == Status.WAITING)
        generate.side_effect = ValueError("boom")
        job = AgentJob.objects.create(user=self.user, application=application)
        run_analysis_job(job.pk, {"action": ACCEPT})
        self.assertEqual(generate.call_args.kwargs["resume"], {"action": ACCEPT})
        job.refresh_from_db()
        self.assertEqual((job.status, job.error), (Status.FAILED, "ValueError: boom"))
        self.assertEqual(connection.close.call_count, 4)

    @mock.patch("applications.services.connection")
    @mock.patch("applications.services.notify_user")
    @mock.patch("applications.services.generate_analysis")
    def test_job_result_is_sent_to_discord(self, generate, notify, connection):
        application = self.make_application("알림기업", fit_score=81, fit_grade="mid_high")
        for result, expected in (
            ((None, "", False), "[분석 완료] 적합도 81점 (중상)"),
            ((None, "한도 초과", False), "[분석 실패] 한도 초과"),
            ((None, "", True), "[확인 대기]"),
        ):
            generate.return_value = result
            job = AgentJob.objects.create(user=self.user, application=application)
            run_analysis_job(job.pk)
            user, content = notify.call_args.args
            self.assertEqual(user, self.user)
            self.assertIn(expected, content)
            self.assertIn("알림기업", content)
            self.assertIn(f"http://127.0.0.1:8000/applications/{application.pk}/", content)
        # 알림을 보내다 오류가 나도 작업 결과는 그대로 남는다.
        notify.side_effect = RuntimeError("boom")
        generate.return_value = (None, "", False)
        job = AgentJob.objects.create(user=self.user, application=application)
        with self.assertLogs("applications.services", level="ERROR"):
            run_analysis_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, Status.SUCCEEDED)

    @mock.patch("applications.management.commands.send_deadline_alerts.notify_user")
    def test_deadline_alert_lists_urgent_unapplied_applications(self, notify):
        today = timezone.localdate()
        self.make_application("임박기업", deadline=today + timedelta(days=2))
        self.make_application("여유기업", deadline=today + timedelta(days=10))
        self.make_application("지원한기업", deadline=today + timedelta(days=1), stage="applied")
        self.assertEqual([a.company.name for a in urgent_applications()], ["임박기업"])
        notify.return_value = True
        call_command("send_deadline_alerts", stdout=StringIO())
        content = notify.call_args.args[1]
        self.assertIn("[마감 임박] 아직 지원하지 않은 1건", content)
        self.assertIn("D-2 · 임박기업", content)
        self.assertNotIn("여유기업", content)

    @mock.patch("applications.services.threading.Thread")
    def test_resume_validates_and_restarts_job(self, thread):
        application = self.make_application()
        self.assertIn("기다리는 분석이 없습니다", resume_analysis(application, ACCEPT)[1])
        job = AgentJob.objects.create(
            user=self.user, application=application, status=Status.WAITING
        )
        self.assertIn("지시 사항을 적어", resume_analysis(application, REVISE, "  ")[1])
        self.assertIn("알 수 없는", resume_analysis(application, "other")[1])
        with self.captureOnCommitCallbacks(execute=True):
            resumed, reason = resume_analysis(application, REVISE, " 더 짧게 ")
        self.assertEqual((resumed, reason), (job, ""))
        job.refresh_from_db()
        self.assertEqual(job.status, Status.RUNNING)
        self.assertEqual(
            thread.call_args.kwargs["args"],
            (job.pk, {"action": REVISE, "instruction": "더 짧게"}),
        )

    @mock.patch("applications.services.threading.Thread")
    def test_create_view_starts_analysis(self, thread):
        self.give_api_key()
        self.client.post(
            reverse("applications:create"),
            {
                "company_name": "새기업",
                "position": "백엔드",
                "source": "wanted",
                "stage": "interest",
                "result": "unread",
            },
        )
        application = Application.objects.get()
        self.assertEqual(application.agent_jobs.count(), 1)

    @mock.patch("applications.services.threading.Thread")
    def test_detail_shows_progress_then_failure(self, thread):
        self.give_api_key()
        application = self.make_application()
        url = reverse("applications:detail", args=[application.pk])
        self.assertContains(self.client.get(url), "에이전트로 분석")
        response = self.client.post(
            reverse("applications:analysis_run", args=[application.pk]), follow=True
        )
        self.assertContains(response, 'id="agent-job-running"')
        self.assertNotContains(response, "에이전트로 분석</button>")
        application.agent_jobs.get().finish("한도 초과")
        self.assertContains(self.client.get(url), "최근 에이전트 분석이 실패했습니다: 한도 초과")

    def test_dashboard_marks_and_filters_waiting_applications(self):
        waiting = self.make_application("아주 긴 이름의 기다리는 기업")
        other = self.make_application("다른기업")
        AgentJob.objects.create(user=self.user, application=waiting, status=Status.WAITING)
        AgentJob.objects.create(user=self.user, application=other, status=Status.SUCCEEDED)
        response = self.client.get(reverse("home"))
        self.assertEqual(response.context["stats"]["waiting"], 1)
        flags = {a.pk: a.is_waiting for a in response.context["applications"]}
        self.assertEqual(flags, {waiting.pk: True, other.pk: False})
        # 행은 칠하지 않고 배지 색으로만 구분한다: 마감 여유가 있으면 노랑, 7일 이내면 빨강.
        self.assertContains(response, 'class="cursor-pointer hover:bg-gray-50"', count=2)
        self.assertContains(response, 'bg-yellow-100 font-medium text-yellow-900">확인 대기</span>', count=1)
        waiting.deadline = timezone.localdate() + timedelta(days=7)
        waiting.save()
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'bg-red-200 font-bold text-black">확인 대기</span>', count=1)
        response = self.client.get(reverse("home"), {"waiting": "1"})
        self.assertEqual(list(response.context["applications"]), [waiting])
        self.assertContains(response, "필터 해제")

    @mock.patch("applications.services.threading.Thread")
    def test_detail_asks_human_and_resumes(self, thread):
        application = self.make_application()
        job = AgentJob.objects.create(
            user=self.user, application=application, status=Status.WAITING, revisions=2
        )
        Analysis.objects.create(
            application=application, motivation_draft="초안", review_notes="- 규칙 위반"
        )
        url = reverse("applications:detail", args=[application.pk])
        response = self.client.get(url)
        self.assertContains(response, "에이전트가 확인을 기다립니다")
        self.assertContains(response, "- 규칙 위반")
        self.assertNotContains(response, "에이전트로 분석</button>")
        resume_url = reverse("applications:analysis_resume", args=[application.pk])
        # 지시 없이 다시 쓰기를 누르면 그대로 기다린다.
        self.client.post(resume_url, {"action": REVISE, "instruction": ""})
        job.refresh_from_db()
        self.assertEqual(job.status, Status.WAITING)
        response = self.client.post(resume_url, {"action": ACCEPT}, follow=True)
        job.refresh_from_db()
        self.assertEqual(job.status, Status.RUNNING)
        self.assertContains(response, 'id="agent-job-running"')
