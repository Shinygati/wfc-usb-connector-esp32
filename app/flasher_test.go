//go:build linux

package main

import (
	"bytes"
	"compress/zlib"
	"crypto/md5"
	"encoding/binary"
	"encoding/hex"
	"io"
	"os"
	"strconv"
	"testing"
	"unsafe"

	"golang.org/x/sys/unix"
)

func openPTY(t *testing.T) (*os.File, string) {
	m, err := os.OpenFile("/dev/ptmx", os.O_RDWR, 0)
	if err != nil {
		t.Skip("no pty")
	}
	var unlock int32
	unix.Syscall(unix.SYS_IOCTL, m.Fd(), unix.TIOCSPTLCK, uintptr(unsafe.Pointer(&unlock)))
	n, err := unix.IoctlGetInt(int(m.Fd()), unix.TIOCGPTN)
	if err != nil {
		t.Skip("no pty")
	}
	return m, "/dev/pts/" + strconv.Itoa(n)
}

// fakeROM emulates the ESP32-S3 ROM serial bootloader.
func fakeROM(t *testing.T, m *os.File, done chan []byte) {
	var raw, comp []byte
	var size int
	buf := make([]byte, 4096)
	reply := func(op byte, val uint32, data []byte) {
		body := append(append([]byte{}, data...), 0, 0, 0, 0)
		p := make([]byte, 8)
		p[0], p[1] = 1, op
		binary.LittleEndian.PutUint16(p[2:], uint16(len(body)))
		binary.LittleEndian.PutUint32(p[4:], val)
		p = append(p, body...)
		out := []byte{0xC0}
		for _, c := range p {
			switch c {
			case 0xC0:
				out = append(out, 0xDB, 0xDC)
			case 0xDB:
				out = append(out, 0xDB, 0xDD)
			default:
				out = append(out, c)
			}
		}
		m.Write(append(out, 0xC0))
	}
	for {
		n, err := m.Read(buf)
		if err != nil {
			return
		}
		raw = append(raw, buf[:n]...)
		for {
			i := bytes.IndexByte(raw, 0xC0)
			if i < 0 {
				raw = nil
				break
			}
			j := bytes.IndexByte(raw[i+1:], 0xC0)
			if j < 0 {
				raw = raw[i:]
				break
			}
			f := raw[i+1 : i+1+j]
			raw = raw[i+1+j:]
			if len(f) == 0 {
				continue
			}
			f = bytes.ReplaceAll(bytes.ReplaceAll(f, []byte{0xDB, 0xDC}, []byte{0xC0}), []byte{0xDB, 0xDD}, []byte{0xDB})
			op, data := f[1], f[8:]
			chk := binary.LittleEndian.Uint32(f[4:])
			switch op {
			case espSecInfo:
				d := make([]byte, 20)
				binary.LittleEndian.PutUint32(d[12:], 9)
				reply(op, 0, d)
			case espDeflBegin:
				size = int(binary.LittleEndian.Uint32(data))
				if len(data) != 20 {
					t.Errorf("DEFL_BEGIN for S3 ROM must have 5 words, got %d bytes", len(data))
				}
				reply(op, 0, nil)
			case espDeflData:
				ln := binary.LittleEndian.Uint32(data)
				chunk := data[16 : 16+ln]
				var c byte = 0xEF
				for _, b := range chunk {
					c ^= b
				}
				if uint32(c) != chk {
					t.Errorf("bad checksum in block %d", binary.LittleEndian.Uint32(data[4:]))
				}
				comp = append(comp, chunk...)
				reply(op, 0, nil)
			case espFlashMD5:
				zr, err := zlib.NewReader(bytes.NewReader(comp))
				if err != nil {
					t.Error(err)
					return
				}
				img, _ := io.ReadAll(zr)
				want := int(binary.LittleEndian.Uint32(data[4:]))
				sum := md5.Sum(img[:want])
				reply(op, 0, []byte(hex.EncodeToString(sum[:])))
				_ = size
				done <- img
			default:
				reply(op, 0, nil)
			}
		}
	}
}

func TestFlasherAgainstFakeROM(t *testing.T) {
	m, slave := openPTY(t)
	defer m.Close()
	// Hold the slave open so reads on the master don't fail with EIO before the flasher opens it.
	hold, err := os.OpenFile(slave, os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		t.Skip(err)
	}
	defer hold.Close()
	done := make(chan []byte, 1)
	go fakeROM(t, m, done)
	var last FlashProgress
	if err := FlashFirmware(slave, false, func(p FlashProgress) { last = p }); err != nil {
		t.Fatal(err)
	}
	img := <-done
	want, _ := firmwareImage("esp32s3")
	if !bytes.Equal(img[:len(want)], want) {
		t.Fatal("flashed image differs")
	}
	if !last.Done || last.Chip != "ESP32-S3" {
		t.Fatalf("progress = %+v", last)
	}
}
