"""Bounded cloud reads after vehicle commands."""

import asyncio
import logging

from .exceptions import AuthError
from .patch_base_vehicle import RemoteRequestCommand

_LOGGER = logging.getLogger(__name__)
ENGINE_STATUS_INTERVAL = 20
ENGINE_STATUS_TIMEOUT = 90


def cancel_on_unload(entry, task) -> None:
    """Cancel a follow-up task when the entry unloads."""

    def cancel() -> None:
        # Home Assistant runs a truthy return value as a job, and Task.cancel returns True.
        task.cancel()

    entry.async_on_unload(cancel)


async def refresh_after_command(coordinator, vin=None, command=None, *, delay=10):
    try:
        if command in (RemoteRequestCommand.DoorLock, RemoteRequestCommand.DoorUnlock):
            for _ in range(3):
                await asyncio.sleep(delay)
                vehicle = next((item for item in coordinator.data or [] if item.vin == vin), None)
                if vehicle is None or not vehicle.can_receive_status:
                    return
                try:
                    received = await vehicle.poll_vehicle_status()
                except AuthError:
                    if coordinator.config_entry is not None:
                        coordinator.config_entry.async_start_reauth_if_available(coordinator.hass)
                    return
                except Exception as err:
                    _LOGGER.debug("Post-command vehicle status failed: %s", err)
                    continue
                vehicle = next((item for item in coordinator.data or [] if item.vin == vin), None)
                if vehicle is None:
                    return
                coordinator.async_update_listeners()
                if received and vehicle.doors_locked is (command == RemoteRequestCommand.DoorLock):
                    return
            return
        if command in (RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop):
            vehicle = next((item for item in coordinator.data or [] if item.vin == vin), None)
            if vehicle is None:
                return
            if not vehicle.uses_appsync:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + ENGINE_STATUS_TIMEOUT
                for _ in range(4):
                    await asyncio.sleep(min(ENGINE_STATUS_INTERVAL, max(0, deadline - loop.time())))
                    remaining = deadline - loop.time()
                    vehicle = next(
                        (item for item in coordinator.data or [] if item.vin == vin), None
                    )
                    if remaining <= 0 or vehicle is None or not vehicle.can_receive_status:
                        return
                    try:
                        running = await asyncio.wait_for(
                            vehicle.poll_engine_status(), min(20, remaining)
                        )
                    except AuthError:
                        await coordinator.async_request_refresh()
                        return
                    except Exception as err:
                        _LOGGER.debug("Post-command engine status failed: %s", err)
                        continue
                    coordinator.async_set_updated_data(coordinator.data)
                    if running is (command == RemoteRequestCommand.EngineStart):
                        return
                return
        await asyncio.sleep(delay)
        await coordinator.async_request_refresh()
    except Exception as err:
        _LOGGER.debug("Post-command refresh failed: %s", err)
