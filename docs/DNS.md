# Community DNS services

Research and local checks: **2026-09-25**. DNS availability can change. A DNS
answer does not prove that authentication, a gift download or multiplayer works.

## Select a service

In the desktop app, stop the current session, choose **DNS service**, then start
again. The choice is saved in the ignored state directory. Existing settings
default to Windows DNS. The standalone CLI remains available:

```powershell
python proto/ap.py run --channel 6 --seconds 14400 --dns 178.62.43.212
```

The console uses the connector's DHCP-provided DNS address, `192.168.77.2`.
The application forwards queries to the chosen upstream. This changes neither
Windows DNS settings nor the console's saved registration. CLI arguments do not
inherit desktop settings. Use `--dns system` to return to the Windows resolver.

| Desktop choice | Upstream | Purpose and official source |
|---|---|---|
| Kaeru WFC / Wiimmfi | `178.62.43.212` | [Kaeru](https://kaeru.world/projects/wfc) provides a DS gateway to Wiimmfi without ROM patches. [Poké Classic Network](https://pkmnclassic.net/) publishes the same address for its Generation IV/V services. Recommended starting point for this project's Pokémon Pearl test. |
| WiiLink WFC | `5.161.56.11` | Separate WFC network. The [official WFC site](https://wfc.wiilink24.com/online/mariokartds) publishes this DNS and says DS games need only a DNS change. Game compatibility must be checked individually. |
| AltWFC | `172.104.88.237` | Separate network published in the [AltWFC project wiki](https://github.com/barronwaffles/dwc_network_server_emulator/wiki). Pokémon service compatibility is not interchangeable with Kaeru/Wiimmfi. |
| WiiLink DNS | `167.235.229.36` | The [WiiLink DNS guide](https://wiilink.ca/en-GB/guide/dns/) targets Wii/Wii U services. This is different from WiiLink WFC. Its [published mappings](https://github.com/WiiLink24/DNS-Server/blob/master/dns_zones-hosts.txt) include Kaeru/Wiimmfi destinations. The DS download-domain check below returned NXDOMAIN. |
| Custom DNS | User supplied IPv4 | Use another known resolver or a local service. |

Kaeru and Wiimmfi are parts of the same connection path in the Kaeru preset.
Selecting another network can affect game accounts and friend-code compatibility;
the application does not erase or migrate console/game profiles.

## Local DNS results

Direct UDP and TCP queries were made to the published servers on port 53.
Two runs agreed on the results below, using three-second and two-second timeouts.
Each cell lists the A-record answer for the indicated hostname. These are
observations from this PC, not global uptime claims.

| Upstream | `conntest.nintendowifi.net` | `nas.nintendowifi.net` | `dls1.nintendowifi.net` | `gamestats2.gs.nintendowifi.net` |
|---|---|---|---|---|
| Kaeru `178.62.43.212` | `198.62.122.140` | `178.62.43.212` | `178.62.43.212` | `195.201.236.139` |
| WiiLink WFC `5.161.56.11` | `5.161.56.11` | `5.161.56.11` | `5.161.56.11` | `5.161.56.11` |
| AltWFC `172.104.88.237` | `172.104.88.237` | `172.104.88.237` | `172.105.219.148` | `172.104.88.237` |
| WiiLink DNS `167.235.229.36` | `167.235.229.36` | `178.62.43.212` | NXDOMAIN | `195.201.236.139` |
| Wiimmfi Wii DNS `95.217.77.151` | `78.46.231.155`, `188.34.177.217` | `78.46.231.155` | NXDOMAIN | `195.201.236.139` |

- Kaeru, WiiLink WFC, WiiLink DNS and Wiimmfi answered over both transports.
- AltWFC answered all four UDP queries; all four TCP queries timed out locally.
- The historical AltWFC alternate `104.131.93.87`, still in the project's
  [old server list](https://github.com/barronwaffles/dwc_network_server_emulator/wiki/List-of-Servers),
  timed out over both transports. It is not a preset.
- `95.217.77.151` is a Wii-oriented Wiimmfi DNS, not the recommended unpatched DS
  gateway. See the [Wiimmfi team clarification](https://forum.wii-homebrew.com/index.php/Thread/60792-What-is-the-newest-and-most-current-Wiimmfi-DNS-for-use-with-the-Nintendo-DS-for/).
  It is surveyed for comparison, not offered as a DS preset.
- Control queries for `example.com` to Cloudflare `1.1.1.1` answered over both
  transports. Public general-purpose DNS is not a community WFC replacement.

## Backend checks and limitations

The earlier custom-DNS path, which rewrote addresses before passing packets to
libslirp, timed out while direct queries succeeded. Custom DNS now uses bounded
Python UDP workers, alongside the existing Windows resolver mode. Other NAT
traffic and DHCP remain in libslirp. No Windows network or firewall settings
were changed. The underlying cause of the old path's timeout is not established.

After this change, synthetic guest queries passed through the backend for Kaeru
(`conntest`, `nas` and `dls1`) and for WiiLink WFC and AltWFC (`nas`). This verifies the guest
request/reply path, without requiring the USB adapter. Automated tests also cover
reply identity, guest addressing, upstream timeouts and preservation of NXDOMAIN.

The proxy accepts standard single-question UDP queries, checks the upstream
reply's transaction ID and question, and never falls back to a different DNS
service. There are two worker threads, at most 32 queued/running queries and a
two-second upstream timeout. Failures return SERVFAIL for valid queries.
Oversized replies are marked truncated to fit the guest MTU; TCP DNS and IP
fragmentation are not implemented.

After the DNS changes, the operator launched `python gui.py` and **confirmed that
Pokémon Pearl Mystery Gift worked on 2026-09-25**. The saved desktop settings
selected Kaeru WFC / Wiimmfi (`178.62.43.212`) on channel 6. This is a real-game
result beyond the DNS survey; see the [hardware test record](HARDWARE.md#successful-mystery-gift-test-2026-09-25).
The specific gift was not recorded. Other games, multiplayer and the alternative
services have not been validated by this test.

## Repeat the checks

```powershell
python tools/probe_dns.py
python tools/check_network.py --dns 178.62.43.212 --dns-only --host nas.nintendowifi.net
python tools/check_network.py --dns 178.62.43.212 --dns-only --host dls1.nintendowifi.net
```

The survey writes the full result set and UTC timestamp to the ignored file
`runtime/dns-probes.json`. It prints negative answers and timeouts as results;
an exit code of zero means the survey completed, not that every server passed.
`check_network.py` exits unsuccessfully on a negative answer or timeout.
