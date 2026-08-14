from toyota_na.client import ToyotaOneClient
from toyota_na.vehicle.base_vehicle import (
    ApiVehicleGeneration,
    ToyotaVehicle,
)
from toyota_na.vehicle.vehicle_generations.seventeen_cy import SeventeenCYToyotaVehicle
from toyota_na.vehicle.vehicle_generations.seventeen_cy_plus import (
    SeventeenCYPlusToyotaVehicle,
)

from .vehicle_helpers import has_remote_subscription, is_electric_vehicle


async def get_vehicles(client: ToyotaOneClient) -> list[ToyotaVehicle]:
    api_vehicles = await client.get_user_vehicle_list()
    supported_generations = {item.value for item in ApiVehicleGeneration}
    vehicles = []

    for api_vehicle in api_vehicles or []:
        generation_name = api_vehicle.get("generation")
        if generation_name not in supported_generations:
            continue
        generation = ApiVehicleGeneration(generation_name)
        brand = api_vehicle.get("brand")
        region = api_vehicle.get("region")
        backdoor_type = api_vehicle.get("backdoorType")
        common = {
            "client": client,
            "has_remote_subscription": has_remote_subscription(api_vehicle),
            "has_electric": is_electric_vehicle(api_vehicle),
            "model_name": api_vehicle["modelName"],
            "model_year": api_vehicle["modelYear"],
            "vin": api_vehicle["vin"],
            "region": region.upper() if isinstance(region, str) and region else "US",
            "brand": brand.upper() if isinstance(brand, str) and brand else "T",
            "backdoor_type": (
                backdoor_type.lower()
                if isinstance(backdoor_type, str) and backdoor_type
                else None
            ),
            "remote_capabilities": api_vehicle.get("remoteServiceCapabilities"),
            "extended_capabilities": api_vehicle.get("extendedCapabilities"),
        }

        if (
            generation == ApiVehicleGeneration.CY17PLUS
            or generation == ApiVehicleGeneration.MM21
            or generation == ApiVehicleGeneration.MM24
        ):
            vehicle = SeventeenCYPlusToyotaVehicle(generation=generation, **common)

        elif generation == ApiVehicleGeneration.CY17:
            vehicle = SeventeenCYToyotaVehicle(**common)
        else:
            continue

        await vehicle.update()
        vehicles.append(vehicle)

    return vehicles
