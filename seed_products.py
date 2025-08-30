import sqlite3

products = [
  {
    "name": "AOP Sports Bra",
    "price": 460,
    "description": "Supportive and comfortable activewear with sublimation print.",
    "image_url": "https://qikink.com/wp-content/uploads/2025/08/sports-bra-dropshipping-qikink-600x600.webp"
  },
  {
    "name": "Baby Tee",
    "price": 160,
    "description": "Popular cropped baby tee in trendy styles and fabrics.",
    "image_url": "https://qikink.com/wp-content/uploads/2025/07/Baby-tee-print-on-demand-qikink-600x600.webp"
  },
  {
    "name": "Acrylic Display Stand",
    "price": 140,
    "description": "Practical acrylic stand for displays and promotional material.",
    "image_url": "https://qikink.com/wp-content/uploads/2025/07/Acrylic-stand-qikink-600x600.webp"
  },
  # …add the rest of your items here the same way …
]

conn = sqlite3.connect("site.db")
for p in products:
    conn.execute(
        "INSERT INTO products (name, price, description, image_url) VALUES (?,?,?,?)",
        (p["name"], float(p["price"]), p.get("description",""), p.get("image_url",""))
    )
conn.commit()
conn.close()
print(f"Inserted {len(products)} products.")
