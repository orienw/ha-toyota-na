"""AppSync subscriptions for 21MM, 24MM, and 26BEV vehicle status updates."""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from time import monotonic
from typing import TYPE_CHECKING

import aiohttp

from .appsync import (
    GRAPHQL_BASIC_VEHICLE_STATUS_FIELDS,
    GRAPHQL_VEHICLE_STATUS_FIELDS,
    _token_rejected,
    _TokenRejected,
    appsync_authorization,
    appsync_subscription_message,
    appsync_ws_url,
    graphql_schema_errors,
)
from .exceptions import AuthError
from .session import HTTP_TIMEOUT

_LOGGER = logging.getLogger(__name__)

SUBSCRIBE_VEHICLE_STATUS = (
    "subscription ReceiveVehicleStatus($vin: String!) {"
    " onVehicleStatusUpdated(vin: $vin) {" + GRAPHQL_VEHICLE_STATUS_FIELDS + "} }"
)

SUBSCRIBE_BASIC_VEHICLE_STATUS = (
    "subscription ReceiveVehicleStatus($vin: String!) {"
    " onVehicleStatusUpdated(vin: $vin) {" + GRAPHQL_BASIC_VEHICLE_STATUS_FIELDS + "} }"
)


def _rejects_token(msg) -> bool:
    """Whether an AppSync error frame reports the access token as rejected."""
    payload = msg.get("payload")
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return isinstance(errors, list) and _token_rejected(errors)


class ToyotaWebSocketHandler:
    """Manages AppSync WebSocket connection for vehicle status push notifications."""

    def __init__(
        self,
        client,
        status_callback: Callable[[str, dict], None] | None = None,
        auth_error_callback: Callable[[AuthError], None] | None = None,
    ):
        """Initialize with a ToyotaOneClient instance."""
        self._client = client
        self._status_callback = status_callback
        self._auth_error_callback = auth_error_callback
        self._session: aiohttp.ClientSession | None = None
        self._owns_session = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._subscriptions: dict[str, str] = {}  # vin -> subscription_id
        self._subscription_tokens: dict[str, str] = {}  # vin -> the token its subscription sent
        # VINs, and None for the connection, whose rejection has already refreshed the sign-in.
        # One stays until Toyota accepts it, so a lasting refusal refreshes once, not per retry.
        self._refreshed: set[str | None] = set()
        self._basic_status_vins: set[str] = set()
        self._cached_status: dict[str, dict] = {}  # vin -> latest vehicle status dict
        self._vehicle_contexts: dict[str, dict] = {}
        self._task: asyncio.Task | None = None
        self._retry_tasks: dict[str, asyncio.Task] = {}
        self._retry_delays: dict[str, float] = {}
        self._running = False
        self._ready = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 300
        self._keepalive_timeout: float = 300
        self._keepalive_deadline: float | None = None

    @property
    def is_connected(self):
        return self._ws is not None and not self._ws.closed

    def get_cached_status(self, vin):
        """Get the latest cached vehicle status received via WebSocket."""
        return self._cached_status.get(vin)

    async def start(self, vehicle_contexts):
        """Start the WebSocket handler and subscribe to the given VINs."""
        self._running = True
        await self.update_vehicle_contexts(vehicle_contexts)

    async def update_vehicle_contexts(self, vehicle_contexts):
        """Reconcile subscriptions with the account's current vehicles."""
        if isinstance(vehicle_contexts, Mapping):
            contexts = {vin: dict(context) for vin, context in vehicle_contexts.items()}
        else:
            contexts = {vin: {} for vin in vehicle_contexts}
        previous_contexts = self._vehicle_contexts
        self._vehicle_contexts = contexts
        for vin, context in previous_contexts.items():
            if contexts.get(vin) == context:
                continue
            self._cancel_retry(vin)
            self._retry_delays.pop(vin, None)
            self._cached_status.pop(vin, None)
            self._basic_status_vins.discard(vin)
            self._refreshed.discard(vin)
            self._subscription_tokens.pop(vin, None)
            sub_id = self._subscriptions.pop(vin, None)
            # Held here because a dropped connection clears it while the stop is being sent.
            ws = self._ws
            if sub_id and ws is not None and not ws.closed:
                try:
                    await ws.send_json({"id": sub_id, "type": "stop"})
                except Exception as err:
                    _LOGGER.debug("WebSocket: failed to stop subscription: %s", err)
                    await ws.close()

        if not self._running:
            return
        if not contexts:
            await self._disconnect()
        elif self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())
        elif self._ready:
            for vin in contexts:
                if vin not in self._subscriptions and vin not in self._retry_tasks:
                    self._schedule_retry(vin, delay=0)

    async def stop(self):
        """Stop the WebSocket handler and clean up resources."""
        self._running = False
        await self._disconnect()
        _LOGGER.debug("WebSocket handler stopped")

    async def _disconnect(self):
        self._ready = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._cancel_retries()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
        self._ws = None
        self._session = None
        self._owns_session = False
        self._subscriptions.clear()
        self._subscription_tokens.clear()
        self._refreshed.clear()

    def _notify_auth_error(self, err: AuthError) -> None:
        self._running = False
        if self._auth_error_callback is not None:
            try:
                self._auth_error_callback(err)
            except Exception as callback_err:
                _LOGGER.debug("WebSocket auth error callback failed: %s", callback_err)

    async def _run_loop(self):
        """Main loop: connect, listen, reconnect on failure."""
        while self._running and self._vehicle_contexts:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                return
            except AuthError as err:
                _LOGGER.debug("WebSocket stopped after authentication failure: %s", err)
                self._notify_auth_error(err)
                return
            except Exception as e:
                status = f" (HTTP {e.status})" if isinstance(e, aiohttp.ClientResponseError) else ""
                _LOGGER.debug("WebSocket connection error: %s%s", type(e).__name__, status)

            if not self._running or not self._vehicle_contexts:
                return

            _LOGGER.debug("WebSocket: reconnecting in %ds", self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _connect_and_listen(self):
        """Connect to AppSync WebSocket, subscribe, and process messages."""
        token = await self._client.auth.get_access_token()
        guid = await self._client.auth.get_guid()
        device_id = self._client.auth.get_device_id()

        # AppSync requires auth headers as base64-encoded URL params
        first_vin = next(iter(self._vehicle_contexts), "")
        first_context = self._vehicle_contexts.get(first_vin, {})
        auth_header = appsync_authorization(
            token,
            guid,
            first_vin,
            first_context.get("region", "US"),
            device_id,
        )
        ws_url = appsync_ws_url(auth_header)

        shared_session = getattr(self._client, "_session", None)
        if shared_session is not None and not getattr(shared_session, "closed", False):
            self._session = shared_session
            self._owns_session = False
        else:
            self._session = aiohttp.ClientSession(
                timeout=HTTP_TIMEOUT, cookie_jar=aiohttp.DummyCookieJar()
            )
            self._owns_session = True
        try:
            self._ws = await self._session.ws_connect(
                ws_url, protocols=["graphql-ws"], heartbeat=30
            )
            # Initiate AppSync connection handshake
            await self._ws.send_json({"type": "connection_init"})
            self._keepalive_deadline = monotonic() + 30

            while self._running:
                msg = await asyncio.wait_for(
                    self._ws.receive(),
                    timeout=max(0, self._keepalive_deadline - monotonic()),
                )
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(json.loads(msg.data), token, guid)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except (aiohttp.ClientResponseError, _TokenRejected) as err:
            rejected = isinstance(err, _TokenRejected) or err.status == 401
            if rejected and None not in self._refreshed:
                await self._client.auth.check_tokens(rejected_token=token)
                self._refreshed.add(None)
            raise
        finally:
            self._ready = False
            await self._cancel_retries()
            self._subscriptions.clear()
            self._subscription_tokens.clear()
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._owns_session and self._session and not self._session.closed:
                await self._session.close()
            self._ws = None
            self._session = None
            self._owns_session = False
            self._keepalive_deadline = None

    async def _handle_message(self, msg, token, guid):
        """Handle an AppSync WebSocket protocol message."""
        msg_type = msg.get("type")
        vin: str | None

        if msg_type == "connection_ack":
            self._ready = True
            self._refreshed.discard(None)
            self._keepalive_timeout = (
                msg.get("payload", {}).get("connectionTimeoutMs", 300_000) / 1000
            )
            self._keepalive_deadline = monotonic() + self._keepalive_timeout
            _LOGGER.debug(
                "WebSocket: connected, subscribing to %d VINs",
                len(self._vehicle_contexts),
            )
            self._reconnect_delay = 5  # Reset backoff on successful connect
            for vin in list(self._vehicle_contexts):
                await self._subscribe_vin(vin, token, guid)

        elif msg_type == "start_ack":
            sub_id = msg.get("id")
            vin = next(
                (v for v, sid in self._subscriptions.items() if sid == sub_id),
                None,
            )
            _LOGGER.debug(
                "WebSocket: subscription active for VIN ...%s",
                (vin or "???")[-4:],
            )
            # Per app flow: call ConfirmSubscription after subscription active
            if vin:
                self._cancel_retry(vin)
                self._refreshed.discard(vin)
                try:
                    context = self._vehicle_contexts.get(vin, {})
                    result = await self._client.graphql_confirm_subscription(
                        vin,
                        context.get("backdoor_type"),
                        context.get("region", "US"),
                    )
                    if result is not None:
                        _LOGGER.debug(
                            "WebSocket: subscription confirmed for VIN ...%s",
                            vin[-4:],
                        )
                    else:
                        _LOGGER.debug(
                            "WebSocket: confirm subscription returned None "
                            "(device limit exceeded?) for VIN ...%s",
                            vin[-4:],
                        )
                except AuthError:
                    raise
                except Exception as e:
                    _LOGGER.debug("WebSocket: confirm subscription failed: %s", e)

        elif msg_type == "data":
            payload = msg.get("payload")
            data = payload.get("data") if isinstance(payload, dict) else None
            status = data.get("onVehicleStatusUpdated") if isinstance(data, dict) else None
            if isinstance(status, dict) and status:
                vin = status.get("vin", "")
                if (
                    vin not in self._vehicle_contexts
                    or not msg.get("id")
                    or self._subscriptions.get(vin) != msg["id"]
                ):
                    return
                self._retry_delays.pop(vin, None)
                _LOGGER.debug(
                    "WebSocket: received vehicle status for VIN ...%s (updated: %s)",
                    vin[-4:],
                    status.get("lastUpdateDateTime", "?"),
                )
                self._cached_status[vin] = status
                if self._status_callback is not None:
                    try:
                        self._status_callback(vin, status)
                    except Exception as e:
                        _LOGGER.warning(
                            "WebSocket: failed to apply status for VIN ...%s: %s",
                            vin[-4:],
                            e,
                        )

        elif msg_type == "connection_error" or (msg_type == "error" and not msg.get("id")):
            self._ready = False
            _LOGGER.warning("WebSocket connection error: %s", msg.get("payload", msg))
            if _rejects_token(msg):
                raise _TokenRejected("AppSync rejected the access token")
            raise ConnectionError("AppSync rejected the connection")

        elif msg_type in ("error", "complete"):
            vin = next(
                (vin for vin, sub_id in self._subscriptions.items() if sub_id == msg.get("id")),
                None,
            )
            if vin is not None:
                self._subscriptions.pop(vin)
                sent_token = self._subscription_tokens.pop(vin, None)
                payload = msg.get("payload")
                errors = payload.get("errors") if isinstance(payload, dict) else None
                if msg_type == "error" and graphql_schema_errors(errors):
                    self._basic_status_vins.add(vin)
                _LOGGER.warning(
                    "WebSocket: subscription ended for VIN ...%s (%s); retrying",
                    vin[-4:],
                    msg_type,
                )
                _LOGGER.debug("WebSocket subscription response: %s", json.dumps(msg)[:500])
                rejected = (
                    msg_type == "error" and vin not in self._refreshed and _rejects_token(msg)
                )
                self._schedule_retry(vin, rejected_token=sent_token if rejected else None)

        elif msg_type == "ka":
            self._keepalive_deadline = monotonic() + self._keepalive_timeout

    async def _subscribe_vin(self, vin, token, guid):
        """Subscribe to vehicle status updates for a specific VIN."""
        if vin not in self._vehicle_contexts:
            return
        sub_id = str(uuid.uuid4())
        self._subscriptions[vin] = sub_id
        self._subscription_tokens[vin] = token
        context = self._vehicle_contexts.get(vin, {})
        authorization = appsync_authorization(
            token,
            guid,
            vin,
            context.get("region", "US"),
            self._client.auth.get_device_id(),
        )

        subscription = appsync_subscription_message(
            sub_id,
            (
                SUBSCRIBE_BASIC_VEHICLE_STATUS
                if vin in self._basic_status_vins
                else SUBSCRIBE_VEHICLE_STATUS
            ),
            {"vin": vin},
            authorization,
        )
        self._schedule_retry(vin, delay=30)
        if TYPE_CHECKING:
            assert self._ws is not None
        await self._ws.send_json(subscription)

    def _cancel_retry(self, vin):
        task = self._retry_tasks.pop(vin, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _cancel_retries(self):
        tasks = list(self._retry_tasks.values())
        for vin in list(self._retry_tasks):
            self._cancel_retry(vin)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_retry(self, vin, delay=None, rejected_token=None):
        self._cancel_retry(vin)
        if not self._running or not self._ready or vin not in self._vehicle_contexts:
            return
        if delay is None:
            delay = self._retry_delays.get(vin, 5)
            self._retry_delays[vin] = min(delay * 2, self._max_reconnect_delay)
        self._retry_tasks[vin] = asyncio.create_task(
            self._retry_subscription(vin, delay, rejected_token)
        )

    async def _retry_subscription(self, vin, delay, rejected_token=None):
        try:
            await asyncio.sleep(delay)
            sub_id = self._subscriptions.pop(vin, None)
            self._subscription_tokens.pop(vin, None)
            if sub_id:
                if TYPE_CHECKING:
                    assert self._ws is not None
                await self._ws.send_json({"id": sub_id, "type": "stop"})
                self._schedule_retry(vin)
                return
            if rejected_token is not None:
                await self._client.auth.check_tokens(rejected_token=rejected_token)
                self._refreshed.add(vin)
            token = await self._client.auth.get_access_token()
            guid = await self._client.auth.get_guid()
            await self._subscribe_vin(vin, token, guid)
        except asyncio.CancelledError:
            raise
        except AuthError as err:
            _LOGGER.debug("WebSocket: subscription retry stopped after auth failure: %s", err)
            self._ready = False
            self._notify_auth_error(err)
            if self._ws and not self._ws.closed:
                await self._ws.close()
        except Exception as err:
            _LOGGER.debug("WebSocket: subscription retry failed: %s", err)
            self._schedule_retry(vin, rejected_token=rejected_token)
        finally:
            if self._retry_tasks.get(vin) is asyncio.current_task():
                self._retry_tasks.pop(vin)
