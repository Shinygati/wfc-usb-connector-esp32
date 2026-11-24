package main

// ESP32-S3 radio link (firmware/esp32s3-radio). Port of proto/esp32radio.py.

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"go.bug.st/serial"
	"go.bug.st/serial/enumerator"
)

const (
	radioBaud = 921600
	syncByte  = 0xC5

	cmdHello    = 1
	cmdRadioOn  = 2
	cmdTx       = 3
	cmdRadioOff = 4
	cmdLed      = 5
	cmdStatus   = 6
	cmdSetBSSID = 7

	evInfo   = 0x81
	evRadio  = 0x82
	evRx     = 0x83
	evLog    = 0x84
	evStatus = 0x86
	evBSSID  = 0x87
)

// Radio is what the SoftAP needs from the hardware.
type Radio interface {
	MAC() []byte
	Tx(frame []byte) error
	Rx() <-chan []byte
	Close() error
}

// RadioDevice is a radio the app can run a session on (ESP32 or the original RT2570 connector).
type RadioDevice interface {
	Radio
	RadioOn(channel int) error
	LED(on bool)
	Describe() string
	Status() *RadioStatus
	Dead() bool
}

type portInfo struct {
	Name, VID, PID, Product string
	Espressif, Bridge       bool
}

// candidateVIDs: Espressif native USB + USB-UART bridges used on ESP32 boards.
var bridgeVIDs = map[string]bool{"1A86": true, "10C4": true, "0403": true}

func listPorts() []portInfo {
	ports, err := enumerator.GetDetailedPortsList()
	if err != nil {
		return nil
	}
	var out []portInfo
	for _, p := range ports {
		vid := strings.ToUpper(p.VID)
		out = append(out, portInfo{p.Name, vid, strings.ToUpper(p.PID), p.Product, vid == "303A", bridgeVIDs[vid]})
	}
	return out
}

// candidatePorts: UART bridges first (the native USB port did not answer on the
// tested board), then Espressif native USB.
func candidatePorts() []string {
	var bridge, native []string
	for _, p := range listPorts() {
		if p.Bridge {
			bridge = append(bridge, p.Name)
		} else if p.Espressif {
			native = append(native, p.Name)
		}
	}
	return append(bridge, native...)
}

type ESP32Radio struct {
	port     serial.Port
	portName string
	mac      []byte
	ownMAC   []byte
	firmware string
	alias    bool
	rx       chan []byte
	replies  chan packet
	wmu      sync.Mutex
	closed   chan struct{}
	once     sync.Once
	logf     func(string, ...any)
	dead     bool
}

type packet struct {
	kind    byte
	payload []byte
}

func openPort(name string) (serial.Port, error) { return openSerial(name, radioBaud) }

// openSerial opens with DTR/RTS low (no reset pulse on Windows); falls back to
// the defaults on ports without modem-control lines.
func openSerial(name string, baud int) (serial.Port, error) {
	p, err := serial.Open(name, &serial.Mode{BaudRate: baud, InitialStatusBits: &serial.ModemOutputBits{}})
	if err != nil && strings.Contains(err.Error(), "modem bits") {
		p, err = serial.Open(name, &serial.Mode{BaudRate: baud})
	}
	return p, err
}

// OpenRadio opens a named port, or probes all candidates when name is "".
func OpenRadio(name string, alias bool, logf func(string, ...any)) (*ESP32Radio, error) {
	names := []string{name}
	if name == "" {
		names = candidatePorts()
		if len(names) == 0 {
			return nil, errors.New("no ESP32 found: plug your ESP32 into this computer (UART/COM port)")
		}
	}
	var problems []string
	for _, n := range names {
		r, err := tryRadio(n, alias, logf)
		if err == nil {
			return r, nil
		}
		problems = append(problems, n+": "+err.Error())
	}
	return nil, fmt.Errorf("no ESP32 radio answered (%s)", strings.Join(problems, "; "))
}

func tryRadio(name string, alias bool, logf func(string, ...any)) (*ESP32Radio, error) {
	p, err := openPort(name)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "busy") || strings.Contains(strings.ToLower(err.Error()), "denied") {
			return nil, errors.New("port in use by another program (close any serial monitor)")
		}
		return nil, err
	}
	p.SetReadTimeout(50 * time.Millisecond)
	r := &ESP32Radio{port: p, portName: name, rx: make(chan []byte, 512), replies: make(chan packet, 16),
		closed: make(chan struct{}), logf: logf}
	go r.readLoop()
	var info []byte
	for i := 0; i < 4 && info == nil; i++ {
		info = r.request(cmdHello, nil, evInfo, time.Second)
	}
	if len(info) < 8 {
		r.Close()
		return nil, errors.New("no reply: install the radio firmware from the ESP32 page")
	}
	r.ownMAC = append([]byte{}, info[2:8]...)
	r.firmware = string(info[8:])
	// HELLO reports the BSSID currently in use, which is still the alias if the
	// board wasn't reset since the last session. Reset it to the radio's own MAC
	// (an all-zero SET_BSSID) and take that as the base.
	if own := r.request(cmdSetBSSID, make([]byte, 6), evBSSID, time.Second); len(own) == 6 {
		r.ownMAC = append([]byte{}, own...)
	}
	r.mac = r.ownMAC
	if alias {
		a := append([]byte{r.ownMAC[0] ^ 0x02}, r.ownMAC[1:]...)
		if got := r.request(cmdSetBSSID, a, evBSSID, time.Second); string(got) != string(a) {
			r.Close()
			return nil, errors.New("this ESP32 has old radio firmware: open the ESP32 page and choose Install firmware")
		}
		r.mac, r.alias = a, true
	}
	return r, nil
}

func (r *ESP32Radio) send(kind byte, payload []byte) error {
	n := len(payload)
	buf := make([]byte, 0, n+5)
	buf = append(buf, syncByte, kind, byte(n), byte(n>>8))
	buf = append(buf, payload...)
	sum := kind + byte(n) + byte(n>>8)
	for _, b := range payload {
		sum += b
	}
	buf = append(buf, sum)
	r.wmu.Lock()
	defer r.wmu.Unlock()
	_, err := r.port.Write(buf)
	return err
}

func (r *ESP32Radio) readLoop() {
	var buf []byte
	tmp := make([]byte, 4096)
	for {
		n, err := r.port.Read(tmp)
		if err != nil {
			select {
			case <-r.closed:
			default:
				r.dead = true
				if r.logf != nil {
					r.logf("ESP32 radio disconnected: %v", err)
				}
				r.Close()
			}
			return
		}
		if n == 0 {
			select {
			case <-r.closed:
				return
			default:
				continue
			}
		}
		buf = append(buf, tmp[:n]...)
		for {
			i := 0
			for i < len(buf) && buf[i] != syncByte {
				i++
			}
			buf = buf[i:]
			if len(buf) < 4 {
				break
			}
			ln := int(buf[2]) | int(buf[3])<<8
			if ln > 1600 {
				buf = buf[1:]
				continue
			}
			if len(buf) < 5+ln {
				break
			}
			var sum byte
			for _, b := range buf[1 : 4+ln] {
				sum += b
			}
			if sum != buf[4+ln] {
				buf = buf[1:]
				continue
			}
			kind, payload := buf[1], append([]byte{}, buf[4:4+ln]...)
			buf = buf[5+ln:]
			r.dispatch(kind, payload)
		}
	}
}

func (r *ESP32Radio) dispatch(kind byte, payload []byte) {
	switch kind {
	case evRx:
		if len(payload) > 2 {
			select {
			case r.rx <- payload[2:]:
			default:
			}
		}
	case evLog:
		if r.logf != nil {
			r.logf("ESP32: %s", payload)
		}
	default:
		select {
		case r.replies <- packet{kind, payload}:
		default:
		}
	}
}

func (r *ESP32Radio) request(kind byte, payload []byte, expect byte, timeout time.Duration) []byte {
	for len(r.replies) > 0 {
		<-r.replies
	}
	if r.send(kind, payload) != nil {
		return nil
	}
	deadline := time.After(timeout)
	for {
		select {
		case p := <-r.replies:
			if p.kind == expect {
				return p.payload
			}
		case <-deadline:
			return nil
		case <-r.closed:
			return nil
		}
	}
}

func (r *ESP32Radio) RadioOn(channel int) error {
	rep := r.request(cmdRadioOn, []byte{byte(channel)}, evRadio, 2*time.Second)
	if len(rep) < 1 || rep[0] == 0 {
		return fmt.Errorf("ESP32 radio failed to start on channel %d", channel)
	}
	return nil
}

func (r *ESP32Radio) MAC() []byte       { return r.mac }
func (r *ESP32Radio) Dead() bool        { return r.dead }
func (r *ESP32Radio) Rx() <-chan []byte { return r.rx }
func (r *ESP32Radio) Tx(frame []byte) error {
	if r.dead {
		return errors.New("ESP32 radio disconnected")
	}
	return r.send(cmdTx, frame)
}
func (r *ESP32Radio) LED(on bool) {
	v := byte(0)
	if on {
		v = 1
	}
	r.send(cmdLed, []byte{v})
}

type RadioStatus struct {
	RxForwarded, RxDropped, TxOK, TxErr, LastTxErr, Channel, RxBad, RxProtected uint32
}

func (r *ESP32Radio) Status() *RadioStatus {
	rep := r.request(cmdStatus, nil, evStatus, 500*time.Millisecond)
	if len(rep) < 24 {
		return nil
	}
	v := make([]uint32, 8)
	for i := 0; i < 8 && 4*i+4 <= len(rep); i++ {
		v[i] = binary.LittleEndian.Uint32(rep[4*i:])
	}
	return &RadioStatus{v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7]}
}

func (r *ESP32Radio) Describe() string {
	mode := "own"
	if r.alias {
		mode = "alias"
	}
	return fmt.Sprintf("ESP32 radio on %s, BSSID %s (%s), firmware %s", r.portName, macStr(r.mac), mode, r.firmware)
}

func (r *ESP32Radio) Close() error {
	r.once.Do(func() {
		if !r.dead {
			r.send(cmdRadioOff, nil)
			r.send(cmdLed, []byte{0})
			time.Sleep(50 * time.Millisecond)
		}
		close(r.closed)
		r.port.Close()
	})
	return nil
}

var _ io.Closer = (*ESP32Radio)(nil)

func macStr(m []byte) string {
	if len(m) != 6 {
		return "?"
	}
	return fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5])
}
