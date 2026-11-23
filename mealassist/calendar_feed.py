"""ICS calendar feed parsing and recurrence expansion for pickup bookings.

Two elder-attested time semantics are supported and never guessed:

* ``WALLCLOCK`` -- the confirmed agreement is a local wall-clock time,
  e.g. "every Sunday 09:00 in Europe/Berlin".  Recurrence keys are naive
  local datetimes tagged with their TZID; the UTC instant is derived per
  occurrence through zoneinfo, so a DST switch moves the UTC instant
  (Berlin 09:00 -> 07:00Z after the spring forward).
* ``UTC-INTERVAL`` -- the confirmed agreement is an absolute 168-hour
  cadence anchored on the first UTC instant (medication-linked meals).
  Recurrence keys are UTC instants; the local wall clock drifts across
  DST (08:00Z stays 08:00Z, i.e. 10:00 local after the switch).

A series without an attested ``X-MEAL-TIME-MODE`` stays *pending*: only
the first occurrence (pinned by DTSTART itself) carries an instant; later
occurrences are recorded with unknown times instead of a parser guess.

Ambiguous wall clocks (DST fall-back fold) keep both candidate instants
with their fold markers; nonexistent wall clocks (spring-forward gap) are
marked as gaps.  Neither is silently resolved to a single instant.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MAX_OCCURRENCES = 512
_WEEKDAYS = ['MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU']
_DURATION_RE = re.compile(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$')


# --- legacy seed API (UTC cadence baseline, kept bit-compatible) -----------

def weekly_utc(start, count):
    first=datetime.fromisoformat(start.replace('Z','+00:00'))
    return [(first+timedelta(days=7*i)).isoformat() for i in range(count)]


# --- ICS text handling ------------------------------------------------------

def _unfold(text):
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out = []
    for line in lines:
        if line[:1] in (' ', '\t') and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return [l for l in out if l.strip()]


def _split_unquoted(text, sep):
    parts, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == sep and not quoted:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append(''.join(buf))
    return parts


def _split_first_unquoted(text, sep):
    quoted = False
    for i, ch in enumerate(text):
        if ch == '"':
            quoted = not quoted
        elif ch == sep and not quoted:
            return text[:i], text[i+1:]
    raise ValueError(f'bad-ics-line:{text!r}')


def _parse_prop(line):
    head, value = _split_first_unquoted(line, ':')
    parts = _split_unquoted(head, ';')
    params = {}
    for part in parts[1:]:
        k, _, v = part.partition('=')
        params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, value


def _parse_basic(value):
    for fmt in ('%Y%m%dT%H%M%S', '%Y%m%dT%H%M'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f'bad-ics-datetime:{value!r}')


def _parse_dt(value, params):
    """Parse a date-time property into a JSON-safe dict.

    Only UTC (``...Z``) and TZID-qualified local forms are accepted; bare
    floating or date-only values are refused rather than guessed.
    """
    value = value.strip()
    tzid = params.get('TZID')
    if value.endswith('Z'):
        dt = _parse_basic(value[:-1]).replace(tzinfo=timezone.utc)
        return {'form': 'utc', 'value': dt.isoformat(), 'tzid': None, 'raw': value}
    if 'T' not in value or params.get('VALUE') == 'DATE':
        raise ValueError('date-valued-time-unsupported')
    naive = _parse_basic(value)
    if tzid:
        return {'form': 'local', 'value': naive.isoformat(), 'tzid': tzid, 'raw': value}
    raise ValueError('floating-time-unsupported')


def _parse_rrule(value):
    rule = {'freq': None, 'interval': 1, 'count': None, 'until': None, 'byday': None}
    for part in value.split(';'):
        k, _, v = part.partition('=')
        k, v = k.strip().upper(), v.strip()
        if k == 'FREQ':
            rule['freq'] = v.upper()
        elif k == 'INTERVAL':
            rule['interval'] = int(v)
        elif k == 'COUNT':
            rule['count'] = int(v)
        elif k == 'UNTIL':
            rule['until'] = v
        elif k == 'BYDAY':
            rule['byday'] = v.upper()
    if rule['interval'] < 1:
        raise ValueError('bad-rrule-interval')
    return rule


def _parse_duration(value):
    m = _DURATION_RE.fullmatch(value.strip().upper())
    if not m:
        raise ValueError(f'bad-duration:{value!r}')
    days, hours, minutes = (int(g) if g else 0 for g in m.groups())
    return days * 1440 + hours * 60 + minutes


def parse_ics(text):
    """Parse an ICS stream into ``{'method': ..., 'events': [...]}``.

    Each event is a JSON-safe dict; EXDATE may repeat and may carry
    comma-separated values.  VTIMEZONE/VALARM sub-components are skipped
    (TZID values are resolved as IANA names via zoneinfo).
    """
    method, events, stack = None, [], []
    current, depth = None, 0
    for line in _unfold(text.lstrip('﻿')):
        name, params, value = _parse_prop(line)
        if name == 'BEGIN':
            tag = value.strip().upper()
            if tag == 'VEVENT' and current is None:
                current, depth = [], 0
            elif current is not None:
                depth += 1
            stack.append(tag)
            continue
        if name == 'END':
            tag = value.strip().upper()
            if tag == 'VEVENT' and current is not None and depth == 0:
                events.append(_finish_event(current))
                current = None
            elif current is not None and depth > 0:
                depth -= 1
            if stack and stack[-1] == tag:
                stack.pop()
            continue
        if current is not None and depth == 0:
            current.append((name, params, value))
        elif current is None and 'VEVENT' not in stack and name == 'METHOD':
            method = value.strip().upper()
    return {'method': method, 'events': events}


def _finish_event(props):
    ev = {'uid': None, 'sequence': 0, 'dtstamp': '', 'status': 'CONFIRMED',
          'summary': '', 'dtstart': None, 'rrule': None, 'exdates': [],
          'recurrence_id': None, 'time_mode': None, 'attest_version': None,
          'display_tz': None, 'meal': None, 'duration_minutes': None}
    for name, params, value in props:
        if name == 'UID':
            ev['uid'] = value.strip()
        elif name == 'SEQUENCE':
            ev['sequence'] = int(value.strip())
        elif name == 'DTSTAMP':
            ev['dtstamp'] = value.strip()
        elif name == 'STATUS':
            ev['status'] = value.strip().upper()
        elif name == 'SUMMARY':
            ev['summary'] = value
        elif name == 'DTSTART':
            ev['dtstart'] = _parse_dt(value, params)
        elif name == 'RRULE':
            ev['rrule'] = _parse_rrule(value)
        elif name == 'EXDATE':
            for v in value.split(','):
                ev['exdates'].append(_parse_dt(v, params))
        elif name == 'RECURRENCE-ID':
            if params.get('RANGE'):
                raise ValueError('unsupported-recurrence-range')
            ev['recurrence_id'] = _parse_dt(value, params)
        elif name == 'DURATION':
            ev['duration_minutes'] = _parse_duration(value)
        elif name == 'X-MEAL-TIME-MODE':
            ev['time_mode'] = value.strip().upper()
        elif name == 'X-MEAL-ATTEST-VERSION':
            ev['attest_version'] = int(value.strip())
        elif name == 'X-MEAL-TZID':
            ev['display_tz'] = value.strip()
        elif name == 'X-MEAL-KIND':
            ev['meal'] = value.strip()
    return ev


# --- recurrence keys and time conversion ------------------------------------

def occurrence_key(dt):
    """Canonical recurrence key, keeping the key's own time identity.

    Local keys stay naive local datetimes tagged with their TZID; UTC keys
    stay UTC instants.  A local-form EXDATE/RECURRENCE-ID can therefore
    never match a UTC-interval series and vice versa.
    """
    if dt['form'] == 'utc':
        return 'utc|' + datetime.fromisoformat(dt['value']).astimezone(timezone.utc).isoformat()
    if dt['form'] == 'local':
        return f"local|{dt['tzid']}|{dt['value']}"
    raise ValueError('unsupported-recurrence-key')


def localize(tzid, naive_iso):
    """Convert a local wall clock to UTC without ever guessing.

    Returns {'conversion': 'ok'|'ambiguous'|'gap', 'utc', 'fold',
    'candidates': [{'utc', 'fold'}, ...]}.  Ambiguous wall clocks (DST
    fall-back fold) keep both instants; nonexistent ones (spring-forward
    gap) keep none.
    """
    tz = ZoneInfo(tzid)
    naive = datetime.fromisoformat(naive_iso)
    found = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == naive and utc not in found:
            found[utc] = fold
    if not found:
        return {'conversion': 'gap', 'utc': None, 'fold': None, 'candidates': []}
    if len(found) == 1:
        utc, fold = next(iter(found.items()))
        return {'conversion': 'ok', 'utc': utc.isoformat(), 'fold': fold,
                'candidates': [{'utc': utc.isoformat(), 'fold': fold}]}
    cands = [{'utc': u.isoformat(), 'fold': f} for u, f in sorted(found.items())]
    return {'conversion': 'ambiguous', 'utc': None, 'fold': None, 'candidates': cands}


def _display_local(instant, tzid):
    if not tzid:
        return None, None
    return instant.astimezone(ZoneInfo(tzid)).replace(tzinfo=None).isoformat(), tzid


def _parse_until(raw, form):
    raw = raw.strip()
    if raw.endswith('Z'):
        if form != 'utc':
            raise ValueError('until-form-mismatch')
        return _parse_basic(raw[:-1]).replace(tzinfo=timezone.utc)
    if form != 'local':
        raise ValueError('until-form-mismatch')
    if 'T' in raw:
        return _parse_basic(raw)
    return datetime.strptime(raw, '%Y%m%d').replace(hour=23, minute=59, second=59)


def _rrule_bounds(dtstart, rrule):
    if rrule is None:
        return 1, None, timedelta(days=7)
    freq = rrule['freq']
    if freq == 'WEEKLY':
        step = timedelta(days=7 * rrule['interval'])
    elif freq == 'DAILY':
        step = timedelta(days=rrule['interval'])
    else:
        raise ValueError('unsupported-rrule-freq')
    byday = rrule['byday']
    if byday:
        start = datetime.fromisoformat(dtstart['value'])
        if freq != 'WEEKLY' or byday != _WEEKDAYS[start.weekday()]:
            raise ValueError('unsupported-byday')
    until = _parse_until(rrule['until'], dtstart['form']) if rrule['until'] else None
    if rrule['count'] is None and until is None:
        raise ValueError('unbounded-rrule')
    return rrule['count'], until, step


def _first_occurrence(dtstart, display_tz):
    if dtstart['form'] == 'utc':
        instant = datetime.fromisoformat(dtstart['value'])
        local, tzid = _display_local(instant, display_tz)
        return {'seq': 0, 'rkey': None, 'local': local, 'tzid': tzid,
                'utc': instant.isoformat(), 'fold': None, 'candidates': [],
                'conversion': 'ok'}
    conv = localize(dtstart['tzid'], dtstart['value'])
    return {'seq': 0, 'rkey': None, 'local': dtstart['value'],
            'tzid': dtstart['tzid'], **conv}


def _pending_occurrences(dtstart, rrule, display_tz):
    count = 1
    if rrule:
        if rrule['freq'] not in ('WEEKLY', 'DAILY'):
            raise ValueError('unsupported-rrule-freq')
        count = rrule['count'] or 1
    first = _first_occurrence(dtstart, display_tz)
    rest = [{'seq': i, 'rkey': None, 'local': None, 'tzid': None, 'utc': None,
             'fold': None, 'candidates': [], 'conversion': 'pending'}
            for i in range(1, count)]
    return [first] + rest


def expand_series(dtstart, rrule, time_mode, display_tz=None):
    """Expand a series into occurrence dicts under its attested semantics.

    Each occurrence carries: seq, rkey (canonical recurrence key), local
    wall clock, tzid, utc instant (or None), fold marker, candidate
    instants and the conversion outcome.  EXDATE/RECURRENCE-ID resolution
    is the ledger's job (visit_capture), not done here.
    """
    if time_mode is None:
        return _pending_occurrences(dtstart, rrule, display_tz)
    count, until, step = _rrule_bounds(dtstart, rrule)
    if time_mode == 'WALLCLOCK':
        if dtstart['form'] != 'local':
            raise ValueError('wallclock-series-needs-local-dtstart')
        tzid = dtstart['tzid']
        base = datetime.fromisoformat(dtstart['value'])
        occs, i = [], 0
        while count is None or i < count:
            local = base + step * i
            if until is not None and local > until:
                break
            conv = localize(tzid, local.isoformat())
            occs.append({'seq': i, 'rkey': f"local|{tzid}|{local.isoformat()}",
                         'local': local.isoformat(), 'tzid': tzid, **conv})
            i += 1
            if i > MAX_OCCURRENCES:
                raise ValueError('unbounded-rrule')
        return occs
    if time_mode == 'UTC-INTERVAL':
        if dtstart['form'] != 'utc':
            raise ValueError('utc-interval-series-needs-utc-dtstart')
        tz = ZoneInfo(display_tz) if display_tz else None
        base = datetime.fromisoformat(dtstart['value'])
        occs, i = [], 0
        while count is None or i < count:
            instant = base + step * i
            if until is not None and instant > until:
                break
            local = instant.astimezone(tz) if tz else None
            occs.append({'seq': i, 'rkey': f"utc|{instant.isoformat()}",
                         'local': local.replace(tzinfo=None).isoformat() if local else None,
                         'tzid': display_tz if tz else None,
                         'utc': instant.isoformat(),
                         'fold': local.fold if local else None,
                         'candidates': [], 'conversion': 'ok'})
            i += 1
            if i > MAX_OCCURRENCES:
                raise ValueError('unbounded-rrule')
        return occs
    raise ValueError('unknown-time-mode')
