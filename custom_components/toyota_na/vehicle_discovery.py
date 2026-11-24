import logging

from .client import ToyotaOneClient
from .exceptions import AuthError
from .generations import (
    GENERATION_TRANSPORTS,
    KNOWN_UNSUPPORTED_GENERATIONS,
    ApiVehicleGeneration,
)
from .vehicle import ToyotaVehicle
from .vehicle_helpers import has_remote_subscription, is_electric_vehicle, normalize_remote_display

_LOGGER = logging.getLogger(__name__)


async def get_vehicles(
    client: ToyotaOneClient,
    window_telemetry: bool = False,
    *,
    vehicles_by_vin: dict[str, ToyotaVehicle] | None = None,
    unsupported_vins: set[str] | None = None,
    ws_handler=None,
) -> list[ToyotaVehicle]:
    api_vehicles = await client.get_user_vehicle_list()
    if not isinstance(api_vehicles, list):
        # Treated as a failed read so known vehicles keep their readings.
        raise RuntimeError("Toyota returned no vehicle list.")
    supported_generations = {item.value for item in GENERATION_TRANSPORTS}
    # VINs already reported as unsupported, so each warns once per setup, not every poll.
    reported = unsupported_vins if unsupported_vins is not None else set()
    vehicles: list[ToyotaVehicle] = []

    for api_vehicle in api_vehicles:
        vin = api_vehicle.get("vin")
        if not vin:
            continue
        generation_name = api_vehicle.get("generation")
        if not isinstance(generation_name, str):
            continue
        generation_name = generation_name.strip().upper()
        if generation_name in KNOWN_UNSUPPORTED_GENERATIONS:
            _LOGGER.debug(
                "Skipping %s vehicle with legacy generation %s", vin[-4:], generation_name
            )
            continue
        if generation_name not in supported_generations:
            if generation_name and vin not in reported:
                reported.add(vin)
                _LOGGER.warning(
                    "Skipping %s %s: unsupported vehicle generation %s. Please open an issue "
                    "with the model, year and this code",
                    api_vehicle.get("modelYear") or "",
                    api_vehicle.get("modelName") or "vehicle",
                    generation_name,
                )
            continue

        generation = ApiVehicleGeneration(generation_name)
        raw_brand = api_vehicle.get("brand")
        raw_region = api_vehicle.get("region")
        raw_backdoor = api_vehicle.get("backdoorType")
        region = raw_region.upper() if isinstance(raw_region, str) and raw_region else "US"
        brand = raw_brand.upper() if isinstance(raw_brand, str) and raw_brand else "T"
        backdoor_type = (
            raw_backdoor.lower() if isinstance(raw_backdoor, str) and raw_backdoor else None
        )
        subscribed = has_remote_subscription(api_vehicle)
        electric = is_electric_vehicle(api_vehicle)
        model_name = api_vehicle.get("modelName")
        model_year = api_vehicle.get("modelYear")
        remote_display = normalize_remote_display(api_vehicle.get("remoteDisplay"))

        existing = vehicles_by_vin.get(vin) if vehicles_by_vin is not None else None
        if (
            existing is not None
            and existing.generation == generation
            and existing.region == region
            and existing.electric == electric
        ):
            existing.update_metadata(
                has_remote_subscription=subscribed,
                model_name=model_name or existing.model_name,
                model_year=model_year or existing.model_year,
                brand=brand,
                backdoor_type=backdoor_type,
                remote_capabilities=api_vehicle.get("remoteServiceCapabilities"),
                extended_capabilities=api_vehicle.get("extendedCapabilities"),
                feature_flags=api_vehicle.get("features"),
                legacy_capabilities=api_vehicle.get("capabilities"),
                remote_display=remote_display,
                window_telemetry=window_telemetry,
            )
            vehicle = existing
        else:
            vehicle = ToyotaVehicle(
                client=client,
                has_remote_subscription=subscribed,
                has_electric=electric,
                model_name=model_name or "Vehicle",
                model_year=model_year or "",
                vin=vin,
                region=region,
                generation=generation,
                brand=brand,
                backdoor_type=backdoor_type,
                remote_capabilities=api_vehicle.get("remoteServiceCapabilities"),
                extended_capabilities=api_vehicle.get("extendedCapabilities"),
                feature_flags=api_vehicle.get("features"),
                legacy_capabilities=api_vehicle.get("capabilities"),
                remote_display=remote_display,
                window_telemetry=window_telemetry,
            )

        await vehicle.update(ws_handler=ws_handler)
        vehicles.append(vehicle)

    if vehicles:
        await _read_notification_history(client, vehicles)
    if vehicles_by_vin is not None:
        vehicles_by_vin.clear()
        vehicles_by_vin.update({vehicle.vin: vehicle for vehicle in vehicles})
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
