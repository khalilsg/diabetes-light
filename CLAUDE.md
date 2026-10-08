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
- **Status page (optional, `STATUS_PORT`):** a stdlib `ThreadingHTTPServer` on
  a daemon thread, serving one self-contained HTML page (`STATUS_PAGE`, at the
  bottom of the file) and `/status.json`. `Runner.tick()` hands a
  `StatusBoard` the same facts the log line prints, via `_report()`. HTTPS is
  `tailscale serve` in front of it, never TLS in-process. Its one write is
  `POST /snooze`, which sets a deadline on the Runner's `Snooze`.
- **Failures:** Share and bridge failures go through `Trouble`, which holds
  them back to INFO until they've lasted `WARN_AFTER_MINUTES`.

## Invariants — don't break these

**Fail dark, never bright.** Off means "no reading you can trust". It's the
only meaning it has. Both the staleness path and the watchdog end there.

**`STALE_MINUTES` must stay below `WATCHDOG_MINUTES`.** If the bridge timer
fires mid-fade, the next poll turns the light back on and you get a visible
flicker. `Config` warns when the gap drops under two poll intervals.

**Only advancing timestamps count as new readings.** Share keeps returning the
last reading forever after a sensor session ends. `Runner.last_stamp` guards
this. Re-arming the watchdog on repeat data would defeat the entire mechanism.

**The trend offset moves the number, not the clock.** `TREND_OFFSETS` shifts the
reading before colour and level are picked — including the `URGENT_BELOW`
comparison, so a falling arrow can trigger urgent brightness on an in-range
reading. It must never touch the fade, `STALE_MINUTES` or the watchdog: those
work off the real reading's real timestamp, or an arrow could make old data look
fresh. The log line prints the real reading first, then the shift.

**Staleness outranks urgency.** A low reading pins brightness at
`URGENT_LEVEL`, but once it passes `STALE_MINUTES` the light still goes off. We
don't blaze at 100% on data that might be an hour old.

**Groups differ in level, never in meaning.** `HUE_LIGHT_GROUPS` lets each room
set `max`, `min` and `urgent_level`. Everything else is global on purpose:
colour (one reading described one way), `FRESH_MINUTES`/`STALE_MINUTES` (one
light going dark while another glows on the same dead reading would wreck what
"off" means), and `URGENT_BELOW` (a fact about the person, not the room).
`prepare_light_groups` rejects those keys by name rather than ignoring them.
A light may only be in one group — two groups asking for different brightness
on one bulb has no answer, so it's a startup error.

**The status page reports, it never decides.** It shows what `tick()` sent
and computes no colour or level of its own, so it can't disagree with the
light. It fails dark like the light does: it re-applies `STALE_MINUTES` between
cycles (ageing on the server's clock, not the phone's), greys out when it
loses contact, and flags a loop that has stopped cycling. It binds `127.0.0.1`
only, with no flag to change that, because it serves health data with no auth.
The snooze is the one exception to "read-only", and it only moves a deadline:
what the lights do about it is decided in `tick()`. `POST /snooze` requires
the `X-Diabetes-Light` header (a cross-site page can't send it without a CORS
preflight, which this server never approves) and rejects any `Sec-Fetch-Site`
other than `same-origin`. Keep both checks.

**A snooze is off, for everyone, for a fixed time.** Snoozing says "this reading
is wrong", which is what off already means, so it turns the lights off rather
than inventing a dimmed or tinted look. It is global, like staleness. It does
not end early when the reading recovers, because a wrong sensor hovering
around `URGENT_BELOW` would flicker the light on and off. Staleness still
outranks it (a stale cycle reports "stale", not "snoozed"), the watchdog arms
as usual underneath, and it lives in memory so a restart ends it. It's capped
at `SNOOZE_MAX_MINUTES` whatever asks for it.

**Reading age comes from the clock, never from counting cycles.** A failed
fetch ages the last reading as `now - last_good_stamp`. Adding `POLL_SECONDS`
per cycle undercounted whenever a cycle ran long on timeouts, making old data
look fresher than it was, and a snooze tap now runs cycles off-schedule.
Warnings shown on it have the Hue app key and Dexcom password redacted (a
failed v1 call's URL contains the key). Server values go into the DOM via
`textContent` only. A busy port logs a warning and the lights carry on without
the page.

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
- **`min_dim_level` differs per bulb.** `HueBridge.dim_floor()` takes the
  strictest floor within a group, so nothing is asked to dim below what it can
  do — and a strict bulb in one room doesn't drag a warning onto another.
- **One failing light must not stop the others.** Per-light calls go through
  `HueBridge._each_light`, which catches and reports each one to `Trouble`.
  A `requests.ConnectionError` is the bridge's problem, so it goes under one
  `"bridge"` key, not one per light. Anything else, a `ReadTimeout` included,
  is filed under that light.

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
(`glucose_to_rgb`, `brightness_for_age`, `prepare_stops`, `prepare_light_groups`,
`format_levels`, `_watchdog_body`) rather than trying to run the loop.
`brightness_for_age` takes a `LightGroup`, but anything carrying the same six
brightness attributes works.

For the status page, build a `Runner` with `HueBridge` stubbed out and
`runner.dexcom` / `runner.connect_dexcom` replaced by fakes, attach a
`StatusBoard` (pass it `runner.snooze` so the snooze controls appear), call
`tick()` a few times and `start_status_page()`. Check it at phone width in
both colour schemes, and in the stale, snoozed and lost-contact states.

`Trouble` and `Snooze` both read `time.time()`, so swapping in a fake clock
lets you test a six-hour outage or a snooze expiring without waiting.

## Logging

Routine output to stdout, warnings and errors to stderr, no overlap. An empty
error log is a health signal, so don't log routine things at WARNING.

A failure that fixes itself on the next cycle is routine. Anything that can
fail transiently, like a network call made every cycle, reports to `Trouble`
with `failed()`/`ok()` and must not call `log.warning` itself. The first
failure is INFO, a streak past `WARN_AFTER_MINUTES` is one WARNING, a change
of exception type mid-streak is another, and recovery from a warned streak is
a WARNING with `extra={"resolved": True}`, which the status page shows muted.
One Oct 2026 network outage wrote ~2,000 lines to err.log before this
existed. Config problems found at startup still warn straight away, once.

The status page's HTTP server logs requests at DEBUG only. A request line at
INFO would land on stdout between two cycle lines and break the columns.

The per-cycle line is the primary debugging tool and should stay parseable.
Every field is padded to a fixed width so a run of lines reads as columns and
the eye lands on whichever number changed:

```
Glucose 143 →             |   90s old          | #FFC200 [amber]        |  70%
```

The colour word comes from `rgb_to_name`, which reads the RGB actually being
sent rather than the glucose value — a custom palette gets accurate words, and
the word can never disagree with the hex beside it.

With `HUE_LIGHT_GROUPS` set, `format_levels` turns the brightness field into one
`NN% name` cell per group, padded to the longest group name. Without it the
field is the bare percentage it has always been, so an ungrouped install's log
is unchanged.

## Style

- Comments explain *why*, especially where the code looks arbitrary. Most of
  the odd-looking bits are protecting an invariant above.
- Fail with `sys.exit("plain message")` at startup, never a traceback. Users
  are following a README, not reading Python.
- British or American spelling — the file currently uses "colour" in prose and
  `color` in identifiers. Keep that split.
