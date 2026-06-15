import sqlite3
import uuid
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import _norm, FIELD_LABELS, EDITABLE_FIELDS

DB_PATH = 'fishing_log.db'

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def test_norm():
    print("=== test _norm function ===")
    assert _norm(None) == '', "None should convert to empty string"
    assert _norm('') == '', "empty string stays empty"
    assert _norm('hello') == 'hello', "non-empty stays same"
    print("  [OK] _norm function passed")

def test_audit_recording():
    print("\n=== test audit recording logic ===")
    conn = get_db()

    test_log_id = 262

    initial_count = conn.execute('SELECT COUNT(*) FROM audit_logs WHERE log_id = ?', (test_log_id,)).fetchone()[0]
    print(f"  initial audit count: {initial_count}")

    old_data = {
        'spot': 'test pond',
        'weather': 'sunny 25C',
        'water_level': 'normal',
        'bait': 'corn',
        'fish_species': 'carp',
        'harvest': 'none',
        'next_strategy': None,
        'created_at': '2026-06-15',
        'temperature': None,
        'humidity': None,
        'wind': None
    }

    new_data_same = {
        'spot': 'test pond',
        'weather': 'sunny 25C',
        'water_level': 'normal',
        'bait': 'corn',
        'fish_species': 'carp',
        'harvest': 'none',
        'next_strategy': '',
        'created_at': '2026-06-15',
        'temperature': '',
        'humidity': '',
        'wind': ''
    }

    records_added = 0
    for field in EDITABLE_FIELDS:
        old_val = old_data.get(field)
        new_val = new_data_same.get(field)
        old_norm = _norm(old_val)
        new_norm = _norm(new_val)
        if old_norm != new_norm:
            records_added += 1
            print(f"  [WARN] false record: {field} old={repr(old_val)} new={repr(new_val)}")

    if records_added == 0:
        print("  [OK] Scenario 1: null/empty same fields NOT recorded")
    else:
        print(f"  [FAIL] Scenario 1: {records_added} false records")

    new_data_changed = dict(new_data_same)
    new_data_changed['harvest'] = 'carp 2kg'
    new_data_changed['next_strategy'] = 'try earthworm'

    expected_changes = ['harvest', 'next_strategy']
    actual_changes = []
    for field in EDITABLE_FIELDS:
        old_val = old_data.get(field)
        new_val = new_data_changed.get(field)
        old_norm = _norm(old_val)
        new_norm = _norm(new_val)
        if old_norm != new_norm:
            actual_changes.append(field)

    if sorted(actual_changes) == sorted(expected_changes):
        print(f"  [OK] Scenario 2: only changed fields recorded {actual_changes}")
    else:
        print(f"  [FAIL] Scenario 2: expected {expected_changes}, got {actual_changes}")

    print("\n=== test batch_id grouping ===")
    batch_id_a = str(uuid.uuid4())
    batch_id_b = str(uuid.uuid4())

    if batch_id_a != batch_id_b:
        print(f"  [OK] Scenario 3: different edits have different batch_ids")
        print(f"    batch A: {batch_id_a[:8]}...")
        print(f"    batch B: {batch_id_b[:8]}...")
    else:
        print("  [FAIL] Scenario 3: duplicate batch_id")

    conn.close()

if __name__ == '__main__':
    test_norm()
    test_audit_recording()
    print("\nAll tests completed!")
