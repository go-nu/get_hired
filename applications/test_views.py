from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from profiles.models import GuidelineVersion

from .models import Application, Company, Result, Stage
from .views import this_week_range


class LoggedInTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tester", password="pw")

    def setUp(self):
        self.client.force_login(self.user)

    def make_application(self, company_name="테스트기업", **kwargs):
        company, _ = Company.objects.get_or_create(name=company_name)
        kwargs.setdefault("position", "백엔드")
        return Application.objects.create(company=company, **kwargs)


class LoginRequiredTests(LoggedInTestCase):
    def test_all_pages_require_login(self):
        application = self.make_application()
        self.client.logout()
        urls = [
            reverse("home"),
            reverse("applications:create"),
            reverse("applications:detail", args=[application.pk]),
            reverse("applications:update", args=[application.pk]),
            reverse("applications:analysis_create", args=[application.pk]),
            reverse("applications:company_list"),
            reverse("applications:company_deleted_list"),
            reverse("applications:company_update", args=[application.company.pk]),
        ]
        for url in urls:
            self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")


class WeekRangeTests(TestCase):
    def test_week_runs_monday_to_sunday(self):
        # 2026-10-07 은 수요일
        self.assertEqual(
            this_week_range(date(2026, 10, 7)), (date(2026, 10, 5), date(2026, 10, 11))
        )
        self.assertEqual(
            this_week_range(date(2026, 10, 11)), (date(2026, 10, 5), date(2026, 10, 11))
        )
        self.assertEqual(
            this_week_range(date(2026, 10, 12)), (date(2026, 10, 12), date(2026, 10, 18))
        )


class DashboardTests(LoggedInTestCase):
    def test_empty_dashboard_renders(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "아직 등록한 지원 건이 없습니다.")

    def test_stats(self):
        today = timezone.localdate()
        monday, sunday = this_week_range(today)
        self.make_application("관심A", deadline=sunday)  # 이번 주 마감, 미지원
        self.make_application("관심B", deadline=sunday + timedelta(days=1))  # 다음 주
        self.make_application("지원A", stage=Stage.APPLIED, deadline=monday)
        self.make_application("지원B", stage=Stage.INTERVIEW, result=Result.FAILED)
        self.make_application("지원C", stage=Stage.APPLIED, result=Result.READ)
        hidden = self.make_application("삭제기업", deadline=sunday)
        hidden.company.soft_delete()

        stats = self.client.get(reverse("home")).context["stats"]
        self.assertEqual(stats["total_applied"], 3)
        self.assertEqual(stats["in_progress"], 2)
        self.assertEqual(stats["due_this_week"], 1)
        self.assertEqual(
            dict(stats["stages"]),
            {
                "관심": 2,
                "지원 완료": 2,
                "서류 합격": 0,
                "코딩테스트/과제": 0,
                "면접": 1,
                "최종": 0,
            },
        )

    def test_deleted_company_applications_are_hidden(self):
        application = self.make_application("삭제기업")
        application.company.soft_delete()
        response = self.client.get(reverse("home"))
        self.assertEqual(list(response.context["applications"]), [])

    def test_filters(self):
        applied = self.make_application("A", stage=Stage.APPLIED, source="wanted")
        self.make_application("B", source="saramin")
        response = self.client.get(reverse("home"), {"stage": Stage.APPLIED})
        self.assertEqual(list(response.context["applications"]), [applied])
        response = self.client.get(reverse("home"), {"source": "wanted"})
        self.assertEqual(list(response.context["applications"]), [applied])
        response = self.client.get(reverse("home"), {"result": Result.FAILED})
        self.assertEqual(list(response.context["applications"]), [])
        self.assertContains(response, "조건에 맞는 지원 건이 없습니다.")

    def make_sortable(self):
        today = timezone.localdate()
        first = self.make_application(
            "첫째", stage=Stage.APPLIED, applied_at=today - timedelta(days=5), result=Result.FAILED
        )
        second = self.make_application("둘째")  # 미지원, 진행 중
        third = self.make_application(
            "셋째", stage=Stage.APPLIED, applied_at=today, result=Result.PASSED
        )
        return first, second, third

    def order(self, **params):
        return list(self.client.get(reverse("home"), params).context["applications"])

    def test_default_sort_is_farthest_deadline_first_and_past_last(self):
        today = timezone.localdate()
        far = self.make_application("먼 마감", deadline=today + timedelta(days=10))
        long_past = self.make_application("오래전 마감", deadline=today - timedelta(days=30))
        no_deadline = self.make_application("마감 없음")
        due_today = self.make_application("오늘 마감", deadline=today)
        just_past = self.make_application("어제 마감", deadline=today - timedelta(days=1))
        near = self.make_application("가까운 마감", deadline=today + timedelta(days=2))

        expected = [far, near, due_today, no_deadline, just_past, long_past]
        self.assertEqual(self.order(), expected)
        self.assertEqual(self.order(sort="nonsense", dir="sideways"), expected)
        self.assertEqual(self.order(sort="deadline", dir="asc"), expected[::-1])
        # 지원일 정렬은 없앴으므로 기본 정렬로 처리된다.
        self.assertEqual(self.order(sort="applied"), expected)

    def test_sort_by_index_and_result(self):
        first, second, third = self.make_sortable()
        self.assertEqual(self.order(sort="index"), [first, second, third])
        self.assertEqual(self.order(sort="index", dir="desc"), [third, second, first])
        # 결과는 미열람 → 열람 → 진행 중 → 합격 → 불합격 → 포기 순
        self.assertEqual(self.order(sort="result"), [second, third, first])
        self.assertEqual(self.order(sort="result", dir="desc"), [first, third, second])

    def test_numbers_start_at_one_and_stay_fixed(self):
        first, second, third = self.make_sortable()
        numbers = {a.company.name: a.number for a in self.order()}
        self.assertEqual(numbers, {"첫째": 1, "둘째": 2, "셋째": 3})
        # 필터로 일부만 보여도 번호는 그대로
        filtered = self.order(result=Result.PASSED)
        self.assertEqual([(a.company.name, a.number) for a in filtered], [("셋째", 3)])

    def test_sort_headers_toggle_direction_and_keep_filters(self):
        self.make_sortable()
        response = self.client.get(reverse("home"), {"stage": Stage.APPLIED})
        headers = response.context["sort_headers"]
        self.assertNotIn("applied", headers)
        self.assertEqual(headers["deadline"]["direction"], "desc")
        self.assertIn("dir=asc", headers["deadline"]["url"])
        self.assertIn("stage=applied", headers["deadline"]["url"])
        self.assertEqual(headers["index"]["direction"], "")
        self.assertIn("sort=index&dir=asc", headers["index"]["url"])

    def test_pagination_shows_15_per_page_and_keeps_query(self):
        for number in range(1, 18):
            self.make_application(f"기업{number}", source="wanted")
        first = self.client.get(reverse("home"), {"source": "wanted", "sort": "index"})
        self.assertEqual(len(first.context["applications"]), 15)
        self.assertEqual(first.context["page_obj"].paginator.count, 17)
        self.assertContains(first, "17건")
        self.assertContains(first, "?source=wanted&amp;sort=index&page=2")
        # 정렬 링크는 첫 페이지로 돌아간다.
        self.assertNotIn("page=", first.context["sort_headers"]["deadline"]["url"])

        second = self.client.get(
            reverse("home"), {"source": "wanted", "sort": "index", "page": 2}
        )
        self.assertEqual([a.number for a in second.context["applications"]], [16, 17])

    def test_no_pagination_nav_for_single_page(self):
        self.make_application()
        self.assertNotContains(self.client.get(reverse("home")), 'aria-label="페이지"')

    def test_no_deadline_is_shown_as_always_open(self):
        self.make_application()
        self.assertContains(self.client.get(reverse("home")), "상시지원")

    def test_deadline_level(self):
        today = timezone.localdate()
        levels = {
            days: self.make_application(f"D{days}", deadline=today + timedelta(days=days)).deadline_level
            for days in (-1, 0, 1, 7, 8)
        }
        self.assertEqual(levels, {-1: "", 0: "today", 1: "soon", 7: "soon", 8: ""})
        self.assertEqual(self.make_application("상시").deadline_level, "")
        self.assertEqual(self.make_application("오늘", deadline=today).d_day_label, "D-DAY")

    def test_rows_link_to_detail(self):
        application = self.make_application()
        response = self.client.get(reverse("home"))
        self.assertContains(
            response, f'data-href="{reverse("applications:detail", args=[application.pk])}"'
        )

    def test_urgent_only_when_due_within_three_days_and_not_applied(self):
        today = timezone.localdate()
        urgent = self.make_application("급함", deadline=today + timedelta(days=3))
        later = self.make_application("여유", deadline=today + timedelta(days=4))
        applied = self.make_application(
            "지원함", deadline=today + timedelta(days=1), stage=Stage.APPLIED
        )
        past = self.make_application("지남", deadline=today - timedelta(days=1))
        self.assertTrue(urgent.is_urgent)
        self.assertFalse(later.is_urgent)
        self.assertFalse(applied.is_urgent)
        self.assertFalse(past.is_urgent)
        self.assertEqual(urgent.d_day_label, "D-3")
        self.assertEqual(past.d_day_label, "D+1")


class ApplicationFormViewTests(LoggedInTestCase):
    def post_data(self, **overrides):
        data = {
            "company_name": "새기업",
            "position": "백엔드 개발자",
            "posting_url": "",
            "source": "wanted",
            "deadline": "",
            "applied_at": "",
            "stage": Stage.INTEREST,
            "result": Result.IN_PROGRESS,
            "main_tasks": "",
            "requirements": "",
            "preferred": "",
            "guideline_version": "",
            "memo": "",
        }
        data.update(overrides)
        return data

    def test_create_makes_new_company(self):
        response = self.client.post(reverse("applications:create"), self.post_data())
        application = Application.objects.get()
        self.assertRedirects(response, reverse("applications:detail", args=[application.pk]))
        self.assertEqual(application.company.name, "새기업")
        self.assertIsNone(application.fit_grade)

    def test_create_reuses_existing_company(self):
        existing = Company.objects.create(name="새기업")
        self.client.post(reverse("applications:create"), self.post_data(company_name=" 새기업 "))
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(Application.objects.get().company, existing)

    def test_create_rejects_deleted_company_name(self):
        Company.objects.create(name="새기업").soft_delete()
        response = self.client.post(reverse("applications:create"), self.post_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "삭제된 기업입니다.")
        self.assertEqual(Application.objects.count(), 0)

    def test_create_form_defaults_to_active_guideline(self):
        active = GuidelineVersion.objects.create(is_active=True)
        response = self.client.get(reverse("applications:create"))
        self.assertEqual(response.context["form"]["guideline_version"].value(), active.pk)

    def test_form_ignores_posted_fit_fields(self):
        # 적합도는 에이전트가 채운다. 폼으로 보낸 값은 저장하지 않는다.
        self.client.post(
            reverse("applications:create"),
            self.post_data(fit_score="85", fit_grade="mid_high"),
        )
        application = Application.objects.get()
        self.assertEqual((application.fit_score, application.fit_grade), (None, None))

    def test_update_shows_company_name_and_saves(self):
        application = self.make_application("기존기업", fit_score=85, fit_grade="mid_high")
        url = reverse("applications:update", args=[application.pk])
        response = self.client.get(url)
        self.assertContains(response, 'value="기존기업"')
        # 저장 버튼은 상단(폼 바깥, form 속성으로 연결)과 하단에 하나씩
        self.assertContains(response, 'form="application-form"', count=1)
        self.assertContains(response, 'type="submit"', count=3)  # 저장 2 + 로그아웃 1
        self.client.post(url, self.post_data(company_name="기존기업", position="새 직무"))
        application.refresh_from_db()
        self.assertEqual(application.position, "새 직무")
        # 수정해도 이미 매겨진 적합도는 그대로 남는다.
        self.assertEqual((application.fit_score, application.fit_grade), (85, "mid_high"))
        self.assertEqual(application.company.name, "기존기업")


class ApplicationDetailTests(LoggedInTestCase):
    def test_detail_renders_without_analysis(self):
        application = self.make_application()
        response = self.client.get(reverse("applications:detail", args=[application.pk]))
        self.assertContains(response, "아직 분석 결과가 없습니다.")
        self.assertContains(response, "등록 → 관심")

    def test_stage_change_records_history_and_sets_applied_date(self):
        application = self.make_application()
        response = self.client.post(
            reverse("applications:stage", args=[application.pk]),
            {"stage": Stage.APPLIED, "result": Result.IN_PROGRESS},
        )
        self.assertRedirects(response, reverse("applications:detail", args=[application.pk]))
        application.refresh_from_db()
        self.assertEqual(application.stage, Stage.APPLIED)
        self.assertEqual(application.applied_at, timezone.localdate())
        self.assertEqual(application.stage_history.count(), 2)

    def test_analysis_paste_adds_history_and_shows_latest(self):
        application = self.make_application()
        url = reverse("applications:analysis_create", args=[application.pk])
        empty = {"company_analysis": "", "fit_evaluation": "", "motivation_draft": "", "title_candidates": ""}
        self.assertContains(self.client.post(url, empty), "한 항목 이상 입력하세요.")
        self.client.post(url, {**empty, "company_analysis": "첫 번째 분석"})
        self.client.post(url, {**empty, "company_analysis": "두 번째 분석"})
        self.assertEqual(application.analyses.count(), 2)

        detail = reverse("applications:detail", args=[application.pk])
        response = self.client.get(detail)
        self.assertContains(response, "두 번째 분석")
        self.assertNotContains(response, "첫 번째 분석")

        oldest = application.analyses.last()
        response = self.client.get(detail, {"analysis": oldest.pk})
        self.assertContains(response, "첫 번째 분석")


class NoResponseTests(LoggedInTestCase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        old = today - timedelta(days=15)
        recent = today - timedelta(days=14)
        applied = {"stage": Stage.APPLIED, "applied_at": today - timedelta(days=40)}
        # 대상
        self.unread = self.make_application("미열람", deadline=old, **applied)
        self.read = self.make_application(
            "열람", deadline=today - timedelta(days=30), result=Result.READ, **applied
        )
        self.always_open = self.make_application("상시", **applied)
        # 대상 아님
        self.recent = self.make_application("최근마감", deadline=recent, **applied)
        self.in_progress = self.make_application(
            "진행중", deadline=old, result=Result.IN_PROGRESS, **applied
        )
        self.interest = self.make_application("관심만", deadline=old)
        self.always_open_recent = self.make_application(
            "상시최근", stage=Stage.APPLIED, applied_at=recent
        )
        self.hidden = self.make_application("삭제될기업", deadline=old)
        self.hidden.company.soft_delete()

    def test_candidates_by_threshold(self):
        self.assertEqual(
            set(Application.objects.no_response()),
            {self.unread, self.read, self.always_open},
        )
        self.assertEqual(
            set(Application.objects.no_response(30)), {self.read, self.always_open}
        )
        self.assertEqual(self.unread.days_without_response, 15)
        self.assertEqual(self.always_open.days_without_response, 40)

    def test_dashboard_has_button_and_modal_list(self):
        response = self.client.get(reverse("home"))
        # 가장 오래 응답이 없는 건부터 (40일, 30일, 15일)
        self.assertEqual(
            response.context["no_response_candidates"],
            [self.always_open, self.read, self.unread],
        )
        self.assertContains(response, 'id="no-response-open"')
        for days in (15, 30):
            self.assertContains(response, f'name="no-response-days" value="{days}"')
        self.assertContains(response, 'data-days="15"')
        self.assertContains(response, "D+30")
        self.assertContains(response, "지원 후 40일")
        self.assertNotContains(response, "응답 없음")

    def results(self):
        return dict(Application.objects.values_list("company__name", "result"))

    def test_only_checked_candidates_are_failed(self):
        url = reverse("applications:no_response_fail")
        self.assertEqual(self.client.get(url).status_code, 405)
        before = self.results()

        # 체크한 건 + 대상이 아닌 건의 id 를 섞어 보내도 대상만 바뀐다.
        ids = [self.unread.pk, self.always_open.pk, self.recent.pk, self.in_progress.pk,
               self.interest.pk, self.hidden.pk, "abc"]
        self.assertRedirects(self.client.post(url, {"ids": ids}), reverse("home"))
        self.assertEqual(
            self.results(),
            {**before, "미열람": Result.FAILED, "상시": Result.FAILED},
        )

    def test_nothing_changes_without_selection(self):
        before = self.results()
        self.client.post(reverse("applications:no_response_fail"))
        self.assertEqual(self.results(), before)


class BannerTests(LoggedInTestCase):
    def test_banner_is_hidden_only_on_application_form(self):
        application = self.make_application()
        with_banner = [
            reverse("home"),
            reverse("applications:detail", args=[application.pk]),
            reverse("applications:company_list"),
        ]
        without_banner = [
            reverse("applications:create"),
            reverse("applications:update", args=[application.pk]),
        ]
        for url in with_banner:
            self.assertContains(self.client.get(url), 'id="cheer-banner"')
        for url in without_banner:
            self.assertNotContains(self.client.get(url), 'id="cheer-banner"')


class CompanyListTests(LoggedInTestCase):
    def test_sorted_by_name_and_paginated(self):
        for number in range(17, 0, -1):
            Company.objects.create(name=f"기업{number:02d}")
        url = reverse("applications:company_list")
        first = self.client.get(url)
        names = [company.name for company in first.context["companies"]]
        self.assertEqual(names, [f"기업{number:02d}" for number in range(1, 16)])
        second = self.client.get(url, {"page": 2, "q": "기업01"})
        self.assertEqual(
            [company.name for company in second.context["companies"]], ["기업16", "기업17"]
        )
        # 검색어는 페이지 링크에 유지된다.
        self.assertEqual(second.context["page_query"], "q=%EA%B8%B0%EC%97%8501")

    def test_search_shows_application_history(self):
        self.make_application("지원한회사", stage=Stage.APPLIED)
        self.make_application("지원한회사", stage=Stage.INTERVIEW)
        self.make_application("지원한회사")  # 관심 단계는 지원 횟수에 넣지 않는다
        self.make_application("관심만회사")
        url = reverse("applications:company_list")

        self.assertNotContains(self.client.get(url), 'id="search-result"')

        response = self.client.get(url, {"q": "회사"})
        results = {c.name: c.applied_count for c in response.context["search_results"]}
        self.assertEqual(results, {"관심만회사": 0, "지원한회사": 2})
        self.assertContains(response, "O (2회)")
        # 검색해도 아래 목록은 그대로다.
        self.assertEqual(len(response.context["companies"]), 2)

        response = self.client.get(url, {"q": "없는회사"})
        self.assertEqual(list(response.context["search_results"]), [])
        self.assertContains(response, "등록된 기업 없음")

    def test_search_ignores_deleted_companies(self):
        Company.objects.create(name="삭제회사").soft_delete()
        response = self.client.get(reverse("applications:company_list"), {"q": "삭제"})
        self.assertEqual(list(response.context["search_results"]), [])


class CompanyViewTests(LoggedInTestCase):
    def test_delete_and_restore_flow(self):
        application = self.make_application("관심기업")
        company = application.company

        response = self.client.post(reverse("applications:company_delete", args=[company.pk]))
        self.assertRedirects(response, reverse("applications:company_list"))
        company.refresh_from_db()
        self.assertTrue(company.is_deleted)
        self.assertNotContains(self.client.get(reverse("applications:company_list")), "관심기업")
        self.assertContains(self.client.get(reverse("applications:company_deleted_list")), "관심기업")

        self.client.post(reverse("applications:company_restore", args=[company.pk]))
        company.refresh_from_db()
        self.assertFalse(company.is_deleted)
        self.assertEqual(Application.objects.visible().count(), 1)

    def test_applied_company_is_not_deleted(self):
        company = self.make_application("지원기업", stage=Stage.APPLIED).company
        self.client.post(reverse("applications:company_delete", args=[company.pk]))
        company.refresh_from_db()
        self.assertFalse(company.is_deleted)
        response = self.client.get(reverse("applications:company_list"))
        self.assertContains(response, "이미 지원한 기업은 삭제할 수 없습니다.")

    def test_delete_requires_post(self):
        company = Company.objects.create(name="기업")
        url = reverse("applications:company_delete", args=[company.pk])
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_company_update(self):
        company = Company.objects.create(name="기업")
        self.client.post(
            reverse("applications:company_update", args=[company.pk]),
            {"name": "기업", "industry": "핀테크", "size": "startup", "website": "", "memo": ""},
        )
        company.refresh_from_db()
        self.assertEqual((company.industry, company.size), ("핀테크", "startup"))
