# Changelog

## 0.2.0 – 2026-11-02

- New: detects whether dropbear's SSH host key is stored in the device's flash.
  Firmware 7.0.110 keeps the RSA host key only in RAM, so it changes on every
  reboot. A fixable Repairs issue stores the running key in /etc/persistent
  and saves the configuration through the firmware's own save routine
  (`syswrapper.sh cfg_save_check`), then verifies the flash content.
- Agent version 2 (host key status in `info`, new mode `persist_host_key`).

## 0.1.0 – 2026-11-02

First release.

- Setup in the UI with SSH password or private key; host key confirmation
  (trust on first use), re-authentication when it changes, reconfigure.
- DHCP discovery; a configured device follows IP changes automatically.
- Option to accept changed host keys automatically (Repairs notice).
- Receive: polling of the SIM, multipart SMS joined, event entity and bus
  event `ulte_sms_received`, delete after receive (option).
- Send: action `ulte_sms.send_sms` (GSM 7-bit / Unicode, up to 10 parts,
  several recipients, response with message references), notify entity with
  default recipients.
- Sensors: RSRP, RSRQ, SINR, RSSI, operator, network type, band,
  registration, SMS on SIM, modem temperature, cell ID, own number.
- Diagnostics with secrets, IMEI and phone number redacted.
