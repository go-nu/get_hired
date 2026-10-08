import json
import urllib.error
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from applications.test_views import LoggedInTestCase

from .crypto import decrypt, encrypt
from .models import AgentJob, AgentRun, AgentSettings, Provider, Status
from .notify import notify_user, send_discord
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


WEBHOOK = "https://discord.com/api/webhooks/123/abc"


class DiscordTests(LoggedInTestCase):
    url = reverse("agents:settings")
    form_data = {
        "provider": Provider.CLAUDE,
        "claude_model": "claude-opus-5-5",
        "gemini_model": "gemini-3.5-flash-lite",
    }

    @mock.patch("agents.notify.urllib.request.urlopen")
    def test_send_posts_json_only_to_discord(self, urlopen):
        self.assertEqual(send_discord(WEBHOOK, "안녕" * 2000), (True, ""))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, WEBHOOK)
        self.assertEqual(len(json.loads(request.data)["content"]), 2000)  # 길이 제한
        self.assertEqual(request.get_header("User-agent"), "get-hired/1.0")
        urlopen.reset_mock()
        ok, reason = send_discord("https://example.com/hook", "안녕")
        self.assertFalse(ok)
        urlopen.assert_not_called()

    @mock.patch("agents.notify.urllib.request.urlopen")
    def test_send_reports_failures_without_raising(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(WEBHOOK, 404, "Not Found", None, None)
        ok, reason = send_discord(WEBHOOK, "안녕")
        self.assertEqual((ok, "404" in reason), (False, True))
        urlopen.side_effect = urllib.error.URLError("down")
        self.assertFalse(send_discord(WEBHOOK, "안녕")[0])

    @mock.patch("agents.notify.urllib.request.urlopen")
    def test_settings_page_saves_webhook_encrypted_and_sends_test(self, urlopen):
        response = self.client.post(self.url, {**self.form_data, "discord_webhook": "http://evil.example/x"})
        self.assertContains(response, "디스코드 웹훅 주소가 아닙니다")
        urlopen.assert_not_called()
        response = self.client.post(self.url, {**self.form_data, "discord_webhook": WEBHOOK}, follow=True)
        settings = AgentSettings.objects.get(user=self.user)
        self.assertNotIn("abc", settings.discord_webhook)
        self.assertEqual(settings.get_discord_webhook(), WEBHOOK)
        urlopen.assert_called_once()
        self.assertContains(response, "시험 알림을 보냈습니다")
        self.assertNotContains(response, WEBHOOK)
        # 비워서 저장하면 그대로 두고, "알림 끄기"를 체크하면 지운다.
        self.client.post(self.url, self.form_data)
        self.assertTrue(notify_user(self.user, "알림"))
        self.client.post(self.url, {**self.form_data, "clear_discord_webhook": "on"})
        self.assertFalse(notify_user(self.user, "알림"))
        self.assertEqual(urlopen.call_count, 2)  # 시험 알림 1 + 알림 1

    def send(self, kind):
        return self.client.post(reverse("agents:discord_send", args=[kind]), follow=True)

    def sent_text(self, urlopen):
        return json.loads(urlopen.call_args.args[0].data)["content"]

    @mock.patch("agents.notify.urllib.request.urlopen")
    def test_send_buttons_need_a_webhook_and_something_to_send(self, urlopen):
        self.assertNotContains(self.client.get(self.url), "discord/deadline/")
        self.assertContains(self.send("test"), "웹훅 주소를 먼저 저장하세요")
        settings = AgentSettings.for_user(self.user)
        settings.set_discord_webhook(WEBHOOK)
        settings.save()
        response = self.client.get(self.url)
        for kind in ("test", "analysis", "waiting", "deadline"):
            self.assertContains(response, f"discord/{kind}/")
        # 보낼 것이 없으면 보내지 않고 알려 준다.
        self.assertContains(self.send("analysis"), "아직 끝난 분석 작업이 없습니다")
        self.assertContains(self.send("waiting"), "확인 대기인 건이 없습니다")
        self.assertContains(self.send("deadline"), "마감이 임박한 미지원 건이 없습니다")
        urlopen.assert_not_called()
        self.assertEqual(self.send("unknown").status_code, 404)
        self.assertEqual(self.client.get(reverse("agents:discord_send", args=["test"])).status_code, 405)

    @mock.patch("agents.notify.urllib.request.urlopen")
    def test_send_buttons_send_each_kind_of_alert(self, urlopen):
        settings = AgentSettings.for_user(self.user)
        settings.set_discord_webhook(WEBHOOK)
        settings.save()
        today = timezone.localdate()
        urgent = self.make_application("임박기업", deadline=today + timedelta(days=2))
        scored = self.make_application("완료기업", fit_score=81, fit_grade="mid_high")
        AgentJob.objects.create(user=self.user, application=scored, status=Status.SUCCEEDED)
        AgentJob.objects.create(user=self.user, application=urgent, status=Status.WAITING)

        self.assertContains(self.send("test"), "디스코드로 보냈습니다: 연결 시험")
        self.assertIn("시험 알림", self.sent_text(urlopen))
        self.send("analysis")  # 가장 최근에 멈춘 작업은 확인 대기
        self.assertIn("[확인 대기] 검수 지적이 남아", self.sent_text(urlopen))
        self.send("waiting")
        self.assertIn("[확인 대기] 에이전트가 답을 기다리는 1건\n- 임박기업", self.sent_text(urlopen))
        self.send("deadline")
        self.assertIn("[마감 임박] 아직 지원하지 않은 1건\n- D-2 · 임박기업", self.sent_text(urlopen))
        self.assertEqual(urlopen.call_count, 4)


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
