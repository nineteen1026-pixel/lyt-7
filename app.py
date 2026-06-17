import csv
import io
import math
import os
import re
import sqlite3
import urllib.request
import urllib.parse
import json
import uuid
import shutil
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_from_directory

app = Flask(__name__)
app.secret_key = 'fishing_log_secret_key'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fishing_log.db')
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

WEATHER_API_CONFIG = {
    'city': '北京',
    'timeout': 5,
}

ASTRO_API_CONFIG = {
    'timeout': 8,
    'base_url': 'https://api.sunrise-sunset.org/json',
}

MOON_PHASE_NAMES = {
    0: '新月',
    1: '蛾眉月',
    2: '上弦月',
    3: '盈凸月',
    4: '满月',
    5: '亏凸月',
    6: '下弦月',
    7: '残月',
}

MOON_PHASE_ICONS = {
    0: '🌑',
    1: '🌒',
    2: '🌓',
    3: '🌔',
    4: '🌕',
    5: '🌖',
    6: '🌗',
    7: '🌘',
}

TIDE_TYPES = {
    'spring': '大潮',
    'neap': '小潮',
    'normal': '中潮',
}


def calculate_moon_phase(date_str):
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None

    ref_new_moon = datetime(2000, 1, 6, 18, 14).date()
    synodic_period = 29.53058867
    days_since = (target_date - ref_new_moon).days
    lunar_age = days_since % synodic_period
    illumination = (1 - math.cos(2 * math.pi * lunar_age / synodic_period)) / 2

    phase_index = int((lunar_age / synodic_period) * 8) % 8

    lunar_day = int(lunar_age) + 1
    if lunar_day > 30:
        lunar_day = 30

    if phase_index in (0, 4):
        tide_type = 'spring'
    elif phase_index in (2, 6):
        tide_type = 'neap'
    else:
        tide_type = 'normal'

    return {
        'moon_phase': MOON_PHASE_NAMES.get(phase_index, '未知'),
        'moon_phase_icon': MOON_PHASE_ICONS.get(phase_index, '🌙'),
        'moon_illumination': round(illumination * 100, 1),
        'lunar_day': lunar_day,
        'lunar_age': round(lunar_age, 2),
        'tide_type': tide_type,
        'tide_name': TIDE_TYPES.get(tide_type, '未知'),
        'phase_index': phase_index,
    }


def fetch_external_astro_data(date_str, lat=None, lng=None):
    external_data = {}
    try:
        params = urllib.parse.urlencode({
            'date': date_str,
            'formatted': 0,
        })
        if lat and lng:
            params += f'&lat={lat}&lng={lng}'
        else:
            params += '&lat=39.9042&lng=116.4074'
        url = f"{ASTRO_API_CONFIG['base_url']}?{params}"
        req = urllib.request.Request(url, headers={'User-Agent': 'FishingLog/1.0'})
        with urllib.request.urlopen(req, timeout=ASTRO_API_CONFIG['timeout']) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('status') == 'OK' and data.get('results'):
            results = data['results']
            for key in ['sunrise', 'sunset', 'moonrise', 'moonset',
                        'civil_twilight_begin', 'civil_twilight_end',
                        'nautical_twilight_begin', 'nautical_twilight_end']:
                val = results.get(key)
                if val and val != 'None':
                    try:
                        dt = datetime.strptime(val, '%Y-%m-%dT%H:%M:%S%z')
                        local_dt = dt.astimezone()
                        external_data[key] = local_dt.strftime('%H:%M')
                    except (ValueError, OSError):
                        external_data[key] = val
                else:
                    external_data[key] = None

            day_length = results.get('day_length')
            if day_length:
                try:
                    total_seconds = int(day_length)
                    hours, remainder = divmod(total_seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    external_data['day_length'] = f'{hours}小时{minutes}分钟'
                except (ValueError, TypeError):
                    external_data['day_length'] = str(day_length)
            else:
                sr = results.get('sunrise')
                ss = results.get('sunset')
                if sr and ss and sr != 'None' and ss != 'None':
                    try:
                        sr_dt = datetime.strptime(sr, '%Y-%m-%dT%H:%M:%S%z')
                        ss_dt = datetime.strptime(ss, '%Y-%m-%dT%H:%M:%S%z')
                        diff = ss_dt - sr_dt
                        hours, remainder = divmod(int(diff.total_seconds()), 3600)
                        minutes, _ = divmod(remainder, 60)
                        external_data['day_length'] = f'{hours}小时{minutes}分钟'
                    except (ValueError, OSError):
                        external_data['day_length'] = None
    except Exception:
        pass
    return external_data


def calculate_fishing_index(astro_data, weather_data=None):
    if not astro_data:
        return 5, '无法计算适钓指数'

    score = 5
    reasons = []
    phase_index = astro_data.get('phase_index', -1)
    tide_type = astro_data.get('tide_type', 'normal')
    illumination = astro_data.get('moon_illumination', 50)

    if phase_index in (1, 7):
        score += 1
        reasons.append('蛾眉月/残月期，鱼类觅食活跃度较高')
    elif phase_index in (2, 6):
        score += 1
        reasons.append('上下弦月期，潮汐变化明显，鱼口较好')
    elif phase_index == 0:
        score -= 1
        reasons.append('新月期，光线极弱，部分鱼种活跃度下降')
    elif phase_index == 4:
        score += 0
        reasons.append('满月期，夜间光线充足，鱼可能在夜间已觅食')

    if tide_type == 'spring':
        score += 1
        reasons.append('大潮期，水流强劲，鱼类随潮觅食积极')
    elif tide_type == 'neap':
        score -= 1
        reasons.append('小潮期，水流平缓，鱼类活动范围缩小')

    if 30 <= illumination <= 70:
        score += 1
        reasons.append('月光适中，有利于夜钓和晨昏时段作钓')

    if weather_data:
        weather = (weather_data.get('weather', '') or '').lower()
        temp_str = weather_data.get('temperature', '') or ''
        wind_str = weather_data.get('wind', '') or ''

        if any(w in weather for w in ['阴', '多云', 'cloudy']):
            score += 1
            reasons.append('阴天/多云天气，光线柔和，鱼更敢靠岸觅食')
        elif '晴' in weather or 'sunny' in weather:
            score += 0
            reasons.append('晴天光照强烈，建议选择晨昏时段出钓')

        if '雨' in weather:
            if '大' in weather or '暴' in weather:
                score -= 2
                reasons.append('大雨/暴雨天气，不宜出钓')
            elif '小' in weather or '阵' in weather:
                score += 1
                reasons.append('小雨/阵雨天气，水中溶氧增加，鱼口活跃')

        temp_val = 0
        temp_match = re.search(r'(\d+)', temp_str)
        if temp_match:
            temp_val = int(temp_match.group(1))
            if 18 <= temp_val <= 28:
                score += 1
                reasons.append(f'气温{temp_val}℃处于鱼类适宜觅食温度区间')
            elif temp_val < 10:
                score -= 1
                reasons.append(f'气温{temp_val}℃偏低，鱼类活性降低')
            elif temp_val > 35:
                score -= 1
                reasons.append(f'气温{temp_val}℃偏高，鱼类潜入深水避暑')

        wind_speed = 0
        ws_match = re.search(r'(\d+)', wind_str)
        if ws_match:
            wind_speed = int(ws_match.group(1))
        if 5 <= wind_speed <= 20:
            score += 1
            reasons.append('微风天气，水面溶氧充足，利于垂钓')
        elif wind_speed > 30:
            score -= 1
            reasons.append('风力较大，影响观漂和抛竿')

    score = max(1, min(10, round(score)))

    if not reasons:
        reasons.append('天文条件一般，结合经验选择钓位和饵料')

    return score, '；'.join(reasons)


def get_astro_summary(astro_data, fishing_index, fishing_reason):
    if not astro_data:
        return '暂无天文数据'

    parts = []
    phase = astro_data.get('moon_phase', '未知')
    icon = astro_data.get('moon_phase_icon', '🌙')
    illumination = astro_data.get('moon_illumination', 0)
    tide_name = astro_data.get('tide_name', '未知')
    lunar_day = astro_data.get('lunar_day', 0)

    parts.append(f'当日月相为{icon}{phase}，月面照明度{illumination}%')
    parts.append(f'农历初{lunar_day}，潮汐类型为{tide_name}')

    sunrise = astro_data.get('sunrise')
    sunset = astro_data.get('sunset')
    day_length = astro_data.get('day_length')
    if sunrise and sunset:
        parts.append(f'日出{sunrise}，日落{sunset}，白昼时长{day_length or "未知"}')

    moonrise = astro_data.get('moonrise')
    moonset = astro_data.get('moonset')
    if moonrise or moonset:
        moon_times = []
        if moonrise:
            moon_times.append(f'月出{moonrise}')
        if moonset:
            moon_times.append(f'月落{moonset}')
        parts.append('，'.join(moon_times))

    if fishing_index >= 8:
        parts.append('综合天文条件非常有利，是出钓的好时机')
    elif fishing_index >= 6:
        parts.append('天文条件尚可，配合合适的饵料和钓位可有收获')
    elif fishing_index >= 4:
        parts.append('天文条件一般，需要更耐心地等待和更精准的策略')
    else:
        parts.append('天文条件欠佳，建议谨慎出钓或择日再战')

    return '。'.join(parts)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table_name, column_name):
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    columns = [row['name'] for row in cursor.fetchall()]
    return column_name in columns


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fishing_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot TEXT NOT NULL,
            weather TEXT NOT NULL,
            water_level TEXT NOT NULL,
            bait TEXT NOT NULL,
            fish_species TEXT NOT NULL,
            harvest TEXT NOT NULL,
            next_strategy TEXT,
            created_at DATE NOT NULL
        )
    ''')

    if not column_exists(conn, 'fishing_logs', 'temperature'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN temperature TEXT')
    if not column_exists(conn, 'fishing_logs', 'humidity'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN humidity TEXT')
    if not column_exists(conn, 'fishing_logs', 'wind'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN wind TEXT')
    if not column_exists(conn, 'fishing_logs', 'next_strategy_date'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN next_strategy_date DATE')
    if not column_exists(conn, 'fishing_logs', 'moon_phase'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN moon_phase TEXT')
    if not column_exists(conn, 'fishing_logs', 'moon_illumination'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN moon_illumination REAL')
    if not column_exists(conn, 'fishing_logs', 'lunar_day'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN lunar_day INTEGER')
    if not column_exists(conn, 'fishing_logs', 'tide_type'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN tide_type TEXT')
    if not column_exists(conn, 'fishing_logs', 'fishing_index'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN fishing_index INTEGER')
    if not column_exists(conn, 'fishing_logs', 'astro_description'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN astro_description TEXT')
    if not column_exists(conn, 'fishing_logs', 'sunrise'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN sunrise TEXT')
    if not column_exists(conn, 'fishing_logs', 'sunset'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN sunset TEXT')
    if not column_exists(conn, 'fishing_logs', 'moonrise'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN moonrise TEXT')
    if not column_exists(conn, 'fishing_logs', 'moonset'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN moonset TEXT')
    if not column_exists(conn, 'fishing_logs', 'day_length'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN day_length TEXT')
    if not column_exists(conn, 'fishing_logs', 'civil_twilight_begin'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN civil_twilight_begin TEXT')
    if not column_exists(conn, 'fishing_logs', 'civil_twilight_end'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN civil_twilight_end TEXT')
    if not column_exists(conn, 'fishing_logs', 'fishing_reason'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN fishing_reason TEXT')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fishing_spots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            latitude REAL,
            longitude REAL,
            address TEXT,
            is_favorite INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS spot_ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (spot_id) REFERENCES fishing_spots(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS baits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT,
            brand TEXT,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id INTEGER NOT NULL,
            batch_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (log_id) REFERENCES fishing_logs(id) ON DELETE CASCADE
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS fishing_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot TEXT NOT NULL,
            date DATE NOT NULL,
            notes TEXT,
            status TEXT DEFAULT 'planned',
            total_cost REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS invitation_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            harvest_detail TEXT,
            cost_share REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invitation_id) REFERENCES fishing_invitations(id) ON DELETE CASCADE
        )
    ''')
    if not column_exists(conn, 'fishing_invitations', 'total_harvest'):
        conn.execute('ALTER TABLE fishing_invitations ADD COLUMN total_harvest TEXT')
    if not column_exists(conn, 'audit_logs', 'batch_id'):
        conn.execute('ALTER TABLE audit_logs ADD COLUMN batch_id TEXT DEFAULT \'legacy\' NOT NULL')
        conn.execute('UPDATE audit_logs SET batch_id = \'legacy\' WHERE batch_id IS NULL OR batch_id = \'\'')
    if not column_exists(conn, 'fishing_logs', 'deleted_at'):
        conn.execute('ALTER TABLE fishing_logs ADD COLUMN deleted_at TIMESTAMP')
    if not column_exists(conn, 'fishing_spots', 'deleted_at'):
        conn.execute('ALTER TABLE fishing_spots ADD COLUMN deleted_at TIMESTAMP')
    if not column_exists(conn, 'baits', 'deleted_at'):
        conn.execute('ALTER TABLE baits ADD COLUMN deleted_at TIMESTAMP')
    if not column_exists(conn, 'fishing_invitations', 'deleted_at'):
        conn.execute('ALTER TABLE fishing_invitations ADD COLUMN deleted_at TIMESTAMP')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS equipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            brand TEXT,
            model TEXT,
            spec TEXT,
            unit_price REAL DEFAULT 0,
            total_quantity INTEGER DEFAULT 0,
            available_quantity INTEGER DEFAULT 0,
            purchase_date DATE,
            supplier TEXT,
            description TEXT,
            lifespan_months INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS equipment_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            equipment_id INTEGER NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('in', 'out', 'adjust', 'discard')),
            quantity INTEGER NOT NULL,
            unit_price REAL,
            total_cost REAL,
            related_log_id INTEGER,
            operator TEXT,
            reason TEXT,
            transaction_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (equipment_id) REFERENCES equipments(id) ON DELETE CASCADE,
            FOREIGN KEY (related_log_id) REFERENCES fishing_logs(id) ON DELETE SET NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS log_equipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id INTEGER NOT NULL,
            equipment_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            usage_cost REAL DEFAULT 0,
            wear_rate REAL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (log_id) REFERENCES fishing_logs(id) ON DELETE CASCADE,
            FOREIGN KEY (equipment_id) REFERENCES equipments(id) ON DELETE CASCADE
        )
    ''')

    if not column_exists(conn, 'equipments', 'deleted_at'):
        conn.execute('ALTER TABLE equipments ADD COLUMN deleted_at TIMESTAMP')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS log_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            original_name TEXT,
            caption TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (log_id) REFERENCES fishing_logs(id) ON DELETE CASCADE
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS spot_wiki (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot_id INTEGER,
            spot_name TEXT NOT NULL,
            road_condition TEXT,
            parking_info TEXT,
            parking_fee TEXT,
            fishing_fee TEXT,
            ban_info TEXT,
            best_time TEXT,
            water_features TEXT,
            suitable_methods TEXT,
            facilities TEXT,
            safety_notes TEXT,
            tips TEXT,
            source TEXT,
            info_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP,
            FOREIGN KEY (spot_id) REFERENCES fishing_spots(id) ON DELETE SET NULL
        )
    ''')

    conn.commit()
    conn.close()


FIELD_LABELS = {
    'spot': '钓点',
    'weather': '天气',
    'water_level': '水位',
    'bait': '饵料',
    'fish_species': '鱼种',
    'harvest': '收获情况',
    'next_strategy': '下次策略',
    'next_strategy_date': '策略执行日期',
    'created_at': '日期',
    'temperature': '温度',
    'humidity': '湿度',
    'wind': '风力',
    'moon_phase': '月相',
    'moon_illumination': '月亮照明度',
    'lunar_day': '农历日',
    'tide_type': '潮汐类型',
    'fishing_index': '适钓指数',
    'astro_description': '天文环境说明',
    'sunrise': '日出时间',
    'sunset': '日落时间',
    'moonrise': '月出时间',
    'moonset': '月落时间',
    'day_length': '白昼时长',
    'civil_twilight_begin': '晨曦开始',
    'civil_twilight_end': '暮光结束',
    'fishing_reason': '适钓分析',
}

EDITABLE_FIELDS = [
    'spot', 'weather', 'water_level', 'bait', 'fish_species',
    'harvest', 'next_strategy', 'next_strategy_date', 'created_at', 'temperature',
    'humidity', 'wind'
]


def _norm(value):
    if value is None:
        return ''
    return value


def record_audit_log(conn, log_id, batch_id, field_name, old_value, new_value):
    old_norm = _norm(old_value)
    new_norm = _norm(new_value)
    if old_norm != new_norm:
        conn.execute('''
            INSERT INTO audit_logs (log_id, batch_id, field_name, old_value, new_value)
            VALUES (?, ?, ?, ?, ?)
        ''', (log_id, batch_id, field_name, old_norm or None, new_norm or None))


def get_audit_logs(conn, log_id):
    rows = conn.execute('''
        SELECT * FROM audit_logs
        WHERE log_id = ?
        ORDER BY changed_at DESC, id DESC
    ''', (log_id,)).fetchall()

    logs = []
    group_map = {}
    for row in rows:
        batch = row['batch_id']
        if batch not in group_map:
            group_map[batch] = {
                'timestamp': row['changed_at'],
                'changes': [],
                '_id': row['id']
            }
            logs.append(group_map[batch])
        else:
            if row['id'] > group_map[batch]['_id']:
                group_map[batch]['_id'] = row['id']
        group_map[batch]['changes'].append({
            'field': row['field_name'],
            'field_label': FIELD_LABELS.get(row['field_name'], row['field_name']),
            'old_value': row['old_value'],
            'new_value': row['new_value']
        })
    logs.sort(key=lambda g: g['_id'], reverse=True)
    for g in logs:
        del g['_id']
    return logs


@app.route('/log/<int:log_id>/edit', methods=['GET', 'POST'])
def edit_log(log_id):
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort', 'date_desc')
    per_page = request.args.get('per_page', 20, type=int)

    conn = get_db()
    log = conn.execute('SELECT * FROM fishing_logs WHERE id = ?', (log_id,)).fetchone()
    bait_list = get_all_baits(conn)
    common_species = get_common_species(conn)
    harvest_templates = get_harvest_templates(conn)
    equipment_list = get_all_equipments(conn)
    log_equipments, total_eq_cost = get_log_equipments(conn, log_id)
    log_photos = get_log_photos(conn, log_id)
    common_weathers = get_common_weathers(conn)
    common_water_levels = get_common_water_levels(conn)
    normalized_weather = normalize_weather_desc(log['weather']) if log else ''
    normalized_water_level = normalize_water_level(log['water_level']) if log else ''

    if log is None:
        conn.close()
        flash('记录不存在！', 'error')
        return redirect(url_for('index', page=page, sort=sort_by, per_page=per_page))

    if request.method == 'POST':
        spot = request.form['spot'].strip()
        weather = request.form['weather'].strip()
        water_level = request.form['water_level'].strip()
        bait = request.form['bait'].strip()
        fish_species = request.form['fish_species'].strip()
        harvest = request.form['harvest'].strip()
        next_strategy = request.form['next_strategy'].strip()
        next_strategy_date = request.form.get('next_strategy_date', '').strip()
        created_at = request.form['created_at'] or datetime.now().strftime('%Y-%m-%d')
        temperature = request.form.get('temperature', '').strip()
        humidity = request.form.get('humidity', '').strip()
        wind = request.form.get('wind', '').strip()

        eq_ids = request.form.getlist('equipment_id[]')
        eq_qtys = request.form.getlist('equipment_qty[]')
        eq_wears = request.form.getlist('equipment_wear[]')
        eq_notes = request.form.getlist('equipment_note[]')

        equipment_data = []
        for i in range(len(eq_ids)):
            try:
                eid = int(eq_ids[i]) if eq_ids[i] else None
                eqty = int(eq_qtys[i]) if i < len(eq_qtys) and eq_qtys[i] else 1
                ewear = float(eq_wears[i]) if i < len(eq_wears) and eq_wears[i] else 0.05
                enote = eq_notes[i] if i < len(eq_notes) else ''
                equipment_data.append({
                    'equipment_id': eid,
                    'quantity': eqty,
                    'wear_rate': ewear,
                    'notes': enote
                })
            except (ValueError, IndexError):
                continue

        if not spot or not weather or not water_level or not bait or not fish_species or not harvest:
            flash('请填写所有必填项！', 'error')
        else:
            moon_phase = request.form.get('moon_phase', '').strip()
            moon_illumination = request.form.get('moon_illumination', '').strip()
            lunar_day = request.form.get('lunar_day', '').strip()
            tide_type = request.form.get('tide_type', '').strip()
            fishing_index = request.form.get('fishing_index', '').strip()
            astro_description = request.form.get('astro_description', '').strip()
            sunrise = request.form.get('sunrise', '').strip()
            sunset_val = request.form.get('sunset', '').strip()
            moonrise = request.form.get('moonrise', '').strip()
            moonset = request.form.get('moonset', '').strip()
            day_length = request.form.get('day_length', '').strip()
            civil_twilight_begin = request.form.get('civil_twilight_begin', '').strip()
            civil_twilight_end = request.form.get('civil_twilight_end', '').strip()
            fishing_reason = request.form.get('fishing_reason', '').strip()

            if not moon_phase:
                astro_data = calculate_moon_phase(created_at)
                if astro_data:
                    external_data = fetch_external_astro_data(created_at)
                    if external_data:
                        astro_data.update(external_data)
                    weather_data = {
                        'weather': weather,
                        'temperature': temperature,
                        'wind': wind,
                    }
                    fi, fr = calculate_fishing_index(astro_data, weather_data)
                    moon_phase = astro_data['moon_phase']
                    moon_illumination = astro_data['moon_illumination']
                    lunar_day = astro_data['lunar_day']
                    tide_type = astro_data['tide_type']
                    fishing_index = fi
                    fishing_reason = fr
                    astro_description = get_astro_summary(astro_data, fi, fr)
                    sunrise = astro_data.get('sunrise', '') or ''
                    sunset_val = astro_data.get('sunset', '') or ''
                    moonrise = astro_data.get('moonrise', '') or ''
                    moonset = astro_data.get('moonset', '') or ''
                    day_length = astro_data.get('day_length', '') or ''
                    civil_twilight_begin = astro_data.get('civil_twilight_begin', '') or ''
                    civil_twilight_end = astro_data.get('civil_twilight_end', '') or ''

            old_data = dict(log)
            new_data = {
                'spot': spot,
                'weather': weather,
                'water_level': water_level,
                'bait': bait,
                'fish_species': fish_species,
                'harvest': harvest,
                'next_strategy': next_strategy,
                'next_strategy_date': next_strategy_date or None,
                'created_at': created_at,
                'temperature': temperature or None,
                'humidity': humidity or None,
                'wind': wind or None,
                'moon_phase': moon_phase or None,
                'moon_illumination': float(moon_illumination) if moon_illumination else None,
                'lunar_day': int(lunar_day) if lunar_day else None,
                'tide_type': tide_type or None,
                'fishing_index': int(fishing_index) if fishing_index else None,
                'astro_description': astro_description or None,
                'sunrise': sunrise or None,
                'sunset': sunset_val or None,
                'moonrise': moonrise or None,
                'moonset': moonset or None,
                'day_length': day_length or None,
                'civil_twilight_begin': civil_twilight_begin or None,
                'civil_twilight_end': civil_twilight_end or None,
                'fishing_reason': fishing_reason or None,
            }

            batch_id = str(uuid.uuid4())
            for field in EDITABLE_FIELDS:
                old_val = old_data.get(field)
                new_val = new_data.get(field)
                record_audit_log(conn, log_id, batch_id, field, old_val, new_val)

            conn.execute('''
                UPDATE fishing_logs
                SET spot = ?, weather = ?, water_level = ?, bait = ?, fish_species = ?,
                    harvest = ?, next_strategy = ?, next_strategy_date = ?, created_at = ?, temperature = ?,
                    humidity = ?, wind = ?, moon_phase = ?, moon_illumination = ?, lunar_day = ?,
                    tide_type = ?, fishing_index = ?, astro_description = ?,
                    sunrise = ?, sunset = ?, moonrise = ?, moonset = ?, day_length = ?,
                    civil_twilight_begin = ?, civil_twilight_end = ?, fishing_reason = ?
                WHERE id = ?
            ''', (spot, weather, water_level, bait, fish_species, harvest,
                  next_strategy, next_strategy_date or None, created_at, temperature or None, humidity or None,
                  wind or None, moon_phase or None, float(moon_illumination) if moon_illumination else None,
                  int(lunar_day) if lunar_day else None, tide_type or None,
                  int(fishing_index) if fishing_index else None, astro_description or None,
                  sunrise or None, sunset_val or None, moonrise or None, moonset or None,
                  day_length or None, civil_twilight_begin or None, civil_twilight_end or None,
                  fishing_reason or None, log_id))

            save_log_equipments(conn, log_id, equipment_data)

            photos = request.files.getlist('photos')
            photo_count = save_log_photos(conn, log_id, photos)

            conn.commit()
            if photo_count > 0:
                flash(f'记录更新成功！已新增 {photo_count} 张照片', 'success')
            else:
                flash('记录更新成功！', 'success')
            conn.close()
            return redirect(url_for('log_detail', log_id=log_id, page=page, sort=sort_by, per_page=per_page))

    conn.close()
    return render_template('edit.html', log=log, bait_list=bait_list,
                           page=page, sort_by=sort_by, per_page=per_page,
                           default_city=WEATHER_API_CONFIG['city'],
                           common_species=common_species, harvest_templates=harvest_templates,
                           equipment_list=equipment_list, log_equipments=log_equipments,
                           total_equipment_cost=total_eq_cost,
                           log_photos=log_photos,
                           common_weathers=common_weathers,
                           common_water_levels=common_water_levels,
                           normalized_weather=normalized_weather,
                           normalized_water_level=normalized_water_level)


def get_upcoming_strategies(conn):
    today = datetime.now().date()
    three_days_later = today + timedelta(days=3)
    
    upcoming_strategies = conn.execute('''
        SELECT id, spot, next_strategy, next_strategy_date, created_at, harvest
        FROM fishing_logs
        WHERE deleted_at IS NULL
          AND next_strategy IS NOT NULL
          AND next_strategy != ''
          AND next_strategy_date IS NOT NULL
          AND next_strategy_date >= ?
          AND next_strategy_date <= ?
        ORDER BY next_strategy_date ASC, id ASC
    ''', (today.strftime('%Y-%m-%d'), three_days_later.strftime('%Y-%m-%d'))).fetchall()
    
    result = []
    for s in upcoming_strategies:
        strategy_date = datetime.strptime(s['next_strategy_date'], '%Y-%m-%d').date()
        days_until = (strategy_date - today).days
        result.append({
            'id': s['id'],
            'spot': s['spot'],
            'next_strategy': s['next_strategy'],
            'next_strategy_date': s['next_strategy_date'],
            'created_at': s['created_at'],
            'harvest': s['harvest'],
            'days_until': days_until,
            'is_today': days_until == 0,
            'is_tomorrow': days_until == 1,
        })
    return result


def get_overview_stats(conn):
    total_logs = conn.execute(
        'SELECT COUNT(*) as cnt FROM fishing_logs WHERE deleted_at IS NULL'
    ).fetchone()['cnt']

    active_spots = conn.execute(
        'SELECT COUNT(DISTINCT spot) as cnt FROM fishing_logs WHERE spot IS NOT NULL AND spot != "" AND deleted_at IS NULL'
    ).fetchone()['cnt']

    today = datetime.now().date()
    seven_months_ago = today - timedelta(days=210)

    monthly_counts = conn.execute('''
        SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as count
        FROM fishing_logs
        WHERE deleted_at IS NULL
          AND created_at >= ?
        GROUP BY month
        ORDER BY month DESC
    ''', (seven_months_ago.strftime('%Y-%m-%d'),)).fetchall()

    monthly_map = {row['month']: row['count'] for row in monthly_counts}

    last_seven_months = []
    for i in range(6, -1, -1):
        month_date = today.replace(day=1) - timedelta(days=i * 30)
        month_str = month_date.strftime('%Y-%m')
        display_month = month_date.strftime('%m月')
        last_seven_months.append({
            'month': month_str,
            'display': display_month,
            'count': monthly_map.get(month_str, 0)
        })

    recent_seven_month_total = sum(m['count'] for m in last_seven_months)
    avg_per_month = round(recent_seven_month_total / 7, 1) if recent_seven_month_total > 0 else 0
    max_month_count = max((m['count'] for m in last_seven_months), default=1)

    return {
        'total_logs': total_logs,
        'active_spots': active_spots,
        'last_seven_months': last_seven_months,
        'avg_per_month': avg_per_month,
        'seven_month_total': recent_seven_month_total,
        'max_month_count': max_month_count
    }


@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    sort_by = request.args.get('sort', 'date_desc')
    search_query = request.args.get('q', '').strip()

    per_page = max(10, min(per_page, 100))

    sort_options = {
        'date_desc': ('created_at DESC, id DESC', '日期降序'),
        'date_asc': ('created_at ASC, id ASC', '日期升序'),
        'spot_desc': ('spot DESC, created_at DESC, id DESC', '钓点降序'),
        'spot_asc': ('spot ASC, created_at DESC, id DESC', '钓点升序'),
    }
    order_clause, sort_label = sort_options.get(sort_by, sort_options['date_desc'])

    conn = get_db()

    upcoming_strategies = get_upcoming_strategies(conn)
    overview_stats = get_overview_stats(conn)

    conditions = ['deleted_at IS NULL']
    params = []

    if search_query:
        search_pattern = f'%{search_query}%'
        conditions.append('(spot LIKE ? OR bait LIKE ? OR fish_species LIKE ? OR harvest LIKE ?)')
        params.extend([search_pattern, search_pattern, search_pattern, search_pattern])

    where_clause = ' AND '.join(conditions)

    count_result = conn.execute(f'SELECT COUNT(*) as total FROM fishing_logs WHERE {where_clause}', params).fetchone()
    total = count_result['total']
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    logs = conn.execute(
        f'SELECT * FROM fishing_logs WHERE {where_clause} ORDER BY {order_clause} LIMIT ? OFFSET ?',
        params + [per_page, offset]
    ).fetchall()
    conn.close()

    return render_template(
        'index.html',
        logs=logs,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        sort_by=sort_by,
        sort_label=sort_label,
        upcoming_strategies=upcoming_strategies,
        overview_stats=overview_stats,
        search_query=search_query
    )


@app.route('/add', methods=['GET', 'POST'])
def add_log():
    conn = get_db()
    bait_list = get_all_baits(conn)
    common_species = get_common_species(conn)
    harvest_templates = get_harvest_templates(conn)
    equipment_list = get_all_equipments(conn)
    common_weathers = get_common_weathers(conn)
    common_water_levels = get_common_water_levels(conn)
    latest_weather = get_latest_weather(conn)
    latest_water_level = get_latest_water_level(conn)

    if request.method == 'POST':
        spot = request.form['spot'].strip()
        weather = request.form['weather'].strip()
        water_level = request.form['water_level'].strip()
        bait = request.form['bait'].strip()
        fish_species = request.form['fish_species'].strip()
        harvest = request.form['harvest'].strip()
        next_strategy = request.form['next_strategy'].strip()
        next_strategy_date = request.form.get('next_strategy_date', '').strip()
        created_at = request.form['created_at'] or datetime.now().strftime('%Y-%m-%d')
        temperature = request.form.get('temperature', '').strip()
        humidity = request.form.get('humidity', '').strip()
        wind = request.form.get('wind', '').strip()

        eq_ids = request.form.getlist('equipment_id[]')
        eq_qtys = request.form.getlist('equipment_qty[]')
        eq_wears = request.form.getlist('equipment_wear[]')
        eq_notes = request.form.getlist('equipment_note[]')

        equipment_data = []
        for i in range(len(eq_ids)):
            try:
                eid = int(eq_ids[i]) if eq_ids[i] else None
                eqty = int(eq_qtys[i]) if i < len(eq_qtys) and eq_qtys[i] else 1
                ewear = float(eq_wears[i]) if i < len(eq_wears) and eq_wears[i] else 0.05
                enote = eq_notes[i] if i < len(eq_notes) else ''
                equipment_data.append({
                    'equipment_id': eid,
                    'quantity': eqty,
                    'wear_rate': ewear,
                    'notes': enote
                })
            except (ValueError, IndexError):
                continue

        if not spot or not weather or not water_level or not bait or not fish_species or not harvest:
            flash('请填写所有必填项！', 'error')
        else:
            moon_phase = request.form.get('moon_phase', '').strip()
            moon_illumination = request.form.get('moon_illumination', '').strip()
            lunar_day = request.form.get('lunar_day', '').strip()
            tide_type = request.form.get('tide_type', '').strip()
            fishing_index = request.form.get('fishing_index', '').strip()
            astro_description = request.form.get('astro_description', '').strip()
            sunrise = request.form.get('sunrise', '').strip()
            sunset_val = request.form.get('sunset', '').strip()
            moonrise = request.form.get('moonrise', '').strip()
            moonset = request.form.get('moonset', '').strip()
            day_length = request.form.get('day_length', '').strip()
            civil_twilight_begin = request.form.get('civil_twilight_begin', '').strip()
            civil_twilight_end = request.form.get('civil_twilight_end', '').strip()
            fishing_reason = request.form.get('fishing_reason', '').strip()

            if not moon_phase:
                astro_data = calculate_moon_phase(created_at)
                if astro_data:
                    external_data = fetch_external_astro_data(created_at)
                    if external_data:
                        astro_data.update(external_data)
                    weather_data = {
                        'weather': weather,
                        'temperature': temperature,
                        'wind': wind,
                    }
                    fi, fr = calculate_fishing_index(astro_data, weather_data)
                    moon_phase = astro_data['moon_phase']
                    moon_illumination = astro_data['moon_illumination']
                    lunar_day = astro_data['lunar_day']
                    tide_type = astro_data['tide_type']
                    fishing_index = fi
                    fishing_reason = fr
                    astro_description = get_astro_summary(astro_data, fi, fr)
                    sunrise = astro_data.get('sunrise', '') or ''
                    sunset_val = astro_data.get('sunset', '') or ''
                    moonrise = astro_data.get('moonrise', '') or ''
                    moonset = astro_data.get('moonset', '') or ''
                    day_length = astro_data.get('day_length', '') or ''
                    civil_twilight_begin = astro_data.get('civil_twilight_begin', '') or ''
                    civil_twilight_end = astro_data.get('civil_twilight_end', '') or ''

            cursor = conn.execute('''
                INSERT INTO fishing_logs (spot, weather, water_level, bait, fish_species, harvest, next_strategy, next_strategy_date, created_at, temperature, humidity, wind, moon_phase, moon_illumination, lunar_day, tide_type, fishing_index, astro_description, sunrise, sunset, moonrise, moonset, day_length, civil_twilight_begin, civil_twilight_end, fishing_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (spot, weather, water_level, bait, fish_species, harvest, next_strategy, next_strategy_date or None, created_at, temperature or None, humidity or None, wind or None, moon_phase or None, float(moon_illumination) if moon_illumination else None, int(lunar_day) if lunar_day else None, tide_type or None, int(fishing_index) if fishing_index else None, astro_description or None, sunrise or None, sunset_val or None, moonrise or None, moonset or None, day_length or None, civil_twilight_begin or None, civil_twilight_end or None, fishing_reason or None))
            log_id = cursor.lastrowid

            save_log_equipments(conn, log_id, equipment_data)

            photos = request.files.getlist('photos')
            photo_count = save_log_photos(conn, log_id, photos)

            conn.commit()
            if photo_count > 0:
                flash(f'记录添加成功！已上传 {photo_count} 张照片', 'success')
            else:
                flash('记录添加成功！', 'success')
            conn.close()
            return redirect(url_for('index'))

    conn.close()
    return render_template('add.html', bait_list=bait_list, default_city=WEATHER_API_CONFIG['city'],
                           common_species=common_species, harvest_templates=harvest_templates,
                           equipment_list=equipment_list, log_equipments=[], total_equipment_cost=0,
                           log_photos=[], common_weathers=common_weathers,
                           common_water_levels=common_water_levels,
                           latest_weather=latest_weather,
                           latest_water_level=latest_water_level)


@app.route('/log/<int:log_id>')
def log_detail(log_id):
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort', 'date_desc')
    per_page = request.args.get('per_page', 20, type=int)

    conn = get_db()
    log = conn.execute('SELECT * FROM fishing_logs WHERE id = ?', (log_id,)).fetchone()
    if log is None:
        conn.close()
        flash('记录不存在！', 'error')
        return redirect(url_for('index'))
    audit_logs = get_audit_logs(conn, log_id)
    log_equipments, total_equipment_cost = get_log_equipments(conn, log_id)
    log_photos = get_log_photos(conn, log_id)

    spot_logs = conn.execute('''
        SELECT id, created_at, harvest, fish_species, bait
        FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
        ORDER BY created_at ASC, id ASC
    ''', (log['spot'],)).fetchall()

    date_groups = {}
    date_list = []
    for item in spot_logs:
        item_dict = dict(item)
        item_dict['harvest_value'] = parse_harvest_value(item['harvest'])
        date = item['created_at']
        if date not in date_groups:
            date_groups[date] = []
            date_list.append(date)
        date_groups[date].append(item_dict)

    spot_timeline = []
    current_date_index = None
    current_log_in_date = None
    for idx, date in enumerate(date_list):
        logs = date_groups[date]
        total_harvest = sum(l['harvest_value'] for l in logs)
        has_current = any(l['id'] == log_id for l in logs)
        if has_current:
            current_date_index = idx
            for i, l in enumerate(logs):
                if l['id'] == log_id:
                    current_log_in_date = i
                    break
        spot_timeline.append({
            'date': date,
            'logs': logs,
            'log_count': len(logs),
            'total_harvest': total_harvest,
            'has_current': has_current
        })

    prev_day_logs = None
    next_day_logs = None
    if current_date_index is not None:
        if current_date_index > 0:
            prev_date_logs = date_groups[date_list[current_date_index - 1]]
            prev_day_logs = prev_date_logs[0]
        if current_date_index < len(date_list) - 1:
            next_date_logs = date_groups[date_list[current_date_index + 1]]
            next_day_logs = next_date_logs[0]

    harvest_value = parse_harvest_value(log['harvest'])
    cost_per_kg = 0
    if harvest_value > 0 and total_equipment_cost > 0:
        cost_per_kg = round(total_equipment_cost / harvest_value, 2)

    astro_info = None
    if log['created_at']:
        astro_calc = calculate_moon_phase(log['created_at'])
        if astro_calc:
            log_moon_phase = log['moon_phase'] if log['moon_phase'] else astro_calc['moon_phase']
            log_tide_type = log['tide_type'] if log['tide_type'] else astro_calc['tide_type']
            log_fishing_index = log['fishing_index'] if log['fishing_index'] else 5
            log_astro_description = log['astro_description'] if log['astro_description'] else ''
            log_moon_illumination = log['moon_illumination'] if log['moon_illumination'] else astro_calc['moon_illumination']
            log_lunar_day = log['lunar_day'] if log['lunar_day'] else astro_calc['lunar_day']
            log_fishing_reason = log['fishing_reason'] if 'fishing_reason' in log.keys() and log['fishing_reason'] else ''

            astro_info = {
                'moon_phase': log_moon_phase,
                'moon_phase_icon': astro_calc['moon_phase_icon'],
                'moon_illumination': log_moon_illumination,
                'lunar_day': log_lunar_day,
                'tide_type': log_tide_type,
                'tide_name': TIDE_TYPES.get(log_tide_type, '中潮'),
                'fishing_index': log_fishing_index,
                'astro_description': log_astro_description,
                'fishing_reason': log_fishing_reason,
                'sunrise': log['sunrise'] if 'sunrise' in log.keys() and log['sunrise'] else None,
                'sunset': log['sunset'] if 'sunset' in log.keys() and log['sunset'] else None,
                'moonrise': log['moonrise'] if 'moonrise' in log.keys() and log['moonrise'] else None,
                'moonset': log['moonset'] if 'moonset' in log.keys() and log['moonset'] else None,
                'day_length': log['day_length'] if 'day_length' in log.keys() and log['day_length'] else None,
                'civil_twilight_begin': log['civil_twilight_begin'] if 'civil_twilight_begin' in log.keys() and log['civil_twilight_begin'] else None,
                'civil_twilight_end': log['civil_twilight_end'] if 'civil_twilight_end' in log.keys() and log['civil_twilight_end'] else None,
            }

    conn.close()
    return render_template('detail.html', log=log, audit_logs=audit_logs,
                           page=page, sort_by=sort_by, per_page=per_page,
                           spot_timeline=spot_timeline,
                           current_date_index=current_date_index,
                           current_log_in_date=current_log_in_date,
                           prev_day_logs=prev_day_logs, next_day_logs=next_day_logs,
                           log_equipments=log_equipments,
                           total_equipment_cost=total_equipment_cost,
                           harvest_value=harvest_value,
                           cost_per_kg=cost_per_kg,
                           log_photos=log_photos,
                           astro_info=astro_info)


@app.route('/by-spot')
def by_spot():
    conn = get_db()
    spots = conn.execute('SELECT DISTINCT spot FROM fishing_logs WHERE deleted_at IS NULL ORDER BY spot').fetchall()
    spot_list = [row['spot'] for row in spots]

    spot_skunk_map = {}
    for spot_name in spot_list:
        spot_skunk_map[spot_name] = get_spot_skunk_stats(conn, spot_name)

    selected_spot = request.args.get('spot', spot_list[0] if spot_list else None)
    logs = []
    log_photos_dict = {}
    selected_skunk_stats = None
    if selected_spot:
        selected_skunk_stats = spot_skunk_map.get(selected_spot)
        log_rows = conn.execute(
            'SELECT * FROM fishing_logs WHERE spot = ? AND deleted_at IS NULL ORDER BY created_at DESC, id DESC',
            (selected_spot,)
        ).fetchall()
        logs = [dict(row) for row in log_rows]

        log_ids = [log['id'] for log in logs]
        if log_ids:
            placeholders = ','.join('?' * len(log_ids))
            photo_rows = conn.execute(
                f'SELECT * FROM log_photos WHERE log_id IN ({placeholders}) ORDER BY log_id, sort_order, id',
                log_ids
            ).fetchall()
            for photo in photo_rows:
                p = dict(photo)
                p['url'] = f'/static/uploads/{p["filename"]}'
                if p['log_id'] not in log_photos_dict:
                    log_photos_dict[p['log_id']] = []
                log_photos_dict[p['log_id']].append(p)

        for log in logs:
            log['photos'] = log_photos_dict.get(log['id'], [])
            log['photo_count'] = len(log['photos'])
            log['is_skunked'] = is_skunked(log['harvest'])

    conn.close()

    return render_template('by_spot.html', spot_list=spot_list, selected_spot=selected_spot, logs=logs,
                           spot_skunk_map=spot_skunk_map, selected_skunk_stats=selected_skunk_stats)


@app.route('/by-date')
def by_date():
    conn = get_db()
    dates = conn.execute(
        'SELECT DISTINCT created_at FROM fishing_logs WHERE deleted_at IS NULL ORDER BY created_at DESC'
    ).fetchall()
    date_list = [row['created_at'] for row in dates]

    selected_date = request.args.get('date', date_list[0] if date_list else None)
    logs = []
    if selected_date:
        logs = conn.execute(
            'SELECT * FROM fishing_logs WHERE created_at = ? AND deleted_at IS NULL ORDER BY id DESC',
            (selected_date,)
        ).fetchall()
    conn.close()

    return render_template('by_date.html', date_list=date_list, selected_date=selected_date, logs=logs)


@app.route('/delete/<int:log_id>', methods=['POST'])
def delete_log(log_id):
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort', 'date_desc')
    per_page = request.args.get('per_page', 20, type=int)

    conn = get_db()
    conn.execute('UPDATE fishing_logs SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?', (log_id,))
    conn.commit()
    conn.close()
    flash('记录已移入回收站！', 'success')
    return redirect(url_for('index', page=page, sort=sort_by, per_page=per_page))


def get_spot_visit_count(conn, spot_name):
    result = conn.execute(
        'SELECT COUNT(*) as cnt FROM fishing_logs WHERE spot = ? AND deleted_at IS NULL',
        (spot_name,)
    ).fetchone()
    return result['cnt'] if result else 0


def get_spot_avg_rating(conn, spot_id):
    result = conn.execute(
        'SELECT AVG(rating) as avg_rating, COUNT(*) as rating_count FROM spot_ratings WHERE spot_id = ?',
        (spot_id,)
    ).fetchone()
    return {
        'avg_rating': round(result['avg_rating'], 1) if result and result['avg_rating'] else 0,
        'rating_count': result['rating_count'] if result else 0
    }


@app.route('/spots')
def spots_list():
    conn = get_db()
    filter_type = request.args.get('filter', 'all')
    sort_by = request.args.get('sort', 'name')

    query = '''
        SELECT s.*,
               (SELECT AVG(r.rating) FROM spot_ratings r WHERE r.spot_id = s.id) as avg_rating,
               (SELECT COUNT(*) FROM spot_ratings r WHERE r.spot_id = s.id) as rating_count
        FROM fishing_spots s
        WHERE s.deleted_at IS NULL
    '''
    params = []
    conditions = []

    if filter_type == 'favorite':
        conditions.append('s.is_favorite = 1')

    if conditions:
        query += ' AND ' + ' AND '.join(conditions)

    if sort_by == 'rating':
        query += ' ORDER BY avg_rating IS NULL, avg_rating DESC, s.name'
    elif sort_by == 'visit':
        query += ' ORDER BY (SELECT COUNT(*) FROM fishing_logs l WHERE l.spot = s.name) DESC, s.name'
    elif sort_by == 'recent':
        query += ' ORDER BY s.created_at DESC'
    else:
        query += ' ORDER BY s.name'

    spots = conn.execute(query, params).fetchall()

    spot_list = []
    for spot in spots:
        spot_dict = dict(spot)
        spot_dict['visit_count'] = get_spot_visit_count(conn, spot['name'])
        spot_dict['avg_rating'] = round(spot_dict['avg_rating'], 1) if spot_dict['avg_rating'] else 0
        spot_dict['skunk_stats'] = get_spot_skunk_stats(conn, spot['name'])
        spot_list.append(spot_dict)

    conn.close()
    return render_template('spots.html', spots=spot_list, filter_type=filter_type, sort_by=sort_by)


@app.route('/spots/add', methods=['GET', 'POST'])
def add_spot():
    if request.method == 'POST':
        name = request.form['name'].strip()
        description = request.form['description'].strip()
        latitude = request.form['latitude'].strip()
        longitude = request.form['longitude'].strip()
        address = request.form['address'].strip()

        if not name:
            flash('请填写钓点名称！', 'error')
        else:
            conn = get_db()
            try:
                lat = float(latitude) if latitude else None
                lng = float(longitude) if longitude else None
                conn.execute('''
                    INSERT INTO fishing_spots (name, description, latitude, longitude, address)
                    VALUES (?, ?, ?, ?, ?)
                ''', (name, description, lat, lng, address))
                conn.commit()
                flash('钓点添加成功！', 'success')
                return redirect(url_for('spots_list'))
            except sqlite3.IntegrityError:
                flash('该钓点名称已存在！', 'error')
            finally:
                conn.close()

    return render_template('spot_form.html', spot=None)


def get_spot_bait_stats(conn, spot_name):
    rows = conn.execute('''
        SELECT bait, COUNT(*) as use_count
        FROM fishing_logs
        WHERE spot = ? AND bait IS NOT NULL AND bait != '' AND deleted_at IS NULL
        GROUP BY bait
        ORDER BY use_count DESC
        LIMIT 3
    ''', (spot_name,)).fetchall()
    return [{'name': r['bait'], 'count': r['use_count']} for r in rows]


def get_spot_best_harvest(conn, spot_name):
    rows = conn.execute('''
        SELECT id, harvest, created_at, fish_species, bait
        FROM fishing_logs
        WHERE spot = ? AND harvest IS NOT NULL AND harvest != '' AND deleted_at IS NULL
        ORDER BY id DESC
    ''', (spot_name,)).fetchall()

    best_log = None
    best_value = -1
    for row in rows:
        value = parse_harvest_value(row['harvest'])
        if value > best_value:
            best_value = value
            best_log = dict(row)
            best_log['harvest_value'] = value

    return best_log


@app.route('/spots/<int:spot_id>')
def spot_detail(spot_id):
    conn = get_db()
    spot = conn.execute('SELECT * FROM fishing_spots WHERE id = ?', (spot_id,)).fetchone()

    if spot is None:
        conn.close()
        flash('钓点不存在！', 'error')
        return redirect(url_for('spots_list'))

    spot_dict = dict(spot)
    spot_dict['visit_count'] = get_spot_visit_count(conn, spot['name'])
    rating_info = get_spot_avg_rating(conn, spot_id)
    spot_dict['avg_rating'] = rating_info['avg_rating']
    spot_dict['rating_count'] = rating_info['rating_count']

    bait_stats = get_spot_bait_stats(conn, spot['name'])
    best_harvest = get_spot_best_harvest(conn, spot['name'])

    ratings = conn.execute(
        'SELECT * FROM spot_ratings WHERE spot_id = ? ORDER BY created_at DESC',
        (spot_id,)
    ).fetchall()

    related_logs = conn.execute(
        'SELECT * FROM fishing_logs WHERE spot = ? AND deleted_at IS NULL ORDER BY created_at DESC, id DESC',
        (spot['name'],)
    ).fetchall()

    spot_photos_rows = conn.execute('''
        SELECT p.*, l.created_at as log_date, l.harvest as log_harvest
        FROM log_photos p
        JOIN fishing_logs l ON p.log_id = l.id
        WHERE l.spot = ? AND l.deleted_at IS NULL
        ORDER BY p.sort_order ASC, p.id DESC
    ''', (spot['name'],)).fetchall()

    spot_photos = []
    for row in spot_photos_rows:
        photo = dict(row)
        photo['url'] = url_for('static', filename=f'uploads/{photo["filename"]}')
        spot_photos.append(photo)

    conn.close()
    return render_template('spot_detail.html', spot=spot_dict, ratings=ratings, related_logs=related_logs,
                           bait_stats=bait_stats, best_harvest=best_harvest,
                           spot_photos=spot_photos)


@app.route('/spots/<int:spot_id>/edit', methods=['GET', 'POST'])
def edit_spot(spot_id):
    conn = get_db()
    spot = conn.execute('SELECT * FROM fishing_spots WHERE id = ?', (spot_id,)).fetchone()

    if spot is None:
        conn.close()
        flash('钓点不存在！', 'error')
        return redirect(url_for('spots_list'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        description = request.form['description'].strip()
        latitude = request.form['latitude'].strip()
        longitude = request.form['longitude'].strip()
        address = request.form['address'].strip()

        if not name:
            flash('请填写钓点名称！', 'error')
        else:
            try:
                lat = float(latitude) if latitude else None
                lng = float(longitude) if longitude else None
                conn.execute('''
                    UPDATE fishing_spots
                    SET name = ?, description = ?, latitude = ?, longitude = ?, address = ?
                    WHERE id = ?
                ''', (name, description, lat, lng, address, spot_id))
                conn.commit()
                flash('钓点更新成功！', 'success')
                return redirect(url_for('spot_detail', spot_id=spot_id))
            except sqlite3.IntegrityError:
                flash('该钓点名称已存在！', 'error')
            finally:
                conn.close()
    else:
        conn.close()

    return render_template('spot_form.html', spot=spot)


@app.route('/spots/<int:spot_id>/favorite', methods=['POST'])
def toggle_favorite(spot_id):
    conn = get_db()
    spot = conn.execute('SELECT is_favorite FROM fishing_spots WHERE id = ?', (spot_id,)).fetchone()

    if spot is None:
        conn.close()
        return jsonify({'success': False, 'message': '钓点不存在'}), 404

    new_status = 1 if spot['is_favorite'] == 0 else 0
    conn.execute('UPDATE fishing_spots SET is_favorite = ? WHERE id = ?', (new_status, spot_id))
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'is_favorite': new_status == 1})


@app.route('/spots/<int:spot_id>/rate', methods=['POST'])
def rate_spot(spot_id):
    conn = get_db()
    spot = conn.execute('SELECT id FROM fishing_spots WHERE id = ?', (spot_id,)).fetchone()

    if spot is None:
        conn.close()
        flash('钓点不存在！', 'error')
        return redirect(url_for('spots_list'))

    rating = request.form.get('rating', type=int)
    comment = request.form.get('comment', '').strip()

    if not rating or rating < 1 or rating > 5:
        flash('请选择有效的评分（1-5星）！', 'error')
    else:
        conn.execute('''
            INSERT INTO spot_ratings (spot_id, rating, comment)
            VALUES (?, ?, ?)
        ''', (spot_id, rating, comment))
        conn.commit()
        flash('评分提交成功！', 'success')

    conn.close()
    return redirect(url_for('spot_detail', spot_id=spot_id))


@app.route('/api/spots/map')
def spots_map_data():
    conn = get_db()
    spots = conn.execute('''
        SELECT s.id, s.name, s.latitude, s.longitude, s.is_favorite,
               (SELECT AVG(r.rating) FROM spot_ratings r WHERE r.spot_id = s.id) as avg_rating,
               (SELECT COUNT(*) FROM fishing_logs l WHERE l.spot = s.name) as visit_count
        FROM fishing_spots s
        WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL
    ''').fetchall()
    conn.close()
    result = []
    for s in spots:
        result.append({
            'id': s['id'],
            'name': s['name'],
            'lat': s['latitude'],
            'lng': s['longitude'],
            'is_favorite': bool(s['is_favorite']),
            'avg_rating': round(s['avg_rating'], 1) if s['avg_rating'] else 0,
            'visit_count': s['visit_count']
        })
    return jsonify(result)


@app.route('/api/spots/<int:spot_id>/visits')
def spot_visits_data(spot_id):
    conn = get_db()
    spot = conn.execute('SELECT id, name FROM fishing_spots WHERE id = ?', (spot_id,)).fetchone()
    if spot is None:
        conn.close()
        return jsonify({'error': '钓点不存在'}), 404

    visits = conn.execute('''
        SELECT created_at, COUNT(*) as count
        FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
        GROUP BY created_at
        ORDER BY created_at DESC
    ''', (spot['name'],)).fetchall()

    monthly = conn.execute('''
        SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as count
        FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    ''', (spot['name'],)).fetchall()

    conn.close()

    return jsonify({
        'spot_name': spot['name'],
        'timeline': [{'date': v['created_at'], 'count': v['count']} for v in visits],
        'monthly': [{'month': m['month'], 'count': m['count']} for m in monthly]
    })


@app.route('/spots/<int:spot_id>/delete', methods=['POST'])
def delete_spot(spot_id):
    conn = get_db()
    conn.execute('UPDATE fishing_spots SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?', (spot_id,))
    conn.commit()
    conn.close()
    flash('钓点已移入回收站！', 'success')
    return redirect(url_for('spots_list'))


@app.route('/spots/batch-rename', methods=['POST'])
def batch_rename_spots():
    spot_ids = request.form.getlist('spot_ids')
    new_name = request.form.get('new_name', '').strip()

    if not spot_ids:
        flash('请先选择要修改的钓点！', 'error')
        return redirect(url_for('spots_list'))

    if not new_name:
        flash('请输入新的钓点名称！', 'error')
        return redirect(url_for('spots_list'))

    conn = get_db()
    try:
        placeholders = ','.join('?' * len(spot_ids))
        spots = conn.execute(
            f'SELECT id, name FROM fishing_spots WHERE id IN ({placeholders}) AND deleted_at IS NULL',
            spot_ids
        ).fetchall()

        if not spots:
            flash('未找到选中的钓点！', 'error')
            return redirect(url_for('spots_list'))

        old_names = [row['name'] for row in spots]

        existing_spot = conn.execute(
            'SELECT id, name FROM fishing_spots WHERE name = ? AND deleted_at IS NULL',
            (new_name,)
        ).fetchone()

        if existing_spot and str(existing_spot['id']) not in spot_ids:
            flash(f'名称 "{new_name}" 已存在！', 'error')
            return redirect(url_for('spots_list'))

        batch_id = str(uuid.uuid4())
        updated_count = 0
        log_updated_count = 0
        invitation_updated_count = 0

        if existing_spot:
            target_spot_id = existing_spot['id']

            for spot in spots:
                if spot['id'] == target_spot_id:
                    continue

                old_name = spot['name']

                log_rows = conn.execute(
                    'SELECT id, spot FROM fishing_logs WHERE spot = ? AND deleted_at IS NULL',
                    (old_name,)
                ).fetchall()
                for log_row in log_rows:
                    record_audit_log(conn, log_row['id'], batch_id, 'spot', old_name, new_name)
                    log_updated_count += 1

                conn.execute(
                    'UPDATE fishing_logs SET spot = ? WHERE spot = ? AND deleted_at IS NULL',
                    (new_name, old_name)
                )

                conn.execute(
                    'UPDATE fishing_invitations SET spot = ? WHERE spot = ? AND deleted_at IS NULL',
                    (new_name, old_name)
                )
                inv_result = conn.execute('SELECT changes() as cnt').fetchone()
                invitation_updated_count += inv_result['cnt']

                conn.execute(
                    'UPDATE spot_ratings SET spot_id = ? WHERE spot_id = ?',
                    (target_spot_id, spot['id'])
                )

                conn.execute(
                    'UPDATE fishing_spots SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (spot['id'],)
                )
                updated_count += 1
        else:
            first_spot_id = spots[0]['id']
            first_old_name = spots[0]['name']

            conn.execute(
                'UPDATE fishing_spots SET name = ? WHERE id = ?',
                (new_name, first_spot_id)
            )

            log_rows = conn.execute(
                'SELECT id, spot FROM fishing_logs WHERE spot = ? AND deleted_at IS NULL',
                (first_old_name,)
            ).fetchall()
            for log_row in log_rows:
                record_audit_log(conn, log_row['id'], batch_id, 'spot', first_old_name, new_name)
                log_updated_count += 1

            conn.execute(
                'UPDATE fishing_logs SET spot = ? WHERE spot = ? AND deleted_at IS NULL',
                (new_name, first_old_name)
            )

            conn.execute(
                'UPDATE fishing_invitations SET spot = ? WHERE spot = ? AND deleted_at IS NULL',
                (new_name, first_old_name)
            )
            inv_result = conn.execute('SELECT changes() as cnt').fetchone()
            invitation_updated_count += inv_result['cnt']

            updated_count += 1

            for spot in spots[1:]:
                old_name = spot['name']

                log_rows = conn.execute(
                    'SELECT id, spot FROM fishing_logs WHERE spot = ? AND deleted_at IS NULL',
                    (old_name,)
                ).fetchall()
                for log_row in log_rows:
                    record_audit_log(conn, log_row['id'], batch_id, 'spot', old_name, new_name)
                    log_updated_count += 1

                conn.execute(
                    'UPDATE fishing_logs SET spot = ? WHERE spot = ? AND deleted_at IS NULL',
                    (new_name, old_name)
                )

                conn.execute(
                    'UPDATE fishing_invitations SET spot = ? WHERE spot = ? AND deleted_at IS NULL',
                    (new_name, old_name)
                )
                inv_result = conn.execute('SELECT changes() as cnt').fetchone()
                invitation_updated_count += inv_result['cnt']

                conn.execute(
                    'UPDATE spot_ratings SET spot_id = ? WHERE spot_id = ?',
                    (first_spot_id, spot['id'])
                )

                conn.execute(
                    'UPDATE fishing_spots SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (spot['id'],)
                )
                updated_count += 1

        conn.commit()

        message = f'成功修改 {updated_count} 个钓点名称'
        if log_updated_count > 0:
            message += f'，同步更新 {log_updated_count} 条垂钓记录'
        if invitation_updated_count > 0:
            message += f'，同步更新 {invitation_updated_count} 条邀约记录'
        flash(message, 'success')

    except Exception as e:
        conn.rollback()
        flash(f'批量修改失败：{str(e)}', 'error')
    finally:
        conn.close()

    return redirect(url_for('spots_list'))


def is_skunked(harvest_str):
    if not harvest_str:
        return True
    harvest_str = str(harvest_str).strip()
    if not harvest_str:
        return True

    zero_keywords = [
        '空军', '白板', '打龟', '空手', '空竿', '光头',
        '没钓到', '无收获', '零收获', '零', '0', '打飞机',
        '参军', '空', '龟', '白跑一趟', '参军了', '空军了'
    ]
    lower_str = harvest_str.lower()
    for kw in zero_keywords:
        if kw.lower() in lower_str:
            return True
    return False


def get_spot_skunk_stats(conn, spot_name):
    rows = conn.execute('''
        SELECT harvest FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
    ''', (spot_name,)).fetchall()

    total_count = len(rows)
    if total_count == 0:
        return {
            'skunk_count': 0,
            'total_count': 0,
            'skunk_rate': 0.0,
            'has_skunk': False,
            'recent_skunk': False
        }

    skunk_count = sum(1 for r in rows if is_skunked(r['harvest']))
    skunk_rate = round((skunk_count / total_count) * 100, 1)

    recent_skunk = False
    recent_rows = conn.execute('''
        SELECT harvest FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 3
    ''', (spot_name,)).fetchall()
    for r in recent_rows:
        if is_skunked(r['harvest']):
            recent_skunk = True
            break

    return {
        'skunk_count': skunk_count,
        'total_count': total_count,
        'skunk_rate': skunk_rate,
        'has_skunk': skunk_count > 0,
        'recent_skunk': recent_skunk
    }


def parse_harvest_value(harvest_str):
    if not harvest_str:
        return 0
    harvest_str = str(harvest_str).strip()
    if not harvest_str:
        return 0

    zero_keywords = [
        '空军', '白板', '打龟', '空手', '空竿', '光头',
        '没钓到', '无收获', '零收获', '零', '0', '打飞机',
        '参军', '空', '龟', '白跑一趟', '参军了', '空军了'
    ]
    lower_str = harvest_str.lower()
    for kw in zero_keywords:
        if kw.lower() in lower_str:
            return 0

    matches = re.findall(r'(\d+(?:\.\d+)?)', harvest_str)
    if matches:
        num_str = matches[-1]
        try:
            if '.' in num_str:
                return float(num_str)
            else:
                return int(num_str)
        except ValueError:
            pass

    if harvest_str.startswith('h') or harvest_str.startswith('H'):
        try:
            return int(harvest_str[1:])
        except ValueError:
            pass

    return 0


@app.route('/invitations')
def invitations_list():
    conn = get_db()
    filter_status = request.args.get('status', 'all')

    query = '''
        SELECT i.*,
               (SELECT COUNT(*) FROM invitation_members m WHERE m.invitation_id = i.id) as member_count
        FROM fishing_invitations i
        WHERE i.deleted_at IS NULL
    '''
    params = []
    conditions = []

    if filter_status != 'all':
        conditions.append('i.status = ?')
        params.append(filter_status)

    if conditions:
        query += ' AND ' + ' AND '.join(conditions)

    query += ' ORDER BY i.date DESC, i.id DESC'

    invitations = conn.execute(query, params).fetchall()

    inv_list = []
    for inv in invitations:
        inv_dict = dict(inv)
        members = conn.execute(
            'SELECT * FROM invitation_members WHERE invitation_id = ? ORDER BY id',
            (inv['id'],)
        ).fetchall()
        inv_dict['members'] = [dict(m) for m in members]
        
        member_count = len(members)
        total_cost = inv_dict.get('total_cost') or 0
        inv_dict['avg_cost'] = round(total_cost / member_count, 2) if member_count > 0 and total_cost > 0 else 0
        
        total_harvest = inv_dict.get('total_harvest') or ''
        inv_dict['total_harvest_display'] = total_harvest if total_harvest else ''
        if total_harvest:
            harvest_val = parse_harvest_value(total_harvest)
            inv_dict['avg_harvest'] = round(harvest_val / member_count, 2) if member_count > 0 and harvest_val > 0 else 0
        else:
            inv_dict['avg_harvest'] = 0
        
        members_with_harvest = [m for m in inv_dict['members'] if m.get('harvest_detail')]
        inv_dict['members_with_harvest_count'] = len(members_with_harvest)
        
        inv_list.append(inv_dict)

    conn.close()
    return render_template('invitations.html', invitations=inv_list, filter_status=filter_status)


def get_all_spot_names(conn):
    rows = conn.execute('SELECT DISTINCT name FROM fishing_spots WHERE deleted_at IS NULL ORDER BY name').fetchall()
    spot_set = set()
    result = []
    for row in rows:
        name = row['name']
        if name and name not in spot_set:
            spot_set.add(name)
            result.append(name)
    log_rows = conn.execute('SELECT DISTINCT spot FROM fishing_logs WHERE spot IS NOT NULL AND spot != "" AND deleted_at IS NULL ORDER BY spot').fetchall()
    for row in log_rows:
        name = row['spot']
        if name and name not in spot_set:
            spot_set.add(name)
            result.append(name)
    return result


@app.route('/invitations/add', methods=['GET', 'POST'])
def add_invitation():
    conn = get_db()
    spot_list = get_all_spot_names(conn)

    if request.method == 'POST':
        spot = request.form['spot'].strip()
        date = request.form['date'].strip()
        notes = request.form.get('notes', '').strip()
        total_cost = request.form.get('total_cost', '0').strip()
        total_harvest = request.form.get('total_harvest', '').strip()
        status = request.form.get('status', 'planned').strip()

        member_names = request.form.getlist('member_name[]')
        member_harvests = request.form.getlist('member_harvest[]')
        member_costs = request.form.getlist('member_cost[]')

        if not spot or not date:
            flash('请填写钓点和日期！', 'error')
        else:
            cost = float(total_cost) if total_cost else 0
            cursor = conn.execute('''
                INSERT INTO fishing_invitations (spot, date, notes, status, total_cost, total_harvest)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (spot, date, notes or None, status, cost, total_harvest or None))
            inv_id = cursor.lastrowid

            for i, name in enumerate(member_names):
                name = name.strip()
                if name:
                    harvest = member_harvests[i].strip() if i < len(member_harvests) else ''
                    share = float(member_costs[i].strip() or 0) if i < len(member_costs) else 0
                    conn.execute('''
                        INSERT INTO invitation_members (invitation_id, name, harvest_detail, cost_share)
                        VALUES (?, ?, ?, ?)
                    ''', (inv_id, name, harvest or None, share))

            conn.commit()
            conn.close()
            flash('邀约创建成功！', 'success')
            return redirect(url_for('invitations_detail', inv_id=inv_id))

    conn.close()
    return render_template('invitation_form.html', invitation=None, members=[], spot_list=spot_list)


@app.route('/invitations/<int:inv_id>')
def invitations_detail(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT * FROM fishing_invitations WHERE id = ?', (inv_id,)).fetchone()
    if inv is None:
        conn.close()
        flash('邀约不存在！', 'error')
        return redirect(url_for('invitations_list'))

    inv_dict = dict(inv)
    members = conn.execute(
        'SELECT * FROM invitation_members WHERE invitation_id = ? ORDER BY id',
        (inv_id,)
    ).fetchall()
    inv_dict['members'] = [dict(m) for m in members]
    inv_dict['member_count'] = len(members)

    total_cost = inv_dict['total_cost'] or 0
    member_count = max(1, len(members))
    inv_dict['avg_cost'] = round(total_cost / member_count, 2) if total_cost > 0 else 0

    total_harvest = inv_dict.get('total_harvest') or ''
    inv_dict['total_harvest_display'] = total_harvest if total_harvest else ''
    if total_harvest:
        harvest_val = parse_harvest_value(total_harvest)
        inv_dict['avg_harvest'] = round(harvest_val / member_count, 2) if member_count > 0 and harvest_val > 0 else 0
    else:
        inv_dict['avg_harvest'] = 0

    members_with_harvest = [m for m in inv_dict['members'] if m.get('harvest_detail')]
    inv_dict['members_with_harvest_count'] = len(members_with_harvest)

    conn.close()
    return render_template('invitation_detail.html', invitation=inv_dict)


@app.route('/invitations/<int:inv_id>/edit', methods=['GET', 'POST'])
def edit_invitation(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT * FROM fishing_invitations WHERE id = ?', (inv_id,)).fetchone()
    spot_list = get_all_spot_names(conn)

    if inv is None:
        conn.close()
        flash('邀约不存在！', 'error')
        return redirect(url_for('invitations_list'))

    members = conn.execute(
        'SELECT * FROM invitation_members WHERE invitation_id = ? ORDER BY id',
        (inv_id,)
    ).fetchall()

    if request.method == 'POST':
        spot = request.form['spot'].strip()
        date = request.form['date'].strip()
        notes = request.form.get('notes', '').strip()
        total_cost = request.form.get('total_cost', '0').strip()
        total_harvest = request.form.get('total_harvest', '').strip()
        status = request.form.get('status', 'planned').strip()

        member_names = request.form.getlist('member_name[]')
        member_harvests = request.form.getlist('member_harvest[]')
        member_costs = request.form.getlist('member_cost[]')

        if not spot or not date:
            flash('请填写钓点和日期！', 'error')
        else:
            cost = float(total_cost) if total_cost else 0
            conn.execute('''
                UPDATE fishing_invitations
                SET spot = ?, date = ?, notes = ?, status = ?, total_cost = ?, total_harvest = ?
                WHERE id = ?
            ''', (spot, date, notes or None, status, cost, total_harvest or None, inv_id))

            conn.execute('DELETE FROM invitation_members WHERE invitation_id = ?', (inv_id,))

            for i, name in enumerate(member_names):
                name = name.strip()
                if name:
                    harvest = member_harvests[i].strip() if i < len(member_harvests) else ''
                    share = float(member_costs[i].strip() or 0) if i < len(member_costs) else 0
                    conn.execute('''
                        INSERT INTO invitation_members (invitation_id, name, harvest_detail, cost_share)
                        VALUES (?, ?, ?, ?)
                    ''', (inv_id, name, harvest or None, share))

            conn.commit()
            conn.close()
            flash('邀约更新成功！', 'success')
            return redirect(url_for('invitations_detail', inv_id=inv_id))

    conn.close()
    return render_template('invitation_form.html', invitation=inv, members=members, spot_list=spot_list)


@app.route('/invitations/<int:inv_id>/delete', methods=['POST'])
def delete_invitation(inv_id):
    conn = get_db()
    conn.execute('UPDATE fishing_invitations SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?', (inv_id,))
    conn.commit()
    conn.close()
    flash('邀约已移入回收站！', 'success')
    return redirect(url_for('invitations_list'))


@app.route('/invitations/<int:inv_id>/members/add', methods=['POST'])
def add_invitation_member(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT id FROM fishing_invitations WHERE id = ?', (inv_id,)).fetchone()
    if inv is None:
        conn.close()
        flash('邀约不存在！', 'error')
        return redirect(url_for('invitations_list'))

    name = request.form.get('name', '').strip()
    harvest_detail = request.form.get('harvest_detail', '').strip()
    cost_share = request.form.get('cost_share', '0').strip()

    if not name:
        flash('请填写钓友姓名！', 'error')
    else:
        share = float(cost_share) if cost_share else 0
        conn.execute('''
            INSERT INTO invitation_members (invitation_id, name, harvest_detail, cost_share)
            VALUES (?, ?, ?, ?)
        ''', (inv_id, name, harvest_detail or None, share))
        conn.commit()
        flash('钓友添加成功！', 'success')

    conn.close()
    return redirect(url_for('invitations_detail', inv_id=inv_id))


@app.route('/invitations/<int:inv_id>/members/<int:member_id>/edit', methods=['POST'])
def edit_invitation_member(inv_id, member_id):
    conn = get_db()
    name = request.form.get('name', '').strip()
    harvest_detail = request.form.get('harvest_detail', '').strip()
    cost_share = request.form.get('cost_share', '0').strip()

    if not name:
        flash('请填写钓友姓名！', 'error')
    else:
        share = float(cost_share) if cost_share else 0
        conn.execute('''
            UPDATE invitation_members
            SET name = ?, harvest_detail = ?, cost_share = ?
            WHERE id = ? AND invitation_id = ?
        ''', (name, harvest_detail or None, share, member_id, inv_id))
        conn.commit()
        flash('钓友信息更新成功！', 'success')

    conn.close()
    return redirect(url_for('invitations_detail', inv_id=inv_id))


@app.route('/invitations/<int:inv_id>/members/<int:member_id>/delete', methods=['POST'])
def delete_invitation_member(inv_id, member_id):
    conn = get_db()
    conn.execute('DELETE FROM invitation_members WHERE id = ? AND invitation_id = ?', (member_id, inv_id))
    conn.commit()
    conn.close()
    flash('钓友已移除！', 'success')
    return redirect(url_for('invitations_detail', inv_id=inv_id))


@app.route('/invitations/<int:inv_id>/status', methods=['POST'])
def update_invitation_status(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT id FROM fishing_invitations WHERE id = ?', (inv_id,)).fetchone()
    if inv is None:
        conn.close()
        flash('邀约不存在！', 'error')
        return redirect(url_for('invitations_list'))

    status = request.form.get('status', '').strip()
    valid_statuses = ['planned', 'ongoing', 'completed']
    if status not in valid_statuses:
        flash('无效的状态！', 'error')
    else:
        conn.execute('UPDATE fishing_invitations SET status = ? WHERE id = ?', (status, inv_id))
        conn.commit()
        flash('状态更新成功！', 'success')

    conn.close()
    return redirect(request.referrer or url_for('invitations_detail', inv_id=inv_id))


@app.route('/invitations/<int:inv_id>/split-cost', methods=['POST'])
def split_invitation_cost(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT * FROM fishing_invitations WHERE id = ?', (inv_id,)).fetchone()
    if inv is None:
        conn.close()
        flash('邀约不存在！', 'error')
        return redirect(url_for('invitations_list'))

    members = conn.execute(
        'SELECT id FROM invitation_members WHERE invitation_id = ?',
        (inv_id,)
    ).fetchall()

    total_cost = inv['total_cost'] or 0
    member_count = len(members)

    if member_count == 0:
        conn.close()
        flash('暂无可分摊费用的钓友！', 'error')
        return redirect(url_for('invitations_detail', inv_id=inv_id))

    avg_share = round(total_cost / member_count, 2)
    remainder = round(total_cost - avg_share * member_count, 2)

    for i, member in enumerate(members):
        share = avg_share
        if i == 0 and remainder > 0:
            share = round(share + remainder, 2)
        conn.execute(
            'UPDATE invitation_members SET cost_share = ? WHERE id = ?',
            (share, member['id'])
        )

    conn.commit()
    conn.close()
    flash('费用已平均分摊！', 'success')
    return redirect(url_for('invitations_detail', inv_id=inv_id))


@app.route('/invitations/<int:inv_id>/split-harvest', methods=['POST'])
def split_invitation_harvest(inv_id):
    conn = get_db()
    inv = conn.execute('SELECT * FROM fishing_invitations WHERE id = ?', (inv_id,)).fetchone()
    if inv is None:
        conn.close()
        flash('邀约不存在！', 'error')
        return redirect(url_for('invitations_list'))

    members = conn.execute(
        'SELECT id FROM invitation_members WHERE invitation_id = ?',
        (inv_id,)
    ).fetchall()

    total_harvest = inv['total_harvest'] or ''
    member_count = len(members)

    if member_count == 0:
        conn.close()
        flash('暂无可分摊收获的钓友！', 'error')
        return redirect(url_for('invitations_detail', inv_id=inv_id))

    if not total_harvest:
        conn.close()
        flash('请先填写总收获！', 'error')
        return redirect(url_for('invitations_detail', inv_id=inv_id))

    harvest_val = parse_harvest_value(total_harvest)
    if harvest_val <= 0:
        conn.close()
        flash('总收获数值无效，无法分摊！', 'error')
        return redirect(url_for('invitations_detail', inv_id=inv_id))

    unit = '斤'
    total_lower = total_harvest.lower()
    if '条' in total_harvest:
        unit = '条'
    elif '尾' in total_harvest:
        unit = '尾'
    elif 'kg' in total_lower or '公斤' in total_harvest:
        unit = '公斤'

    if unit in ['条', '尾']:
        avg_share = harvest_val // member_count
        remainder = harvest_val - avg_share * member_count
    else:
        avg_share = round(harvest_val / member_count, 2)
        remainder = round(harvest_val - avg_share * member_count, 2)

    for i, member in enumerate(members):
        share = avg_share
        if i == 0 and remainder > 0:
            share = share + remainder
        if unit in ['条', '尾']:
            share_str = f'{int(share)}{unit}'
        else:
            share_str = f'{share}{unit}' if share != int(share) else f'{int(share)}{unit}'
        conn.execute(
            'UPDATE invitation_members SET harvest_detail = ? WHERE id = ?',
            (share_str, member['id'])
        )

    conn.commit()
    conn.close()
    flash('收获已按人数平均分摊！', 'success')
    return redirect(url_for('invitations_detail', inv_id=inv_id))


@app.route('/monthly-report')
def monthly_report():
    conn = get_db()

    months = conn.execute('''
        SELECT DISTINCT strftime('%Y-%m', created_at) as month
        FROM fishing_logs
        WHERE deleted_at IS NULL
        ORDER BY month DESC
    ''').fetchall()
    month_list = [row['month'] for row in months]

    selected_month = request.args.get('month', month_list[0] if month_list else None)

    species_stats = []
    spot_stats = []
    total_logs = 0
    total_harvest = 0
    total_spots = 0
    total_species = 0

    if selected_month:
        species_rows = conn.execute('''
            SELECT fish_species, COUNT(*) as log_count
            FROM fishing_logs
            WHERE strftime('%Y-%m', created_at) = ? AND deleted_at IS NULL
            GROUP BY fish_species
            ORDER BY log_count DESC
        ''', (selected_month,)).fetchall()

        total_logs = sum(r['log_count'] for r in species_rows)
        total_species = len(species_rows)

        species_stats = []
        for row in species_rows:
            percentage = round((row['log_count'] / total_logs * 100), 1) if total_logs > 0 else 0
            species_stats.append({
                'species': row['fish_species'],
                'count': row['log_count'],
                'percentage': percentage
            })

        spot_rows = conn.execute('''
            SELECT spot, COUNT(*) as log_count
            FROM fishing_logs
            WHERE strftime('%Y-%m', created_at) = ? AND deleted_at IS NULL
            GROUP BY spot
        ''', (selected_month,)).fetchall()

        total_spots = len(spot_rows)

        all_logs = conn.execute('''
            SELECT harvest FROM fishing_logs
            WHERE strftime('%Y-%m', created_at) = ? AND deleted_at IS NULL
        ''', (selected_month,)).fetchall()

        total_harvest = sum(parse_harvest_value(log['harvest']) for log in all_logs)

        spot_data = []
        for row in spot_rows:
            spot_logs = conn.execute('''
                SELECT harvest FROM fishing_logs
                WHERE spot = ? AND strftime('%Y-%m', created_at) = ? AND deleted_at IS NULL
            ''', (row['spot'], selected_month)).fetchall()
            spot_harvest = sum(parse_harvest_value(log['harvest']) for log in spot_logs)
            spot_data.append({
                'spot': row['spot'],
                'log_count': row['log_count'],
                'harvest_count': spot_harvest
            })

        spot_data.sort(key=lambda x: x['harvest_count'], reverse=True)

        spot_stats = []
        for idx, data in enumerate(spot_data, 1):
            spot_stats.append({
                'rank': idx,
                'spot': data['spot'],
                'log_count': data['log_count'],
                'harvest_count': data['harvest_count']
            })

    conn.close()

    return render_template(
        'monthly_report.html',
        month_list=month_list,
        selected_month=selected_month,
        species_stats=species_stats,
        spot_stats=spot_stats,
        total_logs=total_logs,
        total_harvest=total_harvest,
        total_spots=total_spots,
        total_species=total_species
    )


def get_all_baits(conn):
    baits = conn.execute('SELECT * FROM baits WHERE deleted_at IS NULL ORDER BY name').fetchall()
    return [dict(b) for b in baits]


def get_common_species(conn, limit=15):
    rows = conn.execute('''
        SELECT fish_species FROM fishing_logs
        WHERE fish_species IS NOT NULL AND fish_species != '' AND deleted_at IS NULL
    ''').fetchall()
    species_counter = {}
    for row in rows:
        species_str = row['fish_species'].strip()
        if not species_str:
            continue
        parts = re.split(r'[、,，/；;]', species_str)
        for part in parts:
            part = part.strip()
            if part:
                species_counter[part] = species_counter.get(part, 0) + 1
    sorted_species = sorted(species_counter.items(), key=lambda x: x[1], reverse=True)
    return [{'name': name, 'count': count} for name, count in sorted_species[:limit]]


def get_harvest_templates(conn, limit=15):
    rows = conn.execute('''
        SELECT harvest, fish_species, COUNT(*) as use_count
        FROM fishing_logs
        WHERE harvest IS NOT NULL AND harvest != '' AND deleted_at IS NULL
        GROUP BY harvest
        ORDER BY use_count DESC, created_at DESC
        LIMIT ?
    ''', (limit * 3,)).fetchall()

    templates = []
    seen_patterns = set()

    for row in rows:
        harvest = row['harvest'].strip()
        if not harvest:
            continue

        harvest_val = parse_harvest_value(harvest)
        is_zero = harvest_val == 0

        pattern_key = harvest
        if pattern_key in seen_patterns:
            continue
        seen_patterns.add(pattern_key)

        template_type = 'zero' if is_zero else 'normal'

        templates.append({
            'text': harvest,
            'count': row['use_count'],
            'type': template_type,
            'species': row['fish_species'] or ''
        })

    templates.sort(key=lambda x: x['count'], reverse=True)
    return templates[:limit]


def get_default_species_list():
    return [
        '鲫鱼', '鲤鱼', '草鱼', '鲢鱼', '鳙鱼',
        '白条', '马口', '黄颡鱼', '黑鱼', '鲶鱼',
        '罗非鱼', '鲈鱼', '鳜鱼', '青鱼', '翘嘴'
    ]


def get_default_harvest_templates():
    return [
        {'text': '空军', 'type': 'zero'},
        {'text': '白板', 'type': 'zero'},
        {'text': '打龟', 'type': 'zero'},
        {'text': '白条若干', 'type': 'normal'},
        {'text': '鲫鱼5条约2斤', 'type': 'normal'},
        {'text': '鲤鱼1条约3斤', 'type': 'normal'},
        {'text': '草鱼2条约5斤', 'type': 'normal'},
        {'text': '杂鱼若干约1斤', 'type': 'normal'},
    ]


def get_bait_usage_stats(conn, bait_name):
    logs = conn.execute(
        'SELECT harvest FROM fishing_logs WHERE bait = ? AND deleted_at IS NULL',
        (bait_name,)
    ).fetchall()
    use_count = len(logs)
    total_harvest = sum(parse_harvest_value(log['harvest']) for log in logs)
    success_count = sum(1 for log in logs if parse_harvest_value(log['harvest']) > 0)
    success_rate = round((success_count / use_count * 100), 1) if use_count > 0 else 0
    avg_harvest = round((total_harvest / use_count), 2) if use_count > 0 else 0
    return {
        'use_count': use_count,
        'total_harvest': total_harvest,
        'success_count': success_count,
        'success_rate': success_rate,
        'avg_harvest': avg_harvest
    }


@app.route('/baits')
def baits_list():
    conn = get_db()
    sort_by = request.args.get('sort', 'success')

    baits = conn.execute('SELECT * FROM baits WHERE deleted_at IS NULL ORDER BY name').fetchall()

    bait_stats = []
    for bait in baits:
        bait_dict = dict(bait)
        stats = get_bait_usage_stats(conn, bait['name'])
        bait_dict.update(stats)
        bait_stats.append(bait_dict)

    if sort_by == 'usage':
        bait_stats.sort(key=lambda x: x['use_count'], reverse=True)
    elif sort_by == 'success':
        bait_stats.sort(key=lambda x: x['success_rate'], reverse=True)
    elif sort_by == 'harvest':
        bait_stats.sort(key=lambda x: x['total_harvest'], reverse=True)
    elif sort_by == 'avg_harvest':
        bait_stats.sort(key=lambda x: x['avg_harvest'], reverse=True)

    all_baits_from_logs = conn.execute(
        'SELECT DISTINCT bait FROM fishing_logs WHERE bait NOT IN (SELECT name FROM baits WHERE deleted_at IS NULL) AND bait IS NOT NULL AND bait != "" AND deleted_at IS NULL ORDER BY bait'
    ).fetchall()
    unmanaged_baits = [row['bait'] for row in all_baits_from_logs if row['bait']]

    conn.close()

    return render_template(
        'baits.html',
        baits=bait_stats,
        sort_by=sort_by,
        unmanaged_baits=unmanaged_baits
    )


@app.route('/baits/add', methods=['GET', 'POST'])
def add_bait():
    if request.method == 'POST':
        name = request.form['name'].strip()
        bait_type = request.form['type'].strip()
        brand = request.form['brand'].strip()
        description = request.form['description'].strip()

        if not name:
            flash('请填写饵料名称！', 'error')
        else:
            conn = get_db()
            try:
                conn.execute('''
                    INSERT INTO baits (name, type, brand, description)
                    VALUES (?, ?, ?, ?)
                ''', (name, bait_type or None, brand or None, description or None))
                conn.commit()
                flash('饵料添加成功！', 'success')
                return redirect(url_for('baits_list'))
            except sqlite3.IntegrityError:
                flash('该饵料名称已存在！', 'error')
            finally:
                conn.close()

    return render_template('bait_form.html', bait=None)


@app.route('/baits/<int:bait_id>/edit', methods=['GET', 'POST'])
def edit_bait(bait_id):
    conn = get_db()
    bait = conn.execute('SELECT * FROM baits WHERE id = ?', (bait_id,)).fetchone()

    if bait is None:
        conn.close()
        flash('饵料不存在！', 'error')
        return redirect(url_for('baits_list'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        bait_type = request.form['type'].strip()
        brand = request.form['brand'].strip()
        description = request.form['description'].strip()

        if not name:
            flash('请填写饵料名称！', 'error')
        else:
            try:
                old_name = bait['name']
                conn.execute('''
                    UPDATE baits
                    SET name = ?, type = ?, brand = ?, description = ?
                    WHERE id = ?
                ''', (name, bait_type or None, brand or None, description or None, bait_id))
                conn.execute(
                    'UPDATE fishing_logs SET bait = ? WHERE bait = ?',
                    (name, old_name)
                )
                conn.commit()
                flash('饵料更新成功！', 'success')
                return redirect(url_for('baits_list'))
            except sqlite3.IntegrityError:
                flash('该饵料名称已存在！', 'error')
            finally:
                conn.close()
    else:
        conn.close()

    return render_template('bait_form.html', bait=bait)


@app.route('/baits/<int:bait_id>/delete', methods=['POST'])
def delete_bait(bait_id):
    conn = get_db()
    conn.execute('UPDATE baits SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?', (bait_id,))
    conn.commit()
    conn.close()
    flash('饵料已移入回收站！', 'success')
    return redirect(url_for('baits_list'))


@app.route('/baits/import/<bait_name>', methods=['POST'])
def import_bait(bait_name):
    conn = get_db()
    try:
        conn.execute('INSERT INTO baits (name) VALUES (?)', (bait_name,))
        conn.commit()
        flash(f'饵料"{bait_name}"已导入！', 'success')
    except sqlite3.IntegrityError:
        flash('该饵料已存在！', 'error')
    finally:
        conn.close()
    return redirect(url_for('baits_list'))


@app.route('/api/weather/current')
def api_weather_current():
    city = request.args.get('city', WEATHER_API_CONFIG['city'])
    try:
        encoded_city = urllib.parse.quote(city)
        url = f'https://wttr.in/{encoded_city}?format=j1'
        req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.68.0'})
        with urllib.request.urlopen(req, timeout=WEATHER_API_CONFIG['timeout']) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        current = data.get('current_condition', [{}])[0]
        weather_desc = ''
        if current.get('lang_zh'):
            weather_desc = current['lang_zh'][0].get('value', '')
        if not weather_desc and current.get('weatherDesc'):
            weather_desc = current['weatherDesc'][0].get('value', '')

        temp_c = current.get('temp_C', '')
        humidity = current.get('humidity', '')
        windspeed = current.get('windspeedKmph', '')
        winddir = ''
        if current.get('lang_zh') and len(current.get('lang_zh', [])) > 0:
            pass
        if current.get('winddir16Point'):
            winddir = current['winddir16Point']

        wind_display = ''
        if winddir and windspeed:
            wind_display = f'{winddir} {windspeed}km/h'
        elif windspeed:
            wind_display = f'{windspeed}km/h'

        weather_full = weather_desc
        if temp_c:
            weather_full += f' {temp_c}℃'

        return jsonify({
            'success': True,
            'data': {
                'weather': weather_full,
                'weather_desc': weather_desc,
                'temperature': f'{temp_c}℃' if temp_c else '',
                'temperature_value': temp_c,
                'humidity': f'{humidity}%' if humidity else '',
                'humidity_value': humidity,
                'wind': wind_display,
                'wind_speed': windspeed,
                'wind_direction': winddir,
                'city': city,
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'获取天气失败：{str(e)}'
        }), 500


@app.route('/api/weather/history')
def api_weather_history():
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT DISTINCT weather, temperature, humidity, wind, created_at
            FROM fishing_logs
            WHERE weather IS NOT NULL AND weather != '' AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT 20
        ''').fetchall()

        history = []
        seen = set()
        for row in rows:
            key = (row['weather'] or '', row['temperature'] or '', row['humidity'] or '', row['wind'] or '')
            if key and key not in seen:
                seen.add(key)
                history.append({
                    'weather': row['weather'] or '',
                    'temperature': row['temperature'] or '',
                    'humidity': row['humidity'] or '',
                    'wind': row['wind'] or '',
                    'date': row['created_at'] or '',
                })
                if len(history) >= 10:
                    break

        return jsonify({
            'success': True,
            'data': history
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
    finally:
        conn.close()


@app.route('/api/astro/data')
def api_astro_data():
    date_str = request.args.get('date', '').strip()
    weather = request.args.get('weather', '').strip()
    temperature = request.args.get('temperature', '').strip()
    wind = request.args.get('wind', '').strip()

    if not date_str:
        date_str = datetime.now().strftime('%Y-%m-%d')

    astro_data = calculate_moon_phase(date_str)
    if not astro_data:
        return jsonify({
            'success': False,
            'message': '无法计算天文数据，请检查日期格式'
        }), 400

    external_data = fetch_external_astro_data(date_str)
    if external_data:
        astro_data.update(external_data)

    weather_data = {
        'weather': weather,
        'temperature': temperature,
        'wind': wind,
    }
    fishing_index, fishing_reason = calculate_fishing_index(astro_data, weather_data)
    astro_summary = get_astro_summary(astro_data, fishing_index, fishing_reason)

    return jsonify({
        'success': True,
        'data': {
            'moon_phase': astro_data['moon_phase'],
            'moon_phase_icon': astro_data['moon_phase_icon'],
            'moon_illumination': astro_data['moon_illumination'],
            'lunar_day': astro_data['lunar_day'],
            'tide_type': astro_data['tide_type'],
            'tide_name': astro_data['tide_name'],
            'fishing_index': fishing_index,
            'fishing_reason': fishing_reason,
            'astro_summary': astro_summary,
            'sunrise': astro_data.get('sunrise'),
            'sunset': astro_data.get('sunset'),
            'moonrise': astro_data.get('moonrise'),
            'moonset': astro_data.get('moonset'),
            'day_length': astro_data.get('day_length'),
            'civil_twilight_begin': astro_data.get('civil_twilight_begin'),
            'civil_twilight_end': astro_data.get('civil_twilight_end'),
            'data_source': 'api+local' if external_data else 'local',
        }
    })


@app.route('/api/spots/search')
def api_spots_search():
    conn = get_db()
    try:
        keyword = request.args.get('q', '').strip()
        query = '''
            SELECT DISTINCT spot FROM fishing_logs
            WHERE spot IS NOT NULL AND spot != '' AND deleted_at IS NULL
        '''
        params = []
        if keyword:
            query += ' AND spot LIKE ?'
            params.append(f'%{keyword}%')
        query += ' ORDER BY (SELECT COUNT(*) FROM fishing_logs l WHERE l.spot = fishing_logs.spot AND l.deleted_at IS NULL) DESC, spot LIMIT 20'
        rows = conn.execute(query, params).fetchall()
        spots = []
        for row in rows:
            spot_name = row['spot']
            last_log = conn.execute('''
                SELECT water_level, bait FROM fishing_logs
                WHERE spot = ? AND water_level IS NOT NULL AND water_level != '' AND deleted_at IS NULL
                ORDER BY created_at DESC, id DESC LIMIT 1
            ''', (spot_name,)).fetchone()
            spots.append({
                'name': spot_name,
                'water_level': last_log['water_level'] if last_log else '',
                'bait': last_log['bait'] if last_log else ''
            })
        return jsonify({'success': True, 'data': spots})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/species/search')
def api_species_search():
    conn = get_db()
    try:
        keyword = request.args.get('q', '').strip()
        rows = conn.execute('''
            SELECT fish_species FROM fishing_logs
            WHERE fish_species IS NOT NULL AND fish_species != '' AND deleted_at IS NULL
        ''').fetchall()
        species_counter = {}
        for row in rows:
            species_str = row['fish_species'].strip()
            if not species_str:
                continue
            parts = re.split(r'[、,，/；;]', species_str)
            for part in parts:
                part = part.strip()
                if part:
                    species_counter[part] = species_counter.get(part, 0) + 1
        if keyword:
            filtered = {k: v for k, v in species_counter.items() if keyword in k}
        else:
            filtered = species_counter
        sorted_species = sorted(filtered.items(), key=lambda x: x[1], reverse=True)
        result = [{'name': name, 'count': count} for name, count in sorted_species[:20]]
        return jsonify({'success': True, 'data': result})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/harvest/search')
def api_harvest_search():
    conn = get_db()
    try:
        keyword = request.args.get('q', '').strip()
        query = '''
            SELECT harvest, fish_species, COUNT(*) as use_count
            FROM fishing_logs
            WHERE harvest IS NOT NULL AND harvest != '' AND deleted_at IS NULL
        '''
        params = []
        if keyword:
            query += ' AND harvest LIKE ?'
            params.append(f'%{keyword}%')
        query += ' GROUP BY harvest ORDER BY use_count DESC, created_at DESC LIMIT 20'
        rows = conn.execute(query, params).fetchall()
        templates = []
        seen = set()
        for row in rows:
            harvest = row['harvest'].strip()
            if not harvest or harvest in seen:
                continue
            seen.add(harvest)
            harvest_val = parse_harvest_value(harvest)
            template_type = 'zero' if harvest_val == 0 else 'normal'
            templates.append({
                'text': harvest,
                'count': row['use_count'],
                'type': template_type,
                'species': row['fish_species'] or ''
            })
        return jsonify({'success': True, 'data': templates})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/candidates/by-spot')
def api_candidates_by_spot():
    conn = get_db()
    try:
        spot = request.args.get('spot', '').strip()
        result_species = get_common_species(conn, limit=15)
        result_harvest = get_harvest_templates(conn, limit=15)

        if spot:
            spot_species = get_common_species_by_spot(conn, spot, limit=15)
            spot_harvest = get_harvest_templates_by_spot(conn, spot, limit=10)

            if spot_species:
                seen = set()
                merged = []
                for s in spot_species:
                    if s['name'] not in seen:
                        seen.add(s['name'])
                        merged.append(s)
                for s in result_species:
                    if s['name'] not in seen:
                        seen.add(s['name'])
                        merged.append(s)
                result_species = merged[:15]

            if spot_harvest:
                seen = set()
                merged = []
                for h in spot_harvest:
                    if h['text'] not in seen:
                        seen.add(h['text'])
                        merged.append(h)
                for h in result_harvest:
                    if h['text'] not in seen:
                        seen.add(h['text'])
                        merged.append(h)
                result_harvest = merged[:15]

        return jsonify({
            'success': True,
            'data': {
                'species': result_species,
                'harvest': result_harvest
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


def get_common_species_by_spot(conn, spot, limit=15):
    rows = conn.execute('''
        SELECT fish_species FROM fishing_logs
        WHERE spot = ? AND fish_species IS NOT NULL AND fish_species != '' AND deleted_at IS NULL
    ''', (spot,)).fetchall()
    species_counter = {}
    for row in rows:
        species_str = row['fish_species'].strip()
        if not species_str:
            continue
        parts = re.split(r'[、,，/；;]', species_str)
        for part in parts:
            part = part.strip()
            if part:
                species_counter[part] = species_counter.get(part, 0) + 1
    sorted_species = sorted(species_counter.items(), key=lambda x: x[1], reverse=True)
    return [{'name': name, 'count': count} for name, count in sorted_species[:limit]]


def get_harvest_templates_by_spot(conn, spot, limit=10):
    rows = conn.execute('''
        SELECT harvest, fish_species, COUNT(*) as use_count
        FROM fishing_logs
        WHERE spot = ? AND harvest IS NOT NULL AND harvest != '' AND deleted_at IS NULL
        GROUP BY harvest
        ORDER BY use_count DESC, created_at DESC
        LIMIT ?
    ''', (spot, limit * 3)).fetchall()

    templates = []
    seen_patterns = set()
    for row in rows:
        harvest = row['harvest'].strip()
        if not harvest:
            continue
        harvest_val = parse_harvest_value(harvest)
        is_zero = harvest_val == 0
        pattern_key = harvest
        if pattern_key in seen_patterns:
            continue
        seen_patterns.add(pattern_key)
        template_type = 'zero' if is_zero else 'normal'
        templates.append({
            'text': harvest,
            'count': row['use_count'],
            'type': template_type,
            'species': row['fish_species'] or ''
        })
    templates.sort(key=lambda x: x['count'], reverse=True)
    return templates[:limit]


@app.route('/search')
def search_logs():
    conn = get_db()

    spot_list = [row['spot'] for row in conn.execute('SELECT DISTINCT spot FROM fishing_logs WHERE deleted_at IS NULL ORDER BY spot').fetchall()]
    species_list = [row['fish_species'] for row in conn.execute('SELECT DISTINCT fish_species FROM fishing_logs WHERE deleted_at IS NULL ORDER BY fish_species').fetchall()]

    date_start = request.args.get('date_start', '').strip()
    date_end = request.args.get('date_end', '').strip()
    selected_spot = request.args.get('spot', '').strip()
    selected_species = request.args.get('fish_species', '').strip()
    selected_season = request.args.get('season', '').strip()

    conditions = []
    params = []

    if date_start:
        conditions.append('created_at >= ?')
        params.append(date_start)
    if date_end:
        conditions.append('created_at <= ?')
        params.append(date_end)
    if selected_spot:
        conditions.append('spot = ?')
        params.append(selected_spot)
    if selected_species:
        conditions.append('fish_species = ?')
        params.append(selected_species)
    if selected_season and selected_season in SEASON_MONTHS:
        months = SEASON_MONTHS[selected_season]
        placeholders = ','.join(['?'] * len(months))
        conditions.append(f'CAST(strftime(\'%m\', created_at) AS INTEGER) IN ({placeholders})')
        params.extend(months)

    logs = []
    if conditions:
        where_clause = ' AND '.join(conditions) + ' AND deleted_at IS NULL'
        logs = conn.execute(
            f'SELECT * FROM fishing_logs WHERE {where_clause} ORDER BY created_at DESC, id DESC',
            params
        ).fetchall()

    conn.close()

    has_filter = bool(date_start or date_end or selected_spot or selected_species or selected_season)

    return render_template(
        'search.html',
        spot_list=spot_list,
        species_list=species_list,
        date_start=date_start,
        date_end=date_end,
        selected_spot=selected_spot,
        selected_species=selected_species,
        selected_season=selected_season,
        season_labels=SEASON_LABELS,
        logs=logs,
        has_filter=has_filter
    )


@app.route('/search/export')
def export_search_csv():
    conn = get_db()

    date_start = request.args.get('date_start', '').strip()
    date_end = request.args.get('date_end', '').strip()
    selected_spot = request.args.get('spot', '').strip()
    selected_species = request.args.get('fish_species', '').strip()
    selected_season = request.args.get('season', '').strip()

    conditions = []
    params = []

    if date_start:
        conditions.append('created_at >= ?')
        params.append(date_start)
    if date_end:
        conditions.append('created_at <= ?')
        params.append(date_end)
    if selected_spot:
        conditions.append('spot = ?')
        params.append(selected_spot)
    if selected_species:
        conditions.append('fish_species = ?')
        params.append(selected_species)
    if selected_season and selected_season in SEASON_MONTHS:
        months = SEASON_MONTHS[selected_season]
        placeholders = ','.join(['?'] * len(months))
        conditions.append(f'CAST(strftime(\'%m\', created_at) AS INTEGER) IN ({placeholders})')
        params.extend(months)

    if not conditions:
        conn.close()
        flash('请至少设置一个筛选条件再导出！', 'error')
        return redirect(url_for('search_logs'))

    where_clause = ' AND '.join(conditions) + ' AND deleted_at IS NULL'
    logs = conn.execute(
        f'SELECT * FROM fishing_logs WHERE {where_clause} ORDER BY created_at DESC, id DESC',
        params
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['日期', '钓点', '天气', '温度', '湿度', '风力', '水位', '饵料', '鱼种', '收获', '下次策略'])
    for log in logs:
        writer.writerow([
            log['created_at'],
            log['spot'],
            log['weather'],
            log['temperature'] or '',
            log['humidity'] or '',
            log['wind'] or '',
            log['water_level'],
            log['bait'],
            log['fish_species'],
            log['harvest'],
            log['next_strategy'] or ''
        ])

    filename_parts = []
    if date_start or date_end:
        filename_parts.append(f"{date_start or '起'}~{date_end or '止'}")
    if selected_spot:
        filename_parts.append(selected_spot)
    if selected_species:
        filename_parts.append(selected_species)
    filename = '_'.join(filename_parts) if filename_parts else 'search_results'

    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename={urllib.parse.quote(filename)}.csv'}
    )


def get_season(month):
    if month in (3, 4, 5):
        return 'spring'
    elif month in (6, 7, 8):
        return 'summer'
    elif month in (9, 10, 11):
        return 'autumn'
    else:
        return 'winter'


SEASON_LABELS = {
    'spring': '春季 (3-5月)',
    'summer': '夏季 (6-8月)',
    'autumn': '秋季 (9-11月)',
    'winter': '冬季 (12-2月)',
    'all': '全年'
}


SEASON_MONTHS = {
    'spring': (3, 4, 5),
    'summer': (6, 7, 8),
    'autumn': (9, 10, 11),
    'winter': (12, 1, 2),
}


@app.route('/heatmap')
def heatmap():
    selected_season = request.args.get('season', 'all')
    conn = get_db()

    years = conn.execute('''
        SELECT DISTINCT strftime('%Y', created_at) as year
        FROM fishing_logs
        WHERE created_at IS NOT NULL AND created_at != '' AND deleted_at IS NULL
        ORDER BY year DESC
    ''').fetchall()
    year_list = [row['year'] for row in years]
    selected_year = request.args.get('year', year_list[0] if year_list else None)

    conn.close()

    return render_template(
        'heatmap.html',
        selected_season=selected_season,
        season_labels=SEASON_LABELS,
        year_list=year_list,
        selected_year=selected_year
    )


@app.route('/api/heatmap/data')
def heatmap_data():
    selected_season = request.args.get('season', 'all')
    selected_year = request.args.get('year', None)

    conn = get_db()

    log_query = '''
        SELECT l.spot, l.created_at, l.id as log_id, l.harvest, l.fish_species
        FROM fishing_logs l
        WHERE l.created_at IS NOT NULL AND l.created_at != '' AND l.deleted_at IS NULL
    '''
    params = []

    if selected_year:
        log_query += ' AND strftime(\'%Y\', l.created_at) = ?'
        params.append(selected_year)

    if selected_season != 'all' and selected_season in SEASON_MONTHS:
        months = SEASON_MONTHS[selected_season]
        placeholders = ','.join(['?'] * len(months))
        log_query += f' AND CAST(strftime(\'%m\', l.created_at) AS INTEGER) IN ({placeholders})'
        params.extend(months)

    log_rows = conn.execute(log_query, params).fetchall()

    spot_log_count = {}
    spot_log_ids = {}
    spot_harvests = {}

    for row in log_rows:
        spot_name = row['spot']
        if spot_name not in spot_log_count:
            spot_log_count[spot_name] = 0
            spot_log_ids[spot_name] = []
            spot_harvests[spot_name] = []
        spot_log_count[spot_name] += 1
        spot_log_ids[spot_name].append(row['log_id'])
        harvest_val = parse_harvest_value(row['harvest'])
        if harvest_val > 0:
            spot_harvests[spot_name].append(harvest_val)

    spots_query = '''
        SELECT id, name, latitude, longitude, address, is_favorite
        FROM fishing_spots
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND deleted_at IS NULL
    '''
    spot_rows = conn.execute(spots_query).fetchall()

    result = []
    max_count = 0

    for s in spot_rows:
        spot_name = s['name']
        count = spot_log_count.get(spot_name, 0)
        if count > max_count:
            max_count = count

        harvests = spot_harvests.get(spot_name, [])
        total_harvest = sum(harvests)
        avg_harvest = round(total_harvest / len(harvests), 2) if harvests else 0

        result.append({
            'id': s['id'],
            'name': spot_name,
            'lat': s['latitude'],
            'lng': s['longitude'],
            'address': s['address'] or '',
            'is_favorite': bool(s['is_favorite']),
            'log_count': count,
            'log_ids': spot_log_ids.get(spot_name, []),
            'total_harvest': total_harvest,
            'avg_harvest': avg_harvest,
            'intensity': 0
        })

    conn.close()

    for spot in result:
        if max_count > 0:
            spot['intensity'] = round(spot['log_count'] / max_count, 4)

    return jsonify({
        'season': selected_season,
        'season_label': SEASON_LABELS.get(selected_season, '全年'),
        'year': selected_year,
        'max_count': max_count,
        'total_spots': len(result),
        'total_logs': sum(s['log_count'] for s in result),
        'spots': result
    })


@app.route('/recycle-bin')
def recycle_bin():
    tab = request.args.get('tab', 'logs')
    valid_tabs = ['logs', 'spots', 'baits', 'invitations', 'equipments', 'wiki']
    if tab not in valid_tabs:
        tab = 'logs'

    conn = get_db()

    log_count = conn.execute('SELECT COUNT(*) FROM fishing_logs WHERE deleted_at IS NOT NULL').fetchone()[0]
    spot_count = conn.execute('SELECT COUNT(*) FROM fishing_spots WHERE deleted_at IS NOT NULL').fetchone()[0]
    bait_count = conn.execute('SELECT COUNT(*) FROM baits WHERE deleted_at IS NOT NULL').fetchone()[0]
    inv_count = conn.execute('SELECT COUNT(*) FROM fishing_invitations WHERE deleted_at IS NOT NULL').fetchone()[0]
    eq_count = conn.execute('SELECT COUNT(*) FROM equipments WHERE deleted_at IS NOT NULL').fetchone()[0]
    wiki_count = conn.execute('SELECT COUNT(*) FROM spot_wiki WHERE deleted_at IS NOT NULL').fetchone()[0]

    items = []
    if tab == 'logs':
        items = conn.execute('''
            SELECT id, spot, created_at, harvest, fish_species, deleted_at
            FROM fishing_logs
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
        ''').fetchall()
    elif tab == 'spots':
        items = conn.execute('''
            SELECT id, name, description, created_at, deleted_at
            FROM fishing_spots
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
        ''').fetchall()
    elif tab == 'baits':
        items = conn.execute('''
            SELECT id, name, type, brand, created_at, deleted_at
            FROM baits
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
        ''').fetchall()
    elif tab == 'invitations':
        items = conn.execute('''
            SELECT id, spot, date, status, total_cost, deleted_at
            FROM fishing_invitations
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
        ''').fetchall()
    elif tab == 'equipments':
        items = conn.execute('''
            SELECT id, name, category, brand, model, unit_price, deleted_at
            FROM equipments
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
        ''').fetchall()
    elif tab == 'wiki':
        items = conn.execute('''
            SELECT id, spot_name, best_time, source, info_date, deleted_at
            FROM spot_wiki
            WHERE deleted_at IS NOT NULL
            ORDER BY deleted_at DESC
        ''').fetchall()

    conn.close()
    return render_template(
        'recycle_bin.html',
        tab=tab,
        items=[dict(i) for i in items],
        counts={
            'logs': log_count,
            'spots': spot_count,
            'baits': bait_count,
            'invitations': inv_count,
            'equipments': eq_count,
            'wiki': wiki_count
        }
    )


@app.route('/recycle-bin/restore/<item_type>/<int:item_id>', methods=['POST'])
def restore_item(item_type, item_id):
    valid_types = {
        'logs': 'fishing_logs',
        'spots': 'fishing_spots',
        'baits': 'baits',
        'invitations': 'fishing_invitations',
        'equipments': 'equipments',
        'wiki': 'spot_wiki'
    }
    if item_type not in valid_types:
        flash('无效的类型！', 'error')
        return redirect(url_for('recycle_bin'))

    table = valid_types[item_type]
    conn = get_db()
    conn.execute(f'UPDATE {table} SET deleted_at = NULL WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()
    flash('恢复成功！', 'success')
    return redirect(url_for('recycle_bin', tab=item_type))


@app.route('/recycle-bin/delete/<item_type>/<int:item_id>', methods=['POST'])
def permanent_delete_item(item_type, item_id):
    valid_types = {
        'logs': 'fishing_logs',
        'spots': 'fishing_spots',
        'baits': 'baits',
        'invitations': 'fishing_invitations',
        'equipments': 'equipments',
        'wiki': 'spot_wiki'
    }
    if item_type not in valid_types:
        flash('无效的类型！', 'error')
        return redirect(url_for('recycle_bin'))

    table = valid_types[item_type]
    conn = get_db()
    conn.execute(f'DELETE FROM {table} WHERE id = ?', (item_id,))
    conn.commit()
    conn.close()
    flash('已彻底删除！', 'success')
    return redirect(url_for('recycle_bin', tab=item_type))


@app.route('/recycle-bin/batch-restore', methods=['POST'])
def batch_restore():
    item_type = request.form.get('item_type', '')
    ids = request.form.getlist('ids')

    valid_types = {
        'logs': 'fishing_logs',
        'spots': 'fishing_spots',
        'baits': 'baits',
        'invitations': 'fishing_invitations',
        'equipments': 'equipments',
        'wiki': 'spot_wiki'
    }
    if item_type not in valid_types or not ids:
        flash('请先选择要恢复的项目！', 'error')
        return redirect(url_for('recycle_bin'))

    table = valid_types[item_type]
    placeholders = ','.join(['?'] * len(ids))
    conn = get_db()
    conn.execute(f'UPDATE {table} SET deleted_at = NULL WHERE id IN ({placeholders})', [int(i) for i in ids])
    conn.commit()
    conn.close()
    flash(f'已批量恢复 {len(ids)} 条记录！', 'success')
    return redirect(url_for('recycle_bin', tab=item_type))


@app.route('/recycle-bin/batch-delete', methods=['POST'])
def batch_permanent_delete():
    item_type = request.form.get('item_type', '')
    ids = request.form.getlist('ids')

    valid_types = {
        'logs': 'fishing_logs',
        'spots': 'fishing_spots',
        'baits': 'baits',
        'invitations': 'fishing_invitations',
        'equipments': 'equipments',
        'wiki': 'spot_wiki'
    }
    if item_type not in valid_types or not ids:
        flash('请先选择要彻底删除的项目！', 'error')
        return redirect(url_for('recycle_bin'))

    table = valid_types[item_type]
    placeholders = ','.join(['?'] * len(ids))
    conn = get_db()
    conn.execute(f'DELETE FROM {table} WHERE id IN ({placeholders})', [int(i) for i in ids])
    conn.commit()
    conn.close()
    flash(f'已彻底删除 {len(ids)} 条记录！', 'success')
    return redirect(url_for('recycle_bin', tab=item_type))


EQUIPMENT_CATEGORIES = [
    '鱼竿', '渔轮', '鱼线', '鱼钩', '浮漂', '鱼护',
    '钓箱', '钓椅', '抄网', '支架', '遮阳伞', '饵料盆',
    '夜钓灯', '探鱼器', '钓鱼服', '其他'
]


def get_all_equipments(conn, include_deleted=False):
    query = 'SELECT * FROM equipments'
    conditions = []
    if not include_deleted:
        conditions.append('deleted_at IS NULL')
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY category, name'
    rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def get_equipment_usage_stats(conn, equipment_id):
    total_used = conn.execute('''
        SELECT COALESCE(SUM(le.quantity), 0) as cnt
        FROM log_equipments le
        JOIN fishing_logs fl ON le.log_id = fl.id
        WHERE le.equipment_id = ? AND fl.deleted_at IS NULL
    ''', (equipment_id,)).fetchone()['cnt']

    total_cost = conn.execute('''
        SELECT COALESCE(SUM(le.usage_cost), 0) as cost
        FROM log_equipments le
        JOIN fishing_logs fl ON le.log_id = fl.id
        WHERE le.equipment_id = ? AND fl.deleted_at IS NULL
    ''', (equipment_id,)).fetchone()['cost']

    last_used = conn.execute('''
        SELECT MAX(fl.created_at) as last_date
        FROM log_equipments le
        JOIN fishing_logs fl ON le.log_id = fl.id
        WHERE le.equipment_id = ? AND fl.deleted_at IS NULL
    ''', (equipment_id,)).fetchone()['last_date']

    return {
        'use_count': total_used,
        'total_usage_cost': round(total_cost, 2),
        'last_used_date': last_used
    }


def calculate_usage_cost(equipment, quantity, wear_rate=0.05):
    unit_price = equipment.get('unit_price') or 0
    lifespan = equipment.get('lifespan_months') or 24
    base_cost = unit_price / lifespan
    wear_cost = unit_price * wear_rate
    single_cost = (base_cost + wear_cost) * quantity
    return round(single_cost, 2)


def update_equipment_quantity(conn, equipment_id):
    total_in = conn.execute('''
        SELECT COALESCE(SUM(quantity), 0) as cnt
        FROM equipment_transactions
        WHERE equipment_id = ? AND type = 'in'
    ''', (equipment_id,)).fetchone()['cnt']

    total_out = conn.execute('''
        SELECT COALESCE(SUM(quantity), 0) as cnt
        FROM equipment_transactions
        WHERE equipment_id = ? AND type IN ('out', 'discard')
    ''', (equipment_id,)).fetchone()['cnt']

    adjustment = conn.execute('''
        SELECT COALESCE(SUM(quantity), 0) as cnt
        FROM equipment_transactions
        WHERE equipment_id = ? AND type = 'adjust'
    ''', (equipment_id,)).fetchone()['cnt']

    total_quantity = total_in + adjustment
    available_quantity = total_in - total_out + adjustment

    conn.execute('''
        UPDATE equipments SET total_quantity = ?, available_quantity = ? WHERE id = ?
    ''', (total_quantity, available_quantity, equipment_id))


@app.route('/equipments')
def equipments_list():
    conn = get_db()
    category = request.args.get('category', 'all')
    sort_by = request.args.get('sort', 'category')

    query = '''
        SELECT e.*,
               (SELECT COALESCE(SUM(le.quantity), 0)
                FROM log_equipments le
                JOIN fishing_logs fl ON le.log_id = fl.id
                WHERE le.equipment_id = e.id AND fl.deleted_at IS NULL) as use_count
        FROM equipments e
        WHERE e.deleted_at IS NULL
    '''
    params = []

    if category != 'all':
        query += ' AND e.category = ?'
        params.append(category)

    if sort_by == 'name':
        query += ' ORDER BY e.name'
    elif sort_by == 'price':
        query += ' ORDER BY e.unit_price DESC'
    elif sort_by == 'usage':
        query += ' ORDER BY use_count DESC'
    elif sort_by == 'available':
        query += ' ORDER BY e.available_quantity DESC'
    elif sort_by == 'recent':
        query += ' ORDER BY e.created_at DESC'
    else:
        query += ' ORDER BY e.category, e.name'

    equipments = conn.execute(query, params).fetchall()

    result = []
    for eq in equipments:
        eq_dict = dict(eq)
        stats = get_equipment_usage_stats(conn, eq['id'])
        eq_dict.update(stats)
        result.append(eq_dict)

    categories = conn.execute('''
        SELECT DISTINCT category FROM equipments WHERE deleted_at IS NULL ORDER BY category
    ''').fetchall()
    category_list = [c['category'] for c in categories if c['category']]

    stats_overview = conn.execute('''
        SELECT
            COUNT(*) as total_types,
            COALESCE(SUM(unit_price * total_quantity), 0) as total_value,
            COALESCE(SUM(total_quantity), 0) as total_items
        FROM equipments WHERE deleted_at IS NULL
    ''').fetchone()

    conn.close()

    return render_template(
        'equipments.html',
        equipments=result,
        category=category,
        sort_by=sort_by,
        category_list=category_list,
        all_categories=EQUIPMENT_CATEGORIES,
        stats_overview=dict(stats_overview)
    )


@app.route('/equipments/add', methods=['GET', 'POST'])
def add_equipment():
    if request.method == 'POST':
        name = request.form['name'].strip()
        category = request.form['category'].strip()
        brand = request.form.get('brand', '').strip()
        model = request.form.get('model', '').strip()
        spec = request.form.get('spec', '').strip()
        unit_price = request.form.get('unit_price', '0').strip()
        init_quantity = request.form.get('init_quantity', '0').strip()
        purchase_date = request.form.get('purchase_date', '').strip()
        supplier = request.form.get('supplier', '').strip()
        description = request.form.get('description', '').strip()
        lifespan_months = request.form.get('lifespan_months', '24').strip()

        if not name or not category:
            flash('请填写装备名称和分类！', 'error')
        else:
            conn = get_db()
            try:
                price = float(unit_price) if unit_price else 0
                qty = int(init_quantity) if init_quantity else 0
                lifespan = int(lifespan_months) if lifespan_months else 24

                cursor = conn.execute('''
                    INSERT INTO equipments (name, category, brand, model, spec, unit_price,
                                           purchase_date, supplier, description, lifespan_months)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, category, brand or None, model or None, spec or None,
                      price, purchase_date or None, supplier or None,
                      description or None, lifespan))

                equipment_id = cursor.lastrowid

                if qty > 0:
                    conn.execute('''
                        INSERT INTO equipment_transactions
                        (equipment_id, type, quantity, unit_price, total_cost, reason, transaction_date)
                        VALUES (?, 'in', ?, ?, ?, ?, ?)
                    ''', (equipment_id, qty, price, price * qty, '初始入库',
                          purchase_date or datetime.now().strftime('%Y-%m-%d')))
                    update_equipment_quantity(conn, equipment_id)

                conn.commit()
                flash('装备添加成功！', 'success')
                return redirect(url_for('equipments_list'))
            except Exception as e:
                flash(f'添加失败：{str(e)}', 'error')
            finally:
                conn.close()

    return render_template('equipment_form.html', equipment=None, categories=EQUIPMENT_CATEGORIES)


@app.route('/equipments/<int:eq_id>')
def equipment_detail(eq_id):
    conn = get_db()
    equipment = conn.execute('SELECT * FROM equipments WHERE id = ?', (eq_id,)).fetchone()

    if equipment is None:
        conn.close()
        flash('装备不存在！', 'error')
        return redirect(url_for('equipments_list'))

    eq_dict = dict(equipment)
    stats = get_equipment_usage_stats(conn, eq_id)
    eq_dict.update(stats)

    transactions = conn.execute('''
        SELECT et.*,
               (SELECT spot FROM fishing_logs WHERE id = et.related_log_id) as related_spot,
               (SELECT created_at FROM fishing_logs WHERE id = et.related_log_id) as related_date
        FROM equipment_transactions et
        WHERE et.equipment_id = ?
        ORDER BY et.transaction_date DESC, et.id DESC
    ''', (eq_id,)).fetchall()

    usage_logs = conn.execute('''
        SELECT le.*, fl.spot, fl.created_at as log_date, fl.harvest
        FROM log_equipments le
        JOIN fishing_logs fl ON le.log_id = fl.id
        WHERE le.equipment_id = ? AND fl.deleted_at IS NULL
        ORDER BY fl.created_at DESC, le.id DESC
    ''', (eq_id,)).fetchall()

    conn.close()

    return render_template(
        'equipment_detail.html',
        equipment=eq_dict,
        transactions=[dict(t) for t in transactions],
        usage_logs=[dict(l) for l in usage_logs]
    )


@app.route('/equipments/<int:eq_id>/edit', methods=['GET', 'POST'])
def edit_equipment(eq_id):
    conn = get_db()
    equipment = conn.execute('SELECT * FROM equipments WHERE id = ?', (eq_id,)).fetchone()

    if equipment is None:
        conn.close()
        flash('装备不存在！', 'error')
        return redirect(url_for('equipments_list'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        category = request.form['category'].strip()
        brand = request.form.get('brand', '').strip()
        model = request.form.get('model', '').strip()
        spec = request.form.get('spec', '').strip()
        unit_price = request.form.get('unit_price', '0').strip()
        purchase_date = request.form.get('purchase_date', '').strip()
        supplier = request.form.get('supplier', '').strip()
        description = request.form.get('description', '').strip()
        lifespan_months = request.form.get('lifespan_months', '24').strip()

        if not name or not category:
            flash('请填写装备名称和分类！', 'error')
        else:
            try:
                price = float(unit_price) if unit_price else 0
                lifespan = int(lifespan_months) if lifespan_months else 24

                conn.execute('''
                    UPDATE equipments SET
                        name = ?, category = ?, brand = ?, model = ?, spec = ?,
                        unit_price = ?, purchase_date = ?, supplier = ?,
                        description = ?, lifespan_months = ?
                    WHERE id = ?
                ''', (name, category, brand or None, model or None, spec or None,
                      price, purchase_date or None, supplier or None,
                      description or None, lifespan, eq_id))
                conn.commit()
                flash('装备更新成功！', 'success')
                return redirect(url_for('equipment_detail', eq_id=eq_id))
            except Exception as e:
                flash(f'更新失败：{str(e)}', 'error')
            finally:
                conn.close()
    else:
        conn.close()

    return render_template('equipment_form.html', equipment=equipment, categories=EQUIPMENT_CATEGORIES)


@app.route('/equipments/<int:eq_id>/delete', methods=['POST'])
def delete_equipment(eq_id):
    conn = get_db()
    conn.execute('UPDATE equipments SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?', (eq_id,))
    conn.commit()
    conn.close()
    flash('装备已移入回收站！', 'success')
    return redirect(url_for('equipments_list'))


@app.route('/equipments/<int:eq_id>/transaction', methods=['GET', 'POST'])
def equipment_transaction(eq_id):
    conn = get_db()
    equipment = conn.execute('SELECT * FROM equipments WHERE id = ?', (eq_id,)).fetchone()

    if equipment is None:
        conn.close()
        flash('装备不存在！', 'error')
        return redirect(url_for('equipments_list'))

    recent_logs = conn.execute('''
        SELECT id, spot, created_at FROM fishing_logs
        WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 20
    ''').fetchall()

    if request.method == 'POST':
        trans_type = request.form['type'].strip()
        quantity = request.form.get('quantity', '0').strip()
        unit_price = request.form.get('unit_price', '0').strip()
        reason = request.form.get('reason', '').strip()
        operator = request.form.get('operator', '').strip()
        trans_date = request.form.get('transaction_date', '').strip()
        related_log_id = request.form.get('related_log_id', '').strip()

        if trans_type not in ('in', 'out', 'adjust', 'discard'):
            flash('无效的操作类型！', 'error')
        elif not quantity or int(quantity) <= 0:
            flash('请填写有效的数量！', 'error')
        else:
            try:
                qty = int(quantity)
                price = float(unit_price) if unit_price else (equipment['unit_price'] or 0)
                qty_signed = qty if trans_type in ('in', 'adjust') else -qty
                total = price * abs(qty)

                if trans_type in ('out', 'discard'):
                    if qty > (equipment['available_quantity'] or 0):
                        flash(f'库存不足！当前可用：{equipment["available_quantity"] or 0}', 'error')
                        conn.close()
                        return redirect(url_for('equipment_transaction', eq_id=eq_id))

                conn.execute('''
                    INSERT INTO equipment_transactions
                    (equipment_id, type, quantity, unit_price, total_cost,
                     related_log_id, operator, reason, transaction_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (eq_id, trans_type, qty_signed, price, total,
                      int(related_log_id) if related_log_id else None,
                      operator or None, reason or None,
                      trans_date or datetime.now().strftime('%Y-%m-%d')))

                update_equipment_quantity(conn, eq_id)
                conn.commit()
                flash('出入库操作成功！', 'success')
                return redirect(url_for('equipment_detail', eq_id=eq_id))
            except Exception as e:
                flash(f'操作失败：{str(e)}', 'error')
            finally:
                conn.close()
    else:
        conn.close()

    return render_template(
        'equipment_transaction.html',
        equipment=equipment,
        recent_logs=recent_logs
    )


@app.route('/equipment-transactions')
def transactions_list():
    conn = get_db()
    eq_filter = request.args.get('equipment', 'all', type=str)
    type_filter = request.args.get('type', 'all')
    date_start = request.args.get('date_start', '').strip()
    date_end = request.args.get('date_end', '').strip()

    query = '''
        SELECT et.*, e.name as equipment_name, e.category as equipment_category,
               (SELECT spot FROM fishing_logs WHERE id = et.related_log_id) as related_spot
        FROM equipment_transactions et
        JOIN equipments e ON et.equipment_id = e.id
        WHERE e.deleted_at IS NULL
    '''
    params = []

    if eq_filter.isdigit() and int(eq_filter) > 0:
        query += ' AND et.equipment_id = ?'
        params.append(int(eq_filter))

    if type_filter != 'all':
        query += ' AND et.type = ?'
        params.append(type_filter)

    if date_start:
        query += ' AND et.transaction_date >= ?'
        params.append(date_start)

    if date_end:
        query += ' AND et.transaction_date <= ?'
        params.append(date_end)

    query += ' ORDER BY et.transaction_date DESC, et.id DESC LIMIT 500'

    transactions = conn.execute(query, params).fetchall()
    equipments = get_all_equipments(conn)

    totals = conn.execute('''
        SELECT
            COALESCE(SUM(CASE WHEN type = 'in' THEN total_cost ELSE 0 END), 0) as total_in_cost,
            COALESCE(SUM(CASE WHEN type IN ('out', 'discard') THEN total_cost ELSE 0 END), 0) as total_out_cost
        FROM equipment_transactions et
        JOIN equipments e ON et.equipment_id = e.id
        WHERE e.deleted_at IS NULL
    ''').fetchone()

    conn.close()

    return render_template(
        'equipment_transactions.html',
        transactions=[dict(t) for t in transactions],
        equipments=equipments,
        eq_filter=eq_filter,
        type_filter=type_filter,
        date_start=date_start,
        date_end=date_end,
        totals=dict(totals)
    )


def get_log_equipments(conn, log_id):
    rows = conn.execute('''
        SELECT le.*, e.name as equipment_name, e.category as equipment_category,
               e.unit_price as equipment_price, e.spec as equipment_spec
        FROM log_equipments le
        JOIN equipments e ON le.equipment_id = e.id
        WHERE le.log_id = ?
        ORDER BY le.id
    ''', (log_id,)).fetchall()
    result = []
    total_cost = 0
    for r in rows:
        r_dict = dict(r)
        total_cost += r_dict.get('usage_cost') or 0
        result.append(r_dict)
    return result, round(total_cost, 2)


def save_log_equipments(conn, log_id, equipment_data):
    conn.execute('DELETE FROM log_equipments WHERE log_id = ?', (log_id,))

    for item in equipment_data:
        eq_id = item.get('equipment_id')
        quantity = item.get('quantity', 1)
        notes = item.get('notes', '')
        wear_rate = item.get('wear_rate', 0.05)

        if not eq_id:
            continue

        equipment = conn.execute('SELECT * FROM equipments WHERE id = ?', (eq_id,)).fetchone()
        if not equipment:
            continue

        usage_cost = calculate_usage_cost(dict(equipment), quantity, wear_rate)

        conn.execute('''
            INSERT INTO log_equipments
            (log_id, equipment_id, quantity, usage_cost, wear_rate, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (log_id, eq_id, quantity, usage_cost, wear_rate, notes or None))


@app.route('/equipments/api/search')
def api_equipments_search():
    conn = get_db()
    try:
        keyword = request.args.get('q', '').strip()
        category = request.args.get('category', '').strip()

        query = '''
            SELECT id, name, category, brand, spec, unit_price, available_quantity
            FROM equipments
            WHERE deleted_at IS NULL AND available_quantity > 0
        '''
        params = []

        if keyword:
            query += ' AND (name LIKE ? OR brand LIKE ? OR spec LIKE ?)'
            kw = f'%{keyword}%'
            params.extend([kw, kw, kw])

        if category:
            query += ' AND category = ?'
            params.append(category)

        query += ' ORDER BY category, name LIMIT 50'
        rows = conn.execute(query, params).fetchall()
        return jsonify({'success': True, 'data': [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_log_photos(conn, log_id):
    rows = conn.execute('''
        SELECT * FROM log_photos
        WHERE log_id = ?
        ORDER BY sort_order ASC, id ASC
    ''', (log_id,)).fetchall()
    photos = []
    for r in rows:
        photo = dict(r)
        photo['url'] = url_for('static', filename=f'uploads/{photo["filename"]}')
        photos.append(photo)
    return photos


def save_log_photos(conn, log_id, files):
    photo_count = 0
    existing_count = conn.execute(
        'SELECT COUNT(*) FROM log_photos WHERE log_id = ?',
        (log_id,)
    ).fetchone()[0]

    for idx, file in enumerate(files):
        if file and file.filename and allowed_file(file.filename):
            ext = file.filename.rsplit('.', 1)[1].lower()
            unique_name = f'{uuid.uuid4().hex}.{ext}'
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
            file.save(filepath)

            original_name = secure_filename(file.filename)
            sort_order = existing_count + idx
            conn.execute('''
                INSERT INTO log_photos (log_id, filename, original_name, sort_order)
                VALUES (?, ?, ?, ?)
            ''', (log_id, unique_name, original_name, sort_order))
            photo_count += 1
    return photo_count


@app.route('/log/<int:log_id>/photos/<int:photo_id>/delete', methods=['POST'])
def delete_photo(log_id, photo_id):
    conn = get_db()
    photo = conn.execute(
        'SELECT * FROM log_photos WHERE id = ? AND log_id = ?',
        (photo_id, log_id)
    ).fetchone()

    if photo is None:
        conn.close()
        flash('照片不存在！', 'error')
        return redirect(request.referrer or url_for('log_detail', log_id=log_id))

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo['filename'])
    if os.path.exists(filepath):
        os.remove(filepath)

    conn.execute('DELETE FROM log_photos WHERE id = ?', (photo_id,))
    conn.commit()
    conn.close()
    flash('照片已删除！', 'success')
    return redirect(request.referrer or url_for('log_detail', log_id=log_id))


@app.route('/log/<int:log_id>/photos/<int:photo_id>/caption', methods=['POST'])
def update_photo_caption(log_id, photo_id):
    caption = request.form.get('caption', '').strip()
    conn = get_db()
    photo = conn.execute(
        'SELECT id FROM log_photos WHERE id = ? AND log_id = ?',
        (photo_id, log_id)
    ).fetchone()

    if photo is None:
        conn.close()
        return jsonify({'success': False, 'message': '照片不存在'}), 404

    conn.execute(
        'UPDATE log_photos SET caption = ? WHERE id = ?',
        (caption or None, photo_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/gallery')
def gallery():
    conn = get_db()
    spot_param = request.args.get('spot', None)

    query = '''
        SELECT p.*, l.spot, l.created_at as log_date, l.harvest
        FROM log_photos p
        JOIN fishing_logs l ON p.log_id = l.id
        WHERE l.deleted_at IS NULL
    '''
    params = []

    if spot_param:
        query += ' AND l.spot = ?'
        params.append(spot_param)

    query += ' ORDER BY p.sort_order ASC, p.id DESC'

    all_photos = conn.execute(query, params).fetchall()

    spot_photos = {}
    spot_photo_count = {}
    for row in all_photos:
        spot = row['spot']
        if spot not in spot_photos:
            spot_photos[spot] = []
            spot_photo_count[spot] = 0
        photo = dict(row)
        photo['url'] = url_for('static', filename=f'uploads/{photo["filename"]}')
        spot_photos[spot].append(photo)
        spot_photo_count[spot] += 1

    all_spots = sorted(spot_photos.keys())

    total_photos = sum(spot_photo_count.values())

    conn.close()
    return render_template(
        'gallery.html',
        spot_photos=spot_photos,
        all_spots=all_spots,
        spot_photo_count=spot_photo_count,
        selected_spot=spot_param,
        total_photos=total_photos
    )


COMPARE_ENV_FIELDS = [
    ('spot', '钓点'),
    ('weather', '天气'),
    ('temperature', '温度'),
    ('humidity', '湿度'),
    ('wind', '风力'),
    ('water_level', '水位'),
]

COMPARE_HARVEST_FIELDS = [
    ('fish_species', '鱼种'),
    ('bait', '饵料'),
    ('harvest', '收获情况'),
]

COMPARE_OTHER_FIELDS = [
    ('created_at', '垂钓日期'),
    ('next_strategy', '下次策略'),
]


def analyze_env_differences(log1, log2):
    differences = []
    similarities = []

    for field_key, field_label in COMPARE_ENV_FIELDS:
        val1 = log1.get(field_key) or ''
        val2 = log2.get(field_key) or ''

        if val1 == val2:
            similarities.append({
                'field': field_key,
                'label': field_label,
                'value': val1
            })
        else:
            differences.append({
                'field': field_key,
                'label': field_label,
                'value1': val1,
                'value2': val2
            })

    return differences, similarities


def analyze_harvest_differences(log1, log2):
    differences = []
    similarities = []

    harvest_val1 = parse_harvest_value(log1.get('harvest', ''))
    harvest_val2 = parse_harvest_value(log2.get('harvest', ''))

    for field_key, field_label in COMPARE_HARVEST_FIELDS:
        val1 = log1.get(field_key) or ''
        val2 = log2.get(field_key) or ''

        if val1 == val2:
            similarities.append({
                'field': field_key,
                'label': field_label,
                'value': val1
            })
        else:
            differences.append({
                'field': field_key,
                'label': field_label,
                'value1': val1,
                'value2': val2
            })

    harvest_diff = harvest_val2 - harvest_val1
    harvest_pct = 0
    if harvest_val1 > 0:
        harvest_pct = round((harvest_diff / harvest_val1) * 100, 1)

    harvest_analysis = {
        'value1': harvest_val1,
        'value2': harvest_val2,
        'diff': harvest_diff,
        'pct': harvest_pct,
        'winner': 1 if harvest_val1 > harvest_val2 else (2 if harvest_val2 > harvest_val1 else 0),
        'display1': log1.get('harvest', ''),
        'display2': log2.get('harvest', ''),
    }

    return differences, similarities, harvest_analysis


def generate_conclusions(log1, log2, env_diffs, harvest_analysis):
    conclusions = []

    if harvest_analysis['winner'] == 1:
        conclusions.append({
            'type': 'harvest',
            'icon': '🎣',
            'title': '收获对比',
            'detail': f"记录 #{log1['id']} 收获更优，比记录 #{log2['id']} 多 {abs(harvest_analysis['diff'])}（{abs(harvest_analysis['pct'])}%）"
        })
    elif harvest_analysis['winner'] == 2:
        conclusions.append({
            'type': 'harvest',
            'icon': '🎣',
            'title': '收获对比',
            'detail': f"记录 #{log2['id']} 收获更优，比记录 #{log1['id']} 多 {abs(harvest_analysis['diff'])}（{abs(harvest_analysis['pct'])}%）"
        })
    else:
        conclusions.append({
            'type': 'harvest',
            'icon': '🎣',
            'title': '收获对比',
            'detail': '两条记录收获量相当'
        })

    spot_diff = next((d for d in env_diffs if d['field'] == 'spot'), None)
    if spot_diff:
        conclusions.append({
            'type': 'env',
            'icon': '📍',
            'title': '钓点差异',
            'detail': f"钓点不同（{spot_diff['value1']} vs {spot_diff['value2']}），可能是影响收获的主要因素"
        })

    weather_diff = next((d for d in env_diffs if d['field'] == 'weather'), None)
    if weather_diff:
        conclusions.append({
            'type': 'env',
            'icon': '🌤️',
            'title': '天气差异',
            'detail': f"天气状况不同（{weather_diff['value1']} vs {weather_diff['value2']}），鱼的活性可能有差异"
        })

    water_diff = next((d for d in env_diffs if d['field'] == 'water_level'), None)
    if water_diff:
        conclusions.append({
            'type': 'env',
            'icon': '💧',
            'title': '水位差异',
            'detail': f"水位不同（{water_diff['value1']} vs {water_diff['value2']}），会影响鱼的栖息位置"
        })

    bait_diff = next((d for d in COMPARE_HARVEST_FIELDS if d[0] == 'bait'), None)
    if bait_diff:
        bait_val1 = log1.get('bait', '')
        bait_val2 = log2.get('bait', '')
        if bait_val1 != bait_val2:
            conclusions.append({
                'type': 'strategy',
                'icon': '🐛',
                'title': '饵料差异',
                'detail': f"使用的饵料不同（{bait_val1} vs {bait_val2}），建议多次验证找出最适合的饵料"
            })

    temp_diff = next((d for d in env_diffs if d['field'] == 'temperature'), None)
    if temp_diff and temp_diff['value1'] and temp_diff['value2']:
        conclusions.append({
            'type': 'env',
            'icon': '🌡️',
            'title': '温度差异',
            'detail': f"温度不同（{temp_diff['value1']} vs {temp_diff['value2']}），水温会影响鱼的进食活跃度"
        })

    if not env_diffs:
        conclusions.append({
            'type': 'env',
            'icon': '✅',
            'title': '环境相似',
            'detail': '两次垂钓环境条件基本一致，可作为对照组分析其他因素'
        })

    return conclusions


@app.route('/compare')
def compare_select():
    conn = get_db()

    logs = conn.execute('''
        SELECT id, spot, created_at, harvest, fish_species, weather
        FROM fishing_logs
        WHERE deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 100
    ''').fetchall()

    log_list = []
    for log in logs:
        log_dict = dict(log)
        log_dict['harvest_value'] = parse_harvest_value(log['harvest'])
        log_list.append(log_dict)

    total = conn.execute(
        'SELECT COUNT(*) as cnt FROM fishing_logs WHERE deleted_at IS NULL'
    ).fetchone()['cnt']

    conn.close()

    log1_id = request.args.get('log1', type=int)
    log2_id = request.args.get('log2', type=int)

    return render_template(
        'compare.html',
        log_list=log_list,
        total_logs=total,
        log1_id=log1_id,
        log2_id=log2_id,
        show_results=False
    )


@app.route('/compare/<int:log_id1>/<int:log_id2>')
def compare_results(log_id1, log_id2):
    conn = get_db()

    log1 = conn.execute(
        'SELECT * FROM fishing_logs WHERE id = ? AND deleted_at IS NULL',
        (log_id1,)
    ).fetchone()

    log2 = conn.execute(
        'SELECT * FROM fishing_logs WHERE id = ? AND deleted_at IS NULL',
        (log_id2,)
    ).fetchone()

    if log1 is None or log2 is None:
        conn.close()
        flash('记录不存在！', 'error')
        return redirect(url_for('compare_select'))

    log1_dict = dict(log1)
    log2_dict = dict(log2)

    log1_photos = get_log_photos(conn, log_id1)
    log2_photos = get_log_photos(conn, log_id2)

    logs = conn.execute('''
        SELECT id, spot, created_at, harvest, fish_species, weather
        FROM fishing_logs
        WHERE deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 100
    ''').fetchall()

    log_list = []
    for log in logs:
        log_dict = dict(log)
        log_dict['harvest_value'] = parse_harvest_value(log['harvest'])
        log_list.append(log_dict)

    total = conn.execute(
        'SELECT COUNT(*) as cnt FROM fishing_logs WHERE deleted_at IS NULL'
    ).fetchone()['cnt']

    conn.close()

    env_diffs, env_similar = analyze_env_differences(log1_dict, log2_dict)
    harvest_diffs, harvest_similar, harvest_analysis = analyze_harvest_differences(log1_dict, log2_dict)
    conclusions = generate_conclusions(log1_dict, log2_dict, env_diffs, harvest_analysis)

    other_fields = []
    for field_key, field_label in COMPARE_OTHER_FIELDS:
        val1 = log1_dict.get(field_key) or ''
        val2 = log2_dict.get(field_key) or ''
        other_fields.append({
            'field': field_key,
            'label': field_label,
            'value1': val1,
            'value2': val2,
            'same': val1 == val2
        })

    return render_template(
        'compare.html',
        log_list=log_list,
        total_logs=total,
        log1_id=log_id1,
        log2_id=log_id2,
        log1=log1_dict,
        log2=log2_dict,
        log1_photos=log1_photos,
        log2_photos=log2_photos,
        show_results=True,
        env_diffs=env_diffs,
        env_similar=env_similar,
        harvest_diffs=harvest_diffs,
        harvest_similar=harvest_similar,
        harvest_analysis=harvest_analysis,
        conclusions=conclusions,
        other_fields=other_fields
    )


@app.route('/wiki')
def wiki_list():
    conn = get_db()
    sort_by = request.args.get('sort', 'recent')
    keyword = request.args.get('q', '').strip()
    has_ban_filter = request.args.get('ban', None)
    has_fee_filter = request.args.get('fee', None)

    query = '''
        SELECT w.*,
               (SELECT COUNT(*) FROM fishing_logs l WHERE l.spot = w.spot_name AND l.deleted_at IS NULL) as visit_count
        FROM spot_wiki w
        WHERE w.deleted_at IS NULL
    '''
    params = []
    conditions = []

    if keyword:
        conditions.append('(w.spot_name LIKE ? OR w.road_condition LIKE ? OR w.parking_info LIKE ? OR w.ban_info LIKE ? OR w.tips LIKE ?)')
        like = f'%{keyword}%'
        params.extend([like, like, like, like, like])

    if has_ban_filter == 'yes':
        conditions.append('w.ban_info IS NOT NULL AND w.ban_info != ""')
    elif has_ban_filter == 'no':
        conditions.append('(w.ban_info IS NULL OR w.ban_info = "")')

    if has_fee_filter == 'free':
        conditions.append('(w.fishing_fee LIKE ? OR w.fishing_fee IS NULL OR w.fishing_fee = "")')
        params.append('%免费%')
    elif has_fee_filter == 'paid':
        conditions.append('w.fishing_fee IS NOT NULL AND w.fishing_fee != "" AND w.fishing_fee NOT LIKE ?')
        params.append('%免费%')

    if conditions:
        query += ' AND ' + ' AND '.join(conditions)

    if sort_by == 'name':
        query += ' ORDER BY w.spot_name'
    elif sort_by == 'visit':
        query += ' ORDER BY visit_count DESC'
    elif sort_by == 'info_date':
        query += ' ORDER BY w.info_date DESC NULLS LAST'
    else:
        query += ' ORDER BY w.updated_at DESC, w.created_at DESC'

    wikis = conn.execute(query, params).fetchall()
    wiki_list_data = [dict(w) for w in wikis]

    stats = conn.execute('''
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN ban_info IS NOT NULL AND ban_info != '' THEN 1 ELSE 0 END) as ban_count,
            SUM(CASE WHEN fishing_fee IS NOT NULL AND fishing_fee != '' AND fishing_fee NOT LIKE '%免费%' THEN 1 ELSE 0 END) as paid_count
        FROM spot_wiki WHERE deleted_at IS NULL
    ''').fetchone()

    conn.close()
    return render_template(
        'spot_wiki_list.html',
        wikis=wiki_list_data,
        sort_by=sort_by,
        keyword=keyword,
        has_ban_filter=has_ban_filter,
        has_fee_filter=has_fee_filter,
        total=stats['total'] or 0,
        ban_count=stats['ban_count'] or 0,
        paid_count=stats['paid_count'] or 0
    )


def get_wiki_spot_options(conn):
    spots = conn.execute('''
        SELECT id, name FROM fishing_spots WHERE deleted_at IS NULL ORDER BY name
    ''').fetchall()
    return [dict(s) for s in spots]


@app.route('/wiki/add', methods=['GET', 'POST'])
def wiki_add():
    conn = get_db()
    spot_options = get_wiki_spot_options(conn)

    if request.method == 'POST':
        spot_id = request.form.get('spot_id', '').strip()
        spot_name = request.form['spot_name'].strip()
        road_condition = request.form.get('road_condition', '').strip()
        parking_info = request.form.get('parking_info', '').strip()
        parking_fee = request.form.get('parking_fee', '').strip()
        fishing_fee = request.form.get('fishing_fee', '').strip()
        ban_info = request.form.get('ban_info', '').strip()
        best_time = request.form.get('best_time', '').strip()
        water_features = request.form.get('water_features', '').strip()
        suitable_methods = request.form.get('suitable_methods', '').strip()
        facilities = request.form.get('facilities', '').strip()
        safety_notes = request.form.get('safety_notes', '').strip()
        tips = request.form.get('tips', '').strip()
        source = request.form.get('source', '').strip()
        info_date = request.form.get('info_date', '').strip()

        if not spot_name:
            flash('请填写钓点名称！', 'error')
        else:
            try:
                sid = int(spot_id) if spot_id else None
                cursor = conn.execute('''
                    INSERT INTO spot_wiki (
                        spot_id, spot_name, road_condition, parking_info, parking_fee,
                        fishing_fee, ban_info, best_time, water_features, suitable_methods,
                        facilities, safety_notes, tips, source, info_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    sid, spot_name,
                    road_condition or None, parking_info or None, parking_fee or None,
                    fishing_fee or None, ban_info or None, best_time or None,
                    water_features or None, suitable_methods or None,
                    facilities or None, safety_notes or None, tips or None,
                    source or None, info_date or None
                ))
                conn.commit()
                flash('钓点百科添加成功！', 'success')
                conn.close()
                return redirect(url_for('wiki_detail', wiki_id=cursor.lastrowid))
            except Exception as e:
                flash(f'添加失败：{str(e)}', 'error')

    conn.close()
    return render_template('spot_wiki_form.html', wiki=None, spot_options=spot_options)


@app.route('/wiki/<int:wiki_id>')
def wiki_detail(wiki_id):
    conn = get_db()
    wiki = conn.execute('SELECT * FROM spot_wiki WHERE id = ? AND deleted_at IS NULL', (wiki_id,)).fetchone()
    if wiki is None:
        conn.close()
        flash('钓点百科不存在！', 'error')
        return redirect(url_for('wiki_list'))

    wiki_dict = dict(wiki)

    visit_count = conn.execute('''
        SELECT COUNT(*) as cnt FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
    ''', (wiki_dict['spot_name'],)).fetchone()['cnt']
    wiki_dict['visit_count'] = visit_count

    related_spot = None
    if wiki_dict['spot_id']:
        related_spot = conn.execute('''
            SELECT id, name, is_favorite, latitude, longitude, address, description
            FROM fishing_spots WHERE id = ? AND deleted_at IS NULL
        ''', (wiki_dict['spot_id'],)).fetchone()

    related_logs = conn.execute('''
        SELECT id, created_at, weather, water_level, bait, fish_species, harvest
        FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 10
    ''', (wiki_dict['spot_name'],)).fetchall()

    conn.close()
    return render_template(
        'spot_wiki_detail.html',
        wiki=wiki_dict,
        related_spot=dict(related_spot) if related_spot else None,
        related_logs=[dict(l) for l in related_logs]
    )


@app.route('/wiki/<int:wiki_id>/edit', methods=['GET', 'POST'])
def wiki_edit(wiki_id):
    conn = get_db()
    wiki = conn.execute('SELECT * FROM spot_wiki WHERE id = ? AND deleted_at IS NULL', (wiki_id,)).fetchone()
    spot_options = get_wiki_spot_options(conn)

    if wiki is None:
        conn.close()
        flash('钓点百科不存在！', 'error')
        return redirect(url_for('wiki_list'))

    if request.method == 'POST':
        spot_id = request.form.get('spot_id', '').strip()
        spot_name = request.form['spot_name'].strip()
        road_condition = request.form.get('road_condition', '').strip()
        parking_info = request.form.get('parking_info', '').strip()
        parking_fee = request.form.get('parking_fee', '').strip()
        fishing_fee = request.form.get('fishing_fee', '').strip()
        ban_info = request.form.get('ban_info', '').strip()
        best_time = request.form.get('best_time', '').strip()
        water_features = request.form.get('water_features', '').strip()
        suitable_methods = request.form.get('suitable_methods', '').strip()
        facilities = request.form.get('facilities', '').strip()
        safety_notes = request.form.get('safety_notes', '').strip()
        tips = request.form.get('tips', '').strip()
        source = request.form.get('source', '').strip()
        info_date = request.form.get('info_date', '').strip()

        if not spot_name:
            flash('请填写钓点名称！', 'error')
        else:
            try:
                sid = int(spot_id) if spot_id else None
                conn.execute('''
                    UPDATE spot_wiki SET
                        spot_id = ?, spot_name = ?, road_condition = ?,
                        parking_info = ?, parking_fee = ?, fishing_fee = ?,
                        ban_info = ?, best_time = ?, water_features = ?,
                        suitable_methods = ?, facilities = ?, safety_notes = ?,
                        tips = ?, source = ?, info_date = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    sid, spot_name,
                    road_condition or None, parking_info or None, parking_fee or None,
                    fishing_fee or None, ban_info or None, best_time or None,
                    water_features or None, suitable_methods or None,
                    facilities or None, safety_notes or None, tips or None,
                    source or None, info_date or None, wiki_id
                ))
                conn.commit()
                flash('钓点百科更新成功！', 'success')
                conn.close()
                return redirect(url_for('wiki_detail', wiki_id=wiki_id))
            except Exception as e:
                flash(f'更新失败：{str(e)}', 'error')

    conn.close()
    return render_template('spot_wiki_form.html', wiki=dict(wiki), spot_options=spot_options)


@app.route('/wiki/<int:wiki_id>/delete', methods=['POST'])
def wiki_delete(wiki_id):
    conn = get_db()
    wiki = conn.execute('SELECT id FROM spot_wiki WHERE id = ? AND deleted_at IS NULL', (wiki_id,)).fetchone()
    if wiki is None:
        conn.close()
        flash('钓点百科不存在！', 'error')
        return redirect(url_for('wiki_list'))

    conn.execute('UPDATE spot_wiki SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?', (wiki_id,))
    conn.commit()
    conn.close()
    flash('钓点百科已移入回收站！', 'success')
    return redirect(url_for('wiki_list'))


@app.route('/wiki/import/<spot_name>')
def wiki_import_spot(spot_name):
    conn = get_db()
    spot_name_decoded = urllib.parse.unquote(spot_name)

    existing = conn.execute('''
        SELECT id FROM spot_wiki WHERE spot_name = ? AND deleted_at IS NULL
    ''', (spot_name_decoded,)).fetchone()

    if existing:
        conn.close()
        flash('该钓点百科已存在！', 'error')
        return redirect(url_for('wiki_detail', wiki_id=existing['id']))

    spot_info = conn.execute('''
        SELECT id, name, description, address FROM fishing_spots
        WHERE name = ? AND deleted_at IS NULL
    ''', (spot_name_decoded,)).fetchone()

    sid = None
    desc = None
    if spot_info:
        sid = spot_info['id']
        desc = spot_info['description']

    cursor = conn.execute('''
        INSERT INTO spot_wiki (spot_id, spot_name, tips, info_date)
        VALUES (?, ?, ?, ?)
    ''', (sid, spot_name_decoded, desc or None, datetime.now().strftime('%Y-%m-%d')))

    conn.commit()
    conn.close()
    flash('已从钓点管理导入，快去完善信息吧！', 'success')
    return redirect(url_for('wiki_edit', wiki_id=cursor.lastrowid))


def get_spot_harvest_stats(conn, spot_name):
    rows = conn.execute('''
        SELECT harvest, weather, water_level, created_at
        FROM fishing_logs
        WHERE spot = ? AND deleted_at IS NULL
        ORDER BY created_at DESC
    ''', (spot_name,)).fetchall()

    total_count = len(rows)
    if total_count == 0:
        return {
            'total_count': 0,
            'avg_harvest': 0,
            'total_harvest': 0,
            'skunk_count': 0,
            'skunk_rate': 0,
            'weather_stats': {},
            'water_level_stats': {},
            'best_weather': '',
            'best_water_level': '',
            'last_log_date': None
        }

    harvest_values = []
    skunk_count = 0
    weather_harvests = {}
    water_level_harvests = {}
    last_date = None

    for row in rows:
        val = parse_harvest_value(row['harvest'])
        harvest_values.append(val)
        if val == 0:
            skunk_count += 1

        weather = normalize_weather_desc(row['weather']) or '未知'
        if weather not in weather_harvests:
            weather_harvests[weather] = []
        weather_harvests[weather].append(val)

        water_level = normalize_water_level(row['water_level']) or '未知'
        if water_level not in water_level_harvests:
            water_level_harvests[water_level] = []
        water_level_harvests[water_level].append(val)

        if last_date is None:
            last_date = row['created_at']

    avg_harvest = round(sum(harvest_values) / total_count, 2) if total_count > 0 else 0
    total_harvest = sum(harvest_values)
    skunk_rate = round((skunk_count / total_count) * 100, 1) if total_count > 0 else 0

    weather_stats = {}
    best_weather = ''
    best_weather_avg = -1
    for w, vals in weather_harvests.items():
        w_avg = round(sum(vals) / len(vals), 2) if vals else 0
        w_count = len(vals)
        weather_stats[w] = {
            'avg_harvest': w_avg,
            'count': w_count,
            'total_harvest': sum(vals)
        }
        if w_avg > best_weather_avg and w_count >= 1:
            best_weather_avg = w_avg
            best_weather = w

    water_level_stats = {}
    best_water_level = ''
    best_wl_avg = -1
    for wl, vals in water_level_harvests.items():
        wl_avg = round(sum(vals) / len(vals), 2) if vals else 0
        wl_count = len(vals)
        water_level_stats[wl] = {
            'avg_harvest': wl_avg,
            'count': wl_count,
            'total_harvest': sum(vals)
        }
        if wl_avg > best_wl_avg and wl_count >= 1:
            best_wl_avg = wl_avg
            best_water_level = wl

    return {
        'total_count': total_count,
        'avg_harvest': avg_harvest,
        'total_harvest': total_harvest,
        'skunk_count': skunk_count,
        'skunk_rate': skunk_rate,
        'weather_stats': weather_stats,
        'water_level_stats': water_level_stats,
        'best_weather': best_weather,
        'best_water_level': best_water_level,
        'last_log_date': last_date
    }


def get_weather_match_score(spot_stats, target_weather):
    weather_stats = spot_stats['weather_stats']
    if not weather_stats:
        return 0.5

    if not target_weather:
        best_w = spot_stats.get('best_weather', '')
        if best_w and best_w in weather_stats:
            spot_avg = spot_stats['avg_harvest']
            if spot_avg > 0:
                score = weather_stats[best_w]['avg_harvest'] / spot_avg
                return min(score, 1.5)
        return 0.8

    if target_weather in weather_stats:
        w_stat = weather_stats[target_weather]
        spot_avg = spot_stats['avg_harvest']
        if spot_avg > 0:
            score = w_stat['avg_harvest'] / spot_avg
            return min(score, 2.0)
        return 0.5

    return 0.3


def find_similar_weather(target_weather, weather_stats):
    weather_groups = {
        '晴': ['晴', 'Sunny', '晴天'],
        '多云': ['多云', '阴', '阴天', 'Cloudy'],
        '小雨': ['小雨', '雨', '阵雨', '毛毛雨', 'Rain'],
        '大雨': ['大雨', '暴雨', '中雨', '雷阵雨'],
        '雪': ['雪', '小雪', '大雪', 'Snow']
    }

    target_group = None
    for group, weathers in weather_groups.items():
        if target_weather in weathers:
            target_group = group
            break

    if target_group:
        for w in weather_groups.get(target_group, []):
            if w in weather_stats:
                return w

    return None


def get_water_level_match_score(spot_stats, target_water_level):
    water_level_stats = spot_stats['water_level_stats']
    if not water_level_stats:
        return 0.5

    if not target_water_level:
        best_wl = spot_stats.get('best_water_level', '')
        if best_wl and best_wl in water_level_stats:
            spot_avg = spot_stats['avg_harvest']
            if spot_avg > 0:
                score = water_level_stats[best_wl]['avg_harvest'] / spot_avg
                return min(score, 1.5)
        return 0.8

    if target_water_level in water_level_stats:
        wl_stat = water_level_stats[target_water_level]
        spot_avg = spot_stats['avg_harvest']
        if spot_avg > 0:
            score = wl_stat['avg_harvest'] / spot_avg
            return min(score, 2.0)
        return 0.5

    return 0.3


def find_similar_water_level(target_wl, water_level_stats):
    wl_groups = {
        '高': ['高', '偏高', '涨水', 'High', '高水位'],
        '正常': ['正常', 'Normal', '适中', '中水位'],
        '低': ['低', '偏低', '退水', 'Low', '低水位', '枯水']
    }

    target_group = None
    for group, levels in wl_groups.items():
        if target_wl in levels:
            target_group = group
            break

    if target_group:
        for wl in wl_groups.get(target_group, []):
            if wl in water_level_stats:
                return wl

    return None


def calculate_spot_score(spot_stats, target_weather, target_water_level):
    harvest_performance = spot_stats['avg_harvest']
    skunk_penalty = spot_stats['skunk_rate'] / 100.0

    base_harvest_score = harvest_performance * (1 - skunk_penalty * 0.5)

    weather_score = get_weather_match_score(spot_stats, target_weather)
    water_level_score = get_water_level_match_score(spot_stats, target_water_level)

    total_score = (base_harvest_score * 0.4) + (weather_score * harvest_performance * 0.3) + (water_level_score * harvest_performance * 0.3)

    if spot_stats['total_count'] < 3:
        data_confidence = spot_stats['total_count'] / 3.0
        total_score = total_score * (0.5 + data_confidence * 0.5)

    return round(total_score, 2)


def get_recommended_spots(conn, target_weather='', target_water_level='', limit=10):
    spot_names = conn.execute('''
        SELECT DISTINCT spot FROM fishing_logs
        WHERE spot IS NOT NULL AND spot != '' AND deleted_at IS NULL
    ''').fetchall()

    spots_data = []
    for row in spot_names:
        spot_name = row['spot']
        stats = get_spot_harvest_stats(conn, spot_name)

        if stats['total_count'] == 0:
            continue

        score = calculate_spot_score(stats, target_weather, target_water_level)

        spots_data.append({
            'name': spot_name,
            'score': score,
            'stats': stats
        })

    spots_data.sort(key=lambda x: x['score'], reverse=True)

    for idx, spot in enumerate(spots_data, 1):
        spot['rank'] = idx

    return spots_data[:limit]


def get_common_weathers(conn):
    rows = conn.execute('''
        SELECT weather FROM fishing_logs
        WHERE weather IS NOT NULL AND weather != '' AND deleted_at IS NULL
    ''').fetchall()
    freq = {}
    for r in rows:
        cat = normalize_weather_desc(r['weather'])
        if cat:
            freq[cat] = freq.get(cat, 0) + 1
    sorted_items = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [{'name': k, 'count': v} for k, v in sorted_items]


def normalize_water_level(water_level):
    if not water_level:
        return ''
    wl = water_level.strip()
    high_kw = ['涨', '高', 'High', '满', '溢', '偏高', 'above', 'rising']
    normal_kw = ['正常', 'Normal', '平', '稳', 'normal', 'stable']
    low_kw = ['退', '低', 'Low', '枯', '偏低', 'below', '干', '降', '落']
    for kw in high_kw:
        if kw.lower() in wl.lower() or wl in kw:
            return '高'
    for kw in normal_kw:
        if kw.lower() in wl.lower() or wl in kw:
            return '正常'
    for kw in low_kw:
        if kw.lower() in wl.lower() or wl in kw:
            return '低'
    return ''


def get_common_water_levels(conn):
    rows = conn.execute('''
        SELECT water_level FROM fishing_logs
        WHERE water_level IS NOT NULL AND water_level != '' AND deleted_at IS NULL
    ''').fetchall()
    freq = {}
    for r in rows:
        cat = normalize_water_level(r['water_level'])
        if cat:
            freq[cat] = freq.get(cat, 0) + 1
    order = {'高': 0, '正常': 1, '低': 2}
    sorted_items = sorted(freq.items(), key=lambda x: (order.get(x[0], 99), -x[1]))
    return [{'name': k, 'count': v} for k, v in sorted_items]


def get_latest_weather(conn):
    row = conn.execute('''
        SELECT weather, temperature, humidity, wind FROM fishing_logs
        WHERE weather IS NOT NULL AND weather != '' AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    ''').fetchone()
    return dict(row) if row else None


def get_latest_water_level(conn):
    row = conn.execute('''
        SELECT water_level FROM fishing_logs
        WHERE water_level IS NOT NULL AND water_level != '' AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
    ''').fetchone()
    return row['water_level'] if row else ''


def normalize_weather_desc(weather_desc):
    weather_mapping = {
        '晴': ['晴', 'Sunny', '晴天', '晴朗', 'Clear'],
        '多云': ['多云', '阴', '阴天', 'Cloudy', 'Partly cloudy', 'Overcast', 'Mist', 'Fog', '雾'],
        '小雨': ['小雨', '雨', '阵雨', '毛毛雨', 'Rain', 'Light rain', 'Drizzle', 'Patchy rain possible'],
        '大雨': ['大雨', '暴雨', '中雨', '雷阵雨', 'Heavy rain', 'Moderate rain', 'Thunderstorm', '雷暴'],
        '雪': ['雪', '小雪', '大雪', 'Snow', 'Light snow', 'Heavy snow', 'Sleet']
    }
    if not weather_desc:
        return ''
    desc_lower = weather_desc.strip()
    for standard, keywords in weather_mapping.items():
        for kw in keywords:
            if kw.lower() in desc_lower.lower() or desc_lower in kw:
                return standard
    return ''


def fetch_today_weather():
    try:
        city = WEATHER_API_CONFIG['city']
        encoded_city = urllib.parse.quote(city)
        url = f'https://wttr.in/{encoded_city}?format=j1'
        req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.68.0'})
        with urllib.request.urlopen(req, timeout=WEATHER_API_CONFIG['timeout']) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        current = data.get('current_condition', [{}])[0]
        weather_desc = ''
        if current.get('lang_zh'):
            weather_desc = current['lang_zh'][0].get('value', '')
        if not weather_desc and current.get('weatherDesc'):
            weather_desc = current['weatherDesc'][0].get('value', '')
        temp_c = current.get('temp_C', '')
        normalized = normalize_weather_desc(weather_desc)
        return {
            'success': True,
            'weather_desc': weather_desc,
            'normalized_weather': normalized,
            'temperature': f'{temp_c}℃' if temp_c else '',
            'city': city
        }
    except Exception as e:
        return {
            'success': False,
            'message': str(e)
        }


@app.route('/api/recommend/today-weather')
def api_recommend_today_weather():
    result = fetch_today_weather()
    return jsonify(result)


@app.route('/recommend')
def spot_recommendation():
    conn = get_db()

    target_weather = request.args.get('weather', '').strip()
    target_water_level = request.args.get('water_level', '').strip()
    auto_weather = request.args.get('auto', '0') == '1'

    common_weathers = get_common_weathers(conn)
    common_water_levels = get_common_water_levels(conn)

    today_weather_info = None
    if not target_weather and auto_weather:
        today_weather_info = fetch_today_weather()
        if today_weather_info.get('success'):
            target_weather = today_weather_info.get('normalized_weather', '')

    recommended_spots = get_recommended_spots(
        conn,
        target_weather=target_weather,
        target_water_level=target_water_level,
        limit=20
    )

    today = datetime.now().strftime('%Y年%m月%d日')

    conn.close()

    return render_template(
        'spot_recommendation.html',
        recommended_spots=recommended_spots,
        target_weather=target_weather,
        target_water_level=target_water_level,
        common_weathers=common_weathers,
        common_water_levels=common_water_levels,
        today=today,
        today_weather_info=today_weather_info
    )


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='127.0.0.1', port=5000)
