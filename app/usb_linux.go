//go:build linux

package main

// usbfs access (/dev/bus/usb) with plain ioctls — no libusb, no cgo.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

const (
	usbdevfsControl          = 0xC0185500
	usbdevfsBulk             = 0xC0185502
	usbdevfsSetConfiguration = 0x80045505
	usbdevfsClaimInterface   = 0x8004550F
	usbdevfsReleaseInterface = 0x80045510
	usbdevfsIoctl            = 0xC0105512
	usbdevfsDisconnect       = 0x5516
)

type ctrlTransfer struct {
	reqType uint8
	req     uint8
	value   uint16
	index   uint16
	length  uint16
	timeout uint32
	_       uint32
	data    uintptr
}

type bulkTransfer struct {
	ep      uint32
	length  uint32
	timeout uint32
	_       uint32
	data    uintptr
}

type usbIoctl struct {
	ifno int32
	code int32
	data uintptr
}

type usbfs struct{ fd int }

func sysfsRead(dir, name string) string {
	b, _ := os.ReadFile(filepath.Join(dir, name))
	return strings.TrimSpace(string(b))
}

func openUSB(vid, pid uint16) (usbDev, error) {
	dirs, _ := filepath.Glob("/sys/bus/usb/devices/*")
	for _, d := range dirs {
		if sysfsRead(d, "idVendor") != fmt.Sprintf("%04x", vid) || sysfsRead(d, "idProduct") != fmt.Sprintf("%04x", pid) {
			continue
		}
		bus, _ := strconv.Atoi(sysfsRead(d, "busnum"))
		dev, _ := strconv.Atoi(sysfsRead(d, "devnum"))
		path := fmt.Sprintf("/dev/bus/usb/%03d/%03d", bus, dev)
		fd, err := unix.Open(path, unix.O_RDWR|unix.O_CLOEXEC, 0)
		if err != nil {
			if errors.Is(err, unix.EACCES) || errors.Is(err, unix.EPERM) {
				return nil, fmt.Errorf("no permission for %s: run as root once, or add a udev rule for 0411:008b", path)
			}
			return nil, err
		}
		u := &usbfs{fd: fd}
		// Detach the kernel's rt2500usb driver if it grabbed the adapter.
		dc := usbIoctl{ifno: 0, code: usbdevfsDisconnect}
		unix.Syscall(unix.SYS_IOCTL, uintptr(fd), usbdevfsIoctl, uintptr(unsafe.Pointer(&dc)))
		cfg := uint32(1)
		unix.Syscall(unix.SYS_IOCTL, uintptr(fd), usbdevfsSetConfiguration, uintptr(unsafe.Pointer(&cfg)))
		ifc := uint32(0)
		if _, _, e := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), usbdevfsClaimInterface, uintptr(unsafe.Pointer(&ifc))); e != 0 {
			unix.Close(fd)
			if e == unix.EBUSY {
				return nil, errBusy
			}
			return nil, fmt.Errorf("claim interface: %v", e)
		}
		return u, nil
	}
	return nil, errNotFound
}

func (u *usbfs) Control(reqType, req uint8, value, index uint16, data []byte, timeout time.Duration) (int, error) {
	c := ctrlTransfer{reqType: reqType, req: req, value: value, index: index, length: uint16(len(data)), timeout: uint32(timeout / time.Millisecond)}
	if len(data) > 0 {
		c.data = uintptr(unsafe.Pointer(&data[0]))
	}
	n, _, e := unix.Syscall(unix.SYS_IOCTL, uintptr(u.fd), usbdevfsControl, uintptr(unsafe.Pointer(&c)))
	runtime.KeepAlive(data)
	if e != 0 {
		return 0, fmt.Errorf("control transfer: %v", e)
	}
	return int(n), nil
}

func (u *usbfs) bulk(ep uint8, data []byte, timeout time.Duration) (int, error) {
	b := bulkTransfer{ep: uint32(ep), length: uint32(len(data)), timeout: uint32(timeout / time.Millisecond), data: uintptr(unsafe.Pointer(&data[0]))}
	n, _, e := unix.Syscall(unix.SYS_IOCTL, uintptr(u.fd), usbdevfsBulk, uintptr(unsafe.Pointer(&b)))
	runtime.KeepAlive(data)
	if e == unix.ETIMEDOUT {
		return 0, errUSBTimeout
	}
	if e != 0 {
		return 0, fmt.Errorf("bulk transfer: %v", e)
	}
	return int(n), nil
}

func (u *usbfs) BulkOut(ep uint8, data []byte, timeout time.Duration) (int, error) {
	return u.bulk(ep, data, timeout)
}
func (u *usbfs) BulkIn(ep uint8, buf []byte, timeout time.Duration) (int, error) {
	return u.bulk(ep, buf, timeout)
}

func (u *usbfs) Close() error {
	ifc := uint32(0)
	unix.Syscall(unix.SYS_IOCTL, uintptr(u.fd), usbdevfsReleaseInterface, uintptr(unsafe.Pointer(&ifc)))
	return unix.Close(u.fd)
}

func usbPresent(vid, pid uint16) bool {
	dirs, _ := filepath.Glob("/sys/bus/usb/devices/*")
	for _, d := range dirs {
		if sysfsRead(d, "idVendor") == fmt.Sprintf("%04x", vid) && sysfsRead(d, "idProduct") == fmt.Sprintf("%04x", pid) {
			return true
		}
	}
	return false
}
