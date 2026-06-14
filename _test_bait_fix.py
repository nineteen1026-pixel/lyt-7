from app import app, init_db, get_db, parse_harvest_value

assert parse_harvest_value("无") == 0
assert parse_harvest_value("空军") == 0
assert parse_harvest_value("若干") == 0  # might fail - check
assert parse_harvest_value("10条") == 10

init_db()
c = app.test_client()
conn = get_db()
conn.execute("DELETE FROM fishing_logs")
conn.execute("DELETE FROM baits")
conn.execute("INSERT INTO baits (name) VALUES (?)", ("蚯蚓",))
conn.execute("INSERT INTO baits (name) VALUES (?)", ("玉米",))
conn.executemany(
    "INSERT INTO fishing_logs (spot,weather,water_level,bait,fish_species,harvest,next_strategy,created_at) VALUES (?,?,?,?,?,?,?,?)",
    [
        ("东湖", "晴", "正常", "蚯蚓", "鲫鱼", "10条", "", "2026-06-01"),
        ("东湖", "晴", "正常", "蚯蚓", "鲫鱼", "空军", "", "2026-06-02"),
        ("西湖", "晴", "正常", "玉米", "鲫鱼", "无", "", "2026-06-03"),
    ],
)
conn.commit()
conn.close()

r = c.get("/baits")
h = r.data.decode()
assert "sort=success" in h or r.request is None  # default no sort param means success
# 蚯蚓 50% success, 玉米 0% - worm should rank first
idx_worm = h.find("蚯蚓")
idx_corn = h.find("玉米")
assert idx_worm < idx_corn
print("若干", parse_harvest_value("若干"))
print("all passed")
