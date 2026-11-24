package main

// Nintendo Wi-Fi USB Connector SSID format and WEP-104 key derivation.
// Port of wfc-usb-connector proto/wfckey.py (clean reimplementation of
// NintendoWFCReg.exe v1.07). The SSID is "NWCUSBAP" + 4 spaces + 20 ASCII
// digits; the console derives the 13-byte WEP key from the digits.

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"math/big"
)

var nwcMagic = []byte("NWCUSBAP")

var (
	wfcX1   = []byte("gwi'6&fs=0Nf~")
	wfcX2   = []byte("%(egEr)ag(s&m")
	wfcSbox = []byte{10, 13, 14, 8, 9, 3, 6, 0, 12, 5, 2, 7, 11, 1, 15, 4}
	wfcPerm = []byte{5, 1, 12, 4, 2, 3, 10, 0, 11, 7, 9, 8, 6}
)

func makeSSID() []byte {
	s := append([]byte{}, nwcMagic...)
	s = append(s, "    "...)
	for i := 0; i < 4; i++ {
		n, _ := rand.Int(rand.Reader, big.NewInt(0x8000))
		s = append(s, fmt.Sprintf("%05d", n.Int64())...)
	}
	return s
}

func deriveKey(d []byte) []byte {
	k := make([]byte, 13)
	for i := 0; i < 13; i++ {
		k[i] = d[i] ^ d[13+i%7]
	}
	for i := 3; i < 10; i++ {
		k[i] ^= d[10+i]
	}
	for i := range k {
		k[i] ^= wfcX1[i]
	}
	t := append([]byte{}, k...)
	for i := 0; i < 13; i++ {
		k[wfcPerm[i]] = t[i]
	}
	for i := range k {
		k[i] ^= wfcX2[i]
		k[i] = wfcSbox[k[i]>>4]<<4 | wfcSbox[k[i]&15]
	}
	k[0] ^= k[6]
	k[3] ^= k[9]
	k[6] = k[3] ^ k[6]
	k[9] ^= k[0]
	k[12] ^= k[0]
	k[1] ^= k[7]
	k[4] ^= k[10]
	k[7] = k[4] ^ k[7]
	k[10] ^= k[1]
	k[12] ^= k[1]
	k[2] ^= k[8]
	k[5] ^= k[11]
	k[8] = k[5] ^ k[8]
	k[11] ^= k[2]
	k[12] ^= k[2]
	return k
}

func ssidKey(ssid []byte) []byte { return deriveKey(ssid[12:32]) }

func validSSID(s []byte) bool {
	if len(s) != 32 || !bytes.HasPrefix(s, nwcMagic) {
		return false
	}
	for _, c := range s[12:] {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
