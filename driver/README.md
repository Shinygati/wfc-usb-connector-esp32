# WinUSB setup

The original driver does not support 64-bit Windows. This prototype accesses the
adapter through libusb and **WinUSB**.

1. Download [Zadig 2.9](https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe),
   signed by Akeo Consulting.
2. Run it and select **Nintendo Wi-Fi USB Connector** (USB ID `0411 008B`).
   If it is missing, enable **Options > List All Devices**.
3. Select **WinUSB** as the target driver and click **Install Driver**.

Make sure the selected device has the correct USB ID before replacing its driver.
Driver installation requires administrator approval; ordinary AP execution does not.

Check the device in PowerShell:

```powershell
Get-PnpDevice -InstanceId 'USB\VID_0411&PID_008B*'
```

Its status should be `OK`. To remove the binding, uninstall the device in Device
Manager and select the option to remove its driver software if offered.

The project does not install its own root certificates or a custom kernel driver.
