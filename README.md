# SB Metra for Home Assistant

Live train tracking, timetables and maps for all 11 Metra commuter rail lines in
Chicago, built on Metra's public GTFS and GTFS-realtime feeds.

- **Two network-wide sensors** carry every line's running trains and its
  schedule outlook, so dashboards never need one entity per line.
- **Running trains appear on the map card** as `geo_location` entities with a
  line-colored engine icon.
- **Actions** answer schedule, arrivals, trip and train questions for one line
  or several at once.
- **Jinja macros**, installed automatically, render timetables, departure
  grids, service calendars and live train tables in markdown cards, or return
  the same data as JSON for your own templates.
- **Commute pairs** (optional) add next / upcoming / en-route sensors for a
  station pair.

Not affiliated with or endorsed by Metra.

## Requirements

- Home Assistant 2026.3 or newer
- A Metra GTFS API token, free from [metra.com/developers](https://metra.com/developers)

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/snadboy/ha-sb-metra`
   with type **Integration**.
2. Search for **SB Metra**, download it, and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → **SB Metra**, then
   paste your API token.

### Manual

Copy `custom_components/metra` into `<config>/custom_components/`, restart, and
add the integration as above.

## Configuration

**Options** (Settings → Devices & services → SB Metra → Configure):

| Option | Default | Notes |
|---|---|---|
| Lines to publish | all 11 | Unselected lines are left out of every sensor, map and action |
| Days of schedule outlook | 28 (7–190) | How far ahead `sensor.metra_schedule` looks. Startup computes every day for every line before the entities appear, so large values delay them by a minute or more after a restart |

**Commute pairs** (optional): on the integration page choose **Add commute
pair**, pick a line and type an origin and a destination station. Each pair gets
its own device with six sensors (see below). Use at most one pair per line: the
entity ids are built from the line name.

## What you get

### Sensors (device "Metra")

| Entity | State | Attributes |
|---|---|---|
| `sensor.metra_active_trains` | trains running now, all lines | `lines`: `{line: [trains]}`, each train with `train`, `direction`, `destination`, `next_station`, `eta`, `delay_min`, `latitude`, `longitude`, remaining `stops` (`station`, `eta`); `updated` |
| `sensor.metra_schedule` | trains scheduled today, all lines | `lines`: `{line: {days, patterns}}`. `days` lists each service day with its pattern key (`weekday`, `saturday`, `sunday` or a holiday date); `patterns` holds each pattern's trains with every stop time |

Data refreshes every 2 minutes. `sensor.metra_schedule` only changes at
midnight, so it is not rewritten on every refresh.

### Map (`geo_location`)

Every running train with a position becomes `geo_location.metra_<line>_<train>`.
The `source` is `metra_<line>` with the line lowercased and `-` turned into `_`
(`metra_up_w`). Attributes: `line`, `train`, `direction`, `status` (`running`),
`destination`, `next_station`, `eta`, `delay_min`, `terminal_eta`.

When a train finishes, its entity stays until about 3:30 AM with no coordinates
and `status: finished`, so maps stop drawing it. It is kept rather than deleted
because the Home Assistant frontend keeps showing entities that were deleted
while a browser was disconnected (after a restart, a network drop, or a tab left
in the background), until the page is reloaded. Filter on `status: running` if
you list these entities yourself.

```yaml
type: map
geo_location_sources: [metra_up_w, metra_bnsf]   # or all eleven
hours_to_show: 0
auto_fit: true
```

### Commute pair sensors (device "Metra `<line>` commute")

`sensor.metra_<line>_next_inbound`, `_upcoming_inbound`, `_en_route_inbound` and
the same three for outbound. "Next" shows the next departure as text (with
`train`, `departure`, `arrival`, `is_live`, `delay_min` attributes); "upcoming"
and "en route" count trains and list them in `trains`.

### Actions

All actions take `line` (one name or a list) and return a response keyed by line
name, so one call can cover several lines.

| Action | Fields | Returns |
|---|---|---|
| `metra.schedule` | `line`, `date` (default today) | every scheduled train for that service day |
| `metra.arrivals` | `line`, `station`, `n` (5) | next arrivals at a station, both directions, with live ETAs |
| `metra.query` | `line`, `origin`, `destination`, `n` (3) | next trains between two stations |
| `metra.train` | `line`, `train`, `date` | one train's stops with live ETAs, passed flags, position and delay |

```yaml
action: metra.query
data:
  line: UP-W
  origin: Geneva
  destination: Chicago OTC
response_variable: trips   # trips["UP-W"] is the list
```

## Macros

On startup the integration installs `custom_templates/metra.jinja` and reloads
custom templates. **That file is replaced whenever the bundled copy changes**, so
don't edit it in place; copy macros into your own file if you want to change them.

Every macro takes a line **name** (`'UP-W'`). Renderers return markdown for a
markdown card; each has a `*_data` twin that returns JSON, which you turn into a
real list with `| from_json`.

| Renderer | Data twin | Shows |
|---|---|---|
| `metra_timetable_grid(line, direction='inbound', pattern=none, at=none, include_finished=false)` | `metra_timetable_grid_data` | printed-timetable grid: stations down, trains across; `…` passed, blank = no stop, **bold** = next stop of a running train |
| `metra_timetable(line, direction='inbound', pattern=none, limit=none)` | `metra_timetable_data` | one row per train: depart, arrive, from, to, stops |
| `metra_stops_table(line, train_no, pattern=none)` | `metra_stops_table_data` | one train's stop list |
| `metra_pattern_summary(line)` | `metra_pattern_summary_data` | trains per service pattern, first and last departure |
| `metra_service_calendar(line)` | `metra_service_calendar_data` | which pattern runs on which dates, holidays included |
| `metra_service_browser(line, on_date=none)` | `metra_service_browser_data` | collapsible outlook: every pattern's trains and their stops |
| `metra_active_trains(lines=none)` | `metra_active_trains_data` | running trains with next station, ETA, delay and terminal; `none` = every line |

Helpers: `metra_pattern_key(line, pattern=none)` (today's pattern key) and
`metra_service_label(line, pkey)`.

A markdown card needs an explicit `entity_id` list, because Home Assistant can't
see the sensors referenced inside an imported macro:

```yaml
type: markdown
entity_id: [sensor.metra_active_trains, sensor.metra_schedule]
content: |
  {%- from 'metra.jinja' import metra_timetable_grid -%}
  {{- metra_timetable_grid('UP-W', 'inbound', at=now().strftime('%H:%M')) -}}
card_mod:   # optional: horizontal scroll with a pinned station column
  style:
    ha-markdown $: |
      table { display: block; overflow-x: auto; white-space: nowrap; }
      th:first-child, td:first-child { position: sticky; left: 0; z-index: 1;
        background: var(--ha-card-background, var(--card-background-color)); }
```

Passing `at=now()...` from the card makes it refresh every minute; the
`entity_id` list refreshes it when the live feed updates. The `card_mod` block
needs [card-mod](https://github.com/thomasloven/lovelace-card-mod).

Home Assistant rejects any template output over 256 KiB.
`metra_service_browser` keeps itself under that by dropping stop lists from
later patterns on very large lines (ME); pass a date to see one service in full.

## Example dashboards

`examples/dashboards/` holds the author's views as YAML: a commute view, a
gallery of every macro, per-line pop-ups, maps, and trials of tabbed and
expander layouts. Some cards use HACS cards
([Bubble Card](https://github.com/Clooos/Bubble-Card),
[card-mod](https://github.com/thomasloven/lovelace-card-mod),
[Tabbed Card](https://github.com/kinghat/tabbed-card),
[Expander Card](https://github.com/MelleD/lovelace-expander-card)), commute-pair
sensors, and two zones (`zone.elburn_station`, `zone.ogilvie_otc`) you would
replace with your own.

## Recorder

The sensors carry large, fast-changing attributes and the map entities churn
every refresh. History of them is rarely useful, so consider excluding them:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.metra_active_trains
      - sensor.metra_schedule
      - geo_location.metra_*
      - sensor.metra_*_next_*
      - sensor.metra_*_upcoming_*
      - sensor.metra_*_en_route_*
```

## Files the integration writes

| Path | Purpose |
|---|---|
| `<config>/.metra_cache/` | downloaded GTFS schedule and its parsed index; refreshed when Metra publishes a new schedule |
| `<config>/custom_templates/metra.jinja` | the macros (replaced when the bundled copy changes) |

The engine icons are served from the integration folder at `/metra_static/`.

## Development

- `tools/brand_icon.svg` is the source of the integration icon in
  `custom_components/metra/brand/` (256 and 512 px PNGs).
- `tools/gen_engines.py` regenerates the 22 line-colored icons in
  `custom_components/metra/www/` from `tools/engine_template.svg`.
- `scripts/deploy.sh` copies the integration to a Home Assistant host over SSH
  for testing before a release. Python changes need a restart.
- GitHub Actions runs hassfest and the HACS validator on every push.
- Releases are GitHub releases tagged `vX.Y.Z`, matching `version` in
  `manifest.json`; HACS offers the latest one.

## License

MIT. See [LICENSE](LICENSE).
