"""Turnstile configuration and Siteverify response boundaries."""
import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs
from unittest.mock import patch

from atoms_demo.adapters.captcha_provider import TurnstileCaptchaProvider, create_adapter
from atoms_demo.config import load_settings
from atoms_demo.core.errors import DomainError, ErrorCode


class Response(io.BytesIO):
    status = 200


class TurnstileTests(unittest.TestCase):
    def _env(self, root: Path) -> dict[str, str]:
        return {
            "ATOMS_DATABASE_URL": f"sqlite:///{root / 'platform.db'}",
            "ATOMS_BLOB_DIRECTORY": str(root / "blobs"),
            "ATOMS_PUBLIC_ORIGIN": "http://app.preview.localhost:8080",
            "ATOMS_PREVIEW_ORIGIN": "http://preview.localhost:8001",
            "ATOMS_CAPTCHA_PROVIDER": "turnstile",
            "ATOMS_TURNSTILE_SITE_KEY": "public-test-key",
            "ATOMS_TURNSTILE_SECRET_KEY": "private-test-key",
        }

    def test_siteverify_checks_action_host_and_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = load_settings(self._env(Path(temporary)))
            self.assertNotIn("private-test-key", repr(settings))
            adapter = create_adapter(settings)
            self.assertIsInstance(adapter, TurnstileCaptchaProvider)

            def answer(body: bytes):
                with patch("atoms_demo.adapters.captcha_provider.urlopen",
                           return_value=Response(body)) as send:
                    result = asyncio.run(adapter.verify("synthetic-token", "request"))
                call = send.call_args
                self.assertEqual(call.args[0].full_url, adapter.SITEVERIFY)
                form = parse_qs(call.args[0].data.decode("ascii"))
                self.assertEqual(form, {"secret": ["private-test-key"],
                                        "response": ["synthetic-token"]})
                return result

            self.assertTrue(answer(b'{"success":true,"hostname":"app.preview.localhost","action":"register"}'))
            self.assertFalse(answer(b'{"success":true,"hostname":"elsewhere.example","action":"register"}'))
            self.assertFalse(answer(b'{"success":true,"hostname":"app.preview.localhost","action":"login"}'))
            self.assertFalse(answer(b'{"success":false,"error-codes":["timeout-or-duplicate"]}'))
            with patch("atoms_demo.adapters.captcha_provider.urlopen") as send:
                self.assertFalse(asyncio.run(adapter.verify("", "request")))
                self.assertFalse(asyncio.run(adapter.verify("x" * 2049, "request")))
                send.assert_not_called()
            with patch("atoms_demo.adapters.captcha_provider.urlopen",
                       side_effect=URLError("offline")):
                with self.assertRaises(DomainError) as unavailable:
                    asyncio.run(adapter.verify("synthetic-token", "request"))
            self.assertEqual(unavailable.exception.code, ErrorCode.PROVIDER_UNAVAILABLE)

    def test_turnstile_configuration_is_explicit_and_separate_from_private_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = self._env(Path(temporary))
            env.pop("ATOMS_TURNSTILE_SECRET_KEY")
            with self.assertRaisesRegex(ValueError, "site key and secret key"):
                load_settings(env)
            env = self._env(Path(temporary))
            env["ATOMS_PRIVATE_TEST_MODE"] = "true"
            env["ATOMS_PRIVATE_MAIL_OUTBOX"] = str(Path(temporary) / "private-mail" / "outbox.jsonl")
            with self.assertRaisesRegex(ValueError, "official Turnstile test keys"):
                load_settings(env)
            env["ATOMS_TURNSTILE_SITE_KEY"] = "1x00000000000000000000AA"
            env["ATOMS_TURNSTILE_SECRET_KEY"] = "1x0000000000000000000000000000000AA"
            settings = load_settings(env)
            adapter = create_adapter(settings)
            self.assertIsInstance(adapter, TurnstileCaptchaProvider)
            self.assertTrue(adapter.test_mode)
            with patch("atoms_demo.adapters.captcha_provider.urlopen",
                       return_value=Response(b'{"success":true,"hostname":"example.com",'
                                             b'"metadata":{"result_with_testing_key":true}}')):
                self.assertTrue(asyncio.run(adapter.verify("XXXX.DUMMY.TOKEN.XXXX", "request")))
            with patch("atoms_demo.adapters.captcha_provider.urlopen",
                       return_value=Response(b'{"success":true,"hostname":"example.com"}')):
                self.assertFalse(asyncio.run(adapter.verify("XXXX.DUMMY.TOKEN.XXXX", "request")))
            env["ATOMS_PRIVATE_TEST_MODE"] = "false"
            env.pop("ATOMS_PRIVATE_MAIL_OUTBOX")
            with self.assertRaisesRegex(ValueError, "test keys require private test mode"):
                load_settings(env)


if __name__ == "__main__":
    unittest.main()
