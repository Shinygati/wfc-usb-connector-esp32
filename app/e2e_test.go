package main

import (
	"bytes"
	"encoding/binary"
	"net"
	"testing"
	"time"
	"unicode/utf16"
)

type fakeRadio struct {
	mac []byte
	rx  chan []byte
	tx  chan []byte
}

func (f *fakeRadio) MAC() []byte       { return f.mac }
func (f *fakeRadio) Rx() <-chan []byte { return f.rx }
func (f *fakeRadio) Tx(b []byte) error { f.tx <- append([]byte{}, b...); return nil }
func (f *fakeRadio) Close() error      { return nil }

var dsMAC = []byte{0x00, 0x09, 0xbf, 0x12, 0x34, 0x56}

func waitFrame(t *testing.T, r *fakeRadio, fc0 byte) []byte {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case f := <-r.tx:
			if f[0] == fc0 && bytes.Equal(f[4:10], dsMAC) {
				return f
			}
		case <-deadline:
			t.Fatalf("no frame with fc0=%02x", fc0)
		}
	}
}

func TestEndToEnd(t *testing.T) {
	bssid := []byte{0x22, 0x6e, 0xf1, 0xaa, 0x68, 0xe8}
	r := &fakeRadio{mac: bssid, rx: make(chan []byte, 16), tx: make(chan []byte, 256)}
	ssid := makeSSID()
	ap := NewSoftAP(r, 7, ssid, nil, t.Logf)
	netw, err := NewNetwork("178.62.43.212", ap.Output, t.Logf)
	if err != nil {
		t.Fatal(err)
	}
	defer netw.Close()
	ap.toNet = netw.Input
	ap.Start()

	// probe for registration
	name := utf16.Encode([]rune("Player One"))
	nb := make([]byte, 20)
	for i, c := range name {
		binary.LittleEndian.PutUint16(nb[2*i:], c)
	}
	probe := append(append([]byte{}, nwcMagic...), 0, 1, 0, 0)
	probe = append(probe, nb...)
	ap.Handle(append(header(0x40, bcast, dsMAC, bcast, 1, 0), append(ie(0, probe), ie(1, rates)...)...))
	resp := waitFrame(t, r, 0x50)
	es, _ := parseElements(resp[36:])
	if s, _ := findElement(es, 0); !bytes.Equal(s[12:], make([]byte, 20)) {
		t.Fatal("unapproved console got key digits")
	}
	_, _, pend := ap.Snapshot()
	if len(pend) != 1 || pend[0].Name != "Player One" {
		t.Fatalf("pending = %+v", pend)
	}
	ap.SetAllowed([][6]byte{mac6(dsMAC)})
	ap.Handle(append(header(0x40, bcast, dsMAC, bcast, 2, 0), append(ie(0, probe), ie(1, rates)...)...))
	resp = waitFrame(t, r, 0x50)
	es, _ = parseElements(resp[36:])
	full, _ := findElement(es, 0)
	key := deriveKey(full[12:32])
	if !bytes.Equal(key, ap.key) {
		t.Fatal("key mismatch")
	}

	// shared-key auth
	ap.Handle(append(header(0xB0, bssid, dsMAC, bssid, 3, 0), 1, 0, 1, 0, 0, 0))
	a2 := waitFrame(t, r, 0xB0)
	es, _ = parseElements(a2[30:])
	ch, _ := findElement(es, 16)
	w := newWEPTx(key)
	enc, _ := w.encrypt(append([]byte{1, 0, 3, 0, 0, 0}, ie(16, ch)...))
	ap.Handle(append(header(0x40B0, bssid, dsMAC, bssid, 4, 0), enc...))
	a4 := waitFrame(t, r, 0xB0)
	if binary.LittleEndian.Uint16(a4[28:]) != 0 || binary.LittleEndian.Uint16(a4[26:]) != 4 {
		t.Fatalf("auth step4 = %x", a4[24:30])
	}
	// association
	ap.Handle(append(header(0x00, bssid, dsMAC, bssid, 5, 0), append([]byte{0x11, 0, 10, 0}, append(ie(0, full), ie(1, rates)...)...)...))
	as := waitFrame(t, r, 0x10)
	if binary.LittleEndian.Uint16(as[26:]) != 0 {
		t.Fatal("assoc refused")
	}

	// DHCP DISCOVER inside WEP
	dhcp := make([]byte, 240)
	dhcp[0], dhcp[1], dhcp[2] = 1, 1, 6
	copy(dhcp[4:], []byte{1, 2, 3, 4})
	copy(dhcp[28:], dsMAC)
	copy(dhcp[236:], []byte{99, 130, 83, 99})
	dhcp = append(dhcp, 53, 1, 1, 255)
	udp := make([]byte, 8)
	binary.BigEndian.PutUint16(udp, 68)
	binary.BigEndian.PutUint16(udp[2:], 67)
	binary.BigEndian.PutUint16(udp[4:], uint16(8+len(dhcp)))
	ipb := make([]byte, 20)
	ipb[0], ipb[8], ipb[9] = 0x45, 64, 17
	binary.BigEndian.PutUint16(ipb[2:], uint16(20+8+len(dhcp)))
	copy(ipb[16:], []byte{255, 255, 255, 255})
	binary.BigEndian.PutUint16(ipb[10:], checksum(ipb))
	payload := append(append(append([]byte{0xaa, 0xaa, 3, 0, 0, 0, 8, 0}, ipb...), udp...), dhcp...)
	enc, _ = w.encrypt(payload)
	ap.Handle(append(header(0x4108, bssid, dsMAC, bcast, 6, 0), enc...))

	deadline := time.After(5 * time.Second)
	for {
		select {
		case f := <-r.tx:
			if f[0] != 0x08 || !bytes.Equal(f[4:10], dsMAC) && !bytes.Equal(f[4:10], bcast) {
				continue
			}
			plain := wepDecrypt(key, f[24:])
			if plain == nil {
				t.Fatal("bad WEP from AP")
			}
			ip := plain[8:]
			if ip[9] != 17 || binary.BigEndian.Uint16(ip[22:]) != 68 {
				continue
			}
			bootp := ip[28:]
			yi := net.IP(bootp[16:20])
			t.Logf("DHCP offer: yiaddr=%s", yi)
			if !yi.Equal(net.IPv4(192, 168, 77, yi[3])) || yi[3] < 2 {
				t.Fatalf("bad offer %s", yi)
			}
			return
		case <-deadline:
			t.Fatal("no DHCP offer")
		}
	}
}

func TestDNSRewrite(t *testing.T) {
	gw, up := net.ParseIP("192.168.77.1").To4(), net.ParseIP("178.62.43.212").To4()
	eth := make([]byte, 14+28)
	binary.BigEndian.PutUint16(eth[12:], 0x0800)
	ip := eth[14:]
	ip[0], ip[9] = 0x45, 17
	copy(ip[12:], []byte{192, 168, 77, 10})
	copy(ip[16:], gw)
	binary.BigEndian.PutUint16(ip[20+2:], 53)
	rewriteUDP(eth, gw, up, 53, true)
	if !net.IP(ip[16:20]).Equal(up) || checksum(ip[:20]) != 0 {
		t.Fatal("inbound rewrite failed")
	}
}

func TestBundledFirmware(t *testing.T) {
	for _, c := range []string{"esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c5", "esp32c6"} {
		img, ok := firmwareImage(c)
		if !ok || len(img) < 0x20000 || len(img)%0x1000 != 0 {
			t.Fatalf("%s: bad image (%d bytes)", c, len(img))
		}
		off := 0
		switch c {
		case "esp32", "esp32s2":
			off = 0x1000
		case "esp32c5":
			off = 0x2000
		}
		if img[off] != 0xE9 {
			t.Fatalf("%s: no bootloader image magic at 0x%x", c, off)
		}
	}
}
