package main

import (
	"encoding/hex"
	"testing"
)

func TestDeriveKeyMatchesPython(t *testing.T) {
	// expected values produced by proto/wfckey.py
	cases := map[string]string{
		"00000111112222233333": "505bc46ad1375cbb9d30b2236f",
		"12345678901234567890": "5f5cc066d03557b09235bf2f6e",
	}
	for d, want := range cases {
		got := hex.EncodeToString(deriveKey([]byte(d)))
		if got != want {
			t.Errorf("%s: got %s want %s", d, got, want)
		}
	}
}
