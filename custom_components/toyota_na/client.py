import asyncio
import json
import logging
import time
import uuid
from urllib.parse import urlencode, urljoin

import aiohttp

from .appsync import (
    APPSYNC_API_KEY,
    GRAPHQL_CHARGE_SETTINGS,
    GRAPHQL_CONFIRM_SUBSCRIPTION,
    GRAPHQL_ENDPOINT,
    GRAPHQL_GET_BASIC_VEHICLE_STATUS,
    GRAPHQL_GET_VEHICLE_STATUS,
    GRAPHQL_POWER_SUPPLY_LIMIT,
    GRAPHQL_PRE_WAKE,
    GRAPHQL_REFRESH_STATUS,
    GRAPHQL_SEND_REMOTE_COMMAND,
    _failed_status_sections,
    _merge_status_fallback,
    _require_remote_execution,
    _run_appsync_operation,
    _token_rejected,
    graphql_schema_errors,
)
from .auth import ToyotaOneAuth
from .exceptions import AuthError, TokenExpired
from .generations import transport_for
from .session import HTTP_TIMEOUT, TRANSPORT_BRAND, request_session

API_GATEWAY = "https://onecdn.telematicsct.com/oneapi/"
REMOTE_ROUTE = "https://onecdn.telematicsct.com/v1/remote/route/"
# Outside /oneapi/, like the app's CDN base URL.
OTA_UPDATE_CHECK = "https://onecdn.telematicsct.com/oa24mm/v1/ota/update/check"
RESOLVER_API_KEY = "pypIHG015k4ABHWbcI4G0a94F7cC0JDo1OynpAsG"
APP_VERSION = "3.5.0"
USER_AGENT = "okhttp/5.3.2"
ELECTRIC_COMMAND_TIMEOUT = 90

_LOGGER = logging.getLogger(__name__)


def _app_headers():
    """Headers the Toyota app's shared interceptors add to every request."""
    return {
        "Content-Type": "application/json",
        "X-APPBRAND": TRANSPORT_BRAND,
        "X-APPVERSION": APP_VERSION,
        "X-OSNAME": "Android",
        "X-OSVERSION": "14",
        "X-LOCALE": "en-US",
        "X-DEVICE-TIMEZONE": time.tzname[0],
        "X-CORRELATIONID": str(uuid.uuid4()),
        "User-Agent": USER_AGENT,
    }


def _vehicle_headers(vehicle_vin, region="US", **extra):
    """Build headers for Toyota's shared North American API transport."""
    return {
        "VIN": vehicle_vin,
        "X-BRAND": TRANSPORT_BRAND,
        "x-region": region,
        **extra,
    }


def _legacy_electric(generation):
    """Whether a generation is on Toyota's first electric API."""
    transport = transport_for(generation)
    return transport is not None and transport.legacy_electric


async def _vehicle_health(self, endpoint, vin, generation, region, brand):
    """Return only the payload; Toyota can answer errors with a 2xx body and no payload."""
    body = await self.api_request(
        "GET",
        endpoint,
        _vehicle_headers(vin, region, GENERATION=generation, **{"X-BRAND": brand}),
        envelope=True,
    )
    return body.get("payload") if isinstance(body, dict) else None


class ToyotaOneClient:
    """Toyota cloud requests for account and vehicle data."""

    def __init__(self, auth=None, session=None):
        self._session = session
        self.auth = auth or ToyotaOneAuth(session=session)

    async def api_get(self, endpoint, header_params=None):
        return await self.api_request("GET", endpoint, header_params)

    async def api_post(self, endpoint, json, header_params=None):
        return await self.api_request("POST", endpoint, header_params, json=json)

    async def get_user_vehicle_list(self):
        """Vehicle list; Toyota reports ONE-VL-10002 failures with a 2xx status."""
        body = await self.api_request(
            "GET", "v2/vehicle/guid", envelope=True, auth_failure_raises_expired=True
        )
        if not isinstance(body, dict):
            return body
        status = body.get("status")
        messages = status.get("messages") if isinstance(status, dict) else None
        if not isinstance(messages, list):
            messages = []
        message = next(
            (
                item
                for item in messages
                if isinstance(item, dict) and item.get("responseCode") is not None
            ),
            {},
        )
        if message.get("responseCode") == "ONE-VL-10002":
            detail = message.get("detailedDescription") or message.get("description")
            if not isinstance(detail, str) or not detail:
                detail = "Toyota could not return the vehicle list."
            err = RuntimeError(f"{detail} [ONE-VL-10002]")
            err.response_code = "ONE-VL-10002"  # type: ignore[attr-defined]
            raise err
        return body.get("payload", body)

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

    async def get_vehicle_health_report(self, vin, generation, region="US", brand="T"):
        return await _vehicle_health(
            self, "v1/vehiclehealth/report", vin, generation, region, brand
        )

    async def get_vehicle_health_status(self, vin, generation, region="US", brand="T"):
        return await _vehicle_health(
            self, "v1/vehiclehealth/status", vin, generation, region, brand
        )

    async def get_service_campaigns(self, vin):
        """Recalls and campaigns; Toyota's app accepts either envelope spelling."""
        body = await self.api_get("v2/service-campaign", {"vin": vin})
        return body["payLoad"] if isinstance(body, dict) and "payLoad" in body else body

    async def get_software_update(self, vin):
        """Toyota's software update check, with the app's lowercase vin header.

        The app's shared interceptor adds X-APIVERSION v1 to every /oa24mm request.
        """
        body = await self.api_request(
            "GET", OTA_UPDATE_CHECK, {"vin": vin, "X-APIVERSION": "v1"}, envelope=True
        )
        return body.get("payload") if isinstance(body, dict) else None

    async def get_notification_history(self):
        """Account notifications grouped by vehicle; reading them never wakes a vehicle."""
        return await self.api_get("v2/notification/history", {"GUID": await self.auth.get_guid()})

    async def _auth_headers(self):
        return {
            **_app_headers(),
            "AUTHORIZATION": "Bearer " + await self.auth.get_access_token(),
            "X-API-KEY": RESOLVER_API_KEY,
            "X-GUID": await self.auth.get_guid(),
            "X-CHANNEL": "ONEAPP",
            "X-BRAND": TRANSPORT_BRAND,
            "x-region": "US",
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
        return await self.get_vehicle_status_route(vin, "21MM", region)

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
        return await self.get_engine_status_route(vin, "21MM", region)

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
        return await self.send_refresh_request_route(vin, "21MM", region)

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
        return await self.remote_request_route(vin, "21MM", command, region)

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
            _vehicle_headers(
                vin,
                region,
                **{
                    "X-GENERATION": generation,
                    "X-BRAND": brand,
                    "device-id": self.auth.get_device_id(),
                },
            ),
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

        version = "v2" if _legacy_electric(generation) else "v3"
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
            # Like Toyota's app, a finished command with a non-zero result was refused.
            state, outcome, error = (
                completion.get("status"),
                completion.get("result"),
                completion.get("errorCode"),
            )
            if type(state) is int and state == 0 and type(outcome) is int and outcome != 0:
                detail = f"result {outcome}" + (
                    f", error {error}" if isinstance(error, (str, int)) and error != "" else ""
                )
                raise RuntimeError(f"Toyota refused the charging command ({detail}).")
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

    async def get_electric_realtime_status(self, vin, generation="17CYPLUS", region="US"):
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
            if not _legacy_electric(generation):
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

    async def get_electric_status(
        self, vin, realtime_status=None, region="US", generation="17CYPLUS"
    ):
        """Read EV status, retrying the legacy request if v3 has no readings."""
        legacy = _legacy_electric(generation)
        versions = ("v2",) if legacy else ("v3", "v2")
        primary = None
        for version in versions:
            try:
                url = f"{version}/electric/status"
                if realtime_status:
                    url += "?" + urlencode({"realtime-status": realtime_status})
                headers = _vehicle_headers(vin, region)
                if version == "v3" or legacy:
                    headers["X-GENERATION"] = generation
                electric_status = await self.api_get(url, headers)
                vehicle_info = (
                    electric_status.get("vehicleInfo")
                    if isinstance(electric_status, dict)
                    else None
                )
                if not isinstance(vehicle_info, dict):
                    _LOGGER.debug("Electric status %s returned no vehicle info", version)
                    continue
                charge_info = vehicle_info.get("chargeInfo")
                if version == "v2" or (
                    isinstance(charge_info, dict)
                    and any(
                        charge_info.get(key) is not None
                        for key in (
                            "evDistance",
                            "evDistanceAC",
                            "chargeRemainingAmount",
                            "plugStatus",
                            "remainingChargeTime",
                            "chargeType",
                            "connectorStatus",
                        )
                    )
                ):
                    if primary is not None:
                        primary_info = primary["vehicleInfo"]
                        vehicle_info = dict(vehicle_info)
                        if isinstance(primary_info.get("timerChargeInfo"), list):
                            vehicle_info["timerChargeInfo"] = primary_info["timerChargeInfo"]
                            vehicle_info["_schedule_acquisition_datetime"] = primary_info.get(
                                "acquisitionDatetime"
                            )
                        if isinstance(primary_info.get("maxNoOfChargeSchedules"), int):
                            vehicle_info["maxNoOfChargeSchedules"] = primary_info[
                                "maxNoOfChargeSchedules"
                            ]
                        return {**electric_status, "vehicleInfo": vehicle_info}
                    return electric_status
                primary = electric_status
                _LOGGER.debug("Electric status %s returned no charge readings; trying v2", version)
            except AuthError:
                raise
            except Exception as e:
                _LOGGER.debug("Electric status %s failed: %s", version, e)
        return primary

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
        fallback_query=None,
    ):
        """Make an AppSync request, retrying only explicitly read-only operations."""
        headers = {
            **_app_headers(),
            "x-api-key": APPSYNC_API_KEY,
            "x-resolver-api-key": RESOLVER_API_KEY,
            "vin": vin or variables.get("vin", ""),
            "x-guid": await self.auth.get_guid(),
            "x-deviceid": self.auth.get_device_id(),
            "X-BRAND": TRANSPORT_BRAND,
            "x-region": region,
            "x-channel": "ONEAPP",
        }
        if backdoor_type:
            headers["backdoorType"] = backdoor_type
        payload = {
            "operationName": operation_name,
            "query": query,
            "variables": variables,
        }
        auth_retried = False
        retries = 0
        original_data = None
        recovery_sections = set()
        async with request_session(getattr(self, "_session", None)) as (session, shared):
            post_kwargs = {"timeout": HTTP_TIMEOUT} if shared else {}
            while True:
                token = await self.auth.get_access_token()
                headers["Authorization"] = "Bearer " + token
                try:
                    async with session.post(
                        GRAPHQL_ENDPOINT,
                        headers=headers,
                        data=json.dumps(payload),
                        **post_kwargs,
                    ) as resp:
                        body = await resp.text()
                        status = resp.status
                    try:
                        result = json.loads(body)
                    except json.JSONDecodeError:
                        if status < 400:
                            raise
                        result = {}
                except TimeoutError, aiohttp.ClientError, json.JSONDecodeError:
                    if original_data is not None and not raise_errors:
                        return original_data
                    raise
                if not isinstance(result, dict):
                    if status >= 400:
                        result = {}
                    elif raise_errors:
                        raise RuntimeError("Toyota GraphQL returned an invalid response.")
                    else:
                        return original_data
                errors = result.get("errors") or []
                auth_errors = errors or ([result] if status == 403 else [])
                auth_failed = status == 401 or _token_rejected(auth_errors)
                # A rejection after a successful refresh is about this request, such as a
                # vehicle without access, not the sign-in. A failed refresh raises TokenExpired.
                if auth_failed and not auth_retried:
                    await self.auth.check_tokens(rejected_token=token)
                    if not read_only:
                        raise RuntimeError(
                            "Toyota could not authenticate the request. Credentials were refreshed; retry the action."
                        )
                    auth_retried = True
                    continue
                if read_only and (status == 429 or status >= 500) and retries < 2:
                    await asyncio.sleep(2**retries)
                    retries += 1
                    continue
                data = result.get("data")
                if data is not None and not isinstance(data, dict):
                    if raise_errors:
                        raise RuntimeError("Toyota GraphQL returned an invalid response.")
                    return original_data
                if read_only and fallback_query and status in (200, 400):
                    recovery_sections = _failed_status_sections(data, errors)
                    if recovery_sections or (
                        graphql_schema_errors(errors)
                        and all(value is None for value in (data or {}).values())
                    ):
                        _LOGGER.debug(
                            "GraphQL %s failed on optional status fields; retrying the basic query",
                            operation_name,
                        )
                        original_data = data
                        payload["query"] = fallback_query
                        fallback_query = None
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
                            f"Toyota GraphQL {operation_name} failed with HTTP {status}"
                        )
                    return original_data
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
                if original_data is not None:
                    return _merge_status_fallback(original_data, data, recovery_sections)
                return data

    async def graphql_pre_wake(self, guid, region="US"):
        """Send pre-wake command to wake the vehicle's telematics unit."""
        return await self.graphql_request(
            "SendPreWakeCommand",
            GRAPHQL_PRE_WAKE,
            {"guid": guid},
            region=region,
            raise_errors=True,
        )

    async def graphql_confirm_subscription(self, vin, backdoor_type="hatch", region="US"):
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

    async def graphql_get_vehicle_status(self, vin, backdoor_type="hatch", region="US"):
        """Read current AppSync state before subscription updates arrive."""
        backdoor_type = backdoor_type or "hatch"
        data = await self.graphql_request(
            "GetVehicleStatus",
            GRAPHQL_GET_VEHICLE_STATUS,
            {"vin": vin},
            region=region,
            backdoor_type=backdoor_type,
            read_only=True,
            fallback_query=GRAPHQL_GET_BASIC_VEHICLE_STATUS,
        )
        status = data.get("getVehicleStatus") if data else None
        return status if isinstance(status, dict) else None

    async def graphql_send_remote_command(self, vin, command, region="US"):
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

    async def remote_request_24mm(self, vin, command, region="US"):
        """Run an AppSync command and await Toyota's callback."""
        return await _run_appsync_operation(
            self,
            vin,
            lambda: self.graphql_send_remote_command(vin, command, region),
            region,
            fail_on_unknown=command
            not in (
                "immediate-charge",
                "resume-charge",
                "charge-stop",
                "power-supply-stop",
            ),
        )

    async def update_charge_settings(self, vin, variable, value, region="US"):
        """Change one charging preference and await Toyota's completion callback."""

        async def submit():
            if variable == "minimumElectricSupply":
                operation, document, key = (
                    "PostPowerSupplyModeLimit",
                    GRAPHQL_POWER_SUPPLY_LIMIT,
                    "executeRemoteCommand",
                )
                variables = {"command": "set-power-supply", variable: str(value)}
            elif variable in ("chargingTargetLimit", "quickChargePowerLimit", "currentCharge"):
                operation, document, key = (
                    "PostChargeSettings",
                    GRAPHQL_CHARGE_SETTINGS,
                    "postChargeSettings",
                )
                variables = {variable: value}
            else:
                raise ValueError("Unknown charging preference.")
            data = await self.graphql_request(
                operation,
                document,
                variables,
                vin=vin,
                region=region,
                raise_errors=True,
            )
            return _require_remote_execution(data.get(key) if data else None)

        return await _run_appsync_operation(self, vin, submit, region)

    async def save_charge_schedule(
        self, vin, generation, schedule, region="US", brand="T", *, delete=False
    ):
        """Submit a schedule using its generation's time format and confirmation."""
        transport = transport_for(generation)
        appsync = transport is not None and transport.uses_appsync
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
                method,
                endpoint,
                _vehicle_headers(
                    vin,
                    region,
                    **{
                        "X-GENERATION": generation,
                        "X-BRAND": brand,
                        "device-id": str(uuid.uuid4()),
                    },
                ),
                **({} if delete else {"json": body}),
            )
            result = result or {}
            request_no = result.get("appRequestNo") or result.get("correlationId")
            if result.get("returnCode") != "ONE-RES-10000" or not request_no:
                raise RuntimeError(
                    result.get("message") or "Toyota did not accept the charge schedule change."
                )
            return {"payload": {"requestNo": request_no}}

        if appsync:
            return await _run_appsync_operation(self, vin, submit, region)
        return await submit()

    async def get_climate_schedules(self, vin, generation, region="US", brand="T"):
        return await self.api_get(
            REMOTE_ROUTE + "ac-reservation",
            _vehicle_headers(vin, region, **{"X-GENERATION": generation, "X-BRAND": brand}),
        )

    async def save_climate_schedule(
        self, vin, generation, schedule, region="US", brand="T", *, identifier=None, delete=False
    ):
        headers = _vehicle_headers(
            vin,
            region,
            **{
                "X-GENERATION": generation,
                "X-BRAND": brand,
                "device-id": self.auth.get_device_id(),
            },
        )
        if identifier is not None:
            headers["ReservationNo"] = str(identifier)
        method = "DELETE" if delete else "PUT" if identifier is not None else "POST"
        result = await self.api_request(
            method,
            REMOTE_ROUTE + "ac-reservation",
            headers,
            **({} if delete else {"json": schedule}),
        )
        # Toyota's app treats any successful response as accepted and never reads
        # its returnCode, so the caller confirms the change by reading it back.
        if isinstance(result, dict) and isinstance(result.get("payload"), dict):
            result = {"message": result.get("message"), **result["payload"]}
        return result if isinstance(result, dict) else {}

    async def api_request(
        self,
        method,
        endpoint,
        header_params=None,
        *,
        envelope=False,
        auth_failure_raises_expired=False,
        **kwargs,
    ):
        if endpoint.startswith("/"):
            endpoint = endpoint[1:]

        url = urljoin(API_GATEWAY, endpoint)
        auth_retried = False

        async with request_session(getattr(self, "_session", None)) as (session, shared):
            request_kwargs = {"timeout": HTTP_TIMEOUT, **kwargs} if shared else kwargs
            while True:
                headers = await self._auth_headers()
                if header_params:
                    headers.update(header_params)
                auth_header = headers.get("AUTHORIZATION") or headers.get("Authorization") or ""
                sent_token = (
                    auth_header.removeprefix("Bearer ")
                    if auth_header.startswith("Bearer ")
                    else None
                )

                async with session.request(method, url, headers=headers, **request_kwargs) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        _LOGGER.debug(
                            "Toyota API error: %s %s -> %d %s | Response: %s",
                            method,
                            url,
                            resp.status,
                            resp.reason,
                            body[:500],
                        )
                        try:
                            parsed_body = json.loads(body)
                        except json.JSONDecodeError:
                            parsed_body = None
                        check_tokens = getattr(self.auth, "check_tokens", None)
                        if resp.status == 401 and callable(check_tokens):
                            if auth_retried:
                                if auth_failure_raises_expired:
                                    raise TokenExpired(
                                        "Toyota rejected the refreshed access token."
                                    )
                            elif sent_token is not None:
                                await check_tokens(rejected_token=sent_token)
                                if method.upper() != "GET":
                                    raise RuntimeError(
                                        "Toyota could not authenticate the request. "
                                        "Credentials were refreshed; retry the action."
                                    )
                                auth_retried = True
                                continue
                        try:
                            resp.raise_for_status()
                        except aiohttp.ClientResponseError as err:
                            try:
                                message = parsed_body["status"]["messages"][0]
                            except KeyError, IndexError, TypeError:
                                message = {}
                            if isinstance(message, dict):
                                detail = message.get("detailedDescription") or message.get(
                                    "description"
                                )
                                code = message.get("responseCode")
                                if isinstance(detail, str) and detail:
                                    err.message = detail
                                if isinstance(code, str) and code:
                                    err.message = f"{err.message} [{code}]"
                                    err.response_code = code  # type: ignore[attr-defined]
                            raise
                    try:
                        resp_json = await resp.json()
                        if isinstance(resp_json, dict) and "payload" in resp_json and not envelope:
                            return resp_json["payload"]
                        return resp_json
                    except Exception:
                        _LOGGER.error("Error parsing response")
                        raise
