# 🏮 Lantern

Lights that follow presence and darkness, learned from your house. Open it from the **Lantern** sidebar panel.

**Echo** remembers what the house did, **Luxie** learns what goes together, **Flint** lights it.

## First start

- Starts in **dry run**: it computes and logs what it would do, but never touches your bulbs. Leave it on
  for the first week while Phase 1 records data, and turn it off on the Configuration tab when you're happy.
- Example config is copied to the app's config folder: `/addon_configs/<id>_lantern/` on your HA host
  (reach it with the File editor / Samba / SSH apps). Edit `rooms/lounge.yaml` for your entities, or add more
  `rooms/*.yaml` files, then restart the app. Your edits are never overwritten.
- Home Assistant access is automatic (Supervisor token); no URL or token needed.

## Options

| Option | What |
|---|---|
| `dry_run` | `true` = log only, `false` = drive the lights |
| `tick_seconds` | How often rooms are re-evaluated |
| `opnsense_url`, `opnsense_api_key`, `opnsense_api_secret` | Optional. Lets Luxie learn which network devices move with which zone. The API user needs *Diagnostics: ARP Table* and *Reporting: Traffic*. |
| `opnsense_verify_ssl` | Turn off only if OPNsense uses its self-signed certificate |

## One controller per light

Take the lights Flint drives out of Magic Areas, Adaptive Lighting and any automations. Anything that
still writes to them shows up as **fighting** under *Luxie Logs & Data > Contested Lights*.
