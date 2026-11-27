"""Aircraft table and CoT markers built from the decoder's aircraft.json.

readsb and dump1090-fa both write the same file: one snapshot of every aircraft
they currently track, already merged from the individual ADS-B messages.

No Flask or OpenTAKServer imports here, so it can be tested on its own.
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

KNOT = 0.514444  # m/s
FOOT = 0.3048  # m
UNKNOWN = 9999999.0  # CoT value for "not known"
# 24-bit ICAO address. A leading "~" marks an address that is not ICAO (TIS-B, some MLAT).
HEX = re.compile(r"~?[0-9a-f]{6}")


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def cot_type(category):
    """ADS-B emitter category (A0-C7) to a CoT type.

    Everything is drawn as unknown (yellow), so that aircraft stand out from
    the ships of the AIS plugin, which are neutral.
    """
    if category == "A7":
        return "a-u-A-C-H"  # rotorcraft
    if category == "B2":
        return "a-u-A-C-L"  # lighter than air
    if category == "B6":
        return "a-u-A-M-F-Q"  # unmanned
    if category in ("C1", "C2"):
        return "a-u-G-E-V"  # airport surface vehicle
    return "a-u-A-C-F"  # civil fixed wing


def read_snapshot(path):
    """The decoder's aircraft.json as a dict, or None when it is missing or unreadable."""
    try:
        with open(path) as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return None
    return snap if isinstance(snap, dict) else None


class AircraftTable:
    """The aircraft of the latest snapshot that have a position."""

    def __init__(self):
        self.aircraft = {}
        self.messages = 0

    def feed(self, snap):
        """Replaces the table with the contents of one aircraft.json snapshot."""
        now = time.time()
        table = {}
        for a in snap.get("aircraft") or []:
            if not isinstance(a, dict):
                continue
            icao = a.get("hex")
            lat, lon = _num(a.get("lat")), _num(a.get("lon"))
            if not isinstance(icao, str) or not HEX.fullmatch(icao.lower()):
                continue
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                continue
            # The older dump1090 names are "altitude" and "speed".
            baro = a.get("alt_baro", a.get("altitude"))
            flight = a.get("flight")
            table[icao.lower()] = {
                "hex": icao.lower(),
                "lat": lat,
                "lon": lon,
                "pos_time": now - max(_num(a.get("seen_pos")) or 0, 0),
                "flight": flight.strip() if isinstance(flight, str) else "",
                "alt_geom": _num(a.get("alt_geom")),  # feet
                "alt_baro": _num(baro),  # feet
                "on_ground": baro == "ground",
                "speed": _num(a.get("gs", a.get("speed"))),  # knots
                "track": _num(a.get("track")),
                "category": a.get("category") if isinstance(a.get("category"), str) else "",
                "squawk": a.get("squawk") if isinstance(a.get("squawk"), str) else "",
                "emergency": a.get("emergency") if isinstance(a.get("emergency"), str) else "",
                # Only present when the decoder has an aircraft database (readsb --db-file).
                "registration": a.get("r") if isinstance(a.get("r"), str) else "",
                "model": a.get("t") if isinstance(a.get("t"), str) else "",
            }
        self.aircraft = table
        if _num(snap.get("messages")) is not None:
            self.messages = snap["messages"]

    def current(self, max_age_s):
        """Aircraft whose position is at most this old."""
        limit = time.time() - max_age_s
        return [a for a in self.aircraft.values() if a["pos_time"] >= limit]


def make_cot(ac, stale_s):
    icao = ac["hex"].upper()
    callsign = ac["flight"] or ac["registration"] or f"ICAO {icao}"
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start = datetime.fromtimestamp(ac["pos_time"], timezone.utc)
    # Height above the ellipsoid is only known from the GNSS altitude. The
    # barometric one is good enough for a map marker when that is missing.
    feet = ac["alt_geom"] if ac["alt_geom"] is not None else ac["alt_baro"]
    hae = feet * FOOT if feet is not None else UNKNOWN
    le = 50.0 if ac["alt_geom"] is not None else UNKNOWN
    speed, course = ac["speed"], ac["track"]
    track = ""
    if speed is not None or course is not None:
        track = f'<track course="{course or 0:.0f}" speed="{(speed or 0) * KNOT:.1f}"/>'
    parts = [f"ADS-B: {callsign}"]
    if callsign != f"ICAO {icao}":
        parts.append(f"ICAO {icao}")
    if ac["registration"] and ac["registration"] != callsign:
        parts.append(ac["registration"])
    if ac["model"]:
        parts.append(ac["model"])
    if ac["on_ground"]:
        parts.append("on ground")
    elif feet is not None:
        # The text shows the barometric altitude, the one used in aviation.
        parts.append(f"{ac['alt_baro'] if ac['alt_baro'] is not None else feet:.0f} ft")
    if speed is not None:
        parts.append(f"{speed:.0f} kn")
    if ac["squawk"]:
        parts.append(f"squawk {ac['squawk']}")
    if ac["emergency"] and ac["emergency"] != "none":
        parts.append(f"EMERGENCY {ac['emergency']}")
    return (
        f'<event version="2.0" uid={quoteattr("ICAO-" + icao)} type="{cot_type(ac["category"])}" how="m-g" '
        f'time="{now.strftime(fmt)}" start="{start.strftime(fmt)}" '
        f'stale="{(now + timedelta(seconds=stale_s)).strftime(fmt)}">'
        f'<point lat="{ac["lat"]:.6f}" lon="{ac["lon"]:.6f}" hae="{hae:.1f}" ce="50.0" le="{le:.1f}"/>'
        f"<detail><contact callsign={quoteattr(callsign)}/>{track}"
        f"<remarks>{escape(', '.join(parts))}</remarks></detail></event>"
    )


def replay_snapshots(path, speed, stop):
    """Yields the snapshots of a recording with the original pauses between them, over and over.

    The recording has one aircraft.json snapshot per line. `stop` is a
    threading.Event; the generator ends when it is set.
    """
    while not stop.is_set():
        previous = None
        count = 0
        with open(path) as fh:
            for line in fh:
                if stop.is_set():
                    return
                try:
                    snap = json.loads(line)
                    stamp = float(snap["now"])
                except (ValueError, KeyError, TypeError):
                    continue
                if previous is not None:
                    # Long silences in the recording are shortened so the map never looks dead.
                    stop.wait(min(max(stamp - previous, 0), 10) / speed)
                previous = stamp
                count += 1
                yield snap
        if count == 0:
            raise ValueError(f"no aircraft.json snapshots in {path}")
        stop.wait(2)
