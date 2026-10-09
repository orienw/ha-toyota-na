"""Translate expected Toyota failures at Home Assistant service boundaries."""

import json
from contextlib import contextmanager

from aiohttp import ClientError, ClientResponseError
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from toyota_na.exceptions import AuthError


@contextmanager
def translate_service_errors():
    try:
        yield
    except json.JSONDecodeError as err:
        raise HomeAssistantError("Toyota returned an invalid response.") from err
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    except TimeoutError as err:
        raise HomeAssistantError(str(err) or "The Toyota request timed out.") from err
    except AuthError as err:
        raise HomeAssistantError(
            str(err) or "Toyota authentication failed. Sign in again."
        ) from err
    except ClientResponseError as err:
        raise HomeAssistantError(err.message or "The Toyota request failed. Try again.") from err
    except (ClientError, RuntimeError) as err:
        raise HomeAssistantError(str(err) or "The Toyota request failed. Try again.") from err
