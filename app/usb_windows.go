//go:build windows

package main

// WinUSB access through winusb.dll / setupapi.dll (no libusb, no cgo). The
// adapter must use the WinUSB driver (installed once with Zadig).

import (
	"errors"
	"fmt"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

var (
	setupapi                        = syscall.NewLazyDLL("setupapi.dll")
	procSetupDiGetClassDevsW        = setupapi.NewProc("SetupDiGetClassDevsW")
	procSetupDiEnumDeviceInterfaces = setupapi.NewProc("SetupDiEnumDeviceInterfaces")
	procSetupDiGetInterfaceDetailW  = setupapi.NewProc("SetupDiGetDeviceInterfaceDetailW")
	procSetupDiEnumDeviceInfo       = setupapi.NewProc("SetupDiEnumDeviceInfo")
	procSetupDiGetDeviceRegistryW   = setupapi.NewProc("SetupDiGetDeviceRegistryPropertyW")
	procSetupDiOpenDevRegKey        = setupapi.NewProc("SetupDiOpenDevRegKey")
	procSetupDiDestroyDeviceInfo    = setupapi.NewProc("SetupDiDestroyDeviceInfoList")
	advapi32                        = syscall.NewLazyDLL("advapi32.dll")
	procRegQueryValueExW            = advapi32.NewProc("RegQueryValueExW")
	procRegCloseKey                 = advapi32.NewProc("RegCloseKey")
	winusb                          = syscall.NewLazyDLL("winusb.dll")
	procWinUsbInitialize            = winusb.NewProc("WinUsb_Initialize")
	procWinUsbFree                  = winusb.NewProc("WinUsb_Free")
	procWinUsbControlTransfer       = winusb.NewProc("WinUsb_ControlTransfer")
	procWinUsbWritePipe             = winusb.NewProc("WinUsb_WritePipe")
	procWinUsbReadPipe              = winusb.NewProc("WinUsb_ReadPipe")
	procWinUsbSetPipePolicy         = winusb.NewProc("WinUsb_SetPipePolicy")
)

const (
	digcfPresent         = 0x2
	digcfAllClasses      = 0x4
	digcfDeviceInterface = 0x10
	spdrpHardwareID      = 0x1
	dicsFlagGlobal       = 1
	diregDev             = 1
	keyRead              = 0x20019
	pipeTransferTimeout  = 0x03
	errorSemTimeout      = 121
)

// GUID_DEVINTERFACE_USB_DEVICE
var guidUSBDevice = syscall.GUID{Data1: 0xA5DCBF10, Data2: 0x6530, Data3: 0x11D2, Data4: [8]byte{0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED}}

type spDevInfoData struct {
	cbSize    uint32
	classGUID syscall.GUID
	devInst   uint32
	reserved  uintptr
}

type spDevIfaceData struct {
	cbSize   uint32
	guid     syscall.GUID
	flags    uint32
	reserved uintptr
}

type winUSB struct {
	file syscall.Handle
	h    uintptr
}

// deviceInterfaceGUIDs reads the WinUSB interface GUID(s) Zadig registered for vid:pid.
func deviceInterfaceGUIDs(vidpid string) []syscall.GUID {
	h, _, _ := procSetupDiGetClassDevsW.Call(0, 0, 0, digcfPresent|digcfAllClasses)
	if h == uintptr(syscall.InvalidHandle) {
		return nil
	}
	defer procSetupDiDestroyDeviceInfo.Call(h)
	var out []syscall.GUID
	for i := 0; ; i++ {
		d := spDevInfoData{cbSize: uint32(unsafe.Sizeof(spDevInfoData{}))}
		if r, _, _ := procSetupDiEnumDeviceInfo.Call(h, uintptr(i), uintptr(unsafe.Pointer(&d))); r == 0 {
			break
		}
		buf := make([]uint16, 512)
		if r, _, _ := procSetupDiGetDeviceRegistryW.Call(h, uintptr(unsafe.Pointer(&d)), spdrpHardwareID, 0,
			uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)*2), 0); r == 0 {
			continue
		}
		if !strings.Contains(strings.ToLower(syscall.UTF16ToString(buf)), vidpid) {
			continue
		}
		key, _, _ := procSetupDiOpenDevRegKey.Call(h, uintptr(unsafe.Pointer(&d)), dicsFlagGlobal, 0, diregDev, keyRead)
		if key == uintptr(syscall.InvalidHandle) || key == 0 {
			continue
		}
		for _, name := range []string{"DeviceInterfaceGUIDs", "DeviceInterfaceGUID"} {
			n, _ := syscall.UTF16PtrFromString(name)
			val := make([]uint16, 1024)
			size := uint32(len(val) * 2)
			if r, _, _ := procRegQueryValueExW.Call(key, uintptr(unsafe.Pointer(n)), 0, 0, uintptr(unsafe.Pointer(&val[0])), uintptr(unsafe.Pointer(&size))); r == 0 {
				for _, s := range strings.Split(string(utf16Multi(val[:size/2])), "\x00") {
					if g, ok := parseGUID(s); ok {
						out = append(out, g)
					}
				}
			}
		}
		procRegCloseKey.Call(key)
	}
	return out
}

func utf16Multi(u []uint16) []rune {
	r := make([]rune, 0, len(u))
	for _, c := range u {
		r = append(r, rune(c))
	}
	return r
}

func parseGUID(s string) (syscall.GUID, bool) {
	var g syscall.GUID
	s = strings.Trim(strings.TrimSpace(s), "{}")
	var d4 [8]uint32
	n, _ := fmt.Sscanf(s, "%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
		&g.Data1, &g.Data2, &g.Data3, &d4[0], &d4[1], &d4[2], &d4[3], &d4[4], &d4[5], &d4[6], &d4[7])
	if n != 11 {
		return g, false
	}
	for i := range d4 {
		g.Data4[i] = byte(d4[i])
	}
	return g, true
}

func devicePaths(guid syscall.GUID, vidpid string) []string {
	h, _, _ := procSetupDiGetClassDevsW.Call(uintptr(unsafe.Pointer(&guid)), 0, 0, digcfPresent|digcfDeviceInterface)
	if h == uintptr(syscall.InvalidHandle) {
		return nil
	}
	defer procSetupDiDestroyDeviceInfo.Call(h)
	var out []string
	for i := 0; ; i++ {
		ifd := spDevIfaceData{cbSize: uint32(unsafe.Sizeof(spDevIfaceData{}))}
		if r, _, _ := procSetupDiEnumDeviceInterfaces.Call(h, 0, uintptr(unsafe.Pointer(&guid)), uintptr(i), uintptr(unsafe.Pointer(&ifd))); r == 0 {
			break
		}
		var need uint32
		procSetupDiGetInterfaceDetailW.Call(h, uintptr(unsafe.Pointer(&ifd)), 0, 0, uintptr(unsafe.Pointer(&need)), 0)
		if need < 8 {
			continue
		}
		buf := make([]byte, need)
		*(*uint32)(unsafe.Pointer(&buf[0])) = 8 // sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W) on x64
		if r, _, _ := procSetupDiGetInterfaceDetailW.Call(h, uintptr(unsafe.Pointer(&ifd)), uintptr(unsafe.Pointer(&buf[0])), uintptr(need), 0, 0); r == 0 {
			continue
		}
		u := unsafe.Slice((*uint16)(unsafe.Pointer(&buf[4])), (len(buf)-4)/2)
		p := syscall.UTF16ToString(u)
		if strings.Contains(strings.ToLower(p), vidpid) {
			out = append(out, p)
		}
	}
	return out
}

func openUSB(vid, pid uint16) (usbDev, error) {
	vidpid := fmt.Sprintf("vid_%04x&pid_%04x", vid, pid)
	guids := append(deviceInterfaceGUIDs(vidpid), guidUSBDevice)
	var paths []string
	for _, g := range guids {
		paths = append(paths, devicePaths(g, vidpid)...)
	}
	if len(paths) == 0 {
		return nil, errNotFound
	}
	var lastErr error
	for _, p := range paths {
		name, _ := syscall.UTF16PtrFromString(p)
		f, err := syscall.CreateFile(name, syscall.GENERIC_READ|syscall.GENERIC_WRITE, syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE,
			nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL|syscall.FILE_FLAG_OVERLAPPED, 0)
		if err != nil {
			lastErr = err
			continue
		}
		var h uintptr
		if r, _, e := procWinUsbInitialize.Call(uintptr(f), uintptr(unsafe.Pointer(&h))); r == 0 {
			syscall.CloseHandle(f)
			lastErr = errNoWinUSB
			_ = e
			continue
		}
		return &winUSB{file: f, h: h}, nil
	}
	if errors.Is(lastErr, syscall.ERROR_ACCESS_DENIED) {
		return nil, errBusy
	}
	if lastErr == nil {
		lastErr = errNoWinUSB
	}
	return nil, lastErr
}

func (w *winUSB) Control(reqType, req uint8, value, index uint16, data []byte, timeout time.Duration) (int, error) {
	// WINUSB_SETUP_PACKET is 8 bytes and passed by value (one register on x64).
	setup := uint64(reqType) | uint64(req)<<8 | uint64(value)<<16 | uint64(index)<<32 | uint64(len(data))<<48
	var n uint32
	var p uintptr
	if len(data) > 0 {
		p = uintptr(unsafe.Pointer(&data[0]))
	}
	r, _, e := procWinUsbControlTransfer.Call(w.h, uintptr(setup), p, uintptr(len(data)), uintptr(unsafe.Pointer(&n)), 0)
	if r == 0 {
		return int(n), fmt.Errorf("control transfer: %v", e)
	}
	return int(n), nil
}

func (w *winUSB) setTimeout(ep uint8, d time.Duration) {
	ms := uint32(d / time.Millisecond)
	procWinUsbSetPipePolicy.Call(w.h, uintptr(ep), pipeTransferTimeout, 4, uintptr(unsafe.Pointer(&ms)))
}

func (w *winUSB) BulkOut(ep uint8, data []byte, timeout time.Duration) (int, error) {
	w.setTimeout(ep, timeout)
	var n uint32
	r, _, e := procWinUsbWritePipe.Call(w.h, uintptr(ep), uintptr(unsafe.Pointer(&data[0])), uintptr(len(data)), uintptr(unsafe.Pointer(&n)), 0)
	if r == 0 {
		return int(n), fmt.Errorf("bulk write: %v", e)
	}
	return int(n), nil
}

func (w *winUSB) BulkIn(ep uint8, buf []byte, timeout time.Duration) (int, error) {
	w.setTimeout(ep, timeout)
	var n uint32
	r, _, e := procWinUsbReadPipe.Call(w.h, uintptr(ep), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), uintptr(unsafe.Pointer(&n)), 0)
	if r == 0 {
		if errno, ok := e.(syscall.Errno); ok && errno == errorSemTimeout {
			return 0, errUSBTimeout
		}
		return int(n), fmt.Errorf("bulk read: %v", e)
	}
	return int(n), nil
}

func (w *winUSB) Close() error {
	procWinUsbFree.Call(w.h)
	return syscall.CloseHandle(w.file)
}

// usbPresent reports whether a device with this VID:PID is plugged in (any driver).
func usbPresent(vid, pid uint16) bool {
	vidpid := fmt.Sprintf("vid_%04x&pid_%04x", vid, pid)
	h, _, _ := procSetupDiGetClassDevsW.Call(0, 0, 0, digcfPresent|digcfAllClasses)
	if h == uintptr(syscall.InvalidHandle) {
		return false
	}
	defer procSetupDiDestroyDeviceInfo.Call(h)
	for i := 0; ; i++ {
		d := spDevInfoData{cbSize: uint32(unsafe.Sizeof(spDevInfoData{}))}
		if r, _, _ := procSetupDiEnumDeviceInfo.Call(h, uintptr(i), uintptr(unsafe.Pointer(&d))); r == 0 {
			return false
		}
		buf := make([]uint16, 512)
		if r, _, _ := procSetupDiGetDeviceRegistryW.Call(h, uintptr(unsafe.Pointer(&d)), spdrpHardwareID, 0,
			uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)*2), 0); r != 0 &&
			strings.Contains(strings.ToLower(syscall.UTF16ToString(buf)), vidpid) {
			return true
		}
	}
}
