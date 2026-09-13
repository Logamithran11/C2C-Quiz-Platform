import sqlite3
import psycopg2
import os
import sys

def migrate():
    sqlite_path = 'instance/c2c.sqlite3'
    if not os.path.exists(sqlite_path):
        print("SQLite database not found.")
        return

    pg_url = os.environ.get('DATABASE_URL')
    if not pg_url:
        print("DATABASE_URL environment variable is required.")
        return

    print("Connecting to databases...")
    lite_conn = sqlite3.connect(sqlite_path)
    lite_conn.row_factory = sqlite3.Row
    lite_cur = lite_conn.cursor()

    pg_conn = psycopg2.connect(pg_url)
    pg_cur = pg_conn.cursor()

    try:
        print("Initializing PostgreSQL schema...")
        with open('schema.sql', 'r') as f:
            pg_cur.execute(f.read())
        
        tables = [
            'creators',
            'question_banks',
            'bank_questions',
            'quizzes',
            'questions',
            'attempts',
            'answers'
        ]

        summary = {}
        
        boolean_cols = {'pass_fail_enabled', 'randomize_questions', 'randomize_options'}

        for table in tables:
            print(f"Migrating table: {table}...")
            lite_cur.execute(f"SELECT * FROM {table}")
            rows = lite_cur.fetchall()
            
            if not rows:
                summary[table] = 0
                continue
                
            cols = list(rows[0].keys())
            col_names = ', '.join(cols)
            placeholders = ', '.join(['%s'] * len(cols))
            insert_query = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
            
            count = 0
            for row in rows:
                values = []
                for col in cols:
                    val = row[col]
                    if col in boolean_cols and val is not None:
                        val = bool(val)
                    values.append(val)
                try:
                    pg_cur.execute(insert_query, tuple(values))
                    count += 1
                except psycopg2.IntegrityError as e:
                    print(f"Integrity Error: {e}")
                    raise
            
            summary[table] = count
            
            if 'id' in cols:
                pg_cur.execute(f"SELECT setval('{table}_id_seq', (SELECT COALESCE(MAX(id), 1) FROM {table}));")

        pg_conn.commit()
        print("\nMigration successful!")
        for t, c in summary.items():
            print(f"{t.capitalize()}: {c}")

    except Exception as e:
        pg_conn.rollback()
        print(f"\nMigration failed: {e}")
        print("PostgreSQL changes rolled back. SQLite database remains untouched.")
        sys.exit(1)
    finally:
        lite_conn.close()
        pg_conn.close()

if __name__ == '__main__':
    migrate()
