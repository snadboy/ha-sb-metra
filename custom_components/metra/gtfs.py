"""Metra GTFS static + realtime logic (sync; call via executor).

Ported from the retired metra_mqtt.py publisher. Pure functions over a cached,
pickled static-schedule index plus the GTFS-realtime protobuf feeds.

Gotchas handled: GTFS times past 24:00 (post-midnight trips), calendar_dates
holiday exceptions, realtime-vs-static trip_id version-suffix drift (matched on
the train-number token), Metra leaving the GTFS-rt `delay` field at zero
(delay is COMPUTED as live prediction minus scheduled time), and the positions
feed carrying coordinates but empty stop fields.
"""
from __future__ import annotations

import json
import pickle
import re
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
CACHE = Path("/config/.metra_mqtt")   # reuse the publisher-era cache
N_UPCOMING = 3
LOOKAHEAD_DAYS = 2

SCHEDULE_URLS = [
    "https://schedules.metrarail.com/gtfs/schedule.zip",
    "https://gtfspublic.metrarr.com/gtfs/raw/schedule.zip",
]
PUBLISHED_URLS = [
    "https://schedules.metrarail.com/gtfs/published.txt",
    "https://gtfspublic.metrarr.com/gtfs/raw/published.txt",
]
RT_BASE = "https://gtfspublic.metrarr.com/gtfs/public"

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _fetch(url: str, timeout: int = 20) -> bytes:
    return urllib.request.urlopen(url, timeout=timeout).read()


def train_token(trip_id: str) -> str:
    m = re.search(r"_([A-Z]{2,4}\d+)_", trip_id + "_")
    return m.group(1) if m else trip_id


def train_number(tok: str) -> str:
    m = re.search(r"(\d+)$", tok)
    return m.group(1) if m else tok


def refresh_schedule() -> str:
    """Ensure schedule.zip is current; return the published version string."""
    CACHE.mkdir(exist_ok=True)
    zpath, meta = CACHE / "schedule.zip", CACHE / "published.txt"
    published = None
    for u in PUBLISHED_URLS:
        try:
            published = _fetch(u, 10).decode().strip()
            break
        except Exception:
            continue
    if zpath.exists() and published and meta.exists() and meta.read_text() == published:
        return published
    if not zpath.exists() or published:
        for u in SCHEDULE_URLS:
            try:
                tmp = CACHE / "schedule.zip.tmp"
                tmp.write_bytes(_fetch(u, 60))
                zipfile.ZipFile(tmp).close()
                tmp.rename(zpath)
                if published:
                    meta.write_text(published)
                break
            except Exception:
                continue
    if not zpath.exists():
        raise RuntimeError("no schedule.zip available")
    return published or "unknown"


def _read_table(z: zipfile.ZipFile, name: str) -> list[dict]:
    lines = z.read(name).decode("utf-8-sig").splitlines()
    hdr = [h.strip() for h in lines[0].split(",")]
    return [dict(zip(hdr, (c.strip() for c in row.split(",")))) for row in lines[1:] if row.strip()]


def load_index() -> dict:
    """Parsed static schedule, rebuilt only when Metra publishes a new one."""
    version = refresh_schedule()
    ipath = CACHE / "index.pkl"
    if ipath.exists():
        try:
            idx = pickle.loads(ipath.read_bytes())
            if idx.get("version") == version:
                return idx
        except Exception:
            pass
    z = zipfile.ZipFile(CACHE / "schedule.zip")
    routes = sorted(_read_table(z, "routes.txt"), key=lambda r: r["route_id"])
    trips = {}
    for r in _read_table(z, "trips.txt"):
        trips[r["trip_id"]] = {"route": r["route_id"], "service": r["service_id"],
                               "direction": r.get("direction_id", "")}
    trip_stops: dict[str, dict] = {}
    for r in _read_table(z, "stop_times.txt"):
        t = r["trip_id"]
        if t in trips:
            trip_stops.setdefault(t, {})[r["stop_id"]] = (
                int(r["stop_sequence"]), r["departure_time"], r["arrival_time"])
    tokens: dict[str, dict[str, list]] = {}
    for tid, tr in trips.items():
        tokens.setdefault(tr["route"], {}).setdefault(train_token(tid), []).append(tid)
    idx = {
        "version": version,
        "routes": [{"id": r["route_id"], "name": r.get("route_long_name") or r["route_short_name"],
                    "color": "#" + r["route_color"] if r.get("route_color") else None} for r in routes],
        "names": {r["stop_id"]: r["stop_name"] for r in _read_table(z, "stops.txt")},
        "trips": trips,
        "trip_stops": trip_stops,
        "tokens": tokens,
        "calendar": _read_table(z, "calendar.txt"),
        "caldates": _read_table(z, "calendar_dates.txt"),
    }
    ipath.write_bytes(pickle.dumps(idx))
    return idx


def active_services(idx: dict, d: date) -> set:
    ds = d.strftime("%Y%m%d")
    dow = DAYS[d.weekday()]
    active = set()
    for r in idx["calendar"]:
        if r.get(dow) == "1" and r["start_date"] <= ds <= r["end_date"]:
            active.add(r["service_id"])
    for r in idx["caldates"]:
        if r["date"] == ds:
            (active.add if r["exception_type"] == "1" else active.discard)(r["service_id"])
    return active


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def us_holiday_name(d: date) -> str | None:
    """Names for the majors (GTFS carries no holiday names)."""
    y = d.year
    fixed = {(1, 1): "New Year's Day", (6, 19): "Juneteenth",
             (7, 4): "Independence Day", (11, 11): "Veterans Day",
             (12, 25): "Christmas Day"}
    if (d.month, d.day) in fixed:
        return fixed[(d.month, d.day)]
    floating = {
        _nth_weekday(y, 1, 0, 3): "MLK Day",
        _nth_weekday(y, 2, 0, 3): "Presidents' Day",
        _nth_weekday(y, 9, 0, 1): "Labor Day",
        _nth_weekday(y, 10, 0, 2): "Columbus Day",
        _nth_weekday(y, 11, 3, 4): "Thanksgiving",
    }
    memorial = _nth_weekday(y, 5, 0, 5)
    if memorial.month != 5:
        memorial -= timedelta(weeks=1)
    floating[memorial] = "Memorial Day"
    return floating.get(d)


def gtfs_dt(service_day: date, hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(service_day.year, service_day.month, service_day.day, tzinfo=TZ) + timedelta(
        hours=h, minutes=m, seconds=s)


def realtime(token: str):
    """(trip updates by route/token, positions by route/token)."""
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(_fetch(f"{RT_BASE}/tripupdates?api_token={token}"))
    rt: dict[str, dict] = {}
    for e in feed.entity:
        if e.HasField("trip_update") and e.trip_update.stop_time_update:
            tu = e.trip_update
            rt.setdefault(tu.trip.route_id, {})[train_token(tu.trip.trip_id)] = tu
    pos: dict[str, dict] = {}
    try:
        pfeed = gtfs_realtime_pb2.FeedMessage()
        pfeed.ParseFromString(_fetch(f"{RT_BASE}/positions?api_token={token}"))
        for e in pfeed.entity:
            if e.HasField("vehicle") and e.vehicle.HasField("position"):
                v = e.vehicle
                pos.setdefault(v.trip.route_id, {})[train_token(v.trip.trip_id)] = (
                    round(v.position.latitude, 5), round(v.position.longitude, 5))
    except Exception:
        pass
    return rt, pos


def stu_time(stu) -> int:
    return stu.departure.time or stu.arrival.time


def sched_delay_min(live_ts, st_entry, now: datetime):
    """Minutes late vs static schedule (Metra leaves GTFS-rt `delay` at 0)."""
    if not live_ts or not st_entry:
        return 0
    live = datetime.fromtimestamp(live_ts, TZ)
    best = None
    for off in (-1, 0, 1):
        sd = gtfs_dt(now.date() + timedelta(days=off), st_entry[2])
        diff = (live - sd).total_seconds()
        if best is None or abs(diff) < abs(best):
            best = diff
    return round(best / 60)


def static_trip_for(idx: dict, line: str, tok: str, origin: str | None = None, dest: str | None = None):
    for tid in idx["tokens"].get(line, {}).get(tok, []):
        st = idx["trip_stops"].get(tid, {})
        if origin and dest:
            if origin in st and dest in st and st[origin][0] < st[dest][0]:
                return tid, st
        else:
            return tid, st
    return None, {}


def resolve_stop(idx: dict, line: str, needle: str) -> str:
    needle_l = str(needle).lower().strip()
    stops_on_line: set = set()
    for tid, tr in idx["trips"].items():
        if tr["route"] == line:
            stops_on_line.update(idx["trip_stops"].get(tid, {}))
    exact = [s for s in stops_on_line
             if s.lower() == needle_l or idx["names"].get(s, "").lower() == needle_l]
    if exact:
        return exact[0]
    part = [s for s in stops_on_line
            if needle_l in s.lower() or needle_l in idx["names"].get(s, "").lower()]
    if len(part) == 1:
        return part[0]
    raise ValueError(f"stop '{needle}' on {line}: "
                     + ("no match" if not part else "ambiguous: " + ", ".join(sorted(part))))


def check_line(idx: dict, line: str) -> None:
    if line not in {r["id"] for r in idx["routes"]}:
        raise ValueError(f"unknown line {line}; lines: "
                         + ", ".join(r["id"] for r in idx["routes"]))


def upcoming(idx, line, origin, dest, rt_line, now, n=N_UPCOMING):
    names = idx["names"]
    out = []
    for offset in range(LOOKAHEAD_DAYS + 1):
        day = now.date() + timedelta(days=offset)
        services = active_services(idx, day)
        for tid, tr in idx["trips"].items():
            if tr["route"] != line or tr["service"] not in services:
                continue
            st = idx["trip_stops"].get(tid, {})
            if origin not in st or dest not in st or st[origin][0] >= st[dest][0]:
                continue
            dep, arr = gtfs_dt(day, st[origin][1]), gtfs_dt(day, st[dest][2])
            tok = train_token(tid)
            is_live, delay = False, 0
            live_times = {}
            if offset == 0 and tok in rt_line:
                remaining = {s.stop_id: s for s in rt_line[tok].stop_time_update}
                if origin not in remaining:
                    continue  # already departed origin -> en route, not upcoming
                stu = remaining[origin]
                if stu_time(stu):
                    sched_dep = dep
                    dep = datetime.fromtimestamp(stu_time(stu), TZ)
                    delay = round((dep - sched_dep).total_seconds() / 60)
                    is_live = True
                if dest in remaining and stu_time(remaining[dest]):
                    arr = datetime.fromtimestamp(stu_time(remaining[dest]), TZ)
                live_times = {u.stop_id: stu_time(u) for u in rt_line[tok].stop_time_update if stu_time(u)}
            if dep < now - timedelta(minutes=1):
                continue
            lo, hi = st[origin][0], st[dest][0]
            stops_list = [{"station": names.get(sid, sid),
                           "eta": (datetime.fromtimestamp(live_times[sid], TZ) if sid in live_times
                                   else gtfs_dt(day, sarr)).strftime("%H:%M")}
                          for sid, (seq, sdep, sarr) in sorted(st.items(), key=lambda kv: kv[1][0])
                          if lo <= seq <= hi]
            tom = " (Tomorrow)" if dep.date() != now.date() else ""
            out.append({"train": train_number(tok),
                        "display": f"{dep.strftime('%H:%M')} → {arr.strftime('%H:%M')}{tom}",
                        "departure": dep.isoformat(), "arrival": arr.isoformat(),
                        "is_live": is_live, "delay_min": delay, "stops": stops_list,
                        "_sort": dep.timestamp()})
    out.sort(key=lambda t: t["_sort"])
    seen, dedup = set(), []
    for t in out:
        key = (t["train"], t["departure"])
        if key not in seen:
            seen.add(key)
            dedup.append({k: v for k, v in t.items() if k != "_sort"})
    return dedup[:n]


def enroute(idx, line, origin, dest, rt_line, pos_line):
    names = idx["names"]
    now = datetime.now(TZ)
    trains = []
    for tok, tu in rt_line.items():
        tid, st = static_trip_for(idx, line, tok, origin, dest)
        if not tid:
            continue
        remaining = [s.stop_id for s in tu.stop_time_update]
        if dest not in remaining or origin in remaining:
            continue
        stops = list(tu.stop_time_update)
        nxt = stops[0]
        dstu = next(s for s in stops if s.stop_id == dest)
        stops_list = []
        for u in stops:
            stops_list.append({"station": names.get(u.stop_id, u.stop_id),
                               "eta": datetime.fromtimestamp(stu_time(u), TZ).strftime("%H:%M") if stu_time(u) else "?"})
            if u.stop_id == dest:
                break
        lat, lon = pos_line.get(tok, (None, None))
        trains.append({"train": train_number(tok),
                       "next_station": names.get(nxt.stop_id, nxt.stop_id),
                       "eta": datetime.fromtimestamp(stu_time(nxt), TZ).strftime("%H:%M") if stu_time(nxt) else "?",
                       "delay_min": sched_delay_min(stu_time(nxt), st.get(nxt.stop_id), now),
                       "dest_eta": datetime.fromtimestamp(stu_time(dstu), TZ).strftime("%H:%M") if stu_time(dstu) else "?",
                       "stops": stops_list, "latitude": lat, "longitude": lon,
                       "_sort": stu_time(dstu) or 0})
    trains.sort(key=lambda t: t["_sort"])
    return [{k: v for k, v in t.items() if k != "_sort"} for t in trains]


def active_trains(idx, line, rt_line, pos_line):
    names = idx["names"]
    now = datetime.now(TZ)
    out = []
    for tok, tu in rt_line.items():
        stops = list(tu.stop_time_update)
        nxt = stops[0]
        tid, st = static_trip_for(idx, line, tok)
        if st:
            last_id = max(st.items(), key=lambda kv: kv[1][0])[0]
            direction = "inbound" if idx["trips"][tid].get("direction") == "1" else "outbound"
        else:
            last_id = stops[-1].stop_id
            direction = "?"
        lat, lon = pos_line.get(tok, (None, None))
        stops_list = [{"station": names.get(u.stop_id, u.stop_id),
                       "eta": datetime.fromtimestamp(stu_time(u), TZ).strftime("%H:%M") if stu_time(u) else "?"}
                      for u in stops]
        out.append({"train": train_number(tok), "direction": direction,
                    "destination": names.get(last_id, last_id),
                    "stops": stops_list,
                    "next_station": names.get(nxt.stop_id, nxt.stop_id),
                    "eta": datetime.fromtimestamp(stu_time(nxt), TZ).strftime("%H:%M") if stu_time(nxt) else "?",
                    "delay_min": sched_delay_min(stu_time(nxt), st.get(nxt.stop_id), now),
                    "latitude": lat, "longitude": lon,
                    "_sort": stu_time(nxt) or 0})
    out.sort(key=lambda t: t["_sort"])
    return [{k: v for k, v in t.items() if k != "_sort"} for t in out]


def schedule_day(idx, line, d: date, services=None):
    if services is None:
        services = active_services(idx, d)
    names = idx["names"]
    out = []
    for tid, tr in idx["trips"].items():
        if tr["route"] != line or tr["service"] not in services:
            continue
        st = idx["trip_stops"].get(tid, {})
        if not st:
            continue
        ordered = sorted(st.items(), key=lambda kv: kv[1][0])
        (o_id, (_, o_dep, _oa)), (d_id, (_, _dd, d_arr)) = ordered[0], ordered[-1]
        dep = gtfs_dt(d, o_dep)
        out.append({"train": train_number(train_token(tid)),
                    "direction": "inbound" if tr.get("direction") == "1" else "outbound",
                    "origin": names.get(o_id, o_id), "departs": dep.strftime("%H:%M"),
                    "destination": names.get(d_id, d_id),
                    "arrives": gtfs_dt(d, d_arr).strftime("%H:%M"),
                    "stops": len(ordered),
                    "stations": [{"station": names.get(sid, sid),
                                  "time": gtfs_dt(d, arr).strftime("%H:%M")}
                                 for sid, (_sq, _dp, arr) in ordered],
                    "_sort": dep.timestamp()})
    out.sort(key=lambda t: t["_sort"])
    trains = [{k: v for k, v in t.items() if k != "_sort"} for t in out]
    return {"line": line, "date": d.isoformat(), "count": len(trains), "trains": trains}


def schedule_span(idx, line, start: date, days: int = 28):
    """Next-N-days outlook, deduplicated by service pattern.

    A day is holiday-flagged when its timetable differs from the MODAL pattern
    for its day-of-week category (rider semantics: 'Monday running sunday
    service' flags; GTFS bookkeeping like exception-driven service-id swaps
    that produce an identical timetable does not).
    """
    from collections import Counter

    scheds, fps = {}, {}
    for off in range(days):
        d = start + timedelta(days=off)
        sched = schedule_day(idx, line, d)
        scheds[d] = sched
        fps[d] = hash(tuple((t["train"], t["departs"]) for t in sched["trains"]))

    def cat(d: date) -> str:
        return "weekday" if d.weekday() < 5 else d.strftime("%A").lower()

    modal = {}
    for c in ("weekday", "saturday", "sunday"):
        counts = Counter(fps[d] for d in scheds if cat(d) == c)
        if counts:
            modal[c] = counts.most_common(1)[0][0]

    patterns, days_list, extra_key = {}, {}, {}
    days_list = []
    for d in sorted(scheds):
        fp, c = fps[d], cat(d)
        # CONSISTENT key vocabulary: weekday/saturday/sunday always (identical
        # sat+sun timetables appear under BOTH keys), date-keys only for
        # one-off special schedules matching no category's modal timetable
        if c in modal and fp == modal[c]:
            key = c
        else:
            other = next((oc for oc, ofp in modal.items() if ofp == fp), None)
            if other:
                key = other          # e.g. Labor Day Monday -> "sunday"
            elif fp in extra_key:
                key = extra_key[fp]
            else:
                extra_key[fp] = d.isoformat()
                key = extra_key[fp]
        if key not in patterns:
            patterns[key] = {"count": scheds[d]["count"], "trains": scheds[d]["trains"]}
        entry = {"date": d.isoformat(), "day": d.strftime("%A"), "pattern": key}
        if c in modal and fp != modal[c]:
            entry["holiday"] = us_holiday_name(d) or "modified service"
        days_list.append(entry)
    for c, fp in modal.items():
        if c not in patterns:
            src = next(d for d in scheds if fps[d] == fp)
            patterns[c] = {"count": scheds[src]["count"], "trains": scheds[src]["trains"]}
    return days_list, patterns


def arrivals(idx, line, station_needle, rt, n=5):
    station = resolve_stop(idx, line, station_needle)
    rt_line = rt.get(line, {})
    now = datetime.now(TZ)
    names = idx["names"]
    out = []
    for offset in range(LOOKAHEAD_DAYS + 1):
        day = now.date() + timedelta(days=offset)
        services = active_services(idx, day)
        for tid, tr in idx["trips"].items():
            if tr["route"] != line or tr["service"] not in services:
                continue
            st = idx["trip_stops"].get(tid, {})
            if station not in st:
                continue
            when = gtfs_dt(day, st[station][2])
            tok = train_token(tid)
            is_live, delay = False, 0
            if offset == 0 and tok in rt_line:
                remaining = {u.stop_id: u for u in rt_line[tok].stop_time_update}
                if station not in remaining:
                    continue  # already passed
                stu = remaining[station]
                if stu_time(stu):
                    sched = when
                    when = datetime.fromtimestamp(stu_time(stu), TZ)
                    delay = round((when - sched).total_seconds() / 60)
                    is_live = True
            if when < now - timedelta(minutes=1):
                continue
            last_id = max(st.items(), key=lambda kv: kv[1][0])[0]
            tom = " (Tomorrow)" if when.date() != now.date() else ""
            out.append({"train": train_number(tok),
                        "direction": "inbound" if tr.get("direction") == "1" else "outbound",
                        "destination": names.get(last_id, last_id),
                        "time": when.strftime("%H:%M") + tom,
                        "is_live": is_live, "delay_min": delay,
                        "_sort": when.timestamp()})
    out.sort(key=lambda t: t["_sort"])
    seen, dedup = set(), []
    for t in out:
        key = (t["train"], t["_sort"])
        if key not in seen:
            seen.add(key)
            dedup.append({k: v for k, v in t.items() if k != "_sort"})
    return {"line": line, "station": names.get(station, station), "arrivals": dedup[:n]}


def query_pair(idx, line, origin_needle, dest_needle, rt, n=N_UPCOMING):
    origin = resolve_stop(idx, line, origin_needle)
    dest = resolve_stop(idx, line, dest_needle)
    now = datetime.now(TZ)
    trains = upcoming(idx, line, origin, dest, rt.get(line, {}), now, n=n)
    for t in trains:
        t.pop("stops", None)
    return {"line": line, "origin": idx["names"].get(origin, origin),
            "destination": idx["names"].get(dest, dest), "trains": trains}


def train_details(idx, line, number, d: date, rt, pos):
    names = idx["names"]
    services = active_services(idx, d)
    match = [(tok, tid) for tok, tids in idx["tokens"].get(line, {}).items()
             if train_number(tok) == str(number)
             for tid in tids if idx["trips"][tid]["service"] in services]
    if not match:
        dow = d.strftime("%A")
        day_trains = {}
        for tok, tids in idx["tokens"].get(line, {}).items():
            for tid in tids:
                tr2 = idx["trips"][tid]
                if tr2["service"] not in services:
                    continue
                st2 = idx["trip_stops"].get(tid, {})
                if not st2:
                    continue
                o2 = min(st2.items(), key=lambda kv: kv[1][0])
                dep2 = gtfs_dt(d, o2[1][1])
                day_trains[train_number(tok)] = (
                    dep2, "inbound" if tr2.get("direction") == "1" else "outbound",
                    names.get(o2[0], o2[0]))
        that_day = [f"#{n2} {di} {dp.strftime('%H:%M')} from {o}"
                    for n2, (dp, di, o) in sorted(day_trains.items(), key=lambda kv: kv[1][0])]
        result = {"error": f"train {number} does not run on {line} on {d.isoformat()} ({dow})",
                  "trains_scheduled_that_day": that_day}
        any_trips = [tid for tok, tids in idx["tokens"].get(line, {}).items()
                     if train_number(tok) == str(number) for tid in tids]
        if any_trips:
            svc = idx["trips"][any_trips[0]]["service"]
            row = next((r for r in idx["calendar"] if r["service_id"] == svc), None)
            if row:
                result["train_runs_on"] = [dy.capitalize() for dy in DAYS if row.get(dy) == "1"]
        else:
            result["hint"] = f"no train {number} exists on {line} in the current schedule"
        return result
    tok, tid = match[0]
    tr = idx["trips"][tid]
    st = idx["trip_stops"][tid]
    ordered = sorted(st.items(), key=lambda kv: kv[1][0])
    now = datetime.now(TZ)
    live = {"active": False}
    live_times = {}
    if d == now.date():
        tu = rt.get(line, {}).get(tok)
        if tu:
            stops_r = list(tu.stop_time_update)
            live_times = {u.stop_id: stu_time(u) for u in stops_r if stu_time(u)}
            nxt = stops_r[0]
            lat, lon = pos.get(line, {}).get(tok, (None, None))
            live = {"active": True,
                    "next_station": names.get(nxt.stop_id, nxt.stop_id),
                    "delay_min": sched_delay_min(stu_time(nxt), st.get(nxt.stop_id), now),
                    "latitude": lat, "longitude": lon,
                    "remaining_stops": len(stops_r)}
    stops_out = []
    for sid, (_seq, _dep, arr) in ordered:
        sched = gtfs_dt(d, arr)
        eta = (datetime.fromtimestamp(live_times[sid], TZ).strftime("%H:%M")
               if sid in live_times else None)
        stops_out.append({"station": names.get(sid, sid),
                          "scheduled": sched.strftime("%H:%M"),
                          **({"eta": eta} if eta else {}),
                          **({"passed": True} if live["active"] and sid not in live_times else {})})
    (o_id, (_, o_dep, _oa)), (d_id, (_, _dd, d_arr)) = ordered[0], ordered[-1]
    return {"line": line, "train": str(number), "date": d.isoformat(),
            "direction": "inbound" if tr.get("direction") == "1" else "outbound",
            "origin": names.get(o_id, o_id), "departs": gtfs_dt(d, o_dep).strftime("%H:%M"),
            "destination": names.get(d_id, d_id), "arrives": gtfs_dt(d, d_arr).strftime("%H:%M"),
            "live": live, "stops": stops_out}
