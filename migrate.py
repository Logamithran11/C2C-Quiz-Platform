import sqlite3

def migrate():
    print("Migrating database...")
    db = sqlite3.connect('instance/c2c.sqlite3')
    db.row_factory = sqlite3.Row
    cursor = db.cursor()
    
    # Add new columns to quizzes
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN pass_fail_enabled INTEGER NOT NULL DEFAULT 0")
        print("Added pass_fail_enabled")
    except sqlite3.OperationalError as e:
        print(f"pass_fail_enabled: {e}")
        
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN pass_percentage INTEGER NOT NULL DEFAULT 50 CHECK(pass_percentage BETWEEN 1 AND 100)")
        print("Added pass_percentage")
    except sqlite3.OperationalError as e:
        print(f"pass_percentage: {e}")
        
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN randomize_questions INTEGER NOT NULL DEFAULT 0")
        print("Added randomize_questions")
    except sqlite3.OperationalError as e:
        print(f"randomize_questions: {e}")
        
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN randomize_options INTEGER NOT NULL DEFAULT 0")
        print("Added randomize_options")
    except sqlite3.OperationalError as e:
        print(f"randomize_options: {e}")
        
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN scheduled_start INTEGER")
        print("Added scheduled_start")
    except sqlite3.OperationalError as e:
        print(f"scheduled_start: {e}")
        
    try:
        cursor.execute("ALTER TABLE quizzes ADD COLUMN scheduled_end INTEGER")
        print("Added scheduled_end")
    except sqlite3.OperationalError as e:
        print(f"scheduled_end: {e}")
        
    # Add new columns to attempts
    try:
        cursor.execute("ALTER TABLE attempts ADD COLUMN question_order TEXT")
        print("Added question_order")
    except sqlite3.OperationalError as e:
        print(f"question_order: {e}")

    db.commit()
    db.close()
    print("Migration complete.")

if __name__ == '__main__':
    migrate()
