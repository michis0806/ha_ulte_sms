# Changelog

## 0.2.2 – 2026-11-03

- Re-authentication only uses the SSH host key whose fingerprint was shown
  and confirmed. A further key change requires another confirmation.
- Preserve separate multipart SMS when concatenation references are reused;
  never discard conflicting fragments or delete unconsumed content. Reassembly
  uses a conservative 60-second SMSC timestamp window. Ambiguous or delayed
  parts are delivered incomplete after the existing one-hour timeout.
- Reject malformed user-data headers without blocking subsequent SMS polling.
- Do not replay send, delete or host-key persistence commands after an SSH
  connection failure. Unknown outcomes are reported explicitly.
- Check every SIM deletion for a modem confirmation and account for partial
  success. Persist delivered-part fingerprints so remnants are not delivered
  again after a partial deletion, including across Home Assistant restarts.
- Redact default recipient phone numbers from diagnostic exports.
- Agent version 3; regression tests cover these fixes, including partial
  deletion recovery across Home Assistant restarts.

## 0.2.1 – 2026-11-03

- Brand icon for HACS and the integration page.
- README: why this is a separate integration (no core integration can send
  or read SMS through a UniFi LTE device). No functional changes.

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
