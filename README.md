# OTS-ADSB-SDR-Plugin

A plugin for [OpenTAKServer](https://github.com/brian7704/OpenTAKServer) that shows
aircraft received **over the air** as markers on the TAK map. An RTL-SDR receiver on
the server listens to ADS-B (1090 MHz), [readsb](https://github.com/wiedehopf/readsb)
or [dump1090-fa](https://github.com/flightaware/dump1090) decodes it, and the plugin
sends each aircraft to the connected TAK clients with call sign, altitude, course
and speed.

No internet connection and no API account is needed. You see what your own antenna hears.

It is the sister of [OTS-AIS-SDR-Plugin](https://github.com/boberbyte/OTS-AIS-SDR-Plugin),
which does the same for ships. The two can run on the same server, each with its
own receiver.

> Status: early version. Tested on OpenTAKServer 1.8.3 on Linux (Raspberry Pi CM5)
> with readsb 3.16 in `sdr` mode. The `external` and `replay` modes are only tested
> with made-up data.

## What it does

- Starts and stops the decoder by itself. Turn the plugin on and off under **Plugins**
  in the OpenTAKServer dashboard.
- The receiver is switched off while no TAK client is connected, to save power
  (can be changed).
- Aircraft are sent straight to the clients. They are not stored in the database and
  are not forwarded to Meshtastic/LoRa.
- **External mode** reads the data of a decoder that is already running on the
  server, for example one that feeds tar1090 or a flight tracking site.
- **Replay mode** plays back a recording instead of using the receiver. Useful
  for a demonstration.

## Requirements

- OpenTAKServer 1.8 or later on Linux (the plugin uses the `ss` command to see
  whether a client is connected).
- An RTL-SDR receiver with an antenna for 1090 MHz. AIS and ADS-B are on
  different frequencies, so the AIS plugin needs a receiver of its own.
- readsb or dump1090-fa, built or installed on the server.

## Installation

Install into the same Python environment as OpenTAKServer, then restart the server:

```
~/.opentakserver_venv/bin/pip install git+https://github.com/boberbyte/OTS-ADSB-SDR-Plugin.git
sudo systemctl restart opentakserver
```

If `readsb` is not on the server's `PATH`, or you use dump1090-fa, set the program
in `~/ots/config.yml` before restarting:

```
OTS_ADSB_SDR_PLUGIN_DECODER: /usr/bin/dump1090-fa
```

With two receivers on the server, give each plugin the serial number of its own
(`DEVICE_SERIAL`). `rtl_test` lists them.

## Settings

All names start with `OTS_ADSB_SDR_PLUGIN_`. They are changed under the plugin's
**Config** tab in the dashboard, except the three marked "server only".

| Setting | Default | Meaning |
|---|---|---|
| `SOURCE` | `sdr` | `sdr` = the plugin runs the decoder, `external` = read a decoder that is already running, `replay` = play back a recording |
| `DEVICE_SERIAL` | empty | Serial number of the RTL-SDR to use. Empty = the first one |
| `INTERVAL_S` | `5` | How often all known aircraft are sent to the clients |
| `AIRCRAFT_TIMEOUT_S` | `60` | An aircraft whose position is older than this is removed |
| `ONLY_WHEN_CLIENTS` | `true` | Receiver is off while no TAK client is connected |
| `REPLAY_FILE` | `~/.cache/ots-adsb/replay.jsonl` | Recording used in replay mode |
| `REPLAY_SPEED` | `1` | Playback speed, `1` = real time |
| `GROUP` | `__ANON__` | TAK group that receives the markers |
| `DECODER` | `readsb` | Program to run. **Server only** |
| `EXTRA_ARGS` | empty | Extra decoder arguments, for example `--gain 40`. **Server only** |
| `JSON_DIR` | `/dev/shm/ots-adsb` | Folder with the decoder's `aircraft.json`. **Server only** |

"Server only" settings decide which program the server runs and where it writes.
They can only be changed by editing `~/ots/config.yml`, not through the dashboard,
so that a dashboard login cannot be used to run other commands on the server.

## Using a decoder that is already running

Only one program can use an SDR at a time. If readsb or dump1090-fa already runs
as a service, let the plugin read its data instead of starting a second one. In
`~/ots/config.yml`:

```
OTS_ADSB_SDR_PLUGIN_SOURCE: external
OTS_ADSB_SDR_PLUGIN_JSON_DIR: /run/readsb
```

The folder is `/run/readsb` for readsb and `/run/dump1090-fa` for dump1090-fa.
In this mode the plugin never starts or stops the receiver.

## Making a recording for replay mode

A recording is a file with one `aircraft.json` snapshot per line. While the plugin
(or another decoder) is receiving, run:

```
while sleep 2; do tr -d '\n' < /dev/shm/ots-adsb/aircraft.json; echo; done > ~/.cache/ots-adsb/replay.jsonl
```

Stop it with Ctrl+C, then set `SOURCE` to `replay`. The recording is played in a
loop with the original timing.

## Pausing without the dashboard

Create the file `~/.cache/ots-adsb/paused` to pause, remove it to resume. The
current state is written to `~/.cache/ots-adsb/status.json`. The decoder's own
output is in `~/.cache/ots-adsb/decoder.log`.

## Notes

- If the receiver is busy or missing, the state becomes `error` and the plugin
  tries again every 30 seconds.
- An aircraft is shown at its GNSS altitude when it sends one, otherwise at its
  barometric altitude.
- ADS-B positions are broadcast openly by the aircraft themselves. Check the rules
  for receiving and sharing radio traffic where you live.

## License

GPL-3.0-or-later. See `LICENSE`.
