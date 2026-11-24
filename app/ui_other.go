//go:build !windows

package main

import (
	_ "embed"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
)

//go:embed web/icon.png
var iconPNG []byte

// installDesktopEntry adds WFC Connector (with its icon) to the Linux app menu.
// ELF programs can't carry an icon themselves; desktops take it from here.
func installDesktopEntry() {
	if runtime.GOOS != "linux" {
		return
	}
	exe, err := os.Executable()
	if err != nil {
		return
	}
	exe, _ = filepath.Abs(exe)
	data := os.Getenv("XDG_DATA_HOME")
	if data == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return
		}
		data = filepath.Join(home, ".local", "share")
	}
	iconDir := filepath.Join(data, "icons", "hicolor", "256x256", "apps")
	appDir := filepath.Join(data, "applications")
	os.MkdirAll(iconDir, 0o755)
	os.MkdirAll(appDir, 0o755)
	os.WriteFile(filepath.Join(iconDir, "wfc-connector.png"), iconPNG, 0o644)
	entry := fmt.Sprintf("[Desktop Entry]\nType=Application\nName=WFC Connector\nComment=Get your Nintendo DS online with an ESP32\nExec=\"%s\"\nIcon=wfc-connector\nTerminal=false\nCategories=Game;Network;\nStartupWMClass=wfc-connector\n", exe)
	path := filepath.Join(appDir, "wfc-connector.desktop")
	if old, err := os.ReadFile(path); err != nil || string(old) != entry {
		os.WriteFile(path, []byte(entry), 0o644)
	}
}

func runUI(url string) {
	installDesktopEntry()
	fmt.Println("WFC Connector is running.")
	fmt.Println("Open this address if your browser did not open:\n  " + url)
	fmt.Println("Press Ctrl+C to stop.")
	opener := "xdg-open"
	if runtime.GOOS == "darwin" {
		opener = "open"
	}
	// Prefer a chromeless app window when Chrome/Chromium/Edge is installed.
	for _, b := range []string{"google-chrome", "chromium", "chromium-browser", "microsoft-edge"} {
		if p, err := exec.LookPath(b); err == nil {
			exec.Command(p, "--app="+url, "--window-size=1180,780", "--class=wfc-connector").Start()
			goto wait
		}
	}
	exec.Command(opener, url).Start()
wait:
	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt, syscall.SIGTERM)
	// Quit when the app window has been closed (it stops polling).
	t := time.NewTicker(2 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-c:
			fmt.Println("\nStopping…")
			return
		case <-t.C:
			if last := lastPoll.Load(); last != 0 && time.Since(time.Unix(0, last)) > 20*time.Second {
				fmt.Println("Window closed; stopping.")
				return
			}
		}
	}
}

func showError(msg string) {}
