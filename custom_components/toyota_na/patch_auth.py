import asyncio
import base64
import hashlib
import logging
import math
import secrets
import time
from copy import deepcopy
import aiohttp
import jwt
from urllib.parse import urlparse, parse_qs, urlencode

from toyota_na import ToyotaOneAuth
from toyota_na.exceptions import LoginError, NotLoggedIn, TokenExpired

from .patch_client import HTTP_TIMEOUT

_LOGGER = logging.getLogger(__name__)
_get_tokens = ToyotaOneAuth.get_tokens
_set_tokens = ToyotaOneAuth.set_tokens

SSO_PROVIDER_CHOICES = frozenset({"google", "facebook", "apple"})


class SsoAccountError(LoginError):
    """Raised when a Toyota account cannot sign in with a password."""


async def check_tokens(self, *, rejected_token=None):
    """Serialize refreshes and cap expiry margins at 10% of token lifetime."""
    lock = getattr(self, "_token_lock", None)
    if lock is None:
        lock = self._token_lock = asyncio.Lock()
    async with lock:
        if self._expires_at is None:
            raise NotLoggedIn()
        now = time.time()
        if (
            now >= self._expires_at
            or (rejected_token is not None and self._access_token == rejected_token)
            or self._refresh_secs == 0
            or (self._refresh_secs > 0 and now >= self._updated_at + self._refresh_secs)
            or (
                self._refresh_secs < 0
                and now >= self._expires_at - min(
                    -self._refresh_secs, (self._expires_at - self._updated_at) / 10,
                )
            )
        ):
            try:
                await self.refresh_tokens()
            except LoginError as err:
                raise TokenExpired() from err


def logged_in(self):
    return self._expires_at is not None and time.time() < self._expires_at


def get_tokens(self):
    return {**_get_tokens(self), "clock": "unix"}


def set_tokens(self, tokens):
    _set_tokens(self, tokens)
    if tokens.get("clock") != "unix":
        # Refresh once instead of guessing which local timezone produced the
        # old SDK's naive UTC timestamps.
        self._expires_at = self._updated_at = 0


def extract_tokens(self, response):
    """Replace tokens atomically, retaining fields omitted during refresh."""
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token") or self._refresh_token
    id_token = response.get("id_token") or self._id_token
    try:
        lifetime = float(response["expires_in"])
    except (KeyError, TypeError, ValueError) as err:
        raise RuntimeError("Toyota returned an invalid token lifetime.") from err
    if (
        not all(isinstance(token, str) and token for token in (access_token, refresh_token, id_token))
        or not math.isfinite(lifetime) or lifetime <= 0
    ):
        raise RuntimeError("Toyota returned an incomplete token response.")
    guid = jwt.decode(
        id_token, algorithms=["RS256"], options={"verify_signature": False},
        audience="oneappsdkclient",
    )["sub"]
    now = time.time()
    self._access_token = access_token
    self._refresh_token = refresh_token
    self._id_token = id_token
    self._guid = guid
    self._expires_at = now + lifetime
    self._updated_at = now
    if self._callback:
        try:
            self._callback(self.get_tokens())
        except Exception:
            _LOGGER.exception("Token persistence callback failed")


def _choice_callback_choices(callback):
    """Return the login methods offered by a ForgeRock ChoiceCallback."""
    for output in callback.get("output", []):
        if output.get("name") == "choices" and isinstance(output.get("value"), list):
            return output["value"]
    return None


async def authorize(self, username, password, otp=None):
    """
    Toyota ForgeRock auth flow.

    Handles both the legacy password-based flow and the newer passwordless
    OTP-only flow. The callback sequence varies, but this handles all known
    callback types:
      - NameCallback (username, ui_locales)
      - PasswordCallback (password, OTP)
      - HiddenValueCallback (devicePrint — pass through)
      - ChoiceCallback (login method selection)
      - ConfirmationCallback (OTP verify/resend)
      - TextOutputCallback (error messages)

    When otp=None and OTP is requested, saves callbacks and returns (caller
    should re-call with otp set).
    """
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        headers = {
            "Accept-API-Version": "resource=2.1, protocol=1.0",
            "Accept-Language": "en-US",
        }

        data = {}
        otp_brake = False
        otp_submitted = False
        password_submitted = False
        previous_callbacks = None
        if otp is not None:    # Retrieve callbacks if we have the otp code
            data = self.otp_callbacks
            
        for _ in range(15):
            if "callbacks" in data:
                for cb in data["callbacks"]:
                    cb_type = cb["type"]
                    _LOGGER.debug("Toyota authentication callback: %s", cb_type)
                    if cb_type in ("NameCallback", "PasswordCallback"):
                        prompt = next((
                            str(output.get("value", "")).strip().casefold()
                            for output in cb.get("output", [])
                            if output.get("name") == "prompt"
                        ), "")

                    if cb_type == "NameCallback":
                        if prompt in ("ui_locales", "ui locale"):
                            cb["input"][0]["value"] = "en-US"
                        elif prompt not in ("deviceprint", "mail"):
                            cb["input"][0]["value"] = username

                    elif cb_type == "PasswordCallback":
                        if prompt == "one time password":
                            if otp is None:
                                otp_brake = True
                                break
                            if otp_submitted:
                                self.otp_callbacks = data
                                raise LoginError("Toyota requested another one-time password.")
                            cb["input"][0]["value"] = otp
                            otp_submitted = True
                        else:
                            if password_submitted:
                                raise LoginError("Toyota did not accept the password.")
                            cb["input"][0]["value"] = password
                            password_submitted = True

                    elif cb_type == "ChoiceCallback":
                        # Local is the password option; its position is not fixed.
                        choices = _choice_callback_choices(cb) or []
                        labels = [str(choice).strip().lower() for choice in choices]
                        local = [i for i, label in enumerate(labels) if label == "local"]
                        other = [
                            i for i, label in enumerate(labels)
                            if label not in SSO_PROVIDER_CHOICES
                        ]
                        if labels and not other:
                            _LOGGER.error(
                                "Toyota account does not offer password sign-in, only: %s",
                                ", ".join(map(str, choices)),
                            )
                            raise SsoAccountError()
                        cb["input"][0]["value"] = (local or other or [0])[0]

                    elif cb_type == "ConfirmationCallback":
                        # Verify OTP=0, Resend OTP=1
                        cb["input"][0]["value"] = 0

                    elif cb_type in ("HiddenValueCallback", "TextOutputCallback"):
                        pass  # devicePrint etc — pass through unchanged
                    else:
                        _LOGGER.error("Unsupported Toyota authentication callback: %s", cb_type)
                        raise LoginError("Unsupported authentication challenge.")

            if otp_brake:
                self.otp_callbacks = data # Store callback to restart auth loop when we have the otp
                _LOGGER.debug("Fetching otp...")
                return data

            if "callbacks" in data:
                if data["callbacks"] == previous_callbacks:
                    raise LoginError("Toyota repeated an authentication challenge.")
                previous_callbacks = deepcopy(data["callbacks"])

            async with session.post(f"{ToyotaOneAuth.AUTHENTICATE_URL}", json=data, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.info("Toyota authentication failed with HTTP %s", resp.status)
                    raise LoginError()
                data = await resp.json()
                if any(
                    cb["type"] == "TextOutputCallback"
                    and any(
                        (output.get("name") == "messageType" and output.get("value") == 2)
                        or str(output.get("value", "")).strip().casefold() == "invalid otp"
                        for output in cb.get("output", [])
                    )
                    for cb in data.get("callbacks", [])
                ):
                    self.otp_callbacks = data
                    _LOGGER.error("Toyota rejected the authentication challenge")
                    raise LoginError()
                if "tokenId" in data:
                    break
                if not any(cb.get("input") for cb in data.get("callbacks", [])):
                    raise LoginError("Toyota returned no actionable authentication challenge.")

        if "tokenId" not in data:
            _LOGGER.error("Toyota login did not complete its authentication challenge")
            raise LoginError()
        headers["Cookie"] = f"iPlanetDirectoryPro={data['tokenId']}"
        self._code_verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self._code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        auth_params = {
            "client_id": "oneappsdkclient",
            "scope": "openid profile write",
            "response_type": "code",
            "redirect_uri": "com.toyota.oneapp:/oauth2Callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        AUTHORIZE_URL_QS = f"{ToyotaOneAuth.AUTHORIZE_URL}?{urlencode(auth_params)}"
        async with session.get(AUTHORIZE_URL_QS, headers=headers, allow_redirects=False) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                _LOGGER.error("Toyota authentication request failed with HTTP %s", resp.status)
                raise LoginError()
            try:
                redirect = urlparse(resp.headers.get("Location", ""))
            except ValueError as err:
                raise LoginError("Toyota returned an invalid authentication redirect.") from err
            if (
                redirect._replace(query="", fragment="").geturl() != auth_params["redirect_uri"]
                or redirect.fragment
            ):
                raise LoginError("Toyota returned an unexpected authentication redirect.")
            query = parse_qs(redirect.query, keep_blank_values=True)
            codes = query.get("code", [])
            if "error" in query or len(codes) != 1 or not codes[0].strip():
                raise LoginError("Toyota authentication redirect did not contain a valid code.")
            return codes[0]
            
async def request_tokens(self, code):
    verifier = getattr(self, "_code_verifier", None)
    if not verifier:
        raise LoginError("Start a new login before exchanging an authorization code.")
    data = {
        "client_id": "oneappsdkclient",
        "redirect_uri": "com.toyota.oneapp:/oauth2Callback",
        "grant_type": "authorization_code",
        "code_verifier": verifier,
        "code": code,
    }
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.post(ToyotaOneAuth.ACCESS_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                raise LoginError()
            self._extract_tokens(await resp.json())
            self._code_verifier = None


async def refresh_tokens(self):
    data = {
        "client_id": "oneappsdkclient",
        "grant_type": "refresh_token",
        "refresh_token": self._refresh_token,
    }
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.post(ToyotaOneAuth.ACCESS_TOKEN_URL, data=data) as resp:
            if resp.status in (400, 401):
                try:
                    error = await resp.json(content_type=None)
                except ValueError:
                    error = None
                if isinstance(error, dict) and error.get("error") == "invalid_grant":
                    raise LoginError()
            resp.raise_for_status()
            self._extract_tokens(await resp.json())
