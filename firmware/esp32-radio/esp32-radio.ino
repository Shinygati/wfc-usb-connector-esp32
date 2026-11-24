// esp32-radio — ESP32 (any Wi-Fi model) replacement for the RT2570 in wfc-usb-connector.
// Builds for ESP32, ESP32-S2, ESP32-S3, ESP32-C3, ESP32-C5 and ESP32-C6.
//
// The ESP32 is only a raw 802.11 radio. All of the access-point logic (hidden
// NWCUSBAP SSID, registration, MAC permissions, WEP-104, DHCP/DNS/NAT) stays in
// the project's Python (proto/ap.py + proto/network.py). proto/esp32radio.py
// talks to this firmware over USB serial.
//
//  - Station interface is enabled but never connects; its MAC is the BSSID, so
//    the radio hardware ACKs the console's unicast frames.
//  - Promiscuous RX forwards only frames the AP needs (addressed to the BSSID,
//    PS-Poll, and NWCUSBAP probe requests).
//  - Raw TX at 1 Mbps 802.11b, like the original connector.
//
// hostLink: native USB (CDC) or the UART port at 921600 baud — whichever the host
// talks to first.
//
// Build (arduino-cli, ESP32 core 3.x/4.x). The -zmuldefs flag is REQUIRED: it
// lets the override below replace the Wi-Fi library's raw-frame filter, which
// otherwise refuses authentication/association responses.
//   arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc \
//     --build-property "compiler.c.elf.extra_flags=-Wl,-zmuldefs" .

#include <WiFi.h>
#include <esp_wifi.h>
#include <freertos/ringbuf.h>

#define FW_VERSION   "esp32-radio 1.3 (" CONFIG_IDF_TARGET ")"
#define PROTO_VER    1
#define UART_BAUD    921600
#define MAX_PAYLOAD  1600
#define SYNC         0xC5

// host -> radio
enum { CMD_HELLO = 0x01, CMD_RADIO_ON = 0x02, CMD_TX = 0x03, CMD_RADIO_OFF = 0x04,
       CMD_LED = 0x05, CMD_STATUS = 0x06, CMD_SET_BSSID = 0x07 };
// radio -> host
enum { EV_INFO = 0x81, EV_RADIO = 0x82, EV_RX = 0x83, EV_LOG = 0x84, EV_STATUS = 0x86, EV_BSSID = 0x87 };

// Allow every raw frame type through esp_wifi_80211_tx (needs -Wl,-zmuldefs).
extern "C" int ieee80211_raw_frame_sanity_check(int32_t arg, int32_t arg2, int32_t arg3) {
  return 0;
}

static uint8_t bssid[6];        // the AP's BSSID (normally this radio's own MAC)
static uint8_t ownMac[6];
static volatile bool radioOn = false;
static RingbufHandle_t rxRing;
static Stream *hostLink = nullptr;      // chosen when the host first talks to us
static uint8_t channel = 6;

static volatile uint32_t rxForwarded = 0, rxDropped = 0, rxBad = 0, rxBadProtected = 0;
static uint32_t txOk = 0, txErr = 0;
static int32_t lastTxErr = 0;

static const uint8_t MAGIC[8] = {'N', 'W', 'C', 'U', 'S', 'B', 'A', 'P'};

// ---------- framing ----------

static void sendPacket(uint8_t type, const uint8_t *a, size_t alen, const uint8_t *b = nullptr, size_t blen = 0) {
  if (!hostLink) return;
  size_t len = alen + blen;
  uint8_t hdr[4] = {SYNC, type, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8)};
  uint8_t sum = type + hdr[2] + hdr[3];
  for (size_t i = 0; i < alen; i++) sum += a[i];
  for (size_t i = 0; i < blen; i++) sum += b[i];
  hostLink->write(hdr, 4);
  if (alen) hostLink->write(a, alen);
  if (blen) hostLink->write(b, blen);
  hostLink->write(&sum, 1);
}

static void logMsg(const char *fmt, ...) {
  char buf[160];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n > 0) sendPacket(EV_LOG, (const uint8_t *)buf, min(n, (int)sizeof(buf) - 1));
}

// ---------- RX (Wi-Fi task context: keep it short) ----------

static bool wanted(const uint8_t *f, int len) {
  if (len < 10) return false;
  uint8_t fc0 = f[0];
  uint8_t type = (fc0 >> 2) & 3, subtype = fc0 >> 4;
  if (fc0 & 3) return false;                              // protocol version must be 0
  if (type == 1) {                                        // control: PS-Poll to us
    return subtype == 10 && len >= 16 && memcmp(f + 4, bssid, 6) == 0;
  }
  if (len < 24) return false;
  if (memcmp(f + 4, bssid, 6) == 0) return true;          // addressed to the BSSID
  if (type == 0 && subtype == 4 && len >= 24 + 2 + 8) {   // probe request for NWCUSBAP
    const uint8_t *ie = f + 24;
    return ie[0] == 0 && ie[1] >= 8 && memcmp(ie + 2, MAGIC, 8) == 0;
  }
  return false;
}

static void promiscCb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (!radioOn) return;
  const wifi_promiscuous_pkt_t *p = (const wifi_promiscuous_pkt_t *)buf;
  int len = p->rx_ctrl.sig_len;                           // includes the 4-byte FCS
  if (type != WIFI_PKT_CTRL || len >= 20) len -= 4;       // (PS-Poll: 16 bytes + FCS)
  if (len < 10 || len > MAX_PAYLOAD - 2) return;
  if (!wanted(p->payload, len)) return;
  if (p->rx_ctrl.rx_state != 0) {
    // The radio tries to decrypt protected frames sent to its own MAC and, having
    // no key, flags them as errors. WEP is done on the PC, which verifies the WEP
    // ICV itself, so pass protected frames through; drop other errored frames.
    if (!(p->payload[1] & 0x40)) { rxBad++; return; }
    rxBadProtected++;
  }

  uint8_t *item = nullptr;                                 // no big copy on the Wi-Fi task stack
  if (xRingbufferSendAcquire(rxRing, (void **)&item, len + 2, 0) != pdTRUE) {
    rxDropped++;
    return;
  }
  item[0] = (uint8_t)(int8_t)p->rx_ctrl.rssi;
  item[1] = p->rx_ctrl.channel;
  memcpy(item + 2, p->payload, len);
  xRingbufferSendComplete(rxRing, item);
  rxForwarded++;
}

// ---------- radio control ----------

static bool startRadio(uint8_t ch) {
  esp_wifi_set_promiscuous(false);
  esp_wifi_set_ps(WIFI_PS_NONE);
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_11B);
  esp_wifi_config_80211_tx_rate(WIFI_IF_STA, WIFI_PHY_RATE_1M_L);

  wifi_promiscuous_filter_t filt = {};
  // FCSFAIL: also deliver frames the radio flagged (e.g. protected frames it could not decrypt).
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA | WIFI_PROMIS_FILTER_MASK_CTRL |
                     WIFI_PROMIS_FILTER_MASK_FCSFAIL;
  esp_wifi_set_promiscuous_filter(&filt);
  wifi_promiscuous_filter_t ctrl = {};
  ctrl.filter_mask = WIFI_PROMIS_CTRL_FILTER_MASK_PSPOLL;
  esp_wifi_set_promiscuous_ctrl_filter(&ctrl);
  esp_wifi_set_promiscuous_rx_cb(promiscCb);
  if (esp_wifi_set_promiscuous(true) != ESP_OK) return false;
  esp_err_t e = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  if (e != ESP_OK) {
    logMsg("set_channel(%u) failed: 0x%x", ch, e);
    return false;
  }
  channel = ch;
  radioOn = true;
  return true;
}

static void stopRadio() {
  radioOn = false;
  esp_wifi_set_promiscuous(false);
}

static void setLed(bool on) {
#ifdef RGB_BUILTIN
  rgbLedWrite(RGB_BUILTIN, 0, on ? 24 : 0, 0);
#endif
}

// ---------- command handling ----------

static void handle(uint8_t type, const uint8_t *p, uint16_t len) {
  switch (type) {
    case CMD_HELLO: {
      uint8_t info[8] = {PROTO_VER, 0};
      memcpy(info + 2, bssid, 6);
      sendPacket(EV_INFO, info, sizeof(info), (const uint8_t *)FW_VERSION, strlen(FW_VERSION));
      break;
    }
    case CMD_RADIO_ON: {
      uint8_t ch = len ? p[0] : 6;
      uint8_t ok = (ch >= 1 && ch <= 13) && startRadio(ch);
      uint8_t r[2] = {ok, channel};
      sendPacket(EV_RADIO, r, 2);
      break;
    }
    case CMD_RADIO_OFF:
      stopRadio();
      setLed(false);
      break;
    case CMD_TX: {
      if (!radioOn || len < 24) { txErr++; break; }
      esp_err_t e = ESP_FAIL;
      for (int i = 0; i < 4; i++) {                         // brief retry if TX buffers are full
        e = esp_wifi_80211_tx(WIFI_IF_STA, p, len, true);
        if (e != ESP_ERR_NO_MEM) break;
        delay(1);
      }
      if (e == ESP_OK) txOk++;
      else {
        if (txErr < 5) logMsg("tx error 0x%x (fc=%02x%02x len=%u)", e, p[0], p[1], len);
        txErr++;
        lastTxErr = e;
      }
      break;
    }
    case CMD_LED:
      setLed(len && p[0]);
      break;
    case CMD_SET_BSSID:
      // Use a BSSID other than our own MAC: the radio then treats the console's
      // frames as other stations' traffic and passes protected ones through
      // (at the cost of no hardware ACKs). An all-zero MAC restores our own.
      if (len == 6) {
        static const uint8_t zero[6] = {0};
        memcpy(bssid, memcmp(p, zero, 6) ? p : ownMac, 6);
      }
      sendPacket(EV_BSSID, bssid, 6);
      break;
    case CMD_STATUS: {
      uint32_t s[8] = {rxForwarded, rxDropped, txOk, txErr, (uint32_t)lastTxErr, channel, rxBad, rxBadProtected};
      sendPacket(EV_STATUS, (const uint8_t *)s, sizeof(s));
      break;
    }
  }
}

// One parser per physical hostLink, so either port can be used.
struct Parser {
  Stream *port;
  uint8_t buf[MAX_PAYLOAD + 8];
  uint16_t pos = 0, need = 0;

  void poll() {
    while (port->available()) {
      uint8_t c = port->read();
      if (pos == 0 && c != SYNC) continue;
      buf[pos++] = c;
      if (pos == 4) {
        need = buf[2] | (buf[3] << 8);
        if (need > MAX_PAYLOAD) { pos = 0; continue; }
      }
      if (pos >= 4 && pos == 4 + need + 1) {
        uint8_t sum = 0;
        for (uint16_t i = 1; i < 4 + need; i++) sum += buf[i];
        if (sum == buf[4 + need]) {
          hostLink = port;                                      // reply on the port the host uses
          handle(buf[1], buf + 4, need);
        }
        pos = 0;
      }
    }
  }
};

#if ARDUINO_USB_CDC_ON_BOOT
// Native USB (Serial) and the UART/COM port (Serial0): answer on whichever the host uses.
static Parser usbParser, uartParser;
#else
static Parser uartParser;           // chips/builds without native USB CDC: UART only
#endif

void setup() {
#if ARDUINO_USB_CDC_ON_BOOT
  Serial.begin(UART_BAUD);          // native USB CDC (baud ignored)
  Serial0.begin(UART_BAUD);         // UART / COM port
  usbParser.port = &Serial;
  uartParser.port = &Serial0;
#else
  Serial.begin(UART_BAUD);
  uartParser.port = &Serial;
#endif

  rxRing = xRingbufferCreate(48 * 1024, RINGBUF_TYPE_NOSPLIT);

  WiFi.persistent(false);
  WiFi.setAutoReconnect(false);
  WiFi.mode(WIFI_STA);
  esp_wifi_disconnect();
  esp_wifi_get_mac(WIFI_IF_STA, ownMac);
  memcpy(bssid, ownMac, 6);
  setLed(false);
}

void loop() {
#if ARDUINO_USB_CDC_ON_BOOT
  usbParser.poll();
#endif
  uartParser.poll();

  // forward received frames
  for (int n = 0; n < 16; n++) {
    size_t sz;
    uint8_t *item = (uint8_t *)xRingbufferReceive(rxRing, &sz, 0);
    if (!item) break;
    sendPacket(EV_RX, item, sz);
    vRingbufferReturnItem(rxRing, item);
  }
  delay(1);
}
