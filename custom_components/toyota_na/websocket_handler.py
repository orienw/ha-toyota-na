"""AWS AppSync WebSocket handler for Toyota vehicle status subscriptions.

The 21MM and 24MM paths use AppSync subscriptions to supplement polled
status with live door, lock, window, cargo opening, and engine updates.

Flow (per APK analysis of SubscribeVehicleStatusDefaultRepo):
1. Connect WebSocket to AppSync realtime endpoint
2. Subscribe to ReceiveVehicleStatus for each VIN
3. On subscription active (start_ack) -> call ConfirmSubscription mutation
4. PreWake + RefreshStatus trigger the vehicle to upload fresh data
5. Data arrives via WebSocket push -> cached and sent to Home Assistant immediately
"""
import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

GRAPHQL_WS_ENDPOINT = "wss://oa-api.telematicsct.com/graphql/realtime"
GRAPHQL_HOST = "oa-api.telematicsct.com"
APPSYNC_API_KEY = "da2-zgeayo2qh5eo7cj6pmdwhwugze"
TRANSPORT_BRAND = "T"

# Subscription query from Toyota app v3.1.0 APK (yj/h$i.smali)
SUBSCRIBE_VEHICLE_STATUS = (
    "subscription ReceiveVehicleStatus($vin: String!) {"
    " onVehicleStatusUpdated(vin: $vin) {"
    " vin lastUpdateDateTime"
    " vehicleState {"
    " lastUpdateDateTime driverPosition"
    " doors {"
    " driverSide { lock { status } position { status } }"
    " passengerSide { lock { status } position { status } }"
    " rearDriverSide { lock { status } position { status } }"
    " rearPassengerSide { lock { status } position { status } }"
    " }"
    " windows {"
    " driverSide { position { status } }"
    " passengerSide { position { status } }"
    " rearDriverSide { position { status } }"
    " rearPassengerSide { position { status } }"
    " }"
    " hatch { lock { status } position { status } }"
    " hood { position { status } }"
    " moonroof { position { status } }"
    " trunk { lock { status } position { status } }"
    " tailgate { lock { status } position { status } }"
    " engine { running status }"
    " }"
    " location { latitude longitude lastUpdateDateTime }"
    " telemetry {"
    " lastUpdateDateTime"
    " odo { unit value }"
    " fugage { unit value }"
    " range { unit value }"
    " }"
    " }"
    "}"
)


class ToyotaWebSocketHandler:
    """Manages AppSync WebSocket connection for vehicle status push notifications."""

    def __init__(
        self,
        client,
        status_callback: Optional[Callable[[str, dict], None]] = None,
    ):
        """Initialize with a ToyotaOneClient instance (already monkey-patched)."""
        self._client = client
        self._status_callback = status_callback
        self._session = None
        self._ws = None
        self._subscriptions = {}  # vin -> subscription_id
        self._cached_status = {}  # vin -> latest vehicle status dict
        self._confirmed_vins = set()  # VINs that have been confirmed
        self._vins = []
        self._vehicle_contexts: dict[str, dict] = {}
        self._task = None
        self._retry_task = None
        self._running = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 300

    @property
    def is_connected(self):
        return self._ws is not None and not self._ws.closed

    def get_cached_status(self, vin):
        """Get the latest cached vehicle status received via WebSocket."""
        return self._cached_status.get(vin)

    async def start(self, vehicle_contexts):
        """Start the WebSocket handler and subscribe to the given VINs."""
        self._running = True
        if isinstance(vehicle_contexts, Mapping):
            self._vehicle_contexts = {
                vin: dict(context) for vin, context in vehicle_contexts.items()
            }
        else:
            self._vehicle_contexts = {vin: {} for vin in vehicle_contexts}
        self._vins = list(self._vehicle_contexts)
        if self._vins:
            self._task = asyncio.ensure_future(self._run_loop())
            _LOGGER.debug("WebSocket handler started for %d VINs", len(self._vins))

    async def stop(self):
        """Stop the WebSocket handler and clean up resources."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _LOGGER.debug("WebSocket handler stopped")

    async def _run_loop(self):
        """Main loop: connect, listen, reconnect on failure."""
        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.debug("WebSocket connection error: %s", e)

            if not self._running:
                return

            _LOGGER.debug(
                "WebSocket: reconnecting in %ds", self._reconnect_delay
            )
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self._max_reconnect_delay
            )

    async def _connect_and_listen(self):
        """Connect to AppSync WebSocket, subscribe, and process messages."""
        token = await self._client.auth.get_access_token()
        guid = await self._client.auth.get_guid()

        # AppSync requires auth headers as base64-encoded URL params
        auth_header = {
            "host": GRAPHQL_HOST,
            "x-api-key": APPSYNC_API_KEY,
            "Authorization": f"Bearer {token}",
            "x-channel": "ONEAPP",
        }
        header_b64 = base64.b64encode(
            json.dumps(auth_header).encode()
        ).decode()
        payload_b64 = base64.b64encode(b"{}").decode()
        ws_url = (
            f"{GRAPHQL_WS_ENDPOINT}?header={header_b64}&payload={payload_b64}"
        )

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                ws_url, protocols=["graphql-ws"], heartbeat=30
            )
        except Exception:
            await self._session.close()
            self._session = None
            raise

        try:
            # Initiate AppSync connection handshake
            await self._ws.send_json({"type": "connection_init"})

            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(
                        json.loads(msg.data), token, guid
                    )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        finally:
            self._subscriptions.clear()
            if self._ws and not self._ws.closed:
                await self._ws.close()
            if self._session and not self._session.closed:
                await self._session.close()
            self._ws = None
            self._session = None

    async def _handle_message(self, msg, token, guid):
        """Handle an AppSync WebSocket protocol message."""
        msg_type = msg.get("type")

        if msg_type == "connection_ack":
            _LOGGER.debug(
                "WebSocket: connected, subscribing to %d VINs",
                len(self._vins),
            )
            self._reconnect_delay = 5  # Reset backoff on successful connect
            for vin in self._vins:
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
                try:
                    context = self._vehicle_contexts.get(vin, {})
                    result = await self._client.graphql_confirm_subscription(
                        vin,
                        context.get("backdoor_type"),
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
                except Exception as e:
                    _LOGGER.debug(
                        "WebSocket: confirm subscription failed: %s", e
                    )

        elif msg_type == "data":
            payload = msg.get("payload", {}).get("data", {})
            status = payload.get("onVehicleStatusUpdated")
            if status:
                vin = status.get("vin", "")
                _LOGGER.info(
                    "WebSocket: received vehicle status for VIN ...%s "
                    "(updated: %s)",
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

        elif msg_type == "error":
            _LOGGER.debug("WebSocket error: %s", json.dumps(msg)[:500])

        elif msg_type == "ka":
            pass  # keepalive, no action needed

        elif msg_type == "connection_error":
            _LOGGER.warning(
                "WebSocket connection error: %s",
                msg.get("payload", msg),
            )

    async def _subscribe_vin(self, vin, token, guid):
        """Subscribe to vehicle status updates for a specific VIN."""
        sub_id = str(uuid.uuid4())
        self._subscriptions[vin] = sub_id
        context = self._vehicle_contexts.get(vin, {})

        subscription = {
            "id": sub_id,
            "type": "start",
            "payload": {
                "data": json.dumps(
                    {
                        "query": SUBSCRIBE_VEHICLE_STATUS,
                        "variables": {"vin": vin},
                    }
                ),
                "extensions": {
                    "authorization": {
                        "host": GRAPHQL_HOST,
                        "x-api-key": APPSYNC_API_KEY,
                        "Authorization": f"Bearer {token}",
                        "x-channel": "ONEAPP",
                        "X-BRAND": TRANSPORT_BRAND,
                        "X-APPBRAND": TRANSPORT_BRAND,
                        "x-region": context.get("region", "US"),
                        "vin": vin,
                        "x-guid": guid,
                    }
                },
            },
        }
        await self._ws.send_json(subscription)
