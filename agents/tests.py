import json
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from applications.test_views import LoggedInTestCase

from .crypto import decrypt, encrypt
from .models import AgentRun, AgentSettings, Provider
from .providers import Completion, ProviderError, complete
from .services import run_agent


class CryptoTests(TestCase):
    def test_round_trip_and_ciphertext_differs(self):
        token = encrypt("sk-secret")
        self.assertNotIn("sk-secret", token)
        self.assertEqual(decrypt(token), "sk-secret")

    def test_empty_and_undecryptable_values_become_empty(self):
        self.assertEqual(encrypt(""), "")
        self.assertEqual(decrypt(""), "")
        token = encrypt("sk-secret")
        with override_settings(SECRET_KEY="another-secret-key"):
            self.assertEqual(decrypt(token), "")


class AgentSettingsPageTests(LoggedInTestCase):
    url = reverse("agents:settings")

    def post(self, **overrides):
        data = {
            "provider": Provider.CLAUDE,
            "claude_model": "claude-opus-5-5",
            "gemini_model": "gemini-2.5-flash",
            "claude_api_key": "",
            "gemini_api_key": "",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_key_is_stored_encrypted_and_never_rendered(self):
        self.post(claude_api_key="sk-ant-test-1234")
        settings = AgentSettings.objects.get(user=self.user)
        self.assertNotIn("sk-ant-test-1234", settings.claude_api_key)
        self.assertEqual(settings.get_api_key(Provider.CLAUDE), "sk-ant-test-1234")
        response = self.client.get(self.url)
        self.assertNotContains(response, "sk-ant-test-1234")
        self.assertContains(response, "····1234")

    def test_blank_key_keeps_saved_key(self):
        self.post(claude_api_key="sk-ant-test-1234")
        self.post(provider=Provider.GEMINI, gemini_api_key="gm-key-5678")
        settings = AgentSettings.objects.get(user=self.user)
        self.assertEqual(settings.provider, Provider.GEMINI)
        self.assertEqual(settings.get_api_key(Provider.CLAUDE), "sk-ant-test-1234")
        self.assertEqual(settings.get_api_key(), "gm-key-5678")
        self.assertEqual(settings.model, "gemini-2.5-flash")

    def test_usage_table_sums_tokens_per_provider(self):
        for tokens in (100, 250):
            AgentRun.objects.create(
                user=self.user,
                role=AgentRun.Role.OCR,
                provider=Provider.CLAUDE,
                model="claude-opus-5-5",
                input_tokens=tokens,
                output_tokens=10,
            )
        rows = self.client.get(self.url).context["usage_rows"]
        claude, gemini = rows
        self.assertEqual((claude["total"]["runs"], claude["total"]["input"]), (2, 350))
        self.assertEqual(claude["month"]["output"], 20)
        self.assertEqual(gemini["total"]["runs"], 0)


class RunAgentTests(LoggedInTestCase):
    def test_missing_api_key_is_reported_without_calling_provider(self):
        with self.assertRaisesMessage(ProviderError, "API 키가 없습니다"):
            complete(AgentSettings.for_user(self.user), system="s", text="t")

    @mock.patch("agents.services.complete")
    def test_success_records_output_and_tokens(self, complete_mock):
        complete_mock.return_value = Completion("결과", 120, 30)
        run = run_agent(self.user, AgentRun.Role.OCR, system="지시", text="본문")
        self.assertEqual(run.status, AgentRun.Status.SUCCEEDED)
        self.assertEqual((run.output, run.total_tokens), ("결과", 150))
        self.assertIn("지시", run.prompt)
        self.assertIsNotNone(run.finished_at)

    @mock.patch("agents.services.complete")
    def test_failure_is_recorded_and_raised(self, complete_mock):
        complete_mock.side_effect = ProviderError("키가 틀림")
        with self.assertRaises(ProviderError):
            run_agent(self.user, AgentRun.Role.OCR, system="s", text="t")
        run = AgentRun.objects.get()
        self.assertEqual((run.status, run.error), (AgentRun.Status.FAILED, "키가 틀림"))

    @mock.patch("agents.services.complete")
    def test_unexpected_error_is_recorded_as_failure(self, complete_mock):
        complete_mock.side_effect = ValueError("boom")
        with self.assertRaises(ProviderError):
            run_agent(self.user, AgentRun.Role.OCR, system="s", text="t")
        self.assertIn("ValueError: boom", AgentRun.objects.get().error)


class RunPagesTests(LoggedInTestCase):
    def make_run(self, **kwargs):
        kwargs.setdefault("role", AgentRun.Role.OCR)
        return AgentRun.objects.create(
            user=self.user, provider=Provider.CLAUDE, model="claude-opus-5-5", **kwargs
        )

    def test_pages_require_login(self):
        run = self.make_run()
        self.client.logout()
        for url in (
            reverse("agents:settings"),
            reverse("agents:run_list"),
            reverse("agents:run_detail", args=[run.pk]),
        ):
            self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")

    def test_list_filters_by_status(self):
        self.make_run(status=AgentRun.Status.SUCCEEDED)
        failed = self.make_run(status=AgentRun.Status.FAILED, error="오류 내용")
        response = self.client.get(reverse("agents:run_list"), {"status": "failed"})
        self.assertEqual(list(response.context["runs"]), [failed])
        self.assertContains(response, "실패 1건")

    def test_cost_table_follows_filter_and_flags_unknown_models(self):
        self.make_run(input_tokens=1_000_000, output_tokens=100_000)  # 4 + 2 달러
        self.make_run(role=AgentRun.Role.REVIEW, input_tokens=500_000)  # 2 달러
        AgentRun.objects.create(
            user=self.user, role=AgentRun.Role.OCR, provider=Provider.GEMINI, model="new-model"
        )
        response = self.client.get(reverse("agents:run_list"))
        rows = {row["model"]: row for row in response.context["cost_rows"]}
        self.assertEqual(rows["claude-opus-5-5"]["cost"], 8)
        self.assertIsNone(rows["new-model"]["cost"])
        self.assertEqual(response.context["cost_total"], 8)
        self.assertContains(response, "단가 미등록")
        response = self.client.get(reverse("agents:run_list"), {"role": "review"})
        self.assertEqual(response.context["cost_total"], 2)

    def test_empty_list_and_detail_render(self):
        self.assertContains(self.client.get(reverse("agents:run_list")), "실행 기록이 없습니다.")
        run = self.make_run(status=AgentRun.Status.FAILED, error="오류 내용", prompt="입력 글")
        response = self.client.get(reverse("agents:run_detail", args=[run.pk]))
        self.assertContains(response, "오류 내용")
        self.assertContains(response, "입력 글")


class PostingOcrTests(LoggedInTestCase):
    url = reverse("applications:posting_ocr")

    def image(self, name="capture.png", content_type="image/png", size=10):
        return SimpleUploadedFile(name, b"x" * size, content_type=content_type)

    @mock.patch("agents.services.complete")
    def test_returns_fields_read_from_images(self, complete_mock):
        complete_mock.return_value = Completion(
            json.dumps({"main_tasks": "- API 개발 ", "requirements": "- Python", "preferred": ""}),
            900,
            40,
        )
        response = self.client.post(self.url, {"images": [self.image(), self.image("b.png")]})
        self.assertEqual(
            response.json()["fields"],
            {"main_tasks": "- API 개발", "requirements": "- Python", "preferred": ""},
        )
        run = AgentRun.objects.get()
        self.assertEqual((run.role, run.image_count, run.input_tokens), ("ocr", 2, 900))
        images = complete_mock.call_args.kwargs["images"]
        self.assertEqual(images[0], (b"x" * 10, "image/png"))

    def test_rejects_missing_wrong_type_and_too_many(self):
        self.assertEqual(self.client.post(self.url).status_code, 400)
        response = self.client.post(
            self.url, {"images": [self.image("a.pdf", "application/pdf")]}
        )
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            self.url, {"images": [self.image(f"{i}.png") for i in range(6)]}
        )
        self.assertIn("5장", response.json()["error"])
        self.assertEqual(AgentRun.objects.count(), 0)

    def test_missing_api_key_returns_readable_error(self):
        response = self.client.post(self.url, {"images": [self.image()]})
        self.assertEqual(response.status_code, 502)
        self.assertIn("API 키가 없습니다", response.json()["error"])
        self.assertEqual(AgentRun.objects.get().status, AgentRun.Status.FAILED)

    @mock.patch("agents.services.complete")
    def test_memo_ocr_returns_plain_text(self, complete_mock):
        complete_mock.return_value = Completion(" 면접 일정: 10/20 14시\n장소: 본사 3층 ", 500, 20)
        response = self.client.post(reverse("applications:memo_ocr"), {"images": [self.image()]})
        self.assertEqual(response.json(), {"text": "면접 일정: 10/20 14시\n장소: 본사 3층"})
        self.assertIsNone(complete_mock.call_args.kwargs["schema"])  # 칸 나누기 없이 글 그대로
        self.assertEqual(self.client.post(reverse("applications:memo_ocr")).status_code, 400)

    def test_form_shows_capture_box(self):
        response = self.client.get(reverse("applications:create"))
        self.assertContains(response, 'id="posting-ocr"')
        self.assertContains(response, 'id="memo-ocr"')
