# WFC Connector (ESP32 Supported)

An app that gets a Nintendo DS online through the Nintendo Wi-Fi USB Connector using the original adapter or either an ESP32 (ESP32, S2, S3, C3, C5, C6) chosen with the Radio dropdown option. The DS uses its
"Nintendo Wi-Fi USB Connector" option; the app does the access-point and networking tasks from the PC.

Based on [edgarburgues/wfc-usb-connector](https://github.com/edgarburgues/wfc-usb-connector)
(MIT): the SoftAP, registration, permission and WEP logic are ported from its
Python code to Go, networking uses gVisor's userspace network stack
(gvisor-tap-vsock), and the radio is an ESP32 running `firmware/esp32-radio`.

## Layout

- `app/` – Go program (Windows .exe and Linux binary from the same source)
  - `softap.go` – NWCUSBAP registration, permissions, shared-key WEP auth, association, power save, beacons
  - `wfckey.go`, `wifi.go` – SSID/WEP-104 key derivation, 802.11 + WEP helpers
  - `radio.go` – serial link to the ESP32 radio firmware
  - `rt2570.go` – userspace driver for the original connector (port of proto/rt2570.py);
    `usb_windows.go` (WinUSB via winusb.dll) and `usb_linux.go` (usbfs ioctls), no libusb/cgo
  - `rt2570_test.go` – checks every USB request against the original Python driver (`testdata/rt2570_reference.py`)
  - `network.go` – DHCP/DNS/NAT (gvisor-tap-vsock); DNS service redirect
  - `flasher.go` – built-in ESP32 ROM-bootloader flasher with chip detection
  - `web/` – the interface (served locally; WebView2 window on Windows, browser on Linux)
  - `fw/` – bundled radio firmware per chip (gzip of the merged image)
- `firmware/esp32-radio/` – Arduino sketch for the radio (ESP32, S2, S3, C3, C5, C6)
- `app/tools/pack_firmware.py` – packs compiled firmware into `app/fw/`
- `app/tools/mkres.py` – writes `rsrc_windows_amd64.syso` (exe icon, version info, DPI manifest)
- `docs/` – screenshots

## Build

```sh
cd app
python tools/mkres.py icon.ico rsrc_windows_amd64.syso 1.1.0
go test ./...
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -H windowsgui" -o WFC-Connector.exe .
GOOS=linux   GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o wfc-connector-linux-x64 .
```

Firmware (Arduino ESP32 core 3.x/4.x). `-zmuldefs` is required (it replaces the
Wi-Fi library's raw-frame filter so authentication/association replies can be sent):

```sh
arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc \
  --build-property "compiler.c.elf.extra_flags=-Wl,-zmuldefs" firmware/esp32-radio
# repeat per chip with --output-dir out/<chip>, then:
python app/tools/pack_firmware.py out
```

## Notes

- Nintendo Wi-Fi USB connector: bind WinUSB once with Zadig (USB ID 0411 008B). On Linux the kernel's
  rt2500usb driver is detached automatically; you need access to /dev/bus/usb (root or a udev rule).

- The radio uses a locally administered alias of the ESP32's MAC as BSSID; with its own
  MAC the ESP32 drops the console's WEP frames before they reach the firmware.
- Settings live in `settings.json` next to the program if that file exists,
  otherwise it's located in the user config folder (`%APPDATA%\WFC Connector`, `~/.config/WFC Connector`).
- Linux: your user needs access to the serial port (e.g. the `dialout` group). On first run the
  app adds itself (with icon) to the app menu via `~/.local/share/applications/wfc-connector.desktop`,
  and it quits when its window is closed.

## License

MIT, see `LICENSE`. Based on [wfc-usb-connector](https://github.com/edgarburgues/wfc-usb-connector) by edgarburgues (MIT).
