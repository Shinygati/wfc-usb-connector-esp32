package main

import (
	"crypto/rand"
	"embed"
	"encoding/hex"
	"encoding/json"
	"io/fs"
	"net"
	"net/http"
	"strconv"
	"sync/atomic"
	"time"
)

//go:embed web
var webFS embed.FS

const appVersion = "1.2.0"

// lastPoll is when the interface last asked for state (used to quit on Linux when its window closes).
var lastPoll atomic.Int64

// StartServer serves the UI on 127.0.0.1 and returns its URL.
func StartServer(a *App) (string, error) {
	tok := make([]byte, 16)
	rand.Read(tok)
	token := hex.EncodeToString(tok)
	mux := http.NewServeMux()
	sub, _ := fs.Sub(webFS, "web")
	mux.Handle("/", http.FileServer(http.FS(sub)))

	api := func(path string, h func(r *http.Request) (any, error)) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("X-Token") != token {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Cache-Control", "no-store")
			v, err := h(r)
			if err != nil {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
				return
			}
			json.NewEncoder(w).Encode(v)
		})
	}
	type body struct {
		Channel int    `json:"channel"`
		DNS     string `json:"dns"`
		Port    string `json:"port"`
		MAC     string `json:"mac"`
		Name    string `json:"name"`
		Radio   string `json:"radio"`
	}
	decode := func(r *http.Request) body {
		var b body
		json.NewDecoder(r.Body).Decode(&b)
		return b
	}
	ok := map[string]bool{"ok": true}
	api("/api/state", func(r *http.Request) (any, error) { lastPoll.Store(time.Now().UnixNano()); return a.State(), nil })
	api("/api/logs", func(r *http.Request) (any, error) {
		since, _ := strconv.Atoi(r.URL.Query().Get("since"))
		return a.Logs(since), nil
	})
	api("/api/start", func(r *http.Request) (any, error) {
		b := decode(r)
		go a.Start(b.Channel, b.DNS, b.Port, b.Radio)
		return ok, nil
	})
	api("/api/stop", func(r *http.Request) (any, error) { go a.Stop(); return ok, nil })
	api("/api/approve", func(r *http.Request) (any, error) { b := decode(r); return ok, a.Approve(b.MAC, b.Name) })
	api("/api/revoke", func(r *http.Request) (any, error) { b := decode(r); return ok, a.Revoke(b.MAC) })
	api("/api/rename", func(r *http.Request) (any, error) { b := decode(r); a.Rename(b.MAC, b.Name); return ok, nil })
	api("/api/flash", func(r *http.Request) (any, error) { b := decode(r); return ok, a.Flash(b.Port) })

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	go http.Serve(ln, mux)
	return "http://" + ln.Addr().String() + "/#" + token, nil
}
