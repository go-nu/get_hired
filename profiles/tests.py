from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .diff import compare_versions, diff_lines
from .models import GuidelineVersion


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

    def test_detail_page_shows_sections(self):
        version = GuidelineVersion.objects.create(profile="프로필 본문")
        response = self.client.get(reverse("profiles:detail", args=[version.version]))
        self.assertContains(response, "프로필 본문")
