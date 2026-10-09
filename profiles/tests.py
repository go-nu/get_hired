import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from agents.models import Role
from agents.providers import ProviderError

from .diff import compare_versions, diff_lines
from .models import GuidelineVersion
from .services import find_resident_numbers

FAMILY_FINDING = {
    "section": "고정 정보",
    "category": "가족 사항",
    "quote": "아버지는 은행 지점장",
    "reason": "부모의 직업을 적었다.",
}


class GuidelineVersionTests(TestCase):
    def test_version_auto_increments(self):
        first = GuidelineVersion.objects.create(profile="a")
        second = GuidelineVersion.objects.create(profile="b")
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)

    def test_only_one_active_version(self):
        first = GuidelineVersion.objects.create(is_active=True)
        second = GuidelineVersion.objects.create(is_active=True)
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(GuidelineVersion.objects.filter(is_active=True).count(), 1)

    def test_activate_switches_active_version(self):
        first = GuidelineVersion.objects.create(is_active=True)
        second = GuidelineVersion.objects.create()
        first.refresh_from_db()
        self.assertTrue(first.is_active)

        second.activate()
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(GuidelineVersion.get_active(), second)

    def test_get_active_returns_none_when_empty(self):
        self.assertIsNone(GuidelineVersion.get_active())


class DiffTests(TestCase):
    def test_diff_lines_marks_added_and_removed(self):
        lines = diff_lines("a\nb", "a\nc")
        self.assertEqual(
            lines,
            [
                {"tag": "same", "text": "a"},
                {"tag": "removed", "text": "b"},
                {"tag": "added", "text": "c"},
            ],
        )

    def test_compare_versions_flags_changed_sections(self):
        old = GuidelineVersion.objects.create(profile="p", writing_rules="r1")
        new = GuidelineVersion.objects.create(profile="p", writing_rules="r2")
        sections = compare_versions(old, new)
        self.assertEqual(len(sections), len(GuidelineVersion.SECTION_FIELDS))
        self.assertEqual([s["changed"] for s in sections], [False, True, False, False])


class ProfileViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("tester", password="pw")

    def setUp(self):
        self.client.force_login(self.user)
        # 개인정보 점검의 AI 호출. 기본은 "찾은 것 없음".
        patcher = mock.patch("profiles.services.run_agent")
        self.run_agent = patcher.start()
        self.addCleanup(patcher.stop)
        self.set_findings()

    def set_findings(self, *findings):
        self.run_agent.side_effect = None
        self.run_agent.return_value = mock.Mock(
            output=json.dumps({"findings": list(findings)}, ensure_ascii=False)
        )

    def post_profile(self, profile, **extra):
        data = {"note": "", "profile": profile, "writing_rules": "", "output_format": "", "extra": ""}
        return self.client.post(reverse("profiles:create"), {**data, **extra})

    def test_login_required(self):
        self.client.logout()
        for name in ("profiles:active", "profiles:list", "profiles:create", "profiles:compare"):
            response = self.client.get(reverse(name))
            self.assertRedirects(
                response, f"{reverse('login')}?next={reverse(name)}"
            )

    def test_pages_render_when_empty(self):
        for name in ("profiles:active", "profiles:list", "profiles:create", "profiles:compare"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_create_makes_new_active_version_and_keeps_old(self):
        old = GuidelineVersion.objects.create(profile="old", is_active=True)
        response = self.client.post(
            reverse("profiles:create"),
            {"note": "수정", "profile": "new", "writing_rules": "", "output_format": "", "extra": ""},
        )
        self.assertRedirects(response, reverse("profiles:active"))
        old.refresh_from_db()
        self.assertEqual(old.profile, "old")
        self.assertFalse(old.is_active)
        active = GuidelineVersion.get_active()
        self.assertEqual((active.version, active.profile), (2, "new"))

    def test_create_form_prefills_from_active_version(self):
        GuidelineVersion.objects.create(profile="내 프로필", is_active=True)
        response = self.client.get(reverse("profiles:create"))
        self.assertEqual(response.context["form"].initial["profile"], "내 프로필")

    def test_activate_requires_post_and_switches(self):
        first = GuidelineVersion.objects.create(is_active=True)
        second = GuidelineVersion.objects.create()
        url = reverse("profiles:activate", args=[first.version])
        second.activate()
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse("profiles:list"))
        self.assertEqual(GuidelineVersion.get_active(), first)

    def test_compare_defaults_to_previous_version(self):
        for text in ("a", "b", "c"):
            GuidelineVersion.objects.create(profile=text)
        response = self.client.get(reverse("profiles:compare"), {"new": 2})
        self.assertEqual(response.context["old"].version, 1)
        self.assertEqual(response.context["new"].version, 2)
        self.assertContains(response, "변경 없음")

    def test_resident_number_pattern(self):
        hits = {"profile": "주민번호 900101-1234567", "extra": "9001011234567"}
        misses = {
            "profile": "연락처 010-1234-5678",
            "writing_rules": "사업자번호 123-45-67890, 주문번호 991301-1234567",  # 13월은 없다
            "extra": "",
        }
        self.assertEqual(find_resident_numbers(hits), ["profile", "extra"])
        self.assertEqual(find_resident_numbers(misses), [])

    def test_create_runs_privacy_check_and_marks_version_checked(self):
        self.assertRedirects(self.post_profile("백엔드 3년"), reverse("profiles:active"))
        self.assertTrue(GuidelineVersion.get_active().privacy_checked)
        user, role = self.run_agent.call_args.args
        self.assertEqual((user, role), (self.user, Role.PRIVACY))
        kwargs = self.run_agent.call_args.kwargs
        self.assertIn("## 찾을 항목", kwargs["system"])  # skill 파일의 본문
        self.assertIn("## 고정 정보\n백엔드 3년", kwargs["text"])
        self.assertNotIn("## 작성 규칙", kwargs["text"])  # 빈 섹션은 보내지 않는다

    def test_resident_number_blocks_save_without_calling_ai(self):
        response = self.post_profile("주민번호 900101-1234567")
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"], "profile", "주민등록번호로 보이는 숫자가 있습니다. 지워야 저장할 수 있습니다."
        )
        self.assertNotContains(response, 'id="privacy-override"')  # 그래도 저장할 수 없다
        self.run_agent.assert_not_called()
        self.assertFalse(GuidelineVersion.objects.exists())

    def test_ai_finding_blocks_save_until_confirmed(self):
        self.set_findings(FAMILY_FINDING)
        profile = "아버지는 은행 지점장\n백엔드 3년"
        response = self.post_profile(profile)
        self.assertFalse(GuidelineVersion.objects.exists())
        form = response.context["form"]
        self.assertFormError(
            form, "profile", "[가족 사항] “아버지는 은행 지점장” — 부모의 직업을 적었다."
        )
        self.assertContains(response, "개인정보로 보이는 내용이 1건")
        self.assertContains(response, 'id="privacy-override"')

        # 내용을 바꾸면 앞에서 받은 확인 값은 통하지 않고 다시 점검한다.
        self.run_agent.reset_mock()
        self.post_profile(profile + "\n어머니는 교사", privacy_confirm=form.privacy_confirm)
        self.run_agent.assert_called_once()
        self.assertFalse(GuidelineVersion.objects.exists())

        # 같은 내용으로 [그래도 저장]을 누르면 점검 없이 저장한다.
        self.run_agent.reset_mock()
        response = self.post_profile(profile, privacy_confirm=form.privacy_confirm)
        self.assertRedirects(response, reverse("profiles:active"))
        self.run_agent.assert_not_called()
        active = GuidelineVersion.get_active()
        self.assertEqual(active.profile.splitlines()[0], "아버지는 은행 지점장")
        self.assertTrue(active.privacy_checked)

    def test_create_saves_with_warning_when_ai_check_fails(self):
        self.run_agent.side_effect = ProviderError("Claude API 키가 없습니다.")
        response = self.post_profile("백엔드 3년")
        self.assertRedirects(response, reverse("profiles:active"))
        self.assertFalse(GuidelineVersion.get_active().privacy_checked)
        texts = [str(message) for message in response.wsgi_request._messages]
        self.assertIn("개인정보 점검을 하지 못한 채 저장했습니다. (Claude API 키가 없습니다.)", texts)

    def test_activate_checks_versions_that_were_not_checked(self):
        GuidelineVersion.objects.create(profile="현재", is_active=True, privacy_checked=True)
        old = GuidelineVersion.objects.create(profile="아버지는 은행 지점장")
        url = reverse("profiles:activate", args=[old.version])

        self.set_findings(FAMILY_FINDING)
        self.assertRedirects(self.client.post(url), reverse("profiles:detail", args=[old.version]))
        self.assertEqual(GuidelineVersion.get_active().profile, "현재")

        self.set_findings()
        self.assertRedirects(self.client.post(url), reverse("profiles:list"))
        old.refresh_from_db()
        self.assertTrue(old.is_active and old.privacy_checked)

        # 이미 점검한 버전은 다시 점검하지 않는다.
        self.run_agent.reset_mock()
        first = GuidelineVersion.objects.get(profile="현재")
        self.client.post(reverse("profiles:activate", args=[first.version]))
        self.run_agent.assert_not_called()
        self.assertEqual(GuidelineVersion.get_active(), first)

    def test_detail_page_shows_sections(self):
        version = GuidelineVersion.objects.create(profile="프로필 본문")
        response = self.client.get(reverse("profiles:detail", args=[version.version]))
        self.assertContains(response, "프로필 본문")

    def test_activation_is_offered_only_on_the_list(self):
        GuidelineVersion.objects.create(is_active=True)
        old = GuidelineVersion.objects.create()
        url = reverse("profiles:activate", args=[old.version])
        self.assertContains(self.client.get(reverse("profiles:list")), f'data-url="{url}"')
        detail = self.client.get(reverse("profiles:detail", args=[old.version]))
        self.assertNotContains(detail, url)
        self.assertContains(detail, "이 내용으로 새로 작성")
