# Add-ons: design, licences, roadmap

What exists today is described in `AGENTS.md` (gotcha 58). This file is the longer design behind it.

## Two kinds of add-on

| | Built in (ACARS, APRS) | External program (rtl_433, VDL2) |
|---|---|---|
| Code | our own, `falconsdr/addons/` | the author's program, installed by the user |
| Signal | the audio of a scanner slot | the receiver itself (the scanner is paused) |
| Cost | a few ms per 30 ms block, while a slot is open | the program's own |
| Switching | none: it works while scanning | one receiver, one owner: Start/Stop on the Add-ons page |
| Windows | yes (numpy/scipy only) | only if the program has a build |

**Built-in decoders are a bonus, not full reception.** A scanner visits a window for a fraction of a
second per cycle (about 2 s), and ACARS/APRS bursts last 0.2-0.5 s. A burst that is caught keeps the
slot open for the linger time, so the replies that follow are caught too, but the first one is
usually cut. For complete reception use a dedicated receiver (the multi-dongle support parks one on
one window) or an external program.

A claimed channel becomes a *data slot*: the engine forces the demodulation the decoder needs (AM for
ACARS, NFM for APRS), feeds the decoder the demodulated audio, and neither plays nor records it
(unless "Hear and record data channels too" is on). A data slot never takes the speaker from a voice
channel (weight -1 in the primary choice).

## Parts

* `addons/tone.py` - `ToneDiscriminator` (audio -> frequency, streamed) and `BitClock` (a digital PLL).
* `addons/aprs.py` - AFSK -> NRZI -> HDLC (flags, un-stuffing, CRC-16/X.25) -> AX.25 -> APRS (plain and
  compressed positions, Mic-E, objects, messages, status, weather).
* `addons/acars.py` - MSK tones -> four tone-to-bit mappings tried in parallel (direct, inverted, and both
  after differential decoding) -> preamble match -> 7-bit characters with odd parity -> CRC-16 -> fields.
* `addons/events.py` - `EventHub`: a ring in memory plus one JSON-lines file per day (`CONFIG_DIR/addons`).
* `addons/external.py` - `ExternalRunner` (process, stdout lines, stderr tail) and the parsers of what
  rtl_433 and dumpvdl2 print.
* `addons/manager.py` - the catalog, which channels are claimed, one external program at a time.
* `Controller.run_addon/stop_addon` pause and resume the scanner around an external program.
* UI: `views/addons.js` (the card), `views/messages.js`, and `settings.js` with `scope='addons'` for
  transcription and digital voice, which moved to the Add-ons page.

## Licences

FalconSDR is GPL-3.0-or-later. The rule is **separate programs, fetched by the user**:

* A program FalconSDR only starts and reads (command line, stdin/stdout, JSON) is a separate work. Its
  licence does not reach FalconSDR, and GPL-2.0-only programs are not a problem (the GPL FAQ treats pipes
  and command lines as communication between separate programs; read it again if the exchange ever
  becomes more intimate than JSON lines).
* Obligations (licence text, source offer) start only when *we distribute* a program. Nothing is bundled.
  If one ever is: keep its licence, say where its source is, ship it as a separate file.
* Checked on GitHub (2026-11-16): dumpvdl2 GPL-3.0, libacars MIT, rtl_433 GPL-2.0, readsb GPL-3.0,
  dump1090-fa GPL-2.0, tar1090 GPL-2.0-or-later, acarsdec LGPL-2 (upstream README) / GPL-2.0 (fork),
  ACARS Hub GPL-3.0, acars_router MIT, Leaflet BSD-2-Clause. Not bundling anything also means none of
  these needs a notice in `THIRD-PARTY-LICENSES`.
* **No code is copied from another decoder.** ACARS and APRS are written from the public format
  descriptions; `aprslib` was used once, in a throwaway environment, only to cross-check positions.
  rtl_433's device decoders are its value and GPL-2.0: do not port them.
* **Data.** A map needs Leaflet (BSD-2, may be bundled) and tiles. `tile.openstreetmap.org` allows
  interactive viewing but needs a visible "(c) OpenStreetMap contributors", a valid Referer, a
  configurable tile URL, and forbids bulk or offline use. Waypoint databases are the hard part: the
  popular worldwide CSV is MIT for its *script* but a scrape of OpenNav, which states no terms. Do not
  ship or download it. OurAirports (airports, VOR/NDB) is public domain; RNAV fixes (GERVI...) come from
  the user's own CSV or are learned from the user's own reception (an aircraft reporting "over GERVI" with
  a known position).

## Roadmap

Done: shell (Add-ons page, Messages page), APRS, ACARS (experimental: synthetic tests only; real
bursts from the receiver's recordings could not be validated because they went through noise
reduction and MP3), rtl_433 and VDL2 as supervised external programs, presets.

Next, in order of value: a shared map (Leaflet, APRS positions first); VDL2/ACARS enrichment (airports
from OurAirports, learned fixes, reception range); installing external programs from their authors
(pinned SHA-256 on Windows, a root command hint on Linux); ADS-B (own dongle, readsb or an own
numpy demodulator writing `aircraft.json` for tar1090); a decoder lane that revisits claimed channels
more often (the weighted lane exists).

Open: whether a built-in VDL2 decoder is worth it (D8PSK burst modem, Reed-Solomon, AVLC, X.25, ASN.1 for
ADS-C/CPDLC; weeks, and dumpvdl2 has years of tuning) - only if Windows users ask.

## Digital voice in our own code? (estimate)

Today: dsd-fme + mbelib as an external add-on (gotcha 45). The work splits in two:

1. **Framing and metadata** (who talks, which talkgroup, text and GPS data), no audio. DMR: symbol timing,
   sync patterns, CACH (Hamming), slot type (Golay), embedded and full link control (BPTC 196,96 + Reed-Solomon
   12,9), data headers. YSF: Golay, convolutional code with Viterbi, CRC. Roughly **4-6 weeks for DMR and
   2-3 for YSF** for someone fluent in it, mostly in tests; the owner has no real DMR signal, so it would
   rest on synthetic frames and a reporter.
2. **The vocoder (AMBE+2 / IMBE).** Voice needs it. It is DVSI's proprietary codec (patents, which is why
   mbelib is never distributed - gotcha 45); the open implementation is itself a reverse-engineered
   work of unclear status. A clean-room decoder has no public specification to work from: **months
   (3-6) and a legal question mark**. Not recommended.

So: keep dsd-fme as an external add-on (the new Add-ons page gives it a better home); a metadata-only
decoder in our code is possible later for "who is talking" without sound, but only if a tester with real
DMR traffic exists. These are estimates, not measurements.
