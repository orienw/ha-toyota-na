"""Login continuation and expired-credential recovery."""

import copy
import base64
import hashlib
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import aiohttp
import jwt

import test_button as platform
from custom_components.toyota_na import patch_auth
from toyota_na.exceptions import LoginError, TokenExpired


class AuthFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = types.SimpleNamespace(
            authorize=AsyncMock(),
            request_tokens=AsyncMock(),
            get_id_info=AsyncMock(return_value={"email": "owner@example.com"}),
            get_tokens=lambda: {"access_token": "token"},
        )
        self.flow = platform.config_flow.ToyotaNAConfigFlow()
        self.flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
        self.flow.async_set_unique_id = AsyncMock(return_value=None)
        self.flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
        client_patch = patch.object(
            platform.config_flow, "ToyotaOneClient",
            return_value=types.SimpleNamespace(auth=self.auth),
        )
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.credentials = {"username": "owner", "password": "password"}

    async def test_authorized_login_finishes_without_asking_for_otp(self):
        self.auth.authorize.return_value = "authorization-code"

        result = await self.flow.async_step_user(self.credentials)

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["tokens"], {"access_token": "token"})
        self.auth.authorize.assert_awaited_once_with("owner", "password")
        self.auth.request_tokens.assert_awaited_once_with("authorization-code")

    async def test_otp_can_be_retried_before_finishing_login(self):
        self.auth.authorize.side_effect = [{"callbacks": []}, LoginError(), "code"]

        result = await self.flow.async_step_user(self.credentials)
        self.assertEqual(result["step_id"], "otp")
        self.auth.request_tokens.assert_not_awaited()
        with self.assertLogs(platform.config_flow.__name__, level="ERROR"):
            result = await self.flow.async_step_otp({"code": "wrong"})
        self.assertEqual(result["step_id"], "otp")
        self.assertEqual(result["errors"], {"base": "otp_not_logged_in"})
        self.auth.request_tokens.assert_not_awaited()

        result = await self.flow.async_step_otp({"code": "correct"})

        self.assertEqual(result["type"], "create_entry")
        self.auth.authorize.assert_awaited_with("owner", "password", "correct")
        self.auth.request_tokens.assert_awaited_once_with("code")

    async def test_expired_credentials_request_home_assistant_reauthentication(self):
        client = types.SimpleNamespace(auth=types.SimpleNamespace(login=AsyncMock()))
        entry = platform.ConfigEntry()
        entry.data = self.credentials
        with patch.object(
            platform.integration_runtime, "get_vehicles", AsyncMock(side_effect=LoginError())
        ):
            with self.assertRaises(platform.exceptions.ConfigEntryAuthFailed):
                await platform.integration_runtime.update_vehicles_status(
                    None, client, entry, None,
                )
        client.auth.login.assert_not_called()

    async def test_reauthentication_keeps_device_id_and_updates_existing_entry(self):
        entry = platform.ConfigEntry()
        entry.data = {"device_id": "existing-device", "tokens": {"access_token": "expired"}}
        self.auth.authorize.return_value = "code"
        self.flow.async_set_unique_id.return_value = entry
        entries = types.SimpleNamespace(async_update_entry=MagicMock(), async_reload=AsyncMock())
        self.flow.hass = types.SimpleNamespace(config_entries=entries)
        self.flow.async_abort = lambda **kwargs: {"type": "abort", **kwargs}

        result = await self.flow.async_step_user(self.credentials)

        self.assertEqual(result, {"type": "abort", "reason": "reauth_successful"})
        saved = entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual(saved["device_id"], "existing-device")
        self.assertEqual(saved["tokens"], {"access_token": "token"})
        entries.async_reload.assert_awaited_once_with(entry.entry_id)


class AuthCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_login_exchanges_its_own_s256_verifier(self):
        response = AsyncMock()
        response.status = 200
        response.json.return_value = {"tokenId": "session"}
        response.__aenter__.return_value = response
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "com.toyota.oneapp:/oauth2Callback?code=code"}
        redirect.__aenter__.return_value = redirect
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.return_value = response
        session.get.return_value = redirect
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        verifiers = []
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            for _ in range(2):
                code = await patch_auth.authorize(auth, "owner", "password")
                query = parse_qs(urlparse(session.get.call_args.args[0]).query)
                await patch_auth.request_tokens(auth, code)
                form = session.post.call_args.kwargs["data"]
                verifier = form["code_verifier"]
                self.assertEqual(len(verifier), 86)
                self.assertEqual(form["code"], "code")
                self.assertEqual(query["code_challenge_method"], ["S256"])
                self.assertEqual(query["code_challenge"], [
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
                ])
                self.assertIsNone(auth._code_verifier)
                verifiers.append(verifier)
        self.assertNotEqual(*verifiers)

    async def test_token_exchange_requires_authorization_and_preserves_failed_attempt(self):
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        with self.assertRaises(LoginError):
            await patch_auth.request_tokens(auth, "code")
        auth._code_verifier = "pending-verifier"
        response = AsyncMock()
        response.status = 400
        response.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.return_value = response
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            with self.assertRaises(LoginError):
                await patch_auth.request_tokens(auth, "code")
        auth._extract_tokens.assert_not_called()
        self.assertEqual(auth._code_verifier, "pending-verifier")

    async def test_refresh_of_saved_session_needs_no_login_verifier(self):
        auth = types.SimpleNamespace(_refresh_token="saved-refresh", _extract_tokens=MagicMock())
        response = AsyncMock()
        response.status = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = {"access_token": "new-token"}
        response.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.return_value = response
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            await patch_auth.refresh_tokens(auth)
        form = session.post.call_args.kwargs["data"]
        self.assertEqual(form["refresh_token"], "saved-refresh")
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertNotIn("code_verifier", form)
        auth._extract_tokens.assert_called_once_with({"access_token": "new-token"})

    async def test_concurrent_requests_refresh_an_expired_token_once(self):
        auth = patch_auth.ToyotaOneAuth()
        auth._expires_at = 0
        auth._guid = "guid"

        async def refresh():
            await asyncio.sleep(0)
            auth._extract_tokens({
                "access_token": "new-token", "refresh_token": "new-refresh",
                "id_token": jwt.encode({"sub": "guid"}, key="", algorithm="none"),
                "expires_in": 3600,
            })

        auth.refresh_tokens = AsyncMock(side_effect=refresh)
        results = await asyncio.gather(
            auth.get_access_token(), auth.get_access_token(), auth.get_guid(),
        )
        self.assertEqual(results, ["new-token", "new-token", "guid"])
        auth.refresh_tokens.assert_awaited_once()

    async def test_temporary_refresh_failure_does_not_expire_login(self):
        auth = patch_auth.ToyotaOneAuth()
        auth._expires_at = 0
        auth._refresh_token = "saved-refresh"
        response = AsyncMock()
        response.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.return_value = response
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            for status in (429, 503):
                response.status = status
                error = aiohttp.ClientResponseError(MagicMock(), (), status=status)
                response.raise_for_status = MagicMock(side_effect=error)
                with self.subTest(status=status), self.assertRaises(aiohttp.ClientResponseError):
                    await auth.check_tokens()
                self.assertEqual(auth._refresh_token, "saved-refresh")
            response.status = 400
            with self.assertRaises(TokenExpired):
                await auth.check_tokens()

    async def test_login_pauses_for_otp_before_exchanging_tokens(self):
        challenge = {"callbacks": []}
        auth = types.SimpleNamespace(
            authorize=AsyncMock(side_effect=[challenge, "code"]), request_tokens=AsyncMock(),
        )
        self.assertEqual(await patch_auth.login(auth, "owner", "password"), challenge)
        auth.request_tokens.assert_not_awaited()
        await patch_auth.login(auth, "owner", "password", "otp")
        auth.request_tokens.assert_awaited_once_with("code")

    async def test_invalid_otp_retry_uses_the_latest_server_challenge(self):
        def challenge(auth_id):
            return {
                "authId": auth_id,
                "callbacks": [{
                    "type": "PasswordCallback",
                    "output": [{"name": "prompt", "value": "One Time Password"}],
                    "input": [{"name": "IDToken1", "value": ""}],
                }],
            }

        rejected = challenge("retry-auth-id")
        rejected["callbacks"].append({
            "type": "TextOutputCallback",
            "output": [{"name": "message", "value": "Invalid OTP"}],
        })
        response = AsyncMock()
        response.status = 200
        response.json.side_effect = [
            challenge("first-auth-id"), rejected, {"tokenId": "session"},
            {"access_token": "new-token"},
        ]
        response.__aenter__.return_value = response
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "com.toyota.oneapp:/oauth2Callback?code=code"}
        redirect.__aenter__.return_value = redirect
        sent = []
        session = MagicMock()
        session.__aenter__.return_value = session

        def post(url, *, json=None, headers=None, data=None):
            if json is not None:
                sent.append(copy.deepcopy(json))
            return response

        session.post.side_effect = post
        session.get.return_value = redirect
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            await patch_auth.authorize(auth, "owner", "password")
            with self.assertLogs(patch_auth.__name__, level="ERROR"):
                with self.assertRaises(LoginError):
                    await patch_auth.authorize(auth, "owner", "password", "wrong")
            code = await patch_auth.authorize(auth, "owner", "password", "correct")
            verifier = auth._code_verifier
            await patch_auth.request_tokens(auth, code)

        self.assertEqual(code, "code")
        self.assertEqual(sent[-1]["authId"], "retry-auth-id")
        self.assertEqual(sent[-1]["callbacks"][0]["input"][0]["value"], "correct")
        self.assertEqual(session.post.call_args.kwargs["data"]["code_verifier"], verifier)
        auth._extract_tokens.assert_called_once_with({"access_token": "new-token"})
