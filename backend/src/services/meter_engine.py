"""电表累计读数 -> 相邻区间 -> 线体日耗的纯函数计量引擎。

口径（与 BUSINESS.md 电表核算章节一致）：
- 区间由同一电表按时刻排序的相邻读数构成；只有两端落在同一日历日的区间才贡献日耗。
- 跨日缺测标记 gap，绝不把区间差额摊到缺测的日子。
- 读数回零，或换表/复位事件缺少交接读数，区间隔离（isolated），不计任何日耗。
- 每个有效日耗严格等于两个有效累计读数之差；同表日耗之和恒等于有效读数差额。
"""
from datetime import date, datetime, timedelta
import math

EVENT_REPLACE = 'replace'
EVENT_RESET = 'reset'
EVENT_NORMAL = ('', 'normal', None)

SEGMENT_COMPLETE = 'complete'
SEGMENT_GAP = 'gap'
SEGMENT_ISOLATED = 'isolated'

DAY_PENDING = 'pending'
DAY_GAP = 'gap'
DAY_ISOLATED = 'isolated'
DAY_COMPLETE = 'complete'


class MeterValidationError(ValueError):
    pass


def parse_read_time(value):
    text = str(value).strip()
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise MeterValidationError(f'时刻 {value!r} 不是合法 ISO 日期时间') from None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def validate_readings(raw):
    """校验并按电表、时刻排序，返回归一化记录列表。"""
    if not isinstance(raw, list) or not raw:
        raise MeterValidationError('至少需要一条电表读数')
    normalized = []
    seen = set()
    for index, row in enumerate(raw):
        prefix = f'第 {index + 1} 行'
        if not isinstance(row, dict):
            raise MeterValidationError(f'{prefix} 必须是对象')
        meter_id = str(row.get('meter_id') or '').strip()
        line = str(row.get('line') or '').strip()
        if not meter_id:
            raise MeterValidationError(f'{prefix} 缺少电表编号 meter_id')
        if not line:
            raise MeterValidationError(f'{prefix} 缺少线体 line')
        moment = parse_read_time(row.get('read_time'))
        try:
            reading = float(row.get('reading'))
        except (TypeError, ValueError):
            raise MeterValidationError(f'{prefix} 累计读数 reading 必须是数值') from None
        if isinstance(row.get('reading'), bool) or not math.isfinite(reading) or reading < 0:
            raise MeterValidationError(f'{prefix} 累计读数必须是非负有限数值')
        event = row.get('event')
        event = None if event in EVENT_NORMAL else str(event).strip().lower()
        if event not in (EVENT_RESET, EVENT_REPLACE, None):
            raise MeterValidationError(f'{prefix} 事件 event 仅支持 reset/replace')
        handoff = row.get('handoff')
        if handoff not in (None, ''):
            try:
                handoff = float(handoff)
            except (TypeError, ValueError):
                raise MeterValidationError(f'{prefix} 交接读数 handoff 必须是数值') from None
            if not math.isfinite(handoff) or handoff < 0:
                raise MeterValidationError(f'{prefix} 交接读数必须是非负有限数值')
        else:
            handoff = None
        key = (meter_id, moment.isoformat())
        if key in seen:
            raise MeterValidationError(f'{prefix} 电表 {meter_id} 在 {moment.isoformat()} 存在重复读数')
        seen.add(key)
        normalized.append({'meter_id': meter_id, 'line': line, 'read_time': moment,
                           'reading': reading, 'event': event, 'handoff': handoff,
                           'read_time_str': moment.isoformat(), 'day': moment.date().isoformat()})
    normalized.sort(key=lambda r: (r['meter_id'], r['read_time']))
    return normalized


def build_segments(records):
    """按电表构建相邻区间，输出带状态与原因的区间字典列表（records 已排序）。"""
    by_meter = {}
    for position, record in enumerate(records):
        by_meter.setdefault(record['meter_id'], []).append((position, record))
    segments = []
    for meter_id, indexed_rows in by_meter.items():
        indexed_rows.sort(key=lambda pair: pair[1]['read_time'])
        for (prev_pos, previous), (cur_pos, current) in zip(indexed_rows, indexed_rows[1:]):
            calendar_distance = (current['read_time'].date() - previous['read_time'].date()).days
            base = {
                'meter_id': meter_id,
                'line': current['line'],
                'start_time': previous['read_time_str'],
                'end_time': current['read_time_str'],
                'start_reading': previous['reading'],
                'end_reading': current['reading'],
                'start_day': previous['day'],
                'end_day': current['day'],
                'start_index': prev_pos,
                'end_index': cur_pos,
            }
            rollback = current['reading'] < previous['reading']
            change_event = current['event'] in (EVENT_RESET, EVENT_REPLACE)
            missing_handoff = change_event and current['handoff'] is None
            # 带交接读数的换表/复位：新表表底为 0 是正常的，旧表差额由交接读数承接。
            if (rollback and not (change_event and current['handoff'] is not None)) or missing_handoff:
                if missing_handoff:
                    reason = f"{current['event']} 事件缺少交接读数，区间隔离"
                else:
                    reason = '累计读数回零/倒退，缺少可信交接读数，区间隔离'
                segments.append({**base, 'delta': None, 'status': SEGMENT_ISOLATED, 'reason': reason})
                continue
            if change_event:
                # 有交接读数：交接表底数给出了旧表末值，旧表区间差额仍可成立。
                base['end_reading'] = current['handoff']
                if current['handoff'] < previous['reading']:
                    segments.append({**base, 'delta': None, 'status': SEGMENT_ISOLATED,
                                     'reason': '交接读数早于前一读数，区间隔离'})
                    continue
            delta = round(base['end_reading'] - base['start_reading'], 6)
            if calendar_distance > 1:
                segments.append({**base, 'delta': delta, 'status': SEGMENT_GAP,
                                 'reason': f"跨日缺测：{previous['day']} 至 {current['day']} 相隔 {calendar_distance} 天且中间无读数，差额不均摊"})
            else:
                segments.append({**base, 'delta': delta, 'status': SEGMENT_COMPLETE,
                                 'reason': '相邻抄表周期的有效读数差额'})
    segments.sort(key=lambda s: (s['meter_id'], s['start_time']))
    return segments


def enumerate_days(start_day, end_day):
    start = date.fromisoformat(start_day)
    end = date.fromisoformat(end_day)
    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def build_daily_view(records, segments):
    """汇总每块电表覆盖期内每个日历日的状态与日耗（不写库，供草稿预览与发布共用）。

    归属口径：日耗归属区间末端读数所在日（当日首读数 − 此前最近有效读数）；
    同一日历日有多段时累加。gap 跳过的中间日标 gap 且 kwh=None，绝不均摊。
    """
    by_meter = {}
    for record in records:
        by_meter.setdefault(record['meter_id'], []).append(record)
    view = {}
    for meter_id, rows in by_meter.items():
        rows.sort(key=lambda r: r['read_time'])
        meter_segments = [s for s in segments if s['meter_id'] == meter_id]
        # 首读数日没有前序读数，无法形成日耗；其余覆盖日先置 pending。
        days = enumerate_days(rows[0]['day'], rows[-1]['day'])[1:]
        day_map = {day: {'status': DAY_PENDING, 'kwh': 0.0, 'basis': []} for day in days}
        for segment in meter_segments:
            if segment['status'] == SEGMENT_COMPLETE:
                entry = day_map.setdefault(segment['end_day'], {'status': DAY_COMPLETE, 'kwh': 0.0, 'basis': []})
                entry['status'] = DAY_COMPLETE
                entry['kwh'] = round(entry['kwh'] + segment['delta'], 6)
                entry['basis'].append(segment)
            elif segment['status'] == SEGMENT_ISOLATED:
                entry = day_map.setdefault(segment['end_day'], {'status': DAY_ISOLATED, 'kwh': None, 'basis': []})
                if entry['status'] != DAY_COMPLETE:
                    entry['status'] = DAY_ISOLATED
                    entry['kwh'] = None
                entry['basis'].append(segment)
            else:  # gap：缺测覆盖到的内部日期全部标 gap，不摊差额
                for missing in enumerate_days(segment['start_day'], segment['end_day']):
                    entry = day_map.setdefault(missing, {'status': DAY_GAP, 'kwh': None, 'basis': []})
                    if entry['status'] == DAY_PENDING:
                        entry['status'] = DAY_GAP
                        entry['kwh'] = None
                    entry['basis'].append(segment)
        view[meter_id] = day_map
    return view


def reconciliation(records, segments):
    """对账：每块电表 complete 日耗之和必须等于相邻有效读数差额的可核对口径。

    采用区间口径：complete 区间 delta 之和（隔离与跨日缺测区间不参与），
    与日耗视图累加值逐表核对；另给出首尾有效读数差额与被排除区间差额，
    保证人工可以解释每一度电的去向。
    """
    report = {}
    by_meter = {}
    for record in records:
        by_meter.setdefault(record['meter_id'], []).append(record)
    daily_view = build_daily_view(records, segments)
    for meter_id, rows in by_meter.items():
        rows.sort(key=lambda r: r['read_time'])
        meter_segments = [s for s in segments if s['meter_id'] == meter_id]
        complete_sum = round(sum(s['delta'] for s in meter_segments if s['status'] == SEGMENT_COMPLETE), 6)
        daily_sum = round(sum(day['kwh'] for day in daily_view[meter_id].values()
                              if day['status'] == DAY_COMPLETE), 6)
        excluded_gap = round(sum(s['delta'] for s in meter_segments if s['status'] == SEGMENT_GAP), 6)
        isolated = sum(1 for s in meter_segments if s['status'] == SEGMENT_ISOLATED)
        balanced = abs(complete_sum - daily_sum) < 1e-6
        report[meter_id] = {
            'first_time': rows[0]['read_time_str'], 'last_time': rows[-1]['read_time_str'],
            'first_reading': rows[0]['reading'], 'last_reading': rows[-1]['reading'],
            'complete_kwh': complete_sum, 'daily_kwh': daily_sum,
            'excluded_gap_kwh': excluded_gap, 'isolated_segments': isolated,
            'balanced': balanced,
        }
    return report


def build_preview(records):
    """草稿批次预览：区间分类、逐日状态计数、对账与影响日期。"""
    segments = build_segments(records)
    view = build_daily_view(records, segments)
    recon = reconciliation(records, segments)
    meters = sorted({r['meter_id'] for r in records})
    lines = sorted({r['line'] for r in records})
    affected_days = sorted({day for meter_days in view.values() for day in meter_days})
    counts = {SEGMENT_COMPLETE: 0, SEGMENT_GAP: 0, SEGMENT_ISOLATED: 0}
    day_counts = {DAY_COMPLETE: 0, DAY_GAP: 0, DAY_ISOLATED: 0, DAY_PENDING: 0}
    for segment in segments:
        counts[segment['status']] += 1
    for meter_days in view.values():
        for entry in meter_days.values():
            day_counts[entry['status']] += 1
    publishable = all(item['balanced'] for item in recon.values())
    meter_summary = []
    for meter_id in meters:
        meter_rows = [r for r in records if r['meter_id'] == meter_id]
        meter_summary.append({
            'meter_id': meter_id, 'line': meter_rows[-1]['line'],
            'readings': len(meter_rows), **recon[meter_id],
            'days': len(view[meter_id]),
            'pending_days': [d for d, v in view[meter_id].items() if v['status'] == DAY_PENDING],
            'gap_days': [d for d, v in view[meter_id].items() if v['status'] == DAY_GAP],
            'isolated_days': [d for d, v in view[meter_id].items() if v['status'] == DAY_ISOLATED],
        })
    return {
        'meters': meters, 'lines': lines, 'segments': segments, 'daily_view': view,
        'segment_counts': counts, 'day_counts': day_counts,
        'reconciliation': recon, 'affected_days': affected_days,
        'publishable': publishable, 'meter_summary': meter_summary,
    }
