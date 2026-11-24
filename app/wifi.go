package main

// 802.11 frame helpers and WEP (RC4 + CRC-32 ICV). Port of proto/wifi.py.

import (
	"crypto/rand"
	"crypto/rc4"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"sync"
)

var bcast = []byte{0xff, 0xff, 0xff, 0xff, 0xff, 0xff}
var rates = []byte{0x82, 0x84, 0x0b, 0x16}

type element struct {
	id   byte
	data []byte
}

func parseElements(b []byte) ([]element, error) {
	var out []element
	for len(b) > 0 {
		if len(b) < 2 || len(b) < 2+int(b[1]) {
			return nil, errors.New("truncated information element")
		}
		out = append(out, element{b[0], b[2 : 2+int(b[1])]})
		b = b[2+int(b[1]):]
	}
	return out, nil
}

func findElement(es []element, id byte) ([]byte, bool) {
	for _, e := range es {
		if e.id == id {
			return e.data, true
		}
	}
	return nil, false
}

func ie(id byte, v []byte) []byte { return append([]byte{id, byte(len(v))}, v...) }

func header(fc uint16, dst, src, bssid []byte, seq int, duration uint16) []byte {
	h := make([]byte, 24)
	binary.LittleEndian.PutUint16(h[0:], fc)
	binary.LittleEndian.PutUint16(h[2:], duration)
	copy(h[4:], dst)
	copy(h[10:], src)
	copy(h[16:], bssid)
	binary.LittleEndian.PutUint16(h[22:], uint16(seq&4095)<<4)
	return h
}

func rc4xor(key, data []byte) []byte {
	c, err := rc4.NewCipher(key)
	if err != nil {
		panic(err)
	}
	out := make([]byte, len(data))
	c.XORKeyStream(out, data)
	return out
}

func wepDecrypt(key, body []byte) []byte {
	if len(body) < 8 || body[3] != 0 {
		return nil
	}
	plain := rc4xor(append(append([]byte{}, body[:3]...), key...), body[4:])
	data := plain[:len(plain)-4]
	if binary.LittleEndian.Uint32(plain[len(plain)-4:]) != crc32.ChecksumIEEE(data) {
		return nil
	}
	return data
}

type wepTx struct {
	mu   sync.Mutex
	key  []byte
	iv   uint32
	sent uint32
}

func newWEPTx(key []byte) *wepTx {
	var b [4]byte
	rand.Read(b[:])
	return &wepTx{key: key, iv: binary.LittleEndian.Uint32(b[:]) & 0xffffff}
}

func (w *wepTx) encrypt(data []byte) ([]byte, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.sent >= 1<<24 {
		return nil, errors.New("WEP IV space exhausted; restart the session")
	}
	w.sent++
	w.iv = (w.iv + 1) & 0xffffff
	iv := []byte{byte(w.iv), byte(w.iv >> 8), byte(w.iv >> 16)}
	icv := make([]byte, 4)
	binary.LittleEndian.PutUint32(icv, crc32.ChecksumIEEE(data))
	enc := rc4xor(append(append([]byte{}, iv...), w.key...), append(append([]byte{}, data...), icv...))
	return append(append(iv, 0), enc...), nil
}
