//go:build linux

package main

import (
	"bytes"
	"os"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestRadioProtocol(t *testing.T) {
	m, slave := openPTY(t)
	defer m.Close()
	hold, _ := os.OpenFile(slave, os.O_RDWR|unix.O_NOCTTY, 0)
	defer hold.Close()
	own := []byte{0x20, 0x6e, 0xf1, 0xaa, 0x68, 0xe8}
	txs := make(chan []byte, 8)
	send := func(kind byte, p []byte) {
		n := len(p)
		sum := kind + byte(n) + byte(n>>8)
		for _, b := range p {
			sum += b
		}
		m.Write(append(append([]byte{syncByte, kind, byte(n), byte(n >> 8)}, p...), sum))
	}
	go func() {
		buf := make([]byte, 4096)
		var acc []byte
		for {
			n, err := m.Read(buf)
			if err != nil {
				return
			}
			acc = append(acc, buf[:n]...)
			for len(acc) >= 5 {
				ln := int(acc[2]) | int(acc[3])<<8
				if len(acc) < 5+ln {
					break
				}
				kind, p := acc[1], append([]byte{}, acc[4:4+ln]...)
				acc = acc[5+ln:]
				switch kind {
				case cmdHello:
					// report a stale alias BSSID, as a board that wasn't reset does
					send(evInfo, append(append([]byte{1, 0}, own[0]^2, own[1], own[2], own[3], own[4], own[5]), "esp32-radio 1.3 (esp32s3)"...))
				case cmdSetBSSID:
					if bytes.Equal(p, make([]byte, 6)) {
						p = own
					}
					send(evBSSID, p)
				case cmdRadioOn:
					send(evRadio, []byte{1, p[0]})
				case cmdTx:
					txs <- p
				}
			}
		}
	}()
	r, err := OpenRadio(slave, true, t.Logf)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	if !bytes.Equal(r.MAC(), append([]byte{own[0] ^ 2}, own[1:]...)) {
		t.Fatalf("alias BSSID = %x", r.MAC())
	}
	if err := r.RadioOn(7); err != nil {
		t.Fatal(err)
	}
	frame := header(0x80, bcast, r.MAC(), r.MAC(), 1, 0)
	r.Tx(frame)
	select {
	case got := <-txs:
		if !bytes.Equal(got, frame) {
			t.Fatal("tx mismatch")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no tx")
	}
	rx := header(0xB0, r.MAC(), dsMAC, r.MAC(), 2, 0)
	send(evRx, append([]byte{0xd8, 7}, rx...))
	select {
	case got := <-r.Rx():
		if !bytes.Equal(got, rx) {
			t.Fatal("rx mismatch")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no rx")
	}
}
