# Phase 1 · Data — design

Goal: one trustworthy dataset that describes *what the house was doing* and *what the lights were*, every
5 minutes, going back as far as Home Assistant remembers. Everything predictive (intents, wake-up times,
learned light levels) is built on top of it. Garbage in, garbage out, so every number has a known source.

## 1.1 Traceback: where past data comes from

| Source | What | How far back | Used for |
|---|---|---|---|
| HA recorder history (`/api/history/period`) | Every state change + attributes: light on/off, brightness, kelvin, rgb; presence and zone sensors; media player; lux sensors; `sun.sun` elevation; bedtime switch | `recorder: purge_keep_days` (default **10 days**) | The 5-minute grid, labels, model |
| HA long-term statistics (`recorder/statistics_during_period`, hourly mean/min/max) | Numeric sensors with a `state_class`: **indoor + outdoor lux** | Kept forever | Seasonal lux baselines (hour × month) |
| Live capture | Same entities as history, recorded as they change | From now on | Keeps the grid growing past the recorder purge |

Lights have no long-term statistics, so light-level history only reaches back as far as the recorder keeps.
To get more, raise `purge_keep_days` in HA before importing. Luxador keeps its own copy from then on.

All of it lands in one raw table, `ha_history (entity_id, ts, state, attrs)`, whether imported or live, so
there is exactly one source of truth.

## 1.2 The grid: `slots` (one row per room per 5 minutes)

| Column | Meaning | Source |
|---|---|---|
| `slot` | Local start time of the 5-minute slot | HA's configured time zone |
| `minute_of_day`, `weekday`, `daytype` | Clock features (`weekday` / `weekend`) | derived |
| `lux_in` | Indoor lux at slot midpoint | room `lux_sensor` |
| `lux_out` | Outdoor lux | `outdoor_lux_sensor` in settings (optional) |
| `sun_elev` | Sun elevation in degrees | `sun.sun` |
| `occupied` | Any room or zone presence sensor on during the slot | room `presence` |
| `zones` | Zones occupied during the slot | room zones |
| `media` | TV playing | room `media_sensor` |
| `lights` | Per-role brightness % at slot midpoint (max of the role's bulbs; 0 = off) | room `bulbs` |
| `light_max` | Brightest role | derived |
| `intent`, `intent_source` | Label (see below) and where it came from: `rule` or `user` | labeller |

Missing data stays missing (`NULL`), never guessed.

## 1.3 Intents and labels

Intents describe what the house is doing, not what the lights should be:

| Intent | Rule-based label (first pass; your corrections override it) |
|---|---|
| `sleep` | Longest stretch between 19:00 and 11:00 of at least 3 h with no light above 15 % and no TV |
| `nightlight` | A slot inside that sleep stretch where a light was on at 15 % or less |
| `wake` | First 30 min after the sleep stretch ends, if it ends between 04:00 and 12:00 |
| `away` | Nobody present, no lights, not in a sleep stretch |
| `evening` | Active (presence, light or TV), the sun below the horizon (or after 18:00 with no sun data), and after 14:00 or before 04:00; a dark winter morning is `day`, not evening |
| `day` | Any other active slot |

User labels (a time range plus an intent, from the UI or the API) count **5×** in training and always win
over rule labels. The rule labels are scaffolding: the model's accuracy is first measured against them,
so a score of "agrees with the rules" is not the same as "right". Real ground truth comes from your labels.

## 1.4 Predictive layer

**Naive Bayes:** P(intent | features), where

- the prior is P(intent | hour, daytype);
- the likelihoods are lux_in bucket, lux_out/sun bucket, occupied, TV, and minutes since last activity;
- Laplace smoothing, user labels weighted 5×;
- the current light level is **not** a feature (it would teach "lights on, so keep lights on").

**Arbitration** (`config/intents.yaml`): every intent has a `priority` and a `min_confidence`. Of the intents
whose probability clears their threshold, the highest priority wins; otherwise the most probable one wins.
Defaults: `wake 90 > sleep 80 > nightlight 60 > evening 40 > day 30 > away 20`. So sleep overrides
nightlight, but nothing overrides the day starting.

**Predictions** from the labelled history:

- wake and sleep times per day, with median / p10 / p90 for weekdays and weekends;
- light-level traceback: median brightness and on-ratio per role × hour × daytype, and per role × intent;
- lux profile: median indoor/outdoor lux per hour (grid) and per hour × month (long-term statistics).

Phase 1 is **read-only**: nothing here drives a bulb yet. Phase 2/3 will feed the arbitrated intent and the
traceback levels into the resolver.

## What gets graphed (Data Lab tab)

1. **Light traceback heatmap:** hour × weekday, median brightness of the brightest role.
2. **Lux by hour:** median indoor vs outdoor lux (and sun elevation).
3. **Intent by hour:** share of slots per intent at each hour (the model's prior).
4. **Wake / sleep times:** one dot per day, with the median band.
5. **Coverage:** days imported, slots, missing-data share per signal, labels per intent.

## Discovered states (the "double helix")

Two strands, wound together:

1. **Context strand:** each 5-minute slot is reduced to time of day (circular), weekday/weekend, daylight,
   presence, each zone, TV, and time since last activity. Identical contexts merge, then a weighted k-means
   finds up to `max_states` (default 28) states. The number is chosen by silhouette: the largest k within
   10 % of the best score, each state covering at least 1 % of the data. Light levels are *not* in the context.
2. **Behaviour strand:** per state, per light role: how often it's on, median brightness and kelvin, and
   brightness **by daylight** (dark → twilight → low sun → daylight). That is each bulb's own learned rule
   for that condition, read-only for now.

States get a plain description ("weekdays · 20–23 · sofa · TV on · active · dark") and a majority intent,
so intent layering still applies. Names you give stick to the state's centre, so they survive rebuilds.

## Roadmap

| Phase | What |
|---|---|
| 0 · Structure | HOUSE → ROOM → ZONE. Room picker in the UI; HA areas compared with configured rooms (read-only). **(built)** |
| 1 · Data | Traceback import, 5-minute grid, rule labels, intent model, discovered states, Data Lab. **(built)** |
| 2 · Learn presence | Name patterns/states, device↔zone links, label ground truth. **(built, grows with use)** |
| 3 · Learn lighting | Feed the arbitrated intent + each state's per-light behaviour into the resolver, starting from an empty room YAML. |
| 4 · Custom event log | Your own markers, "trigger" or "suspected" (e.g. *"Fridays ~17:00, people over"*), logged as events with time/day. They join the context strand as features, so they can split or merge discovered states. Same shape as today's time-range labels. |
| House level | HOUSE → ROOM → ZONE: house-wide states (asleep, away, waking) built from all rooms, which rooms then inherit. Sleep is really a house state, not a lounge state. |

## Storage: nothing grows forever

Measured on a simulated month of live running, one room: the old 5-second tick log was 104 MB/month. Now:

| Data | Kept |
|---|---|
| Raw HA history (`ha_history`) and the 5-minute grid (`slots`) | `detail_days` (default 90), the window the models train on |
| Tick log (`raw_events`, now every 5 min) | 7 days |
| Audit log (`causal_log`, only real moves ≥ 2 %) | 14 days |
| `light_samples`, `foreign_writes` | 30 days |
| `presence_minutes`, `net_minutes` | 30 days |
| `seasonal` (week × hour × daytype sums), `lts_hourly` | forever, about 1 MB per room |

Slots that age out of the detail window are folded into `seasonal` exactly once. The database is compacted weekly.
The Luxie tab has a Storage card with live sizes.

## The yearly model

The sun is computed for every hour of the year from the home's latitude/longitude (24 × 365, cached, ~0.4 s),
so it works from day one and in either hemisphere. Optional **seasonal compensation** (`seasonal.enabled`):

* **Winter lift:** daytime brightness up to +15 % and +300 K on the shortest day, scaled by how short the day
  is versus the equinox, between `day_start` and `day_end`.
* **Summer later:** the evening curve shifts later by a third of how much later sunset is than at the equinox
  (clock time, so DST counts), capped at 45 min. Winter never pulls evenings earlier unless you set
  `winter_earlier_max_min`.

Your own behaviour accumulates in the `seasonal` table alongside it, so the compensation can later be checked
against (and learned from) what you actually did.

## Luxie · the switchboard

The **🐱 Luxie** tab is a keyboard-first, terminal-style page over the discovered states:

| Key | Does |
|---|---|
| `↑↓` / `j k` | select a state (its dataset shows on the right: hour, weekday, sun, indoor lux, recent days) |
| `space`, then `m` | mark states, then merge them; `u` splits a merged state again |
| `n` | name the state |
| `1-9` | pick a bulb (light group) |
| `l` `p` `o` `i` | set that bulb's mode in this state: **learned** (default), **pinned** (fixed % and optional K), **off**, **ignore** (hands off) |
| `x` | lock / unlock that bulb's setting |

Names, merges and bulb links are keyed to the states' centres in context space (not their numbers), so they
survive every rebuild and retrain. Links are stored now and used by the resolver in Phase 3.

## Locks: tunable, not a black box

* **Seasonal lift** is tunable live in the Data Lab's year card (sliders, saved in the database, over
  `settings.yaml`), with the arithmetic for today shown underneath.
* Every lift setting and every bulb link has a 🔒. A locked value can't be changed (not by a slider, a reset,
  or any future auto-tuning by Luxie) until it is unlocked. Future inference only proposes changes to
  unlocked values and says why.
