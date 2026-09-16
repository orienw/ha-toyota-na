import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from toyota_na import ToyotaOneClient
from toyota_na.exceptions import AuthError

from .const import DOMAIN, REFRESH_STATUS_INTERVAL
from .patch_auth import SsoAccountError
from .wake_policy import (
    CONF_WAKE_INTERVAL,
    WAKE_INTERVAL_OPTIONS,
    automatic_wake_interval,
)

_LOGGER = logging.getLogger(__name__)


class ToyotaNAConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Toyota (North America) connected services"""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Create the vehicle wake options flow."""
        return ToyotaNAOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        return await self._async_user_step(user_input, {})

    async def _async_user_step(self, user_input, errors):
        if user_input is not None:
            try:
                self.client = ToyotaOneClient()
                self.user_info = user_input
                authorization = await self.client.auth.authorize(
                    user_input["username"], user_input["password"]
                )
                if isinstance(authorization, dict):
                    return await self.async_step_otp()
                data = await self.async_get_entry_data(self.client, authorization)
                return await self.async_create_or_update_entry(data)
            except SsoAccountError:
                errors["base"] = "sso_account"
                _LOGGER.error("Toyota account requires identity provider sign-in")
            except AuthError:
                errors["base"] = "not_logged_in"
                _LOGGER.error("Not logged in with username and password")
            except Exception:
                errors["base"] = "unknown"
                _LOGGER.exception("Unknown error with username and password")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required("username"): str, vol.Required("password"): str}
            ),
            errors=errors,
        )

    async def async_step_otp(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                authorization = await self.client.auth.authorize(
                    self.user_info["username"],
                    self.user_info["password"],
                    user_input["code"],
                )
                data = await self.async_get_entry_data(self.client, authorization)
                return await self.async_create_or_update_entry(data)
            except SsoAccountError:
                _LOGGER.error("Toyota account requires identity provider sign-in")
                return await self._async_user_step(None, {"base": "sso_account"})
            except AuthError:
                errors["base"] = "otp_not_logged_in"
                _LOGGER.error("Not logged in with one time password")
            except Exception:
                errors["base"] = "unknown"
                _LOGGER.exception("Unknown error with one time password")
        return self.async_show_form(
            step_id="otp",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    async def async_get_entry_data(self, client, authorization_code):
        await client.auth.request_tokens(authorization_code)
        id_info = await client.auth.get_id_info()
        return {
            "tokens": client.auth.get_tokens(),
            "email": id_info["email"],
            "username": self.user_info["username"],
            "password": self.user_info["password"],
        }

    async def async_create_or_update_entry(self, data):
        existing_entry = await self.async_set_unique_id(f"{DOMAIN}:{data['email']}")
        if existing_entry:
            self.hass.config_entries.async_update_entry(
                existing_entry, data={**existing_entry.data, **data}
            )
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        return self.async_create_entry(title=data["email"], data=data)

    async def async_step_reauth(self, data):
        return await self.async_step_user()


class ToyotaNAOptionsFlow(config_entries.OptionsFlow):
    """Configure automatic vehicle wakes."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage vehicle wake options."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **user_input,
                    CONF_WAKE_INTERVAL: int(user_input[CONF_WAKE_INTERVAL]),
                },
            )

        current_interval = automatic_wake_interval(
            self._config_entry.options, REFRESH_STATUS_INTERVAL
        )
        interval_options = {
            str(interval): label for interval, label in WAKE_INTERVAL_OPTIONS.items()
        }
        interval_options.setdefault(str(current_interval), "Current default")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_WAKE_INTERVAL, default=str(current_interval)
                    ): vol.In(interval_options)
                }
            ),
        )
