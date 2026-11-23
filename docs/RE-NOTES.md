# Reverse-engineering notes

## Source material

The archive.org item `nintendo-wifi-usb-connector` was uploaded by
OGSoftwareCollector in 2021. It contains `Nintendo_WFC_USB_1.01/1.03/1.07.zip`,
`Nintendo_WFC_1.04.zip` and the manual `wfc_usb_english.pdf`.
Versions 1.06 and 1.07 contain the same ZIP (MD5 prefix `f288bcee`).
Downloaded originals belong in `original/`, which is excluded from Git.

## Original software components (v1.07)

| File | Role |
|------|------|
| `U2G54/{Win2000,WinVista}/rt25usbap.sys` | Ralink NDIS 5.1 x86 driver, build path `C:\jan\25usbap-nintendo\`, version 2.0.4.0. Implements the registration protocol. |
| `SoftAP/*/WIFICON.dll` | `SoftAP_*` API using `IOCTL_NDIS_QUERY_GLOBAL_STATS` (0x170002) and private `0x0D71xxxx` / `0x0DF1xxxx` OIDs |
| `SoftAP/*/NintendoWFCReg.exe` | Registration UI, SSID/key generation and allowlist management |
| `ICSAPI.dll` | Internet Connection Sharing configuration |
| `XpSp2Fw.dll` | Windows XP SP2 firewall exceptions |

## Key functions

| Binary | Address | Purpose |
|--------|---------|---------|
| `rt25usbap.sys` | `0x23b90` | Probe Request handler: NWCUSBAP magic, pending list and response |
| `rt25usbap.sys` | `0x25020` | Probe SSID parsing: first IE, at most 32 bytes, 8-byte comparison against `0x30af8` |
| `rt25usbap.sys` | `0x24f10` | Authentication: rejects MAC addresses outside the allowlist |
| `rt25usbap.sys` | `0x27320` | Allowlist lookup at `adapter+0x140C` |
| `rt25usbap.sys` | `0x272b0` | Pending list lookup at `adapter+0x14D0`, 32 x 0x30 bytes |
| `rt25usbap.sys` | `0x10xxx` | OID query/set dispatcher |
| `NintendoWFCReg.exe` | `0x402550` | `srand(time)` and `sprintf("NWCUSBAP    %05d%05d%05d%05d")` |
| `NintendoWFCReg.exe` | `0x404140` | WEP-104 derivation from `SSID[12:32]` |

At `rt25usbap.sys:0x1c990`, two Nintendo MAC addresses
(`00:09:BF:08:03:71/89`) and the string `SHOYA` appear as unused local values,
likely debugging remnants.

## Reproducing the analysis

The recorded analysis used Ghidra 12.1.4 with Java 24. Install the optional Python
dependencies with `python -m pip install pefile capstone unicorn`.

Run headless analysis separately for each original binary, replacing the
placeholder paths:

```powershell
analyzeHeadless.bat re/ghidra wfc -import PATH_TO_BINARY -scriptPath re/scripts -postScript DecompileAll.java re/out -overwrite
python re/disasm.py PATH_TO_PE re/output.asm
python proto/verify_key.py PATH_TO_NintendoWFCReg.exe
```

Create `re/ghidra` and `re/out` before running Ghidra. All binary inputs and
generated output stay outside version control. The key verification compares
200 cases against the original x86 routine emulated with Unicorn.

Do not rename `re/disasm.py` to `dis.py`: that would shadow Python's standard
library module and break Capstone imports.
