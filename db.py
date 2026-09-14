"""MongoDB helpers via pymongo. Connection pooling is managed automatically by MongoClient."""
import os
from pymongo import MongoClient
from flask import current_app, g
import click

mongo_client = None

def get_client():
    global mongo_client
    if mongo_client is None:
        mongo_uri = current_app.config.get('DATABASE_URL')
        # If DATABASE_URL is somehow missing or is not mongo, fallback to MONGODB_URI
        # Render deployment requirement: read MONGODB_URI
        if not mongo_uri or not mongo_uri.startswith('mongodb'):
            mongo_uri = os.environ.get('MONGODB_URI')
            
        if not mongo_uri:
            raise RuntimeError("MONGODB_URI is not set.")
        import certifi
        mongo_client = MongoClient(mongo_uri, tlsCAFile=certifi.where())
    return mongo_client

def get_db():
    if 'db' not in g:
        client = get_client()
        # Get default database from connection string
        g.db = client.get_default_database(default='c2cdb')
    return g.db

def get_next_sequence_value(sequence_name):
    """Simulates auto-increment behavior for integer IDs"""
    db = get_db()
    sequence_document = db.counters.find_one_and_update(
        {'_id': sequence_name},
        {'$inc': {'seq': 1}},
        upsert=True,
        return_document=True
    )
    return sequence_document['seq']

def close_db(error=None):
    # PyMongo handles connection pooling automatically, no need to manually close per-request
    g.pop('db', None)

def to_dict(mongo_doc):
    if not mongo_doc:
        return None
    mongo_doc['id'] = mongo_doc.get('_id')
    return mongo_doc

def to_dict_list(cursor):
    return [to_dict(doc) for doc in cursor]


def init_db():
    db = get_client().get_default_database()
    # Create required indexes
    db.creators.create_index('email', unique=True)
    db.quizzes.create_index('code', unique=True)
    db.quizzes.create_index('creator_id')
    db.quizzes.create_index('created_at')
    db.questions.create_index('quiz_id')
    db.question_banks.create_index('creator_id')
    db.bank_questions.create_index('bank_id')
    db.attempts.create_index('quiz_id')
    db.attempts.create_index('student_token')
    db.answers.create_index('attempt_id')
    # Additional indexes for uniqueness and fast lookups
    db.attempts.create_index([('quiz_id', 1), ('normalized_roll', 1)], unique=True)
    
def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command('init-db')
    def init_db_command():
        init_db()
        click.echo('C2C database initialized for MongoDB.')
