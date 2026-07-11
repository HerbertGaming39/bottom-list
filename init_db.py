"""
init_db.py – Initialize the database tables before first run.
Run this once before starting the app: python init_db.py
"""
import traceback
from config import create_app, db
from schema import ensure_schema
import models  # Import models so SQLAlchemy registers them

app = create_app()

with app.app_context():
    try:
        print("Creating database tables...")
        db.create_all()
        ensure_schema()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error: {e}")
        traceback.print_exc()
