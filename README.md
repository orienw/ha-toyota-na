# Toyota (North America) for Home Assistant

[![Latest stable release](https://img.shields.io/github/v/release/orienw/ha-toyota-na?sort=date&style=for-the-badge&label=stable)](https://github.com/orienw/ha-toyota-na/releases/latest)

Home Assistant integration for Toyota and Lexus Connected Services in North
America, maintained by [@orienw](https://github.com/orienw).

This is a fork of
[widewing/ha-toyota-na](https://github.com/widewing/ha-toyota-na). If you are
already running upstream, switching keeps your account, devices, entity IDs,
and automations (except trunk entities on vehicles with a tailgate). See
[Switching from upstream](#switching-from-upstream).

## What this fork adds

* Support for `26BEV` and `NG86` vehicles, plus experimental `GR86` support
* `24MM` status and remote commands via Toyota's AppSync service, and `21MM`
  remote commands via Toyota's newer command route
* Button entities for remote commands (Remote Start, Flash Hazards, Find
  Vehicle, and more)
* Remote Start climate preferences, charging settings, and charge and climate
  schedules
* Vehicle health, recalls, software updates, and a Notifications event entity
* Remote Access status, with automatic repairs when Toyota needs you to act in
  its app
* Cloud-only polling by default, so Home Assistant does not wake the vehicle on
  a schedule

## Vehicle support

Toyota groups connected vehicles by internal telematics generation codes. This
integration supports:

* **Supported:** `17CY`, `17CYPLUS`, `21MM`, `24MM`, `26BEV`, `NG86`
* **Experimental:** `GR86` (needs owner testing)

Most North American Toyota and Lexus models with Connected Services fall into
one of these generations. Once installed, you can verify your vehicle's code by
downloading diagnostics under **Settings > Devices & services** and checking
`generation` under `vehicle_list`. Vehicles with an unrecognized generation are
skipped, and the Home Assistant log records the vehicle and its generation code.

## Installation

Requires Home Assistant **2026.8** or newer.

### HACS (recommended)

Already using `widewing/ha-toyota-na`? Follow
[Switching from upstream](#switching-from-upstream) first.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=orienw&repository=ha-toyota-na&category=integration)

Or add it manually:

1. Open **HACS**, open the three-dot menu, and select **Custom repositories**.
2. Add `https://github.com/orienw/ha-toyota-na` with type **Integration**.
3. Open this repository in HACS and select **Download** to install the latest
   stable release.
4. Restart Home Assistant, then add **Toyota (North America)** under
   **Settings > Devices & services**.

**Beta releases:** Open this repository's device under the HACS integration,
enable its **Pre-release** switch entity, and turn it on.

### Switching from upstream

This fork uses the same `toyota_na` domain, so your account, devices, and
entity IDs carry over. **Do not delete** your existing Toyota entry under
**Settings > Devices & services**.

1. In **HACS**, open `widewing/ha-toyota-na` and choose **Remove** from its
   three-dot menu. This removes the integration files without touching your
   Home Assistant data.
2. Add `https://github.com/orienw/ha-toyota-na` as a custom repository (type
   **Integration**).
3. Download the latest stable release **before** restarting Home Assistant.
4. Restart Home Assistant and verify your vehicles and entities are intact.

Confirm HACS shows `orienw/ha-toyota-na` as downloaded. Both repositories
install to `custom_components/toyota_na`, so only one can be installed at a
time.

> **Tailgate migration note:** On vehicles with a tailgate, this fork replaces
> trunk entities with **Tailgate** and **Tailgate Lock**. Update any
> automations that still reference the old trunk entities.

### Manual installation

1. Download `ha_toyota_na.zip` from the
   [latest stable release](https://github.com/orienw/ha-toyota-na/releases/latest).
2. Extract it into `custom_components/toyota_na` inside your Home Assistant
   configuration directory.
3. Restart Home Assistant. For a new install, add **Toyota (North America)**
   under **Settings > Devices & services**. For an existing install, leave the
   configured entry in place.

## Configuration

Add **Toyota (North America)** under **Settings > Devices & services**. Sign in
with your Toyota or Lexus username and password, then enter the verification
code sent to your email or phone.

**Apple, Google, or Facebook accounts:** Social sign-in is not supported. Sign
out of the Toyota or Lexus mobile app, start signing in with the same email
address your social account uses, and tap **Forgot password** to set a
password. Use that email and password here.

### Options

Open **Configure** on the integration entry to adjust polling and telemetry
behavior:

* **Wake vehicle for fresh status** (default: **Cloud updates only**): Home
  Assistant polls Toyota's cloud every 10 minutes without waking the vehicle.
  Readings stay at whatever the vehicle last sent to Toyota; **Refresh Status**
  and remote commands still work on demand. Pick a wake interval (every 2, 6,
  12, or 24 hours) if you want automatic vehicle wakes.
* **Use telemetry for windows and moonroof** (default: off): Some vehicles only
  report window and moonroof states in telemetry. Turn this on if those sensors
  are unavailable. Telemetry can lag behind actual state, so vehicle status
  readings still take precedence when available.

## Entities

Available entities depend on the vehicle and account. Cached status readings
can work without a Remote Connect subscription if Toyota grants access; remote
commands and vehicle wakes require active remote access.

### Sensors

* **Closures & locks:** Doors, windows, Moonroof, Hood, Glass Hatch, and Trunk
  or Tailgate open state; per-door lock sensors plus Trunk Door Lock or
  Tailgate Lock
* **Fuel & driving:** Fuel Level, Distance To Empty, Gasoline Range, Average
  and Trip Fuel Consumption, Odometer, Next Service, Trip Details A and B, Trip
  Count, Speed (choose `km/h` or `mph` in the entity's settings), and Remote
  Start running state
* **Tires:** Pressure and pressure warning for each tire, including the spare
* **EV & charging:** EV Battery Level, EV Range, EV Range AC, Plug Status,
  Connector Status, Charging Status, Charge Type, Charging Rate, Remaining
  Charge Time, Remaining Charge Time to 80%, Charge Target, Battery and
  Gasoline Power Supply Time, Charge Schedules, and Climate Schedules
* **Health & status:** Remote Access, Vehicle Alerts, Safety Recalls, Service
  Campaigns, Maintenance Required, Engine Oil, Key Fob Battery, Software
  Update, and Notifications event
* **Location & timestamps:** Last Parked Location, Current Location (disabled
  by default), Last Update Timestamp, and Last Tire Pressure Update Timestamp

A few sensor details:

* **Plug Status**, **Connector Status**, and **Remote Access** map Toyota's
  codes to readable states. Unrecognized values report `Unknown` with Toyota's
  original value preserved in the `raw_value` attribute.
* **Remaining Charge Time** reports `Unknown` when Toyota has no estimate.
* Unplugging the vehicle clears **Charging Status**.

### Controls

* **Locks & closures:** Door lock, Lock Cargo Door, Unlock Cargo Door, Open
  Windows, Close Windows, Close Sunroof
* **Remote start & alerts:** Remote Start, Remote Stop, Extend Remote Runtime
  (appears during an eligible remote-start session), Flash Hazards, Find
  Vehicle, Sound Horn, Turn On Headlights, Sound Buzzer, and Refresh Status
* **Climate preferences** (under device configuration): Climate Temperature,
  Climate Fan Speed, Climate Airflow, per-seat climate settings, Front
  Defroster, Rear Defroster, Steering Wheel Heat, Recirculate Air, Longer
  Climate Runtime, and Use Climate Settings
* **Charging & power supply:** Charge Now, Resume Charging, Stop Charging, Stop
  Power Supply (appears while external power is active), Charge Limit, AC
  Charging Current, DC Charging Power, and Power Supply Battery Limit
* **Schedules:** An on/off switch for each saved charge schedule and climate
  schedule

The **Door lock** entity mirrors the Doors tile in Toyota's app and does not
include the trunk or tailgate. If an automation checks whether the whole vehicle
is secured, include the cargo lock sensor separately (**Tailgate Lock** on
vehicles with a tailgate).

## Actions

Every lock and remote command button is also available as a `toyota_na` action
for scripts and automations (for example, `toyota_na.door_lock`,
`toyota_na.engine_start`, `toyota_na.refresh`). Actions without a corresponding
button:

| Action | Purpose |
| --- | --- |
| `toyota_na.hazards_off` | Turn hazard lights off. |
| `toyota_na.set_charge_schedule` | Create a charge schedule (`start_time`, `end_time`, `days`) or update specific fields by passing `schedule_id`. Times use the vehicle's local time. |
| `toyota_na.delete_charge_schedule` | Remove a charge schedule by `schedule_id`. |
| `toyota_na.set_climate_schedule` | Create a preconditioning schedule (`start_time`, `temperature`, and either `date` or `days`) or update specific fields by passing `schedule_id` (existing seat and defroster options are preserved). Uses Home Assistant's timezone. |
| `toyota_na.delete_climate_schedule` | Remove a climate schedule by `schedule_id`. |

## Feature notes & known behaviors

### Remote access and stolen vehicles

The **Remote Access** sensor reflects the vehicle's remote activation state in
Toyota's app (such as `Active`, `Activation pending`, or `Subscription
expired`). When Toyota requires action in its app, such as authorizing remote
access or renewing a subscription, Home Assistant raises a repair that clears
automatically once the vehicle reports active.

If Toyota reports a vehicle as stolen, all commands and controls (including
**Refresh Status** and climate/charging settings) become unavailable and
scheduled wakes stop, matching Toyota's app. The last reported data, including
location, stays visible.

### Location

**Current Location** only uses coordinates from Toyota's telemetry. **Last
Parked Location** uses telemetry as well as newer coordinates from vehicle
status, so **Current Location** can lag behind it. **Current Location** starts
disabled on newly added vehicles; enable it in the entity's settings if you want
it.

### Vehicle health and software updates

Health sensors mirror the Health tab in Toyota's app and only appear when
Toyota provides those tiles for the vehicle. Home Assistant reads them hourly
without waking the vehicle, retrying on each update until Toyota first
responds.

* **Vehicle Alerts**, **Safety Recalls**, and **Service Campaigns** report item
  counts and list details in their attributes.
* **Engine Oil** and **Key Fob Battery** show Toyota's English status text, as
  in Toyota's app.
* **Software Update** shows whether Toyota offers a vehicle software update
  (with name, version, and status in attributes) on vehicles where Toyota's app
  checks for updates.

### Notifications

The **Notifications** event fires once for each new vehicle notification Toyota
lists (remote command results, service warnings, software updates, etc.). Its
`event_type` groups Toyota's categories, and the event data keeps Toyota's
category, title, and message. Home Assistant checks on each update without
waking the vehicle. Notifications dated before Home Assistant started do not
fire.

### Climate and charging settings

**Remote Start** starts whichever engine or climate system the vehicle
supports, using the climate preferences under device configuration. Charging
settings and buttons follow the vehicle's reported capabilities and state. If
Toyota reports **Charge Limit** support without sending a current target, the
control stays selectable and displays `Unknown` until Toyota reports a value.

### Charge schedules

The **Charge Schedules** sensor lists saved schedules and their IDs in its
attributes. While eco charging is turned on, Toyota's app locks manual
schedules, so toggling a schedule switch or calling
`toyota_na.set_charge_schedule` can conflict with eco charging.

### Climate schedules

Climate schedules are available on climate-capable `24MM` and newer vehicles,
and on electric vehicles other than `17CY` and `NG86`. The **Climate Schedules**
sensor lists saved reservations, their IDs, and the supported temperature range.

**Daylight saving time:** Toyota stores climate reservations in UTC. Like
Toyota's app, the sensor displays a repeating schedule at the UTC offset from
when its time was last set, so the shown time does not shift after a DST
change. Check that the first run after a DST change starts when expected; if it
is off by an hour, set its time again. Charge schedules always use the
vehicle's local time.

## Removing a vehicle

Remove the vehicle from your Toyota or Lexus account first, then delete its
device under **Settings > Devices & services**. The integration checks your
account before deleting and will refuse to remove vehicles Toyota still lists.

## Reporting issues

Report bugs and request features in
[this fork's issue tracker](https://github.com/orienw/ha-toyota-na/issues). For
bug reports and unsupported vehicles, include your vehicle's model, year, and
generation code, and attach diagnostics from **Settings > Devices & services >
Toyota (North America) > Download diagnostics** (credentials, VINs, and
coordinates are automatically redacted).

## Credits

* [@widewing](https://github.com/widewing) and upstream contributors for the
  [Toyota North America integration](https://github.com/widewing/ha-toyota-na)
* [@DurgNomis-drol](https://github.com/DurgNomis-drol) for the original
  [Toyota Integration](https://github.com/DurgNomis-drol/ha_toyota) and the
  [discussion thread](https://github.com/DurgNomis-drol/mytoyota/issues/7)
* [@visualage](https://github.com/visualage) for figuring out headless
  authentication
