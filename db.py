"""PostgreSQL helpers via psycopg2. Each request gets its own connection from the pool."""
import os
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from flask import current_app, g
from pathlib import Path
import click

db_pool = None

def get_pool():
    global db_pool
    if db_pool is None:
        db_url = current_app.config.get('DATABASE_URL')
        if not db_url:
            raise RuntimeError("DATABASE_URL is not set.")
        db_pool = pool.ThreadedConnectionPool(1, 20, db_url)
    return db_pool

class DBWrapper:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            return None
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

def get_db():
    if 'db' not in g:
        conn = get_pool().getconn()
        g.db = DBWrapper(conn)
    return g.db

def close_db(error=None):
    db_wrapper = g.pop('db', None)
    if db_wrapper is not None:
        # Rollback any uncommitted changes to release locks before returning to pool
        try:
            db_wrapper.conn.rollback()
        except:
            pass
        get_pool().putconn(db_wrapper.conn)

def init_db():
    schema_path = Path(__file__).with_name('schema.sql')
    with get_pool().getconn() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_path.read_text())
        conn.commit()

def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command('init-db')
    def init_db_command():
        init_db()
        click.echo('C2C database initialized for PostgreSQL.')
