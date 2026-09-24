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
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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
#
# These are the defaults for every light. A bedroom and a kitchen rarely want
# the same numbers, so max, min and urgent_level can each be overridden per
# room in HUE_LIGHT_GROUPS — see prepare_light_groups below.

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
# STATUS PAGE — optional
# --------------------------------------------------------------------------
# A web page showing what the per-cycle log line shows: the reading, its age,
# the colour and brightness each room was sent, plus recent history and any
# warnings. 0 leaves it off. Set a port (e.g. STATUS_PORT=8768 in
# diabetes_light.env) to turn it on.
#
# It only ever listens on 127.0.0.1, and speaks plain HTTP. To reach it from a
# phone over HTTPS, put `tailscale serve` in front of it — see the README.

STATUS_PORT = 0

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


# --------------------------------------------------------------------------
# Light groups
# --------------------------------------------------------------------------
# A group is a set of lights that share brightness settings — in practice, a
# room. Every group shows the same colour at the same moment, because they are
# all displaying one reading; what a group gets to decide is how loudly its
# room says it. 30% at the bedside and 90% in the kitchen are the same reading.
#
# Timing (FRESH_MINUTES, STALE_MINUTES) and URGENT_BELOW are deliberately NOT
# per-group. They answer "can this reading be trusted, and is it dangerous",
# which is a fact about the data and the person, not about a room — and a
# per-room staleness would mean one light going dark while another still
# glowed on the same dead reading. That is exactly the ambiguity "off" is
# supposed to be free of.

# The name used when the lights aren't grouped, i.e. plain HUE_LIGHT_IDS.
DEFAULT_GROUP_NAME = "lights"


def brightness_from_env():
    """The global brightness block: the table above, with env overrides."""
    return {
        "max": env("MAX_BRIGHTNESS", str(BRIGHTNESS["max"]), cast=float),
        "min": env("MIN_BRIGHTNESS", str(BRIGHTNESS["min"]), cast=float),
        "fresh_minutes": env("FRESH_MINUTES", str(BRIGHTNESS["fresh_minutes"]), cast=float),
        "stale_minutes": env("STALE_MINUTES", str(BRIGHTNESS["stale_minutes"]), cast=float),
        "urgent_below": env("URGENT_BELOW", str(BRIGHTNESS["urgent_below"]), cast=float),
        "urgent_level": env("URGENT_LEVEL", str(BRIGHTNESS["urgent_level"]), cast=float),
    }


def validate_brightness(settings, group=None):
    """Range-check one brightness block. Raises ValueError with a plain message.

    `group` names the group in the message, and switches the wording from env
    var names to the JSON keys, so the text points at what the user actually
    typed.
    """
    where = f' in group "{group}"' if group else ""
    for key, env_name in (("max", "MAX_BRIGHTNESS"), ("min", "MIN_BRIGHTNESS"),
                          ("urgent_level", "URGENT_LEVEL")):
        name = key if group else env_name
        value = settings[key]
        if not 1 <= value <= 100:
            raise ValueError(f"{name}{where} must be between 1 and 100 (got {value:g}).")
    low, high = ("min", "max") if group else ("MIN_BRIGHTNESS", "MAX_BRIGHTNESS")
    if settings["min"] > settings["max"]:
        raise ValueError(f"{low}{where} cannot exceed {high}.")


class LightGroup:
    """One room's lights, plus the brightness settings they share."""

    def __init__(self, name, light_ids, settings):
        self.name = name
        self.light_ids = list(light_ids)
        self.max_brightness = settings["max"]
        self.min_brightness = settings["min"]
        self.urgent_level = settings["urgent_level"]
        # Shared across groups, but carried here so brightness_for_age() needs
        # nothing but a group to work out a level.
        self.urgent_below = settings["urgent_below"]
        self.fresh_seconds = settings["fresh_minutes"] * 60
        self.stale_seconds = settings["stale_minutes"] * 60


# Group keys are matched loosely, like the trend names: "max", "max_brightness"
# and "maxBrightness" are one key. Both a friendly and a formal spelling exist
# for each, because the env file writes MAX_BRIGHTNESS while the table at the
# top of this file writes "max".
_GROUP_KEYS = {
    "lights": "lights", "lightids": "lights", "ids": "lights",
    "max": "max", "maxbrightness": "max",
    "min": "min", "minbrightness": "min",
    "urgent": "urgent_level", "urgentlevel": "urgent_level",
}

# Settings that only make sense globally. Named individually so a user who
# tries one gets told why rather than "unknown key".
_GROUP_SHARED_KEYS = {
    "freshminutes", "staleminutes", "urgentbelow", "pollseconds",
    "watchdog", "watchdogminutes", "colorstops", "colourstops",
    "trendoffsets", "trendadjust",
}


def _normalise_key(key):
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def prepare_light_groups(raw, defaults):
    """Validate the group table into a list of LightGroup.

    Accepts either a full spec or the shorthand where a group is just its
    lights:

        {"bedroom": {"lights": ["id-a", "id-b"], "max": 30, "min": 5},
         "kitchen": ["id-c"],
         "office":  "id-d, id-e"}

    Anything a group doesn't set falls back to `defaults`, so the common case
    is a couple of rooms differing in one number.
    """
    if not raw:
        raise ValueError("no groups defined.")
    if not hasattr(raw, "items"):
        raise ValueError('must be an object, e.g. {"bedroom": ["light-id"]}')

    groups = []
    owner = {}  # light id -> the group that already claims it
    for name, spec in raw.items():
        name = str(name).strip()
        if not name:
            raise ValueError("a group name can't be empty.")

        # Shorthand: the value is the light list itself.
        if isinstance(spec, (str, list, tuple)):
            spec = {"lights": spec}
        if not hasattr(spec, "items"):
            raise ValueError(
                f'group "{name}" must be a list of light ids or an object, '
                f"got {spec!r}"
            )

        settings = dict(defaults)
        raw_ids = None
        for key, value in spec.items():
            canonical = _GROUP_KEYS.get(_normalise_key(key))
            if canonical is None:
                if _normalise_key(key) in _GROUP_SHARED_KEYS:
                    raise ValueError(
                        f'"{key}" in group "{name}" is a global setting, not a '
                        f"per-group one. Colour, timing and the urgent "
                        f"threshold are shared by every light — only max, min "
                        f"and urgent_level differ per group."
                    )
                raise ValueError(
                    f'unknown setting "{key}" in group "{name}". '
                    f"Valid keys: lights, max, min, urgent_level."
                )
            if canonical == "lights":
                raw_ids = value
                continue
            try:
                settings[canonical] = float(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f'{canonical} in group "{name}" is not a number: {value!r}'
                )

        if raw_ids is None:
            raise ValueError(f'group "{name}" has no "lights".')
        if isinstance(raw_ids, str):
            raw_ids = raw_ids.split(",")
        elif not isinstance(raw_ids, (list, tuple)):
            raise ValueError(
                f'"lights" in group "{name}" must be a list of ids or a '
                f"comma-separated string, got {raw_ids!r}"
            )

        light_ids = []
        for light_id in raw_ids:
            light_id = str(light_id).strip()
            if not light_id:
                continue
            if light_id in light_ids:
                log.warning(
                    'Light %s is listed twice in group "%s"; ignoring the repeat.',
                    light_id, name,
                )
                continue
            if light_id in owner:
                # Two groups asking for different brightness on one bulb has no
                # answer, and picking one silently would leave a light that
                # quietly ignores half its config. Make the user choose.
                raise ValueError(
                    f'light {light_id} is in both "{owner[light_id]}" and '
                    f'"{name}". A light can only be in one group.'
                )
            owner[light_id] = name
            light_ids.append(light_id)

        if not light_ids:
            raise ValueError(f'group "{name}" has no lights in it.')

        validate_brightness(settings, name)
        groups.append(LightGroup(name, light_ids, settings))

    return groups


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
        settings = brightness_from_env()
        self.fresh_minutes = settings["fresh_minutes"]
        self.stale_minutes = settings["stale_minutes"]
        self.max_brightness = settings["max"]
        self.min_brightness = settings["min"]
        self.urgent_below = settings["urgent_below"]
        self.urgent_level = settings["urgent_level"]

        # Everything downstream works in seconds; minutes are the input unit.
        self.fresh_seconds = self.fresh_minutes * 60
        self.stale_seconds = self.stale_minutes * 60

        try:
            validate_brightness(settings)
        except ValueError as exc:
            sys.exit(str(exc))
        if self.fresh_minutes >= self.stale_minutes:
            sys.exit(
                f"FRESH_MINUTES ({self.fresh_minutes:g}) must be less than "
                f"STALE_MINUTES ({self.stale_minutes:g})."
            )

        # Groups. Without HUE_LIGHT_GROUPS every light is one unnamed group
        # running the settings above, which is exactly what this did before
        # groups existed.
        raw_groups = os.environ.get("HUE_LIGHT_GROUPS")
        self.named_groups = bool(raw_groups)
        if raw_groups:
            try:
                self.groups = prepare_light_groups(json.loads(raw_groups), settings)
            except json.JSONDecodeError as exc:
                sys.exit(f"HUE_LIGHT_GROUPS is not valid JSON: {exc}")
            except ValueError as exc:
                sys.exit(f"Bad HUE_LIGHT_GROUPS: {exc}")
            if self.light_ids:
                log.warning(
                    "HUE_LIGHT_GROUPS and HUE_LIGHT_IDS are both set. The "
                    "groups win; HUE_LIGHT_IDS is ignored. Delete it to "
                    "silence this."
                )
            # One flat list for the things that treat every light the same:
            # the capability check, the watchdog, and turning everything off.
            self.light_ids = [
                light_id for group in self.groups for light_id in group.light_ids
            ]
        elif self.light_ids:
            self.groups = [LightGroup(DEFAULT_GROUP_NAME, self.light_ids, settings)]
        else:
            self.groups = []

        # Group names are padded to a fixed width in the log line, same as
        # every other field, so consecutive lines stay in columns.
        self.group_width = max((len(g.name) for g in self.groups), default=0)

        # Dead-man's switch built on the Hue v1 schedules API.
        self.watchdog = env("WATCHDOG", "1") == "1"
        self.watchdog_minutes = env("WATCHDOG_MINUTES", "15", cast=float)

        self.status_port = env("STATUS_PORT", str(STATUS_PORT), cast=int) or 0
        if not 0 <= self.status_port <= 65535:
            sys.exit(f"STATUS_PORT must be between 1 and 65535, or 0 for off "
                     f"(got {self.status_port}).")

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


# Hue in degrees -> name, as (upper_bound, name) walked in order. Red wraps, so
# it appears at both ends. The warm half is cut more finely than the cold half
# because the default palette spends most of its range there and "orange" for
# everything from 90 to 160 would tell you nothing.
HUE_NAMES = [
    (14, "red"), (38, "orange"), (48, "amber"), (66, "yellow"),
    (160, "green"), (195, "cyan"), (250, "blue"), (290, "violet"),
    (330, "magenta"), (345, "pink"), (360, "red"),
]


def rgb_to_name(rgb):
    """A rough colour word for a reading, e.g. 'amber' or 'pale cyan'.

    Derived from the colour actually being sent, not from the glucose value,
    so it stays honest when someone replaces the palette in their env file.
    It's a label for skim-reading a log — the hex beside it is the truth.
    """
    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)

    # Hue is meaningless once the colour is nearly grey, so don't report one.
    if saturation < 0.10:
        return "white" if value >= 0.75 else "grey" if value >= 0.2 else "black"

    degrees = hue * 360
    for upper, name in HUE_NAMES:
        if degrees < upper:
            break

    if value < 0.35:
        return f"dark {name}"
    if saturation < 0.35:
        return f"pale {name}"
    return name


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


def brightness_for_age(age_seconds, group, value=None):
    """Brightness for a reading, for one group. None means 'go dark'.

    `group` is a LightGroup, but anything carrying the same six brightness
    attributes works — Config does, which is what the single-group case used
    to pass.

    Freshness normally drives this, but a reading at or below urgent_below
    overrides the fade and pins the light at urgent_level. Staleness still
    wins over both — a low we can no longer verify is turned off rather than
    left blazing at full brightness on data that might be an hour old.
    """
    if age_seconds >= group.stale_seconds:
        return None

    if value is not None and value <= group.urgent_below:
        return group.urgent_level

    if age_seconds <= group.fresh_seconds:
        return group.max_brightness
    span = max(group.stale_seconds - group.fresh_seconds, 1)
    frac = (age_seconds - group.fresh_seconds) / span
    return group.max_brightness - frac * (group.max_brightness - group.min_brightness)


def format_levels(levels, width=0):
    """The brightness column of the log line, from [(group, level)] pairs.

    A width of 0 means don't name the groups, which is the ungrouped case:
    the field is then the bare percentage it has always been. Otherwise each
    group gets a fixed-width `NN% name` cell so a run of lines still reads as
    columns.
    """
    if not width:
        level = levels[0][1]
        return f"{'off':>4}" if level is None else f"{level:3.0f}%"
    return "  ".join(
        (f"{'off':>4} " if level is None else f"{level:3.0f}% ")
        + f"{group.name:<{width}}"
        for group, level in levels
    )


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
        self.min_dim_levels = {}
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
            self.min_dim_levels[light_id] = light.get("dimming", {}).get("min_dim_level")

        return found

    def dim_floor(self, light_ids):
        """The strictest floor among these lights, or None if none reported one.

        Per group rather than per bridge: a kitchen bulb that bottoms out at
        5% shouldn't force a warning about a bedside lamp that can reach 1%.
        Within a group the strictest one wins, so no light in the set is asked
        to go somewhere it can't follow.
        """
        floors = [
            self.min_dim_levels[light_id] for light_id in light_ids
            if self.min_dim_levels.get(light_id) is not None
        ]
        return max(floors) if floors else None

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
# Status page
# --------------------------------------------------------------------------
# The per-cycle log line, as a web page. The page reports what the loop has
# already decided and never works out a colour or a level of its own, so it
# can't disagree with the light.
#
# It binds 127.0.0.1 and nothing else, on purpose, and there is no setting to
# change that. The page shows health data and has no login, so the only ways in
# are this machine and whatever you deliberately put in front of it — in
# practice `tailscale serve`, which also supplies a real HTTPS certificate.
# A self-signed cert here would only teach you to click through warnings.
#
# History is kept in memory. A restart starts it again, which is honest: the
# page never shows a reading this process didn't see.

STATUS_HISTORY_HOURS = 12
STATUS_PROBLEMS_KEPT = 50


class RecentProblems(logging.Handler):
    """The last few warnings and errors, for the status page.

    Installed at startup rather than with the page, so a config warning logged
    before the page exists still shows up on it.
    """

    def __init__(self, size=STATUS_PROBLEMS_KEPT):
        super().__init__(level=logging.WARNING)
        self.items = deque(maxlen=size)

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        self.items.append(
            {"time": record.created, "level": record.levelname, "message": message}
        )

    def snapshot(self):
        self.acquire()
        try:
            return list(self.items)
        finally:
            self.release()


class StatusBoard:
    """What the page shows: every recent cycle, plus recent problems.

    Written by the loop, read by the web server's threads, hence the lock.
    """

    def __init__(self, cfg, problems):
        self.problems = problems
        self.boot = time.time()
        self._lock = threading.Lock()
        self._seq = 0
        self.history_size = max(
            int(STATUS_HISTORY_HOURS * 3600 / max(cfg.poll_seconds, 1)), 1
        )
        self._cycles = deque(maxlen=self.history_size)
        # Warnings quote exceptions, and a failed v1 call quotes its URL —
        # which has the Hue app key in it. err.log on this machine is one
        # thing; a page on the tailnet is another. Very short values are left
        # alone rather than blanking every matching fragment of a message.
        self._secrets = [
            secret for secret in (cfg.app_key, cfg.dexcom_password)
            if secret and len(secret) >= 4
        ]
        self.settings = {
            "poll_seconds": cfg.poll_seconds,
            "fresh_minutes": cfg.fresh_minutes,
            "stale_minutes": cfg.stale_minutes,
            "watchdog": cfg.watchdog,
            "watchdog_minutes": cfg.watchdog_minutes,
            "urgent_below": cfg.urgent_below,
            "trend_adjust": bool(cfg.trend_offsets),
            "named_groups": cfg.named_groups,
            "groups": [
                {"name": group.name, "lights": len(group.light_ids),
                 "max": group.max_brightness, "min": group.min_brightness,
                 "urgent_level": group.urgent_level}
                for group in cfg.groups
            ],
            "history_size": self.history_size,
        }

    def record(self, cycle):
        with self._lock:
            self._seq += 1
            self._cycles.append(dict(cycle, seq=self._seq))

    def _redact(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "[hidden]")
        return text

    def snapshot(self, after=0):
        """Everything the page needs. `after` skips cycles it already has.

        The page asks every few seconds, and twelve hours of cycles is a few
        hundred KB, so only the full load sends the lot. `boot` changes when
        the process restarts, which tells the page its sequence numbers no
        longer mean anything.
        """
        with self._lock:
            cycles = [cycle for cycle in self._cycles if cycle["seq"] > after]
        problems = [
            dict(item, message=self._redact(item["message"]))
            for item in self.problems.snapshot()
        ]
        return {
            "now": time.time(), "boot": self.boot, "settings": self.settings,
            "cycles": cycles, "problems": problems,
        }


def make_status_handler(board):
    page = STATUS_PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def version_string(self):
            return "diabetes-light"

        def _send(self, code, body, content_type, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Live health data: nothing between here and the browser should
            # keep a copy, and a cached page is a page showing old numbers.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            if parts.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8", {
                    # The page is self-contained: one inline style, one inline
                    # script, and a fetch back to this server. Nothing else
                    # may load.
                    "Content-Security-Policy": (
                        "default-src 'none'; connect-src 'self'; "
                        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                        "img-src data:; base-uri 'none'; form-action 'none'; "
                        "frame-ancestors 'none'"
                    ),
                })
            if parts.path == "/status.json":
                after = 0
                match = re.fullmatch(r"after=(\d+)", parts.query)
                if match:
                    after = int(match.group(1))
                body = json.dumps(board.snapshot(after)).encode("utf-8")
                return self._send(200, body, "application/json")
            self._send(404, b"404 Not Found\n", "text/plain; charset=utf-8")

        do_HEAD = do_GET  # noqa: N815

        def _refuse(self):
            # Read-only by construction: nothing on this page changes anything.
            self._send(405, b"405 Method Not Allowed\n", "text/plain; charset=utf-8",
                       {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

        def log_message(self, format, *args):  # noqa: A002
            # Never stdout at INFO: that stream is the per-cycle log, and a
            # request line between two glucose lines breaks the columns.
            log.debug("status page: %s %s", self.address_string(), format % args)

    return Handler


def start_status_page(board, port):
    """Serve the page from a background thread. Returns the server, or None.

    A port that's taken is a warning, not an exit. The page is a window onto
    the light, and a broken window is no reason to switch the light off.
    """
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_status_handler(board))
    except OSError as exc:
        log.warning(
            "Could not start the status page on port %d (%s). The light carries "
            "on without it. Pick another STATUS_PORT, or set it to 0.",
            port, exc.strerror or exc,
        )
        return None
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, name="status-page", daemon=True
    ).start()
    log.info("Status page on http://127.0.0.1:%d/", port)
    return server


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

class Runner:
    def __init__(self, cfg):
        self.cfg = cfg
        self.running = True
        self.bridge = HueBridge(cfg.bridge_ip, cfg.app_key)
        self.bridge.inspect_lights(cfg.light_ids)
        if cfg.named_groups:
            for group in cfg.groups:
                log.info(
                    "Group %s: %d light%s (%s), %g%% max / %g%% min, "
                    "%g%% when urgent",
                    group.name, len(group.light_ids),
                    "" if len(group.light_ids) == 1 else "s",
                    ", ".join(group.light_ids),
                    group.max_brightness, group.min_brightness,
                    group.urgent_level,
                )
        else:
            log.info(
                "Driving %d light%s: %s",
                len(cfg.light_ids), "" if len(cfg.light_ids) == 1 else "s",
                ", ".join(cfg.light_ids),
            )
        for group in cfg.groups:
            floor = self.bridge.dim_floor(group.light_ids)
            if floor is not None and group.min_brightness < floor:
                # Below its own floor a bulb may flicker or simply not light,
                # which would read as "off" and mean something it doesn't.
                log.warning(
                    "Minimum brightness%s is %.1f%% but the dimmest light there "
                    "bottoms out at %.1f%%. Raise it, or the faint end of the "
                    "fade may not show at all.",
                    f' for group "{group.name}"' if cfg.named_groups else "",
                    group.min_brightness, floor,
                )
        self.dexcom = None
        # (value, seconds of age at last evaluation, trend offset). The offset
        # is carried with the reading so a Share hiccup doesn't jump the colour
        # while we age the same reading out.
        self.last_good = None
        self.last_stamp = None  # timestamp of the most recent NEW reading
        # When the reading in last_good was taken. Only the status page needs
        # it, to keep ageing the reading between cycles.
        self.last_good_stamp = None
        # The status page, if STATUS_PORT is set. Each cycle hands it the same
        # facts the log line prints.
        self.board = None

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
            trend = reading.trend_arrow
            stamp = reading_timestamp(reading)
            self.last_good_stamp = stamp
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
            self._report("waiting", fetch_failed=reading is None)
            self.bridge.turn_off(cfg.light_ids)
            return

        value, age, offset = self.last_good
        # The arrow moves the number the light is drawn from, but nothing else:
        # staleness below still tests the real age of the real reading.
        shown = adjusted_value(value, offset)
        # Every field is padded to a fixed width so consecutive lines form
        # columns: scanning a run of them for the one number that changed is
        # the main thing anyone does with this log. Widths cover the extremes
        # (a 3-digit reading, a 2-glyph arrow, a 4-digit age) and quietly
        # stretch rather than truncate beyond them.
        adjustment = " " * 10 if not offset else f"{offset:+3g} -> {shown:<3g}"
        repeat = "        " if is_new else "[repeat]"
        # Per group, because rooms differ in how bright they want to be. The
        # colour below is worked out once: every group is showing the same
        # reading, so they can only differ in level.
        levels = [(group, brightness_for_age(age, group, shown)) for group in cfg.groups]
        facts = {
            "value": value, "trend": trend, "offset": offset, "shown": shown,
            "age": age, "new": is_new, "fetch_failed": reading is None,
            "reading_time": self.last_good_stamp.timestamp(),
        }

        # Staleness is a global setting, so this is all groups or none. Keeping
        # the test on the computed levels rather than on cfg.stale_seconds
        # means the "off means nothing you can trust" rule stays owned by
        # brightness_for_age alone.
        if all(level is None for _, level in levels):
            # We've handled staleness ourselves, so stand the bridge timers
            # down; they'd only repeat the same action a couple of minutes later.
            if cfg.watchdog:
                self.bridge.disarm_watchdog(cfg.light_ids)
            log.info(
                "Glucose %3s %-2s %s | %4.0fs old %s | STALE, lights off",
                value, trend, adjustment, age, repeat,
            )
            self._report("stale", **facts)
            self.bridge.turn_off(cfg.light_ids)
            return

        rgb = glucose_to_rgb(shown, cfg.stops)
        urgent = shown <= cfg.urgent_below
        log.info(
            "Glucose %3s %-2s %s | %4.0fs old %s | %s %-14s | %s%s",
            value, trend, adjustment, age, repeat,
            rgb_to_hex(rgb), f"[{rgb_to_name(rgb)}]",
            format_levels(levels, cfg.group_width if cfg.named_groups else 0),
            "  URGENT LOW" if urgent else "",
        )
        self._report(
            "on", hex=rgb_to_hex(rgb), colour=rgb_to_name(rgb), urgent=urgent,
            levels=[{"group": group.name, "level": level} for group, level in levels],
            **facts,
        )
        xy = rgb_to_xy(*rgb)
        for group, level in levels:
            # A single group that went dark on its own can't happen while
            # staleness is global, but painting from the levels keeps this
            # honest if that ever changes.
            if level is None:
                self.bridge.turn_off(group.light_ids)
            else:
                self.bridge.set_color(group.light_ids, xy, level)

    def _report(self, state, **facts):
        """Hand this cycle to the status page, if there is one.

        Called before the lights are painted, like the log line, so a bridge
        that hangs doesn't hold back the page. `state` is what the lights were
        told: "on", "stale" (off) or "waiting" (off, nothing read yet).
        """
        if self.board is not None:
            self.board.record(dict(facts, state=state, time=time.time()))

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

    # Keeps recent warnings for the status page. It holds them in memory and
    # prints nothing, so it costs nothing when the page is off.
    problems = RecentProblems()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    root.handlers = [to_stdout, to_stderr, problems]

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
        # Annotate with the group each light is already in, so a long list is
        # easier to check against your config. Broken group config must not
        # stop this — listing the ids is how you fix it — so failures here are
        # simply no annotation.
        in_group = {}
        try:
            raw_groups = os.environ.get("HUE_LIGHT_GROUPS")
            if raw_groups:
                for group in prepare_light_groups(
                    json.loads(raw_groups), brightness_from_env()
                ):
                    in_group.update({i: group.name for i in group.light_ids})
        except Exception:
            pass

        for light in HueBridge(ip, key).lights():
            name = light.get("metadata", {}).get("name", "?")
            colour = "colour" if "color" in light else "white only"
            group = in_group.get(light["id"])
            print(f"{light['id']}  {name}  ({colour})"
                  + (f"  [{group}]" if group else ""))
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
            rgb = glucose_to_rgb(current, stops)
            marker = " <- stop" if current in stop_values else ""
            print(f"  {current:8.1f}  {rgb_to_hex(rgb)}  "
                  f"{rgb_to_name(rgb):<12}{marker}".rstrip())
            current += step
        # Stops that the sampling step skipped over still matter; show them.
        for value, _ in stops:
            if not (low <= value <= high):
                rgb = glucose_to_rgb(value, stops)
                print(f"  {value:8.1f}  {rgb_to_hex(rgb)}  {rgb_to_name(rgb):<12}"
                      "(outside preview range)")

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
                rgb = glucose_to_rgb(shown, stops)
                print(f"  {TREND_ARROWS[name]:<3} {name:<14} {label:>6}"
                      f"  -> {shown:7g}  {rgb_to_hex(rgb)}  {rgb_to_name(rgb)}")

        settings = brightness_from_env()
        try:
            validate_brightness(settings)
        except ValueError as exc:
            sys.exit(str(exc))

        # One curve per group, since that's what differs between them. With no
        # groups configured — including no lights at all, which is the state
        # this command is usually run in — there's a single unnamed curve.
        raw_groups = os.environ.get("HUE_LIGHT_GROUPS")
        if raw_groups:
            try:
                groups = prepare_light_groups(json.loads(raw_groups), settings)
            except json.JSONDecodeError as exc:
                sys.exit(f"HUE_LIGHT_GROUPS is not valid JSON: {exc}")
            except ValueError as exc:
                sys.exit(f"Bad HUE_LIGHT_GROUPS: {exc}")
        else:
            groups = [LightGroup(DEFAULT_GROUP_NAME, [], settings)]

        for group in groups:
            label = f" [{group.name}]" if raw_groups else ""
            stale = group.stale_seconds
            print(f"\nBrightness{label}: {group.max_brightness:g}% fresh -> "
                  f"{group.min_brightness:g}% at {stale / 60:g} min, then off\n")
            span = int(stale) + 120
            for age in range(0, span, max(span // 10, 60)):
                level = brightness_for_age(age, group)
                shown = "off" if level is None else f"{level:5.1f}%"
                note = ""
                if level is not None and age <= group.fresh_seconds:
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
        sys.exit(
            "No lights configured. Set HUE_LIGHT_IDS (or HUE_LIGHT_GROUPS, to "
            "give rooms their own brightness). Run --list-lights first."
        )

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

    # Not for --once: the page would be gone before anyone could load it.
    if cfg.status_port and not args.once:
        runner.board = StatusBoard(cfg, problems)
        start_status_page(runner.board, cfg.status_port)

    runner.run(once=args.once)



# --------------------------------------------------------------------------
# The status page's HTML
# --------------------------------------------------------------------------
# Kept down here so it doesn't sit between the code paths above. One string,
# no external files, fonts or scripts: the page has to work on a phone over
# the tailnet with nothing else reachable.
#
# The page follows the light's rule: fail dark. It re-applies STALE_MINUTES
# itself between cycles, greys out when it can't reach this process, and says
# so when the loop has stopped cycling. Every value from the server goes in
# with textContent, never as HTML — warnings quote exception text from Share
# and the bridge.

STATUS_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="robots" content="noindex">
<title>diabetes-light</title>
<link id="icon" rel="icon" href="data:,">
<style>
:root{
  --bg:#111214; --card:#1a1b1f; --ink:#e9e7e2; --soft:#a6a29a; --faint:#6c6962;
  --rule:#2a2c31; --lamp-off:#26282d; --bar:#34373d;
  --note-bg:#2b2214; --note-rule:#5c4a22; --note-ink:#e6cd8f;
  --alert:#ff6b8a;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:light){
:root{
  --bg:#f6f5f2; --card:#fff; --ink:#1a1916; --soft:#55524c; --faint:#8a867e;
  --rule:#e3dfd8; --lamp-off:#dcd9d2; --bar:#e6e3dd;
  --note-bg:#fdf6e8; --note-rule:#e6d4a8; --note-ink:#6b4e16;
  --alert:#b8123a;
}
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 var(--sans);
  -webkit-font-smoothing:antialiased}
.wrap{max-width:780px; margin:0 auto; padding:20px 16px 64px}
header{display:flex; align-items:baseline; justify-content:space-between; gap:12px;
  flex-wrap:wrap; margin-bottom:16px}
h1{font-size:15px; font-weight:600; margin:0; letter-spacing:.02em}
h2{font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:.08em;
  color:var(--soft); margin:32px 0 10px}
.contact{font-size:13px; color:var(--faint)}
.contact.lost{color:var(--alert); font-weight:600}
.hidden{display:none !important}
.num{font-variant-numeric:tabular-nums}

.notes{display:grid; gap:8px; margin-bottom:12px}
.note{padding:10px 14px; background:var(--note-bg); border:1px solid var(--note-rule);
  border-radius:8px; color:var(--note-ink); font-size:14px}

.card{background:var(--card); border:1px solid var(--rule); border-radius:12px; padding:20px}
.hero{display:flex; gap:24px; align-items:center}
.lamp{width:112px; height:112px; border-radius:50%; flex:none; background:var(--lamp-off);
  transition:background-color .6s, box-shadow .6s, filter .6s}
.lamp.dark{box-shadow:inset 0 0 0 2px var(--rule)}
.readout{min-width:0}
.state{font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:.08em;
  color:var(--soft)}
.state .urgent{color:var(--alert)}
.big{display:flex; align-items:baseline; gap:10px; margin:2px 0 4px}
.value{font-size:64px; line-height:1; font-weight:650; letter-spacing:-.02em}
.arrow{font-size:36px; line-height:1; color:var(--soft)}
.dim .value,.dim .arrow{color:var(--faint)}
.sub{color:var(--soft); font-size:14px}
.lost-view .lamp{filter:grayscale(1) brightness(.5)}
.lost-view .value,.lost-view .arrow{color:var(--faint)}
.lost-view .facts{opacity:.45}

dl.facts{display:grid; grid-template-columns:max-content minmax(0,1fr); gap:8px 18px;
  margin:20px 0 0; padding-top:16px; border-top:1px solid var(--rule)}
dl.facts dt{color:var(--faint); font-size:13px; padding-top:1px}
dl.facts dd{margin:0; min-width:0}
.swatch{display:inline-block; width:.9em; height:.9em; border-radius:3px;
  vertical-align:-.1em; margin-right:6px; box-shadow:inset 0 0 0 1px rgba(128,128,128,.35)}
.mono{font-family:var(--mono); font-size:13px}

.levels{display:grid; gap:6px}
.level{display:grid; grid-template-columns:minmax(0,7em) minmax(0,1fr) 3.2em; gap:10px;
  align-items:center}
.level.single{grid-template-columns:minmax(0,1fr) 3.2em}
.level .name{overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--soft)}
.track{height:8px; border-radius:4px; background:var(--bar); overflow:hidden}
.fill{height:100%; border-radius:4px; background:var(--soft); transition:width .6s}
.level .pct{text-align:right}

.fresh{position:relative; margin-top:6px}
.fresh .band{display:flex; height:8px; border-radius:4px; overflow:hidden}
.fresh .band div{height:100%}
.band .b-fresh{background:var(--soft)}
.band .b-fade{background:linear-gradient(to right,var(--soft),var(--bar))}
.band .b-off{background:var(--bar)}
.fresh .mark{position:absolute; top:-4px; width:3px; height:16px; border-radius:2px;
  background:var(--ink); transform:translateX(-50%); transition:left .6s}
.fresh .tick{position:absolute; top:0; width:2px; height:8px; background:var(--card)}
.fresh .legend{margin-top:4px; font-size:12px; color:var(--faint)}

.disclaimer{margin:14px 2px 0; font-size:13px; color:var(--faint)}

.scroll{overflow-x:auto; -webkit-overflow-scrolling:touch; border:1px solid var(--rule);
  border-radius:10px; background:var(--card)}
table{border-collapse:collapse; width:100%; font-size:13px}
th,td{padding:6px 10px; text-align:left; white-space:nowrap; border-bottom:1px solid var(--rule)}
th{font-weight:600; color:var(--faint); font-size:12px; position:sticky; top:0; background:var(--card)}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right}
tr.off td{color:var(--faint)}
td .flag{font-weight:600}
td .flag.urgent{color:var(--alert)}
button{font:inherit; font-size:13px; color:var(--ink); background:var(--card);
  border:1px solid var(--rule); border-radius:8px; padding:6px 12px; margin-top:10px; cursor:pointer}

.problems{list-style:none; margin:0; padding:0; display:grid; gap:6px}
.problems li{padding:8px 12px; border:1px solid var(--rule); border-left:3px solid var(--note-rule);
  border-radius:6px; background:var(--card); font-size:13px; overflow-wrap:anywhere}
.problems li.error{border-left-color:var(--alert)}
.problems .when{color:var(--faint); margin-right:8px}

footer{margin-top:32px; font-size:13px; color:var(--faint)}
footer p{margin:4px 0}

@media (max-width:520px){
  .hero{gap:18px}
  .lamp{width:84px; height:84px}
  .value{font-size:52px}
  .arrow{font-size:30px}
  .card{padding:16px}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>diabetes-light</h1>
    <span id="contact" class="contact">connecting…</span>
  </header>

  <div id="notes" class="notes"></div>

  <section id="view" class="card">
    <div class="hero">
      <div id="lamp" class="lamp dark" role="img" aria-label="light off"></div>
      <div class="readout">
        <div id="state" class="state">Waiting for the first cycle</div>
        <div id="big" class="big dim">
          <span id="value" class="value num">–</span>
          <span id="arrow" class="arrow"></span>
        </div>
        <div id="sub" class="sub"></div>
      </div>
    </div>
    <dl id="facts" class="facts hidden">
      <dt>Reading age</dt>
      <dd>
        <span id="age" class="num"></span> <span id="newness" class="sub"></span>
        <div id="fresh" class="fresh">
          <div class="band"><div class="b-fresh"></div><div class="b-fade"></div><div class="b-off"></div></div>
          <div class="mark"></div>
          <div class="legend"></div>
        </div>
      </dd>
      <dt>Taken</dt><dd id="taken" class="num"></dd>
      <dt>Colour</dt><dd id="colour"></dd>
      <dt>Brightness</dt><dd><div id="levels" class="levels"></div></dd>
    </dl>
  </section>
  <p class="disclaimer">A convenience display, not an alarm. Keep your Dexcom app's alarms on.</p>

  <section id="problems-section" class="hidden">
    <h2>Recent warnings</h2>
    <ul id="problems" class="problems"></ul>
  </section>

  <section id="history-section" class="hidden">
    <h2>Recent cycles</h2>
    <div class="scroll">
      <table>
        <thead><tr id="history-head"></tr></thead>
        <tbody id="history"></tbody>
      </table>
    </div>
    <button id="more" class="hidden" type="button"></button>
  </section>

  <footer id="footer"></footer>
</div>

<script>
"use strict";
const POLL_MS = 10000;
const HISTORY_ROWS = 60;

let boot = null, lastSeq = 0, cycles = [], problems = [], settings = null;
let serverNow = 0, fetchedAt = 0, lastContact = 0, lost = false, showAll = false;

const $ = id => document.getElementById(id);

function el(tag, cls, text){
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function num(v){
  if (v === null || v === undefined) return "";
  return String(Math.round(v * 10) / 10);
}

function signed(v){
  return (v > 0 ? "+" : "−") + num(Math.abs(v));
}

function dur(s){
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(s / 3600) + "h " + String(Math.floor(s / 60) % 60).padStart(2, "0") + "m";
}

function clock(t, seconds){
  const opts = {hour:"2-digit", minute:"2-digit"};
  if (seconds) opts.second = "2-digit";
  return new Date(t * 1000).toLocaleTimeString([], opts);
}

// Server time now, extrapolated from the last response with the browser's own
// monotonic clock, so a phone whose clock is off can't make a reading look
// fresher or older than it is.
function serverClock(){
  return serverNow + (performance.now() - fetchedAt) / 1000;
}

async function poll(){
  try {
    const query = boot === null ? "" : "?after=" + lastSeq;
    const res = await fetch("status.json" + query, {cache:"no-store"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (boot !== null && data.boot !== boot){
      // The script restarted: its sequence numbers start again, and the
      // history it holds is only what it has seen since.
      boot = null; lastSeq = 0; cycles = [];
      return poll();
    }
    boot = data.boot;
    settings = data.settings;
    problems = data.problems;
    const grew = data.cycles.length > 0;
    cycles.push(...data.cycles);
    if (cycles.length > settings.history_size) cycles.splice(0, cycles.length - settings.history_size);
    if (cycles.length) lastSeq = cycles[cycles.length - 1].seq;
    serverNow = data.now;
    fetchedAt = performance.now();
    lastContact = Date.now();
    lost = false;
    if (grew) renderHistory();
    renderProblems();
    renderFooter();
  } catch (err) {
    lost = true;
  }
  renderLive();
}

function levelsOf(c){
  return (c.levels || settings.groups.map(g => ({group:g.name, level:null})));
}

function renderLive(){
  const contact = $("contact");
  const notes = $("notes");
  notes.replaceChildren();

  if (lost){
    contact.textContent = lastContact
      ? "can't reach the script · last contact " + dur((Date.now() - lastContact) / 1000) + " ago"
      : "can't reach the script";
    contact.className = "contact lost";
    notes.append(el("div", "note",
      "This page can't reach diabetes-light, so what it shows is out of date. " +
      "If the script has stopped, the bridge watchdog turns the lights off on its own."));
  } else if (settings) {
    contact.textContent = "live · checked " + dur((Date.now() - lastContact) / 1000) + " ago";
    contact.className = "contact";
  }
  $("view").classList.toggle("lost-view", lost);
  if (!settings) return;

  const c = cycles[cycles.length - 1];
  if (!c){
    $("state").textContent = "Waiting for the first cycle";
    return;
  }

  const sinceCycle = serverClock() - c.time;
  const expected = settings.poll_seconds;
  if (sinceCycle > expected * 2 + 30){
    notes.append(el("div", "note",
      "No cycle for " + dur(sinceCycle) + " (expected every " + dur(expected) + "). " +
      "The script may be stuck." + (settings.watchdog
        ? " The bridge watchdog switches the lights off " + num(settings.watchdog_minutes) +
          " min after the last new reading."
        : "")));
  }

  const age = c.age === undefined ? null : c.age + sinceCycle;
  const staleSeconds = settings.stale_minutes * 60;
  let state = c.state;
  // Same rule as the light: once the reading passes STALE_MINUTES it is not
  // shown as current, even if the next cycle hasn't happened yet.
  if (state === "on" && age >= staleSeconds) state = "stale-now";
  const on = state === "on" && !lost;

  let label;
  if (state === "on") label = "Lights on";
  else if (state === "stale") label = "Stale — lights off";
  else if (state === "stale-now") label = "Stale — lights off at the next cycle";
  else label = "No reading yet — lights off";
  const stateNode = $("state");
  stateNode.replaceChildren(label);
  if (lost) stateNode.prepend("Last known: ");
  if (state === "on" && c.urgent){
    stateNode.append(" · ", el("span", "urgent", "Urgent low"));
  }

  $("big").classList.toggle("dim", state !== "on");
  $("value").textContent = c.value === undefined ? "–" : num(c.value);
  $("arrow").textContent = c.trend || "";

  const sub = [];
  if (c.offset) sub.push("shown as " + num(c.shown) + " (" + signed(c.offset) + " for the arrow)");
  if (c.fetch_failed) sub.push("Share didn't answer this cycle");
  $("sub").textContent = sub.join(" · ");

  const lamp = $("lamp");
  const peak = Math.max(0, ...levelsOf(c).map(l => l.level || 0));
  if (on && c.hex){
    lamp.classList.remove("dark");
    lamp.style.backgroundColor = c.hex;
    lamp.style.filter = "brightness(" + (0.35 + 0.65 * peak / 100).toFixed(2) + ")";
    lamp.style.boxShadow = "0 0 " + Math.round(8 + 40 * peak / 100) + "px " + c.hex;
    lamp.setAttribute("aria-label", "light on, " + c.colour + ", " + Math.round(peak) + "%");
  } else {
    lamp.classList.add("dark");
    lamp.style.backgroundColor = lamp.style.filter = lamp.style.boxShadow = "";
    lamp.setAttribute("aria-label", "light off");
  }

  if (c.value === undefined){
    $("facts").classList.add("hidden");
    document.title = "no reading · diabetes-light";
    setIcon(null);
    return;
  }
  $("facts").classList.remove("hidden");
  $("age").textContent = dur(age) + " old";
  $("newness").textContent = c.new ? "" : "· no new reading last cycle";
  $("taken").textContent = clock(c.reading_time, false);
  renderFreshness(age);

  const colour = $("colour");
  colour.replaceChildren();
  if (c.hex){
    const sw = el("span", "swatch");
    sw.style.backgroundColor = c.hex;
    colour.append(sw, el("span", "mono", c.hex), " " + c.colour);
    if (state !== "on") colour.append(el("span", "sub", " · not showing"));
  } else {
    colour.append(el("span", "sub", "none — lights off"));
  }

  const levels = $("levels");
  levels.replaceChildren();
  for (const l of levelsOf(c)){
    const row = el("div", settings.named_groups ? "level" : "level single");
    if (settings.named_groups) row.append(el("span", "name", l.group));
    const track = el("div", "track");
    const fill = el("div", "fill");
    const level = state === "on" ? l.level : null;
    fill.style.width = (level || 0) + "%";
    track.append(fill);
    row.append(track, el("span", "pct num", level === null ? "off" : Math.round(level) + "%"));
    levels.append(row);
  }

  document.title = (on ? num(c.value) + " " + (c.trend || "") : "off") + " · diabetes-light";
  setIcon(on ? c.hex : null);
}

function renderFreshness(age){
  const fresh = settings.fresh_minutes * 60, stale = settings.stale_minutes * 60;
  const end = settings.watchdog ? Math.max(settings.watchdog_minutes * 60, stale * 1.1) : stale * 1.25;
  const pct = s => (100 * Math.min(s, end) / end);
  const band = $("fresh").querySelectorAll(".band div");
  band[0].style.width = pct(fresh) + "%";
  band[1].style.width = (pct(stale) - pct(fresh)) + "%";
  band[2].style.width = (100 - pct(stale)) + "%";
  const box = $("fresh");
  box.querySelector(".mark").style.left = pct(age) + "%";
  box.querySelectorAll(".tick").forEach(t => t.remove());
  if (settings.watchdog){
    const tick = el("div", "tick");
    tick.style.left = pct(settings.watchdog_minutes * 60) + "%";
    box.append(tick);
  }
  // Labels get a line of their own: placed on the bar, "off 13m" and
  // "watchdog 15m" sit two minutes apart and print on top of each other.
  box.querySelector(".legend").textContent =
    "full to " + num(settings.fresh_minutes) + " min · fades to " +
    num(settings.stale_minutes) + " min, then off" +
    (settings.watchdog ? " · bridge watchdog at " + num(settings.watchdog_minutes) + " min" : "");
}

function renderHistory(){
  if (!settings) return;
  $("history-section").classList.toggle("hidden", cycles.length === 0);
  // One brightness column per group, headed by its name, so a row reads like
  // the log line's NN% name cells without repeating the names on every row.
  const head = $("history-head");
  head.replaceChildren();
  const cols = [["Time"], ["Glucose", "r"], ["Trend"], ["Adjusted"], ["Age", "r"], [""], ["Colour"]];
  for (const g of settings.groups) cols.push([settings.named_groups ? g.name : "Brightness", "r"]);
  cols.push([""]);
  for (const [text, cls] of cols) head.append(el("th", cls, text));
  const body = $("history");
  body.replaceChildren();
  const rows = cycles.slice().reverse();
  const shown = showAll ? rows : rows.slice(0, HISTORY_ROWS);
  for (const c of shown){
    const tr = el("tr", c.state === "on" ? null : "off");
    tr.append(el("td", "num", clock(c.time, true)));
    tr.append(el("td", "r num", c.value === undefined ? "" : num(c.value)));
    tr.append(el("td", null, c.trend || ""));
    tr.append(el("td", "num", c.offset ? signed(c.offset) + " → " + num(c.shown) : ""));
    tr.append(el("td", "r num", c.age === undefined ? "" : dur(c.age)));
    tr.append(el("td", null, c.state === "waiting" ? "" : (c.new ? "new" : "repeat")));
    const colour = el("td");
    if (c.hex){
      const sw = el("span", "swatch");
      sw.style.backgroundColor = c.hex;
      colour.append(sw, el("span", "mono", c.hex), " " + c.colour);
    }
    tr.append(colour);
    for (const l of levelsOf(c)){
      const level = c.state === "on" ? l.level : null;
      tr.append(el("td", "r num", level === null ? "off" : Math.round(level) + "%"));
    }
    const flags = el("td");
    if (c.state === "on" && c.urgent) flags.append(el("span", "flag urgent", "URGENT LOW"));
    if (c.state === "stale") flags.append(el("span", "flag", "STALE"));
    if (c.state === "waiting") flags.append(el("span", "flag", "no reading yet"));
    if (c.fetch_failed) flags.append((flags.childNodes.length ? " · " : "") + "Share fetch failed");
    tr.append(flags);
    body.append(tr);
  }
  const more = $("more");
  more.classList.toggle("hidden", rows.length <= HISTORY_ROWS);
  more.textContent = showAll ? "Show the last " + HISTORY_ROWS : "Show all " + rows.length;
}

function renderProblems(){
  $("problems-section").classList.toggle("hidden", problems.length === 0);
  const list = $("problems");
  list.replaceChildren();
  for (const p of problems.slice().reverse()){
    const li = el("li", p.level === "WARNING" ? null : "error");
    li.append(el("span", "when num", clock(p.time, true)), p.message);
    list.append(li);
  }
}

function renderFooter(){
  const s = settings;
  const parts = [
    "Polls every " + s.poll_seconds + "s",
    "fades after " + num(s.fresh_minutes) + " min",
    "off at " + num(s.stale_minutes) + " min",
    s.watchdog ? "watchdog " + num(s.watchdog_minutes) + " min" : "watchdog off",
    "urgent at " + num(s.urgent_below) + " and under",
    "trend adjustment " + (s.trend_adjust ? "on" : "off"),
  ];
  const footer = $("footer");
  footer.replaceChildren(el("p", null, parts.join(" · ")));
  if (s.named_groups){
    footer.append(el("p", null, s.groups.map(g =>
      g.name + ": " + num(g.max) + "% → " + num(g.min) + "%, " + num(g.urgent_level) + "% urgent"
    ).join(" · ")));
  }
  footer.append(el("p", null, "Running since " + new Date(boot * 1000).toLocaleString() +
    ". History is kept in memory for " + dur(s.history_size * s.poll_seconds) + "."));
}

function setIcon(hex){
  const fill = hex || "#555";
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>" +
    "<circle cx='8' cy='8' r='7' fill='" + fill + "'/></svg>";
  $("icon").href = "data:image/svg+xml," + encodeURIComponent(svg);
}

$("more").addEventListener("click", () => { showAll = !showAll; renderHistory(); });

poll();
setInterval(poll, POLL_MS);
setInterval(renderLive, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
