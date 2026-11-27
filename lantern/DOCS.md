# 🏮 Lantern

Lights that follow presence and darkness, learned from your house. Open it from the **Lantern** sidebar panel.

**Echo** remembers what the house did, **Luxie** learns what goes together, **Flint** lights it.

## First start

- Starts in **dry run**: it computes and logs what it would do, but never touches your bulbs. Leave it on
  for the first week while Lantern records data, and turn it off on the Configuration tab when you're happy.
- Your house is set up on the **Setup** tab: add rooms (from a Home Assistant area or blank), add zones, and attach
  presence sensors, a lux sensor, media devices and lights to the room or a zone. The setup is stored in Lantern's
  own database; there are no config files to edit. (An install from before v0.10 has its old room files imported
  once, then they are ignored.)
- The **Sensors** tab lists every light, presence, lux and media entity and where it is attached. Lux sensors can be
  given a profile (a preset plus a little maths) that turns the raw reading into a clean relative brightness.
  This happens inside Lantern; nothing is written to Home Assistant.
- Home Assistant access is automatic (Supervisor token); no URL or token needed.

## Options

| Option | What |
|---|---|
| `dry_run` | `true` = log only, `false` = drive the lights |
| `tick_seconds` | How often rooms are re-evaluated |

## One controller per light

Take the lights Flint drives out of Magic Areas, Adaptive Lighting and any automations. Anything that
still writes to them shows up as **fighting** under *Luxie Logs & Data > Contested Lights*.
