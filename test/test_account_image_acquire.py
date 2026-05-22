from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services import account_service as account_service_module
from services.account_service import AccountService
from services.storage.json_storage import JSONStorageBackend


def _make_service(tmp_dir: str, *, tokens: int = 2, quota: int = 5) -> AccountService:
    service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
    for index in range(tokens):
        token = f"token-{index}"
        service.add_accounts([token])
        service.update_account(token, {"status": "正常", "quota": quota})
    return service


class GetAvailableAccessTokenTests(unittest.TestCase):
    def test_does_not_call_fetch_remote_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _make_service(tmp_dir, tokens=1)

            def boom(*args: Any, **kwargs: Any) -> None:
                raise AssertionError("fetch_remote_info must not be called from the hot path")

            with patch.object(service, "fetch_remote_info", side_effect=boom):
                token = service.get_available_access_token()

            self.assertEqual(token, "token-0")
            self.assertEqual(service._image_inflight.get(token), 1)

    def test_raises_when_no_account_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _make_service(tmp_dir, tokens=1, quota=0)
            service.update_account("token-0", {"status": "限流", "quota": 0})

            with self.assertRaises(RuntimeError):
                service.get_available_access_token()

    def test_mark_image_result_releases_inflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _make_service(tmp_dir, tokens=1)
            token = service.get_available_access_token()

            self.assertEqual(service._image_inflight.get(token), 1)
            service.mark_image_result(token, success=True)
            self.assertNotIn(token, service._image_inflight)

    def test_concurrency_cap_excludes_saturated_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _make_service(tmp_dir, tokens=2)
            cap = max(1, int(account_service_module.config.image_account_concurrency or 1))
            for _ in range(cap):
                service._image_inflight["token-0"] = (
                    service._image_inflight.get("token-0", 0) + 1
                )

            token = service.get_available_access_token()

            self.assertEqual(token, "token-1")
            self.assertEqual(service._image_inflight["token-1"], 1)


class StreamImageOutputsWithPoolTests(unittest.TestCase):
    def test_image_poll_timeout_releases_slot(self) -> None:
        from services.openai_backend_api import ImagePollTimeoutError
        from services.protocol import conversation as conversation_module
        from services.protocol.conversation import (
            ConversationRequest,
            stream_image_outputs_with_pool,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            service = _make_service(tmp_dir, tokens=1)

            def fake_acquire() -> str:
                return service.get_available_access_token()

            def fake_mark(token: str, success: bool) -> None:
                service.mark_image_result(token, success=success)

            def raise_timeout(*_args: Any, **_kwargs: Any) -> Any:
                raise ImagePollTimeoutError("upstream poll exceeded the budget")
                yield  # pragma: no cover - generator marker

            with patch.object(conversation_module.account_service, "get_available_access_token", fake_acquire), \
                 patch.object(conversation_module.account_service, "mark_image_result", fake_mark), \
                 patch.object(conversation_module, "stream_image_outputs", raise_timeout), \
                 patch.object(conversation_module, "OpenAIBackendAPI", lambda **_kwargs: None):
                request = ConversationRequest(prompt="x", model="gpt-image-2", n=1)
                with self.assertRaises(ImagePollTimeoutError):
                    list(stream_image_outputs_with_pool(request))

            self.assertEqual(service._image_inflight, {})


if __name__ == "__main__":
    unittest.main()
