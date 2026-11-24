package main

// Console networking: DHCP, DNS and NAT in a userspace TCP/IP stack
// (gvisor-tap-vsock), replacing libslirp. Frames go to the switch over an
// in-memory pipe using the QEMU framing (4-byte big-endian length).
//
// DNS: the console is told to use the gateway (192.168.77.1). With a DNS
// service selected (e.g. Kaeru WFC), its UDP queries to the gateway are
// rewritten to the service's address on the way in, and the replies
// rewritten back on the way out, so the console never needs DNS settings.

import (
	"context"
	"encoding/binary"
	"io"
	"net"
	"sync"

	"github.com/containers/gvisor-tap-vsock/pkg/types"
	"github.com/containers/gvisor-tap-vsock/pkg/virtualnetwork"
	"github.com/sirupsen/logrus"
)

const (
	netSubnet  = "192.168.77.0/24"
	netGateway = "192.168.77.1"
	gatewayMAC = "5a:94:ef:e4:0c:dd"
	netMTU     = 1400 // ESP32 raw TX is limited to 1500-byte 802.11 frames (incl. WEP/LLC)
)

type Network struct {
	logf     func(string, ...any)
	seenTCP  map[string]bool
	vn       *virtualnetwork.VirtualNetwork
	conn     net.Conn
	cancel   context.CancelFunc
	out      func(eth []byte)
	dns      net.IP // nil = PC's resolver (gateway DNS server)
	gw       net.IP
	wmu      sync.Mutex
	RxFrames uint64
	TxFrames uint64
}

func init() { logrus.SetLevel(logrus.FatalLevel); logrus.SetOutput(io.Discard) }

// testNAT lets tests reach a local server through the stack.
var testNAT map[string]string

func NewNetwork(dns string, out func(eth []byte), logf func(string, ...any)) (*Network, error) {
	cfg := &types.Configuration{
		MTU:               netMTU,
		Subnet:            netSubnet,
		GatewayIP:         netGateway,
		GatewayMacAddress: gatewayMAC,
		Protocol:          types.QemuProtocol,
	}
	if testNAT != nil {
		cfg.NAT = testNAT
		for k := range testNAT {
			cfg.GatewayVirtualIPs = append(cfg.GatewayVirtualIPs, k)
		}
	}
	vn, err := virtualnetwork.New(cfg)
	if err != nil {
		return nil, err
	}
	n := &Network{vn: vn, out: out, gw: net.ParseIP(netGateway).To4(), logf: logf, seenTCP: map[string]bool{}}
	if ip := net.ParseIP(dns); ip != nil && ip.To4() != nil {
		n.dns = ip.To4()
	}
	ours, theirs := net.Pipe()
	n.conn = ours
	ctx, cancel := context.WithCancel(context.Background())
	n.cancel = cancel
	go vn.AcceptQemu(ctx, theirs)
	go n.readLoop()
	return n, nil
}

// Input takes an Ethernet frame from a console.
func (n *Network) Input(eth []byte) {
	eth = append([]byte{}, eth...)
	n.inspect(eth, true)
	if n.dns != nil {
		rewriteUDP(eth, n.gw, n.dns, 53, true)
	}
	hdr := make([]byte, 4)
	binary.BigEndian.PutUint32(hdr, uint32(len(eth)))
	n.wmu.Lock()
	defer n.wmu.Unlock()
	n.RxFrames++
	n.conn.Write(append(hdr, eth...))
}

func (n *Network) readLoop() {
	hdr := make([]byte, 4)
	for {
		if _, err := readFull(n.conn, hdr); err != nil {
			return
		}
		size := binary.BigEndian.Uint32(hdr)
		if size > 65536 {
			return
		}
		frame := make([]byte, size)
		if _, err := readFull(n.conn, frame); err != nil {
			return
		}
		if n.dns != nil {
			rewriteUDP(frame, n.dns, n.gw, 53, false)
		}
		n.TxFrames++
		n.inspect(frame, false)
		n.out(frame)
	}
}

func readFull(c net.Conn, b []byte) (int, error) {
	got := 0
	for got < len(b) {
		k, err := c.Read(b[got:])
		if err != nil {
			return got, err
		}
		got += k
	}
	return got, nil
}

func (n *Network) Close() {
	n.cancel()
	n.conn.Close()
	n.vn.Close()
}

// rewriteUDP swaps an IPv4 address on UDP packets to/from port.
// inbound: dst==from && dstPort==port -> dst=to. outbound: src==from && srcPort==port -> src=to.
func rewriteUDP(eth []byte, from, to net.IP, port uint16, inbound bool) {
	if len(eth) < 14+20+8 || binary.BigEndian.Uint16(eth[12:]) != 0x0800 {
		return
	}
	ip := eth[14:]
	ihl := int(ip[0]&15) * 4
	if ip[0]>>4 != 4 || ihl < 20 || len(ip) < ihl+8 || ip[9] != 17 {
		return
	}
	udp := ip[ihl:]
	addrOff, portOff := 16, 2
	if !inbound {
		addrOff, portOff = 12, 0
	}
	if !net.IP(ip[addrOff:addrOff+4]).Equal(from) || binary.BigEndian.Uint16(udp[portOff:]) != port {
		return
	}
	copy(ip[addrOff:addrOff+4], to)
	ip[10], ip[11] = 0, 0
	binary.BigEndian.PutUint16(ip[10:], checksum(ip[:ihl]))
	udp[6], udp[7] = 0, 0 // UDP checksum optional on IPv4
}

func checksum(b []byte) uint16 {
	var s uint32
	for i := 0; i+1 < len(b); i += 2 {
		s += uint32(binary.BigEndian.Uint16(b[i:]))
	}
	if len(b)%2 == 1 {
		s += uint32(b[len(b)-1]) << 8
	}
	for s>>16 != 0 {
		s = s&0xffff + s>>16
	}
	return ^uint16(s)
}

// inspect logs DHCP leases, DNS lookups/answers and new TCP connections so the
// Activity page shows how far a console's connection test gets.
func (n *Network) inspect(eth []byte, fromDS bool) {
	if n.logf == nil || len(eth) < 14+20 || binary.BigEndian.Uint16(eth[12:]) != 0x0800 {
		return
	}
	ip := eth[14:]
	ihl := int(ip[0]&15) * 4
	if ihl < 20 || len(ip) < ihl+8 {
		return
	}
	src, dst := net.IP(ip[12:16]), net.IP(ip[16:20])
	l4 := ip[ihl:]
	sport, dport := binary.BigEndian.Uint16(l4), binary.BigEndian.Uint16(l4[2:])
	switch ip[9] {
	case 17: // UDP
		payload := l4[8:]
		switch {
		case !fromDS && sport == 67 && len(payload) >= 240:
			typ := dhcpType(payload)
			if typ == 2 || typ == 5 {
				n.logf("DHCP: gave %s to the DS (%s)", net.IP(payload[16:20]), map[byte]string{2: "offer", 5: "ack"}[typ])
			}
		case fromDS && dport == 53:
			if name, _ := dnsName(payload); name != "" {
				n.logf("DNS: DS looks up %s", name)
			}
		case !fromDS && sport == 53:
			name, ans := dnsName(payload)
			if name != "" {
				if ans == "" {
					ans = "no address (rcode " + itoa(int(payload[3]&15)) + ")"
				}
				n.logf("DNS: %s -> %s", name, ans)
			}
		}
	case 6:
		if len(l4) < 20 {
			return
		}
		flags := l4[13]
		off := int(l4[12]>>4) * 4
		var payload []byte
		if off >= 20 && len(l4) > off {
			payload = l4[off:]
		}
		conn := dst.String() + ":" + itoa(int(dport)) + "/" + itoa(int(sport))
		if !fromDS {
			conn = src.String() + ":" + itoa(int(sport)) + "/" + itoa(int(dport))
		}
		once := func(tag, msg string) {
			if !n.seenTCP[conn+tag] {
				n.seenTCP[conn+tag] = true
				n.logf("%s", msg)
			}
		}
		server := conn[:len(conn)-len("/")-len(itoa(int(dport)))]
		if !fromDS {
			server = src.String() + ":" + itoa(int(sport))
		} else {
			server = dst.String() + ":" + itoa(int(dport))
		}
		switch {
		case fromDS && flags&0x12 == 0x02:
			once("syn", "TCP: DS connects to "+server)
		case !fromDS && flags&0x12 == 0x12:
			once("synack", "TCP: "+server+" accepted the connection")
		case !fromDS && flags&0x04 != 0:
			once("rst", "TCP: "+server+" connection reset (server unreachable or refused)")
		case fromDS && flags&0x04 != 0:
			once("dsrst", "TCP: DS reset the connection to "+server)
		}
		if len(payload) > 0 {
			line := firstLine(payload)
			if fromDS && (len(line) > 4 && (line[:4] == "GET " || line[:5] == "POST ")) {
				once("req", "HTTP: DS sends "+line)
			} else if !fromDS && len(line) > 5 && line[:5] == "HTTP/" {
				once("resp", "HTTP: "+server+" replies "+line)
			}
		}
		if flags&0x01 != 0 {
			who := "server"
			if fromDS {
				who = "DS"
			}
			once("fin"+who, "TCP: "+who+" closed the connection to "+server)
		}
	}
}

func dhcpType(p []byte) byte {
	for i := 240; i+1 < len(p); {
		code := p[i]
		if code == 255 {
			break
		}
		if code == 0 {
			i++
			continue
		}
		l := int(p[i+1])
		if code == 53 && l == 1 && i+2 < len(p) {
			return p[i+2]
		}
		i += 2 + l
	}
	return 0
}

// dnsName returns the first question name and the first A record (if any).
func dnsName(p []byte) (string, string) {
	if len(p) < 12 {
		return "", ""
	}
	qd, an := binary.BigEndian.Uint16(p[4:]), binary.BigEndian.Uint16(p[6:])
	if qd == 0 {
		return "", ""
	}
	i, name := 12, ""
	for i < len(p) && p[i] != 0 {
		l := int(p[i])
		if l&0xc0 != 0 || i+1+l > len(p) {
			return name, ""
		}
		if name != "" {
			name += "."
		}
		name += string(p[i+1 : i+1+l])
		i += 1 + l
	}
	i += 5 // zero byte + qtype + qclass
	for a := 0; a < int(an) && i+12 <= len(p); a++ {
		if p[i]&0xc0 == 0xc0 {
			i += 2
		} else {
			for i < len(p) && p[i] != 0 {
				i += 1 + int(p[i])
			}
			i++
		}
		if i+10 > len(p) {
			break
		}
		typ, rdl := binary.BigEndian.Uint16(p[i:]), int(binary.BigEndian.Uint16(p[i+8:]))
		i += 10
		if typ == 1 && rdl == 4 && i+4 <= len(p) {
			return name, net.IP(p[i : i+4]).String()
		}
		i += rdl
	}
	return name, ""
}

func firstLine(p []byte) string {
	end := len(p)
	if end > 120 {
		end = 120
	}
	for i := 0; i < end; i++ {
		if p[i] == '\r' || p[i] == '\n' {
			end = i
			break
		}
	}
	s := []byte{}
	for _, c := range p[:end] {
		if c >= 32 && c < 127 {
			s = append(s, c)
		}
	}
	return string(s)
}
