# diabetes-light

An ambient light that tracks a Dexcom G7 continuous glucose monitor. Colour
shows the reading, brightness shows how fresh it is, and the light goes dark
whenever there's nothing trustworthy to show.

Runs as a small Python service on any always-on machine, talking to Philips Hue
bulbs over your LAN.

> ### Not a medical device
>
> This is a convenience display. **It is not an alarm and must not be used as
> one.** Keep your Dexcom app's alarms switched on.
>
> The data arrives minutes late, passes through two clouds and a home network,
> and the light can freeze on a stale reading for up to 15 minutes without any
> indication. Every design decision here assumes you have a real CGM app doing
> the real job. Provided as-is, with no warranty — see LICENSE.

### Never commit your config

`diabetes_light.env` holds your Dexcom password and Hue application key in
plain text. It's in `.gitignore`. Check `git status` before your first commit
and confirm it isn't listed.

---

## Before you start

- **A Hue Bridge, with the Go paired to it.** Controlling the Go over Bluetooth
  from the phone app isn't enough — without a bridge there's no local API to
  talk to. It needs to be either the square **Bridge v2** (released October
  2015) on firmware 1948086000 or newer, or the newer black **Bridge Pro**
  (2025). The original round v1 bridge doesn't speak the CLIP v2 API this
  script uses.
- **Python 3.9+** on a machine that stays awake.
- **Dexcom Share enabled** with at least one follower (see step 1).

## 1. Turn on Dexcom Share

In the Dexcom G7 app: **Connections → Share**, and invite a follower.

Share is dormant until at least one follower exists, so this step is not
optional even if nobody actually needs to watch your data. If you don't have
anyone to share with, invite a second email address you control and accept the
invite yourself in the Dexcom Follow app.

Then confirm the credentials work by logging in at:

- US: <https://uam1.dexcom.com>
- Outside US: <https://uam2.dexcom.com>

Two things to get right here:

- **Use your Dexcom *username*, not your email address.** These are usually
  different, and the email often won't authenticate even though it works fine
  for signing into the Dexcom app. If you don't know your username, it's shown
  in the Dexcom app under your account details.
- **Use the *wearer's* account, not a follower's.**

The Share API reports almost every failure as "invalid password" — wrong
username, wrong region, Share inactive, all of it. Verifying at the link above
before you touch any config saves real time.

## 2. Install

```bash
mkdir diabetes-light && cd diabetes-light
python3 -m venv venv
venv/bin/pip install pydexcom requests
```

On Windows:

```
mkdir diabetes-light && cd diabetes-light
python -m venv venv
venv\Scripts\pip install pydexcom requests
```

The version floor matters. pydexcom's constructor changed at **0.4.1**: 0.4.0
and earlier take an `ous` boolean, 0.4.1 and later take `region`. The script
detects which one it's talking to, so either works — but the older interface
has no Japan region.

If `pip install -U pydexcom` refuses to go past 0.4.0, your Python is too old:
**0.4.1 onwards requires Python 3.9+**. Check with `python --version` and
upgrade Python if you want the current library.

**Every command from here on** shows the Linux/macOS path, `venv/bin/python`.
On Windows the equivalent is `venv\Scripts\python.exe` — same arguments, same
behaviour, just a different path to the interpreter.

## 2a. Get the script

**Download `diabetes_light.py` and save it into the `diabetes-light` folder you
just created**, alongside the `venv` directory. Nothing below this point works
until the file is there.

Your folder should look like this:

```
diabetes-light/
├── venv/
└── diabetes_light.py
```

Keep the filename as `diabetes_light.py` — the service definitions in step 7
refer to it by name.

Check that it landed and runs. This command needs no credentials and no bridge,
so it's a clean test of the file itself:

```bash
venv/bin/python diabetes_light.py --preview
```

You should see the default colour stops and the brightness ramp printed. If you
get `No such file or directory`, the script isn't in this folder. If you get
`pydexcom not installed`, you're using your system Python rather than the venv —
check the `venv/bin/` (or `venv\Scripts\`) prefix.

## 3. Pair with the bridge

Press the physical link button on top of the bridge, then within 30 seconds:

```bash
venv/bin/python diabetes_light.py --pair          # Linux / macOS
venv\Scripts\python.exe diabetes_light.py --pair  # Windows
```

It waits for you to press Enter after the link button, so run it from a terminal
you can type into — not by double-clicking the file.

It prints a `HUE_BRIDGE_IP` and a `HUE_APP_KEY`. That key is a credential —
anyone with it can control your lights. Keep it out of version control.

## 4. Find the Hue Go

First save the two values from the previous step into `diabetes_light.env`,
next to the script:

```
HUE_BRIDGE_IP=192.168.1.50
HUE_APP_KEY=...
```

The script reads that file automatically, which avoids setting environment
variables inline — the `VAR=value command` prefix works in bash but not in
Windows `cmd` or PowerShell.

```bash
venv/bin/python diabetes_light.py --list-lights
```

Copy the id of the Go. It'll be a UUID, and it should say `(colour)`.

You can drive **more than one light** — list the ids comma-separated in
`HUE_LIGHT_IDS` and they'll all show the same colour and brightness together.
Useful if you want one in the bedroom and one in the kitchen.

## 5. Configure

Copy `diabetes_light.env.example` to `diabetes_light.env` and fill it out —
you already started it in step 4:

```
DEXCOM_USERNAME=you@example.com
DEXCOM_PASSWORD=...
DEXCOM_REGION=us

HUE_BRIDGE_IP=192.168.1.50
HUE_APP_KEY=...
HUE_LIGHT_IDS=...          # one id, or several separated by commas

# Optional — see sections 5a and 5b below
MAX_BRIGHTNESS=70
MIN_BRIGHTNESS=10
FRESH_MINUTES=6
STALE_MINUTES=13    # must stay below WATCHDOG_MINUTES
WATCHDOG=1
WATCHDOG_MINUTES=15
```

On Linux: `chmod 600 diabetes_light.env`. On Windows, restrict it to your user in
the file's Security tab. It holds your Dexcom password in plain text.

### Comments and quoting in the env file

Comments work, both on their own line and at the end of a value:

```
STALE_MINUTES=13   # must stay below WATCHDOG_MINUTES
```

Anything after whitespace-then-`#` is discarded. A `#` with no space in front of
it is kept, so `p@ss#word` works unquoted. But if your password has a space
before a `#`, or you want to preserve trailing spaces, wrap it in quotes:

```
DEXCOM_PASSWORD="my pass # with hash"
```

Quoted values are taken verbatim — no comment stripping inside them.

## 5a. Customising the colours

The palette lives in a `COLOR_STOPS` table at the top of `diabetes_light.py`:

```python
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
```

**The design idea:** warm colours in the middle, harsh ones at the edges. Reds
and ambers are comfortable to have glowing in a room and won't wreck your night
vision, so in-range readings stay visually quiet. The edges break out of that
warm family into magenta and cyan — colours that look wrong in a domestic space,
so they catch your eye without needing to be bright.

Two deliberate choices worth knowing:

- **No green anywhere.** Green means "fine" to anyone who has looked at a CGM
  app, and this palette reserves *calm* for in-range rather than *green*. The
  high side climbs through cream and white to reach cyan specifically so it
  never passes through green on the way.
- **White appears around 200.** That's the washed-out midpoint between gold and
  cyan — a deliberately uneasy colour for a reading that's heading somewhere you
  don't want.

Each entry pairs a glucose value with a hex colour, and the light blends
smoothly between neighbours — 85 lands part-way between the 70 and 100 colours.

- **Any number of stops.** Two, or twenty. Add one anywhere.
- **Order doesn't matter.** They're sorted for you.
- **Below the first stop** shows the first colour; above the last, the last.
- **Short hex works** (`#F80`), as does long (`#FF8800`).

### Preview before committing

```bash
python diabetes_light.py --preview
python diabetes_light.py --preview 40:300:10    # LOW:HIGH:STEP
```

Prints the whole ramp as hex without touching the light. Paste a value into any
colour picker to see it.

### One thing that will surprise you

Blending takes the **shortest way round the colour wheel**. Red to blue with no
stops between goes through magenta, not through green — magenta is the shorter
route. If you want a specific path, add a stop on it:

```python
COLOR_STOPS = [(70, "#FF0000"), (120, "#00FF00"), (180, "#0000FF")]
```

### Keep your palette out of the script

Better than editing the file: set `COLOR_STOPS` in `diabetes_light.env` as JSON.
It overrides the table in the code, so dropping in an updated script won't
overwrite your colours.

```
COLOR_STOPS=[[55,"#FF00A8"],[70,"#FF0033"],[90,"#FF4D00"],[115,"#FF9500"],[145,"#FFC400"],[175,"#FFE0A0"],[200,"#FFFFFF"],[250,"#00D0FF"]]
```

That's the default palette written as JSON — a starting point to edit rather
than something you need to retype. Must be one line, with double quotes, since
it's parsed as JSON.

### mmol/L

Just use mmol values in the stops. Here's the same default palette converted:

```
COLOR_STOPS=[[3.1,"#FF00A8"],[3.9,"#FF0033"],[5.0,"#FF4D00"],[6.4,"#FF9500"],[8.0,"#FFC400"],[9.7,"#FFE0A0"],[11.1,"#FFFFFF"],[13.9,"#00D0FF"]]
```

Two other things need converting with it:

- `URGENT_BELOW=3.9` — it's a glucose reading too, and defaults to the mg/dL 70.
- `reading.value` becomes `reading.mmol_l` in the script. That's the one place
  the units are actually baked in.

## 5b. Setting the brightness

Brightness is a **second, independent channel**. Colour tells you the number;
brightness tells you how fresh it is. Both live at the top of the script:

```python
BRIGHTNESS = {
    "max": 70,             # % while the reading is fresh
    "min": 10,             # % just before the reading is declared stale
    "fresh_minutes": 6,    # full brightness up to here
    "stale_minutes": 13,   # light off past here
    "urgent_below": 70,    # at or under this reading, override the fade
    "urgent_level": 100,   # % to use when that happens
}
```

Brightness is 1-100, the same scale as the slider in the Hue app. **Both timings
are in minutes**, matching `WATCHDOG_MINUTES`. Fractions are fine (`2.5`).

A fresh reading shows at `max`. Past `fresh_minutes` it fades linearly toward
`min`, and past `stale_minutes` it goes out. So a bright light is a reading you
can act on, and a dim one is a reading you should double-check on your phone.

### Picking a number

| Where it lives | Try |
|---|---|
| Bedside, overnight | `max` 20-40 |
| Desk, same room | `max` 50-70 |
| Across a room, daylight | `max` 80-100 |

`max` 100 is bright enough to read by. The default of 70 is a compromise; move
it once you've seen the Go in its actual spot.

Keep `min` above your bulb's floor. The Hue Go won't dim below a few percent —
it'll flicker or just not light, which looks like "off" and would mean the wrong
thing. The script reads the light's `min_dim_level` at startup and warns you if
`min` is set below it.

### Preview the curve

```bash
python diabetes_light.py --preview
```

The same command that shows colours also prints the brightness ramp:

```
Brightness: 70% fresh -> 10% at 13 min, then off

    0m 00s   70.0%  (fresh)
    3m 00s   70.0%  (fresh)
    6m 00s   70.0%  (fresh)
    7m 30s   57.1%
    9m 00s   44.3%
   10m 30s   31.4%
   12m 00s   18.6%
   13m 30s  off  (stale)
```

Beats waiting thirteen minutes to see what the fade looks like.

### Urgent lows go to full brightness

A reading at or below `URGENT_BELOW` (default 70) ignores the freshness fade
entirely and pins the light at `URGENT_LEVEL` (default 100%). A low shouldn't be
something you have to squint at.

The trade-off is real and worth understanding: **you lose the freshness signal
on lows.** A low reading looks identical at one minute old and at twelve. It
goes from full brightness straight to off when it hits `STALE_MINUTES` — no
fade in between to warn you the data is ageing.

Staleness still wins over urgency, deliberately. A low we can no longer verify
gets turned off rather than left blazing at 100% on data that might be twenty
minutes stale.

If you'd rather keep some fade on lows, set `URGENT_LEVEL` to something below
100 — the fade still doesn't apply, but at least the level is a choice. There's
no equivalent setting for highs; if you want one, the check is a single line in
`brightness_for_age`.

### In the env file

Any of these can be overridden, and as with the colours this is the better
place for them — updates won't touch it:

```
MAX_BRIGHTNESS=35
MIN_BRIGHTNESS=8
FRESH_MINUTES=6
STALE_MINUTES=13
URGENT_BELOW=70
URGENT_LEVEL=100
```

The script refuses to start on values outside 1-100, a `min` above `max`, or a
`fresh` at or beyond `stale`. If you change `STALE_MINUTES`, keep it comfortably
below `WATCHDOG_MINUTES` — see section 8.

`FRESH_SECONDS` and `STALE_SECONDS` were the old names. If either is still in
your env file the script stops with a message telling you what to rename.

## 6. Test it

```bash
venv/bin/python diabetes_light.py --once --verbose
```

(Windows: `venv\Scripts\python.exe diabetes_light.py --once --verbose`)

The Go should light up. Then run it in the foreground for a few minutes and
watch the log before you make it a service.

## 7. Run it as a service

### Linux (systemd)

`/etc/systemd/system/diabetes-light.service`:

```ini
[Unit]
Description=CGM ambient light
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=diabetes-light
WorkingDirectory=/opt/diabetes-light
ExecStart=/opt/diabetes-light/venv/bin/python /opt/diabetes-light/diabetes_light.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now diabetes-light
journalctl -u diabetes-light -f
```

### Two log streams

Routine glucose updates go to **stdout**. Warnings and errors go to **stderr**.
Nothing appears on both.

That means `err.log` staying empty is itself a health signal — if it has content,
something needs attention, and you don't have to read past a thousand normal
lines to find it.

With systemd, journald keeps them separate already:

```bash
journalctl -u diabetes-light -p warning    # problems only
journalctl -u diabetes-light -f            # everything, live
```

To write them to files instead, add to the `[Service]` block:

```ini
StandardOutput=append:/var/log/diabetes-light/out.log
StandardError=append:/var/log/diabetes-light/err.log
```

(Create that directory first and make it writable by the service user.) With
NSSM, `AppStdout` and `AppStderr` are already pointed at separate files above.

### Windows (NSSM)

```
nssm install diabetes-light C:\diabetes-light\venv\Scripts\python.exe C:\diabetes-light\diabetes_light.py
nssm set diabetes-light AppDirectory C:\diabetes-light
nssm set diabetes-light AppStdout C:\diabetes-light\out.log
nssm set diabetes-light AppStderr C:\diabetes-light\err.log
nssm set diabetes-light AppRotateFiles 1
nssm start diabetes-light
```

Then set the power plan's sleep to **Never** (display sleep is fine — leave it).

Task Scheduler works too: trigger "At startup", "Run whether user is logged on
or not". NSSM gives you proper restart-on-failure, which Task Scheduler doesn't.

### macOS (launchd)

`/Library/LaunchDaemons/com.diabetes-light.plist` — a **LaunchDaemon**, not a
LaunchAgent. Agents live in `~/Library` and only run while you're logged in,
which is exactly the failure you're trying to avoid.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>              <string>com.diabetes-light</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/projects/diabetes-light/venv/bin/python</string>
        <string>/Users/YOU/projects/diabetes-light/diabetes_light.py</string>
    </array>
    <key>WorkingDirectory</key>   <string>/Users/YOU/projects/diabetes-light</string>
    <key>RunAtLoad</key>          <true/>
    <key>KeepAlive</key>          <true/>
    <key>ThrottleInterval</key>   <integer>30</integer>
    <key>StandardOutPath</key>    <string>/Users/YOU/projects/diabetes-light/out.log</string>
    <key>StandardErrorPath</key>  <string>/Users/YOU/projects/diabetes-light/err.log</string>
</dict>
</plist>
```

```bash
sudo chown root:wheel /Library/LaunchDaemons/com.diabetes-light.plist
sudo chmod 644 /Library/LaunchDaemons/com.diabetes-light.plist
sudo launchctl load -w /Library/LaunchDaemons/com.diabetes-light.plist

sudo launchctl list | grep diabetes-light    # status
tail -f out.log                              # live log
sudo launchctl unload /Library/LaunchDaemons/com.diabetes-light.plist
```

Use absolute paths throughout — launchd has almost no environment, and a
relative path will fail in ways the error message won't explain.

Stop the machine sleeping:

```bash
sudo pmset -a sleep 0          # system sleep off
sudo pmset -a displaysleep 10  # screen can still sleep, that's fine
pmset -g                       # check what's actually set
```

**A laptop is a poor host for this.** Closing the lid sleeps it regardless of
`pmset`, unless it's on power with an external display attached. If this is a
MacBook you carry around, it also leaves the network whenever you do. A Mac mini
or an always-docked machine is fine; anything you pick up is not.

**Test a real reboot.** This is the step people assume works and don't check.

## 8. The kill switch

On by default (`WATCHDOG=1`).

Every time a **new** reading arrives, the script resets a 15-minute timer that
lives on the bridge itself — one per configured light. While readings keep coming, the timer never
expires. If it does expire, the bridge turns the lights off — on its own, with
no involvement from your PC.

That covers two different failures with one mechanism:

- **The script or the PC died.** Crash, reboot, power cut, network loss. Nothing
  resets the timer, so it fires.
- **The script is fine but Dexcom stopped.** Sensor ended, phone off, Share
  outage. Also fires, because only new readings re-arm it.

"New" means the reading's timestamp actually advanced. Share keeps returning
the last reading indefinitely after a sensor stops, so counting every poll as a
reading would re-arm the timer forever on dead data.

### Why STALE_MINUTES is 13

The script's own cutoff (`STALE_MINUTES`, 13) deliberately lands *before* the
watchdog window (`WATCHDOG_MINUTES`, 15). If both fired at once you'd get a
race: the script turns the light off, the bridge timer turns it back on dim
white, and which one wins is luck. With the margin, the ordering is deterministic:

- Script alive → light goes **off** at 13 minutes, and the script disarms the
  timer so it never fires.
- Script gone → nothing disarms anything, and the timer fires at 15 minutes →
  light goes **off**.

Both paths end dark, deliberately. **Off has exactly one meaning: there is no
reading you can trust.** You never have to work out which kind of failure a
colour is describing — if it's dark, don't rely on it, check your phone. Which
of the two turned it off is a question for the logs.

The gap still matters, though for a different reason now. If `STALE_MINUTES`
ran past `WATCHDOG_MINUTES`, the bridge would switch the lights off mid-fade and
the next poll would switch them back on — a visible flicker. Keep
`STALE_MINUTES` below `WATCHDOG_MINUTES`; the script warns at startup if the gap
gets too small.

### Caveats

Uses the legacy Hue v1 API. Signify has said v1 will eventually be removed, so
check on this occasionally. You can inspect the schedule at
`https://<bridge-ip>/debug/clip.html` if you want to see it counting down.

The schedules persist on the bridge across restarts by design. With several
lights you get one schedule per light, named `diabetes-light-wd-<id>`. If you
ever uninstall this, delete them all — otherwise they'll each fire once and
switch off a light you were using for something else.

---

## 9. Updating the code

Everything that's yours lives in `diabetes_light.env`. Everything that's mine
lives in `diabetes_light.py`. Keep it that way and updating is a file swap.

### The procedure

```bash
# 1. Stop the service
sudo systemctl stop diabetes-light          # Linux
nssm stop diabetes-light                    # Windows

# 2. Keep a copy you can fall back to
cp diabetes_light.py diabetes_light.py.bak

# 3. Drop in the new script (leave diabetes_light.env alone)

# 4. Refresh dependencies — the Share API shifts and pydexcom follows it
venv/bin/pip install -U pydexcom requests

# 5. Check the palette survived
venv/bin/python diabetes_light.py --preview

# 6. Test one real cycle in the foreground
venv/bin/python diabetes_light.py --once --verbose

# 7. Start it again and watch the first few cycles
sudo systemctl start diabetes-light
journalctl -u diabetes-light -f
```

Don't skip 6. A single foreground run catches a broken config or a changed
dependency immediately, instead of leaving a service that restart-loops quietly
while the light sits frozen on its last colour.

### If you edited COLOR_STOPS inside the script

A new script overwrites it. Before updating, move your palette into
`diabetes_light.env` as a JSON `COLOR_STOPS` line (see 5a) — it takes precedence
over the table in the code, and then updates can never clobber it again.

### Expect the watchdog to fire

The bridge timer keeps counting during the update. If you take more than 15
minutes, it'll expire and switch the Go off. That's it working correctly —
nothing was resetting it, because nothing was running. The next reading after
you restart re-arms it and normal colour returns.

If you're deliberately down for a while, disable the schedule at
`https://<bridge-ip>/debug/clip.html` rather than leaving a light on.

### If an update goes wrong

```bash
cp diabetes_light.py.bak diabetes_light.py
sudo systemctl start diabetes-light
```

Your `.env` was never touched, so rolling back the script is the whole rollback.

### Worth checking periodically

- Whether the brightness still suits the room (`--preview` to check).
- `pip install -U pydexcom` — the Share API changes and this is where fixes land.
- The `diabetes-light-wd-*` schedules still exist on the bridge (one per light),
  and still use an API Signify has said it intends to retire.
- Whether the light has been quietly stuck. Glance at
  `journalctl -u diabetes-light --since "1 hour ago"` now and then.

---

## 10. Moving to a different computer

Only two files are yours: `diabetes_light.py` and `diabetes_light.env`. Nothing
about this install is tied to a particular machine.

Specifically, you do **not** need to redo any of this:

- **The Hue application key** is bound to the bridge, not the host. Copy it
  across in the `.env` and it keeps working.
- **The watchdog schedule** already lives on the bridge. The new machine finds
  it by name and reuses it — don't create a second one.
- **Dexcom Share** has no per-device registration. Nothing to re-authorise.

### The move

```bash
# 1. On the OLD machine — stop it first, and mean it
sudo systemctl disable --now diabetes-light      # Linux
nssm stop diabetes-light && nssm remove diabetes-light confirm   # Windows
sudo launchctl unload /Library/LaunchDaemons/com.diabetes-light.plist   # macOS

# 2. Copy diabetes_light.py and diabetes_light.env to the new machine.
#    Do NOT copy the venv directory — it contains absolute paths and
#    platform-specific binaries. Build a fresh one instead.

# 3. On the NEW machine
python3 -m venv venv
venv/bin/pip install pydexcom requests
chmod 600 diabetes_light.env

# 4. Confirm your settings came across intact
venv/bin/python diabetes_light.py --preview

# 5. One real cycle, in the foreground
venv/bin/python diabetes_light.py --once --verbose

# 6. Install the service (section 7), then reboot and confirm it came back
```

**Stop the old machine before starting the new one.** Two copies pointing at the
same light will fight over it, and both will reset the same watchdog timer, so
you'd get a light flickering between two nearly-identical states and a kill
switch that can no longer tell you if either of them died.

### Things that catch people out

- **Different subnet or VLAN.** The new machine has to reach the bridge on the
  LAN. If `HUE_BRIDGE_IP` doesn't respond, that's the first thing to check.
- **Windows to Linux.** Save the `.env` with Unix line endings, or a trailing
  `\r` ends up inside your password.
- **The bridge's IP may have changed** while you were moving. Re-run `--pair`'s
  discovery, or set a DHCP reservation so it stops moving.

### Cleaning up afterwards

Optional, but tidy: in the Hue app or at `https://<bridge-ip>/debug/clip.html`,
delete the old machine's application key. Every key is a permanent credential
for controlling your lights, and there's no reason to leave a retired one live.

## 11. Restarts, outages, and recovery

### The short version

If the service is installed correctly, a reboot needs nothing from you. The
service starts at boot, the first poll lands within `POLL_SECONDS`, and the
light returns to normal. Everything below is about the gap in between.

### What the light does while you're down

Nothing, and that's the honest problem. A Hue bulb holds its last colour
indefinitely. So for the first 15 minutes of any outage the Go keeps showing the
last reading, at the brightness it had when the script stopped.

| Elapsed | What you see | What it means |
|---|---|---|
| 0-15 min | Last colour, unchanged | **Looks fresh but isn't.** The known gap. |
| 15 min+ | Off | Watchdog fired. Nothing is running. |

That first window is the reason the watchdog exists, and the reason you keep
your Dexcom alarms on. The light cannot tell you it's lying.

### By failure type

- **Planned reboot.** Service restarts itself. If it takes under 15 minutes you
  may see nothing at all — including no indication that the colour went stale.
- **Power loss to the PC.** Same, plus the watchdog fires at 15 minutes.
- **Network drops.** The script can't reach Dexcom *or* the bridge, so it can't
  turn the light off. The bridge timer handles it: off at 15 minutes.
- **Dexcom outage, script fine.** The script ages the reading out and turns the
  light **off** at 13 minutes, then disarms the watchdog.
- **Sensor change or warmup.** Same as above: light goes off for the warmup
  period, then comes back on its own. Not a fault.
- **Bridge reboots or takes a firmware update.** Schedules survive. The script
  logs connection errors and retries; no action needed.
- **You changed your Dexcom password.** Update `diabetes_light.env` and restart
  the service. Nothing else picks it up.

### Power loss to the Hue Go itself

Worth setting up before it happens. If the Go is switched off at the wall or
runs its battery flat, it comes back in whatever the Hue app's **power-on
behaviour** says — by default, warm white at full brightness. That's a bright
lamp sitting there looking like it means something.

In the Hue app, set that light's power-on behaviour to **"Last state"** or
**"Off"**. Then a power blip leaves it dark until the script paints it again,
instead of leaving a confident-looking white light in the corner.

### After any restart, check it actually came back

```bash
systemctl status diabetes-light                        # Linux
journalctl -u diabetes-light --since "10 minutes ago"

nssm status diabetes-light                             # Windows

sudo launchctl list | grep diabetes-light              # macOS
tail -20 out.log
```

You want a recent `Glucose NNN` line. Each update logs what it calculated:

```
Glucose 62 ↓ | 45s old | #FF001F | 100%  URGENT LOW
Glucose 143 → | 90s old | #FFC200 | 70%
Glucose 118 → | 400s old [repeat] | #FF9B00 | 52%
```

Reading, trend, age, the exact hex colour and brightness sent to the bridge, and
`[repeat]` when Share handed back a reading it had already given us. If the light
looks wrong, this line tells you whether the script computed the wrong colour or
the bridge ignored a correct one. A service that's up but restart-looping
looks identical from the outside to one that's working, so read the log rather
than trusting the status.

### If it doesn't come back

```bash
sudo systemctl is-enabled diabetes-light    # should say "enabled"
sudo systemctl enable diabetes-light        # if it doesn't
```

On Windows, confirm the service Startup Type is **Automatic**, not Manual. This
is the single most common reason a working setup quietly stops surviving
reboots — and Windows Update will reboot that machine whether you planned it or
not.

---

## Reading the light

| What you see | What it means |
|---|---|
| Magenta | Urgent low (55 and below) |
| Red → orange → amber → gold | Rising through the in-range band |
| Cream → white | Getting high (175-200) |
| Cyan | Urgent high (250 and above) |
| Full brightness, no fade | Reading at or below `URGENT_BELOW` |
| Dimming | Data is getting old (past 6 min), fading 70% -> 10% |
| Off | No reading you can trust — stale data, stopped readings, or the script itself is gone |

## When it breaks

- **"Invalid password"** — you're probably using your email instead of your
  Dexcom username. Also check the region flag. Confirm at uam1/uam2; the Share
  API says "invalid password" for nearly every kind of failure.
- **"got an unexpected keyword argument 'region'"** — pydexcom 0.4.0 or older,
  which takes `ous` instead. Current versions of this script detect that and
  adapt, so if you're still seeing it, you're running an older copy of
  `diabetes_light.py` — re-download it. Note `pip install -U pydexcom` stops at
  0.4.0 on Python 3.8 and below, since 0.4.1 requires 3.9+.
- **Light stops responding** — the Go's physical button starts a built-in
  effect. The script clears effects on write, so it should recover next cycle.
- **Light unreachable** — the Go runs on battery when unplugged and eventually
  dies. Keep it plugged in. With several lights configured, one unreachable
  light logs a warning to stderr and the others carry on.
- **Wrong colours after a while** — check the PC's clock and timezone. Reading
  age is computed against local time.
- **Everything stops after an update** — Windows rebooted. Verify the service
  actually came back; this is the most common real-world failure.
