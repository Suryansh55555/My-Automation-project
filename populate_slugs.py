import sqlite3
import re

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    return text.strip('-')

# connect to your DB
conn = sqlite3.connect("site.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# get all products
cur.execute("SELECT id, name FROM products")
products = cur.fetchall()

for product in products:
    pid = product["id"]
    name = product["name"]
    slug = slugify(name) + f"-{pid}"  # unique slug (name + id)

    cur.execute("UPDATE products SET slug = ? WHERE id = ?", (slug, pid))
    print(f"Updated {name} -> {slug}")

conn.commit()
conn.close()
print("✅ Slugs populated successfully.")
