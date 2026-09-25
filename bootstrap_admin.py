import os
from dotenv import load_dotenv
load_dotenv()

from werkzeug.security import generate_password_hash
from app import create_app
from db import get_db, init_db, get_next_sequence_value
from quiz_service import now

def bootstrap():
    app = create_app()
    with app.app_context():
        db = get_db()
        email = os.environ.get('ADMIN_EMAIL', 'admin@c2c.local')
        password = os.environ.get('ADMIN_PASSWORD', 'SecureAdmin123!')
        
        existing = db.creators.find_one({'email': email})
        if existing:
            if existing.get('role') != 'admin':
                db.creators.update_one({'_id': existing['_id']}, {'$set': {'role': 'admin'}})
                print(f"Updated existing user {email} to admin role.")
            else:
                print(f"Admin {email} already exists.")
        else:
            creator_id = get_next_sequence_value('creators')
            db.creators.insert_one({
                '_id': creator_id,
                'name': 'System Admin',
                'email': email,
                'password_hash': generate_password_hash(password),
                'role': 'admin',
                'created_at': now()
            })
            print(f"Created initial admin account: {email}")

if __name__ == '__main__':
    bootstrap()
