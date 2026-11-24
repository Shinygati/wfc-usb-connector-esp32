package main

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type fakeUSB struct {
	regs map[uint16]uint16
	log  []string
}

func goldenEEPROM() []byte {
	e := make([]byte, 0x6E)
	for i := range e {
		e[i] = byte(i*7 + 3)
	}
	m, _ := hex.DecodeString("0016016a2b3c")
	copy(e[4:], m)
	binary.LittleEndian.PutUint16(e[0x0B*2:], 5<<11|1<<2|2<<4)
	for i := 0; i < 16; i++ {
		binary.LittleEndian.PutUint16(e[(0x0E+i)*2:], 0xFFFF)
	}
	binary.LittleEndian.PutUint16(e[0x0E*2:], 0x1A2B)
	binary.LittleEndian.PutUint16(e[0x10*2:], 0x0C44)
	for i := 0; i < 14; i++ {
		e[0x3C+i] = byte(20 + i)
		if i == 3 {
			e[0x3C+i] = 40
		}
	}
	return e
}

func (f *fakeUSB) Control(t, req uint8, value, index uint16, data []byte, _ time.Duration) (int, error) {
	if t == 0xC0 {
		if req == 9 {
			return copy(data, goldenEEPROM()), nil
		}
		v, ok := map[uint16]uint16{0x4D0: 0, 0x4D4: 0, 0x422: 0x1E0, 0x4CE: 0x12, 0x400: 4}[index]
		if !ok {
			v = f.regs[index]
		}
		f.log = append(f.log, fmt.Sprintf("R %04x", index))
		binary.LittleEndian.PutUint16(data, v)
		return 2, nil
	}
	f.log = append(f.log, fmt.Sprintf("W %02x %d %04x %04x %s", t, req, value, index, hex.EncodeToString(data)))
	if req == 6 && len(data) == 2 {
		f.regs[index] = binary.LittleEndian.Uint16(data)
	}
	return len(data), nil
}
func (f *fakeUSB) BulkOut(ep uint8, d []byte, _ time.Duration) (int, error) {
	f.log = append(f.log, fmt.Sprintf("B %02x %s", ep, hex.EncodeToString(d)))
	return len(d), nil
}
func (f *fakeUSB) BulkIn(uint8, []byte, time.Duration) (int, error) {
	time.Sleep(5 * time.Millisecond)
	return 0, errUSBTimeout
}
func (f *fakeUSB) Close() error { return nil }

// TestRT2570MatchesPython: every USB request of the Go driver must match the
// original Python driver (proto/rt2570.py) for bring-up, AP setup and TX.
// testdata/rt2570_reference.py runs the Python driver on the same simulated adapter.
func TestRT2570MatchesPython(t *testing.T) {
	// Path to the original project's proto/ folder (clone of edgarburgues/wfc-usb-connector).
	proto := os.Getenv("WFC_ORIGINAL_PROTO")
	if proto == "" {
		proto = filepath.Join(os.Getenv("HOME"), "wfc", "proto")
	}
	if _, err := os.Stat(filepath.Join(proto, "rt2570.py")); err != nil {
		t.Skip("original project not found: set WFC_ORIGINAL_PROTO to its proto folder to run this comparison")
	}
	out, err := exec.Command("python3", filepath.Join("testdata", "rt2570_reference.py"), proto).CombinedOutput()
	if err != nil {
		t.Skipf("python trace unavailable: %v %s", err, out)
	}
	want := strings.Split(strings.TrimSpace(string(out)), "\n")

	f := &fakeUSB{regs: map[uint16]uint16{}}
	r, err := newRT2570(f, t.Logf)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.RadioOn(6); err != nil {
		t.Fatal(err)
	}
	r.LED(true)
	mac, _ := hex.DecodeString("0016016a2b3c")
	bc := append(append(append(append([]byte{0x80, 0, 0, 0}, bcast...), mac...), mac...), 0x10, 0)
	bc = append(bc, []byte(strings.Repeat("\x01", 13))...)
	uni, _ := hex.DecodeString("b00000000009bf123456")
	uni = append(append(append(uni, mac...), mac...), 0x20, 0)
	uni = append(uni, []byte(strings.Repeat("\x02", 6))...)
	r.Tx(bc)
	r.Tx(uni)
	close(r.stop)
	r.wg.Wait()
	got := f.log
	if len(got) != len(want) {
		t.Errorf("request count: go %d, python %d", len(got), len(want))
	}
	for i := 0; i < len(got) && i < len(want); i++ {
		if got[i] != want[i] {
			t.Fatalf("request %d differs:\n  go:     %s\n  python: %s", i, got[i], want[i])
		}
	}
	t.Logf("%d USB requests identical to the Python driver", len(got))
}

func TestParseRx(t *testing.T) {
	frame := []byte("0123456789abcdefghij") // 20 bytes incl. 4-byte FCS
	desc := make([]byte, rxdSize)
	binary.LittleEndian.PutUint32(desc, uint32(len(frame))<<16)
	if got := parseRx(append(append([]byte{}, frame...), desc...)); string(got) != "0123456789abcdef" {
		t.Fatalf("got %q", got)
	}
	binary.LittleEndian.PutUint32(desc, uint32(len(frame))<<16|0x20)
	if parseRx(append(append([]byte{}, frame...), desc...)) != nil {
		t.Fatal("CRC-error frame accepted")
	}
}
