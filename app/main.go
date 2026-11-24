package main

import (
	"fmt"
	"os"
)

func main() {
	app := NewApp()
	app.logf("WFC Connector %s", appVersion)
	url, err := StartServer(app)
	if err != nil {
		fatal("Could not start the interface: " + err.Error())
	}
	runUI(url) // blocks until the window is closed
	app.Stop()
}

func fatal(msg string) {
	fmt.Fprintln(os.Stderr, msg)
	showError(msg)
	os.Exit(1)
}
