import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash

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
    conn.commit()
    conn.close()


@app.route('/')
def index():
    conn = get_db()
    logs = conn.execute('SELECT * FROM fishing_logs ORDER BY created_at DESC, id DESC LIMIT 20').fetchall()
    conn.close()
    return render_template('index.html', logs=logs)


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
    conn = get_db()
    log = conn.execute('SELECT * FROM fishing_logs WHERE id = ?', (log_id,)).fetchone()
    conn.close()
    if log is None:
        flash('记录不存在！', 'error')
        return redirect(url_for('index'))
    return render_template('detail.html', log=log)


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
    conn = get_db()
    conn.execute('DELETE FROM fishing_logs WHERE id = ?', (log_id,))
    conn.commit()
    conn.close()
    flash('记录已删除！', 'success')
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='127.0.0.1', port=5000)
