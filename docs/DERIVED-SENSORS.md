# Derived sensors: exploring "if it BECOMES idle, it was probably on"

Status: **exploration, nothing built.** Written down so we can see what it would cost before deciding.

## The idea

Today a sensor is one entity with one value. Real devices need to be read more cleverly:

- A TV that is *idle* tells us nothing: it could have been off for days.
- A TV that **becomes** idle tells us a lot: it was just on, so someone is probably still around.
- Several devices (TV, console, soundbar) together should read as one thing: "the entertainment is on".

A **derived sensor** is a named device built from one or more source entities plus a few declarative conditions. It is what the rest of Lantern (zones, activities, Flint) would attach to, instead of raw entities.

## What it would look like

```
tv_area  (derived, boolean "active")
  sources:  media_player.tv, media_player.console, media_player.soundbar
  active when ANY of:
    - any source is `playing`
    - any source `became idle` within the last 10 min      <- the edge rule
  hold: 5 min after the last true
```

Three building blocks cover everything we've talked about:

| Block | Meaning | Example |
| --- | --- | --- |
| **is** | the current state matches | `is playing` |
| **became** | a change happened, optionally within a window | `became idle within 10 min` |
| **for** | the state has held this long | `idle for 2 h` (so: truly off) |

Combine with `any` / `all`; add a `hold` (stay true for N minutes after the last true) so flicker doesn't flap lights.
The output is one of: boolean (active / not), a level (for lux: relative brightness, which already exists as a *profile*), or a small enum.

Lux profiles (already built) are the numeric sibling of this: raw lux in, clean relative brightness out. Derived sensors are the boolean/enum sibling.

## The hard part: edges need events, not slots

Echo records a **5-minute grid**. A "became idle" lasts a moment and can fall between two slots, so the grid can't tell us about it.
Edges need a small **state-change log** (entity, from, to, time) for entities that are attached somewhere. Home Assistant already pushes these; Lantern subscribes to them for own-change detection, so the data is available live.

Cost: one more small table with a short retention (days, not months). Only attached entities are logged.

## Why this is useful beyond the TV

- Presence evidence from several sources can be written the same way (radar `is on` OR PC traffic `is active`).
- The **Sensors tab** can show, for every derived sensor, exactly which sources and rules produced its current value: "what data are we using" stays answerable.
- The same rule format can later carry activity detection (working = PC active AND at the desk zone).

## The risk (complexity coming back)

A rule language grows. To keep it small:

1. Only `is`, `became`, `for`, `any`, `all`, `hold`. No arithmetic, no nested expressions beyond one level.
2. Everything is validated on save, shown in plain words on the page, and testable with a replay ("what would this have said yesterday?").
3. Built-in presets first (TV area, PC activity), so most people never write a rule.
4. Derived sensors are **optional**: an entity can still be attached directly.

## Suggested order (when we're ready)

1. Multiple media devices per room (**done**: a room now takes a list and reads "playing" if any is).
2. State-change log for attached entities (small, no behaviour change).
3. Derived sensor with the three blocks, boolean output only, one preset (TV area).
4. Replay a day against it before it is allowed to influence presence.
5. Only then: PC/network activity as a source, and activity detection.

## Open questions

- Should a derived sensor live at home level (shared) or always inside a room/zone?
- Is `hold` per rule or per sensor?
- How far back does "became idle within" need to look (minutes, hours)?
