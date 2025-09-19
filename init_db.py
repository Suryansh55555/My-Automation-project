import sqlite3

def init_sheet_config():
    conn = sqlite3.connect("site.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sheet_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id TEXT NOT NULL,
            tab_name TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()
    print("✅ sheet_config table created (if it didn’t already exist).")

if __name__ == "__main__":
    init_sheet_config()
