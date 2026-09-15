import base64
import hashlib
import logging
import secrets
import aiohttp
from urllib.parse import urlparse, parse_qs, urlencode

from toyota_na import ToyotaOneAuth
from toyota_na.exceptions import LoginError

_LOGGER = logging.getLogger(__name__)


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
    async with aiohttp.ClientSession() as session:
        headers = {"Accept-API-Version": "resource=2.1, protocol=1.0"}

        data = {}
        otp_brake = False
        if otp is not None:    # Retrieve callbacks if we have the otp code
            data = self.otp_callbacks
            
        for _ in range(15):
            if "callbacks" in data:
                for cb in data["callbacks"]:
                    cb_type = cb["type"]
                    _LOGGER.debug("Callback: %s — %s", cb_type, cb["output"][0]["value"] if cb.get("output") else "")

                    if cb_type == "NameCallback":
                        prompt = cb["output"][0].get("value", "")
                        if prompt == "User Name":
                            cb["input"][0]["value"] = username
                        elif prompt == "ui_locales":
                            cb["input"][0]["value"] = "en-US"

                    elif cb_type == "PasswordCallback":
                        prompt = cb["output"][0].get("value", "")
                        if prompt == "One Time Password":
                            if otp is None:
                                otp_brake = True
                                break
                            cb["input"][0]["value"] = otp
                        elif prompt == "Password":
                            cb["input"][0]["value"] = password

                    elif cb_type == "ChoiceCallback":
                        # Login method: Local=0, Google=1, Facebook=2, Apple=3
                        cb["input"][0]["value"] = 0

                    elif cb_type == "ConfirmationCallback":
                        # Verify OTP=0, Resend OTP=1
                        cb["input"][0]["value"] = 0

                    elif cb_type == "HiddenValueCallback":
                        pass  # devicePrint etc — pass through unchanged

            if otp_brake:
                self.otp_callbacks = data # Store callback to restart auth loop when we have the otp
                _LOGGER.debug("Fetching otp...")
                return data
        
            async with session.post(f"{ToyotaOneAuth.AUTHENTICATE_URL}", json=data, headers=headers) as resp:
                if resp.status != 200:
                    _LOGGER.info(await resp.text())
                    raise LoginError()
                data = await resp.json()
                if any(
                    cb["type"] == "TextOutputCallback"
                    and any(output.get("value") == "Invalid OTP" for output in cb.get("output", []))
                    for cb in data.get("callbacks", [])
                ):
                    self.otp_callbacks = data
                    _LOGGER.error("Invalid OTP")
                    raise LoginError()
                if "tokenId" in data:
                    break

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
            if resp.status != 302:
                _LOGGER.error(await resp.text())
                raise LoginError()
            redir = resp.headers["Location"]
            query = parse_qs(urlparse(redir).query)
            if "code" not in query:
                _LOGGER.error(redir)
                raise LoginError()
            return query["code"][0]
            
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
    async with aiohttp.ClientSession() as session:
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
    async with aiohttp.ClientSession() as session:
        async with session.post(ToyotaOneAuth.ACCESS_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                raise LoginError()
            self._extract_tokens(await resp.json())


async def login(self, username, password, otp=None):
    authorization_code = await self.authorize(username, password, otp)
    if isinstance(authorization_code, dict):
        return authorization_code
    await self.request_tokens(authorization_code)
