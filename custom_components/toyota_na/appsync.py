"""Toyota's AppSync API: its GraphQL documents, realtime protocol and command callbacks."""

import asyncio
import base64
import json
import uuid
from contextlib import AsyncExitStack
from urllib.parse import urlencode

import aiohttp

from .session import TRANSPORT_BRAND, request_session

GRAPHQL_ENDPOINT = "https://oa-api.telematicsct.com/graphql"
GRAPHQL_WS_ENDPOINT = "wss://oa-api.telematicsct.com/graphql/realtime"
GRAPHQL_HOST = "oa-api.telematicsct.com"
APPSYNC_API_KEY = "da2-zgeayo2qh5eo7cj6pmdwhwugze"


# --- GraphQL Operations ---

GRAPHQL_PRE_WAKE = """mutation SendPreWakeCommand($guid: String!) {
  postPreWake(guid: $guid) {
    timestamp
    status { messages { responseCode } }
  }
}"""

GRAPHQL_CONFIRM_SUBSCRIPTION = """mutation ConfirmSubscriptionStatus($vin: String!, $backdoorType: String!) {
  confirmSubscriptionActive(vin: $vin, payload: {
    vehicleCapabilities: { backdoorType: $backdoorType }
  }) { vin }
}"""

GRAPHQL_REFRESH_STATUS = """mutation RefreshVehicleStatus($vin: String!) {
  postRefreshStatus(vin: $vin) {
    payload { correlationId appRequestNo }
    status { messages { responseCode description } }
    timestamp
  }
}"""

GRAPHQL_VEHICLE_STATUS_FIELDS = """
    vin lastUpdateDateTime
    vehicleState {
      lastUpdateDateTime driverPosition
      doors {
        driverSide { lock { status } position { status } }
        passengerSide { lock { status } position { status } }
        rearDriverSide { lock { status } position { status } }
        rearPassengerSide { lock { status } position { status } }
      }
      windows {
        driverSide { position { status } }
        passengerSide { position { status } }
        rearDriverSide { position { status } }
        rearPassengerSide { position { status } }
      }
      hatch { lock { status } position { status } }
      hood { position { status } }
      glassHatch { position { status } }
      moonroof { position { status } }
      trunk { lock { status } position { status } }
      tailgate { lock { status } position { status } }
      tires {
        frontLeft { psi kpa bar displayLowTirePressureWarning }
        frontRight { psi kpa bar displayLowTirePressureWarning }
        rearLeft { psi kpa bar displayLowTirePressureWarning }
        rearRight { psi kpa bar displayLowTirePressureWarning }
        spare { psi kpa bar displayLowTirePressureWarning }
        lastUpdateDateTime
      }
      engine { running lastUpdateDateTime status startTime stopTime lastUpdateBy }
    }
    tripdetails {
      lastUpdateDateTime
      tripA { value unit }
      tripB { value unit }
      tripCount { value unit }
    }
    location { latitude longitude lastUpdateDateTime }
    telemetry {
      lastUpdateDateTime
      odo { unit value }
      fugage { unit value }
      range { unit value }
      totalAverageFuelConsumption { unit value }
      averageFuelConsumptionSinceStart { unit value }
    }
    electric {
      lastUpdateDateTime
      battery {
        chargeRemainingAmount { unit value }
        powerSupplyPossibleTime { unit value }
        travelableDistance { unit value }
        travelableDistanceAC { unit value }
        plugInEnergy { unit value }
        stateOfChargeDisplay { unit value }
      }
      charging {
        chargeType chargingStatus chargingState
        remainingChargeTime { unit value }
        remainingChargeTimeTo80Percent { unit value }
        actualChargingRate { unit value }
        connector { status plugInInfo plugStatus }
        chargeSettings {
          schedules {
            enabled settingId startTime endTime daysOfTheWeek status nextChargeSettingId
          }
          targetLimit { value unit }
          maxACCurrent { value setting }
          maxDCPower { value setting }
          electricSupplyModeLimit { value setting }
          electricSupplyLimitFunction
          acCurrentSelections { key enabled }
          dcPowerSelections { key enabled }
          electricSupplyLimitSelections { key enabled }
          lastUpdateDateTime
        }
        limitSelectionValues
        lastUpdateDateTime
      }
      gasoline {
        powerSupplyPossibleTime { unit value }
        travelableDistance { unit value }
      }
    }
"""

GRAPHQL_GET_VEHICLE_STATUS = (
    "query GetVehicleStatus($vin: String!) { getVehicleStatus(vin: $vin) {"
    + GRAPHQL_VEHICLE_STATUS_FIELDS
    + "} }"
)

# Keep the pre-2.9 selection available when an endpoint rejects newer fields.
GRAPHQL_BASIC_VEHICLE_STATUS_FIELDS = """
    vin lastUpdateDateTime
    vehicleState {
      lastUpdateDateTime driverPosition
      doors {
        driverSide { lock { status } position { status } }
        passengerSide { lock { status } position { status } }
        rearDriverSide { lock { status } position { status } }
        rearPassengerSide { lock { status } position { status } }
      }
      windows {
        driverSide { position { status } }
        passengerSide { position { status } }
        rearDriverSide { position { status } }
        rearPassengerSide { position { status } }
      }
      hatch { lock { status } position { status } }
      hood { position { status } }
      moonroof { position { status } }
      trunk { lock { status } position { status } }
      tailgate { lock { status } position { status } }
      tires {
        frontLeft { psi kpa bar displayLowTirePressureWarning }
        frontRight { psi kpa bar displayLowTirePressureWarning }
        rearLeft { psi kpa bar displayLowTirePressureWarning }
        rearRight { psi kpa bar displayLowTirePressureWarning }
        spare { psi kpa bar displayLowTirePressureWarning }
        lastUpdateDateTime
      }
      engine { running lastUpdateDateTime status }
    }
    tripdetails {
      lastUpdateDateTime
      tripA { value unit }
      tripB { value unit }
      tripCount { value unit }
    }
    location { latitude longitude lastUpdateDateTime }
    telemetry {
      lastUpdateDateTime
      odo { unit value }
      fugage { unit value }
      range { unit value }
      totalAverageFuelConsumption { unit value }
      averageFuelConsumptionSinceStart { unit value }
    }
    electric {
      lastUpdateDateTime
      battery {
        chargeRemainingAmount { unit value }
        powerSupplyPossibleTime { unit value }
        travelableDistance { unit value }
        travelableDistanceAC { unit value }
        plugInEnergy { unit value }
        stateOfChargeDisplay { unit value }
      }
      charging {
        chargeType chargingStatus chargingState
        remainingChargeTime { unit value }
        remainingChargeTimeTo80Percent { unit value }
        connector { status plugInInfo plugStatus }
        chargeSettings {
          targetLimit { value unit }
          maxACCurrent { value setting }
          maxDCPower { value setting }
          lastUpdateDateTime
        }
        lastUpdateDateTime
      }
      gasoline {
        powerSupplyPossibleTime { unit value }
        travelableDistance { unit value }
      }
    }
"""

GRAPHQL_GET_BASIC_VEHICLE_STATUS = (
    "query GetVehicleStatus($vin: String!) { getVehicleStatus(vin: $vin) {"
    + GRAPHQL_BASIC_VEHICLE_STATUS_FIELDS
    + "} }"
)

_OPTIONAL_STATUS_PATHS = (
    ("vehicleState", "glassHatch"),
    ("vehicleState", "engine", "startTime"),
    ("vehicleState", "engine", "stopTime"),
    ("vehicleState", "engine", "lastUpdateBy"),
    ("electric", "charging", "actualChargingRate"),
    ("electric", "charging", "limitSelectionValues"),
    ("electric", "charging", "chargeSettings", "schedules"),
    ("electric", "charging", "chargeSettings", "electricSupplyModeLimit"),
    ("electric", "charging", "chargeSettings", "electricSupplyLimitFunction"),
    ("electric", "charging", "chargeSettings", "acCurrentSelections"),
    ("electric", "charging", "chargeSettings", "dcPowerSelections"),
    ("electric", "charging", "chargeSettings", "electricSupplyLimitSelections"),
)

GRAPHQL_REMOTE_COMMAND_STATUS = """subscription ReceiveRemoteCommandStatus($vin: String!) {
  onPostRemoteCallback(vin: $vin) {
    appRequestNo type category remoteCommandType message status vin command commandEnded
  }
}"""

GRAPHQL_SEND_REMOTE_COMMAND = """mutation SendRemoteCommand($command: String!, $autoFixCommands: [String]!) {
  executeRemoteCommand(commandInputBody: {
    command: $command
    autofixCommands: $autoFixCommands
  }) {
    payload { requestNo correlationId returnCode }
    status { messages { responseCode description detailedDescription } }
  }
}"""

GRAPHQL_CHARGE_SETTINGS = """mutation PostChargeSettings(
  $chargingTargetLimit: Int, $quickChargePowerLimit: Int, $currentCharge: Int
) {
  postChargeSettings(postChargeSettingsInputBody: {
    chargingTargetLimit: $chargingTargetLimit
    quickChargePowerLimit: $quickChargePowerLimit
    currentCharge: $currentCharge
  }) {
    payload { correlationId returnCode message }
    status { messages { responseCode description detailedDescription } }
  }
}"""

GRAPHQL_POWER_SUPPLY_LIMIT = """mutation PostPowerSupplyModeLimit(
  $command: String!, $minimumElectricSupply: String
) {
  executeRemoteCommand(commandInputBody: {
    command: $command minimumElectricSupply: $minimumElectricSupply
  }) {
    payload { requestNo correlationId returnCode }
    status { messages { responseCode description detailedDescription } }
  }
}"""


def appsync_authorization(token, guid, vin="", region="US", device_id=None):
    """Build the per-vehicle authorization used by AppSync WebSockets."""
    authorization = {
        "host": GRAPHQL_HOST,
        "x-api-key": APPSYNC_API_KEY,
        "Authorization": "Bearer " + token,
        "x-channel": "ONEAPP",
        "X-BRAND": TRANSPORT_BRAND,
        "X-APPBRAND": TRANSPORT_BRAND,
        "x-region": region,
        "vin": vin,
        "x-guid": guid,
    }
    if device_id:
        authorization["x-deviceid"] = device_id
    return authorization


def appsync_ws_url(authorization):
    """Build the AppSync WebSocket URL with percent-encoded base64 parameters."""
    query = urlencode(
        {
            "header": base64.b64encode(json.dumps(authorization).encode()).decode(),
            "payload": base64.b64encode(b"{}").decode(),
        }
    )
    return f"{GRAPHQL_WS_ENDPOINT}?{query}"


def appsync_subscription_message(subscription_id, query, variables, authorization):
    """Build an AppSync graphql-ws start frame."""
    return {
        "id": subscription_id,
        "type": "start",
        "payload": {
            "data": json.dumps({"query": query, "variables": variables}),
            "extensions": {"authorization": authorization},
        },
    }


def graphql_schema_errors(errors):
    """Recognize rejected query fields separately from auth and resolver errors."""
    # AppSync reports schema errors in the message; a resolver can use a
    # ValidationError type for bad input, which must not drop fields.
    return isinstance(errors, list) and any(
        (
            isinstance(err.get("extensions"), dict)
            and err["extensions"].get("code") == "GRAPHQL_VALIDATION_FAILED"
        )
        or str(err.get("message", ""))
        .lower()
        .startswith(("validation error", "cannot query field"))
        for err in errors
        if isinstance(err, dict)
    )


def _token_rejected(errors):
    """Recognize the errors Toyota's app retries as a rejected sign-in."""
    return any(
        err.get("errorType") in ("APIGW-403", "APPSYNC-AUTH-403")
        or (
            isinstance(err.get("extensions"), dict)
            and err["extensions"].get("code") in ("APIGW-403", "APPSYNC-AUTH-403")
        )
        or str(err.get("message", "")).lower() == "unauthorized"
        or "jwt expired" in str(err.get("message", "")).lower()
        for err in errors
        if isinstance(err, dict)
    )


class _TokenRejected(RuntimeError):
    """AppSync refused a socket's access token before its operation was sent."""


def _failed_status_sections(data, errors):
    """Find sections lost to errors in fields omitted by the basic selection."""
    status = (data or {}).get("getVehicleStatus")
    sections = set()
    for err in errors:
        path = err.get("path")
        if not isinstance(path, list) or len(path) < 3 or path[0] != "getVehicleStatus":
            continue
        if any(
            tuple(path[1 : len(optional) + 1]) == optional for optional in _OPTIONAL_STATUS_PATHS
        ):
            section = status.get(path[1]) if isinstance(status, dict) else None
            if status is None or (
                isinstance(status, dict)
                and (
                    section is None
                    or (
                        path[1] == "electric"
                        and isinstance(section, dict)
                        and section.get("charging") is None
                    )
                )
            ):
                sections.add(path[1])
    return sections


def _merge_status_fallback(original_data, fallback_data, sections):
    """Recover missing sections without replacing valid first-response data."""
    if not isinstance(fallback_data, dict):
        return original_data
    original = (original_data or {}).get("getVehicleStatus")
    if not isinstance(original, dict):
        return fallback_data or original_data
    fallback = fallback_data.get("getVehicleStatus")
    if not isinstance(fallback, dict):
        return original_data
    recovered = {key: fallback[key] for key in sections if isinstance(fallback.get(key), dict)}
    electric = original.get("electric")
    mixed_electric = None
    if "electric" in recovered and isinstance(electric, dict):
        fallback_electric = recovered["electric"]
        charging = fallback_electric.get("charging")
        if not isinstance(charging, dict):
            recovered.pop("electric")
        else:
            mixed_electric = {
                **electric,
                "lastUpdateDateTime": None,
                "charging": {
                    **charging,
                    "lastUpdateDateTime": charging.get("lastUpdateDateTime")
                    or fallback_electric.get("lastUpdateDateTime")
                    or fallback.get("lastUpdateDateTime"),
                },
            }
            # Battery/range readings still belong to the first response.
            for key in ("battery", "gasoline"):
                value = electric.get(key)
                if isinstance(value, dict):
                    mixed_electric[key] = {
                        **value,
                        "lastUpdateDateTime": value.get("lastUpdateDateTime")
                        or electric.get("lastUpdateDateTime")
                        or original.get("lastUpdateDateTime"),
                    }
    if not recovered:
        return original_data
    status = {**original, **recovered}
    if original.get("lastUpdateDateTime") or fallback.get("lastUpdateDateTime"):
        # A combined document has no shared timestamp: retain each section's source.
        for key, value in status.items():
            if isinstance(value, dict) and not value.get("lastUpdateDateTime"):
                source = fallback if key in recovered else original
                status[key] = {**value, "lastUpdateDateTime": source.get("lastUpdateDateTime")}
        status["lastUpdateDateTime"] = None
    if mixed_electric is not None:
        status["electric"] = mixed_electric
    return {**original_data, "getVehicleStatus": status}


def _require_remote_execution(execution):
    correlation_id = ((execution or {}).get("payload") or {}).get("correlationId")
    if correlation_id:
        return execution

    messages = ((execution or {}).get("status") or {}).get("messages") or []
    message = messages[0] if messages else {}
    detail = (
        message.get("detailedDescription")
        or message.get("description")
        or "Toyota did not return a correlation ID for the remote command."
    )
    code = message.get("responseCode")
    if code:
        detail = f"{detail} [{code}]"
    raise RuntimeError(detail)


def _remote_socket_error(message):
    payload = message.get("payload") or {}
    errors = payload.get("errors") if isinstance(payload, dict) else None
    error = errors[0] if errors else payload
    if isinstance(error, dict):
        return (
            error.get("message")
            or error.get("error")
            or "Toyota rejected the AppSync remote-command subscription."
        )
    return "Toyota rejected the AppSync remote-command subscription."


async def _receive_remote_socket_message(ws, timeout):
    message = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if message.type == aiohttp.WSMsgType.TEXT:
        return json.loads(message.data)
    if message.type in (
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.ERROR,
    ):
        raise RuntimeError("Toyota closed the AppSync remote-command connection.")
    return {}


async def _wait_for_remote_socket_event(ws, expected_type, subscription_id=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while loop.time() < deadline:
        message = await _receive_remote_socket_message(ws, max(1, deadline - loop.time()))
        message_type = message.get("type")
        if message_type == "ka":
            continue
        if message_type in ("connection_error", "error"):
            payload = message.get("payload")
            errors = payload.get("errors") if isinstance(payload, dict) else None
            error = _TokenRejected if _token_rejected(errors or []) else RuntimeError
            raise error(_remote_socket_error(message))
        if message_type == expected_type and (
            subscription_id is None or message.get("id") == subscription_id
        ):
            return
    raise RuntimeError("Toyota's AppSync remote-command connection timed out.")


async def _wait_for_remote_command_result(
    ws, vin, subscription_id, request_no=None, *, fail_on_unknown=False
):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        message = await _receive_remote_socket_message(ws, max(1, deadline - loop.time()))
        message_type = message.get("type")
        if message_type == "ka":
            continue
        if message_type in ("connection_error", "error"):
            raise RuntimeError(_remote_socket_error(message))
        if message_type != "data" or message.get("id") != subscription_id:
            continue

        callback = ((message.get("payload") or {}).get("data") or {}).get(
            "onPostRemoteCallback"
        ) or {}
        if callback.get("vin") != vin:
            continue
        callback_request_no = callback.get("appRequestNo")
        # Request numbers are optional; Toyota's app matches callbacks by VIN.
        if (
            request_no is not None
            and callback_request_no is not None
            and str(callback_request_no) != str(request_no)
        ):
            continue
        status = str(callback.get("status") or "unknown").lower()
        detail = callback.get("message")
        if status == "completed":
            return callback
        if status == "in_progress":
            continue
        if (
            fail_on_unknown
            or status in ("error", "timeout")
            or callback.get("commandEnded") is True
        ):
            raise RuntimeError(detail or f"Toyota ended the remote command with status {status}.")
    raise RuntimeError(
        "Toyota accepted the command but did not report completion within 60 seconds."
    )


async def _run_appsync_operation(self, vin, submit, region, *, fail_on_unknown=False):
    # Callbacks can omit request numbers, so serialize this account's
    # operations for each vehicle.
    if not hasattr(self, "_remote_locks"):
        self._remote_locks = {}
    lock = self._remote_locks.setdefault(vin, asyncio.Lock())
    async with lock:
        return await _execute_appsync_operation(
            self,
            vin,
            submit,
            region,
            fail_on_unknown=fail_on_unknown,
        )


async def _execute_appsync_operation(self, vin, submit, region, *, fail_on_unknown=False):
    token = await self.auth.get_access_token()
    try:
        return await _appsync_operation(self, token, vin, submit, region, fail_on_unknown)
    except _TokenRejected:
        # Nothing has been sent yet, so try once more with a refreshed token.
        await self.auth.check_tokens(rejected_token=token)
        token = await self.auth.get_access_token()
        return await _appsync_operation(self, token, vin, submit, region, fail_on_unknown)


async def _appsync_operation(self, token, vin, submit, region, fail_on_unknown):
    guid = await self.auth.get_guid()
    authorization = appsync_authorization(
        token,
        guid,
        vin,
        region,
        self.auth.get_device_id(),
    )
    websocket_url = appsync_ws_url(authorization)

    async with request_session(getattr(self, "_session", None)) as (session, _shared):
        async with AsyncExitStack() as stack:
            try:
                ws = await stack.enter_async_context(
                    session.ws_connect(websocket_url, protocols=["graphql-ws"], heartbeat=30)
                )
            except TimeoutError:
                raise RuntimeError("The Toyota connection timed out. Try again.") from None
            except aiohttp.ClientError as err:
                status = err.status if isinstance(err, aiohttp.ClientResponseError) else None
                error = _TokenRejected if status == 401 else RuntimeError
                detail = f" (HTTP {status})" if status is not None else ""
                raise error(f"Could not connect to Toyota{detail}. Try again.") from None
            await ws.send_json({"type": "connection_init"})
            await _wait_for_remote_socket_event(ws, "connection_ack")

            subscription_id = str(uuid.uuid4())
            await ws.send_json(
                appsync_subscription_message(
                    subscription_id,
                    GRAPHQL_REMOTE_COMMAND_STATUS,
                    {"vin": vin},
                    authorization,
                )
            )
            await _wait_for_remote_socket_event(ws, "start_ack", subscription_id)
            execution = await submit()
            request_no = ((execution or {}).get("payload") or {}).get("requestNo")
            try:
                return await _wait_for_remote_command_result(
                    ws,
                    vin,
                    subscription_id,
                    request_no,
                    fail_on_unknown=fail_on_unknown,
                )
            except TimeoutError as err:
                raise RuntimeError(
                    "Toyota accepted the command but did not report completion within 60 seconds."
                ) from err
