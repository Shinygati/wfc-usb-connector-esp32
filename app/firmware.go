package main

import (
	"bytes"
	"compress/gzip"
	"embed"
	"io"
)

const bundledFirmware = "esp32-radio 1.3"

//go:embed fw
var fwFS embed.FS

// firmwareImage returns the merged radio firmware for a chip, trimmed of the
// trailing erased (0xFF) padding.
func firmwareImage(chip string) ([]byte, bool) {
	z, err := fwFS.Open("fw/" + chip + ".bin.gz")
	if err != nil {
		return nil, false
	}
	defer z.Close()
	gz, err := gzip.NewReader(z)
	if err != nil {
		return nil, false
	}
	b, err := io.ReadAll(gz)
	if err != nil || len(b) == 0 {
		return nil, false
	}
	end := len(bytes.TrimRight(b, "\xff"))
	end = (end + 0xfff) &^ 0xfff
	if end > len(b) {
		end = len(b)
	}
	return b[:end], true
}

func supportedChips() []string {
	var out []string
	for _, c := range []string{"esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c5", "esp32c6"} {
		if _, ok := firmwareImage(c); ok {
			out = append(out, chipNames[c])
		}
	}
	return out
}
