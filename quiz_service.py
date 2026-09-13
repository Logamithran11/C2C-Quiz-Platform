"""Validation, quiz persistence, and authoritative timing/scoring."""
import secrets
import string
import time
import unicodedata

from db import get_db


def now():
    return int(time.time())


def clean_text(value, label, maximum, required=True):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text.')
    # SQLite length() stops at NUL; reject it before reaching database constraints.
    if '\x00' in value:
        raise ValueError(f'{label} must not contain null characters.')
    value = value.strip()
    if (required and not value) or len(value) > maximum:
        raise ValueError(f'{label} is required and must be at most {maximum} characters.' if required
                         else f'{label} must be at most {maximum} characters.')
    return value


def normalize_roll(value):
    return ''.join(unicodedata.normalize('NFKC', value).split()).casefold()


def validate_quiz(data):
    if not isinstance(data, dict):
        raise ValueError('Invalid quiz data.')
    title = clean_text(data.get('title', ''), 'Title', 150)
    description = clean_text(data.get('description', ''), 'Description', 3000, False)
    instructions = clean_text(data.get('instructions', ''), 'Instructions', 3000, False)
    try:
        minutes = int(str(data.get('time_limit', '')))
    except ValueError:
        raise ValueError('Time limit must be a whole number of minutes.') from None
    if not 1 <= minutes <= 1440:
        raise ValueError('Time limit must be between 1 and 1440 minutes.')
        
    try:
        bank_id = int(data.get('bank_id')) if data.get('bank_id') else None
    except ValueError:
        bank_id = None
        
    try:
        bank_question_count = int(data.get('bank_question_count')) if data.get('bank_question_count') else None
    except ValueError:
        bank_question_count = None
        
    questions = data.get('questions', [])
    if not bank_id and (not isinstance(questions, list) or not questions):
        raise ValueError('Add at least one question.')
        
    try:
        pass_percentage = int(data.get('pass_percentage', 50))
    except (ValueError, TypeError):
        pass_percentage = 50
    if not 1 <= pass_percentage <= 100:
        raise ValueError('Pass percentage must be between 1 and 100.')
    
    pass_fail_enabled = 1 if data.get('pass_fail_enabled') else 0
    randomize_questions = 1 if data.get('randomize_questions') else 0
    randomize_options = 1 if data.get('randomize_options') else 0

    scheduled_start = None
    if data.get('scheduled_start'):
        try:
            scheduled_start = int(time.mktime(time.strptime(data['scheduled_start'], "%Y-%m-%dT%H:%M")))
        except ValueError:
            raise ValueError('Invalid start date format.')

    scheduled_end = None
    if data.get('scheduled_end'):
        try:
            scheduled_end = int(time.mktime(time.strptime(data['scheduled_end'], "%Y-%m-%dT%H:%M")))
        except ValueError:
            raise ValueError('Invalid end date format.')
            
    if scheduled_start and scheduled_end and scheduled_start >= scheduled_end:
        raise ValueError('End time must be after start time.')

    validated = []
    if not bank_id:
        for index, question in enumerate(questions, 1):
            if not isinstance(question, dict):
                raise ValueError('Invalid question data.')
            text = clean_text(question.get('text', ''), f'Question {index}', 2000)
            options = question.get('options')
            if not isinstance(options, list) or len(options) != 4:
                raise ValueError(f'Question {index} must have exactly four options.')
            options = [clean_text(option, 'Option', 500) for option in options]
            if len({option.casefold() for option in options}) != 4:
                raise ValueError(f'Question {index} options must be distinct.')
            correct = question.get('correct_option')
            if type(correct) is not int or correct not in range(4):
                raise ValueError(f'Select one correct answer for question {index}.')
            validated.append({'text': text, 'options': options, 'correct_option': correct})
            
    return title, description, instructions, minutes, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, scheduled_start, scheduled_end, bank_id, bank_question_count, validated


def save_quiz(creator_id, data, quiz_id=None):
    title, description, instructions, minutes, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, scheduled_start, scheduled_end, bank_id, bank_question_count, questions = validate_quiz(data)
    db = get_db()
    
    with db:
        if bank_id:
            bank = db.execute('SELECT 1 FROM question_banks WHERE id = %s AND creator_id = %s', (bank_id, creator_id)).fetchone()
            if not bank:
                raise ValueError('Invalid question bank selected.')
            bank_q_count = db.execute('SELECT count(*) as c FROM bank_questions WHERE bank_id = %s', (bank_id,)).fetchone()['c']
            if bank_question_count is not None and bank_question_count < 1:
                raise ValueError('You must select at least 1 question from the question bank.')
            if bank_question_count and bank_question_count > bank_q_count:
                raise ValueError(f'You can select up to {bank_q_count} questions from this question bank.')
                
        if quiz_id is None:
            alphabet = string.ascii_uppercase + string.digits
            code = ''.join(secrets.choice(alphabet) for _ in range(10))
            while db.execute('SELECT 1 FROM quizzes WHERE code = %s', (code,)).fetchone():
                code = ''.join(secrets.choice(alphabet) for _ in range(10))
            cursor = db.execute(
                'INSERT INTO quizzes (creator_id, code, title, description, instructions, time_limit, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, scheduled_start, scheduled_end, bank_id, bank_question_count, created_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)', 
                (creator_id, code, title, description, instructions, minutes, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, scheduled_start, scheduled_end, bank_id, bank_question_count, now()))
            quiz_id = cursor.fetchone()['id']
        else:
            if not db.execute('SELECT 1 FROM quizzes WHERE id = %s AND creator_id = %s',
                              (quiz_id, creator_id)).fetchone():
                raise ValueError('Quiz not found.')
            if db.execute('SELECT 1 FROM attempts WHERE quiz_id = %s', (quiz_id,)).fetchone():
                raise ValueError('This quiz cannot be edited after a student starts an attempt.')
            db.execute('UPDATE quizzes SET title = %s, description = %s, instructions = %s, time_limit = %s, pass_fail_enabled = %s, pass_percentage = %s, randomize_questions = %s, randomize_options = %s, scheduled_start = %s, scheduled_end = %s, bank_id = %s, bank_question_count = %s WHERE id = %s',
                       (title, description, instructions, minutes, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, scheduled_start, scheduled_end, bank_id, bank_question_count, quiz_id))
            db.execute('DELETE FROM questions WHERE quiz_id = %s', (quiz_id,))
            
        if bank_id:
            bank_questions = db.execute('SELECT * FROM bank_questions WHERE bank_id = %s ORDER BY position', (bank_id,)).fetchall()
            for position, question in enumerate(bank_questions):
                db.execute('INSERT INTO questions '
                           '(quiz_id, position, text, option_0, option_1, option_2, option_3, correct_option) '
                           'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                           (quiz_id, position, question['text'], question['option_0'], question['option_1'], question['option_2'], question['option_3'], question['correct_option']))
        else:
            for position, question in enumerate(questions):
                db.execute('INSERT INTO questions '
                           '(quiz_id, position, text, option_0, option_1, option_2, option_3, correct_option) '
                           'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                           (quiz_id, position, question['text'], *question['options'], question['correct_option']))
    return quiz_id


def start_attempt(quiz, name, roll, browser_token):
    name = clean_text(name, 'Student name', 100)
    roll = clean_text(roll, 'Roll number', 50)
    normalized = normalize_roll(roll)
    if not normalized:
        raise ValueError('Enter a valid roll number.')
    db = get_db()
    
    with db:
        existing = db.execute('SELECT * FROM attempts WHERE quiz_id = %s AND normalized_roll = %s',
                              (quiz['id'], normalized)).fetchone()
        if existing:
            if existing['browser_token'] == browser_token:
                return existing['id']
            raise ValueError('This roll number already has an attempt for this quiz.')
        quiz_record = db.execute('SELECT time_limit, scheduled_start, scheduled_end, randomize_questions, randomize_options, bank_question_count FROM quizzes WHERE id = %s', (quiz['id'],)).fetchone()
        minutes = quiz_record['time_limit']
        started = now()
        
        if quiz_record['scheduled_start'] and started < quiz_record['scheduled_start']:
            raise ValueError('This quiz has not started yet.')
        if quiz_record['scheduled_end'] and started > quiz_record['scheduled_end']:
            raise ValueError('This quiz has already ended.')
            
        question_order_json = None
        if quiz_record['randomize_questions'] or quiz_record['randomize_options'] or quiz_record['bank_question_count']:
            import random
            import json
            questions = db.execute('SELECT id FROM questions WHERE quiz_id = %s ORDER BY position', (quiz['id'],)).fetchall()
            q_ids = [q['id'] for q in questions]
            
            if quiz_record['randomize_questions']:
                random.shuffle(q_ids)
                
            if quiz_record['bank_question_count']:
                q_ids = q_ids[:quiz_record['bank_question_count']]
                
            options_map = {}
            if quiz_record['randomize_options']:
                for q_id in q_ids:
                    opt_order = [0, 1, 2, 3]
                    random.shuffle(opt_order)
                    options_map[str(q_id)] = opt_order
            else:
                for q_id in q_ids:
                    options_map[str(q_id)] = [0, 1, 2, 3]
            
            question_order_json = json.dumps({'questions': q_ids, 'options': options_map})

        cursor = db.execute('INSERT INTO attempts '
                            '(quiz_id, browser_token, name, roll_number, normalized_roll, started_at, deadline, question_order) '
                            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
                            (quiz['id'], browser_token, name, roll, normalized, started, started + minutes * 60, question_order_json))
        return cursor.fetchone()['id']


def grade(db, attempt, timestamp):
    """Called only inside a write transaction; scores use stored answers, never client totals."""
    rows = db.execute('SELECT q.correct_option, a.selected_option FROM questions q '
                      'LEFT JOIN answers a ON a.question_id = q.id AND a.attempt_id = %s '
                      'WHERE q.quiz_id = %s', (attempt['id'], attempt['quiz_id'])).fetchall()
    total = len(rows)
    correct = sum(row['selected_option'] == row['correct_option'] for row in rows)
    unanswered = sum(row['selected_option'] is None for row in rows)
    elapsed = max(0, min(timestamp, attempt['deadline']) - attempt['started_at'])
    db.execute('UPDATE attempts SET submitted_at = %s, total = %s, correct = %s, wrong = %s, '
               'unanswered = %s, score = %s, percentage = %s, time_taken = %s '
               'WHERE id = %s AND submitted_at IS NULL',
               (min(timestamp, attempt['deadline']), total, correct, total - correct - unanswered,
                unanswered, correct, round(correct * 100 / total, 2), elapsed, attempt['id']))


def process_attempt(attempt_id, answers=None, submit=False):
    db = get_db()
    
    with db:
        attempt = db.execute('SELECT * FROM attempts WHERE id = %s', (attempt_id,)).fetchone()
        timestamp = now()
        if attempt['submitted_at'] is None:
            if timestamp >= attempt['deadline']:
                # Late requests cannot change saved answers, even with a forged browser timer.
                grade(db, attempt, timestamp)
            else:
                if answers is not None:
                    if not isinstance(answers, dict):
                        raise ValueError('Answers must be an object.')
                    ids = {str(row['id']) for row in db.execute(
                        'SELECT id FROM questions WHERE quiz_id = %s', (attempt['quiz_id'],))}
                    for question_id, option in answers.items():
                        if question_id not in ids or (option is not None and
                                (type(option) is not int or option not in range(4))):
                            raise ValueError('Invalid question or answer option.')
                    for question_id, option in answers.items():
                        if option is None:
                            db.execute('DELETE FROM answers WHERE attempt_id = %s AND question_id = %s',
                                       (attempt_id, question_id))
                        else:
                            db.execute('INSERT INTO answers (attempt_id, quiz_id, question_id, selected_option) '
                                       'VALUES (%s, %s, %s, %s) ON CONFLICT(attempt_id, question_id) '
                                       'DO UPDATE SET selected_option = excluded.selected_option',
                                       (attempt_id, attempt['quiz_id'], question_id, option))
                if submit:
                    grade(db, attempt, timestamp)
    return db.execute('SELECT * FROM attempts WHERE id = %s', (attempt_id,)).fetchone()


def expire_attempts(quiz_id):
    db = get_db()
    
    with db:
        timestamp = now()
        for attempt in db.execute('SELECT * FROM attempts WHERE quiz_id = %s '
                                  'AND submitted_at IS NULL AND deadline <= %s', (quiz_id, timestamp)).fetchall():
            grade(db, attempt, timestamp)


def ranked_results(quiz_id):
    expire_attempts(quiz_id)
    return get_db().execute('SELECT * FROM attempts WHERE quiz_id = %s AND submitted_at IS NOT NULL '
                            'ORDER BY score DESC, time_taken ASC, submitted_at ASC, id ASC', (quiz_id,)).fetchall()
