"""Translate expected Toyota failures at Home Assistant service boundaries."""

import asyncio
from contextlib import contextmanager
import json

from aiohttp import ClientError
from toyota_na.exceptions import AuthError

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError


@contextmanager
def translate_service_errors():
    try:
        yield
    except json.JSONDecodeError as err:
        raise HomeAssistantError("Toyota returned an invalid response.") from err
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    except asyncio.TimeoutError as err:
        raise HomeAssistantError(str(err) or "The Toyota request timed out.") from err
    except AuthError as err:
        raise HomeAssistantError(str(err) or "Toyota authentication failed. Sign in again.") from err
    except (ClientError, RuntimeError) as err:
        raise HomeAssistantError(str(err) or "The Toyota request failed. Try again.") from err
