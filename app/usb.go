package main

import (
	"errors"
	"time"
)

// usbDev is the raw USB access the RT2570 driver needs.
type usbDev interface {
	Control(reqType, req uint8, value, index uint16, data []byte, timeout time.Duration) (int, error)
	BulkOut(ep uint8, data []byte, timeout time.Duration) (int, error)
	BulkIn(ep uint8, buf []byte, timeout time.Duration) (int, error)
	Close() error
}

var errUSBTimeout = errors.New("usb timeout")
