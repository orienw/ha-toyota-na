import logging

from toyota_na.client import ToyotaOneClient
from toyota_na.exceptions import AuthError
from toyota_na.vehicle.base_vehicle import (
    ApiVehicleGeneration,
    ToyotaVehicle,
)
from toyota_na.vehicle.vehicle_generations.seventeen_cy import SeventeenCYToyotaVehicle
from toyota_na.vehicle.vehicle_generations.seventeen_cy_plus import (
    SeventeenCYPlusToyotaVehicle,
)

from .vehicle_helpers import has_remote_subscription, is_electric_vehicle, normalize_remote_display

_LOGGER = logging.getLogger(__name__)


async def get_vehicles(client: ToyotaOneClient) -> list[ToyotaVehicle]:
    api_vehicles = await client.get_user_vehicle_list()
    supported_generations = {item.value for item in ApiVehicleGeneration}
    state_cache = getattr(client, "_vehicle_state_cache", {})
    vehicles = []

    for api_vehicle in api_vehicles or []:
        vin = api_vehicle.get("vin")
        if not vin:
            continue
        generation_name = api_vehicle.get("generation")
        if not isinstance(generation_name, str):
            continue
        generation_name = generation_name.strip().upper()
        if generation_name not in supported_generations:
            continue
        generation = ApiVehicleGeneration(generation_name)
        brand = api_vehicle.get("brand")
        region = api_vehicle.get("region")
        backdoor_type = api_vehicle.get("backdoorType")
        previous = state_cache.get(vin)
        common = {
            "client": client,
            "has_remote_subscription": has_remote_subscription(api_vehicle),
            "has_electric": is_electric_vehicle(api_vehicle),
            "model_name": api_vehicle.get("modelName")
            or (previous.model_name if previous else "Vehicle"),
            "model_year": api_vehicle.get("modelYear") or (previous.model_year if previous else ""),
            "vin": vin,
            "region": region.upper() if isinstance(region, str) and region else "US",
            "brand": brand.upper() if isinstance(brand, str) and brand else "T",
            "backdoor_type": (
                backdoor_type.lower() if isinstance(backdoor_type, str) and backdoor_type else None
            ),
            "remote_capabilities": api_vehicle.get("remoteServiceCapabilities"),
            "extended_capabilities": api_vehicle.get("extendedCapabilities"),
            "feature_flags": api_vehicle.get("features"),
            "legacy_capabilities": api_vehicle.get("capabilities"),
            "remote_display": normalize_remote_display(api_vehicle.get("remoteDisplay")),
        }

        if (
            generation == ApiVehicleGeneration.CY17PLUS
            or generation == ApiVehicleGeneration.MM21
            or generation == ApiVehicleGeneration.MM24
            or generation == ApiVehicleGeneration.BEV26
            or generation == ApiVehicleGeneration.NG86
            or generation == ApiVehicleGeneration.GR86
        ):
            vehicle = SeventeenCYPlusToyotaVehicle(generation=generation, **common)

        elif generation == ApiVehicleGeneration.CY17:
            vehicle = SeventeenCYToyotaVehicle(**common)
        else:
            continue

        if previous is not None:
            vehicle.inherit_state(previous)
        await vehicle.update()
        vehicles.append(vehicle)
        state_cache[vehicle.vin] = vehicle

    if vehicles:
        await _read_notification_history(client, vehicles)
    client._vehicle_state_cache = state_cache
    return vehicles


async def _read_notification_history(
    client: ToyotaOneClient, vehicles: list[ToyotaVehicle]
) -> None:
    """Give each vehicle its notifications, keeping the last list when the read fails."""
    try:
        history = await client.get_notification_history()
    except AuthError:
        raise
    except Exception as e:
        _LOGGER.debug("Error fetching notification history: %s", e)
        return
    if not isinstance(history, list):
        return
    items = [
        (item.get("vin") or group.get("vin"), item)
        for group in history
        if isinstance(group, dict) and isinstance(group.get("notifications"), list)
        for item in group["notifications"]
        if isinstance(item, dict)
    ]
    for vehicle in vehicles:
        # Items without a VIN are account notices, such as payments.
        vehicle.notifications = [item for vin, item in items if vin == vehicle.vin]
