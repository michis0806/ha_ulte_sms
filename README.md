# UniFi LTE SMS (`ulte_sms`)

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=michis0806&repository=ha_ulte_sms&category=integration)
[![Open your Home Assistant instance and start setting up this integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ulte_sms)

Home Assistant custom integration that **sends and receives SMS** through the
SIM card of a **UniFi LTE Backup Pro (U-LTE-Pro)**, and shows the modem's
**signal values** as sensors.

[Deutsche Beschreibung weiter unten.](#deutsch)

> Not affiliated with Ubiquiti. Uses undocumented device internals — a
> firmware update may break it.

## How it works

Home Assistant logs in to the device over **SSH** (the device SSH credentials
from the UniFi Network app) and talks to the Sierra Wireless modem through its
AT command port (`/dev/ttyUSB2`). A small shell script is piped over SSH on
every call; **nothing is installed or changed on the device**, so it survives
firmware updates and re-provisioning. The UniFi firmware itself uses the modem
through QMI and touches the AT port only while booting or upgrading the modem.

Incoming SMS are polled from the SIM (default every 60 s), fired as events and
then deleted from the SIM (it holds only ~20 messages).

## Tested hardware

| Device | Modem | Firmware | Status |
|---|---|---|---|
| U-LTE-Pro-EU (`ULTEPEU`) | Sierra Wireless WP7607 | 7.0.110 | works |

Other UniFi LTE devices with a Sierra WP76xx/RC76xx modem may work. Reports
are welcome. The UniFi 5G Max and the UniFi Mobile Router are different
platforms and not supported.

## Setup

1. In the UniFi Network app enable **device SSH authentication** and note
   username and password, or add an SSH public key
   (Settings → System → Advanced → Device Authentication).
2. Install via HACS (custom repository `michis0806/ha_ulte_sms`, category
   integration) and restart Home Assistant.
3. Add the integration *UniFi LTE SMS*. The device is usually discovered via
   DHCP. Enter username and password or a private key (OpenSSH/PEM, no
   passphrase), confirm the host key fingerprint.

A fixed IP is not required: when the device gets a new IP via DHCP, Home
Assistant updates the entry. The host key is still checked.

### Host key changes

Firmware 7.0.110 starts dropbear with an RSA host key that exists only in RAM
(it is missing from `/etc/persistent`), so the device presents a new key after
every reboot. The integration detects this at startup and creates a **Repairs
issue**. Fixing it copies the running key to `/etc/persistent` and lets the
firmware write its configuration to flash (`syswrapper.sh cfg_save_check`,
the routine the firmware uses itself); afterwards the flash content is read
back and checked. A factory reset removes the key again.

If the key changes anyway, the integration stops by default and asks for
re-authentication showing old and new fingerprint (strict checking). With the
option **Accept changed host keys automatically** it trusts the new key, keeps
working and creates a Repairs notice. Use that option only with key login —
with a password, a device impersonating the LTE device would receive it.

## Entities

| Entity | Description |
|---|---|
| Incoming SMS (event) | Event type `received`; attributes `sender`, `text`, `timestamp` (SMSC time), `parts`, `complete`, `receiver` |
| SMS (notify) | Sends to the default recipients from the options; the title becomes the first line |
| Signal strength (RSRP), Signal quality (RSRQ), SINR | LTE signal values (`AT!GSTATUS?`) |
| Operator, Network type, Band, Registration | `AT+COPS?`, `AT+CEREG?`, band and bandwidth |
| SMS on SIM | Messages currently stored; attributes capacity, waiting multipart parts, last delete error |
| RSSI, Modem temperature (diagnostic) | |
| Cell ID, Phone number (diagnostic, disabled) | Phone number from the SIM (`AT+CNUM`) if the operator stored it |

The device carries the LTE device's MAC address as a connection. Since Home
Assistant 2026 every integration has its own device, so it appears next to
the device of the UniFi Network integration.

## Action `ulte_sms.send_sms`

```yaml
action: ulte_sms.send_sms
data:
  recipients:
    - "+491701234567"
  message: "Power failure, UPS on battery"
```

Long texts are split into up to 10 parts. Characters outside the GSM alphabet
(emoji, many non-Latin scripts) switch the whole message to Unicode with 70
instead of 160 characters per part. The action returns the modem's message
references (`response_variable`). If several recipients are given and one
fails, the others are still sent and the action raises an error afterwards.
`config_entry_id` is only needed with more than one device.

## Event `ulte_sms_received`

Fired on the event bus for every new SMS (in addition to the event entity):

```yaml
triggers:
  - trigger: event
    event_type: ulte_sms_received
actions:
  - action: notify.mobile_app_phone
    data:
      title: "SMS from {{ trigger.event.data.sender }}"
      message: "{{ trigger.event.data.text }}"
```

Data: `config_entry_id`, `sender`, `text`, `timestamp`, `parts`, `complete`,
`receiver`. Multipart SMS are joined; if a part is still missing after one
hour, the message is delivered with `complete: false`.

## Options

| Option | Default | |
|---|---|---|
| Polling interval | 60 s | 15–3600 s, also updates the sensors |
| Delete received SMS from the SIM | on | Without deleting, the SIM fills up and the network rejects new SMS. Each message fires only once either way. |
| Default recipients | – | For the notify entity |
| Accept changed host keys automatically | off | See above |

## Troubleshooting

- *Logged in, but the LTE modem did not respond*: not a supported modem, or
  the AT port is busy (e.g. during a modem firmware upgrade).
- Debug log: `logger: logs: custom_components.ulte_sms: debug`.
- Diagnostics download redacts password, key, IMEI and phone number.

---

<a name="deutsch"></a>
## Deutsch

Benutzerdefinierte Integration für Home Assistant: **SMS senden und empfangen**
über die SIM-Karte eines **UniFi LTE Backup Pro (U-LTE-Pro)**, dazu
**Signalwerte** des Modems als Sensoren.

**Funktionsweise:** Home Assistant meldet sich per SSH mit den
Geräte-Zugangsdaten aus der UniFi Network App am Gerät an und spricht über
den AT-Port mit dem Modem. Bei jedem Aufruf wird ein kleines Skript über SSH
übergeben; auf dem Gerät wird nichts installiert oder verändert. Neue SMS
werden standardmäßig alle 60 s von der SIM abgeholt, als Event gemeldet und
danach von der SIM gelöscht.

**Einrichtung:** In der UniFi Network App die Geräte-SSH-Authentifizierung
einschalten (Einstellungen → System → Erweitert), Integration über HACS
installieren, Home Assistant neu starten, *UniFi LTE SMS* hinzufügen (wird
meist per DHCP gefunden), Passwort oder privaten Schlüssel angeben und den
Host-Key bestätigen. Eine feste IP ist nicht nötig, IP-Wechsel übernimmt
Home Assistant über die DHCP-Erkennung.

**Host-Key:** Die Firmware 7.0.110 hält den RSA-Host-Key nur im RAM; er
ändert sich bei jedem Neustart. Die Integration erkennt das und bietet unter
Reparaturen an, den Schlüssel über die Speicherroutine der Firmware dauerhaft
ins Flash zu schreiben. Ändert er sich trotzdem, fordert die Integration
standardmäßig eine erneute Bestätigung an. Mit der Option *Geänderte
Host-Keys automatisch übernehmen* arbeitet sie weiter und meldet den Wechsel
unter Reparaturen – nur mit Schlüssel-Anmeldung verwenden.

**Senden:** Aktion `ulte_sms.send_sms` (Empfänger, Nachricht) oder die
Notify-Entität *SMS* mit den Standard-Empfängern aus den Optionen.
**Empfangen:** Event-Entität *Eingehende SMS* oder Bus-Event
`ulte_sms_received` mit `sender`, `text`, `timestamp`, `parts`, `complete`.

Getestet mit U-LTE-Pro-EU (Sierra WP7607, Firmware 7.0.110). Kein Produkt
von Ubiquiti.
