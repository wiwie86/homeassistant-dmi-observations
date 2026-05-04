# DMI Observation for Home Assistant

Home Assistant custom integration for DMI Open Data observations.

[![Open your Home Assistant instance and open the custom repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=wiwie86&repository=homeassistant-dmi-observations&category=integration)

Features:
- UI-based setup with latitude/longitude search and distance-sorted station selection
- observation sensors without `command_line` or template sensor setup
- precipitation sensors for now, 10 minutes, 1 hour, 3 hours, 6 hours, 12 hours, and 24 hours
- configurable polling interval with a hard minimum of 10 minutes

Requirements:
- Home Assistant `2025.1.0` or newer

Project layout:
- `custom_components/dmi_observation/` contains the integration code

HACS installation:
1. Open HACS and add `https://github.com/wiwie86/homeassistant-dmi-observations` as a custom repository of type `Integration`.
2. Install `DMI Observation` from HACS.
3. Restart Home Assistant.
4. Add the integration from `Settings -> Devices & Services`.

Local installation:
1. Copy the integration folder into your Home Assistant config:

```bash
cp -r /home/chris/git/dmi_observation_ha/custom_components/dmi_observation /home/chris/git/homeassistant/config/custom_components/
```

2. Restart Home Assistant.
3. Go to `Settings -> Devices & Services -> Add Integration`.
4. Search for `DMI Observation`.
5. Enter latitude and longitude.
6. Select the nearest active DMI station from the distance-sorted list.

Notes:
- uses the unauthenticated DMI Open Data endpoint `opendataapi.dmi.dk`
- polling defaults to 600 seconds and is clamped to a minimum of 600 seconds
- stations expose different parameter sets, so some entities may stay unavailable depending on the station
