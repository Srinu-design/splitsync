# app/seed_data.py — insert members in this order; amounts in paise
ROOM_NAME = "Goa Road Trip"
MEMBERS = [("Aarav", "#E4572E"), ("Priya", "#17BEBB"), ("Rohan", "#FFC914"),
           ("Sneha", "#76B041"), ("Karthik", "#7E52A0")]
ALL = ["Aarav", "Priya", "Rohan", "Sneha", "Karthik"]
EXPENSES = [
    # (title, payer, total_paise, split_type, participants, weights_or_exact, days_ago)
    ("Villa booking (2 nights)",   "Aarav",   1_020_000, "EQUAL",  ALL, None, 3),
    ("Fuel & tolls",               "Priya",     960_000, "EQUAL",  ALL, None, 3),
    ("Beach-shack seafood dinner", "Rohan",     610_000, "EQUAL",  ALL, None, 2),
    ("Scooter rentals",            "Sneha",     330_000, "EQUAL",  ALL, None, 2),
    ("Dudhsagar jeep safari",      "Karthik",   580_000, "EQUAL",  ALL, None, 1),
    ("Parasailing",                "Aarav",     450_000, "EQUAL",  ["Aarav", "Sneha", "Karthik"], None, 1),
    ("Cafe brunch",                "Priya",     240_000, "SHARES", ["Priya", "Rohan", "Karthik"], [1, 1, 2], 0),
    ("Club night",                 "Rohan",     500_000, "EXACT",  ALL,
        {"Aarav": 120_000, "Priya": 90_000, "Rohan": 100_000, "Sneha": 80_000, "Karthik": 110_000}, 0),
]
# spent_on = today in Asia/Kolkata minus days_ago
