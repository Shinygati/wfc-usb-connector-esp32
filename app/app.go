package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

type Console struct {
	MAC    string `json:"mac"`
	Name   string `json:"name"`
	DSName string `json:"dsName,omitempty"` // username the DS last reported
	Added  int64  `json:"added"`
}

type Settings struct {
	SSID    string    `json:"ssid"`
	Allowed []Console `json:"allowed"`
	DNS     string    `json:"dns"`
	Channel int       `json:"channel"`
	Port    string    `json:"port"`
	Radio   string    `json:"radio"` // "esp32" (default) or "original"
}

type DNSPreset struct {
	Name, Address, Note string
}

var dnsPresets = []DNSPreset{
	{"Kaeru WFC / Wiimmfi", "178.62.43.212", "Recommended. Pokémon and most DS games."},
	{"Wiimmfi", "95.217.77.151", "Wiimmfi directly."},
	{"AltWFC", "172.104.88.237", "Separate WFC network. Game support varies."},
	{"WiiLink WFC", "5.161.56.11", "Separate WFC network. Game support varies."},
	{"This computer's DNS", "system", "No WFC redirect. For testing only."},
}

type LogLine struct {
	N    int    `json:"n"`
	Time string `json:"t"`
	Text string `json:"text"`
}

type App struct {
	mu        sync.Mutex
	dir       string
	settings  Settings
	logs      []LogLine
	logN      int
	session   *Session
	starting  bool
	lastError string
	flash     FlashProgress
}

type Session struct {
	radio   RadioDevice
	ap      *SoftAP
	net     *Network
	stop    chan struct{}
	done    chan struct{}
	started time.Time
	status  *RadioStatus
	channel int
	dns     string
}

func NewApp() *App {
	// Portable mode: a settings.json next to the program is used in place.
	dir := ""
	if exe, err := os.Executable(); err == nil {
		if _, err := os.Stat(filepath.Join(filepath.Dir(exe), "settings.json")); err == nil {
			dir = filepath.Dir(exe)
		}
	}
	if dir == "" {
		base, err := os.UserConfigDir()
		if err != nil {
			base = "."
		}
		dir = filepath.Join(base, "WFC Connector")
	}
	a := &App{dir: dir}
	os.MkdirAll(a.dir, 0o755)
	a.settings = Settings{DNS: "178.62.43.212", Channel: 6}
	if b, err := os.ReadFile(filepath.Join(a.dir, "settings.json")); err == nil {
		json.Unmarshal(b, &a.settings)
	}
	if !validSSID([]byte(a.settings.SSID)) {
		a.settings.SSID = string(makeSSID())
		a.save()
	}
	return a
}

func (a *App) save() {
	b, _ := json.MarshalIndent(a.settings, "", "  ")
	tmp := filepath.Join(a.dir, "settings.json.tmp")
	if os.WriteFile(tmp, b, 0o600) == nil {
		os.Rename(tmp, filepath.Join(a.dir, "settings.json"))
	}
}

func (a *App) logf(format string, args ...any) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.logN++
	a.logs = append(a.logs, LogLine{a.logN, time.Now().Format("15:04:05"), fmt.Sprintf(format, args...)})
	if len(a.logs) > 1000 {
		a.logs = a.logs[len(a.logs)-1000:]
	}
}

func parseMAC(s string) ([6]byte, error) {
	hw, err := net.ParseMAC(strings.TrimSpace(s))
	if err != nil || len(hw) != 6 {
		return [6]byte{}, errors.New("invalid MAC address")
	}
	return mac6(hw), nil
}

func (a *App) allowedMACs() [][6]byte {
	var out [][6]byte
	for _, c := range a.settings.Allowed {
		if m, err := parseMAC(c.MAC); err == nil {
			out = append(out, m)
		}
	}
	return out
}

func (a *App) Start(channel int, dns, port, radio string) error {
	a.mu.Lock()
	if a.session != nil || a.starting {
		a.mu.Unlock()
		return errors.New("already running")
	}
	if a.flash.Active {
		a.mu.Unlock()
		return errors.New("firmware is being installed")
	}
	if channel < 1 || channel > 11 {
		channel = 6
	}
	if dns != "system" && net.ParseIP(dns).To4() == nil {
		a.mu.Unlock()
		return errors.New("enter an IPv4 DNS address, e.g. 178.62.43.212")
	}
	if radio != "original" {
		radio = "esp32"
	}
	a.settings.Channel, a.settings.DNS, a.settings.Port, a.settings.Radio = channel, dns, port, radio
	a.save()
	a.starting, a.lastError = true, ""
	ssid := []byte(a.settings.SSID)
	allowed := a.allowedMACs()
	a.mu.Unlock()

	fail := func(err error) error {
		a.mu.Lock()
		a.starting, a.lastError = false, err.Error()
		a.mu.Unlock()
		a.logf("Could not start: %v", err)
		return err
	}
	a.logf("Starting on channel %d, DNS %s", channel, dnsLabel(dns))
	var r RadioDevice
	if radio == "original" {
		rt, err := OpenRT2570(a.logf)
		if err != nil {
			return fail(err)
		}
		r = rt
	} else {
		// The ESP32 always uses an alias BSSID: with its own MAC it discards the DS's WEP frames.
		esp, err := OpenRadio(port, true, a.logf)
		if err != nil {
			return fail(err)
		}
		if !strings.HasPrefix(esp.firmware, bundledFirmware) {
			a.logf("Tip: newer radio firmware (%s) is available. Stop, then use Install firmware on the Hardware page.", bundledFirmware)
		}
		r = esp
	}
	a.logf("%s", r.Describe())
	if err := r.RadioOn(channel); err != nil {
		r.Close()
		return fail(err)
	}
	ap := NewSoftAP(r, channel, ssid, allowed, a.logf)
	ap.onPending = func(p PendingConsole) {}
	ap.onName = a.updateDSName
	netw, err := NewNetwork(dns, ap.Output, a.logf)
	if err != nil {
		r.Close()
		return fail(err)
	}
	ap.toNet = netw.Input
	s := &Session{radio: r, ap: ap, net: netw, stop: make(chan struct{}), done: make(chan struct{}), started: time.Now(), channel: channel, dns: dns}
	ap.Start()
	r.LED(true)
	a.mu.Lock()
	a.session, a.starting = s, false
	a.mu.Unlock()
	a.logf("Live. On your DS, choose Nintendo Wi-Fi USB Connector in Nintendo WFC Settings.")
	go a.run(s)
	return nil
}

func dnsLabel(d string) string {
	for _, p := range dnsPresets {
		if p.Address == d {
			return p.Name + " (" + d + ")"
		}
	}
	return d
}

func (a *App) run(s *Session) {
	defer close(s.done)
	tick := time.NewTicker(4 * time.Millisecond)
	stat := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	defer stat.Stop()
	statN, lastCounts := 0, ""
	for {
		select {
		case <-s.stop:
			return
		case f := <-s.radio.Rx():
			s.ap.Handle(f)
		case <-tick.C:
			s.ap.Tick()
		case <-stat.C:
			statN++
			if statN%5 == 0 {
				c, _, _ := s.ap.Snapshot()
				if fmt.Sprint(c) != lastCounts {
					lastCounts = fmt.Sprint(c)
					st := s.status
					if st != nil {
						a.logf("STATUS radio rx=%d dropped=%d bad=%d protected=%d tx=%d txErr=%d | %s", st.RxForwarded, st.RxDropped, st.RxBad, st.RxProtected, st.TxOK, st.TxErr, countsLine(c))
					} else {
						a.logf("STATUS %s", countsLine(c))
					}
				}
			}
			if s.radio.Dead() {
				a.logf("The radio was disconnected; connector stopped.")
				go a.Stop()
				return
			}
			st := s.radio.Status()
			a.mu.Lock()
			if st != nil {
				s.status = st
			}
			a.mu.Unlock()
		}
	}
}

func (a *App) Stop() {
	a.mu.Lock()
	s := a.session
	a.session = nil
	a.mu.Unlock()
	if s == nil {
		return
	}
	close(s.stop)
	<-s.done
	s.net.Close()
	s.radio.Close()
	a.logf("Connector stopped.")
}

func (a *App) Approve(mac, name string) error {
	m, err := parseMAC(mac)
	if err != nil {
		return err
	}
	a.mu.Lock()
	label := macStr(m[:])
	for _, c := range a.settings.Allowed {
		if strings.EqualFold(c.MAC, label) {
			a.mu.Unlock()
			return nil
		}
	}
	if len(a.settings.Allowed) >= 32 {
		a.mu.Unlock()
		return errors.New("at most 32 consoles can be allowed")
	}
	a.settings.Allowed = append(a.settings.Allowed, Console{MAC: label, Name: strings.TrimSpace(name), DSName: strings.TrimSpace(name), Added: time.Now().Unix()})
	a.save()
	s, list := a.session, a.allowedMACs()
	a.mu.Unlock()
	if s != nil {
		s.ap.SetAllowed(list)
	}
	a.logf("Allowed %s %s", name, label)
	return nil
}

func (a *App) Revoke(mac string) error {
	m, err := parseMAC(mac)
	if err != nil {
		return err
	}
	label := macStr(m[:])
	a.mu.Lock()
	var keep []Console
	for _, c := range a.settings.Allowed {
		if !strings.EqualFold(c.MAC, label) {
			keep = append(keep, c)
		}
	}
	a.settings.Allowed = keep
	a.save()
	s, list := a.session, a.allowedMACs()
	a.mu.Unlock()
	if s != nil {
		s.ap.SetAllowed(list)
	}
	a.logf("Removed %s", label)
	return nil
}

// updateDSName keeps the Consoles list in step with the username set on the DS.
// A name the user typed in the app is kept.
func (a *App) updateDSName(mac, name string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for i, c := range a.settings.Allowed {
		if !strings.EqualFold(c.MAC, mac) || c.DSName == name {
			continue
		}
		custom := c.DSName != "" && c.Name != c.DSName
		if c.DSName == "" && c.Name != name && c.Name != "" && c.Name != "Nintendo DS" {
			custom = false // older entries: the name came from the DS
		}
		old := c.Name
		a.settings.Allowed[i].DSName = name
		if !custom {
			a.settings.Allowed[i].Name = name
		}
		a.save()
		if !custom && old != name {
			go a.logf("%s changed its name on the DS: now %q", mac, name)
		}
	}
}

func (a *App) Rename(mac, name string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for i, c := range a.settings.Allowed {
		if strings.EqualFold(c.MAC, mac) {
			a.settings.Allowed[i].Name = strings.TrimSpace(name)
		}
	}
	a.save()
}

func (a *App) Flash(port string) error {
	a.mu.Lock()
	if a.session != nil || a.starting {
		a.mu.Unlock()
		return errors.New("stop the connector before installing firmware")
	}
	if a.flash.Active {
		a.mu.Unlock()
		return errors.New("already installing")
	}
	native := false
	if port == "" {
		ports := candidatePorts()
		if len(ports) == 0 {
			a.mu.Unlock()
			return errors.New("no ESP32 found: plug the board in")
		}
		port = ports[0]
	}
	for _, p := range listPorts() {
		if p.Name == port {
			native = p.Espressif
		}
	}
	a.flash = FlashProgress{Active: true, Message: "Starting"}
	a.mu.Unlock()
	a.logf("Installing radio firmware on %s", port)
	go func() {
		err := FlashFirmware(port, native, func(p FlashProgress) {
			a.mu.Lock()
			a.flash = p
			a.mu.Unlock()
		})
		a.mu.Lock()
		if err != nil {
			a.flash = FlashProgress{Error: err.Error(), Message: "Install failed"}
		}
		a.mu.Unlock()
		if err != nil {
			a.logf("Firmware install failed: %v", err)
		} else {
			a.logf("Firmware installed.")
		}
	}()
	return nil
}

// ---- state for the UI ----

type ConsoleView struct {
	MAC      string `json:"mac"`
	Name     string `json:"name"`
	State    string `json:"state"` // pending | allowed | connected
	LastSeen int64  `json:"lastSeen,omitempty"`
}

type StateView struct {
	Status     string         `json:"status"` // idle | starting | running | error
	Error      string         `json:"error,omitempty"`
	Radio      string         `json:"radio,omitempty"`
	Channel    int            `json:"channel"`
	DNS        string         `json:"dns"`
	Port       string         `json:"port"`
	RadioKind  string         `json:"radioKind"`
	Original   bool           `json:"originalPresent"`
	Uptime     int64          `json:"uptime"`
	Consoles   []ConsoleView  `json:"consoles"`
	Counters   map[string]int `json:"counters"`
	RadioStats *RadioStatus   `json:"radioStats,omitempty"`
	NetRx      uint64         `json:"netRx"`
	NetTx      uint64         `json:"netTx"`
	Presets    []DNSPreset    `json:"presets"`
	Ports      []portInfo     `json:"ports"`
	Flash      FlashProgress  `json:"flash"`
	Chips      []string       `json:"chips"`
	LogN       int            `json:"logN"`
	Version    string         `json:"version"`
}

func (a *App) State() StateView {
	a.mu.Lock()
	s := a.session
	kind := a.settings.Radio
	if kind != "original" {
		kind = "esp32"
	}
	v := StateView{Channel: a.settings.Channel, DNS: a.settings.DNS, Port: a.settings.Port, RadioKind: kind,
		Presets: dnsPresets, Flash: a.flash, LogN: a.logN, Version: appVersion, Error: a.lastError}
	allowed := append([]Console{}, a.settings.Allowed...)
	switch {
	case a.starting:
		v.Status = "starting"
	case s != nil:
		v.Status = "running"
		v.Radio = s.radio.Describe()
		v.Uptime = int64(time.Since(s.started).Seconds())
		v.RadioStats = s.status
		v.Channel, v.DNS = s.channel, s.dns
	case a.lastError != "":
		v.Status = "error"
	default:
		v.Status = "idle"
	}
	a.mu.Unlock()

	connected := map[string]StationInfo{}
	var pending []PendingConsole
	if s != nil {
		var sts []StationInfo
		v.Counters, sts, pending = s.ap.Snapshot()
		for _, st := range sts {
			connected[st.MAC] = st
		}
		v.NetRx, v.NetTx = s.net.RxFrames, s.net.TxFrames
	}
	for _, c := range allowed {
		cv := ConsoleView{MAC: c.MAC, Name: c.Name, State: "allowed"}
		if st, ok := connected[c.MAC]; ok {
			cv.LastSeen = st.LastSeen
			if st.Associated {
				cv.State = "connected"
			}
		}
		v.Consoles = append(v.Consoles, cv)
	}
	isAllowed := map[string]bool{}
	for _, c := range allowed {
		isAllowed[c.MAC] = true
	}
	sort.Slice(pending, func(i, j int) bool { return pending[i].Seen > pending[j].Seen })
	for _, p := range pending {
		if !isAllowed[p.MAC] {
			v.Consoles = append([]ConsoleView{{MAC: p.MAC, Name: p.Name, State: "pending", LastSeen: p.Seen}}, v.Consoles...)
		}
	}
	v.Ports = listPorts()
	v.Original = usbPresent(rtVID, rtPID)
	v.Chips = supportedChips()
	return v
}

func (a *App) Logs(since int) []LogLine {
	a.mu.Lock()
	defer a.mu.Unlock()
	var out []LogLine
	for _, l := range a.logs {
		if l.N > since {
			out = append(out, l)
		}
	}
	return out
}

func countsLine(c map[string]int) string {
	keys := make([]string, 0, len(c))
	for k := range c {
		if k != "beacon_tx" {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	out := fmt.Sprintf("beacons=%d", c["beacon_tx"])
	for _, k := range keys {
		out += fmt.Sprintf(" %s=%d", k, c[k])
	}
	return out
}
