import asyncio
import base64
import json
import logging
import uuid
from urllib.parse import urlencode, urljoin

import aiohttp
from toyota_na.exceptions import AuthError, TokenExpired

API_GATEWAY = "https://onecdn.telematicsct.com/oneapi/"
REMOTE_ROUTE = "https://onecdn.telematicsct.com/v1/remote/route/"
GRAPHQL_ENDPOINT = "https://oa-api.telematicsct.com/graphql"
GRAPHQL_WS_ENDPOINT = "wss://oa-api.telematicsct.com/graphql/realtime"
GRAPHQL_HOST = "oa-api.telematicsct.com"
APPSYNC_API_KEY = "da2-zgeayo2qh5eo7cj6pmdwhwugze"
RESOLVER_API_KEY = "pypIHG015k4ABHWbcI4G0a94F7cC0JDo1OynpAsG"
USER_AGENT = "ToyotaOneApp/3.10.0 (com.toyota.oneapp; build:3100; Android 14) okhttp/4.12.0"
TRANSPORT_BRAND = "T"
ELECTRIC_COMMAND_TIMEOUT = 90
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=30)

_LOGGER = logging.getLogger(__name__)


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


def _vehicle_headers(vehicle_vin, region="US", **extra):
    """Build headers for Toyota's shared North American API transport."""
    return {
        "VIN": vehicle_vin,
        "X-BRAND": TRANSPORT_BRAND,
        "x-region": region,
        **extra,
    }


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


async def get_telemetry(self, vin, region="US", generation="17CYPLUS"):
    try:
        return await self.api_get(
            "v2/telemetry",
            _vehicle_headers(vin, region, GENERATION=generation),
        )
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("v2/telemetry failed: %s", e)
        return None

async def get_tire_pressure(self, vin, generation, region="US", brand="T"):
    return await self.api_get(
        "v1/telemetry/tires/pressure",
        _vehicle_headers(vin, region, GENERATION=generation, **{"X-BRAND": brand}),
    )


async def _auth_headers(self):
    return {
        "AUTHORIZATION": "Bearer " + await self.auth.get_access_token(),
        "X-API-KEY": RESOLVER_API_KEY,
        "X-GUID": await self.auth.get_guid(),
        "X-CHANNEL": "ONEAPP",
        "X-BRAND": TRANSPORT_BRAND,
        "x-region": "US",
        "X-APPVERSION": "3.4.0",
        "X-LOCALE": "en-US",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

async def get_vehicle_status_17cyplus(self, vin, region="US"):
    """Vehicle status for 17CYPLUS vehicles."""
    try:
        res = await self.api_get(
            "v1/global/remote/status",
            _vehicle_headers(vin, region, vin=vin),
        )
        if res and res.get("vehicleStatus"):
            return res
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("vehicle_status v1/global/remote/status failed: %s", e)
    return None

async def get_vehicle_status_21mm(self, vin, region="US"):
    return await get_vehicle_status_route(self, vin, "21MM", region)


async def get_vehicle_status_route(self, vin, generation, region="US", brand="T"):
    res = await self.api_get(
        REMOTE_ROUTE + "status",
        _vehicle_headers(vin, region, **{"X-GENERATION": generation, "X-BRAND": brand}),
    )
    return res.get("status", res) if res else res

async def get_engine_status_17cyplus(self, vin, region="US"):
    """Engine status for 17CYPLUS vehicles."""
    try:
        res = await self.api_get(
            "v1/global/remote/engine-status",
            _vehicle_headers(vin, region, vin=vin),
        )
        if res:
            return res
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("engine_status v1/global/remote/engine-status failed: %s", e)
    return None

async def get_engine_status_21mm(self, vin, region="US"):
    return await get_engine_status_route(self, vin, "21MM", region)


async def get_engine_status_route(self, vin, generation, region="US", brand="T"):
    return await self.api_get(
        REMOTE_ROUTE + "engine-status",
        _vehicle_headers(vin, region, **{"X-GENERATION": generation, "X-BRAND": brand}),
    )

async def send_refresh_request_17cyplus(self, vin, region="US"):
    """Refresh status via v1/global/remote/refresh-status."""
    return await self.api_post(
        "v1/global/remote/refresh-status",
        {
            "guid": await self.auth.get_guid(),
            "deviceId": self.auth.get_device_id(),
            "vin": vin,
        },
        _vehicle_headers(vin, region),
    )

async def send_refresh_request_21mm(self, vin, region="US"):
    return await send_refresh_request_route(self, vin, "21MM", region)


async def send_refresh_request_route(self, vin, generation, region="US", brand="T"):
    return await self.api_post(
        REMOTE_ROUTE + "refresh-status",
        {"autoFixPopup": False},
        _vehicle_headers(
            vin,
            region,
            **{
                "X-GENERATION": generation,
                "X-BRAND": brand,
                "X-CORRELATIONID": str(uuid.uuid4()),
            },
        ),
    )

async def remote_request_17cyplus(self, vin, command, region="US"):
    """Remote command (lock, unlock, engine start, etc.) via v1/global/remote."""
    return await self.api_post(
        "v1/global/remote/command",
        {"command": command},
        _vehicle_headers(vin, region),
    )

async def remote_request_21mm(self, vin, command, region="US"):
    return await remote_request_route(self, vin, "21MM", command, region)


async def remote_request_route(self, vin, generation, command, region="US", brand="T"):
    body = {"command": command, "autoFixPopup": False}
    if command == "buzzer-warning":
        body["beepCount"] = 10
    return await self.api_post(
        REMOTE_ROUTE + "command",
        body,
        _vehicle_headers(
            vin,
            region,
            **{
                "X-GENERATION": generation,
                "X-BRAND": brand,
                "X-CORRELATIONID": str(uuid.uuid4()),
                "Content-Type": "application/json",
            },
        ),
    )


async def get_climate_settings(self, vin, generation, region="US", brand="T"):
    return await self.api_get(
        REMOTE_ROUTE + "climate-settings",
        _vehicle_headers(vin, region, **{"X-GENERATION": generation, "X-BRAND": brand}),
    )


async def update_climate_settings(self, vin, generation, settings, region="US", brand="T"):
    return await self.api_request(
        "PUT",
        REMOTE_ROUTE + "climate-settings",
        _vehicle_headers(vin, region, **{"X-GENERATION": generation, "X-BRAND": brand}),
        json=settings,
    )


async def electric_command(self, vin, generation, command, region="US", brand="T"):
    result = await self.api_post(
        "v2/electric/command",
        {"command": command},
        _vehicle_headers(vin, region, **{
            "X-GENERATION": generation,
            "X-BRAND": brand,
            "device-id": self.auth.get_device_id(),
        }),
    )
    if (
        not result
        or result.get("returnCode") != "ONE-RES-10000"
        or not result.get("appRequestNo")
    ):
        code = (result or {}).get("returnCode")
        raise RuntimeError(
            f"Toyota did not accept the charging command ({code or 'no request number'})."
        )

    version = "v2" if generation == "17CY" else "v3"
    query = urlencode({"remote-control": result["appRequestNo"]})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ELECTRIC_COMMAND_TIMEOUT
    while loop.time() < deadline:
        status = await self.api_request(
            "GET",
            f"{version}/electric/status?{query}",
            _vehicle_headers(vin, region, **{"X-GENERATION": generation, "X-BRAND": brand}),
            timeout=aiohttp.ClientTimeout(total=max(0.1, deadline - loop.time())),
        )
        completion = (status or {}).get("remoteControlResult") or {}
        if completion.get("status") == 0 and completion.get("result") == 0:
            return status
        await asyncio.sleep(min(2, max(0, deadline - loop.time())))
    raise RuntimeError("Toyota accepted the charging command but did not confirm completion.")


async def get_vehicle_status_17cy(self, vin, region="US"):
    """Legacy vehicle status."""
    try:
        return await self.api_get(
            "v2/legacy/remote/status",
            _vehicle_headers(vin, region),
        )
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("v2/legacy/remote/status failed: %s", e)
        return None

async def get_engine_status_17cy(self, vin, region="US"):
    """Legacy engine status."""
    try:
        return await self.api_get(
            "v1/legacy/remote/engine-status",
            _vehicle_headers(vin, region),
        )
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("v1/legacy/remote/engine-status failed: %s", e)
        return None

async def send_refresh_request_17cy(self, vin, region="US"):
    """Legacy refresh status."""
    return await self.api_post(
        "v1/legacy/remote/refresh-status",
        {
            "guid": await self.auth.get_guid(),
            "deviceId": self.auth.get_device_id(),
            "deviceType": "Android",
            "vin": vin,
        },
        _vehicle_headers(vin, region),
    )


async def remote_request_17cy(self, vin, command, value, region="US"):
    """Remote command for legacy vehicles."""
    return await self.api_post(
        "v1/legacy/remote/command",
        {
            "command": {"code": command, "value": value},
            "guid": await self.auth.get_guid(),
            "deviceId": self.auth.get_device_id(),
            "deviceType": "Android",
            "vin": vin,
        },
        _vehicle_headers(vin, region),
    )

async def get_electric_realtime_status(
    self, vin, generation="17CYPLUS", region="US"
):
    try:
        headers = _vehicle_headers(vin, region, vin=vin)
        headers["device-id"] = self.auth.get_device_id()
        headers["X-GENERATION"] = generation
        headers["X-CORRELATIONID"] = str(uuid.uuid4())
        headers["x-correlation-id"] = headers["X-CORRELATIONID"]
        realtime_electric_status = await self.api_post(
            "v2/electric/realtime-status",
            {},
            headers,
        )
        if generation != "17CY":
            return await self.get_electric_status(
                vin, realtime_electric_status["appRequestNo"], region, generation
            )
        elif realtime_electric_status["returnCode"] == "ONE-RES-10000":
            return await self.get_electric_status(
                vin, realtime_electric_status.get("appRequestNo"), region, generation
            )
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("Electric realtime status failed: %s", e)
        return None

async def get_electric_status(self, vin, realtime_status=None, region="US", generation="17CYPLUS"):
    try:
        version = "v2" if generation == "17CY" else "v3"
        url = f"{version}/electric/status"
        if realtime_status:
            query_params = {"realtime-status": realtime_status}
            url += "?" + urlencode(query_params)

        electric_status = await self.api_get(
            url, {**_vehicle_headers(vin, region), "X-GENERATION": generation}
        )
        if "vehicleInfo" in electric_status:
            return electric_status
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("Electric status failed: %s", e)
        return None

async def graphql_request(
    self,
    operation_name,
    query,
    variables,
    *,
    vin=None,
    region="US",
    backdoor_type=None,
    raise_errors=False,
    read_only=False,
):
    """Make an AppSync request, retrying only explicitly read-only operations."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": APPSYNC_API_KEY,
        "x-resolver-api-key": RESOLVER_API_KEY,
        "vin": vin or variables.get("vin", ""),
        "x-guid": await self.auth.get_guid(),
        "x-deviceid": self.auth.get_device_id(),
        "X-BRAND": TRANSPORT_BRAND,
        "X-APPBRAND": TRANSPORT_BRAND,
        "x-region": region,
        "x-channel": "ONEAPP",
        "X-APPVERSION": "3.4.0",
        "X-OSNAME": "Android",
        "X-OSVERSION": "14",
        "X-LOCALE": "en-US",
        "User-Agent": USER_AGENT,
    }
    if backdoor_type:
        headers["backdoorType"] = backdoor_type
    payload = json.dumps({
        "operationName": operation_name,
        "query": query,
        "variables": variables,
    })
    auth_retried = False
    retries = 0
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        while True:
            token = await self.auth.get_access_token()
            headers["Authorization"] = "Bearer " + token
            async with session.post(GRAPHQL_ENDPOINT, headers=headers, data=payload) as resp:
                body = await resp.text()
                status = resp.status
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                if status < 400:
                    raise
                result = {}
            errors = result.get("errors") or []
            auth_errors = errors or ([result] if status == 403 else [])
            auth_failed = status == 401 or any(
                err.get("errorType") in ("APIGW-403", "APPSYNC-AUTH-403")
                or (err.get("extensions") or {}).get("code") in ("APIGW-403", "APPSYNC-AUTH-403")
                or str(err.get("message", "")).lower() == "unauthorized"
                or "jwt expired" in str(err.get("message", "")).lower()
                for err in auth_errors
            )
            if auth_failed:
                if auth_retried:
                    raise TokenExpired("Toyota rejected the refreshed access token.")
                await self.auth.check_tokens(rejected_token=token)
                if not read_only:
                    raise RuntimeError("Toyota could not authenticate the request. Credentials were refreshed; retry the action.")
                auth_retried = True
                continue
            if read_only and (status == 429 or status >= 500) and retries < 2:
                await asyncio.sleep(2 ** retries)
                retries += 1
                continue
            if status >= 400:
                _LOGGER.debug(
                    "GraphQL %s error: HTTP %d: %s",
                    operation_name,
                    status,
                    body[:500],
                )
                if raise_errors:
                    raise RuntimeError(
                        "Toyota GraphQL %s failed with HTTP %d"
                        % (operation_name, status)
                    )
                return None
            if errors:
                err = errors[0]
                _LOGGER.debug(
                    "GraphQL %s error: %s: %s",
                    operation_name,
                    err.get("errorType"),
                    err.get("message"),
                )
                if raise_errors:
                    extensions = err.get("extensions") or {}
                    code = (
                        extensions.get("responseCode")
                        or extensions.get("code")
                        or err.get("errorType")
                    )
                    detail = (
                        extensions.get("detailedDescription")
                        or err.get("message")
                        or "Toyota rejected the AppSync request"
                    )
                    if code:
                        detail = f"{detail} [{code}]"
                    raise RuntimeError(detail)
            return result.get("data")


async def graphql_pre_wake(self, guid, region="US"):
    """Send pre-wake command to wake the vehicle's telematics unit."""
    return await self.graphql_request(
        "SendPreWakeCommand",
        GRAPHQL_PRE_WAKE,
        {"guid": guid},
        region=region,
        raise_errors=True,
    )


async def graphql_confirm_subscription(
    self, vin, backdoor_type="hatch", region="US"
):
    """Confirm subscription is active for this VIN."""
    backdoor_type = backdoor_type or "hatch"
    return await self.graphql_request(
        "ConfirmSubscriptionStatus",
        GRAPHQL_CONFIRM_SUBSCRIPTION,
        {"vin": vin, "backdoorType": backdoor_type},
        region=region,
        backdoor_type=backdoor_type,
        raise_errors=True,
    )


async def graphql_refresh_status(self, vin, region="US"):
    """Request vehicle to upload fresh status via GraphQL."""
    return await self.graphql_request(
        "RefreshVehicleStatus",
        GRAPHQL_REFRESH_STATUS,
        {"vin": vin},
        region=region,
        raise_errors=True,
    )


async def graphql_get_vehicle_status(
    self, vin, backdoor_type="hatch", region="US"
):
    """Read current AppSync state before subscription updates arrive."""
    backdoor_type = backdoor_type or "hatch"
    data = await self.graphql_request(
        "GetVehicleStatus",
        GRAPHQL_GET_VEHICLE_STATUS,
        {"vin": vin},
        region=region,
        backdoor_type=backdoor_type,
        read_only=True,
    )
    return data.get("getVehicleStatus") if data else None


async def graphql_send_remote_command(
    self, vin, command, region="US"
):
    """Submit an AppSync command after its callback subscription is ready."""
    data = await self.graphql_request(
        "SendRemoteCommand",
        GRAPHQL_SEND_REMOTE_COMMAND,
        {"command": command, "autoFixCommands": []},
        vin=vin,
        region=region,
        raise_errors=True,
    )
    execution = data.get("executeRemoteCommand") if data else None
    return _require_remote_execution(execution)


def _require_remote_execution(execution):
    correlation_id = ((execution or {}).get("payload") or {}).get(
        "correlationId"
    )
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
        raise RuntimeError(
            "Toyota closed the AppSync remote-command connection."
        )
    return {}


async def _wait_for_remote_socket_event(
    ws, expected_type, subscription_id=None
):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while loop.time() < deadline:
        message = await _receive_remote_socket_message(
            ws, max(1, deadline - loop.time())
        )
        message_type = message.get("type")
        if message_type == "ka":
            continue
        if message_type in ("connection_error", "error"):
            raise RuntimeError(_remote_socket_error(message))
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
        message = await _receive_remote_socket_message(
            ws, max(1, deadline - loop.time())
        )
        message_type = message.get("type")
        if message_type == "ka":
            continue
        if message_type in ("connection_error", "error"):
            raise RuntimeError(_remote_socket_error(message))
        if message_type != "data" or message.get("id") != subscription_id:
            continue

        callback = (
            ((message.get("payload") or {}).get("data") or {}).get(
                "onPostRemoteCallback"
            )
            or {}
        )
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
        if fail_on_unknown or status in ("error", "timeout") or callback.get("commandEnded") is True:
            raise RuntimeError(
                detail
                or f"Toyota ended the remote command with status {status}."
            )
    raise RuntimeError(
        "Toyota accepted the command but did not report completion within "
        "60 seconds."
    )


async def remote_request_24mm(self, vin, command, region="US"):
    """Run an AppSync command and await Toyota's callback."""
    return await _run_appsync_operation(
        self, vin, lambda: self.graphql_send_remote_command(vin, command, region), region,
        fail_on_unknown=command not in (
            "immediate-charge", "resume-charge", "charge-stop", "power-supply-stop",
        ),
    )


async def update_charge_settings(self, vin, variable, value, region="US"):
    """Change one charging preference and await Toyota's completion callback."""
    async def submit():
        if variable == "minimumElectricSupply":
            operation, document, key = (
                "PostPowerSupplyModeLimit", GRAPHQL_POWER_SUPPLY_LIMIT, "executeRemoteCommand",
            )
            variables = {"command": "set-power-supply", variable: str(value)}
        elif variable in ("chargingTargetLimit", "quickChargePowerLimit", "currentCharge"):
            operation, document, key = "PostChargeSettings", GRAPHQL_CHARGE_SETTINGS, "postChargeSettings"
            variables = {variable: value}
        else:
            raise ValueError("Unknown charging preference.")
        data = await self.graphql_request(
            operation, document, variables, vin=vin, region=region, raise_errors=True,
        )
        return _require_remote_execution(data.get(key) if data else None)

    return await _run_appsync_operation(self, vin, submit, region)


async def save_charge_schedule(self, vin, generation, schedule, region="US", brand="T", *, delete=False):
    """Submit a schedule using its generation's time format and confirmation."""
    appsync = generation in ("24MM", "26BEV")
    body = dict(schedule)
    endpoint = REMOTE_ROUTE + "charging" if appsync else "v1/electric/charging"
    method = "PUT" if "settingId" in body else "POST"
    if delete:
        method = "DELETE"
        endpoint += f"/{body['settingId']}"
    elif not appsync:
        for key in ("startTime", "endTime"):
            if body.get(key) is not None:
                hour, minute = map(int, body[key].split(":"))
                body[key] = {"hour": hour, "minute": minute}

    async def submit():
        result = await self.api_request(
            method, endpoint,
            _vehicle_headers(vin, region, **{
                "X-GENERATION": generation, "X-BRAND": brand,
                "device-id": str(uuid.uuid4()),
            }),
            **({} if delete else {"json": body}),
        )
        result = result or {}
        request_no = result.get("appRequestNo") or result.get("correlationId")
        if result.get("returnCode") != "ONE-RES-10000" or not request_no:
            raise RuntimeError(result.get("message") or "Toyota did not accept the charge schedule change.")
        return {"payload": {"requestNo": request_no}}

    if appsync:
        return await _run_appsync_operation(self, vin, submit, region)
    return await submit()


async def disable_charge_schedules(self, vin, generation, region="US", brand="T"):
    """Disable all saved charge schedules without deleting them."""
    async def submit():
        result = await self.api_request(
            "PUT", REMOTE_ROUTE + "charging/disable-all",
            _vehicle_headers(vin, region, **{
                "X-GENERATION": generation, "X-BRAND": brand,
                "device-id": str(uuid.uuid4()),
            }),
        ) or {}
        request_no = result.get("appRequestNo") or result.get("correlationId")
        if result.get("returnCode") != "ONE-RES-10000" or not request_no:
            raise RuntimeError(result.get("message") or "Toyota did not accept disabling the charge schedules.")
        return {"payload": {"requestNo": request_no}}

    if generation in ("24MM", "26BEV"):
        return await _run_appsync_operation(self, vin, submit, region)
    return await submit()


async def _run_appsync_operation(self, vin, submit, region, *, fail_on_unknown=False):
    # Callbacks can omit request numbers, so serialize this account's
    # operations for each vehicle.
    if not hasattr(self, "_remote_locks"):
        self._remote_locks = {}
    lock = self._remote_locks.setdefault(vin, asyncio.Lock())
    async with lock:
        return await _execute_appsync_operation(
            self, vin, submit, region, fail_on_unknown=fail_on_unknown,
        )


async def _execute_appsync_operation(self, vin, submit, region, *, fail_on_unknown=False):
    token = await self.auth.get_access_token()
    guid = await self.auth.get_guid()
    authorization = appsync_authorization(
        token,
        guid,
        vin,
        region,
        self.auth.get_device_id(),
    )
    query = urlencode(
        {
            "header": base64.b64encode(
                json.dumps(authorization).encode()
            ).decode(),
            "payload": base64.b64encode(b"{}").decode(),
        }
    )
    websocket_url = f"{GRAPHQL_WS_ENDPOINT}?{query}"

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.ws_connect(
            websocket_url, protocols=["graphql-ws"], heartbeat=30
        ) as ws:
            await ws.send_json({"type": "connection_init"})
            await _wait_for_remote_socket_event(ws, "connection_ack")

            subscription_id = str(uuid.uuid4())
            await ws.send_json(
                {
                    "id": subscription_id,
                    "type": "start",
                    "payload": {
                        "data": json.dumps(
                            {
                                "query": GRAPHQL_REMOTE_COMMAND_STATUS,
                                "variables": {"vin": vin},
                            }
                        ),
                        "extensions": {"authorization": authorization},
                    },
                }
            )
            await _wait_for_remote_socket_event(
                ws, "start_ack", subscription_id
            )
            execution = await submit()
            request_no = ((execution or {}).get("payload") or {}).get(
                "requestNo"
            )
            try:
                return await _wait_for_remote_command_result(
                    ws, vin, subscription_id, request_no, fail_on_unknown=fail_on_unknown,
                )
            except asyncio.TimeoutError as err:
                raise RuntimeError(
                    "Toyota accepted the command but did not report completion within 60 seconds."
                ) from err


async def api_request(self, method, endpoint, header_params=None, **kwargs):
    headers = await self._auth_headers()
    if header_params:
        headers.update(header_params)

    if endpoint.startswith("/"):
        endpoint = endpoint[1:]

    url = urljoin(API_GATEWAY, endpoint)

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.request(
                method, url, headers=headers, **kwargs
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                _LOGGER.debug(
                    "Toyota API error: %s %s -> %d %s | Response: %s",
                    method, url, resp.status, resp.reason, body[:500]
                )
                try:
                    resp.raise_for_status()
                except aiohttp.ClientResponseError as err:
                    try:
                        message = json.loads(body)["status"]["messages"][0]
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        message = {}
                    if isinstance(message, dict):
                        detail = message.get("detailedDescription") or message.get("description")
                        code = message.get("responseCode")
                        if isinstance(detail, str) and detail:
                            err.message = detail
                        if isinstance(code, str) and code:
                            err.message = f"{err.message} [{code}]"
                    raise
            try:
                resp_json = await resp.json()
                if "payload" in resp_json:
                    return resp_json["payload"]
                return resp_json
            except:
                _LOGGER.error("Error parsing response")
                raise
