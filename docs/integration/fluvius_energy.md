# Fluvius Energy Home Assistant Integration

The Fluvius Energy custom integration authenticates against the Mijn Fluvius portal using the Azure B2C PKCE flow and exposes long-term electricity consumption/injection sensors alongside gas consumption sensors (reported in kWh) that are compatible with the Home Assistant Energy dashboard. When adding the integration you can now choose whether the entry targets an electricity or gas meter so polling cadence and parsing logic line up with Fluvius' delivery schedule.

## Configuration

1. Copy `custom_components/fluvius_energy` into your Home Assistant `config/custom_components` directory.
2. Restart Home Assistant to load the new integration.
3. Navigate to **Settings → Devices & Services → Add Integration** and search for **Fluvius Energy**.
4. Provide the same credentials you use on mijn.fluvius.be:
   - Email address
   - Password
   - EAN number
   - Meter serial number
   - Meter type (electricity or gas)
5. After the first data refresh, open the Energy dashboard configuration and map:
   - `sensor.fluvius_consumption_total` → Grid consumption
   - `sensor.fluvius_injection_total` → Return to grid (production)
   - Optional tariff-specific sensors for advanced dashboards

## Options

The integration exposes an options flow so you can fine-tune historic lookback and date handling without re-adding the entry:

- **Timezone** – IANA timezone used when requesting history (`Europe/Brussels` by default)
- **Days back** – How many days of history to query (1–31). Gas entries automatically enforce a 7-day minimum so freshly released data (which Fluvius provides with a ~72-hour delay) is captured.
- **Granularity** – Fluvius API granularity code (`3` quarter-hour, `4` daily)
- **Meter type** – Switch between electricity and gas if you migrate the entry later. This updates the config entry in addition to storing the other options.

## Diagnostics

Use **Settings → Devices & Services → Fluvius Energy → Diagnostics** to download a sanitized JSON payload containing:

- Configured EAN and meter serial
- Cached lifetime totals
- Latest day summary
- Store bookkeeping information

This helps triage support issues without revealing passwords or bearer tokens.

## Reauthentication

If your Fluvius credentials change or expire, Home Assistant will prompt you to reauthenticate. The reauth flow validates the new credentials before updating the stored config entry and reloading the integration.

## Sensors

All energy sensors report `state_class=total_increasing`, `device_class=energy`, and use kWh so they can be added to the Energy dashboard:

- Total consumption
- Consumption (high tariff)
- Consumption (low tariff)
- Total injection
- Injection (high tariff)
- Injection (low tariff)
- Net consumption for the latest day

### Gas meters

Fluvius returns two readings per gas interval: volume in m³ and energy in kWh. The integration automatically ignores the m³ duplicate and only stores the kWh values so the `sensor.fluvius_consumption_total` entity can be added directly to the Energy dashboard as a gas source. Injection metrics remain zero for gas meters because Fluvius does not expose gas injection.

Gas measurements are released more slowly than electricity (typically 72 hours). To avoid missing late-arriving datapoints the integration always requests at least seven days of history for gas entries regardless of the configured lookback in the options flow.

## Testing

Basic config-flow tests live under `tests/components/fluvius_energy/` and exercise:

- Successful user setup
- Invalid credential handling
- Reauthentication flow

Add additional tests as you expand the integration (coordinators, sensors, diagnostics, etc.).
