import csv
import sqlite3

# connect to your DB
conn = sqlite3.connect("site.db")
cur = conn.cursor()

# open the CSV file (exported from Google Sheets)
with open("New Arrival.csv", newline='', encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        # clean price -> remove ₹ and commas
        price_str = row["Price"].replace("₹", "").replace(",", "").strip()
        try:
            price = float(price_str)
        except:
            price = 0.0

        # prepare values
        name = f"{row['Product Type']} | {row['Product Size']}"
        description = row.get("Description", "")
        image_url = row.get("Image Link", "")

        # insert into DB
        cur.execute(
            "INSERT INTO products (name, price, description, image_url) VALUES (?,?,?,?)",
            (name, price, description, image_url)
        )

conn.commit()
conn.close()
print("✅ Products imported successfully!")
