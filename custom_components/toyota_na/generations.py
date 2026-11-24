"""Toyota's telematics generations and how the integration reaches each one."""

from dataclasses import dataclass
from enum import Enum, unique

# Legacy generation that Toyota reports for non-connected pre-2018 vehicles.
KNOWN_UNSUPPORTED_GENERATIONS = frozenset({"PRE17CY"})


@unique
class ApiVehicleGeneration(Enum):
    CY17 = "17CY"
    CY17PLUS = "17CYPLUS"
    MM21 = "21MM"
    MM24 = "24MM"
    BEV26 = "26BEV"
    GR86 = "GR86"
    NG86 = "NG86"


@dataclass(frozen=True)
class GenerationTransport:
    """Wire transport and capability rules for one Toyota telematics generation."""

    endpoint_generation: str
    status_transport: str
    engine_transport: str | None
    refresh_transports: tuple[str, ...]
    command_transport: str
    uses_appsync: bool = False
    appsync_push: bool = False
    explicit_command_capabilities: bool = False
    allows_ev_climate_schedules: bool = True
    # The first electric API: v2 status, and a status request only after an accepted refresh.
    legacy_electric: bool = False


GENERATION_TRANSPORTS: dict[ApiVehicleGeneration, GenerationTransport] = {
    ApiVehicleGeneration.CY17: GenerationTransport(
        endpoint_generation="17CY",
        status_transport="legacy",
        engine_transport="legacy",
        refresh_transports=("legacy",),
        command_transport="legacy",
        allows_ev_climate_schedules=False,
        legacy_electric=True,
    ),
    ApiVehicleGeneration.CY17PLUS: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="global",
        engine_transport="global",
        refresh_transports=("global",),
        command_transport="global",
    ),
    ApiVehicleGeneration.MM21: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="route_21mm",
        engine_transport="route_21mm",
        refresh_transports=("graphql", "route_21mm"),
        command_transport="route_21mm",
        appsync_push=True,
    ),
    ApiVehicleGeneration.MM24: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="graphql",
        engine_transport=None,
        refresh_transports=("graphql",),
        command_transport="graphql",
        uses_appsync=True,
        appsync_push=True,
    ),
    ApiVehicleGeneration.BEV26: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="graphql",
        engine_transport=None,
        refresh_transports=("graphql",),
        command_transport="graphql",
        uses_appsync=True,
        appsync_push=True,
    ),
    ApiVehicleGeneration.NG86: GenerationTransport(
        endpoint_generation="NG86",
        status_transport="route",
        engine_transport="route",
        refresh_transports=("route",),
        command_transport="route",
        explicit_command_capabilities=True,
        allows_ev_climate_schedules=False,
    ),
    ApiVehicleGeneration.GR86: GenerationTransport(
        endpoint_generation="GR86",
        status_transport="route",
        engine_transport="route",
        refresh_transports=("route",),
        command_transport="route",
        explicit_command_capabilities=True,
    ),
}


def transport_for(generation: str | None) -> GenerationTransport | None:
    """Return the rules for Toyota's name for a generation, None for one the integration lacks."""
    try:
        return GENERATION_TRANSPORTS.get(ApiVehicleGeneration(generation))
    except ValueError:
        return None
