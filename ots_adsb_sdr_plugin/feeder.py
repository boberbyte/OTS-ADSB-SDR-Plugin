"""Runs the ADS-B receiver and puts the aircraft on the connected TAK clients' maps.

The markers are published straight to the exchange "groups" (routing
"<group>.OUT"), the same way the server itself reaches the clients. They do NOT
pass cot_parser: nothing is written to the database and nothing is forwarded to
Meshtastic/LoRa.
"""
import json
import os
import shlex
import subprocess
import threading
import time

import pika
from opentakserver.extensions import logger

from . import aircraft
from .default_config import P

FEED_UID = "adsb-sdr-feed"
TAG = "ADSB-SDR:"
# aircraft.json is rewritten every second. Older than this means the decoder is not running.
JSON_STALE_S = 30


class Source:
    """One running input: a decoder's aircraft.json, or a recording being played back.

    kind "sdr" starts the decoder itself (argv), "external" only reads the file
    of a decoder that somebody else runs.
    """

    def __init__(self, table, kind, argv=None, json_dir=None, path=None, speed=1, log_path=None):
        self.table, self.kind, self.argv, self.json_dir = table, kind, argv, json_dir
        self.path, self.speed, self.log_path = path, speed, log_path
        self.error = None
        self._stop = threading.Event()
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="adsb-source")
        self._thread.start()

    def alive(self):
        return self._thread.is_alive()

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._thread.join(8)

    def _run(self):
        try:
            if self.kind == "replay":
                for snap in aircraft.replay_snapshots(self.path, self.speed, self._stop):
                    self.table.feed(snap)
            else:
                self._run_json()
        except Exception as exc:
            if not self._stop.is_set():
                self.error = str(exc)[:200]

    def _run_json(self):
        path = os.path.join(self.json_dir, "aircraft.json")
        if self.kind == "sdr":
            os.makedirs(self.json_dir, exist_ok=True)
            try:
                os.remove(path)  # left over from the previous run
            except OSError:
                pass
            with open(self.log_path, "w") as log:
                self._proc = subprocess.Popen(self.argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        last_stamp = None
        fresh_at = time.time()
        while not self._stop.is_set():
            if self._proc and self._proc.poll() is not None:
                # The decoder ended on its own: usually the SDR is missing or used by another program.
                tail = ""
                try:
                    with open(self.log_path, errors="replace") as fh:
                        tail = ([ln.strip() for ln in fh if ln.strip()] or [""])[-1]
                except OSError:
                    pass
                raise RuntimeError(f"{os.path.basename(self.argv[0])} exited ({self._proc.returncode}): {tail}")
            snap = aircraft.read_snapshot(path)
            stamp = snap.get("now") if snap else None
            if isinstance(stamp, (int, float)) and stamp != last_stamp and time.time() - stamp < JSON_STALE_S:
                last_stamp = stamp
                fresh_at = time.time()
                self.table.feed(snap)
            elif time.time() - fresh_at > JSON_STALE_S:
                raise RuntimeError(f"{path} is not being updated")
            self._stop.wait(1)


class Feeder:
    def __init__(self, app):
        self._app = app
        self._stop = None
        self._thread = None
        self._conn = self._ch = None
        self._state = None
        self._source = None
        self._source_cfg = None
        self._table = aircraft.AircraftTable()

    # --- on/off (called from the plugin's activate/stop) ---
    def start(self):
        if self._thread and self._thread.is_alive() and self._stop and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True, name="adsb-sdr")
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()
        self._stop_source()
        self._set_state("disabled")

    # --- helpers ---
    def cfg(self, key):
        return self._app.config.get(P + key)

    def _cache_dir(self):
        d = os.path.expanduser(self.cfg("CACHE_DIR"))
        os.makedirs(d, exist_ok=True)
        return d

    def _set_state(self, state, **extra):
        """The state in a small file, so that scripts and status icons can show it."""
        if state == self._state and not extra:
            return
        if state != self._state:
            logger.info(f"{TAG} {state}" + (f" ({extra['error']})" if "error" in extra else ""))
        self._state = state
        try:
            path = os.path.join(self._cache_dir(), "status.json")
            with open(path + ".tmp", "w") as fh:
                json.dump({"state": state, "time": int(time.time()), **extra}, fh)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    def _clients_connected(self):
        ports = (self._app.config.get("OTS_TCP_STREAMING_PORT", 8088), self._app.config.get("OTS_SSL_STREAMING_PORT", 8089))
        flt = " or ".join(f"sport = :{p}" for p in ports)
        try:
            out = subprocess.run(
                ["ss", "-Htn", "state", "established", f"( {flt} )"], capture_output=True, text=True, timeout=5
            ).stdout
            return bool(out.strip())
        except Exception:
            return True

    def _stop_source(self):
        source, self._source, self._source_cfg = self._source, None, None
        if source:
            source.stop()

    def _ensure_source(self):
        """Starts the input, or restarts it when its settings were changed in the dashboard."""
        keys = ("SOURCE", "DECODER", "DEVICE_SERIAL", "EXTRA_ARGS", "JSON_DIR", "REPLAY_FILE", "REPLAY_SPEED")
        wanted = tuple(self.cfg(k) for k in keys)
        if self._source and self._source_cfg == wanted:
            return
        self._stop_source()
        self._table.aircraft = {}
        kind = self.cfg("SOURCE")
        json_dir = os.path.expanduser(self.cfg("JSON_DIR"))
        if kind == "replay":
            self._source = Source(
                self._table, "replay", path=os.path.expanduser(self.cfg("REPLAY_FILE")), speed=self.cfg("REPLAY_SPEED")
            )
        elif kind == "external":
            self._source = Source(self._table, "external", json_dir=json_dir)
        else:
            # These options are the same in readsb and dump1090-fa.
            argv = [os.path.expanduser(self.cfg("DECODER")), "--device-type", "rtlsdr"]
            if self.cfg("DEVICE_SERIAL"):
                argv += ["--device", self.cfg("DEVICE_SERIAL")]
            argv += ["--write-json", json_dir, "--write-json-every", "1", "--quiet"]
            argv += shlex.split(self.cfg("EXTRA_ARGS") or "")
            self._source = Source(
                self._table, "sdr", argv=argv, json_dir=json_dir, log_path=os.path.join(self._cache_dir(), "decoder.log")
            )
        self._source_cfg = wanted
        logger.info(f"{TAG} source started: {kind}")

    def _publish(self, cots, ttl_ms):
        if not self._conn or self._conn.is_closed or not self._ch or self._ch.is_closed:
            c = self._app.config
            self._conn = pika.BlockingConnection(
                pika.ConnectionParameters(
                    c.get("OTS_RABBITMQ_SERVER_ADDRESS", "127.0.0.1"),
                    credentials=pika.PlainCredentials(
                        c.get("OTS_RABBITMQ_USERNAME", "guest"), c.get("OTS_RABBITMQ_PASSWORD", "guest")
                    ),
                    heartbeat=0,
                )
            )
            self._ch = self._conn.channel()
        props = pika.BasicProperties(expiration=str(ttl_ms))
        key = f"{self.cfg('GROUP')}.OUT"
        for cot in cots:
            self._ch.basic_publish(
                exchange="groups", routing_key=key, body=json.dumps({"cot": cot, "uid": FEED_UID}), properties=props
            )

    def _close(self):
        try:
            if self._conn and self._conn.is_open:
                self._conn.close()
        except Exception:
            pass
        self._conn = self._ch = None

    # --- main loop ---
    def _run(self, stop):
        try:
            self._loop(stop)
        except Exception as exc:
            logger.exception(f"{TAG} the feed stopped: {exc}")
            if not stop.is_set():
                self._set_state("error", error=str(exc)[:200])
        finally:
            self._stop_source()
            self._close()

    def _idle(self, stop, state):
        self._stop_source()  # frees the SDR and stops the decoding load
        self._close()
        self._set_state(state)
        stop.wait(self.cfg("IDLE_CHECK_S"))

    def _loop(self, stop):
        pause_flag = os.path.join(self._cache_dir(), "paused")
        while not stop.is_set():
            if os.path.exists(pause_flag):
                self._idle(stop, "paused")
                continue
            if self.cfg("ONLY_WHEN_CLIENTS") and not self._clients_connected():
                self._idle(stop, "waiting")
                continue
            if self._source and not self._source.alive():
                error = self._source.error or "the source ended"
                self._stop_source()
                self._set_state("error", error=error)
                stop.wait(30)  # then try again
                continue
            self._ensure_source()
            interval = self.cfg("INTERVAL_S")
            started = time.time()
            try:
                # Aircraft move fast, but a marker must survive one lost update.
                stale_s = max(interval * 3, 20)
                current = self._table.current(self.cfg("AIRCRAFT_TIMEOUT_S"))
                cots = [aircraft.make_cot(a, stale_s=stale_s) for a in current]
                if stop.is_set():
                    break
                self._publish(cots, ttl_ms=int(interval * 1000))
                self._set_state(
                    "running", source=self.cfg("SOURCE"), aircraft=len(cots), messages=self._table.messages
                )
            except Exception as exc:
                logger.warning(f"{TAG} could not publish: {exc}")
                self._close()
            stop.wait(max(1, interval - (time.time() - started)))
