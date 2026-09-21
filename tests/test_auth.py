"""Login continuation and expired-credential recovery."""

import copy
import base64
import hashlib
import asyncio
import os
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import aiohttp
import jwt

import test_button as platform
from custom_components.toyota_na import patch_auth
from toyota_na.exceptions import LoginError, TokenExpired


class TokenStorageTests(unittest.IsolatedAsyncioTestCase):
    def token_response(self):
        return {
            "access_token": "access", "refresh_token": "refresh",
            "id_token": jwt.encode({"sub": "guid"}, key="", algorithm="none"),
            "expires_in": 3600,
        }

    async def test_expiry_is_independent_of_local_timezone(self):
        old_timezone = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
                with self.subTest(zone=zone):
                    os.environ["TZ"] = zone
                    time.tzset()
                    auth = patch_auth.ToyotaOneAuth(refresh_secs=-30)
                    with patch.object(patch_auth.time, "time", return_value=1800000000):
                        auth._extract_tokens(self.token_response())
                        self.assertEqual(1800003600, auth.get_tokens()["expires_at"])
                        self.assertTrue(auth.logged_in())
                    auth.refresh_tokens = AsyncMock()
                    with patch.object(patch_auth.time, "time", return_value=1800003570):
                        await auth.check_tokens()
                    auth.refresh_tokens.assert_awaited_once()
        finally:
            if old_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_timezone
            time.tzset()

    async def test_saved_legacy_sessions_refresh_once_without_a_new_login(self):
        auth = patch_auth.ToyotaOneAuth()
        auth._extract_tokens(self.token_response())
        tokens = auth.get_tokens()
        del tokens["clock"]
        migrated = patch_auth.ToyotaOneAuth(initial_tokens=tokens)
        migrated.refresh_tokens = AsyncMock(side_effect=lambda: migrated._extract_tokens(self.token_response()))
        await migrated.check_tokens()
        await migrated.check_tokens()
        migrated.refresh_tokens.assert_awaited_once()
        self.assertEqual("refresh", migrated.get_tokens()["refresh_token"])
        self.assertEqual("unix", migrated.get_tokens()["clock"])

        reloaded = patch_auth.ToyotaOneAuth(initial_tokens=migrated.get_tokens())
        reloaded.refresh_tokens = AsyncMock()
        await reloaded.check_tokens()
        reloaded.refresh_tokens.assert_not_awaited()

    async def test_refresh_keeps_omitted_tokens_and_persists_rotated_tokens(self):
        callback = MagicMock()
        auth = patch_auth.ToyotaOneAuth(callback=callback)
        auth._extract_tokens(self.token_response())
        identity = auth._id_token
        auth._extract_tokens({"access_token": "next-access", "expires_in": 1800})
        self.assertEqual("refresh", auth._refresh_token)
        self.assertEqual(identity, auth._id_token)
        self.assertEqual("guid", auth._guid)
        auth._extract_tokens({"access_token": "third-access", "refresh_token": "rotated", "expires_in": 1800})
        self.assertEqual("rotated", callback.call_args.args[0]["refresh_token"])
        self.assertEqual("unix", callback.call_args.args[0]["clock"])

    async def test_malformed_refresh_preserves_the_entire_saved_session(self):
        auth = patch_auth.ToyotaOneAuth()
        auth._extract_tokens(self.token_response())
        before = auth.get_tokens()
        for response in (
            {"expires_in": 3600},
            {"access_token": "new", "expires_in": None},
            {"access_token": "new", "expires_in": "NaN"},
            {"access_token": "new", "expires_in": -1},
            {"access_token": "new", "expires_in": 3600, "id_token": "invalid"},
        ):
            with self.subTest(response=response), self.assertRaises((RuntimeError, jwt.InvalidTokenError)):
                auth._extract_tokens(response)
            self.assertEqual(before, auth.get_tokens())

    async def test_concurrent_rejections_of_one_token_share_a_refresh(self):
        auth = patch_auth.ToyotaOneAuth()
        auth._extract_tokens(self.token_response())
        auth.refresh_tokens = AsyncMock(side_effect=lambda: auth._extract_tokens({
            **self.token_response(), "access_token": "replacement",
        }))
        await asyncio.gather(*(auth.check_tokens(rejected_token="access") for _ in range(4)))
        auth.refresh_tokens.assert_awaited_once()
        await auth.check_tokens(rejected_token="access")
        auth.refresh_tokens.assert_awaited_once()

    async def test_runtime_refreshes_near_expiry_instead_of_every_five_minutes(self):
        auth = patch_auth.ToyotaOneAuth(refresh_secs=-180)
        with patch.object(patch_auth.time, "time", return_value=1800000000):
            auth._extract_tokens(self.token_response())
        auth.refresh_tokens = AsyncMock(side_effect=lambda: auth._extract_tokens(self.token_response()))
        for elapsed in (0, 300, 600, 1200, 1800, 2400, 3000, 3419):
            with patch.object(patch_auth.time, "time", return_value=1800000000 + elapsed):
                await auth.check_tokens()
        auth.refresh_tokens.assert_not_awaited()
        with patch.object(patch_auth.time, "time", return_value=1800003420):
            await asyncio.gather(*(auth.get_access_token() for _ in range(4)))
        auth.refresh_tokens.assert_awaited_once()

    async def test_short_lived_tokens_do_not_refresh_repeatedly_inside_the_margin(self):
        for lifetime in (120, 10):
            with self.subTest(lifetime=lifetime):
                auth = patch_auth.ToyotaOneAuth(refresh_secs=-180)
                response = {**self.token_response(), "expires_in": lifetime}
                with patch.object(patch_auth.time, "time", return_value=1800000000):
                    auth._extract_tokens(response)
                    auth.refresh_tokens = AsyncMock(side_effect=lambda: auth._extract_tokens(response))
                    await auth.check_tokens()
                    await auth.check_tokens()
                auth.refresh_tokens.assert_not_awaited()
                with patch.object(patch_auth.time, "time", return_value=1800000000 + lifetime * 0.9):
                    await asyncio.gather(*(auth.check_tokens() for _ in range(4)))
                    await auth.check_tokens()
                auth.refresh_tokens.assert_awaited_once()


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

    async def test_failed_authentication_does_not_log_callback_body(self):
        response = MagicMock(status=401)
        response.text = AsyncMock(return_value='{"authId":"private-auth-session","callbacks":[]}')
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = response
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            with self.assertLogs(patch_auth.__name__, level="INFO") as logs:
                with self.assertRaises(LoginError):
                    await patch_auth.authorize(types.SimpleNamespace(), "owner", "password")
        self.assertIn("401", " ".join(logs.output))
        self.assertNotIn("private-auth-session", " ".join(logs.output))

    async def test_authorized_login_finishes_without_asking_for_otp(self):
        self.auth.authorize.return_value = "authorization-code"

        result = await self.flow.async_step_user(self.credentials)

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["tokens"], {"access_token": "token"})
        self.assertNotIn("password", result["data"])
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
        self.assertNotIn("password", result["data"])
        self.auth.request_tokens.assert_awaited_once_with("code")

    async def test_identity_provider_account_reports_password_setup_error(self):
        self.auth.authorize.side_effect = patch_auth.SsoAccountError()

        with self.assertLogs(platform.config_flow.__name__, level="ERROR"):
            result = await self.flow.async_step_user(self.credentials)

        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["errors"], {"base": "sso_account"})
        self.auth.request_tokens.assert_not_awaited()

    async def test_identity_provider_error_during_otp_returns_to_credentials(self):
        self.auth.authorize.side_effect = patch_auth.SsoAccountError()
        self.flow.user_info = self.credentials
        self.flow.client = types.SimpleNamespace(auth=self.auth)

        with self.assertLogs(platform.config_flow.__name__, level="ERROR"):
            result = await self.flow.async_step_otp({"code": "123456"})

        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["errors"], {"base": "sso_account"})
        self.assertEqual(
            result["data_schema"]({"username": "owner", "password": "password"}),
            self.credentials,
        )
        self.auth.request_tokens.assert_not_awaited()

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
        entry.data = {"device_id": "existing-device", "tokens": {"access_token": "expired"}, "password": "old-password"}
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
        self.assertNotIn("password", saved)
        entries.async_reload.assert_awaited_once_with(entry.entry_id)


    async def test_reauth_updates_original_entry_when_account_email_changes(self):
        entry = platform.ConfigEntry()
        entry.data = {"email": "old@example.com", "device_id": "existing-device", "tokens": {"guid": "same-account"}, "password": "old-password"}
        self.flow.context = {"source": "reauth", "entry_id": entry.entry_id}
        self.flow._get_reauth_entry = lambda: entry
        self.flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort", "reason": "reauth_successful"})
        self.auth.get_tokens = lambda: {"guid": "same-account", "access_token": "new"}
        self.auth.authorize.return_value = "code"

        await self.flow.async_step_reauth(entry.data)
        result = await self.flow.async_step_user(self.credentials)

        self.assertEqual("reauth_successful", result["reason"])
        call = self.flow.async_update_reload_and_abort.call_args
        self.assertIs(entry, call.args[0])
        self.assertEqual("owner@example.com", call.kwargs["data"]["email"])
        self.assertEqual("existing-device", call.kwargs["data"]["device_id"])
        self.assertNotIn("password", call.kwargs["data"])
        self.flow.async_set_unique_id.assert_not_awaited()
        self.assertEqual("existing-device", entry.data["device_id"])

    async def test_reauth_rejects_another_account_without_updating_any_entry(self):
        entry = platform.ConfigEntry()
        entry.data = {"email": "owner@example.com", "tokens": {"guid": "original-account"}}
        self.flow.context = {"source": "reauth", "entry_id": entry.entry_id}
        self.flow._get_reauth_entry = lambda: entry
        self.flow.async_abort = lambda **kwargs: {"type": "abort", **kwargs}
        self.flow.async_update_reload_and_abort = MagicMock()
        self.auth.get_tokens = lambda: {"guid": "different-account"}
        self.auth.authorize.return_value = "code"

        result = await self.flow.async_step_user(self.credentials)

        self.assertEqual("reauth_wrong_account", result["reason"])
        self.flow.async_set_unique_id.assert_not_awaited()
        self.flow.async_update_reload_and_abort.assert_not_called()

    async def test_reauth_email_fallback_is_case_insensitive(self):
        entry = platform.ConfigEntry()
        entry.data = {"email": "OWNER@EXAMPLE.COM", "tokens": {}}
        self.flow.context = {"source": "reauth", "entry_id": entry.entry_id}
        self.flow._get_reauth_entry = lambda: entry
        self.flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort", "reason": "reauth_successful"})
        self.auth.authorize.return_value = "code"
        result = await self.flow.async_step_user(self.credentials)
        self.assertEqual("reauth_successful", result["reason"])
        self.flow.async_update_reload_and_abort.assert_called_once()

    async def test_setup_removes_saved_password_even_when_tokens_are_expired(self):
        entry = platform.ConfigEntry()
        entry.data = {"device_id": "existing-device", "tokens": {"access_token": "expired"}, "password": "old-password"}
        hass = platform.FakeHass(None)
        auth = MagicMock(check_tokens=AsyncMock(side_effect=LoginError()))
        with (
            patch.object(platform.integration_runtime, "ToyotaOneAuth", return_value=auth) as auth_type,
            patch.object(platform.integration_runtime, "ToyotaOneClient", return_value=types.SimpleNamespace(auth=auth)),
            self.assertLogs(platform.integration_runtime.__name__, level="ERROR"),
            self.assertRaises(platform.exceptions.ConfigEntryAuthFailed),
        ):
            await platform.integration_runtime.async_setup_entry(hass, entry)
        self.assertEqual({"device_id": "existing-device", "tokens": {"access_token": "expired"}}, entry.data)
        self.assertEqual(-180, auth_type.call_args.kwargs["refresh_secs"])


class AuthPromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = types.SimpleNamespace()
        self.response = AsyncMock(status=200)
        self.response.__aenter__.return_value = self.response
        redirect = AsyncMock(status=302)
        redirect.headers = {"Location": "com.toyota.oneapp:/oauth2Callback?code=code"}
        redirect.__aenter__.return_value = redirect
        self.session = MagicMock()
        self.session.__aenter__.return_value = self.session
        self.session.get.return_value = redirect
        self.sent = []

        def post(url, *, json, headers):
            self.sent.append(copy.deepcopy(json))
            return self.response

        self.session.post.side_effect = post
        session_patch = patch.object(patch_auth.aiohttp, "ClientSession", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def callback(self, kind, prompt, value=""):
        return {
            "type": kind,
            "output": [{"name": "prompt", "value": prompt}],
            "input": [{"name": "IDToken1", "value": value}],
        }

    async def test_authorization_accepts_valid_redirects(self):
        self.response.json.return_value = {"tokenId": "session"}
        redirect = self.session.get.return_value
        redirect.headers = {
            "Location": "com.toyota.oneapp:/oauth2Callback?code=encoded%2Bcode%3D",
        }
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                redirect.status = status
                self.assertEqual(
                    "encoded+code=", await patch_auth.authorize(self.auth, "owner", "secret"),
                )
                self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    async def test_authorization_rejects_invalid_redirects(self):
        self.response.json.return_value = {"tokenId": "session"}
        redirect = self.session.get.return_value
        callback = "com.toyota.oneapp:/oauth2Callback"
        for status, location in (
            (200, f"{callback}?code=code"),
            (304, f"{callback}?code=code"),
            (400, f"{callback}?code=code"),
            (302, None),
            (302, ""),
            (302, "https://example.com/oauth2Callback?code=code"),
            (302, "com.toyota.oneapp://example.com/oauth2Callback?code=code"),
            (302, "com.toyota.oneapp:/other?code=code"),
            (302, "https://[invalid?code=code"),
            (302, callback),
            (302, f"{callback}?code="),
            (302, f"{callback}?code=%20"),
            (302, f"{callback}?code=first&code=second"),
            (302, f"{callback}?code=first&code="),
            (302, f"{callback}?error=access_denied"),
            (302, f"{callback}?code=code&error=access_denied"),
            (302, f"{callback}#code=code"),
            (302, f"{callback}?code=code#unexpected"),
        ):
            with self.subTest(status=status, location=location), self.assertRaises(LoginError):
                redirect.status = status
                redirect.headers = {} if location is None else {"Location": location}
                await patch_auth.authorize(self.auth, "owner", "secret")

    async def test_renamed_prompts_fill_credentials_and_preserve_metadata(self):
        for name, password, locale in (
            ("User Name", "Password", "ui_locales"),
            ("Username", "Enter your password", "UI Locale"),
            ("Email address", " password ", " UI_LOCALES "),
        ):
            with self.subTest(name=name, password=password, locale=locale):
                callbacks = [
                    self.callback("NameCallback", name),
                    self.callback("NameCallback", locale),
                    self.callback("NameCallback", "devicePrint", "device-metadata"),
                    self.callback("NameCallback", "mail", "server@example.com"),
                    self.callback("PasswordCallback", password),
                ]
                callbacks[-1]["output"].insert(0, {"name": "echoOn", "value": False})
                self.response.json.side_effect = [{"callbacks": callbacks}, {"tokenId": "session"}]

                self.assertEqual("code", await patch_auth.authorize(self.auth, "owner", "secret"))

                self.assertEqual(
                    ["owner", "en-US", "device-metadata", "server@example.com", "secret"],
                    [cb["input"][0]["value"] for cb in self.sent[-1]["callbacks"]],
                )

    async def test_otp_prompts_still_wait_for_a_code(self):
        for prompt in ("One Time Password", " one time PASSWORD "):
            with self.subTest(prompt=prompt):
                self.sent.clear()
                challenge = {"callbacks": [self.callback("PasswordCallback", prompt)]}
                self.response.json.side_effect = [challenge, {"tokenId": "session"}]

                self.assertEqual(challenge, await patch_auth.authorize(self.auth, "owner", "secret"))
                self.assertEqual([{}], self.sent)
                self.assertEqual("code", await patch_auth.authorize(self.auth, "owner", "secret", "123456"))
                self.assertEqual("123456", self.sent[-1]["callbacks"][0]["input"][0]["value"])

    async def test_repeated_challenges_stop_even_when_auth_id_changes(self):
        for kind, prompt in (
            ("NameCallback", "Username"),
            ("NameCallback", "devicePrint"),
            ("PasswordCallback", "Enter your password"),
        ):
            with self.subTest(kind=kind, prompt=prompt):
                self.sent.clear()
                self.response.json.side_effect = [
                    {"authId": auth_id, "callbacks": [self.callback(kind, prompt)]}
                    for auth_id in ("first", "second")
                ]

                with self.assertRaises(LoginError):
                    await patch_auth.authorize(self.auth, "owner", "secret")

                self.assertEqual(2, len(self.sent))
                self.session.get.assert_not_called()

    async def test_password_login_continues_to_otp(self):
        otp_challenge = {"callbacks": [self.callback("PasswordCallback", "One Time Password")]}
        self.response.json.side_effect = [
            {"callbacks": [self.callback("NameCallback", "Email address")]},
            {"callbacks": [self.callback("PasswordCallback", "Enter your password")]},
            otp_challenge,
            {"tokenId": "session"},
        ]

        self.assertEqual(otp_challenge, await patch_auth.authorize(self.auth, "owner", "secret"))
        self.session.get.assert_not_called()
        self.assertEqual(3, len(self.sent))
        self.assertEqual("code", await patch_auth.authorize(self.auth, "owner", "secret", "123456"))
        self.assertEqual(
            ["owner", "secret", "123456"],
            [data["callbacks"][0]["input"][0]["value"] for data in self.sent[1:]],
        )


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
            "output": [{"name": "message", "value": " iNvAlId OtP "}],
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
                self.assertEqual(headers["Accept-Language"], "en-US")
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

    async def test_failed_and_unsupported_challenges_are_not_resubmitted(self):
        otp_callback = {
            "type": "PasswordCallback",
            "output": [{"name": "prompt", "value": "One Time Password"}],
            "input": [{"name": "IDToken1", "value": ""}],
        }
        for callbacks in (
            [{"type": "TextOutputCallback", "output": [{"name": "message", "value": "invalid otp"}]}],
            [{"type": "TextOutputCallback", "output": [{"name": "messageType", "value": 2}]}],
            [{"type": "UnknownCallback", "input": [{"value": ""}]}],
            [otp_callback],
        ):
            with self.subTest(callbacks=callbacks):
                response = AsyncMock(status=200)
                response.json.return_value = {"authId": "next", "callbacks": copy.deepcopy(callbacks)}
                response.__aenter__.return_value = response
                session = MagicMock()
                session.__aenter__.return_value = session
                session.post.return_value = response
                auth = types.SimpleNamespace(otp_callbacks={"authId": "first", "callbacks": [copy.deepcopy(otp_callback)]})
                with (
                    patch.object(patch_auth.aiohttp, "ClientSession", return_value=session),
                    patch.object(patch_auth._LOGGER, "error"),
                    self.assertRaises(LoginError),
                ):
                    await patch_auth.authorize(auth, "owner", "password", "123456")
                session.post.assert_called_once()

    async def test_login_method_choice_is_selected_by_name(self):
        challenge = {
            "authId": "method-auth-id",
            "callbacks": [{
                "type": "ChoiceCallback",
                "output": [
                    {"name": "prompt", "value": "Choose a login method"},
                    {"name": "choices", "value": ["Google", "Local", "Apple"]},
                ],
                "input": [{"name": "IDToken1", "value": 0}],
            }],
        }
        response = AsyncMock()
        response.status = 200
        response.json.side_effect = [challenge, {"tokenId": "session"}]
        response.__aenter__.return_value = response
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "com.toyota.oneapp:/oauth2Callback?code=code"}
        redirect.__aenter__.return_value = redirect
        sent = []
        session = MagicMock()
        session.__aenter__.return_value = session

        def post(url, *, json=None, headers=None, data=None):
            sent.append(copy.deepcopy(json))
            return response

        session.post.side_effect = post
        session.get.return_value = redirect
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            self.assertEqual(await patch_auth.authorize(auth, "owner", "password"), "code")

        self.assertEqual(sent[-1]["callbacks"][0]["input"][0]["value"], 1)

    async def test_login_method_choice_falls_back_to_first_password_option(self):
        challenge = {
            "authId": "fallback-auth-id",
            "callbacks": [{
                "type": "ChoiceCallback",
                "output": [
                    {"name": "prompt", "value": "Choose a login method"},
                    {"name": "choices", "value": ["Google", "Email"]},
                ],
                "input": [{"name": "IDToken1", "value": 0}],
            }],
        }
        response = AsyncMock()
        response.status = 200
        response.json.side_effect = [challenge, {"tokenId": "session"}]
        response.__aenter__.return_value = response
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "com.toyota.oneapp:/oauth2Callback?code=code"}
        redirect.__aenter__.return_value = redirect
        sent = []
        session = MagicMock()
        session.__aenter__.return_value = session

        def post(url, *, json=None, headers=None, data=None):
            sent.append(copy.deepcopy(json))
            return response

        session.post.side_effect = post
        session.get.return_value = redirect
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            self.assertEqual(await patch_auth.authorize(auth, "owner", "password"), "code")

        self.assertEqual(sent[-1]["callbacks"][0]["input"][0]["value"], 1)

    async def test_identity_provider_only_account_raises_sso_error(self):
        challenge = {
            "authId": "sso-auth-id",
            "callbacks": [{
                "type": "ChoiceCallback",
                "output": [{"name": "choices", "value": ["Google", "Apple"]}],
                "input": [{"name": "IDToken1", "value": 0}],
            }],
        }
        response = AsyncMock()
        response.status = 200
        response.json.return_value = challenge
        response.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.post.return_value = response
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            with self.assertLogs(patch_auth.__name__, level="ERROR") as logs:
                with self.assertRaises(patch_auth.SsoAccountError):
                    await patch_auth.authorize(auth, "owner", "password")

        session.post.assert_called_once()
        self.assertIn("Google", " ".join(logs.output))
        self.assertNotIn("owner", " ".join(logs.output))

    async def test_login_method_without_published_choices_keeps_default(self):
        challenge = {
            "authId": "legacy-auth-id",
            "callbacks": [{
                "type": "ChoiceCallback",
                "output": [{"name": "prompt", "value": "Choose a login method"}],
                "input": [{"name": "IDToken1", "value": 0}],
            }],
        }
        response = AsyncMock()
        response.status = 200
        response.json.side_effect = [challenge, {"tokenId": "session"}]
        response.__aenter__.return_value = response
        redirect = AsyncMock()
        redirect.status = 302
        redirect.headers = {"Location": "com.toyota.oneapp:/oauth2Callback?code=code"}
        redirect.__aenter__.return_value = redirect
        sent = []
        session = MagicMock()
        session.__aenter__.return_value = session

        def post(url, *, json=None, headers=None, data=None):
            sent.append(copy.deepcopy(json))
            return response

        session.post.side_effect = post
        session.get.return_value = redirect
        auth = types.SimpleNamespace(_extract_tokens=MagicMock())
        with patch.object(patch_auth.aiohttp, "ClientSession", return_value=session):
            self.assertEqual(await patch_auth.authorize(auth, "owner", "password"), "code")

        self.assertEqual(sent[-1]["callbacks"][0]["input"][0]["value"], 0)
