# ha-toyota-na

## Introduction
This is a Home Assistant integration for Toyota and Lexus connected services in
North America, maintained by [@orienw](https://github.com/orienw). It is a fork of
[widewing/ha-toyota-na](https://github.com/widewing/ha-toyota-na).

Report problems and request features in [this fork's issue tracker](https://github.com/orienw/ha-toyota-na/issues).

## Releases

[![GitHub release](https://img.shields.io/github/v/release/orienw/ha-toyota-na?include_prereleases&style=for-the-badge)](https://github.com/orienw/ha-toyota-na/releases)

## Current features
Certain entities and services require the Remote Subscription.

Sensors:
* Door lock status (Remote Subscription Required)
* Window/Moonroof status (Remote Subscription Required)
* Trunk Status (Remote Subscription Required)
* Real time location (Remote Subscription Required)
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
* Charge target and remaining time to 80%, when reported
* Battery and gasoline power supply time, when reported
* Average and trip fuel consumption, trip count, and gasoline range, when reported

Services:
* Lock/Unlock Doors (Remote Subscription Required)
* Remote Start/Stop Engine (Remote Subscription Required)
* Hazards On/Off (Remote Subscription Required)
* Find Vehicle (Remote Subscription and reported vehicle support required)
* Charge Now, Resume Charging, and Stop Charging, when available
* Refresh Data

Native controls:
* Door lock
* Remote Start and Remote Stop buttons
* Flash Hazards button
* Find Vehicle button, when reported supported
* Horn, headlights, and buzzer buttons, when supported
* Open/close windows, close sunroof, and cargo-door controls, when supported
* Refresh Status button
* Charge Now, Resume Charging, and Stop Charging buttons, when available
* Saved climate temperature, fan speed, airflow, and seat preferences, when supported
* Defroster, steering-wheel heat, recirculation, and longer climate runtime preferences, when supported
* Use Climate Settings switch, when supported
* Charge limit, AC current, DC power, and power supply battery limit, when reported
* Stop Power Supply button, while external power is active

Climate controls save preferences for Remote Start and appear under device
configuration. Remote Start runs the engine or climate system supported by the
vehicle. Charging settings use the choices reported by the vehicle. Charging
buttons become available according to the vehicle's reported charging state.

Supported connected-vehicle generations are `17CY`, `17CYPLUS`, `21MM`, `24MM`,
and `26BEV`. Cached readings can remain available without Remote Connect when Toyota
grants the account access to that data. Commands and vehicle wake requests
require the appropriate remote access.

## Installation
Requires Home Assistant 2022.11 or newer.

### HACS

If you already use the upstream integration, follow [Switching from upstream](#switching-from-upstream) first.

1. Open HACS, select the three-dot menu, then **Custom repositories**.
2. Add `https://github.com/orienw/ha-toyota-na` with type **Integration**.
3. Open this fork's entry and select **Download**. Choose the latest version on
   the [releases page](https://github.com/orienw/ha-toyota-na/releases).
   Enable beta versions in HACS if that release is a prerelease.
4. Restart Home Assistant, then add **Toyota (North America)** under
   **Settings > Devices & services**.

### Switching from upstream

This fork uses the same `toyota_na` integration domain and existing account,
device, and entity identifiers. Keep your Toyota integration entry under
**Settings > Devices & services** so its configuration and automations can be reused.

1. In **HACS**, open the downloaded entry for `widewing/ha-toyota-na` and select
   **Remove** from its three-dot menu. HACS removes the component files while
   keeping the related Home Assistant data.
2. Add `https://github.com/orienw/ha-toyota-na` as a custom repository with type
   **Integration**.
3. Download this fork's latest release, enabling beta versions if needed.
   Complete the download before restarting Home Assistant.
4. Restart Home Assistant and open your existing Toyota integration to check its
   vehicles and entities.

Confirm HACS lists `orienw/ha-toyota-na` as downloaded. Both repositories install
to `custom_components/toyota_na`, so only one can be installed at a time.

This integration removes obsolete trunk entities on vehicles with a tailgate.
Update any automations that still reference those entities.

### Manual installation

1. Download `ha_toyota_na.zip` from the latest release on the [releases page](https://github.com/orienw/ha-toyota-na/releases).
2. Extract its contents into `custom_components/toyota_na` in your Home Assistant
   configuration directory.
3. Restart Home Assistant. For a new installation, add **Toyota (North America)**
   under **Settings > Devices & services**. For an existing installation, keep
   using the configured Toyota entry.

## Configuration
Click "Add integration" from Home Assistant, search "Toyota (North America)", click to add.

Enter your username and password, and then OTP for Toyota One App or Toyota Entune App and all set.

After setting up, Most information in Toyota One app should be available in Home Assistant.

Use the integration's Configure action to choose how vehicle status is updated.
Home Assistant keeps checking Toyota's existing cloud data regardless of this
setting. Cloud updates only disables scheduled wake requests while keeping
remote commands and the Refresh Status button available. Choose an interval only
when you want Home Assistant to wake the vehicle proactively for fresh status.

![image](https://user-images.githubusercontent.com/4755389/147372481-4d280b6e-6f61-434c-a768-f4a089f009c3.png)

## Credits

Thanks @widewing and the upstream contributors for the [Toyota North America integration](https://github.com/widewing/ha-toyota-na).

Thanks @DurgNomis-drol for making the original [Toyota Integration](https://github.com/DurgNomis-drol/ha_toyota) and bringing up the discussion thread at https://github.com/DurgNomis-drol/mytoyota/issues/7.

Thanks @visualage for finding the way to authenticate headlessly.
