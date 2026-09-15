from toyota_na.vehicle.base_vehicle import VehicleFeatures

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPressure

from toyota_na.vehicle.base_vehicle import RemoteRequestCommand


DOMAIN = "toyota_na"

DOOR_LOCK = "door_lock"
DOOR_UNLOCK = "door_unlock"
ENGINE_START = "engine_start"
ENGINE_STOP = "engine_stop"
HAZARDS_ON = "hazards_on"
HAZARDS_OFF = "hazards_off"
VEHICLE_FINDER = "find_vehicle"
REFRESH = "refresh"
CHARGE_START = "charge_start"
CHARGE_RESUME = "charge_resume"
CHARGE_STOP = "charge_stop"

UPDATE_INTERVAL = 600
REFRESH_STATUS_INTERVAL = 2 * 3600

COMMAND_MAP = {
    DOOR_LOCK: RemoteRequestCommand.DoorLock,
    DOOR_UNLOCK: RemoteRequestCommand.DoorUnlock,
    ENGINE_START: RemoteRequestCommand.EngineStart,
    ENGINE_STOP: RemoteRequestCommand.EngineStop,
    HAZARDS_ON: RemoteRequestCommand.HazardsOn,
    HAZARDS_OFF: RemoteRequestCommand.HazardsOff,
    VEHICLE_FINDER: RemoteRequestCommand.VehicleFinder,
    REFRESH: RemoteRequestCommand.Refresh,
    CHARGE_START: RemoteRequestCommand.ChargeStart,
    CHARGE_RESUME: RemoteRequestCommand.ChargeResume,
    CHARGE_STOP: RemoteRequestCommand.ChargeStop,
}

COMMAND_BUTTONS = (
    {
        "command": RemoteRequestCommand.ChargeStart,
        "icon": "mdi:ev-station",
        "name": "Charge Now",
    },
    {
        "command": RemoteRequestCommand.ChargeResume,
        "icon": "mdi:play",
        "name": "Resume Charging",
    },
    {
        "command": RemoteRequestCommand.ChargeStop,
        "icon": "mdi:stop",
        "name": "Stop Charging",
    },
    {
        "command": RemoteRequestCommand.EngineStart,
        "icon": "mdi:engine-outline",
        "name": "Remote Start",
    },
    {
        "command": RemoteRequestCommand.EngineStop,
        "icon": "mdi:engine-off-outline",
        "name": "Remote Stop",
    },
    {
        "command": RemoteRequestCommand.HazardsOn,
        "icon": "mdi:hazard-lights",
        "name": "Flash Hazards",
    },
    {
        "command": RemoteRequestCommand.VehicleFinder,
        "icon": "mdi:map-marker-radius",
        "name": "Find Vehicle",
    },
)

COMMAND_REFRESH_DELAY = 10

BINARY_SENSORS = [
    {
        "device_class": BinarySensorDeviceClass.DOOR,
        "feature": VehicleFeatures.FrontDriverDoor,
        "icon": "mdi:car-door",
        "name": "Front Driver Door",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.DOOR,
        "feature": VehicleFeatures.FrontPassengerDoor,
        "icon": "mdi:car-door",
        "name": "Front Passenger Door",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.DOOR,
        "feature": VehicleFeatures.RearDriverDoor,
        "icon": "mdi:car-door",
        "name": "Rear Driver Door",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.DOOR,
        "feature": VehicleFeatures.RearPassengerDoor,
        "icon": "mdi:car-door",
        "name": "Rear Passenger Door",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.DOOR,
        "feature": VehicleFeatures.Hood,
        "icon": "mdi:car-door",
        "name": "Hood",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.DOOR,
        "feature": VehicleFeatures.Trunk,
        "icon": "mdi:car-door",
        "name": "Trunk",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.WINDOW,
        "feature": VehicleFeatures.Moonroof,
        "icon": "mdi:window-closed-variant",
        "name": "Moonroof",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.WINDOW,
        "feature": VehicleFeatures.FrontDriverWindow,
        "icon": "mdi:window-closed-variant",
        "name": "Front Driver Window",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.WINDOW,
        "feature": VehicleFeatures.FrontPassengerWindow,
        "icon": "mdi:window-closed-variant",
        "name": "Front Passenger Window",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.WINDOW,
        "feature": VehicleFeatures.RearDriverWindow,
        "icon": "mdi:window-closed-variant",
        "name": "Rear Driver Window",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.WINDOW,
        "feature": VehicleFeatures.RearPassengerWindow,
        "icon": "mdi:window-closed-variant",
        "name": "Rear Passenger Window",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.LOCK,
        "feature": VehicleFeatures.FrontDriverDoor,
        "icon": "mdi:car-door-lock",
        "name": "Front Driver Door Lock",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.LOCK,
        "feature": VehicleFeatures.FrontPassengerDoor,
        "icon": "mdi:car-door-lock",
        "name": "Front Passenger Door Lock",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.LOCK,
        "feature": VehicleFeatures.RearDriverDoor,
        "icon": "mdi:car-door-lock",
        "name": "Rear Driver Door Lock",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.LOCK,
        "feature": VehicleFeatures.RearPassengerDoor,
        "icon": "mdi:car-door-lock",
        "name": "Rear Passenger Door Lock",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.LOCK,
        "feature": VehicleFeatures.Trunk,
        "icon": "mdi:car-door-lock",
        "name": "Trunk Door Lock",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.RUNNING,
        "feature": VehicleFeatures.RemoteStartStatus,
        "icon": "mdi:car-hatchback",
        "name": "Remote Start",
        "electric": False,
    },
    {
        "device_class": BinarySensorDeviceClass.BATTERY_CHARGING,
        "feature": VehicleFeatures.ChargingStatus,
        "icon": "mdi:ev-station",
        "name": "Charging Status",
        "electric": True,
    },
]

SENSORS = [
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.DistanceToEmpty,
        "name": "Distance To Empty",
        "unit": "MI_OR_KM",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.FuelLevel,
        "name": "Fuel Level",
        "unit": PERCENTAGE,
        "electric": False,
    },
    {
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "icon": "mdi:counter",
        "feature": VehicleFeatures.Odometer,
        "name": "Odometer",
        "unit": "MI_OR_KM",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:counter",
        "feature": VehicleFeatures.TripDetailsA,
        "name": "Trip Details A",
        "unit": "MI_OR_KM",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:counter",
        "feature": VehicleFeatures.TripDetailsB,
        "name": "Trip Details B",
        "unit": "MI_OR_KM",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:car-tire-alert",
        "feature": VehicleFeatures.FrontDriverTire,
        "name": "Front Driver Tire",
        "unit": UnitOfPressure.PSI,
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:car-tire-alert",
        "feature": VehicleFeatures.FrontPassengerTire,
        "name": "Front Passenger Tire",
        "unit": UnitOfPressure.PSI,
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:car-tire-alert",
        "feature": VehicleFeatures.RearDriverTire,
        "name": "Rear Driver Tire",
        "unit": UnitOfPressure.PSI,
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:car-tire-alert",
        "feature": VehicleFeatures.RearPassengerTire,
        "name": "Rear Passenger Tire",
        "unit": UnitOfPressure.PSI,
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:car-tire-alert",
        "feature": VehicleFeatures.SpareTirePressure,
        "name": "Spare Tire Pressure",
        "unit": UnitOfPressure.PSI,
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:wrench-clock",
        "feature": VehicleFeatures.NextService,
        "name": "Next Service",
        "unit": "MI_OR_KM",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.ChargeDistance,
        "name": "EV Range",
        "unit": "MI_OR_KM",
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.ChargeDistanceAC,
        "name": "EV Range AC",
        "unit": "MI_OR_KM",
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.ChargeLevel,
        "name": "EV Battery Level",
        "unit": PERCENTAGE,
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.LastTimeStamp,
        "name": "Last Update Timestamp",
        "unit": "",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.LastTirePressureTimeStamp,
        "name": "Last Tire Pressure Update Timestamp",
        "unit": "",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.Speed,
        "name": "Speed",
        "unit": "km/h",
        "electric": False,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:ev-plug-type1",
        "feature": VehicleFeatures.PlugStatus,
        "name": "Plug Status",
        "unit": "",
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:clock-outline",
        "feature": VehicleFeatures.RemainingChargeTime,
        "name": "Remaining Charge Time",
        "unit": "",
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:gauge",
        "feature": VehicleFeatures.EvTravelableDistance,
        "name": "EV Travelable Distance",
        "unit": "",
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:ev-plug-type1",
        "feature": VehicleFeatures.ChargeType,
        "name": "Charge Type",
        "unit": "",
        "electric": True,
    },
    {
        "state_class": SensorStateClass.MEASUREMENT,
        "icon": "mdi:ev-plug-type1",
        "feature": VehicleFeatures.ConnectorStatus,
        "name": "Connector Status",
        "unit": "",
        "electric": True,
    },
]
