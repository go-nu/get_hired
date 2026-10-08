from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse

from .models import Analysis, Application, Company, Result, Stage
from .services import save_manual_analysis


class CompanyDeleteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="테스트기업")

    def test_company_without_applications_can_be_deleted(self):
        self.company.soft_delete()
        self.assertTrue(self.company.is_deleted)
        self.assertEqual(list(Company.objects.active()), [])
        self.assertEqual(list(Company.objects.deleted()), [self.company])

    def test_company_with_only_interest_applications_can_be_deleted(self):
        Application.objects.create(company=self.company, position="백엔드")
        self.company.soft_delete()
        self.assertTrue(self.company.is_deleted)
        self.assertEqual(Application.objects.count(), 1)
        self.assertEqual(Application.objects.visible().count(), 0)

    def test_applied_company_cannot_be_deleted(self):
        Application.objects.create(
            company=self.company, position="백엔드", stage=Stage.APPLIED
        )
        self.assertFalse(self.company.can_delete)
        with self.assertRaises(ValidationError):
            self.company.soft_delete()
        self.company.refresh_from_db()
        self.assertFalse(self.company.is_deleted)

    def test_restore(self):
        Application.objects.create(company=self.company, position="백엔드")
        self.company.soft_delete()
        self.company.restore()
        self.assertFalse(self.company.is_deleted)
        self.assertEqual(Application.objects.visible().count(), 1)

    def test_hard_delete_is_blocked_when_applications_exist(self):
        Application.objects.create(company=self.company, position="백엔드")
        with self.assertRaises(ProtectedError):
            self.company.delete()


class ApplicationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="테스트기업")
        self.application = Application.objects.create(
            company=self.company, position="백엔드"
        )

    def test_defaults(self):
        self.assertEqual(self.application.stage, Stage.INTEREST)
        self.assertEqual(self.application.result, Result.UNREAD)

    def test_result_order(self):
        self.assertEqual(
            Result.labels, ["미열람", "열람", "진행 중", "합격", "불합격", "포기"]
        )

    def test_creation_records_initial_stage(self):
        history = self.application.stage_history.get()
        self.assertEqual((history.from_stage, history.to_stage), ("", Stage.INTEREST))

    def test_stage_change_is_recorded(self):
        self.application.stage = Stage.APPLIED
        self.application.save()
        latest = self.application.stage_history.first()
        self.assertEqual(
            (latest.from_stage, latest.to_stage), (Stage.INTEREST, Stage.APPLIED)
        )
        self.assertEqual(self.application.stage_history.count(), 2)

    def test_saving_without_stage_change_adds_no_history(self):
        self.application.memo = "메모"
        self.application.save()
        self.assertEqual(self.application.stage_history.count(), 1)

    def test_stage_can_skip_steps(self):
        self.application.stage = Stage.INTERVIEW
        self.application.save()
        self.assertEqual(self.application.stage_history.first().to_stage, Stage.INTERVIEW)

    def test_fit_score_range_is_validated(self):
        self.application.fit_score = 101
        with self.assertRaises(ValidationError):
            self.application.full_clean()

    def test_fit_grade_has_five_levels(self):
        self.assertEqual(
            Application.FitGrade.labels, ["상", "중상", "중", "중하", "하"]
        )


class DashboardSortTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("tester"))
        for name, stage in (
            ("나기업", Stage.INTERVIEW),
            ("가기업", Stage.APPLIED),
            ("다기업", Stage.INTEREST),
        ):
            Application.objects.create(
                company=Company.objects.create(name=name), position="백엔드", stage=stage
            )

    def company_names(self, query):
        response = self.client.get(reverse("home"), query)
        return [application.company.name for application in response.context["applications"]]

    def test_sort_by_company_name(self):
        self.assertEqual(self.company_names({"sort": "company"}), ["가기업", "나기업", "다기업"])
        self.assertEqual(
            self.company_names({"sort": "company", "dir": "desc"}),
            ["다기업", "나기업", "가기업"],
        )

    def test_sort_by_stage_follows_stage_order(self):
        self.assertEqual(self.company_names({"sort": "stage"}), ["다기업", "가기업", "나기업"])
        self.assertEqual(
            self.company_names({"sort": "stage", "dir": "desc"}),
            ["나기업", "가기업", "다기업"],
        )


class AnalysisServiceTests(TestCase):
    def setUp(self):
        company = Company.objects.create(name="테스트기업")
        self.application = Application.objects.create(company=company, position="백엔드")

    def test_latest_analysis_is_none_without_analysis(self):
        self.assertIsNone(self.application.latest_analysis)

    def test_save_manual_analysis_keeps_history_and_shows_latest(self):
        first = save_manual_analysis(self.application, {"company_analysis": "첫 분석"})
        second = save_manual_analysis(
            self.application, {"company_analysis": "두 번째", "unknown": "무시"}
        )
        self.assertEqual(first.source, Analysis.Source.MANUAL)
        self.assertEqual(first.fit_evaluation, "")
        self.assertEqual(self.application.analyses.count(), 2)
        self.assertEqual(self.application.latest_analysis, second)
