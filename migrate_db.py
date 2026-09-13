import sqlite3
import os

DB_PATH = 'c:\\vs project\\C2C\\instance\\c2c.sqlite3'
if not os.path.exists(DB_PATH):
    print(f"Database not found at {DB_PATH}")
    exit(1)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Create Question Banks table
c.execute('''
CREATE TABLE IF NOT EXISTS question_banks (
    id INTEGER PRIMARY KEY,
    creator_id INTEGER NOT NULL REFERENCES creators(id),
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 150),
    created_at INTEGER NOT NULL
)
''')

# Create Bank Questions table
c.execute('''
CREATE TABLE IF NOT EXISTS bank_questions (
    id INTEGER PRIMARY KEY,
    bank_id INTEGER NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    text TEXT NOT NULL CHECK(length(text) > 0),
    option_0 TEXT NOT NULL CHECK(length(option_0) > 0),
    option_1 TEXT NOT NULL CHECK(length(option_1) > 0),
    option_2 TEXT NOT NULL CHECK(length(option_2) > 0),
    option_3 TEXT NOT NULL CHECK(length(option_3) > 0),
    correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3),
    UNIQUE(bank_id, position)
)
''')

# Alter quizzes table
def add_column(table, col_def):
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        print(f"Added {col_def} to {table}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            print(f"Column {col_def.split()[0]} already exists in {table}")
        else:
            raise e

add_column("quizzes", "instructions TEXT DEFAULT ''")
add_column("quizzes", "bank_id INTEGER REFERENCES question_banks(id)")
add_column("quizzes", "bank_question_count INTEGER")

conn.commit()
conn.close()
print("Migration successful.")
