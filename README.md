# ha-toyota-na

[![Latest stable release](https://img.shields.io/github/v/release/orienw/ha-toyota-na?sort=date&style=for-the-badge&label=stable)](https://github.com/orienw/ha-toyota-na/releases/latest)

## Introduction
This is a Home Assistant integration for Toyota and Lexus connected services in
North America, maintained by [@orienw](https://github.com/orienw). It is a fork of
[widewing/ha-toyota-na](https://github.com/widewing/ha-toyota-na).

Report problems and request features in [this fork's issue tracker](https://github.com/orienw/ha-toyota-na/issues).

## Vehicle support

Supported generations: `17CY`, `17CYPLUS`, `21MM`, `24MM`, `26BEV`, and `NG86`.
`GR86` support is experimental and needs owner testing.

This fork adds:

* 24MM and 26BEV status and remote commands through Toyota's AppSync service
* 21MM remote commands through Toyota's newer command route
* NG86 vehicles, plus experimental GR86 support
* More remote controls, charging and climate settings, and schedules, offered
  only where the vehicle reports support

To check a vehicle's generation, download diagnostics from the Toyota
integration under **Settings > Devices & services**. Each vehicle's
`generation` is listed under `vehicle_list`.

## Installation
Requires Home Assistant 2026.8 or newer.

### HACS

If you already use the upstream integration, follow [Switching from upstream](#switching-from-upstream) first.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=orienw&repository=ha-toyota-na&category=integration)

Use the button above, or add the repository manually:

1. Open HACS, select the three-dot menu, then **Custom repositories**.
2. Add `https://github.com/orienw/ha-toyota-na` with type **Integration**.
3. Open this fork's entry and select **Download**. HACS installs the latest
   stable release.
4. Restart Home Assistant, then add **Toyota (North America)** under
   **Settings > Devices & services**.

To get beta releases, open this repository's device under the HACS
integration. Enable its **Pre-release** switch entity, then turn the switch
on. HACS then offers beta updates.

### Switching from upstream

This fork keeps the `toyota_na` domain and your existing account, device, and
entity IDs. Leave the Toyota entry in place under **Settings > Devices &
services** so configuration and automations stay put.

1. In **HACS**, open the downloaded entry for `widewing/ha-toyota-na` and select
   **Remove** from its three-dot menu. HACS removes the component files and
   leaves the Home Assistant data.
2. Add `https://github.com/orienw/ha-toyota-na` as a custom repository with type
   **Integration**.
3. Download this fork's latest stable release. Finish the download before you
   restart Home Assistant.
4. Restart Home Assistant, then open the Toyota integration and confirm
   vehicles and entities are still there.

Confirm HACS lists `orienw/ha-toyota-na` as downloaded. Both repositories
install to `custom_components/toyota_na`, so only one can be installed at a
time.

On vehicles with a tailgate, this integration replaces the trunk entities with
Tailgate and Tailgate Lock. Update any automations that still reference the
trunk entities.

### Manual installation

1. Download `ha_toyota_na.zip` from the [latest stable release](https://github.com/orienw/ha-toyota-na/releases/latest).
2. Extract its contents into `custom_components/toyota_na` in your Home Assistant
   configuration directory.
3. Restart Home Assistant. For a new install, add **Toyota (North America)**
   under **Settings > Devices & services**. For an existing install, keep the
   configured Toyota entry.

## Configuration
Add **Toyota (North America)** under **Settings > Devices & services**. Enter
your username and password, then the verification code sent to your email or
phone.

Apple, Google, and Facebook sign-in are not supported. If your Toyota account
uses one of them, sign out of the Toyota app and sign in with the same email
address that Apple, Google, or Facebook uses, then tap **Forgot password** to
set a password. Sign in here with that email and password.

**Cloud updates only** is the default. Home Assistant checks Toyota's cloud
every ten minutes without scheduled vehicle wakes. Vehicle readings can
remain unchanged until the vehicle contacts Toyota. **Refresh Status** and
remote commands remain available. Use **Configure** to choose an interval
if you want automatic vehicle wakes.

## Entities

Entities depend on the vehicle and the Toyota account, and not every vehicle
reports every item below. Cached readings can work without Remote Connect if
Toyota grants access. Commands and vehicle wakes need remote access.

### Sensors

* Door, window, Moonroof, Hood, Glass Hatch, and Trunk or Tailgate open state
* A lock sensor for each door, and Trunk Door Lock or Tailgate Lock
* Tire pressure and a pressure warning for each tire, including the spare
* Fuel Level, Distance To Empty, Gasoline Range, and Average and Trip Fuel
  Consumption
* Odometer, Next Service, Trip Details A and B, and Trip Count
* EV Battery Level, EV Range, EV Range AC, and EV Travelable Distance
* Plug Status, Connector Status, Charging Status, Charge Type, and Charging Rate
* Remaining Charge Time, Remaining Charge Time to 80%, and Charge Target
* Battery and Gasoline Power Supply Time
* Remote Start running state, Speed, and Remote Access
* Vehicle Alerts, Safety Recalls, Service Campaigns, Maintenance Required,
  Engine Oil, Key Fob Battery, and Software Update
* Last Update Timestamp and Last Tire Pressure Update Timestamp
* Last Parked Location, and Current Location (disabled by default)
* Charge Schedules and Climate Schedules
* Notifications event

Plug Status, Connector Status, and Remote Access show readable states.
Unrecognized values show Unknown; Toyota's original value is in `raw_value`.
Remaining Charge Time shows Unknown when Toyota has no estimate. Unplugging
the vehicle clears Charging Status. Choose km/h or mph in the Speed sensor's
settings.

### Controls

* Door lock
* Remote Start, Remote Stop, and Extend Remote Runtime. Extend Remote Runtime
  appears during an eligible remote-start session.
* Flash Hazards, Find Vehicle, Sound Horn, Turn On Headlights, and Sound Buzzer
* Open Windows, Close Windows, Close Sunroof, Lock Cargo Door, and Unlock
  Cargo Door
* Charge Now, Resume Charging, Stop Charging, and Stop Power Supply. Stop
  Power Supply appears while external power is active.
* Refresh Status
* Climate Temperature, Climate Fan Speed, Climate Airflow, and a seat climate
  setting for each seat
* Front Defroster, Rear Defroster, Steering Wheel Heat, Recirculate Air, and
  Longer Climate Runtime
* Use Climate Settings
* Charge Limit, AC Charging Current, DC Charging Power, and Power Supply
  Battery Limit
* An on/off switch for each saved charge schedule and climate schedule

The lock and command buttons are also `toyota_na` actions for scripts and
automations, such as `toyota_na.door_lock`. `toyota_na.hazards_off` turns the
hazards off and has no button.

The door lock reports the vehicle's door locks, like the Doors tile in Toyota's
app. Check cargo sensors separately in automations that monitor whether the
whole vehicle is secured; on vehicles with a tailgate, use Tailgate Lock.

### Remote access

The Remote Access sensor shows the vehicle's remote activation state in Toyota's
app, such as Active, Activation pending, or Subscription expired. When Toyota
needs you to act in its app, for example to authorize remote access or renew a
subscription, Home Assistant raises a repair. The repair clears itself once
Toyota reports the vehicle active.

While a vehicle is reported stolen, its commands and controls, including
Refresh Status and climate and charging settings, are unavailable and scheduled
wakes stop, like Toyota's app. Its last reported data, including location,
stays visible.

### Location

Current Location only takes the position from Toyota's telemetry. Last Parked
Location takes that same position and also newer ones from vehicle status, so
Current Location can lag behind it. Current Location starts disabled on newly
added vehicles. Enable it in the entity's settings if you use it.

### Vehicle health

The health sensors mirror the Health tab in Toyota's app and appear only when
Toyota offers that tab's tiles for the vehicle. Home Assistant reads them
hourly without waking the vehicle, retrying at each update until Toyota first
answers. Vehicle Alerts, Safety Recalls, and Service Campaigns count items and
list them in their attributes. Like Toyota's app, Engine Oil and Key Fob
Battery read Toyota's English status text. Software Update shows whether
Toyota offers a vehicle software update, with its name, version, and status in
attributes, on vehicles where Toyota's app checks for updates.

### Notifications

The Notifications event fires once for each new notification Toyota lists for
the vehicle, such as remote command results, service warnings, and software
updates. Its event type groups Toyota's categories, and the event data keeps
Toyota's category, title, and message. Home Assistant checks at each update
without waking the vehicle. Notifications that Toyota dates before Home
Assistant started don't fire.

### Climate and charging settings

Climate preferences for Remote Start appear under device configuration.
Remote Start runs the engine or climate system the vehicle supports.
Charging settings and buttons follow the vehicle's reported options and state.
Charge Limit remains selectable when Toyota explicitly reports support but
omits the current target; its current value stays Unknown until reported.

### Charge schedules

The Charge Schedules sensor lists saved schedules in its attributes.
`toyota_na.set_charge_schedule` creates a schedule (start, end, days of the
week) or updates named fields when you pass a schedule ID. Times are the
vehicle's local time. `toyota_na.delete_charge_schedule` removes a schedule by
ID. While eco charging is on, Toyota's app locks manual schedules, so enabling
one here with its switch or `set_charge_schedule` can conflict with eco charging.

### Climate schedules

Like Toyota's app, climate schedules are available on climate-capable 24MM and
newer vehicles and on electric vehicles other than 17CY and NG86. The Climate
Schedules sensor lists saved preconditioning reservations and their temperature
range. Use `toyota_na.set_climate_schedule` with a start time, temperature, and
either a date or repeating days to create one. Pass a schedule ID to edit
selected fields; existing seat and defroster options are preserved. Each
schedule has an on/off switch, and `toyota_na.delete_climate_schedule` removes
it.

Climate schedule dates and times use Home Assistant's configured timezone.
Toyota stores these reservations in UTC. Like Toyota's app, the Climate Schedules
sensor shows a repeating schedule at the UTC offset of the date its time was
last set, so it keeps the same displayed time after daylight saving time
changes. Check that the first run after a change starts when expected. If it
runs an hour off, set its time again. Charge schedule times continue to use the
vehicle's local time.

## Removing a vehicle

After you remove a vehicle from the Toyota account, delete its device under
**Settings > Devices & services**. The integration checks the account first.
Vehicles Toyota still lists cannot be deleted this way.

## Credits

Thanks @widewing and the upstream contributors for the [Toyota North America integration](https://github.com/widewing/ha-toyota-na).

Thanks @DurgNomis-drol for making the original [Toyota Integration](https://github.com/DurgNomis-drol/ha_toyota) and bringing up the discussion thread at https://github.com/DurgNomis-drol/mytoyota/issues/7.

Thanks @visualage for finding the way to authenticate headlessly.
