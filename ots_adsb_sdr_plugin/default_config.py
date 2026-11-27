import os
import re
import traceback

import yaml
from flask import current_app as app
from opentakserver.extensions import logger

P = "OTS_ADSB_SDR_PLUGIN_"

# These decide which program is run on the server and where it writes its
# files. They can only be changed by editing config.yml on the server itself,
# never through the web API, so that a dashboard login cannot be turned into
# running arbitrary commands.
LOCKED = (P + "DECODER", P + "EXTRA_ARGS", P + "JSON_DIR")
MAY_BE_EMPTY = (P + "DEVICE_SERIAL", P + "EXTRA_ARGS")
SOURCES = ("sdr", "external", "replay")


class DefaultConfig:
    # Loaded first; values in ~/ots/config.yml take precedence.
    # "sdr" = the plugin runs the decoder, "external" = read the aircraft.json of a
    # decoder that is already running, "replay" = play back a recording.
    OTS_ADSB_SDR_PLUGIN_SOURCE = "sdr"
    OTS_ADSB_SDR_PLUGIN_DECODER = "readsb"  # readsb or dump1090-fa; program name on PATH, or full path
    OTS_ADSB_SDR_PLUGIN_DEVICE_SERIAL = ""  # RTL-SDR serial number; empty = first device
    OTS_ADSB_SDR_PLUGIN_EXTRA_ARGS = ""  # extra decoder arguments, e.g. "--gain 40"
    # Where aircraft.json is. /dev/shm is in memory: the file is rewritten every second.
    OTS_ADSB_SDR_PLUGIN_JSON_DIR = "/dev/shm/ots-adsb"
    OTS_ADSB_SDR_PLUGIN_REPLAY_FILE = "~/.cache/ots-adsb/replay.jsonl"
    OTS_ADSB_SDR_PLUGIN_REPLAY_SPEED = 1  # 1 = real time
    OTS_ADSB_SDR_PLUGIN_INTERVAL_S = 5  # how often all known aircraft are sent to the clients
    OTS_ADSB_SDR_PLUGIN_AIRCRAFT_TIMEOUT_S = 60  # drop an aircraft whose position is older than this
    OTS_ADSB_SDR_PLUGIN_ONLY_WHEN_CLIENTS = True  # receiver is off while no TAK client is connected
    OTS_ADSB_SDR_PLUGIN_IDLE_CHECK_S = 15
    OTS_ADSB_SDR_PLUGIN_CACHE_DIR = "~/.cache/ots-adsb"
    OTS_ADSB_SDR_PLUGIN_GROUP = "__ANON__"

    @staticmethod
    def keys():
        return [k for k in vars(DefaultConfig) if k.startswith(P)]

    @staticmethod
    def validate(config: dict) -> dict:
        try:
            for key, value in config.items():
                if key not in DefaultConfig.keys():
                    return {"success": False, "error": f"{key} is not a valid config key"}
                default = getattr(DefaultConfig, key)
                if key in LOCKED and value != app.config.get(key, default):
                    return {"success": False, "error": f"{key} can only be changed in config.yml on the server"}
                if isinstance(default, bool):
                    if not isinstance(value, bool):
                        return {"success": False, "error": f"{key} must be true or false"}
                elif isinstance(default, int):
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                        return {"success": False, "error": f"{key} must be a non-negative number"}
                elif not isinstance(value, str) or (not value.strip() and key not in MAY_BE_EMPTY):
                    return {"success": False, "error": f"{key} must be a non-empty string"}
            if config.get(P + "SOURCE", "sdr") not in SOURCES:
                return {"success": False, "error": "SOURCE must be sdr, external or replay"}
            if not re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.:/-]*)?", config.get(P + "DEVICE_SERIAL", "")):
                return {"success": False, "error": "DEVICE_SERIAL contains invalid characters"}
            if config.get(P + "INTERVAL_S", 5) < 2:
                return {"success": False, "error": "INTERVAL_S must be at least 2"}
            if config.get(P + "IDLE_CHECK_S", 15) < 5:
                return {"success": False, "error": "IDLE_CHECK_S must be at least 5"}
            if config.get(P + "AIRCRAFT_TIMEOUT_S", 60) < 10:
                return {"success": False, "error": "AIRCRAFT_TIMEOUT_S must be at least 10"}
            if config.get(P + "REPLAY_SPEED", 1) <= 0:
                return {"success": False, "error": "REPLAY_SPEED must be greater than 0"}
            return {"success": True, "error": ""}
        except BaseException as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def update_config(config: dict) -> dict:
        try:
            valid = DefaultConfig.validate(config)
            if not valid["success"]:
                return valid
            path = os.path.join(app.config.get("OTS_DATA_FOLDER"), "config.yml")
            with open(path) as fh:
                on_disk = yaml.safe_load(fh.read())
            for key, value in config.items():
                app.config.update({key: value})
                on_disk[key] = value
            with open(path + ".tmp", "w") as fh:
                yaml.safe_dump(on_disk, fh)
            os.chmod(path + ".tmp", os.stat(path).st_mode & 0o777)
            os.replace(path + ".tmp", path)
            return {"success": True}
        except BaseException as e:
            logger.error(f"Failed to update config: {e}")
            logger.error(traceback.format_exc())
            return {"success": False, "error": str(e)}
