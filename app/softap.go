package main

// SoftAP with NWCUSBAP registration and explicit console permissions.
// Port of wfc-usb-connector proto/ap.py.

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"strings"
	"sync"
	"time"
	"unicode/utf16"
)

type station struct {
	mac            [6]byte
	aid            int
	authed         bool
	associated     bool
	challenge      []byte
	challengeUntil time.Time
	authReqSeq     int
	asleep         bool
	queued         [][]byte
	lastSeq        int
	lastSeen       time.Time
}

type PendingConsole struct {
	MAC   string `json:"mac"`
	Name  string `json:"name"`
	Flags byte   `json:"flags"`
	Kind  byte   `json:"kind"`
	Seen  int64  `json:"seen"`
}

type SoftAP struct {
	mu         sync.Mutex
	radio      Radio
	channel    int
	bssid      []byte
	ssid       []byte
	key        []byte
	wep        *wepTx
	allowed    map[[6]byte]bool
	pending    map[[6]byte]*PendingConsole
	stations   map[[6]byte]*station
	counts     map[string]int
	sequence   int
	nextBeacon time.Time
	started    time.Time
	toNet      func(eth []byte)
	logf       func(string, ...any)
	onPending  func(PendingConsole)
	onName     func(mac string, name string)
	running    bool
}

func NewSoftAP(r Radio, channel int, ssid []byte, allowed [][6]byte, logf func(string, ...any)) *SoftAP {
	ap := &SoftAP{radio: r, channel: channel, bssid: r.MAC(), ssid: ssid, key: ssidKey(ssid),
		allowed: map[[6]byte]bool{}, pending: map[[6]byte]*PendingConsole{}, stations: map[[6]byte]*station{},
		counts: map[string]int{}, started: time.Now(), logf: logf}
	ap.wep = newWEPTx(ap.key)
	for _, m := range allowed {
		ap.allowed[m] = true
	}
	return ap
}

func mac6(b []byte) (m [6]byte) { copy(m[:], b); return }

func (ap *SoftAP) SetAllowed(list [][6]byte) {
	ap.mu.Lock()
	defer ap.mu.Unlock()
	next := map[[6]byte]bool{}
	for _, m := range list {
		next[m] = true
		delete(ap.pending, m)
	}
	for m := range ap.allowed {
		if !next[m] {
			delete(ap.stations, m)
		}
	}
	ap.allowed = next
}

func (ap *SoftAP) responseSSID(mac [6]byte) []byte {
	s := append([]byte{}, ap.ssid...)
	if ap.allowed[mac] {
		s[9] |= 1
	} else {
		s[9] &= 0x0e
		for i := 12; i < 32; i++ {
			s[i] = 0
		}
	}
	return s
}

func (ap *SoftAP) station(mac [6]byte) *station {
	if st, ok := ap.stations[mac]; ok {
		return st
	}
	if len(ap.stations) >= 32 {
		return nil
	}
	used := map[int]bool{}
	for _, s := range ap.stations {
		used[s.aid] = true
	}
	aid := 1
	for used[aid] {
		aid++
	}
	st := &station{mac: mac, aid: aid, authReqSeq: -1, lastSeq: -1, lastSeen: time.Now()}
	ap.stations[mac] = st
	return st
}

func (ap *SoftAP) management(subtype int, dst []byte, body []byte) {
	ap.sequence = (ap.sequence + 1) & 4095
	dur := uint16(314)
	if bytes.Equal(dst, bcast) {
		dur = 0
	}
	ap.radio.Tx(append(header(uint16(subtype<<4), dst, ap.bssid, ap.bssid, ap.sequence, dur), body...))
}

func (ap *SoftAP) beaconBody(ssid []byte, tim bool) []byte {
	tsf := uint64(time.Since(ap.started).Microseconds())
	b := make([]byte, 12)
	binary.LittleEndian.PutUint64(b, tsf)
	binary.LittleEndian.PutUint16(b[8:], 100)
	binary.LittleEndian.PutUint16(b[10:], 0x11)
	b = append(b, ie(0, ssid)...)
	b = append(b, ie(1, rates)...)
	b = append(b, ie(3, []byte{byte(ap.channel)})...)
	if tim {
		bitmap := make([]byte, 5)
		for _, st := range ap.stations {
			if st.associated && len(st.queued) > 0 {
				bitmap[st.aid/8] |= 1 << (st.aid % 8)
			}
		}
		b = append(b, ie(5, append([]byte{0, 1, 0}, bitmap...))...)
	}
	return b
}

// Tick sends beacons every 102.4 ms.
func (ap *SoftAP) Tick() {
	ap.mu.Lock()
	defer ap.mu.Unlock()
	now := time.Now()
	if ap.running && !now.Before(ap.nextBeacon) {
		ap.management(8, bcast, ap.beaconBody(nil, true))
		ap.counts["beacon_tx"]++
		ap.nextBeacon = ap.nextBeacon.Add(102400 * time.Microsecond)
		if !ap.nextBeacon.After(now) {
			ap.nextBeacon = now.Add(102400 * time.Microsecond)
		}
	}
}

func (ap *SoftAP) Start() {
	ap.mu.Lock()
	ap.running = true
	ap.nextBeacon = time.Now()
	ap.mu.Unlock()
}

func (ap *SoftAP) Handle(frame []byte) {
	ap.mu.Lock()
	defer ap.mu.Unlock()
	ap.handle(frame)
}

func (ap *SoftAP) handle(frame []byte) {
	if len(frame) < 10 {
		return
	}
	fc := int(binary.LittleEndian.Uint16(frame))
	if fc&3 != 0 {
		return
	}
	kind, subtype := (fc>>2)&3, (fc>>4)&15
	if len(frame) >= 24 && ap.allowed[mac6(frame[10:16])] {
		ap.counts["peer_"+itoa(kind)+"_"+itoa(subtype)]++
	}
	if kind == 1 && subtype == 10 && len(frame) == 16 {
		st := ap.stations[mac6(frame[10:16])]
		aid := int(binary.LittleEndian.Uint16(frame[2:])) & 0x3fff
		if bytes.Equal(frame[4:10], ap.bssid) && st != nil && st.associated && st.aid == aid && len(st.queued) > 0 {
			q := st.queued[0]
			st.queued = st.queued[1:]
			ap.sendEthernet(st, q, true)
		}
		return
	}
	if len(frame) < 24 || !(bytes.Equal(frame[4:10], ap.bssid) || bytes.Equal(frame[4:10], bcast)) {
		return
	}
	macb, body := frame[10:16], frame[24:]
	mac := mac6(macb)
	if macb[0]&1 != 0 || mac == [6]byte{} || bytes.Equal(macb, ap.bssid) {
		return
	}
	if kind == 0 && fc&0x0300 != 0 {
		return
	}
	if kind == 0 && subtype == 4 {
		es, err := parseElements(body)
		if err != nil || len(es) == 0 || es[0].id != 0 {
			return
		}
		ssid := es[0].data
		if len(ssid) < 8 || len(ssid) > 32 || !bytes.HasPrefix(ssid, nwcMagic) {
			return
		}
		ap.counts["nwc_probe"]++
		if ap.counts["nwc_probe"] <= 3 || ap.counts["nwc_probe"]%200 == 0 {
			ap.logf("DS %s is looking for the connector (allowed=%v, saved network=%v)", macStr(macb), ap.allowed[mac],
				len(ssid) == 32 && bytes.Equal(ssid[12:], ap.ssid[12:]))
		}
		if len(ssid) >= 32 && ssid[9]&3 != 0 {
			if name := decodeName(ssid[12:32]); name != "" {
				if p, ok := ap.pending[mac]; ok && p.Name != name {
					p.Name = name
				}
				if ap.allowed[mac] && ap.onName != nil {
					go ap.onName(macStr(macb), name)
				}
			}
		}
		if len(ssid) >= 12 && ssid[9]&3 != 0 && !ap.allowed[mac] {
			if _, seen := ap.pending[mac]; !seen && len(ap.pending) < 32 {
				name := ""
				if len(ssid) >= 32 {
					name = decodeName(ssid[12:32])
				}
				p := &PendingConsole{MAC: macStr(macb), Name: name, Flags: ssid[9], Kind: ssid[10], Seen: time.Now().Unix()}
				ap.pending[mac] = p
				ap.logf("Console %q (%s) is asking for permission", name, p.MAC)
				if ap.onPending != nil {
					go ap.onPending(*p)
				}
			}
		}
		ap.management(5, macb, ap.beaconBody(ap.responseSSID(mac), false))
		return
	}
	if !bytes.Equal(frame[4:10], ap.bssid) {
		return
	}
	if kind == 0 && !bytes.Equal(frame[16:22], ap.bssid) {
		return
	}
	if kind == 0 && subtype == 11 {
		ap.authenticate(macb, body, fc&0x4000 != 0, int(binary.LittleEndian.Uint16(frame[22:])))
		return
	}
	st := ap.stations[mac]
	if st == nil || !ap.allowed[mac] {
		return
	}
	st.lastSeen = time.Now()
	switch {
	case kind == 0 && (subtype == 0 || subtype == 2):
		ap.associate(st, body, subtype)
	case kind == 0 && (subtype == 10 || subtype == 12):
		reason := 0
		if len(body) >= 2 {
			reason = int(binary.LittleEndian.Uint16(body))
		}
		ap.logf("Console %s left (%s, reason %d)", macStr(macb), map[bool]string{true: "deauthenticated", false: "disassociated"}[subtype == 12], reason)
		st.associated = false
		st.queued = nil
		if subtype == 12 {
			st.authed = false
		}
	case kind == 2 && st.associated && fc&0x0300 == 0x0100:
		wasAsleep := st.asleep
		st.asleep = fc&0x1000 != 0
		if wasAsleep && !st.asleep {
			for len(st.queued) > 0 {
				q := st.queued[0]
				st.queued = st.queued[1:]
				ap.sendEthernet(st, q, true)
			}
		}
		if subtype == 4 {
			return
		}
		if subtype != 0 || fc&0x4000 == 0 || fc&0x0400 != 0 || frame[22]&15 != 0 {
			return
		}
		seq := int(binary.LittleEndian.Uint16(frame[22:]))
		if fc&0x0800 != 0 && seq == st.lastSeq {
			ap.counts["dup_rx"]++
			return
		}
		plain := wepDecrypt(ap.key, body)
		if plain == nil || len(plain) < 8 || !bytes.HasPrefix(plain, []byte{0xaa, 0xaa, 3, 0, 0, 0}) {
			ap.counts["bad_wep_or_llc"]++
			if ap.counts["bad_wep_or_llc"] <= 3 {
				ap.logf("Data from %s failed the WEP check", macStr(macb))
			}
			return
		}
		st.lastSeq = seq
		ap.counts["data_rx"]++
		if ap.counts["data_rx"] == 1 {
			ap.logf("First data from %s (DHCP/DNS starting)", macStr(macb))
		}
		if ap.toNet != nil {
			eth := make([]byte, 0, 12+len(plain)-6)
			eth = append(eth, frame[16:22]...)
			eth = append(eth, macb...)
			eth = append(eth, plain[6:]...)
			ap.toNet(eth)
		}
	}
}

func decodeName(b []byte) string {
	u := make([]uint16, 0, 10)
	for i := 0; i+1 < len(b); i += 2 {
		c := binary.LittleEndian.Uint16(b[i:])
		if c == 0 {
			break
		}
		u = append(u, c)
	}
	return strings.TrimSpace(string(utf16.Decode(u)))
}

func (ap *SoftAP) authenticate(macb, body []byte, protected bool, seq int) {
	if protected {
		body = wepDecrypt(ap.key, body)
		if body == nil {
			ap.counts["auth_bad_wep"]++
			ap.logf("AUTH from %s could not be decrypted (wrong WEP key: re-register the DS)", macStr(macb))
			return
		}
	}
	if len(body) < 6 {
		return
	}
	alg := int(binary.LittleEndian.Uint16(body))
	trans := int(binary.LittleEndian.Uint16(body[2:]))
	status := int(binary.LittleEndian.Uint16(body[4:]))
	if (trans != 1 && trans != 3) || status != 0 {
		return
	}
	mac := mac6(macb)
	resp := func(result int, extra []byte) {
		b := make([]byte, 6)
		binary.LittleEndian.PutUint16(b, uint16(alg))
		binary.LittleEndian.PutUint16(b[2:], uint16(trans+1))
		binary.LittleEndian.PutUint16(b[4:], uint16(result))
		ap.management(11, macb, append(b, extra...))
	}
	if !ap.allowed[mac] {
		resp(13, nil)
		ap.logf("AUTH refused for %s: not allowed yet (allow it on the Consoles page)", macStr(macb))
		return
	}
	st := ap.station(mac)
	if st == nil {
		return
	}
	result, challenge := 0, []byte(nil)
	switch {
	case alg == 0 && trans == 1 && !protected:
		st.authed = true
	case alg == 1 && trans == 1 && !protected:
		st.authed = false
		if seq != st.authReqSeq || time.Now().After(st.challengeUntil) {
			st.challenge = make([]byte, 128)
			rand.Read(st.challenge)
			st.challengeUntil = time.Now().Add(5 * time.Second)
			st.authReqSeq = seq
		}
		challenge = ie(16, st.challenge)
	case alg == 1 && trans == 3 && protected:
		var received []byte
		if es, err := parseElements(body[6:]); err == nil {
			received, _ = findElement(es, 16)
		}
		st.authed = st.challenge != nil && bytes.Equal(received, st.challenge) && !time.Now().After(st.challengeUntil)
		if !st.authed {
			result = 15
		}
	default:
		result = 14
		if alg != 0 && alg != 1 {
			result = 13
		}
	}
	resp(result, challenge)
	ap.counts["auth"]++
	kind := "open"
	if alg == 1 {
		kind = "shared-key WEP"
	}
	ap.logf("AUTH %s %s step %d -> status %d", macStr(macb), kind, trans, result)
}

func (ap *SoftAP) associate(st *station, body []byte, subtype int) {
	off := 4
	if subtype == 2 {
		off = 10
	}
	if len(body) < off {
		return
	}
	es, err := parseElements(body[off:])
	if err != nil {
		return
	}
	ssid, _ := findElement(es, 0)
	r1, _ := findElement(es, 1)
	r50, _ := findElement(es, 50)
	have := map[byte]bool{}
	for _, v := range append(append([]byte{}, r1...), r50...) {
		have[v&127] = true
	}
	status := 0
	switch {
	case !st.authed:
		status = 9
	case !bytes.Equal(ssid, ap.ssid) && !bytes.Equal(ssid, ap.responseSSID(st.mac)):
		status = 1
	case !have[2] || !have[4]:
		status = 18
	}
	wasAssociated := st.associated
	st.associated = status == 0
	b := make([]byte, 6)
	binary.LittleEndian.PutUint16(b, 0x11)
	binary.LittleEndian.PutUint16(b[2:], uint16(status))
	binary.LittleEndian.PutUint16(b[4:], uint16(0xc000|st.aid))
	ap.management(subtype+1, st.mac[:], append(b, ie(1, rates)...))
	ap.counts["assoc"]++
	ap.logf("ASSOC %s -> status %d", macStr(st.mac[:]), status)
	if st.associated && !wasAssociated {
		ap.logf("Console %s connected", macStr(st.mac[:]))
	} else if status != 0 {
		ap.logf("Association refused for %s (status %d)", macStr(st.mac[:]), status)
	}
}

// Output delivers an Ethernet frame from the network to associated consoles.
func (ap *SoftAP) Output(eth []byte) {
	if len(eth) < 14 {
		return
	}
	ap.mu.Lock()
	defer ap.mu.Unlock()
	for _, st := range ap.stations {
		if st.associated && (eth[0]&1 != 0 || bytes.Equal(eth[:6], st.mac[:])) {
			ap.sendEthernet(st, eth, false)
		}
	}
}

func (ap *SoftAP) sendEthernet(st *station, eth []byte, force bool) {
	if st.asleep && !force {
		if len(st.queued) >= 32 {
			st.queued = st.queued[1:]
		}
		st.queued = append(st.queued, append([]byte{}, eth...))
		return
	}
	body, err := ap.wep.encrypt(append([]byte{0xaa, 0xaa, 3, 0, 0, 0}, eth[12:]...))
	if err != nil {
		ap.logf("%v", err)
		return
	}
	fc := uint16(0x4208)
	if len(st.queued) > 0 {
		fc |= 0x2000
	}
	ap.sequence = (ap.sequence + 1) & 4095
	ap.radio.Tx(append(header(fc, st.mac[:], ap.bssid, eth[6:12], ap.sequence, 314), body...))
	ap.counts["data_tx"]++
}

type StationInfo struct {
	MAC        string `json:"mac"`
	Associated bool   `json:"associated"`
	LastSeen   int64  `json:"lastSeen"`
}

func (ap *SoftAP) Snapshot() (map[string]int, []StationInfo, []PendingConsole) {
	ap.mu.Lock()
	defer ap.mu.Unlock()
	c := map[string]int{}
	for k, v := range ap.counts {
		c[k] = v
	}
	var sts []StationInfo
	for _, st := range ap.stations {
		sts = append(sts, StationInfo{macStr(st.mac[:]), st.associated, st.lastSeen.Unix()})
	}
	var pend []PendingConsole
	for _, p := range ap.pending {
		pend = append(pend, *p)
	}
	return c, sts, pend
}

func itoa(i int) string {
	if i < 10 {
		return string(rune('0' + i))
	}
	return itoa(i/10) + string(rune('0'+i%10))
}
