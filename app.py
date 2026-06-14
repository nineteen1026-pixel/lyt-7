import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

app = Flask(__name__)
app.secret_key = 'fishing_log_secret_key'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fishing_log.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.commit()
    conn.close()


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
    if request.method == 'POST':
        spot = request.form['spot'].strip()
        weather = request.form['weather'].strip()
        water_level = request.form['water_level'].strip()
        bait = request.form['bait'].strip()
        fish_species = request.form['fish_species'].strip()
        harvest = request.form['harvest'].strip()
        next_strategy = request.form['next_strategy'].strip()
        created_at = request.form['created_at'] or datetime.now().strftime('%Y-%m-%d')

        if not spot or not weather or not water_level or not bait or not fish_species or not harvest:
            flash('请填写所有必填项！', 'error')
        else:
            conn = get_db()
            conn.execute('''
                INSERT INTO fishing_logs (spot, weather, water_level, bait, fish_species, harvest, next_strategy, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (spot, weather, water_level, bait, fish_species, harvest, next_strategy, created_at))
            conn.commit()
            conn.close()
            flash('记录添加成功！', 'success')
            return redirect(url_for('index'))

    return render_template('add.html')


@app.route('/log/<int:log_id>')
def log_detail(log_id):
    page = request.args.get('page', 1, type=int)
    sort_by = request.args.get('sort', 'date_desc')
    per_page = request.args.get('per_page', 20, type=int)

    conn = get_db()
    log = conn.execute('SELECT * FROM fishing_logs WHERE id = ?', (log_id,)).fetchone()
    conn.close()
    if log is None:
        flash('记录不存在！', 'error')
        return redirect(url_for('index'))
    return render_template('detail.html', log=log, page=page, sort_by=sort_by, per_page=per_page)


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

    ratings = conn.execute(
        'SELECT * FROM spot_ratings WHERE spot_id = ? ORDER BY created_at DESC',
        (spot_id,)
    ).fetchall()

    related_logs = conn.execute(
        'SELECT * FROM fishing_logs WHERE spot = ? ORDER BY created_at DESC, id DESC',
        (spot['name'],)
    ).fetchall()

    conn.close()
    return render_template('spot_detail.html', spot=spot_dict, ratings=ratings, related_logs=related_logs)


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
    harvest_str = harvest_str.strip()
    if harvest_str.startswith('h') or harvest_str.startswith('H'):
        try:
            return int(harvest_str[1:])
        except ValueError:
            return 1
    try:
        return int(harvest_str)
    except ValueError:
        try:
            return float(harvest_str)
        except ValueError:
            return 1


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
            ORDER BY log_count DESC
        ''', (selected_month,)).fetchall()

        total_spots = len(spot_rows)

        all_logs = conn.execute('''
            SELECT harvest FROM fishing_logs
            WHERE strftime('%Y-%m', created_at) = ?
        ''', (selected_month,)).fetchall()

        total_harvest = sum(parse_harvest_value(log['harvest']) for log in all_logs)

        spot_stats = []
        for idx, row in enumerate(spot_rows, 1):
            spot_logs = conn.execute('''
                SELECT harvest FROM fishing_logs
                WHERE spot = ? AND strftime('%Y-%m', created_at) = ?
            ''', (row['spot'], selected_month)).fetchall()
            spot_harvest = sum(parse_harvest_value(log['harvest']) for log in spot_logs)
            spot_stats.append({
                'rank': idx,
                'spot': row['spot'],
                'log_count': row['log_count'],
                'harvest_count': spot_harvest
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


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='127.0.0.1', port=5000)
