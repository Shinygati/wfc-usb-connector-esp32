package main

// Userspace driver for the Ralink RT2570 (the original Nintendo Wi-Fi USB
// Connector). Port of wfc-usb-connector proto/rt2570.py (register semantics
// follow the public rt2500usb documentation).

import (
	"encoding/binary"
	"errors"
	"fmt"
	"sync"
	"time"
)

const (
	rtVID, rtPID = 0x0411, 0x008B

	reqDeviceMode, reqSingleWrite, reqMultiWrite, reqMultiRead = 1, 2, 6, 7
	reqEEPROMRead, reqRxControl                                = 9, 12
	modeTest                                                   = 4
	usbOut, usbIn                                              = 0x40, 0xC0

	macCSR0, macCSR1, macCSR2, macCSR5, macCSR8     = 0x400, 0x402, 0x404, 0x40A, 0x410
	macCSR9, macCSR10, macCSR11, macCSR12           = 0x412, 0x414, 0x416, 0x418
	macCSR13, macCSR14, macCSR15, macCSR16          = 0x41A, 0x41C, 0x41E, 0x420
	macCSR17, macCSR18, macCSR20, macCSR22          = 0x422, 0x424, 0x428, 0x42C
	txrxCSR0, txrxCSR1, txrxCSR2                    = 0x440, 0x442, 0x444
	txrxCSR5, txrxCSR6, txrxCSR7, txrxCSR8          = 0x44A, 0x44C, 0x44E, 0x450
	txrxCSR10, txrxCSR11                            = 0x454, 0x456
	txrxCSR18, txrxCSR19, txrxCSR20, txrxCSR21      = 0x464, 0x466, 0x468, 0x46A
	phyCSR2, phyCSR4, phyCSR5, phyCSR6              = 0x4C4, 0x4C8, 0x4CA, 0x4CC
	phyCSR7, phyCSR8, phyCSR9, phyCSR10             = 0x4CE, 0x4D0, 0x4D2, 0x4D4
	rf2522, rf2523, rf2524, rf2525, rf2525e, rf5222 = 0, 1, 2, 3, 5, 0x10
	rxdSize                                         = 16
)

var (
	errNotFound = errors.New("not found")
	errBusy     = errors.New("busy")
	errNoWinUSB = errors.New("WinUSB driver not installed")
)

var rfTables = func() map[int][14][4]uint32 {
	t := map[int][14][4]uint32{}
	common := []uint32{0xC9E, 0xCA2, 0xCA6, 0xCAA, 0xCAE, 0xCB2, 0xCB6, 0xCBA, 0xCBE, 0xD02, 0xD06, 0xD0A, 0xD0E, 0xD1A}
	r4 := func(c int, a, b uint32) uint32 {
		if c == 14 {
			return a
		}
		return b
	}
	var a, b, c, d, e, f [14][4]uint32
	for i := 0; i < 14; i++ {
		v := uint32(0xC1FDA + 0x14*i)
		if i >= 13 {
			v = 0xC20FA
		}
		a[i] = [4]uint32{0x2050, v, 0x101, 0}
		b[i] = [4]uint32{0x22010, common[i], 0xE0111, r4(i+1, 0xA03, 0xA1B)}
		c[i] = [4]uint32{0x32020, common[i], 0x101, r4(i+1, 0xA03, 0xA1B)}
		d[i] = [4]uint32{0x22020, 0x80000 | common[i], 0x60111, r4(i+1, 0xA03, 0xA1B)}
	}
	e25 := [][2]uint32{{0x89A, 0xE1B}, {0x89E, 0xE07}, {0x89E, 0xE1B}, {0x8A2, 0xE07}, {0x8A2, 0xE1B}, {0x8A6, 0xE07},
		{0x8A6, 0xE1B}, {0x8AA, 0xE07}, {0x8AA, 0xE1B}, {0x8AE, 0xE07}, {0x8AE, 0xE1B}, {0x8B2, 0xE07}, {0x8B2, 0xE1B}, {0x8B6, 0xE23}}
	r5222 := []uint32{0x1136, 0x113A, 0x113E, 0x1182, 0x1186, 0x118A, 0x118E, 0x1192, 0x1196, 0x119A, 0x119E, 0x11A2, 0x11A6, 0x11AE}
	for i := 0; i < 14; i++ {
		e[i] = [4]uint32{0x22010, e25[i][0], 0x60111, e25[i][1]}
		f[i] = [4]uint32{0x22020, r5222[i], 0x101, r4(i+1, 0xA1B, 0xA0B)}
	}
	t[rf2522], t[rf2523], t[rf2524], t[rf2525], t[rf2525e], t[rf5222] = a, b, c, d, e, f
	return t
}()

var rf2525eHalf = [14]uint32{0x8AA, 0x8AE, 0x8AE, 0x8B2, 0x8B2, 0x8B6, 0x8B6, 0x8BA, 0x8BA, 0x8BE, 0x8B7, 0x902, 0x902, 0x906}

type RT2570 struct {
	dev          usbDev
	eeprom       []byte
	mac          []byte
	rf           int
	antTx, antRx int
	rev          uint16
	txpower      [14]int
	channel      int
	rx           chan []byte
	stop         chan struct{}
	wg           sync.WaitGroup
	once         sync.Once
	dead         bool
	txOK, txErr  uint32
	rxCount      uint32
	logf         func(string, ...any)
}

// OpenRT2570 finds and probes the original Nintendo Wi-Fi USB Connector.
func OpenRT2570(logf func(string, ...any)) (*RT2570, error) {
	d, err := openUSB(rtVID, rtPID)
	if err != nil {
		switch {
		case errors.Is(err, errNotFound):
			return nil, errors.New("original connector not found: plug in the Nintendo Wi-Fi USB Connector (USB ID 0411:008B)")
		case errors.Is(err, errNoWinUSB):
			return nil, errors.New("the original connector needs the WinUSB driver: install it once with Zadig (select USB ID 0411 008B, choose WinUSB)")
		case errors.Is(err, errBusy):
			return nil, errors.New("the original connector is in use by another program")
		}
		return nil, err
	}
	return newRT2570(d, logf)
}

func newRT2570(d usbDev, logf func(string, ...any)) (*RT2570, error) {
	r := &RT2570{dev: d, rx: make(chan []byte, 512), stop: make(chan struct{}), logf: logf}
	if err := r.probe(); err != nil {
		d.Close()
		return nil, err
	}
	return r, nil
}

// ---- raw access ----

func (r *RT2570) vreq(req uint8, value, index uint16) error {
	_, err := r.dev.Control(usbOut, req, value, index, nil, time.Second)
	return err
}

func (r *RT2570) read(off uint16) uint16 {
	b := make([]byte, 2)
	if _, err := r.dev.Control(usbIn, reqMultiRead, 0, off, b, time.Second); err != nil {
		return 0
	}
	return binary.LittleEndian.Uint16(b)
}

func (r *RT2570) write(off uint16, val uint16) {
	b := []byte{byte(val), byte(val >> 8)}
	r.dev.Control(usbOut, reqMultiWrite, 0, off, b, time.Second)
}

func (r *RT2570) multiwrite(off uint16, data []byte) {
	r.dev.Control(usbOut, reqMultiWrite, 0, off, append([]byte{}, data...), time.Second)
}

func (r *RT2570) modify(off, mask, val uint16) {
	v := r.read(off)
	r.write(off, (v&^mask)|(val&mask))
}

func (r *RT2570) wait(off, busy uint16) (uint16, error) {
	for i := 0; i < 100; i++ {
		v := r.read(off)
		if v&busy == 0 {
			return v, nil
		}
		time.Sleep(time.Millisecond)
	}
	return 0, fmt.Errorf("register 0x%x busy", off)
}

func (r *RT2570) bbpWrite(reg, val int) error {
	if _, err := r.wait(phyCSR8, 1); err != nil {
		return err
	}
	r.write(phyCSR7, uint16((reg&0x7F)<<8|(val&0xFF)))
	return nil
}

func (r *RT2570) bbpRead(reg int) (int, error) {
	if _, err := r.wait(phyCSR8, 1); err != nil {
		return 0, err
	}
	r.write(phyCSR7, uint16(0x8000|(reg&0x7F)<<8))
	if _, err := r.wait(phyCSR8, 1); err != nil {
		return 0, err
	}
	return int(r.read(phyCSR7) & 0xFF), nil
}

func (r *RT2570) rfWrite(val uint32) error {
	if _, err := r.wait(phyCSR10, 0x8000); err != nil {
		return err
	}
	r.write(phyCSR9, uint16(val&0xFFFF))
	r.write(phyCSR10, uint16(0x8000|20<<8|(val>>16)&0xFF))
	return nil
}

func (r *RT2570) ee(word int) uint16 { return binary.LittleEndian.Uint16(r.eeprom[word*2:]) }

// ---- probe / bring-up ----

func (r *RT2570) probe() error {
	b := make([]byte, 0x6E)
	n, err := r.dev.Control(usbIn, reqEEPROMRead, 0, 0, b, time.Second)
	if err != nil || n < 0x6E {
		return fmt.Errorf("could not read the connector's EEPROM: %v", err)
	}
	r.eeprom = b
	r.mac = append([]byte{}, b[4:10]...)
	ant := r.ee(0x0B)
	r.rf = int(ant>>11) & 0x1F
	r.antTx, r.antRx = int(ant>>2)&3, int(ant>>4)&3
	r.rev = r.read(macCSR0)
	for i := 0; i < 14; i++ {
		p := int(b[0x3C+i])
		if p > 31 {
			p = 24
		}
		r.txpower[i] = p
	}
	return nil
}

func (r *RT2570) setStateAwake() error {
	state := uint16(3)
	reg := state<<1 | state<<3
	r.write(macCSR17, reg)
	reg |= 1
	r.write(macCSR17, reg)
	for i := 0; i < 100; i++ {
		v := r.read(macCSR17)
		if (v>>5)&3 == state && (v>>7)&3 == state {
			return nil
		}
		r.write(macCSR17, reg)
		time.Sleep(30 * time.Millisecond)
	}
	return errors.New("the connector did not wake up")
}

func (r *RT2570) initRegisters() error {
	r.vreq(reqDeviceMode, modeTest, 0x0001)
	r.vreq(reqSingleWrite, 0x00F0, 0x0308)
	r.modify(txrxCSR2, 1, 1)
	r.write(macCSR13, 0x1111)
	r.write(macCSR14, 0x1E11)
	r.modify(macCSR1, 7, 3)
	r.modify(macCSR1, 7, 0)
	r.write(txrxCSR5, 0x80|13|(0x80|12)<<8)
	r.write(txrxCSR6, 0x80|10|(0x80|11)<<8)
	r.write(txrxCSR7, 0x80|7|(0x80|6)<<8)
	r.write(txrxCSR8, 0x80|5)
	r.modify(txrxCSR19, 0x1F, 0)
	r.write(txrxCSR21, 0xE78F)
	r.write(macCSR9, 0xFF1D)
	if err := r.setStateAwake(); err != nil {
		return err
	}
	r.modify(macCSR1, 7, 4)
	if r.rev >= 3 {
		r.modify(phyCSR2, 0x0002, 0)
	} else {
		r.write(phyCSR2, 0x0002|3<<12)
	}
	r.write(macCSR11, 0x0002)
	r.write(macCSR22, 0x0053)
	r.write(macCSR15, 0x01EE)
	r.write(macCSR16, 0x0000)
	r.modify(macCSR8, 0x0FFF, 2432)
	r.write(txrxCSR0, 24<<3)
	r.modify(macCSR18, 0x00FF, 90)
	r.modify(phyCSR4, 1, 1)
	r.modify(txrxCSR1, 0xFF00, 0x8000|24<<8)
	return nil
}

func (r *RT2570) initBBP() error {
	ready := false
	for i := 0; i < 100; i++ {
		v, err := r.bbpRead(0)
		if err == nil && v != 0 && v != 0xFF {
			ready = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !ready {
		return errors.New("the connector's baseband did not respond")
	}
	for _, rv := range [][2]int{{3, 0x02}, {4, 0x19}, {14, 0x1C}, {15, 0x30}, {16, 0xAC}, {18, 0x18}, {19, 0xFF},
		{20, 0x1E}, {21, 0x08}, {22, 0x08}, {23, 0x08}, {24, 0x80}, {25, 0x50}, {26, 0x08},
		{27, 0x23}, {30, 0x10}, {31, 0x2B}, {32, 0xB9}, {34, 0x12}, {35, 0x50}, {39, 0xC4},
		{40, 0x02}, {41, 0x60}, {53, 0x10}, {54, 0x18}, {56, 0x08}, {57, 0x10}, {58, 0x08},
		{61, 0x60}, {62, 0x10}, {75, 0xFF}} {
		if err := r.bbpWrite(rv[0], rv[1]); err != nil {
			return err
		}
	}
	for i := 0; i < 16; i++ {
		w := r.ee(0x0E + i)
		if w != 0 && w != 0xFFFF {
			r.bbpWrite(int(w>>8), int(w&0xFF))
		}
	}
	return nil
}

func (r *RT2570) configAntenna() error {
	sel := func(v int) int {
		switch v {
		case 0:
			return 1
		case 1:
			return 0
		}
		return 2
	}
	tx, rx := sel(r.antTx), sel(r.antRx)
	r2, err := r.bbpRead(2)
	if err != nil {
		return err
	}
	r14, err := r.bbpRead(14)
	if err != nil {
		return err
	}
	csr5, csr6 := int(r.read(phyCSR5)), int(r.read(phyCSR6))
	r2 = (r2 &^ 3) | tx
	csr5 = (csr5 &^ 3) | tx
	csr6 = (csr6 &^ 3) | tx
	r14 = (r14 &^ 3) | rx
	if r.rf == rf2525e || r.rf == rf5222 {
		r2 |= 4
		csr5 |= 4
		csr6 |= 4
		if r.rf == rf2525e {
			r14 &^= 4
		}
	} else {
		csr5 &^= 4
		csr6 &^= 4
	}
	r.bbpWrite(2, r2)
	r.bbpWrite(14, r14)
	r.write(phyCSR5, uint16(csr5))
	r.write(phyCSR6, uint16(csr6))
	return nil
}

func (r *RT2570) setChannel(ch int) error {
	if ch < 1 || ch > 14 {
		return errors.New("channel must be between 1 and 14")
	}
	t, ok := rfTables[r.rf]
	if !ok {
		return fmt.Errorf("unsupported RF chip 0x%x", r.rf)
	}
	v := t[ch-1]
	p := r.txpower[ch-1]
	if p < 0 {
		p = 0
	} else if p > 31 {
		p = 31
	}
	rf3 := (v[2] &^ 0x3E00) | uint32(p)<<9
	if r.rf == rf2525e {
		r.rfWrite(rf2525eHalf[ch-1])
		if v[3] != 0 {
			r.rfWrite(v[3])
		}
	}
	for _, w := range []uint32{v[0], v[1], rf3} {
		if err := r.rfWrite(w); err != nil {
			return err
		}
	}
	if v[3] != 0 {
		r.rfWrite(v[3])
	}
	r.channel = ch
	return nil
}

func (r *RT2570) setERP() {
	r.modify(txrxCSR10, 4, 0)
	r.write(txrxCSR11, 0x0003)
	r.modify(txrxCSR18, 0xFFF0, 100*4<<4)
	r.write(macCSR10, 20)
	r.write(macCSR11, 10)
	r.write(macCSR12, 364)
}

func (r *RT2570) enableAPSync() {
	preload := 144 + 48 + (24+20)*8
	r.write(txrxCSR20, uint16((preload>>6)&0x1FFF|2<<13))
	r.modify(txrxCSR18, 0x000F, 0)
	r.modify(txrxCSR19, 0x0006, 3<<1)
}

// RadioOn brings the connector up as an access point (radio_on + SoftAP.start in the Python project).
func (r *RT2570) RadioOn(channel int) error {
	if err := r.initRegisters(); err != nil {
		return err
	}
	if err := r.initBBP(); err != nil {
		return err
	}
	if err := r.configAntenna(); err != nil {
		return err
	}
	if err := r.setChannel(channel); err != nil {
		return err
	}
	r.multiwrite(macCSR2, r.mac)
	r.multiwrite(macCSR5, r.mac)
	r.setERP()
	r.enableAPSync()
	r.modify(txrxCSR19, 0x1F, 1)    // software beacons (SoftAP.tick), as in the Python project
	r.write(txrxCSR2, 0x2|0x4|0x40) // promiscuous RX; also clears DISABLE_RX
	r.modify(txrxCSR2, 1, 0)        // start RX
	r.wg.Add(1)
	go r.rxLoop()
	return nil
}

func (r *RT2570) radioOff() {
	r.modify(txrxCSR2, 1, 1)
	r.write(macCSR13, 0x2121)
	r.write(macCSR14, 0x2121)
	r.write(txrxCSR19, 0)
	r.vreq(reqRxControl, 0, 0)
}

func (r *RT2570) LED(on bool) {
	v := uint16(0)
	if on {
		v = 2
	}
	r.modify(macCSR20, 3, v)
}

// ---- frames ----

func plcp(length, rate500k int) (sig, svc, lo, hi, ofdm int) {
	ofdmSig := map[int]int{12: 0xB, 18: 0xF, 24: 0xA, 36: 0xE, 48: 0x9, 72: 0xD, 96: 0x8, 108: 0xC}
	flen := length + 4
	if s, ok := ofdmSig[rate500k]; ok {
		return s, 0x04, flen & 0x3F, (flen >> 6) & 0x3F, 1
	}
	kbps100 := rate500k * 5
	us := (flen*8*10 + kbps100 - 1) / kbps100
	svc = 0x04
	if rate500k == 22 && us*kbps100-flen*8*10 >= 80 {
		svc |= 0x80
	}
	return map[int]int{2: 0, 4: 1, 11: 2, 22: 3}[rate500k], svc, us & 0xFF, us >> 8, 0
}

func buildTx(frame []byte, rate500k int, ack, timestamp bool, retry, ifs int, newSeq bool) []byte {
	sig, svc, lo, hi, ofdm := plcp(len(frame), rate500k)
	w0 := uint32(retry&0xF)<<4 | uint32(ofdm)<<11 | uint32(ifs&3)<<13 | uint32(len(frame)&0xFFF)<<16
	if ack {
		w0 |= 0x200
	}
	if timestamp {
		w0 |= 0x400
	}
	if newSeq {
		w0 |= 0x1000
	}
	w1 := uint32(24 | 2<<6 | 4<<8 | 10<<12)
	w2 := uint32(sig | svc<<8 | lo<<16 | hi<<24)
	buf := make([]byte, 20, 20+len(frame)+2)
	binary.LittleEndian.PutUint32(buf[0:], w0)
	binary.LittleEndian.PutUint32(buf[4:], w1)
	binary.LittleEndian.PutUint32(buf[8:], w2)
	buf = append(buf, frame...)
	if len(buf)&1 == 1 {
		buf = append(buf, 0)
	}
	if len(buf)%512 == 0 {
		buf = append(buf, 0, 0)
	}
	return buf
}

func (r *RT2570) Tx(frame []byte) error {
	if r.dead {
		return errors.New("connector disconnected")
	}
	if len(frame) < 10 || len(frame) > 2432 {
		return errors.New("invalid 802.11 frame length")
	}
	ack := frame[4]&1 == 0 // unicast frames wait for an ACK (retry up to 7 times)
	_, err := r.dev.BulkOut(0x01, buildTx(frame, 2, ack, false, 7, 2, true), time.Second)
	if err != nil {
		r.txErr++
		return err
	}
	r.txOK++
	return nil
}

func (r *RT2570) rxLoop() {
	defer r.wg.Done()
	buf := make([]byte, 2560)
	fails := 0
	for {
		select {
		case <-r.stop:
			return
		default:
		}
		n, err := r.dev.BulkIn(0x81, buf, 100*time.Millisecond)
		if errors.Is(err, errUSBTimeout) {
			continue
		}
		if err != nil {
			fails++
			if fails > 20 {
				r.dead = true
				if r.logf != nil {
					r.logf("The original connector stopped responding: %v", err)
				}
				return
			}
			time.Sleep(10 * time.Millisecond)
			continue
		}
		fails = 0
		if f := parseRx(buf[:n]); f != nil {
			r.rxCount++
			select {
			case r.rx <- f:
			default:
			}
		}
	}
}

// parseRx strips the RX descriptor and FCS; nil for errored/short frames.
func parseRx(data []byte) []byte {
	if len(data) < rxdSize {
		return nil
	}
	w0 := binary.LittleEndian.Uint32(data[len(data)-rxdSize:])
	size := int(w0>>16) & 0xFFF
	if size < 14 || size > len(data)-rxdSize || w0&0x20 != 0 || w0&0x80 != 0 {
		return nil
	}
	return append([]byte{}, data[:size-4]...)
}

func (r *RT2570) MAC() []byte       { return r.mac }
func (r *RT2570) Rx() <-chan []byte { return r.rx }
func (r *RT2570) Dead() bool        { return r.dead }
func (r *RT2570) Status() *RadioStatus {
	return &RadioStatus{RxForwarded: r.rxCount, TxOK: r.txOK, TxErr: r.txErr, Channel: uint32(r.channel)}
}
func (r *RT2570) Describe() string {
	return fmt.Sprintf("Original Nintendo Wi-Fi USB Connector (RT2570 rev 0x%04x, RF 0x%02x), MAC %s", r.rev, r.rf, macStr(r.mac))
}

func (r *RT2570) Close() error {
	r.once.Do(func() {
		close(r.stop)
		r.wg.Wait()
		if !r.dead {
			r.radioOff()
		}
		r.dev.Close()
	})
	return nil
}
