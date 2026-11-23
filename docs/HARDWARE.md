# Nintendo Wi-Fi USB Connector hardware

Tested adapter: Nintendo Wi-Fi USB Connector with RT2570 rev. 5.
Identifiers specific to the individual unit are omitted.

## USB descriptors

| Field | Value |
|-------|-------|
| VID:PID | `0411:008B` (Buffalo) |
| bcdUSB / bcdDevice | 2.00 / 0.01 |
| Manufacturer / Product | `Nintendo` / `Nintendo Wi-Fi USB Connector` |
| Serial | Matches the unit's MAC address |
| Configuration | One vendor-specific interface (FF/FF/FF), 400 mA bus-powered |
| Endpoints | `0x81` bulk IN, 512 bytes; `0x01` bulk OUT, 512 bytes |

The original manual warns against connecting through USB hubs.

## Chip and EEPROM

| Field | Value |
|-------|-------|
| MAC/BBP | Ralink **RT2570**, `MAC_CSR0` = `0x0005` (revision 5) |
| RF | **RF2525E** (EEPROM word 0x0B, bits 15:11 = 5) |
| MAC | Buffalo OUI `00:16:01`; full address in EEPROM words 2-4 |
| Antenna | TX = A, RX = A (EEPROM word 0x0B) |
| TX power (EEPROM 0x1E onwards) | Channel 1 = 20, channels 2-14 = 19 |

EEPROM readout (0x6E bytes; `xx` masks the unit-specific MAC bytes):

```text
71 25 04 01 00 16 01 xx xx xx ff ff ff ff ff ff ff ff ff ff ff ff 15 28 ff ff ff 02
2c 11 18 15 18 16 00 3e ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff ff
ff ff ff ff 14 13 13 13 13 13 13 13 13 13 13 13 13 13 ff ff ff ff ff ff ff ff ff ff
ff ff ff ff ce 00 ff ff ff ff ff ff 32 00 80 68 50 38 5f 6f 37 ff 45 38 73 79
```

## EP0 vendor requests

| bRequest | Purpose | wValue / wIndex |
|---------:|---------|-----------------|
| 1 | Device mode (4 = test) | 4 / 1 |
| 2 | Single write | Value / address |
| 6 | Multi write (CSR) | 0 / offset |
| 7 | Multi read (CSR) | 0 / offset |
| 9 | Read EEPROM | 0 / 0 |
| 12 | RX control (stop) | 0 / 0 |

CSRs are 16-bit registers at `0x400-0x4FF`. BBP registers (8 bits) are accessed
through `PHY_CSR7/8`; RF registers (20 bits) use `PHY_CSR9/10`.
Bulk OUT contains a 20-byte TX descriptor followed by the frame. Bulk IN contains
the frame, FCS and a trailing 16-byte RX descriptor. OUT length must be even and
must not be a multiple of 512. Register semantics follow the public Linux
`rt2500usb` headers.

## WinUSB

See the [driver setup instructions](../driver/README.md).

## Hardware test evidence (2026-09-23)

- `python proto/diagnose.py --seconds 15` started successfully with `BBP0=0x13`
  and `MAC_CSR17=0x01fe`. It received 453 beacons and other frames on channel 6,
  then shut down cleanly.
- The initial TX descriptor confused received CCK rate units (100 kbps) with
  transmit modulation indices. TX now uses `0,1,2,3` for 1/2/5.5/11 Mbps.
  OFDM retains its rate codes and SERVICE=0x04. References:
  [Linux rate table](https://github.com/torvalds/linux/blob/master/drivers/net/wireless/ralink/rt2x00/rt2x00dev.c)
  and [PLCP construction](https://github.com/torvalds/linux/blob/master/drivers/net/wireless/ralink/rt2x00/rt2x00queue.c).
- A real console detected the connector through NWCUSBAP and advanced after
  approval. Open-system authentication (algorithm 0), accepted association and
  WEP data with valid ICV were observed. Payloads were not archived.
- Authentication requests were missing with `DROP_NOT_TO_ME` enabled but arrived
  with broad reception. MAC/BSSID register readback matched EEPROM in both
  register sets. The cause remains unresolved; the evidence does not establish
  an incorrect MAC register write.
- The first connection test ended with 52003 because DHCP was not implemented
  yet. Libslirp was then added, with a full DHCP exchange and ARP verified by a
  synthetic Ethernet client.

### Final result: connection test completed

The operator confirmed that the console completed its connection test. The run
starting at 19:54 local time showed successful shared-key authentication steps
1/3, association, a DHCP lease ending in `.10`, DNS with RCODE=0, a TCP handshake,
HTTP request/response and TCP close. The independent synthetic client received
HTTP 200 and actual content from `conntest.nintendowifi.net`; the service was not
simulated.

Additional fixes required for that result:

- `TXRX_CSR1` retained `0xb162`, with the TSF field offset incorrectly configured.
  It is now initialized explicitly to offset 24. The working path nevertheless
  uses software timestamps and disables hardware insertion to avoid corrupting
  the SSID.
- TSF synchronization modes 2 and 3 did not stabilize the hardware beacon
  generator. Mode 3 follows Linux AP configuration, while the
  [FreeBSD ural driver](https://github.com/freebsd/freebsd-src/blob/main/sys/dev/usb/wlan/if_ural.c)
  uses mode 2 for this chip. The working path enables only the TSF counter,
  disables synchronization and hardware beacon generation, and sends beacons
  through ordinary bulk OUT transfers.
- With ASIC automatic sequence numbering enabled, each new frame requires
  `NEW_SEQ`. Reused sequence numbers caused authentication retries.
- Winsock rejects `POLLPRI`; requesting it prevented TCP response polling.
  Polling now requests supported events and reports `WSAPoll` errors.
  [Microsoft reference](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/ns-winsock2-wsapollfd).
- Direct UDP DNS requests to the tested resolvers timed out. The verified path
  resolves A/AAAA through Windows without blocking the USB thread.

Validation comprises 24 automated tests (including real-DLL DHCP/ARP checks),
an independent DNS/HTTP test and the successful console connection test.
This does not establish full game compatibility, P2P NAT behavior or long-term stability.

`STA_CSR5` can change after enabling beacon functionality, but is not a reliable
measure of actual beacon transmission rate by itself. Real discovery and
association provide stronger evidence than a completed USB bulk OUT transfer.

### In-game test: Pokemon Pearl Mystery Gift (2026-09-23)

The operator tested Mystery Gift in Pokemon Pearl using the connector and
reported that the game displayed a message saying the servers were shut down.
This is a partial in-game compatibility observation following the successful
console connection test; no gift download was confirmed.

The exact error code, selected service and packet trace for this attempt were
not recorded. The message alone does not establish whether a remote server was
reached or which step failed. Community Mystery Gift and multiplayer were
unverified at that point; the later successful Mystery Gift test is recorded below.

### Successful Mystery Gift test (2026-09-25)

After the community DNS selector and upstream forwarding fix, the operator
launched `python gui.py` and confirmed that **Mystery Gift worked in Pokemon
Pearl** using the physical connector and console.

The saved desktop settings were checked after the report: channel 6 and Kaeru
WFC / Wiimmfi DNS `178.62.43.212`. The GUI process exited normally. The in-game
result is operator-reported; no packet trace or screenshot was collected.

This establishes a successful Mystery Gift flow for the tested setup, beyond
the console connection test and synthetic DNS checks. The specific gift and
console model were not recorded. It does not establish multiplayer compatibility,
long-session stability or compatibility with the other DNS services.

### Desktop session lifecycle (2026-09-23)

The optional desktop controller started the real CLI worker on channel 6,
received structured session telemetry, requested a graceful stop and observed
`RADIO OFF`. This checks radio ownership and shutdown through the desktop path;
it is not an additional console or game connectivity test.

The desktop interface shares the CLI's existing state directory and uses an
optional `--events` flag for telemetry. The ordinary CLI commands and minimal
dependencies remain supported. Automated desktop tests cover approval/revocation,
process lifecycle, closing during startup and an adapter occupied by another CLI.
