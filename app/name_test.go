package main

import "testing"

func TestDSNameSync(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("APPDATA", t.TempDir())
	a := NewApp()
	a.settings.Allowed = []Console{{MAC: "00:09:bf:12:34:56", Name: "Player One"}} // entry from before DSName existed
	a.updateDSName("00:09:bf:12:34:56", "Tony")
	if c := a.settings.Allowed[0]; c.Name != "Tony" || c.DSName != "Tony" {
		t.Fatalf("not renamed from DS: %+v", c)
	}
	a.Rename("00:09:bf:12:34:56", "My DS Lite")
	a.updateDSName("00:09:bf:12:34:56", "Bob")
	if c := a.settings.Allowed[0]; c.Name != "My DS Lite" || c.DSName != "Bob" {
		t.Fatalf("custom name overwritten: %+v", c)
	}
	a.Rename("00:09:bf:12:34:56", "Bob") // back to the DS name: follows the DS again
	a.updateDSName("00:09:bf:12:34:56", "Alice")
	if c := a.settings.Allowed[0]; c.Name != "Alice" {
		t.Fatalf("did not follow DS again: %+v", c)
	}
}
