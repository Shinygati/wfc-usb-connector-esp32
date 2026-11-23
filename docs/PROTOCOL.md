# Nintendo Wi-Fi USB Connector registration protocol

Source: reverse engineering of v1.07 (`rt25usbap.sys` 2.0.4.0,
"25usbap-nintendo", `WIFICON.dll`, `NintendoWFCReg.exe`). Hardware:
Buffalo `VID_0411&PID_008B`, Ralink RT2570 (802.11b/g, USB 1.1/2.0).
The USB serial contains the MAC address (`00:16:01` is a Buffalo OUI).

## Overview

Registration uses no vendor-specific IE: **its data is carried in the 32-byte SSID**.

```text
AP SSID = "NWCUSBAP" + "    " + 20 ASCII digits   (4 x "%05d" from rand())
WEP-104 = derive_key(SSID[12:32])                 (proto/wfckey.py)
```

The SSID is the shared secret: the console derives the WEP key from its digits.

## AP behavior in the original driver

- **Beacon:** hidden SSID (length 0) while `HideSSID` (`adapter+0x1c60`) is 1,
  the default value. The Privacy bit is set for WEP.
- **Probe Request** (`FUN_00023b90`): processed only if the first IE is an SSID,
  at most 32 bytes long, with `SSID[0:8] == "NWCUSBAP"`.

| Offset | Size | Probe SSID field |
|-------:|-----:|------------------|
| 0 | 8 | `NWCUSBAP` |
| 8 | 1 | Unknown |
| 9 | 1 | Flags: `& 3 != 0` means registration request |
| 10 | 1 | Unknown; stored by the driver, possibly console type |
| 11 | 1 | Unknown |
| 12 | 20 | Console name: 10 UTF-16LE code units |

If `flags & 3` is nonzero and the MAC is not in the pending list (up to 32 entries),
the driver adds `{mac[6], flags[9], b[10], name[20]}` and signals an application event.

The reply is a standard Probe Response with timestamp, interval, capabilities,
SSID, rates, DS, ERP and extended rates. The response SSID is based on the AP's
32-byte SSID:

- **Unapproved MAC:** `ssid[9] &= 0x0e`, `ssid[12:32] = 0`; the console keeps waiting.
- **Approved MAC:** `ssid[9] |= 1`, with the full digits; the console saves the SSID
  and derives the key.

**Authentication** (`FUN_00024f10`) is rejected if the MAC is not in the allowlist.
After association, normal WEP-104 is used. The original software provides IP
connectivity through ICS, with DHCP addresses in `192.168.0.x`.

## Application/driver interface

Private OIDs use `IOCTL_NDIS_QUERY_GLOBAL_STATS`.

| OID | Purpose |
|-----|---------|
| `0x0D71011C` (query) | Pending list: 32 x 0x30 bytes plus count; cleared on read |
| `0x0D71011E` / `0x0DF1011E` | Query/set allowlist: `u32 count` plus 32 MAC addresses (0xC4 bytes) |
| `0x0DF1010F` (set) | AP parameters, including HideSSID |
| `0x0DF10108` | SoftAP state: PAUSE=1 / EXECUTE=2 |
| `0x0DF10120` | Age-out timeout in seconds |

`WIFICON.dll` exports:
`SoftAP_{Initialize,Cleanup,SetSSID,GetSSID,SetKeys,SetAddressList,GetAddressList,GetClientStatus,SetSoftAPStatus,GetSoftAPStatus,SetCallback,SetAgeOutSec}`.
