package main

// Built-in ESP32 flasher: the ROM serial bootloader protocol (as used by
// esptool) with compressed writes. Detects the chip and writes the matching
// embedded radio firmware image (a merged image flashed at 0x0).

import (
	"bytes"
	"compress/zlib"
	"crypto/md5"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"go.bug.st/serial"
)

const (
	espSync         = 0x08
	espReadReg      = 0x0A
	espSPISetParams = 0x0B
	espSPIAttach    = 0x0D
	espChangeBaud   = 0x0F
	espDeflBegin    = 0x10
	espDeflData     = 0x11
	espDeflEnd      = 0x12
	espFlashMD5     = 0x13
	espSecInfo      = 0x14

	romBlock  = 0x400 // ROM loader FLASH_WRITE_SIZE
	flashBaud = 460800
)

type FlashProgress struct {
	Active  bool   `json:"active"`
	Percent int    `json:"percent"`
	Message string `json:"message"`
	Error   string `json:"error,omitempty"`
	Done    bool   `json:"done"`
	Chip    string `json:"chip,omitempty"`
}

type espLoader struct {
	p      serial.Port
	native bool // Espressif USB-Serial/JTAG
	dtr    bool
	buf    []byte
}

func (l *espLoader) writeSLIP(b []byte) error {
	out := []byte{0xC0}
	for _, c := range b {
		switch c {
		case 0xC0:
			out = append(out, 0xDB, 0xDC)
		case 0xDB:
			out = append(out, 0xDB, 0xDD)
		default:
			out = append(out, c)
		}
	}
	out = append(out, 0xC0)
	_, err := l.p.Write(out)
	return err
}

func (l *espLoader) readSLIP(timeout time.Duration) ([]byte, error) {
	deadline := time.Now().Add(timeout)
	tmp := make([]byte, 1024)
	for {
		if i := bytes.IndexByte(l.buf, 0xC0); i >= 0 {
			if j := bytes.IndexByte(l.buf[i+1:], 0xC0); j >= 0 {
				raw := l.buf[i+1 : i+1+j]
				l.buf = l.buf[i+1+j:]
				if len(raw) == 0 {
					continue
				}
				var out []byte
				for k := 0; k < len(raw); k++ {
					if raw[k] == 0xDB && k+1 < len(raw) {
						k++
						if raw[k] == 0xDC {
							out = append(out, 0xC0)
						} else {
							out = append(out, 0xDB)
						}
					} else {
						out = append(out, raw[k])
					}
				}
				return out, nil
			}
		}
		if time.Now().After(deadline) {
			return nil, errors.New("timeout waiting for the chip")
		}
		l.p.SetReadTimeout(50 * time.Millisecond)
		n, err := l.p.Read(tmp)
		if err != nil {
			return nil, err
		}
		l.buf = append(l.buf, tmp[:n]...)
	}
}

// command sends a request and returns (value, data-without-status).
func (l *espLoader) command(op byte, data []byte, chk uint32, dataLen int, timeout time.Duration) (uint32, []byte, error) {
	pkt := make([]byte, 8, 8+len(data))
	pkt[1] = op
	binary.LittleEndian.PutUint16(pkt[2:], uint16(len(data)))
	binary.LittleEndian.PutUint32(pkt[4:], chk)
	pkt = append(pkt, data...)
	if err := l.writeSLIP(pkt); err != nil {
		return 0, nil, err
	}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		r, err := l.readSLIP(time.Until(deadline))
		if err != nil {
			return 0, nil, err
		}
		if len(r) < 8 || r[0] != 1 || r[1] != op {
			continue
		}
		val := binary.LittleEndian.Uint32(r[4:])
		body := r[8:]
		if len(body) < dataLen+2 {
			return 0, nil, fmt.Errorf("short reply to 0x%02x", op)
		}
		if body[dataLen] != 0 {
			return 0, nil, fmt.Errorf("chip rejected command 0x%02x (error 0x%02x)", op, body[dataLen+1])
		}
		return val, body[:dataLen], nil
	}
	return 0, nil, fmt.Errorf("timeout on command 0x%02x", op)
}

// Line helpers. On Windows (usbser.sys and some bridge drivers) a new RTS state is
// only sent together with a DTR write, so re-write DTR after RTS (as esptool does).
func (l *espLoader) setDTR(v bool) { l.dtr = v; l.p.SetDTR(v) }
func (l *espLoader) setRTS(v bool) { l.p.SetRTS(v); l.p.SetDTR(l.dtr) }

// classicReset: EN low, then IO0 low while EN rises (auto-reset circuit on UART boards).
func (l *espLoader) classicReset(delay time.Duration) {
	l.setDTR(false)
	l.setRTS(true)
	time.Sleep(100 * time.Millisecond)
	l.setDTR(true)
	l.setRTS(false)
	time.Sleep(delay)
	l.setDTR(false)
}

// jtagReset: reset sequence for the ESP32's own USB-Serial/JTAG port (esptool USBJTAGSerialReset).
func (l *espLoader) jtagReset() {
	l.setRTS(false)
	l.setDTR(false)
	time.Sleep(100 * time.Millisecond)
	l.setDTR(true)
	l.setRTS(false)
	time.Sleep(100 * time.Millisecond)
	l.setRTS(true)
	l.setDTR(false)
	l.setRTS(true)
	time.Sleep(100 * time.Millisecond)
	l.setDTR(false)
	l.setRTS(false)
}

// enterBootloader tries: the board already in download mode (BOOT+RESET by hand),
// then automatic resets with the short and long timings esptool uses.
func (l *espLoader) enterBootloader(report func(string)) error {
	if l.sync(3) == nil {
		return nil
	}
	resets := []func(){
		func() { l.classicReset(50 * time.Millisecond) },
		func() { l.classicReset(550 * time.Millisecond) },
	}
	if l.native {
		resets = []func(){l.jtagReset, resets[0], resets[1]}
	}
	for round := 0; round < 2; round++ {
		for _, reset := range resets {
			reset()
			time.Sleep(50 * time.Millisecond)
			if l.sync(5) == nil {
				return nil
			}
		}
	}
	return errors.New("no answer from the bootloader")
}

func (l *espLoader) hardReset() {
	l.setDTR(false)
	l.setRTS(true)
	time.Sleep(100 * time.Millisecond)
	l.setRTS(false)
}

func (l *espLoader) sync(tries int) error {
	data := append([]byte{0x07, 0x07, 0x12, 0x20}, bytes.Repeat([]byte{0x55}, 32)...)
	for attempt := 0; attempt < tries; attempt++ {
		l.p.ResetInputBuffer()
		l.buf = nil
		if _, _, err := l.command(espSync, data, 0, 0, 120*time.Millisecond); err == nil {
			time.Sleep(50 * time.Millisecond)
			l.p.ResetInputBuffer() // ROM answers SYNC several times
			l.buf = nil
			return nil
		}
	}
	return errors.New("no answer from the ROM bootloader")
}

var chipBySecID = map[uint32]string{2: "esp32s2", 9: "esp32s3", 5: "esp32c3", 13: "esp32c6", 23: "esp32c5", 12: "esp32c2", 16: "esp32h2", 18: "esp32p4"}
var chipByMagic = map[uint32]string{0x00f01d83: "esp32", 0x000007c6: "esp32s2", 0x9: "esp32s3",
	0x6921506f: "esp32c3", 0x1b31506f: "esp32c3", 0x4881606f: "esp32c3", 0x4361606f: "esp32c3",
	0x2ce0806f: "esp32c6", 0x6f51306f: "esp32c2", 0x7c41a06f: "esp32c2"}

func (l *espLoader) detect() (string, error) {
	if _, d, err := l.command(espSecInfo, nil, 0, 20, time.Second); err == nil {
		if c, ok := chipBySecID[binary.LittleEndian.Uint32(d[12:])]; ok {
			return c, nil
		}
	}
	l.p.ResetInputBuffer()
	l.buf = nil
	v, _, err := l.command(espReadReg, le32(0x40001000), 0, 0, time.Second)
	if err != nil {
		return "", err
	}
	if c, ok := chipByMagic[v]; ok {
		return c, nil
	}
	return "", fmt.Errorf("unknown chip (magic 0x%08x)", v)
}

func le32(vs ...uint32) []byte {
	b := make([]byte, 4*len(vs))
	for i, v := range vs {
		binary.LittleEndian.PutUint32(b[4*i:], v)
	}
	return b
}

var chipNames = map[string]string{"esp32": "ESP32", "esp32s2": "ESP32-S2", "esp32s3": "ESP32-S3", "esp32c3": "ESP32-C3",
	"esp32c5": "ESP32-C5", "esp32c6": "ESP32-C6", "esp32c2": "ESP32-C2", "esp32h2": "ESP32-H2 (no Wi-Fi)", "esp32p4": "ESP32-P4 (no Wi-Fi)"}

// FlashFirmware writes the radio firmware for whatever chip is on port.
func FlashFirmware(port string, native bool, progress func(FlashProgress)) error {
	report := func(pct int, msg string) { progress(FlashProgress{Active: true, Percent: pct, Message: msg}) }
	report(0, "Opening "+port)
	p, err := openSerial(port, 115200)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "busy") || strings.Contains(strings.ToLower(err.Error()), "denied") {
			return errors.New(port + " is in use: stop the connector and close any serial monitor")
		}
		return err
	}
	defer p.Close()
	l := &espLoader{p: p, native: native}
	report(1, "Connecting to the ESP32 on "+port)
	if err := l.enterBootloader(func(m string) { report(1, m) }); err != nil {
		return fmt.Errorf("couldn't reach the bootloader on %s. Put the ESP32 in download mode by hand: hold BOOT, press and release RESET (EN), release BOOT, then click Install firmware again (don't unplug it). Also check the right board is selected on the Connection page", port)
	}
	chip, err := l.detect()
	if err != nil {
		return err
	}
	image, ok := firmwareImage(chip)
	if !ok {
		return fmt.Errorf("%s is not supported (no Wi-Fi radio firmware for it)", chipNames[chip])
	}
	progress(FlashProgress{Active: true, Percent: 3, Message: "Found " + chipNames[chip], Chip: chipNames[chip]})
	if _, _, err := l.command(espSPIAttach, make([]byte, 8), 0, 0, 3*time.Second); err != nil {
		return err
	}
	if _, _, err := l.command(espSPISetParams, le32(0, 4<<20, 0x10000, 0x1000, 0x100, 0xffff), 0, 0, 3*time.Second); err != nil {
		return err
	}
	if _, _, err := l.command(espChangeBaud, le32(flashBaud, 0), 0, 0, 3*time.Second); err == nil {
		p.SetMode(&serial.Mode{BaudRate: flashBaud})
		time.Sleep(50 * time.Millisecond)
		p.ResetInputBuffer()
		l.buf = nil
	}

	var z bytes.Buffer
	zw, _ := zlib.NewWriterLevel(&z, zlib.BestCompression)
	zw.Write(image)
	zw.Close()
	comp := z.Bytes()
	blocks := (len(comp) + romBlock - 1) / romBlock
	eraseBlocks := (len(image) + romBlock - 1) / romBlock
	begin := le32(uint32(eraseBlocks*romBlock), uint32(blocks), romBlock, 0)
	if chip != "esp32" {
		begin = append(begin, le32(0)...) // encrypted-write flag (ROMs after the ESP32)
	}
	report(4, "Erasing flash")
	eraseTimeout := time.Duration(len(image)/1024/8+10) * time.Second
	if _, _, err := l.command(espDeflBegin, begin, 0, 0, eraseTimeout); err != nil {
		return err
	}
	for i := 0; i < blocks; i++ {
		chunk := comp[i*romBlock : min((i+1)*romBlock, len(comp))]
		var chk byte = 0xEF
		for _, c := range chunk {
			chk ^= c
		}
		d := append(le32(uint32(len(chunk)), uint32(i), 0, 0), chunk...)
		var err error
		for retry := 0; retry < 3; retry++ {
			if _, _, err = l.command(espDeflData, d, uint32(chk), 0, 10*time.Second); err == nil {
				break
			}
		}
		if err != nil {
			return fmt.Errorf("write failed at block %d/%d: %v", i+1, blocks, err)
		}
		report(5+90*(i+1)/blocks, fmt.Sprintf("Writing firmware (%d%%)", 100*(i+1)/blocks))
	}
	report(96, "Verifying")
	_, d, err := l.command(espFlashMD5, le32(0, uint32(len(image)), 0, 0), 0, 32, 30*time.Second)
	if err == nil {
		sum := md5.Sum(image)
		if !strings.EqualFold(string(d), hex.EncodeToString(sum[:])) {
			return errors.New("verification failed: flash contents do not match")
		}
	}
	l.command(espDeflEnd, le32(1), 0, 0, time.Second) // 1 = stay in loader; we reset below
	report(99, "Restarting the board")
	l.hardReset()
	progress(FlashProgress{Percent: 100, Message: "Firmware installed on " + chipNames[chip], Done: true, Chip: chipNames[chip]})
	return nil
}
