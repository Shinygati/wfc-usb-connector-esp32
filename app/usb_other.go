//go:build !windows && !linux

package main

func openUSB(vid, pid uint16) (usbDev, error) { return nil, errNotFound }

func usbPresent(vid, pid uint16) bool { return false }
