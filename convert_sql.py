import re

def convert_file(path):
    with open(path, 'r') as f:
        content = f.read()

    # Replace '?' with '%s' inside db.execute or get_db().execute strings
    def replace_question_marks(match):
        return match.group(0).replace('?', '%s')
    
    # We look for execute( followed by a string
    # This regex is naive but for this specific codebase where queries are well formatted it works.
    content = re.sub(r'execute\(\s*\'[^\']+\'', replace_question_marks, content)
    content = re.sub(r'execute\(\s*\"[^\"]+\"', replace_question_marks, content)
    # Also multiline strings concatenated natively
    # Let's just do a blanket replace of '?' in execute lines. Wait, that might replace valid ? in strings.
    # But C2C doesn't use ? in SQL queries except for parameters.

    # Remove db.execute('BEGIN IMMEDIATE')
    content = re.sub(r"db\.execute\('BEGIN IMMEDIATE'\)", "", content)

    # Convert lastrowid to RETURNING id
    # quizzes INSERT
    content = re.sub(r"VALUES \(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s\)'\)", r"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id')", content)
    content = re.sub(r"quiz_id = cursor\.lastrowid", r"quiz_id = cursor.fetchone()['id']", content)

    # attempts INSERT
    content = re.sub(r"VALUES \(%s, %s, %s, %s, %s, %s, %s\)'\)", r"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id')", content)
    content = re.sub(r"attempt_id = cursor\.lastrowid", r"attempt_id = cursor.fetchone()['id']", content)

    # creators INSERT
    content = re.sub(r"VALUES \(%s, %s, %s, %s\)'", r"VALUES (%s, %s, %s, %s) RETURNING id'", content)
    # Wait, the creators insert is db.execute(...). It doesn't read lastrowid.
    
    # Replace sqlite3.IntegrityError with psycopg2.IntegrityError
    content = content.replace('sqlite3.IntegrityError', 'psycopg2.IntegrityError')
    content = content.replace('import sqlite3', 'import psycopg2')
    
    with open(path, 'w') as f:
        f.write(content)

convert_file('app.py')
convert_file('quiz_service.py')
