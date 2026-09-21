# ha-toyota-na

## Introduction
This is a Home Assistant integration for Toyota and Lexus connected services in
North America, maintained by [@orienw](https://github.com/orienw). It is a fork of
[widewing/ha-toyota-na](https://github.com/widewing/ha-toyota-na).

Report problems and request features in [this fork's issue tracker](https://github.com/orienw/ha-toyota-na/issues).

## Releases

[![GitHub release](https://img.shields.io/github/v/release/orienw/ha-toyota-na?include_prereleases&style=for-the-badge)](https://github.com/orienw/ha-toyota-na/releases)

## Current features

Available sensors and controls depend on the vehicle and Toyota account.
Cached readings can work without Remote Connect if Toyota grants access.
Remote commands and vehicle wake requests need remote access.

Not every vehicle reports every item below.

Sensors:

* Door Lock Status
* Window/Moonroof Status
* Trunk Status
* Vehicle Location
* Last Parked Location
* Tire Pressure
* Fuel Level
* Odometer
* Oil Status
* Key Fob Battery Status
* Last Update
* Last Tire Pressure Update
* Speed
* EV Plug Status
* EV Remaining Charge Time
* EV Travel Distance
* EV Charge Type
* EV Charge Start Time
* EV Charge End Time
* EV Connector Status
* EV Charging Status
* Charging rate, glass-hatch state, and tire-pressure warnings
* Charge target and remaining time to 80%
* Battery and gasoline power supply time
* Average and trip fuel consumption, trip count, and gasoline range
* Charge schedule count and saved schedules

Lock, remote start, hazards, and find-vehicle commands need a remote
subscription.

Services:

* Lock/Unlock Doors
* Remote Start/Stop Engine
* Hazards On/Off
* Find Vehicle
* Charge Now, Resume Charging, and Stop Charging
* Refresh Data
* Create, update, or delete multi-day charge schedules

Native controls:

* Door lock
* Remote Start and Remote Stop buttons
* Extend Remote Runtime button during an eligible remote-start session
* Flash Hazards button
* Find Vehicle button
* Horn, headlights, and buzzer buttons
* Open/close windows, close sunroof, and cargo-door controls
* Refresh Status button
* Charge Now, Resume Charging, and Stop Charging buttons
* Saved climate temperature, fan speed, airflow, and seat preferences
* Defroster, steering-wheel heat, recirculation, and longer climate runtime preferences
* Use Climate Settings switch
* Charge limit, AC current, DC power, and power supply battery limit
* Stop Power Supply button, while external power is active
* Enable/disable switches for saved multi-day charge schedules

Climate preferences for Remote Start appear under device configuration.
Remote Start runs the engine or climate system the vehicle supports.
Charging settings and buttons follow the vehicle's reported options and state.

The Charge Schedules sensor lists saved schedules in its attributes.
`toyota_na.set_charge_schedule` creates a schedule (start, end, days of the
week) or updates named fields when you pass a schedule ID. Times are the
vehicle's local time. `toyota_na.delete_charge_schedule` removes a schedule by
ID.

Supported generations: `17CY`, `17CYPLUS`, `21MM`, `24MM`, `26BEV`, and `NG86`.

### Sensor display

Plug Status and Connector Status show readable charging and connection states.
Unrecognized values show Unknown; Toyota's original value is in `raw_value`.
Last Update Timestamp and Last Tire Pressure Update Timestamp show dates and
times. Choose km/h or mph in the Speed sensor's settings.

Remaining Charge Time shows Unknown when Toyota has no estimate. Unplugging
the vehicle clears Charging Status.

### Removing a vehicle

After you remove a vehicle from the Toyota account, delete its device under
**Settings > Devices & services**. The integration checks the account first.
Vehicles Toyota still lists cannot be deleted this way.

## Installation
Requires Home Assistant 2024.11 or newer.

### HACS

If you already use the upstream integration, follow [Switching from upstream](#switching-from-upstream) first.

1. Open HACS, select the three-dot menu, then **Custom repositories**.
2. Add `https://github.com/orienw/ha-toyota-na` with type **Integration**.
3. Open this fork's entry and select **Download**. Choose the latest version on
   the [releases page](https://github.com/orienw/ha-toyota-na/releases).
   If that release is a prerelease, enable beta versions in HACS.
4. Restart Home Assistant, then add **Toyota (North America)** under
   **Settings > Devices & services**.

### Switching from upstream

This fork keeps the `toyota_na` domain and your existing account, device, and
entity IDs. Leave the Toyota entry in place under **Settings > Devices &
services** so configuration and automations stay put.

1. In **HACS**, open the downloaded entry for `widewing/ha-toyota-na` and select
   **Remove** from its three-dot menu. HACS removes the component files and
   leaves the Home Assistant data.
2. Add `https://github.com/orienw/ha-toyota-na` as a custom repository with type
   **Integration**.
3. Download this fork's latest release, enabling beta versions if needed.
   Finish the download before you restart Home Assistant.
4. Restart Home Assistant, then open the Toyota integration and confirm
   vehicles and entities are still there.

Confirm HACS lists `orienw/ha-toyota-na` as downloaded. Both repositories
install to `custom_components/toyota_na`, so only one can be installed at a
time.

This integration removes obsolete trunk entities on vehicles with a tailgate.
Update any automations that still reference those entities.

### Manual installation

1. Download `ha_toyota_na.zip` from the latest release on the [releases page](https://github.com/orienw/ha-toyota-na/releases).
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

Use **Configure** to choose how vehicle status is updated. Home Assistant
always reads Toyota's cloud data. That data stays unchanged until the
vehicle contacts Toyota. Choose **Cloud updates only** to skip scheduled
wakes. Refresh Status and remote commands stay available. Pick an interval
to wake the vehicle.

![image](https://user-images.githubusercontent.com/4755389/147372481-4d280b6e-6f61-434c-a768-f4a089f009c3.png)

## Credits

Thanks @widewing and the upstream contributors for the [Toyota North America integration](https://github.com/widewing/ha-toyota-na).

Thanks @DurgNomis-drol for making the original [Toyota Integration](https://github.com/DurgNomis-drol/ha_toyota) and bringing up the discussion thread at https://github.com/DurgNomis-drol/mytoyota/issues/7.

Thanks @visualage for finding the way to authenticate headlessly.
