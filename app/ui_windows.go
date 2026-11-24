//go:build windows

package main

import (
	"os/exec"
	"syscall"
	"unsafe"

	webview2 "github.com/jchv/go-webview2"
)

var (
	user32 = syscall.NewLazyDLL("user32.dll")
)

func init() {
	// Crisp text on high-DPI screens (per-monitor v2).
	if p := user32.NewProc("SetProcessDpiAwarenessContext"); p.Find() == nil {
		p.Call(^uintptr(3)) // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
	}
}

func runUI(url string) {
	w := webview2.NewWithOptions(webview2.WebViewOptions{
		AutoFocus: true,
		WindowOptions: webview2.WindowOptions{
			Title: "WFC Connector", Width: 1180, Height: 780, Center: true, IconId: 1,
		},
	})
	if w == nil {
		// No WebView2 runtime: use the default browser and keep running until closed.
		exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
		showError("Microsoft Edge WebView2 is not installed, so WFC Connector opened in your browser.\n\nKeep this message open while you play; press OK to quit.")
		return
	}
	defer w.Destroy()
	hwnd := uintptr(w.Window())
	darkTitleBar(hwnd)
	w.SetSize(960, 660, webview2.HintMin)
	w.Navigate(url)
	w.Run()
}

// darkTitleBar matches the window frame to the dark interface (Windows 10 20H1+ / 11).
func darkTitleBar(hwnd uintptr) {
	dwm := syscall.NewLazyDLL("dwmapi.dll")
	set := dwm.NewProc("DwmSetWindowAttribute")
	if set.Find() != nil {
		return
	}
	on := int32(1)
	set.Call(hwnd, 20, uintptr(unsafe.Pointer(&on)), 4)      // DWMWA_USE_IMMERSIVE_DARK_MODE
	caption := uint32(0x001c1710)                            // COLORREF 0x00BBGGRR = #10171c
	set.Call(hwnd, 35, uintptr(unsafe.Pointer(&caption)), 4) // DWMWA_CAPTION_COLOR (Windows 11)
}

func showError(msg string) {
	box := user32.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString("WFC Connector")
	m, _ := syscall.UTF16PtrFromString(msg)
	box.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), 0x40)
}
