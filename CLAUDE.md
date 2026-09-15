# ha-sb-metra — session notes

Home Assistant custom integration **SB Metra** (domain `metra`): live Metra
trains, schedules, maps, actions and Jinja macros. Published for HACS.

| | |
|---|---|
| Repo | https://github.com/snadboy/ha-sb-metra (public) — local `~/projects/git/ha-sb-metra` |
| Branch | `main` |
| History | split from claude-env `tools/metra` (2026-09-14) with its full commit history |
| Live install | HA host `homeassistant` (HAOS), dashboard `dashboard-monitor` views metra, metra-tables, metra-up-w, metra-maps, metra-tabs, metra-expander |

## Layout

```
custom_components/metra/   everything HACS installs
  __init__.py              coordinator (2-min refresh), actions, file install + static icons
  sensor.py                sensor.metra_active_trains, sensor.metra_schedule, commute-pair sensors
  geo_location.py          one entity per positioned train (source metra_<line>)
  gtfs.py                  sync GTFS static + realtime logic (call via executor)
  config_flow.py           token flow, options (lines, span_days), commute-pair subentries
  templates/metra.jinja    macros; copied to <config>/custom_templates at startup
  www/engine_*.svg         22 map icons, served at /metra_static
  brand/icon.png, icon@2x  local brand images (HA 2026.3+)
tools/gen_engines.py       regenerates www/ icons from tools/engine_template.svg
scripts/deploy.sh          dev deploy over SSH (nbsp + checksum guards)
examples/dashboards/       exported views (YAML)
.github/workflows/         hassfest + HACS validation
```

## Architecture decisions

- ONE "Metra" device; network-wide sensors keyed by line inside attributes. No
  per-line devices, no select entities: line choice belongs in the dashboard
  (Bubble Card pop-ups by URL hash), never in a global select (it couples
  unrelated cards across every screen).
- Maps via `geo_location` (dynamic add/remove, no unique_id), not sensor slots.
- Actions are DOMAIN actions taking `line` (name or list); responses keyed by line.
- Macros: helpers → `*_data` (all logic, JSON out) → renderers (formatting only).
  A macro may only call one defined above it. Add logic to `_data`, never to a renderer.
- Recorder excludes are the user's job (documented), not something we write.

## Build / test / deploy

- Lint: `python3 -m py_compile custom_components/metra/*.py && uvx pyflakes custom_components/metra/*.py`
  (the `_oa`/`_dd` unused-variable hits in gtfs.py are intentional placeholders).
- hassfest locally: `docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest:latest`
- Macro regression: render every macro × line via REST `/api/template` before and after,
  md5 the outputs, diff. Inline testing works too: POST the whole jinja file text
  followed by the call, so nothing needs deploying.
- Dev deploy: `scripts/deploy.sh` (add `--reload` for macro-only changes). Python
  changes need a FULL restart — a config-entry reload keeps the old module.
- Release: bump `manifest.json` version, commit, tag `vX.Y.Z`, create a GitHub release.

## Gotchas (hard-won)

- **Non-breaking spaces**: metra.jinja has exactly 62 U+00A0 characters. A typed
  nbsp in a shell/python command becomes a plain space — always write `' '`.
  deploy.sh refuses to deploy on a count mismatch.
- `replace(' ', ' ')` with a plain space second arg is a silent no-op.
- Markdown cards render templates STRICT: an Undefined reaching `to_json` or an
  attribute read blanks the whole card (zero-train saturday/sunday patterns on
  HC/NCS/SWS did this).
- HA caps template output at 256 KiB (`MAX_TEMPLATE_OUTPUT`, enforced in
  `Template.async_render`, so cards too). The service browser budgets itself.
- Imported macros hide entity references: cards need `entity_id:` lists; a
  `now()` in the card template (not the macro) gives minute refreshes.
- Tables in markdown cards live in `ha-markdown`'s shadow root → card-mod key `ha-markdown $`.
- Realtime vs static trip_ids differ by version suffix — match on train number.
  tripupdates lists only REMAINING stops; the positions feed has lat/lon but empty stop fields.
- Metra leaves GTFS-rt `delay` at 0; delay is computed (prediction − schedule).
- `span_days` sets startup cost (every day × every line before entities appear).
- NumberSelector options arrive as floats — wrap in `int()`.
- Registry: sensor setup purges this entry's stale rows and retired devices itself.
- `integration_entities('metra')` hides registry orphans; count from `states` or the registry.
- REST calls that raise ServiceValidationError return HTTP 500 (HA core api handler), UI is fine.
- **Stale train markers after a reconnect (frontend bug, proven 2026-09-15)**: home-assistant-js-websocket
  9.6.0 (frontend 20260826.7, HA 2026.9.2) merges the `subscribe_entities` snapshot it gets after a reconnect
  into the old store (`{...store.state}` + additions) and never deletes entities that are gone, so any
  geo_location entity REMOVED while a client was disconnected (HA restart, network blip, tab hidden >5 min,
  phone/laptop sleep) stays in that client's `hass.states` and on its maps until a page reload. Probe: create
  a state via REST, `conn.suspendReconnectUntil(p); conn.suspend()`, DELETE it, resolve p → still present.
  9.7.0 does not change it; no upstream issue found. A marker only disappears for a reconnecting client if the
  entity still EXISTS at reconnect time with updated (location-less) attributes — GeolocationEvent omits
  latitude/longitude when None, and the map card skips entities without numeric coordinates.
- **Icon in the HACS store list stays a placeholder**: HACS 2.0.5 (latest; its frontend is dormant) loads
  row icons straight from `brands.home-assistant.io/_/metra/icon.png`, not HA's Brands Proxy API, so the local
  `brand/` folder only shows on Home Assistant's own pages. The home-assistant/brands repo no longer accepts
  NEW `custom_integrations/<domain>/` folders (a workflow auto-closes such PRs), so there is no fix on our side.
  Icon source: `tools/brand_icon.svg` → render 256/512 px PNGs, optimize losslessly (pyoxipng).

## Status

- [x] Repo split, HACS layout, brand icons, hacs.json, validation workflow, README
- [x] v2.1.0 released; live install CUT OVER to HACS (2026-09-14): HACS custom repo,
      entry/subentry/entities unchanged, 99/99 macro fingerprints identical,
      `.metra_mqtt` renamed to `.metra_cache`, macros reinstalled by startup code,
      icons served at `/metra_static`, brand icon shown on the integration page
- [x] Removed `/config/www/metra` and the `metra.jinja.bak*` copies. Fallbacks kept:
      HA backup `6b28776a`, `/config/backups_metra_pre_hacs_*`
- Updating from now on: change code → bump manifest version → tag + GitHub release →
  update in HACS → restart. `scripts/deploy.sh` is only for testing before a release
  (HACS will show the install as modified until the next release is installed).
- [x] MIT license (HACS validation requires one)
