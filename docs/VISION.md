# 🏮 Lantern · vision and where we actually are

Three parts: **Echo** (the data) remembers, **Luxie** (the learner) finds what goes together, **Flint** (the engine) lights.

## The vision (one sentence)

**Lights that do the obvious thing on their own:** on when someone's in a room and it's dark enough to need
them, as bright as the darkness calls for, warm in the evening, off when you leave, and only one thing
controlling each light. Learned from your house, never a black box.

## The core loop (everything else is optional)

```
presence (room / zone)  ×  darkness (lux, or the sun)  →  general curve  →  each light's settings  →  HA
```

That loop is the product. Data, learning and the cat exist to tune it and to show *why*.

## Phases

| Phase | Goal | Done when |
|---|---|---|
| **0 · Structure** | Lantern knows which lights, sensors and zones belong to which room | Rooms match HA areas; every light owned by one zone |
| **1 · Data** *(we are here)* | Trustworthy record of what the house did, plus the core loop running in **dry run** so you can compare "would do" against reality | A week of real history imported; the timeline shows presence, lux and real light levels; the dry-run trace looks right |
| **2 · Presence** | Room/zone presence you can trust (clear delays, zone ownership, signals) | Presence on the timeline matches what you remember |
| **3 · Lighting** | Dry run off: the core loop drives the bulbs | A week with no "why did it do that?" |
| **4 · Learning** | States, intents, device links, custom events, seasonal tuning feed back into the curve | Suggestions you accept, never silent changes |

## Inventory (audit of v0.6.0, cut in v0.7.0)

### ✅ Real and core: keep

| Piece | Why it stays |
|---|---|
| HA bridge (connect, send-on-change, own-change detection, dry run) | The only way anything reaches a bulb |
| Rooms + zones config, room/zone presence with clear delay | Phase 0/2 foundation |
| **Standard engine**: general curve × presence, zone-owned lights, per-light settings, trace | The core loop |
| Luxie page: curve, room › zones › lights, grid, "why, right now" | The control surface for the core loop |
| History import + live capture + 5-minute grid | Phase 1 itself |
| Data Lab timeline, coverage, light heatmap, sleep/wake times | How you *see* Phase 1 data |
| Retention + storage card | Keeps the database from bloating |
| Contested lights (who else touched a light) | Finds the "group warfare" you wanted gone |
| HA areas vs rooms | Phase 0 |

### 🟥 Fake or dead: cut

| Piece | The problem |
|---|---|
| **Sandbox snapshot** (439 lux, frozen light states) shown whenever HA isn't connected | Fake numbers that look real |
| **Tweaks & Guards** (5 tweaks) | Saved to YAML, read by nothing |
| "Rolling Memory & Sleep Guards" card | Decorative: hardcoded 85 % bar |
| `traits` | Parsed, never used |
| Bulb lux-gain *learning* (`bulb_lux_gain` table) | Never called; the static gains stay |
| Heuristic state ("Relaxing", "Working"…) + confirmation banner | Hardcoded guesses (Gemini) |
| 24 h simulator + TV time-warp | Runs the *legacy* rules, so it no longer shows what the engine does |
| Lux drift (`clean_hourly`, `yearly_baselines`) | Superseded by the seasonal table + HA long-term stats |
| `raw_events` tick log, `light_samples`, "Last week says" preview | Duplicates of the 5-minute grid |
| Learned baseline offsets (silent EWMA from manual nudges) | A hidden learner changing brightness: Phase 4, not now |
| "Build Phases" card | Replaced by this page |

### 🅿️ Real but early: park behind one "Insights" drawer (Phase 2–4)

| Piece | Why park it |
|---|---|
| Four overlapping "state" systems: presence patterns, intent model + labels + layering, discovered states + switchboard pins | Useful later; right now it's four answers to one question |
| OPNsense device↔zone learning | Never tested against a real OPNsense box |
| Yearly sun model + seasonal lift | Real maths; off by default; tuning belongs in Phase 3/4 |
| Legacy rules engine, overlays YAML studio, rule add/remove | Kept as the fallback engine; editing UI hidden |

## Shape after the cut (v0.7.0)

| Tab | Holds |
|---|---|
| **Home** | Connection (honest when offline), rooms, HA areas vs rooms, contested lights |
| **Luxie** | The core loop: general curve, room › zones › lights, the grid, "why, right now"; learned patterns in a drawer |
| **Data** | Right now (live presence/zones/signals), coverage, **what goes together**, timeline, heatmap, sleep/wake, year, network devices, storage |
| **Logs** | The audit log |

Database: 21 tables → 11. API: dead endpoints removed. Every number on screen comes from Home Assistant or your
history; offline, the screens say so.

## Next: the Activity layer (proposal, not built)

The structure gains one level: **HOME › ROOM › ZONE › ACTIVITY**. An activity is something you *do* in a zone
(watching TV on the sofa, working at the desk, a party) and its main job is to **pick a curve**.

```yaml
zones:
  sofa:
    lights: [loungetv]
    activities:
      tv:    { when: "media == playing", curve: relax }     # softer, warmer
  desk_kwn:
    lights: [kwn_desk, kw_desk_led]
    activities:
      work:  { when: "zone.desk_kwn == on", curve: focus }  # crisper, cooler
```

* **Curves become a small library** at house level: `general` (today's curve), plus e.g. `relax`, `focus`,
  `party`; each is the same handful of settings (dark/bright lux, min/max, shape, colour schema).
* A zone uses `general` unless one of its activities is on; then its lights follow that activity's curve.
  Party is just another activity with a colourful curve; "both" = party on the accents, focus on the desk.
* **Activities are suggested from the data, never required:** "What goes together" already shows "TV on goes
  with the sofa, weekdays 20:00–22:00" and "desk active weekdays 17:00–20:00"; one click turns that into an
  activity. Learned patterns stay under the hood as the evidence, not as a concept you manage.

