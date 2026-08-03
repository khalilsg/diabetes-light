# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

A single-file Python daemon that reads glucose values from Dexcom Share and
drives Philips Hue lights as an ambient display. Colour encodes the reading,
brightness encodes how fresh it is.

**This is a convenience display, never a medical alarm.** Any change that makes
the light look more authoritative than the data behind it is a bug, regardless
of how much nicer it looks. When in doubt, fail dark.

## Layout

```
diabetes_light.py          the whole program — keep it one file
diabetes_light.env         local config, gitignored, never commit
diabetes_light.env.example template for the above
README.md                  end-user setup guide
```

Dependencies are `pydexcom` and `requests`. Don't add more without a real
reason — this runs unattended on someone's home PC for years, and every
dependency is a future breakage.

## Architecture

```
Dexcom sensor -> phone -> Dexcom Share cloud -> this script -> Hue bridge (LAN)
```

- **Dexcom side:** `pydexcom` polls Share every `POLL_SECONDS`. Share is an
  undocumented API; treat every call as able to fail or change shape.
- **Hue side:** CLIP v2 over HTTPS on the LAN, self-signed cert, no cloud.
  The watchdog uses the legacy **v1** API because v2 has no equivalent to
  timer schedules.

## Invariants — don't break these

**Fail dark, never bright.** Off means "no reading you can trust". It's the
only meaning it has. Both the staleness path and the watchdog end there.

**`STALE_MINUTES` must stay below `WATCHDOG_MINUTES`.** If the bridge timer
fires mid-fade, the next poll turns the light back on and you get a visible
flicker. `Config` warns when the gap drops under two poll intervals.

**Only advancing timestamps count as new readings.** Share keeps returning the
last reading forever after a sensor session ends. `Runner.last_stamp` guards
this. Re-arming the watchdog on repeat data would defeat the entire mechanism.

**Staleness outranks urgency.** A low reading pins brightness at
`URGENT_LEVEL`, but once it passes `STALE_MINUTES` the light still goes off. We
don't blaze at 100% on data that might be an hour old.

**No green in the default palette.** Green reads as "fine" to anyone who has
used a CGM app. The high side deliberately routes through cream and white to
reach cyan rather than taking the shorter path through green.

**Config in the env file beats config in the code.** Users are told their
palette and settings survive a script update. Every constant at the top of the
file must have a matching env override.

## Landmines

These have all bitten before:

- **Hue durations are `PT[hh]:[mm]:[ss]`.** `PT15:00:00` is fifteen *hours*.
  A wrong watchdog duration fails silently — it just never fires.
- **HSV blending takes the shortest arc.** Red to blue goes through magenta,
  not green. Saturation 0 has no hue, so blends borrow from the other end —
  that's how the palette dodges green.
- **`pydexcom`'s constructor changed at 0.4.1.** 0.4.0 and earlier take
  `ous: bool`; 0.4.1+ takes `region`. `connect_dexcom()` inspects the signature
  at runtime. Don't "simplify" it away — Python 3.8 users are pinned to 0.4.0.
- **The env parser strips `# comments` only after whitespace,** so a `#` inside
  a password survives. Quoted values are taken verbatim.
- **`min_dim_level` differs per bulb.** With multiple lights we use the
  strictest floor so nothing is asked to dim below what it can do.
- **One failing light must not stop the others.** Per-light calls catch and log.

## Testing

No test suite. There's no way to fake a bridge or a Share account without
mocking half the world, so verification is manual:

```bash
python diabetes_light.py --preview            # colour ramp + brightness curve
python diabetes_light.py --preview 40:300:10  # custom range
python diabetes_light.py --once --verbose     # one real cycle
python diabetes_light.py --list-lights        # bridge connectivity
```

`--preview` needs no credentials and no bridge, so it's the right smoke test
for anything touching colour or brightness maths. **Always run it after
changing the palette** and eyeball the hex values — check nothing drifts
through green on the high side.

For logic changes, import the module and call the pure functions directly
(`glucose_to_rgb`, `brightness_for_age`, `prepare_stops`, `_watchdog_body`)
rather than trying to run the loop.

## Logging

Routine output to stdout, warnings and errors to stderr, no overlap. An empty
error log is a health signal, so don't log routine things at WARNING.

The per-cycle line is the primary debugging tool and should stay parseable:

```
Glucose 143 → | 90s old | #FFC200 | 70%
```

## Style

- Comments explain *why*, especially where the code looks arbitrary. Most of
  the odd-looking bits are protecting an invariant above.
- Fail with `sys.exit("plain message")` at startup, never a traceback. Users
  are following a README, not reading Python.
- British or American spelling — the file currently uses "colour" in prose and
  `color` in identifiers. Keep that split.
