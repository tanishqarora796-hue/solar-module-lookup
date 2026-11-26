
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from pathlib import Path
import csv
import sqlite3

import pandas as pd
from flask import Flask, request, jsonify, render_template


# --------------------------------------------------
# 1. APPLICATION CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_NAME = BASE_DIR / "records.db"

KEY_COL = "ModuleID"

# All columns stored in the existing production database
DB_VALUE_COLS = [
    "ModuleCount", "Date", "Time", "Pmax", "Voc", "Isc", "Vm", "Im", "FF",
    "PowerRank", "SortingRank", "CellEfficiency", "ModuleEfficiency",
    "Rserie", "Rshunt", "Irradiance", "Tdut", "Tcorr", "Jsc",
    "ProductName", "Company", "CellType", "Machinename"
]

# Columns returned to the interface
DISPLAY_COLS = [
    "ModuleCount",
    "ModuleID",
    "Pmax",
    "Voc",
    "Isc",
    "Vm",
    "Im",
    "FF",
    "ModuleEfficiency",
    "PowerRank",
    "RFID_Pmax",
    "RFID_STATUS"
]


# --------------------------------------------------
# 2. DATABASE FUNCTIONS
# --------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:

        cols_sql = ", ".join([
            f"{col} INTEGER" if col == "ModuleCount" else f"{col} TEXT"
            for col in DB_VALUE_COLS
        ])

        # Create production table only if it does not already exist.
        # Existing production records are preserved.
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS records (
                {KEY_COL} TEXT PRIMARY KEY,
                {cols_sql}
            )
        """)

        # Separate RF records table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rf_records (
                ModuleID TEXT PRIMARY KEY,
                RF_Pmax TEXT
            )
        """)


# Initialize database for both local Flask and Gunicorn
init_db()


# --------------------------------------------------
# 3. RF FILE READER
# --------------------------------------------------

def read_rf_file(file):

    raw = file.read()
    filename = file.filename.lower()

    if filename.endswith(".csv"):

        last_error = None

        for encoding in ["utf-8-sig", "cp1252", "latin1"]:
            try:
                return pd.read_csv(
                    BytesIO(raw),
                    encoding=encoding,
                    sep=None,
                    engine="python"
                )
            except Exception as e:
                last_error = e

        raise ValueError(f"Unable to read RF CSV: {last_error}")

    elif filename.endswith(".xlsx"):

        return pd.read_excel(
            BytesIO(raw),
            engine="openpyxl"
        )

    elif filename.endswith(".xls"):

        try:
            # Supports the factory's HTML-table Excel export
            return pd.read_html(BytesIO(raw))[0]

        except Exception:
            # Fallback for genuine binary XLS workbooks
            return pd.read_excel(
                BytesIO(raw),
                engine="xlrd"
            )

    raise ValueError("Upload CSV, XLS or XLSX file.")


# --------------------------------------------------
# 4. HOMEPAGE
# --------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        display_cols=DISPLAY_COLS
    )


# --------------------------------------------------
# 5. PRODUCTION CSV UPLOAD
# --------------------------------------------------

@app.route("/upload", methods=["POST"])
def upload_csv():

    file = request.files.get("file")

    if not file or not file.filename:
        return jsonify({"error": "No file uploaded."}), 400

    try:

        raw_bytes = file.stream.read()

        # Read different Excel CSV encodings
        text = None

        for encoding in ["utf-8-sig", "cp1252", "latin1"]:
            try:
                text = raw_bytes.decode(encoding)
                break
            except UnicodeDecodeError:
                continue

        if text is None:
            return jsonify({
                "error": "Unable to decode the production CSV."
            }), 400

        # Detect comma, semicolon or tab delimiter
        try:
            dialect = csv.Sniffer().sniff(
                text[:8192],
                delimiters=",;\t"
            )
        except csv.Error:
            dialect = csv.excel

        stream = StringIO(text, newline="")
        reader = csv.DictReader(stream, dialect=dialect)

        if not reader.fieldnames:
            return jsonify({"error": "CSV file is empty."}), 400

        reader.fieldnames = [
            str(name).strip() if name else ""
            for name in reader.fieldnames
        ]

        if KEY_COL not in reader.fieldnames:
            return jsonify({
                "error": "Production CSV must contain ModuleID."
            }), 400

        all_cols = [KEY_COL] + DB_VALUE_COLS
        placeholders = ", ".join(["?"] * len(all_cols))
        cols_joined = ", ".join(all_cols)

        count = 0

        with get_db() as conn:

            existing_rows = conn.execute(
                f"SELECT {KEY_COL}, ModuleCount FROM records"
            ).fetchall()

            existing_counts = {
                row[KEY_COL]: row["ModuleCount"]
                for row in existing_rows
            }

            next_module_num = max(
                (int(v) for v in existing_counts.values() if v is not None),
                default=0
            ) + 1

            for row in reader:

                key_val = (row.get(KEY_COL) or "").strip()

                if not key_val:
                    continue

                if key_val in existing_counts:
                    module_count = existing_counts[key_val]
                else:
                    module_count = next_module_num
                    existing_counts[key_val] = module_count
                    next_module_num += 1

                values = [key_val] + [
                    module_count if col == "ModuleCount"
                    else (row.get(col) or "").strip()
                    for col in DB_VALUE_COLS
                ]

                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO records
                    ({cols_joined})
                    VALUES ({placeholders})
                    """,
                    values
                )

                count += 1

        return jsonify({
            "message": f"Successfully stored/updated {count} production records."
        })

    except Exception as e:

        app.logger.exception("Production CSV upload failed")

        return jsonify({
            "error": f"Production upload failed: {str(e)}"
        }), 400


# --------------------------------------------------
# 6. RF RECORDS UPLOAD
# --------------------------------------------------

@app.route("/upload_rf", methods=["POST"])
def upload_rf():

    file = request.files.get("file")

    if not file or not file.filename:
        return jsonify({
            "error": "Please select an RF file."
        }), 400

    try:

        df = read_rf_file(file)

        # Normalize column names
        column_map = {
            str(col).strip().casefold(): col
            for col in df.columns
        }

        if "serial" not in column_map or "pmax" not in column_map:
            return jsonify({
                "error": "RF file must contain Serial and PMAX columns."
            }), 400

        df = df[[
            column_map["serial"],
            column_map["pmax"]
        ]].copy()

        df.columns = ["Serial", "PMAX"]

        df["Serial"] = df["Serial"].astype("string").str.strip()

        df = df.dropna(subset=["Serial"])
        df = df[df["Serial"] != ""]

        # Detect duplicate RF IDs before replacing existing data
        duplicates = df[
            df["Serial"].duplicated(keep=False)
        ]["Serial"].unique().tolist()

        if duplicates:
            return jsonify({
                "error": "Duplicate Serial IDs found in RF file.",
                "duplicates": duplicates[:20]
            }), 400

        rows = []

        for _, row in df.iterrows():

            serial = str(row["Serial"]).strip()

            if pd.isna(row["PMAX"]):
                pmax = None
            else:
                pmax = str(row["PMAX"]).strip()

            rows.append((serial, pmax))

        if not rows:
            return jsonify({
                "error": "No valid RF records found."
            }), 400

        # Replace RF data only after validation succeeds
        with get_db() as conn:

            conn.execute("DELETE FROM rf_records")

            conn.executemany("""
                INSERT INTO rf_records (ModuleID, RF_Pmax)
                VALUES (?, ?)
            """, rows)

        return jsonify({
            "message": f"Successfully imported {len(rows)} RF records."
        })

    except Exception as e:

        app.logger.exception("RF upload failed")

        return jsonify({
            "error": f"RF upload failed: {str(e)}"
        }), 400


# --------------------------------------------------
# 7. MODULE LOOKUP AND Pmax COMPARISON
# --------------------------------------------------

@app.route("/lookup", methods=["POST"])
def lookup_records():

    data = request.get_json(silent=True) or {}
    keys = data.get("keys", [])

    if not isinstance(keys, list):
        return jsonify({
            "error": "Keys must be provided as a list."
        }), 400

    clean_keys = list(dict.fromkeys(
        str(k).strip()
        for k in keys
        if k is not None and str(k).strip()
    ))

    if not clean_keys:
        return jsonify({"results": []})

    with get_db() as conn:

        placeholders = ", ".join(["?"] * len(clean_keys))

        production_rows = conn.execute(
            f"""
            SELECT * FROM records
            WHERE {KEY_COL} IN ({placeholders})
            """,
            clean_keys
        ).fetchall()

        rf_rows = conn.execute(
            f"""
            SELECT ModuleID, RF_Pmax
            FROM rf_records
            WHERE ModuleID IN ({placeholders})
            """,
            clean_keys
        ).fetchall()

    production_map = {
        row["ModuleID"]: dict(row)
        for row in production_rows
    }

    rf_map = {
        row["ModuleID"]: row["RF_Pmax"]
        for row in rf_rows
    }

    results = []

    for module_id in clean_keys:

        production = production_map.get(module_id)
        rf_pmax = rf_map.get(module_id)

        # Sequential MOD COUNT starts from 1
        result = {
            "ModuleCount": len(results) + 1,
            "ModuleID": module_id,
            "Pmax": "NOT FOUND",
            "Voc": "NOT FOUND",
            "Isc": "NOT FOUND",
            "Vm": "NOT FOUND",
            "Im": "NOT FOUND",
            "FF": "NOT FOUND",
            "ModuleEfficiency": "NOT FOUND",
            "PowerRank": "NOT FOUND",
            "RFID_Pmax": "MISSING",
            "RFID_STATUS": "MISSING"
        }

        if production:

            for col in [
                "Pmax",
                "Voc",
                "Isc",
                "Vm",
                "Im",
                "FF",
                "ModuleEfficiency",
                "PowerRank"
            ]:
                result[col] = production.get(col) or ""

            if rf_pmax is not None and str(rf_pmax).strip():

                result["RFID_Pmax"] = rf_pmax

                try:

                    main_value = Decimal(
                        str(result["Pmax"]).strip()
                    )

                    rf_value = Decimal(
                        str(rf_pmax).strip()
                    )

                    if main_value.is_finite() and rf_value.is_finite():

                        result["RFID_STATUS"] = (
                            "OK"
                            if main_value == rf_value
                            else "NOT OK"
                        )

                    else:
                        result["RFID_STATUS"] = "NOT OK"

                except (InvalidOperation, ValueError):
                    result["RFID_STATUS"] = "NOT OK"

        results.append(result)

    return jsonify({
        "results": results
    })
@app.route("/delete_production", methods=["POST"])
def delete_production():
    with get_db() as conn:
        conn.execute("DELETE FROM records")

    return jsonify({
        "success": True,
        "message": "All production records deleted successfully."
    })


@app.route("/delete_rf", methods=["POST"])
def delete_rf():
    with get_db() as conn:
        conn.execute("DELETE FROM rf_records")

    return jsonify({
        "success": True,
        "message": "All RF records deleted successfully."
    })

# --------------------------------------------------
# 8. LOCAL EXECUTION
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=False)
