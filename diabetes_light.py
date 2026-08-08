#!/usr/bin/env python3
"""
diabetes_light.py - drive a Philips Hue light from Dexcom CGM data.

An ambient display, NOT an alarm. Keep your Dexcom app's alarms enabled.

Usage:
    python diabetes_light.py --pair          # one-time: press bridge link button first
    python diabetes_light.py --list-lights   # find your light's id
    python diabetes_light.py --once          # single update, for testing
    python diabetes_light.py                 # run forever (this is what the service runs)
"""

import argparse
import colorsys
import inspect
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import urllib3

try:
    from pydexcom import Dexcom
except ImportError:
    sys.exit("pydexcom not installed. Run: pip install pydexcom requests")

# --------------------------------------------------------------------------
# COLOUR STOPS — edit these
# --------------------------------------------------------------------------
# Each entry is (glucose value, hex colour). The light interpolates smoothly
# between neighbouring stops, so a reading of 85 lands part-way between the
# 70 and 100 colours.
#
# Any number of stops works — two, or twenty. They get sorted automatically,
# so you can add one in the middle without reordering the list. Values below
# the first stop show the first colour; above the last, the last colour.
#
# Units follow your meter: mg/dL by default, or mmol/L if you set
# GLUCOSE_UNITS=mmol. Prefer overriding COLOR_STOPS in diabetes_light.env
# rather than editing here, so a code update doesn't overwrite your palette.

# Warm colours sit in the middle where nothing needs doing — reds and ambers are
# easy to live with and don't wreck your night vision. The edges deliberately
# break out of that warm family into colours that look wrong in a room, so a
# glance registers them as "something changed" rather than as decoration.
#
# The high side climbs through cream and white on its way to cyan rather than
# taking the shorter route through green. Green means "fine" to anyone who has
# ever looked at a CGM app, and 200 is not fine.

COLOR_STOPS = [
    (55,  "#FF00A8"),   # urgent low  - harsh magenta
    (70,  "#FF0033"),   # low         - hard red
    (90,  "#FF4D00"),   # low-normal  - warm red-orange
    (115, "#FF9500"),   # in range    - amber
    (145, "#FFC400"),   # in range    - soft gold
    (175, "#FFE0A0"),   # drifting up - pale cream
    (200, "#FFFFFF"),   # high        - washed out white
    (250, "#00D0FF"),   # urgent high - harsh cyan
]

# --------------------------------------------------------------------------
# BRIGHTNESS — edit these
# --------------------------------------------------------------------------
# Percentages, 1-100, matching the slider in the Hue app.
#
# Brightness is a freshness channel, separate from colour. A fresh reading
# shows at MAX; past FRESH_MINUTES it fades toward MIN; past STALE_MINUTES the
# light goes out entirely. So colour tells you the number and brightness tells
# you how much to trust it.
#
# MAX of 100 is bright enough to read by. For a bedside light try 30-50; for
# across a room in daylight, 80+. As with the colours, prefer overriding these
# in diabetes_light.env so a code update doesn't reset them.
#
# URGENT_BELOW is the exception to all of the above: at or under that reading
# the light goes to URGENT_LEVEL and stays there, ignoring the freshness fade.

BRIGHTNESS = {
    "max": 70,             # while the reading is fresh
    "min": 10,             # just before the reading is declared stale
    "fresh_minutes": 6,    # full brightness up to here
    "stale_minutes": 13,   # light off past here, and the watchdog window
                           # (WATCHDOG_MINUTES) must be comfortably later
    "urgent_below": 70,    # at or under this reading, override the fade
    "urgent_level": 100,   # % to use when that happens
}

# --------------------------------------------------------------------------
# TREND ADJUSTMENT — edit these
# --------------------------------------------------------------------------
# Dexcom sends a trend arrow with every reading. These offsets shift the number
# before the colour and the brightness level are picked, so the light shows
# roughly where the arrow says you're heading rather than where you were five
# minutes ago. A steady 145 stays gold; a 145 falling fast lands on 130 and
# leans further toward orange.
#
# Units follow COLOR_STOPS — mg/dL by default, so mmol/L users want something
# like 0.3 / 0.6 / 0.8 instead of 5 / 10 / 15.
#
# Two things to be aware of before turning these up:
#
#   - The offset also moves the URGENT_BELOW comparison, so a falling arrow can
#     put the light at URGENT_LEVEL for a reading that is still in range. That
#     is the point of the feature, but it does mean the light is showing a guess
#     rather than a measurement. The per-cycle log line always prints the real
#     reading alongside the adjusted one.
#   - Nothing else changes. Staleness, the fade and the watchdog all still work
#     off the real reading and its real timestamp.
#
# Set TREND_ADJUST=0 in diabetes_light.env to switch the whole thing off, or
# override TREND_OFFSETS there to retune it. The names are Dexcom's own.

TREND_OFFSETS = {
    "DoubleUp":       15,   # ↑↑  rising quickly
    "SingleUp":       10,   # ↑   rising
    "FortyFiveUp":     5,   # ↗   rising slightly
    "Flat":            0,   # →   steady
    "FortyFiveDown":  -5,   # ↘   falling slightly
    "SingleDown":    -10,   # ↓   falling
    "DoubleDown":    -15,   # ↓↓  falling quickly
}

# --------------------------------------------------------------------------

# The bridge uses a self-signed cert bound to its bridge id, not its IP.
# On a LAN this is an acceptable trade; we're not sending secrets anywhere new.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("diabetes-light")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_env_file():
    """Load KEY=VALUE lines from diabetes_light.env next to this script, if present.

    Real environment variables always win, so a service manager can override.
    """
    env_path = Path(__file__).with_name("diabetes_light.env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and len(value) > 1:
            # Quoted: take the contents verbatim, so a value containing '#'
            # or trailing spaces survives intact.
            quote = value[0]
            closing = value.find(quote, 1)
            value = value[1:closing] if closing > 0 else value[1:]
        else:
            # Unquoted: everything after whitespace-then-# is a comment.
            # Requiring the whitespace means a '#' inside a password is safe
            # as long as it isn't preceded by a space.
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        os.environ.setdefault(key.strip(), value)


def env(name, default=None, required=False, cast=str):
    raw = os.environ.get(name, default)
    if raw is None or raw == "":
        if required:
            sys.exit(f"Missing required setting: {name}")
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        sys.exit(f"Setting {name} has an invalid value: {raw!r}")


class Config:
    def __init__(self):
        self.dexcom_username = env("DEXCOM_USERNAME", required=True)
        self.dexcom_password = env("DEXCOM_PASSWORD", required=True)
        self.dexcom_region = env("DEXCOM_REGION", "us")

        self.bridge_ip = env("HUE_BRIDGE_IP")
        self.app_key = env("HUE_APP_KEY")

        # HUE_LIGHT_IDS is the current name; HUE_LIGHT_ID still works. Either
        # accepts a comma-separated list.
        raw_ids = os.environ.get("HUE_LIGHT_IDS") or os.environ.get("HUE_LIGHT_ID") or ""
        self.light_ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
        if len(self.light_ids) != len(set(self.light_ids)):
            seen, deduped = set(), []
            for light_id in self.light_ids:
                if light_id not in seen:
                    seen.add(light_id)
                    deduped.append(light_id)
            log.warning("Duplicate light ids in the list; ignoring the repeats.")
            self.light_ids = deduped

        # Colour stops. The env var wins over the table at the top of the file,
        # so your palette survives dropping in a new copy of the script.
        raw_stops = os.environ.get("COLOR_STOPS")
        if raw_stops:
            try:
                raw_stops = json.loads(raw_stops)
            except json.JSONDecodeError as exc:
                sys.exit(f"COLOR_STOPS is not valid JSON: {exc}")
        else:
            raw_stops = COLOR_STOPS
        try:
            self.stops = prepare_stops(raw_stops)
        except ValueError as exc:
            sys.exit(f"Bad colour stops: {exc}")

        # Trend offsets, same deal: the env var wins over the table in the file.
        # Parsed even when the feature is off, so a typo is caught at startup
        # rather than the first time someone switches it back on.
        self.trend_adjust = env("TREND_ADJUST", "1") == "1"
        raw_offsets = os.environ.get("TREND_OFFSETS")
        if raw_offsets:
            try:
                raw_offsets = json.loads(raw_offsets)
            except json.JSONDecodeError as exc:
                sys.exit(f"TREND_OFFSETS is not valid JSON: {exc}")
        else:
            raw_offsets = TREND_OFFSETS
        try:
            self.trend_offsets = prepare_trend_offsets(raw_offsets)
        except ValueError as exc:
            sys.exit(f"Bad TREND_OFFSETS: {exc}")
        if not self.trend_adjust:
            self.trend_offsets = {}

        # Freshness, in minutes. Full brightness until FRESH_MINUTES, decaying
        # to MIN_BRIGHTNESS at STALE_MINUTES, then off entirely.
        # STALE_MINUTES must land BEFORE the watchdog window, so that when this
        # process is alive it always wins the race and the bridge timer only
        # ever fires if we're genuinely gone.
        for legacy, replacement in (("FRESH_SECONDS", "FRESH_MINUTES"),
                                    ("STALE_SECONDS", "STALE_MINUTES")):
            if os.environ.get(legacy):
                sys.exit(
                    f"{legacy} is no longer used — these are minutes now. "
                    f"Replace it with {replacement} "
                    f"(divide your old value by 60)."
                )

        self.poll_seconds = env("POLL_SECONDS", "60", cast=int)
        self.fresh_minutes = env("FRESH_MINUTES", str(BRIGHTNESS["fresh_minutes"]), cast=float)
        self.stale_minutes = env("STALE_MINUTES", str(BRIGHTNESS["stale_minutes"]), cast=float)
        self.max_brightness = env("MAX_BRIGHTNESS", str(BRIGHTNESS["max"]), cast=float)
        self.min_brightness = env("MIN_BRIGHTNESS", str(BRIGHTNESS["min"]), cast=float)
        self.urgent_below = env("URGENT_BELOW", str(BRIGHTNESS["urgent_below"]), cast=float)
        self.urgent_level = env("URGENT_LEVEL", str(BRIGHTNESS["urgent_level"]), cast=float)

        # Everything downstream works in seconds; minutes are the input unit.
        self.fresh_seconds = self.fresh_minutes * 60
        self.stale_seconds = self.stale_minutes * 60

        for name, value in (("MAX_BRIGHTNESS", self.max_brightness),
                            ("MIN_BRIGHTNESS", self.min_brightness),
                            ("URGENT_LEVEL", self.urgent_level)):
            if not 1 <= value <= 100:
                sys.exit(f"{name} must be between 1 and 100 (got {value:g}).")
        if self.min_brightness > self.max_brightness:
            sys.exit("MIN_BRIGHTNESS cannot exceed MAX_BRIGHTNESS.")
        if self.fresh_minutes >= self.stale_minutes:
            sys.exit(
                f"FRESH_MINUTES ({self.fresh_minutes:g}) must be less than "
                f"STALE_MINUTES ({self.stale_minutes:g})."
            )

        # Dead-man's switch built on the Hue v1 schedules API.
        self.watchdog = env("WATCHDOG", "1") == "1"
        self.watchdog_minutes = env("WATCHDOG_MINUTES", "15", cast=float)

        if self.watchdog:
            margin_seconds = self.watchdog_minutes * 60 - self.stale_seconds
            if margin_seconds < self.poll_seconds * 2:
                log.warning(
                    "STALE_MINUTES (%g) is too close to WATCHDOG_MINUTES (%g). "
                    "The bridge timer may switch the lights off mid-fade, after "
                    "which the next poll turns them back on — a visible flicker. "
                    "Leave at least %g minutes between them.",
                    self.stale_minutes, self.watchdog_minutes,
                    self.poll_seconds * 2 / 60,
                )


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

HEX_PATTERN = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(value):
    """'#FF8000' or 'F80' -> (r, g, b) floats in 0..1."""
    match = HEX_PATTERN.match(str(value).strip())
    if not match:
        raise ValueError(f"Not a hex colour: {value!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return tuple(int(digits[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def prepare_stops(raw):
    """Validate and normalise the stop table into (value, hsv) pairs.

    Sorts by value and collapses duplicates, so stops can be listed in any
    order and a value can be edited without reordering the whole list.
    """
    if not raw:
        raise ValueError("COLOR_STOPS is empty; at least one stop is required.")

    seen = {}
    for entry in raw:
        try:
            value, colour = entry
        except (TypeError, ValueError):
            raise ValueError(f"Stop must be (value, hex): {entry!r}")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Stop value is not a number: {value!r}")
        if value in seen:
            log.warning("Duplicate stop at %g; the later colour wins.", value)
        seen[value] = colorsys.rgb_to_hsv(*parse_hex(colour))

    return sorted(seen.items())


def _blend_hsv(first, second, fraction):
    """Interpolate two HSV colours, taking the shortest way round the wheel.

    Blending in RGB would drag saturated colours through grey at the midpoint.
    Going the short way keeps red->green through orange rather than through
    the entire spectrum backwards.
    """
    hue_a, sat_a, val_a = first
    hue_b, sat_b, val_b = second

    # Hue is meaningless for greys and whites; borrow the other end's.
    if sat_a == 0:
        hue_a = hue_b
    elif sat_b == 0:
        hue_b = hue_a

    delta = ((hue_b - hue_a + 0.5) % 1.0) - 0.5
    return (
        (hue_a + delta * fraction) % 1.0,
        sat_a + (sat_b - sat_a) * fraction,
        val_a + (val_b - val_a) * fraction,
    )


def glucose_to_rgb(value, stops):
    """Colour for a glucose value, clamped at both ends of the table."""
    if value <= stops[0][0]:
        return colorsys.hsv_to_rgb(*stops[0][1])
    if value >= stops[-1][0]:
        return colorsys.hsv_to_rgb(*stops[-1][1])

    for (low_value, low_hsv), (high_value, high_hsv) in zip(stops, stops[1:]):
        if low_value <= value <= high_value:
            span = high_value - low_value
            fraction = (value - low_value) / span if span else 0.0
            return colorsys.hsv_to_rgb(*_blend_hsv(low_hsv, high_hsv, fraction))

    return colorsys.hsv_to_rgb(*stops[-1][1])  # unreachable, but explicit


def rgb_to_hex(rgb):
    return "#" + "".join(f"{round(channel * 255):02X}" for channel in rgb)


def rgb_to_xy(red, green, blue):
    """sRGB to CIE xy, per Philips' documented conversion."""
    def linearise(channel):
        if channel > 0.04045:
            return ((channel + 0.055) / 1.055) ** 2.4
        return channel / 12.92

    red, green, blue = linearise(red), linearise(green), linearise(blue)
    x = red * 0.4124 + green * 0.3576 + blue * 0.1805
    y = red * 0.2126 + green * 0.7152 + blue * 0.0722
    z = red * 0.0193 + green * 0.1192 + blue * 0.9505
    total = x + y + z
    if total == 0:
        return 0.3127, 0.3290  # D65 white
    return x / total, y / total


def glucose_to_xy(value, stops):
    return rgb_to_xy(*glucose_to_rgb(value, stops))


# --------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------

# Dexcom's own names, in its own order. The numbers are the indexes Share uses
# (0 is "None", 8 and 9 are "NotComputable" and "RateOutOfRange") — those three
# carry no direction, so they never get an offset.
TREND_ARROWS = {
    "DoubleUp": "↑↑", "SingleUp": "↑", "FortyFiveUp": "↗", "Flat": "→",
    "FortyFiveDown": "↘", "SingleDown": "↓", "DoubleDown": "↓↓",
}
TREND_NAMES = tuple(TREND_ARROWS)
_TREND_BY_INDEX = dict(enumerate(TREND_NAMES, start=1))

# Loose matching for the env file, so DoubleUp / double_up / "double up" are
# one key, and the two awkward FortyFive names can be written as 45up / 45down.
_TREND_LOOKUP = {name.lower(): name for name in TREND_NAMES}
_TREND_LOOKUP.update({"45up": "FortyFiveUp", "45down": "FortyFiveDown"})


def _canonical_trend(name):
    """Match a trend name loosely; None if it isn't one we know."""
    return _TREND_LOOKUP.get(re.sub(r"[^a-z0-9]", "", str(name).lower()))


def prepare_trend_offsets(raw):
    """Validate the trend table into {canonical name: offset}."""
    if not raw:
        return {}
    if not hasattr(raw, "items"):
        raise ValueError('must be an object, e.g. {"SingleUp": 10, "SingleDown": -10}')

    offsets = {}
    for name, offset in raw.items():
        canonical = _canonical_trend(name)
        if canonical is None:
            raise ValueError(
                f"unknown trend {name!r}. Valid names: {', '.join(TREND_NAMES)}."
            )
        try:
            offsets[canonical] = float(offset)
        except (TypeError, ValueError):
            raise ValueError(f"offset for {canonical} is not a number: {offset!r}")
    return offsets


def trend_name(reading):
    """Canonical trend name for a reading, or None if it hasn't got one.

    Prefers the name and falls back to the index, because pydexcom has spelled
    this both ways across versions and `trend` has been a plain int and an enum.
    Anything unrecognised means no adjustment, never a crash — the arrow is the
    least important part of a reading.
    """
    direction = getattr(reading, "trend_direction", None)
    if direction is not None:
        name = _canonical_trend(getattr(direction, "value", direction))
        if name:
            return name

    index = getattr(reading, "trend", None)
    index = getattr(index, "value", index)
    try:
        return _TREND_BY_INDEX.get(int(index))
    except (TypeError, ValueError):
        return None


def trend_offset(reading, offsets):
    """How far this reading's arrow shifts it. 0 when there's no usable arrow."""
    return offsets.get(trend_name(reading), 0.0)


def adjusted_value(value, offset):
    """The number the colour and level are read off.

    Floored at 1 so a hard fall can't push the reading to zero or below, where
    it would stop meaning anything.
    """
    return max(value + offset, 1)


def brightness_for_age(age_seconds, cfg, value=None):
    """Brightness for a reading. None means 'go dark'.

    Freshness normally drives this, but a reading at or below urgent_below
    overrides the fade and pins the light at urgent_level. Staleness still
    wins over both — a low we can no longer verify is turned off rather than
    left blazing at full brightness on data that might be an hour old.
    """
    if age_seconds >= cfg.stale_seconds:
        return None

    if value is not None and value <= cfg.urgent_below:
        return cfg.urgent_level

    if age_seconds <= cfg.fresh_seconds:
        return cfg.max_brightness
    span = max(cfg.stale_seconds - cfg.fresh_seconds, 1)
    frac = (age_seconds - cfg.fresh_seconds) / span
    return cfg.max_brightness - frac * (cfg.max_brightness - cfg.min_brightness)


# --------------------------------------------------------------------------
# Hue bridge
# --------------------------------------------------------------------------

class HueBridge:
    def __init__(self, ip, app_key):
        self.ip = ip
        self.app_key = app_key
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"hue-application-key": app_key})
        # Per-light, keyed by v2 id — lights on one bridge can differ in both
        # effect support and how far they dim.
        self.supports_effects = {}
        self.v1_ids = {}
        self.min_dim_level = None
        self._watchdog_ids = {}

    def _v2(self, path):
        return f"https://{self.ip}/clip/v2/resource/{path}"

    def _v1(self, path):
        return f"https://{self.ip}/api/{self.app_key}/{path}"

    def lights(self):
        response = self.session.get(self._v2("light"), timeout=10)
        response.raise_for_status()
        return response.json().get("data", [])

    def inspect_lights(self, light_ids):
        """Cache per-light capabilities so we only send fields each light accepts."""
        found = {light.get("id"): light for light in self.lights()}
        missing = [light_id for light_id in light_ids if light_id not in found]
        if missing:
            raise RuntimeError(
                f"Light(s) not found on bridge {self.ip}: {', '.join(missing)}. "
                f"Run --list-lights to see what's there."
            )

        floors = []
        for light_id in light_ids:
            light = found[light_id]
            effects = light.get("effects", {}).get("status_values", [])
            self.supports_effects[light_id] = "no_effect" in effects
            self.v1_ids[light_id] = light.get("id_v1", "").rsplit("/", 1)[-1] or None
            if "color" not in light:
                log.warning(
                    "Light %s (%s) is white-only; it will track brightness but "
                    "not colour.",
                    light_id, light.get("metadata", {}).get("name", "?"),
                )
            floor = light.get("dimming", {}).get("min_dim_level")
            if floor is not None:
                floors.append(floor)

        # The strictest floor wins, so no light in the set is asked to go
        # somewhere it can't follow.
        self.min_dim_level = max(floors) if floors else None
        return found

    def set_color(self, light_ids, xy, brightness):
        """Paint every light. One failure doesn't stop the others."""
        payload = {
            "on": {"on": True},
            "dimming": {"brightness": round(brightness, 1)},
            "color": {"xy": {"x": round(xy[0], 4), "y": round(xy[1], 4)}},
        }
        for light_id in light_ids:
            body = dict(payload)
            # The Hue Go's physical button can start a built-in effect that
            # would otherwise ignore our colour until someone presses it again.
            if self.supports_effects.get(light_id):
                body["effects"] = {"effect": "no_effect"}
            try:
                response = self.session.put(
                    self._v2(f"light/{light_id}"), json=body, timeout=10
                )
                response.raise_for_status()
            except Exception as exc:
                log.warning("Could not update light %s: %s", light_id, exc)

    def turn_off(self, light_ids):
        for light_id in light_ids:
            try:
                response = self.session.put(
                    self._v2(f"light/{light_id}"), json={"on": {"on": False}}, timeout=10
                )
                response.raise_for_status()
            except Exception as exc:
                log.warning("Could not turn off light %s: %s", light_id, exc)

    # --- dead-man's switch, v1 API ---------------------------------------
    # One-shot timer schedules living on the bridge, one per light. Re-armed on
    # every NEW reading. If readings stop, or this process dies, the timers
    # expire and the bridge itself turns the lights off. This is the only
    # failure signal that survives the PC losing power.
    #
    # Off is the same thing the script does when it notices staleness itself,
    # which is deliberate: dark always means "no reading you can trust", with
    # no second meaning to learn. Which of the two turned it off is a question
    # for the logs, not the light.
    #
    # Note this is the legacy v1 API, which Signify intends to retire.

    WATCHDOG_PREFIX = "diabetes-light-wd"

    def _watchdog_name(self, light_id):
        # v1 schedule names cap at 32 characters, so key off the short v1 id.
        return f"{self.WATCHDOG_PREFIX}-{self.v1_ids.get(light_id) or light_id[:8]}"

    def _watchdog_body(self, light_id, minutes):
        total = max(round(minutes * 60), 1)
        hours, remainder = divmod(total, 3600)
        mins, secs = divmod(remainder, 60)
        return {
            "name": self._watchdog_name(light_id),
            "description": "diabetes-light dead-man's switch",
            "command": {
                "address": f"/api/{self.app_key}/lights/{self.v1_ids[light_id]}/state",
                "method": "PUT",
                "body": {"on": False},
            },
            # PT[hh]:[mm]:[ss] — hours first. PT15:00:00 is fifteen HOURS.
            "localtime": f"PT{hours:02d}:{mins:02d}:{secs:02d}",
            "autodelete": False,
            "status": "enabled",
        }

    def _existing_schedules(self):
        return self.session.get(self._v1("schedules"), timeout=10).json()

    def _find_watchdog(self, light_id):
        if light_id in self._watchdog_ids:
            return self._watchdog_ids[light_id]
        wanted = self._watchdog_name(light_id)
        for sched_id, sched in self._existing_schedules().items():
            if sched.get("name") == wanted:
                self._watchdog_ids[light_id] = sched_id
                return sched_id
        return None

    def arm_watchdog(self, light_ids, minutes):
        """Create or reset each timer. Writing localtime restarts the countdown."""
        for light_id in light_ids:
            if not self.v1_ids.get(light_id):
                log.warning(
                    "Light %s has no v1 id; the watchdog can't cover it.", light_id
                )
                continue
            try:
                self._arm_one(light_id, minutes)
            except Exception as exc:
                log.warning("Could not arm watchdog for light %s: %s", light_id, exc)

    def _arm_one(self, light_id, minutes, retry=True):
        body = self._watchdog_body(light_id, minutes)
        sched_id = self._find_watchdog(light_id)
        if sched_id:
            response = self.session.put(
                self._v1(f"schedules/{sched_id}"), json=body, timeout=10
            )
            response.raise_for_status()
            if any("error" in item for item in response.json()):
                # Schedule was deleted out from under us; recreate it once.
                self._watchdog_ids.pop(light_id, None)
                if retry:
                    return self._arm_one(light_id, minutes, retry=False)
                raise RuntimeError("watchdog schedule kept disappearing")
            return sched_id
        response = self.session.post(self._v1("schedules"), json=body, timeout=10)
        response.raise_for_status()
        result = response.json()[0]
        if "error" in result:
            raise RuntimeError(f"Could not create watchdog: {result['error']}")
        self._watchdog_ids[light_id] = result["success"]["id"]
        return self._watchdog_ids[light_id]

    def disarm_watchdog(self, light_ids):
        """Stop the timers without deleting them.

        Called when this process has already handled staleness itself. The
        timers would only turn the lights off again, so this is tidiness
        rather than correctness — it stops a redundant write landing minutes
        after we've already done the same thing.
        """
        for light_id in light_ids:
            try:
                sched_id = self._find_watchdog(light_id)
                if sched_id:
                    self.session.put(
                        self._v1(f"schedules/{sched_id}"),
                        json={"status": "disabled"}, timeout=10,
                    )
            except Exception as exc:
                log.warning("Could not disarm watchdog for light %s: %s", light_id, exc)


def discover_bridge():
    """Ask Philips' discovery service. Needs internet; prefer HUE_BRIDGE_IP."""
    response = requests.get("https://discovery.meethue.com", timeout=10)
    response.raise_for_status()
    bridges = response.json()
    if not bridges:
        sys.exit("No bridge found. Set HUE_BRIDGE_IP manually.")
    return bridges[0]["internalipaddress"]


def pair(ip):
    """Create an application key. The link button must be pressed first."""
    response = requests.post(
        f"https://{ip}/api", json={"devicetype": "diabetes-light#pc"}, verify=False, timeout=10
    )
    payload = response.json()[0]
    if "error" in payload:
        sys.exit(f"Pairing failed: {payload['error'].get('description')}")
    return payload["success"]["username"]


# --------------------------------------------------------------------------
# Dexcom
# --------------------------------------------------------------------------

def reading_timestamp(reading):
    """Timezone-aware timestamp for a reading, tolerating naive values."""
    stamp = reading.datetime
    if stamp.tzinfo is None:
        # pydexcom can hand back a naive local timestamp. Attach the machine's
        # local zone; if your PC's clock or zone is wrong, ages will be wrong.
        stamp = stamp.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return stamp.astimezone(timezone.utc)


def reading_age_seconds(reading):
    """Age of a reading in seconds."""
    age = (datetime.now(timezone.utc) - reading_timestamp(reading)).total_seconds()
    return max(age, 0.0)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.running = True
        self.bridge = HueBridge(cfg.bridge_ip, cfg.app_key)
        self.bridge.inspect_lights(cfg.light_ids)
        log.info(
            "Driving %d light%s: %s",
            len(cfg.light_ids), "" if len(cfg.light_ids) == 1 else "s",
            ", ".join(cfg.light_ids),
        )
        floor = self.bridge.min_dim_level
        if floor is not None and cfg.min_brightness < floor:
            # Below its own floor a bulb may flicker or simply not light,
            # which would read as "off" and mean something it doesn't.
            log.warning(
                "MIN_BRIGHTNESS is %.1f%% but the dimmest light in the set "
                "bottoms out at %.1f%%. Raise MIN_BRIGHTNESS, or the faint end "
                "of the fade may not show at all.",
                cfg.min_brightness, floor,
            )
        self.dexcom = None
        # (value, seconds of age at last evaluation, trend offset). The offset
        # is carried with the reading so a Share hiccup doesn't jump the colour
        # while we age the same reading out.
        self.last_good = None
        self.last_stamp = None  # timestamp of the most recent NEW reading

    def stop(self, *_):
        log.info("Shutting down.")
        self.running = False

    def connect_dexcom(self):
        cfg = self.cfg
        region = (cfg.dexcom_region or "us").lower()

        # pydexcom changed its constructor at 0.4.1: up to and including
        # 0.4.0 it took ous=True/False, from 0.4.1 it takes
        # region="us"/"ous"/"jp". Ask the installed version which it speaks
        # rather than guessing. (Region is a str enum, so a plain string is
        # accepted by the newer form.)
        params = inspect.signature(Dexcom.__init__).parameters

        if "region" in params:
            self.dexcom = Dexcom(
                username=cfg.dexcom_username,
                password=cfg.dexcom_password,
                region=region,
            )
        elif "ous" in params:
            if region == "jp":
                log.warning(
                    "This pydexcom is older than 0.4.1 and has no Japan region. "
                    "Falling back to the non-US endpoint. Run "
                    "'pip install -U pydexcom' for proper support."
                )
            self.dexcom = Dexcom(
                username=cfg.dexcom_username,
                password=cfg.dexcom_password,
                ous=(region != "us"),
            )
            log.info("pydexcom 0.4.0 or older detected; using the ous flag.")
        else:
            sys.exit(
                "This version of pydexcom has a constructor I don't recognise "
                f"({', '.join(params)}). Run 'pip install -U pydexcom'."
            )

        log.info("Connected to Dexcom Share (%s).", region)

    def tick(self):
        cfg = self.cfg
        try:
            if self.dexcom is None:
                self.connect_dexcom()
            reading = self.dexcom.get_current_glucose_reading()
        except Exception as exc:
            # Sessions expire and the Share API is occasionally moody. Drop the
            # client so the next tick reconnects, and fall through to ageing
            # out the last known reading rather than freezing the light.
            log.warning("Dexcom fetch failed (%s). Will reconnect.", exc)
            self.dexcom = None
            reading = None

        is_new = False
        trend = ""
        if reading is not None:
            trend = f" {reading.trend_arrow}"
            stamp = reading_timestamp(reading)
            # Share keeps returning the last reading forever after a sensor
            # stops. Only a timestamp that has actually advanced counts as new,
            # or the watchdog would re-arm itself on stale data indefinitely.
            if self.last_stamp is None or stamp > self.last_stamp:
                is_new = True
                self.last_stamp = stamp
            self.last_good = (
                reading.value,
                reading_age_seconds(reading),
                trend_offset(reading, cfg.trend_offsets),
            )
        elif self.last_good is not None:
            value, age, offset = self.last_good
            self.last_good = (value, age + cfg.poll_seconds, offset)

        if is_new and cfg.watchdog:
            self.bridge.arm_watchdog(cfg.light_ids, cfg.watchdog_minutes)
            log.debug("Watchdog re-armed for %g minutes.", cfg.watchdog_minutes)

        if self.last_good is None:
            log.info("No reading yet - lights off")
            self.bridge.turn_off(cfg.light_ids)
            return

        value, age, offset = self.last_good
        # The arrow moves the number the light is drawn from, but nothing else:
        # staleness below still tests the real age of the real reading.
        shown = adjusted_value(value, offset)
        adjustment = "" if not offset else f" {offset:+g} -> {shown:g}"
        brightness = brightness_for_age(age, cfg, shown)

        if brightness is None:
            # We've handled staleness ourselves, so stand the bridge timers
            # down; they'd only repeat the same action a couple of minutes later.
            if cfg.watchdog:
                self.bridge.disarm_watchdog(cfg.light_ids)
            log.info(
                "Glucose %s%s | %.0fs old - STALE, lights off",
                value, trend, age,
            )
            self.bridge.turn_off(cfg.light_ids)
            return

        rgb = glucose_to_rgb(shown, cfg.stops)
        urgent = shown <= cfg.urgent_below
        log.info(
            "Glucose %s%s%s | %.0fs old%s | %s | %.0f%%%s",
            value, trend, adjustment, age, "" if is_new else " [repeat]",
            rgb_to_hex(rgb), brightness, "  URGENT LOW" if urgent else "",
        )
        self.bridge.set_color(cfg.light_ids, rgb_to_xy(*rgb), brightness)

    def run(self, once=False):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while self.running:
            try:
                self.tick()
            except Exception as exc:
                # Never let one bad cycle kill the process. The service manager
                # is a backstop for real crashes, not for transient errors.
                log.exception("Cycle failed: %s", exc)
            if once:
                return
            for _ in range(self.cfg.poll_seconds):
                if not self.running:
                    return
                time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Dexcom to Philips Hue ambient light")
    parser.add_argument("--pair", action="store_true", help="create a bridge application key")
    parser.add_argument("--list-lights", action="store_true", help="list lights and ids")
    parser.add_argument("--once", action="store_true", help="run a single update and exit")
    parser.add_argument(
        "--preview", nargs="?", const="", metavar="LOW:HIGH:STEP",
        help="print the colour ramp without touching the light",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Normal output to stdout, problems to stderr. Service managers capture the
    # two streams separately, so the error log only ever holds things worth
    # reading rather than a copy of every routine glucose line.
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    to_stdout = logging.StreamHandler(sys.stdout)
    to_stdout.setFormatter(formatter)
    to_stdout.addFilter(lambda record: record.levelno < logging.WARNING)

    to_stderr = logging.StreamHandler(sys.stderr)
    to_stderr.setFormatter(formatter)
    to_stderr.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    root.handlers = [to_stdout, to_stderr]

    load_env_file()

    if args.pair:
        ip = os.environ.get("HUE_BRIDGE_IP") or discover_bridge()
        print(f"Bridge at {ip}. Press the link button, then press Enter.")
        input()
        key = pair(ip)
        print("\nAdd these to diabetes_light.env:\n")
        print(f"HUE_BRIDGE_IP={ip}")
        print(f"HUE_APP_KEY={key}")
        return

    if args.list_lights:
        ip = os.environ.get("HUE_BRIDGE_IP") or discover_bridge()
        key = os.environ.get("HUE_APP_KEY")
        if not key:
            sys.exit("HUE_APP_KEY not set. Run --pair first.")
        for light in HueBridge(ip, key).lights():
            name = light.get("metadata", {}).get("name", "?")
            colour = "colour" if "color" in light else "white only"
            print(f"{light['id']}  {name}  ({colour})")
        return

    if args.preview is not None:
        raw = os.environ.get("COLOR_STOPS")
        try:
            stops = prepare_stops(json.loads(raw) if raw else COLOR_STOPS)
        except json.JSONDecodeError as exc:
            sys.exit(f"COLOR_STOPS is not valid JSON: {exc}")
        except ValueError as exc:
            sys.exit(f"Bad colour stops: {exc}")
        if args.preview:
            try:
                low, high, step = (float(part) for part in args.preview.split(":"))
            except ValueError:
                sys.exit("--preview expects LOW:HIGH:STEP, e.g. 40:300:10")
        else:
            low = stops[0][0] - (stops[-1][0] - stops[0][0]) * 0.1
            high = stops[-1][0] + (stops[-1][0] - stops[0][0]) * 0.1
            step = max((high - low) / 24, 0.1)

        stop_values = {value for value, _ in stops}
        print(f"{len(stops)} stop{'s' if len(stops) != 1 else ''}: "
              + ", ".join(f"{v:g}" for v, _ in stops))
        print()
        current = low
        while current <= high + 1e-9:
            hex_colour = rgb_to_hex(glucose_to_rgb(current, stops))
            marker = " <- stop" if current in stop_values else ""
            print(f"  {current:8.1f}  {hex_colour}{marker}")
            current += step
        # Stops that the sampling step skipped over still matter; show them.
        for value, _ in stops:
            if not (low <= value <= high):
                print(f"  {value:8.1f}  {rgb_to_hex(glucose_to_rgb(value, stops))}  (outside preview range)")

        raw_offsets = os.environ.get("TREND_OFFSETS")
        try:
            offsets = prepare_trend_offsets(
                json.loads(raw_offsets) if raw_offsets else TREND_OFFSETS
            )
        except json.JSONDecodeError as exc:
            sys.exit(f"TREND_OFFSETS is not valid JSON: {exc}")
        except ValueError as exc:
            sys.exit(f"Bad TREND_OFFSETS: {exc}")

        if env("TREND_ADJUST", "1") != "1":
            print("\nTrend adjustment: off (TREND_ADJUST=0)")
        elif not offsets:
            print("\nTrend adjustment: nothing configured")
        else:
            # Sample from the middle of the table, where a few points either way
            # is most likely to be crossing between stops.
            sample = stops[len(stops) // 2][0]
            print(f"\nTrend adjustment, shown against a reading of {sample:g}:")
            for name in TREND_NAMES:
                offset = offsets.get(name, 0.0)
                shown = adjusted_value(sample, offset)
                label = f"{offset:+g}" if offset else "0"
                print(f"  {TREND_ARROWS[name]:<3} {name:<14} {label:>6}"
                      f"  -> {shown:7g}  {rgb_to_hex(glucose_to_rgb(shown, stops))}")

        fresh = env("FRESH_MINUTES", str(BRIGHTNESS["fresh_minutes"]), cast=float) * 60
        stale = env("STALE_MINUTES", str(BRIGHTNESS["stale_minutes"]), cast=float) * 60
        cap = env("MAX_BRIGHTNESS", str(BRIGHTNESS["max"]), cast=float)
        floor = env("MIN_BRIGHTNESS", str(BRIGHTNESS["min"]), cast=float)
        curve = type("B", (), {
            "fresh_seconds": fresh, "stale_seconds": stale,
            "max_brightness": cap, "min_brightness": floor,
        })

        print(f"\nBrightness: {cap:g}% fresh -> {floor:g}% at {stale / 60:g} min, then off\n")
        span = int(stale) + 120
        for age in range(0, span, max(span // 10, 60)):
            level = brightness_for_age(age, curve)
            shown = "off" if level is None else f"{level:5.1f}%"
            note = ""
            if level is not None and age <= fresh:
                note = "  (fresh)"
            elif level is None:
                note = "  (stale)"
            print(f"  {age // 60:3d}m {age % 60:02d}s  {shown}{note}")
        return

    cfg = Config()
    if not cfg.bridge_ip:
        cfg.bridge_ip = discover_bridge()
    if not cfg.app_key:
        sys.exit("HUE_APP_KEY not set. Run --pair first.")
    if not cfg.light_ids:
        sys.exit("HUE_LIGHT_IDS not set. Run --list-lights first.")

    try:
        runner = Runner(cfg)
    except requests.exceptions.RequestException as exc:
        sys.exit(
            f"Could not reach the Hue bridge at {cfg.bridge_ip}: {exc}\n"
            f"Check HUE_BRIDGE_IP, and that this machine is on the same "
            f"network as the bridge."
        )
    except RuntimeError as exc:
        sys.exit(str(exc))

    runner.run(once=args.once)


if __name__ == "__main__":
    main()
