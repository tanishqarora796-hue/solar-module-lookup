import csv
import io
import sqlite3
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)
DB_NAME = "records.db"

KEY_COL = "ModuleID"

# All columns stored in SQLite database (including new ModuleCount)
DB_VALUE_COLS = [
    "ModuleCount", "Date", "Time", "Pmax", "Voc", "Isc", "Vm", "Im", "FF",
    "PowerRank", "SortingRank", "CellEfficiency", "ModuleEfficiency",
    "Rserie", "Rshunt", "Irradiance", "Tdut", "Tcorr", "Jsc",
    "ProductName", "Company", "CellType", "Machinename"
]

# Only these columns will show on the interface in this exact order
DISPLAY_COLS = [
    "ModuleCount", "ModuleID", "Date", "Time", "Pmax", "Voc",
    "Isc", "Vm", "Im", "FF", "ModuleEfficiency", "PowerRank"
]

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    with get_db() as conn:
        existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()]
        expected_cols = [KEY_COL] + DB_VALUE_COLS
        
        # Rebuild table automatically if schema changed (e.g., ModuleCount added)
        if existing_cols and existing_cols != expected_cols:
            conn.execute("DROP TABLE records")

        cols_sql = ", ".join([
            f"{col} INTEGER" if col == "ModuleCount" else f"{col} TEXT"
            for col in DB_VALUE_COLS
        ])
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS records (
                {KEY_COL} TEXT PRIMARY KEY,
                {cols_sql}
            )
        """)
@app.route("/")
def index():
    return render_template("index.html", display_cols=DISPLAY_COLS)

@app.route("/upload", methods=["POST"])
def upload_csv():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    raw_bytes = file.stream.read()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("cp1252")

    stream = io.StringIO(text)
    reader = csv.DictReader(stream)

    all_cols = [KEY_COL] + DB_VALUE_COLS
    placeholders = ", ".join(["?"] * len(all_cols))
    cols_joined = ", ".join(all_cols)

    count = 0
    with get_db() as conn:
        # Check existing ModuleIDs so their ModuleCount stays intact if re-uploaded
        existing_rows = conn.execute(f"SELECT {KEY_COL}, ModuleCount FROM records").fetchall()
        existing_counts = {r[KEY_COL]: r["ModuleCount"] for r in existing_rows}
        next_module_num = max(existing_counts.values(), default=0) + 1

        for row in reader:
            key_val = (row.get(KEY_COL) or "").strip()
            if not key_val:
                continue

            if key_val in existing_counts:
                row["ModuleCount"] = existing_counts[key_val]
            else:
                row["ModuleCount"] = next_module_num
                existing_counts[key_val] = next_module_num
                next_module_num += 1

            values = [key_val] + [
                row["ModuleCount"] if c == "ModuleCount" else (row.get(c) or "").strip()
                for c in DB_VALUE_COLS
            ]
            conn.execute(
                f"INSERT OR REPLACE INTO records ({cols_joined}) VALUES ({placeholders})",
                values
            )
            count += 1

    return jsonify({"message": f"Successfully stored/updated {count} records in database."})

@app.route("/lookup", methods=["POST"])
def lookup_records():
    data = request.get_json(silent=True) or {}
    keys = data.get("keys", [])
    clean_keys = [k.strip() for k in keys if k.strip()]

    if not clean_keys:
        return jsonify({"results": []})

    with get_db() as conn:
        placeholders = ", ".join(["?"] * len(clean_keys))
        rows = conn.execute(
            f"SELECT * FROM records WHERE {KEY_COL} IN ({placeholders})",
            clean_keys
        ).fetchall()

    db_map = {row[KEY_COL]: dict(row) for row in rows}
    ordered_results = []

    for k in clean_keys:
        if k in db_map:
            ordered_results.append(db_map[k])
        else:
            not_found = {col: "NOT FOUND" for col in DISPLAY_COLS}
            not_found[KEY_COL] = k
            ordered_results.append(not_found)

    return jsonify({"results": ordered_results})

if __name__ == "__main__":
    init_db()
    app.run(debug=True)