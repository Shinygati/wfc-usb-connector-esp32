package main

import (
	"bytes"
	"encoding/binary"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func tcpFrame(srcMAC, dstMAC []byte, src, dst net.IP, sport, dport uint16, seq, ack uint32, flags byte, payload []byte) []byte {
	tcp := make([]byte, 20)
	binary.BigEndian.PutUint16(tcp, sport)
	binary.BigEndian.PutUint16(tcp[2:], dport)
	binary.BigEndian.PutUint32(tcp[4:], seq)
	binary.BigEndian.PutUint32(tcp[8:], ack)
	tcp[12] = 5 << 4
	tcp[13] = flags
	binary.BigEndian.PutUint16(tcp[14:], 8192)
	tcp = append(tcp, payload...)
	ps := append(append(append([]byte{}, src.To4()...), dst.To4()...), 0, 6, byte(len(tcp)>>8), byte(len(tcp)))
	binary.BigEndian.PutUint16(tcp[16:], checksum(append(ps, tcp...)))
	ip := make([]byte, 20)
	ip[0], ip[8], ip[9] = 0x45, 64, 6
	binary.BigEndian.PutUint16(ip[2:], uint16(20+len(tcp)))
	copy(ip[12:], src.To4())
	copy(ip[16:], dst.To4())
	binary.BigEndian.PutUint16(ip[10:], checksum(ip))
	eth := append(append(append([]byte{}, dstMAC...), srcMAC...), 8, 0)
	return append(append(eth, ip...), tcp...)
}

// TestTCPThroughStack: a console-style HTTP GET through the network stack to a local server.
func TestTCPThroughStack(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go http.Serve(ln, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Organization", "Nintendo")
		w.Write([]byte("ok"))
	}))
	port := uint16(ln.Addr().(*net.TCPAddr).Port)
	testNAT = map[string]string{"192.168.77.254": "127.0.0.1"}
	defer func() { testNAT = nil }()

	out := make(chan []byte, 64)
	netw, err := NewNetwork("system", func(f []byte) { out <- append([]byte{}, f...) }, t.Logf)
	if err != nil {
		t.Fatal(err)
	}
	defer netw.Close()
	ds := net.IP{192, 168, 77, 2}
	srv := net.IP{192, 168, 77, 254}
	gw, _ := net.ParseMAC(gatewayMAC)

	// ARP is skipped: send straight to the gateway MAC.
	netw.Input(tcpFrame(dsMAC, gw, ds, srv, 40000, port, 1000, 0, 0x02, nil))
	var srvSeq uint32
	wait := func(want func(flags byte, payload []byte) bool) []byte {
		deadline := time.After(3 * time.Second)
		for {
			select {
			case f := <-out:
				if len(f) >= 42 && binary.BigEndian.Uint16(f[12:]) == 0x0806 && binary.BigEndian.Uint16(f[20:]) == 1 && net.IP(f[38:42]).Equal(ds) {
					t.Logf("ARP request for the DS: answering")
					r := append(append(append([]byte{}, f[6:12]...), dsMAC...), 8, 6, 0, 1, 8, 0, 6, 4, 0, 2)
					r = append(append(append(append(r, dsMAC...), ds.To4()...), f[22:28]...), f[28:32]...)
					netw.Input(r)
					continue
				}
				if len(f) < 54 || binary.BigEndian.Uint16(f[12:]) != 0x0800 || f[23] != 6 {
					continue
				}
				ip := f[14:]
				tcp := ip[20:]
				off := int(tcp[12]>>4) * 4
				if want(tcp[13], tcp[off:]) {
					srvSeq = binary.BigEndian.Uint32(tcp[4:])
					return tcp[off:]
				}
			case <-deadline:
				t.Fatal("timeout waiting for server segment")
			}
		}
	}
	wait(func(fl byte, _ []byte) bool { return fl&0x12 == 0x12 })
	req := []byte("GET / HTTP/1.0\r\nHost: conntest.nintendowifi.net\r\n\r\n")
	netw.Input(tcpFrame(dsMAC, gw, ds, srv, 40000, port, 1001, srvSeq+1, 0x10, nil))
	netw.Input(tcpFrame(dsMAC, gw, ds, srv, 40000, port, 1001, srvSeq+1, 0x18, req))
	resp := wait(func(_ byte, p []byte) bool { return bytes.HasPrefix(p, []byte("HTTP/")) })
	if !strings.Contains(string(resp), "200 OK") || !strings.Contains(string(resp), "X-Organization: Nintendo") {
		t.Fatalf("response: %q", resp)
	}
}
