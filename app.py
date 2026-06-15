import csv
import io
import os
import re
import sqlite3
import urllib.request
import urllib.parse
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response

app = Flask(__name__)
app.secret_key = 'fishing_log_secret_key'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fishing_log.db')

WEATHER_API_CONFIG = {
    'city': '北京',
    'timeout': 5,
}


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
            field_name TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (log_id) REFERENCES fishing_logs(id) ON DELETE CASCADE
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
    'created_at': '日期',
    'temperature': '温度',
    'humidity': '湿度',
    'wind': '风力'
}

EDITABLE_FIELDS = [
    'spot', 'weather', 'water_level', 'bait', 'fish_species',
    'harvest', 'next_strategy', 'created_at', 'temperature',
    'humidity', 'wind'
]


def record_audit_log(conn, log_id, field_name, old_value, new_value):
    if old_value != new_value:
        conn.execute('''
            INSERT INTO audit_logs (log_id, field_name, old_value, new_value)
            VALUES (?, ?, ?, ?)
        ''', (log_id, field_name, old_value, new_value))


def get_audit_logs(conn, log_id):
    rows = conn.execute('''
        SELECT * FROM audit_logs
        WHERE log_id = ?
        ORDER BY changed_at DESC, id DESC
    ''', (log_id,)).fetchall()

    logs = []
    current_group = None
    for row in rows:
        ts = row['changed_at']
        if current_group is None or current_group['timestamp'] != ts:
            if current_group is not None:
                logs.append(current_group)
            current_group = {
                'timestamp': ts,
                'changes': []
            }
        current_group['changes'].append({
            'field': row['field_name'],
            'field_label': FIELD_LABELS.get(row['field_name'], row['field_name']),
            'old_value': row['old_value'],
            'new_value': row['new_value']
        })
    if current_group is not None:
        logs.append(current_group)
    return logs


@app.route('/log/<int:log_id>/edit', methods=['GET', 'POST'])
def edit_log(log_id):
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort', 'date_desc')
    per_page = request.args.get('per_page', 20, type=int)

    conn = get_db()
    log = conn.execute('SELECT * FROM fishing_logs WHERE id = ?', (log_id,)).fetchone()
    bait_list = get_all_baits(conn)

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
        created_at = request.form['created_at'] or datetime.now().strftime('%Y-%m-%d')
        temperature = request.form.get('temperature', '').strip()
        humidity = request.form.get('humidity', '').strip()
        wind = request.form.get('wind', '').strip()

        if not spot or not weather or not water_level or not bait or not fish_species or not harvest:
            flash('请填写所有必填项！', 'error')
        else:
            old_data = dict(log)
            new_data = {
                'spot': spot,
                'weather': weather,
                'water_level': water_level,
                'bait': bait,
                'fish_species': fish_species,
                'harvest': harvest,
                'next_strategy': next_strategy,
                'created_at': created_at,
                'temperature': temperature or None,
                'humidity': humidity or None,
                'wind': wind or None
            }

            for field in EDITABLE_FIELDS:
                old_val = old_data.get(field)
                new_val = new_data.get(field)
                record_audit_log(conn, log_id, field, old_val, new_val)

            conn.execute('''
                UPDATE fishing_logs
                SET spot = ?, weather = ?, water_level = ?, bait = ?, fish_species = ?,
                    harvest = ?, next_strategy = ?, created_at = ?, temperature = ?,
                    humidity = ?, wind = ?
                WHERE id = ?
            ''', (spot, weather, water_level, bait, fish_species, harvest,
                  next_strategy, created_at, temperature or None, humidity or None,
                  wind or None, log_id))
            conn.commit()
            flash('记录更新成功！', 'success')
            conn.close()
            return redirect(url_for('log_detail', log_id=log_id, page=page, sort=sort_by, per_page=per_page))

    conn.close()
    return render_template('edit.html', log=log, bait_list=bait_list,
                           page=page, sort_by=sort_by, per_page=per_page,
                           default_city=WEATHER_API_CONFIG['city'])


@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    sort_by = request.args.get('sort', 'date_desc')

    per_page = max(10, min(per_page, 100))

    sort_options = {
        'date_desc': ('created_at DESC, id DESC', '日期降序'),
        'date_asc': ('created_at ASC, id ASC', '日期升序'),
        'spot_desc': ('spot DESC, created_at DESC, id DESC', '钓点降序'),
        'spot_asc': ('spot ASC, created_at DESC, id DESC', '钓点升序'),
    }
    order_clause, sort_label = sort_options.get(sort_by, sort_options['date_desc'])

    conn = get_db()

    count_result = conn.execute('SELECT COUNT(*) as total FROM fishing_logs').fetchone()
    total = count_result['total']
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    logs = conn.execute(
        f'SELECT * FROM fishing_logs ORDER BY {order_clause} LIMIT ? OFFSET ?',
        (per_page, offset)
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
        sort_label=sort_label
    )


@app.route('/add', methods=['GET', 'POST'])
def add_log():
    conn = get_db()
    bait_list = get_all_baits(conn)

    if request.method == 'POST':
        spot = request.form['spot'].strip()
        weather = request.form['weather'].strip()
        water_level = request.form['water_level'].strip()
        bait = request.form['bait'].strip()
        fish_species = request.form['fish_species'].strip()
        harvest = request.form['harvest'].strip()
        next_strategy = request.form['next_strategy'].strip()
        created_at = request.form['created_at'] or datetime.now().strftime('%Y-%m-%d')
        temperature = request.form.get('temperature', '').strip()
        humidity = request.form.get('humidity', '').strip()
        wind = request.form.get('wind', '').strip()

        if not spot or not weather or not water_level or not bait or not fish_species or not harvest:
            flash('请填写所有必填项！', 'error')
        else:
            conn.execute('''
                INSERT INTO fishing_logs (spot, weather, water_level, bait, fish_species, harvest, next_strategy, created_at, temperature, humidity, wind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (spot, weather, water_level, bait, fish_species, harvest, next_strategy, created_at, temperature or None, humidity or None, wind or None))
            conn.commit()
            flash('记录添加成功！', 'success')
            conn.close()
            return redirect(url_for('index'))

    conn.close()
    return render_template('add.html', bait_list=bait_list, default_city=WEATHER_API_CONFIG['city'])


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
    conn.close()
    return render_template('detail.html', log=log, audit_logs=audit_logs,
                           page=page, sort_by=sort_by, per_page=per_page)


@app.route('/by-spot')
def by_spot():
    conn = get_db()
    spots = conn.execute('SELECT DISTINCT spot FROM fishing_logs ORDER BY spot').fetchall()
    spot_list = [row['spot'] for row in spots]

    selected_spot = request.args.get('spot', spot_list[0] if spot_list else None)
    logs = []
    if selected_spot:
        logs = conn.execute(
            'SELECT * FROM fishing_logs WHERE spot = ? ORDER BY created_at DESC, id DESC',
            (selected_spot,)
        ).fetchall()
    conn.close()

    return render_template('by_spot.html', spot_list=spot_list, selected_spot=selected_spot, logs=logs)


@app.route('/by-date')
def by_date():
    conn = get_db()
    dates = conn.execute(
        'SELECT DISTINCT created_at FROM fishing_logs ORDER BY created_at DESC'
    ).fetchall()
    date_list = [row['created_at'] for row in dates]

    selected_date = request.args.get('date', date_list[0] if date_list else None)
    logs = []
    if selected_date:
        logs = conn.execute(
            'SELECT * FROM fishing_logs WHERE created_at = ? ORDER BY id DESC',
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
    conn.execute('DELETE FROM fishing_logs WHERE id = ?', (log_id,))
    conn.commit()
    conn.close()
    flash('记录已删除！', 'success')
    return redirect(url_for('index', page=page, sort=sort_by, per_page=per_page))


def get_spot_visit_count(conn, spot_name):
    result = conn.execute(
        'SELECT COUNT(*) as cnt FROM fishing_logs WHERE spot = ?',
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
    '''
    params = []
    conditions = []

    if filter_type == 'favorite':
        conditions.append('s.is_favorite = 1')

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)

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
        WHERE spot = ? AND bait IS NOT NULL AND bait != ''
        GROUP BY bait
        ORDER BY use_count DESC
        LIMIT 3
    ''', (spot_name,)).fetchall()
    return [{'name': r['bait'], 'count': r['use_count']} for r in rows]


def get_spot_best_harvest(conn, spot_name):
    rows = conn.execute('''
        SELECT id, harvest, created_at, fish_species, bait
        FROM fishing_logs
        WHERE spot = ? AND harvest IS NOT NULL AND harvest != ''
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
        'SELECT * FROM fishing_logs WHERE spot = ? ORDER BY created_at DESC, id DESC',
        (spot['name'],)
    ).fetchall()

    conn.close()
    return render_template('spot_detail.html', spot=spot_dict, ratings=ratings, related_logs=related_logs,
                           bait_stats=bait_stats, best_harvest=best_harvest)


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
        WHERE spot = ?
        GROUP BY created_at
        ORDER BY created_at DESC
    ''', (spot['name'],)).fetchall()

    monthly = conn.execute('''
        SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as count
        FROM fishing_logs
        WHERE spot = ?
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
    conn.execute('DELETE FROM fishing_spots WHERE id = ?', (spot_id,))
    conn.commit()
    conn.close()
    flash('钓点已删除！', 'success')
    return redirect(url_for('spots_list'))


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


@app.route('/monthly-report')
def monthly_report():
    conn = get_db()

    months = conn.execute('''
        SELECT DISTINCT strftime('%Y-%m', created_at) as month
        FROM fishing_logs
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
            WHERE strftime('%Y-%m', created_at) = ?
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
            WHERE strftime('%Y-%m', created_at) = ?
            GROUP BY spot
        ''', (selected_month,)).fetchall()

        total_spots = len(spot_rows)

        all_logs = conn.execute('''
            SELECT harvest FROM fishing_logs
            WHERE strftime('%Y-%m', created_at) = ?
        ''', (selected_month,)).fetchall()

        total_harvest = sum(parse_harvest_value(log['harvest']) for log in all_logs)

        spot_data = []
        for row in spot_rows:
            spot_logs = conn.execute('''
                SELECT harvest FROM fishing_logs
                WHERE spot = ? AND strftime('%Y-%m', created_at) = ?
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
    baits = conn.execute('SELECT * FROM baits ORDER BY name').fetchall()
    return [dict(b) for b in baits]


def get_bait_usage_stats(conn, bait_name):
    logs = conn.execute(
        'SELECT harvest FROM fishing_logs WHERE bait = ?',
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

    baits = conn.execute('SELECT * FROM baits ORDER BY name').fetchall()

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
        'SELECT DISTINCT bait FROM fishing_logs WHERE bait NOT IN (SELECT name FROM baits) ORDER BY bait'
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
    conn.execute('DELETE FROM baits WHERE id = ?', (bait_id,))
    conn.commit()
    conn.close()
    flash('饵料已删除！', 'success')
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
            WHERE weather IS NOT NULL AND weather != ''
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


@app.route('/search')
def search_logs():
    conn = get_db()

    spot_list = [row['spot'] for row in conn.execute('SELECT DISTINCT spot FROM fishing_logs ORDER BY spot').fetchall()]
    species_list = [row['fish_species'] for row in conn.execute('SELECT DISTINCT fish_species FROM fishing_logs ORDER BY fish_species').fetchall()]

    date_start = request.args.get('date_start', '').strip()
    date_end = request.args.get('date_end', '').strip()
    selected_spot = request.args.get('spot', '').strip()
    selected_species = request.args.get('fish_species', '').strip()

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

    logs = []
    if conditions:
        where_clause = ' AND '.join(conditions)
        logs = conn.execute(
            f'SELECT * FROM fishing_logs WHERE {where_clause} ORDER BY created_at DESC, id DESC',
            params
        ).fetchall()

    conn.close()

    has_filter = bool(date_start or date_end or selected_spot or selected_species)

    return render_template(
        'search.html',
        spot_list=spot_list,
        species_list=species_list,
        date_start=date_start,
        date_end=date_end,
        selected_spot=selected_spot,
        selected_species=selected_species,
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

    if not conditions:
        conn.close()
        flash('请至少设置一个筛选条件再导出！', 'error')
        return redirect(url_for('search_logs'))

    where_clause = ' AND '.join(conditions)
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


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='127.0.0.1', port=5000)
