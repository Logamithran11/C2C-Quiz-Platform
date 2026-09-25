"""Validation, quiz persistence, and authoritative timing/scoring."""
import secrets
import string
import time
import unicodedata
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

from db import get_db, get_next_sequence_value, to_dict, to_dict_list


def now():
    return int(time.time())


def clean_text(value, label, maximum, required=True):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text.')
    # Null byte check
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
    
    pass_fail_enabled = bool(data.get('pass_fail_enabled'))
    randomize_questions = bool(data.get('randomize_questions'))
    randomize_options = bool(data.get('randomize_options'))
    allow_review = bool(data.get('allow_review'))

    scheduled_start = None
    if data.get('scheduled_start'):
        try:
            # Creator enters datetime-local in IST; parse as IST, store as UTC epoch
            dt = datetime.strptime(data['scheduled_start'], "%Y-%m-%dT%H:%M")
            scheduled_start = int(dt.replace(tzinfo=IST).timestamp())
        except ValueError:
            raise ValueError('Invalid start date format.')

    scheduled_end = None
    if data.get('scheduled_end'):
        try:
            dt = datetime.strptime(data['scheduled_end'], "%Y-%m-%dT%H:%M")
            scheduled_end = int(dt.replace(tzinfo=IST).timestamp())
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
            
    return title, description, instructions, minutes, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, allow_review, scheduled_start, scheduled_end, bank_id, bank_question_count, validated


def save_quiz(creator_id, data, quiz_id=None):
    title, description, instructions, minutes, pass_fail_enabled, pass_percentage, randomize_questions, randomize_options, allow_review, scheduled_start, scheduled_end, bank_id, bank_question_count, questions = validate_quiz(data)
    db = get_db()
    
    if bank_id:
        bank = db.question_banks.find_one({'_id': bank_id, 'creator_id': creator_id})
        if not bank:
            raise ValueError('Invalid question bank selected.')
        bank_q_count = db.bank_questions.count_documents({'bank_id': bank_id})
        if bank_question_count is not None and bank_question_count < 1:
            raise ValueError('You must select at least 1 question from the question bank.')
        if bank_question_count and bank_question_count > bank_q_count:
            raise ValueError(f'You can select up to {bank_q_count} questions from this question bank.')
            
    if quiz_id is None:
        alphabet = string.ascii_uppercase + string.digits
        code = ''.join(secrets.choice(alphabet) for _ in range(10))
        while db.quizzes.find_one({'code': code}):
            code = ''.join(secrets.choice(alphabet) for _ in range(10))
            
        quiz_id = get_next_sequence_value('quizzes')
        db.quizzes.insert_one({
            '_id': quiz_id,
            'creator_id': creator_id,
            'code': code,
            'title': title,
            'description': description,
            'instructions': instructions,
            'time_limit': minutes,
            'pass_fail_enabled': pass_fail_enabled,
            'pass_percentage': pass_percentage,
            'randomize_questions': randomize_questions,
            'randomize_options': randomize_options,
            'allow_review': allow_review,
            'scheduled_start': scheduled_start,
            'scheduled_end': scheduled_end,
            'bank_id': bank_id,
            'bank_question_count': bank_question_count,
            'created_at': now()
        })
    else:
        if not db.quizzes.find_one({'_id': quiz_id, 'creator_id': creator_id}):
            raise ValueError('Quiz not found.')
        if db.attempts.find_one({'quiz_id': quiz_id}):
            raise ValueError('This quiz cannot be edited after a student starts an attempt.')
            
        db.quizzes.update_one({'_id': quiz_id}, {'$set': {
            'title': title, 'description': description, 'instructions': instructions,
            'time_limit': minutes, 'pass_fail_enabled': pass_fail_enabled,
            'pass_percentage': pass_percentage, 'randomize_questions': randomize_questions,
            'randomize_options': randomize_options, 'allow_review': allow_review, 'scheduled_start': scheduled_start,
            'scheduled_end': scheduled_end, 'bank_id': bank_id, 'bank_question_count': bank_question_count
        }})
        db.questions.delete_many({'quiz_id': quiz_id})
        
    if bank_id:
        bank_questions = to_dict_list(db.bank_questions.find({'bank_id': bank_id}).sort('position', 1))
        docs = []
        for position, question in enumerate(bank_questions):
            docs.append({
                '_id': get_next_sequence_value('questions'),
                'quiz_id': quiz_id,
                'position': position,
                'text': question['text'],
                'option_0': question['option_0'],
                'option_1': question['option_1'],
                'option_2': question['option_2'],
                'option_3': question['option_3'],
                'correct_option': question['correct_option']
            })
        if docs:
            db.questions.insert_many(docs)
    else:
        docs = []
        for position, question in enumerate(questions):
            docs.append({
                '_id': get_next_sequence_value('questions'),
                'quiz_id': quiz_id,
                'position': position,
                'text': question['text'],
                'option_0': question['options'][0],
                'option_1': question['options'][1],
                'option_2': question['options'][2],
                'option_3': question['options'][3],
                'correct_option': question['correct_option']
            })
        if docs:
            db.questions.insert_many(docs)
            
    return quiz_id


def start_attempt(quiz, name, roll, browser_token):
    name = clean_text(name, 'Student name', 100)
    roll = clean_text(roll, 'Roll number', 50)
    normalized = normalize_roll(roll)
    if not normalized:
        raise ValueError('Enter a valid roll number.')
    db = get_db()
    
    existing = to_dict(db.attempts.find_one({'quiz_id': quiz['id'], 'normalized_roll': normalized}))
    if existing:
        if existing['browser_token'] == browser_token:
            return existing['id']
        raise ValueError('This roll number already has an attempt for this quiz.')
        
    quiz_record = to_dict(db.quizzes.find_one({'_id': quiz['id']}))
    minutes = quiz_record['time_limit']
    started = now()
    
    if quiz_record.get('scheduled_start') and started < quiz_record['scheduled_start']:
        raise ValueError('This quiz has not started yet.')
    if quiz_record.get('scheduled_end') and started > quiz_record['scheduled_end']:
        raise ValueError('This quiz has already ended.')
        
    question_order_json = None
    if quiz_record.get('randomize_questions') or quiz_record.get('randomize_options') or quiz_record.get('bank_question_count'):
        import random
        questions = to_dict_list(db.questions.find({'quiz_id': quiz['id']}).sort('position', 1))
        q_ids = [q['id'] for q in questions]
        
        if quiz_record.get('randomize_questions'):
            random.shuffle(q_ids)
            
        if quiz_record.get('bank_question_count'):
            q_ids = q_ids[:quiz_record['bank_question_count']]
            
        options_map = {}
        if quiz_record.get('randomize_options'):
            for q_id in q_ids:
                opt_order = [0, 1, 2, 3]
                random.shuffle(opt_order)
                options_map[str(q_id)] = opt_order
        else:
            for q_id in q_ids:
                options_map[str(q_id)] = [0, 1, 2, 3]
        
        question_order_json = json.dumps({'questions': q_ids, 'options': options_map})

    attempt_id = get_next_sequence_value('attempts')
    try:
        db.attempts.insert_one({
            '_id': attempt_id,
            'quiz_id': quiz['id'],
            'browser_token': browser_token,
            'name': name,
            'roll_number': roll,
            'normalized_roll': normalized,
            'started_at': started,
            'deadline': started + minutes * 60,
            'submitted_at': None,
            'question_order': question_order_json,
            'total': None,
            'correct': None,
            'wrong': None,
            'unanswered': None,
            'score': None,
            'percentage': None,
            'time_taken': None
        })
    except DuplicateKeyError:
        raise ValueError('This roll number already has an attempt for this quiz.')

    return attempt_id


def grade(db, attempt, timestamp):
    """Calculates correct answers using DB responses."""
    questions = list(db.questions.find({'quiz_id': attempt['quiz_id']}))
    answers = list(db.answers.find({'attempt_id': attempt['id']}))
    ans_map = {a['question_id']: a.get('selected_option') for a in answers}
    
    # We only grade questions that are in the attempt's subset (for banks)
    subset_ids = None
    if attempt.get('question_order'):
        order_data = json.loads(attempt['question_order'])
        if order_data.get('questions'):
            subset_ids = set(order_data['questions'])
            
    total = 0
    correct = 0
    unanswered = 0
    
    for q in questions:
        if subset_ids is not None and q['_id'] not in subset_ids:
            continue
            
        total += 1
        opt = ans_map.get(q['_id'])
        if opt == q['correct_option']:
            correct += 1
        elif opt is None:
            unanswered += 1
            
    elapsed = max(0, min(timestamp, attempt['deadline']) - attempt['started_at'])
    
    # We need to guard against concurrent submissions
    db.attempts.update_one(
        {'_id': attempt['id'], 'submitted_at': None},
        {'$set': {
            'submitted_at': min(timestamp, attempt['deadline']),
            'total': total,
            'correct': correct,
            'wrong': total - correct - unanswered,
            'unanswered': unanswered,
            'score': correct,
            'percentage': round(correct * 100 / total, 2) if total else 0,
            'time_taken': elapsed
        }}
    )


def process_attempt(attempt_id, answers=None, submit=False):
    db = get_db()
    
    attempt = to_dict(db.attempts.find_one({'_id': attempt_id}))
    if not attempt:
        raise ValueError('Attempt not found')
        
    timestamp = now()
    if attempt.get('submitted_at') is None:
        if timestamp >= attempt['deadline']:
            # Late requests cannot change saved answers
            grade(db, attempt, timestamp)
        else:
            if answers is not None:
                if not isinstance(answers, dict):
                    raise ValueError('Answers must be an object.')
                    
                q_cursor = db.questions.find({'quiz_id': attempt['quiz_id']}, {'_id': 1})
                ids = {str(q['_id']) for q in q_cursor}
                
                # Check subset if randomization/banks are active
                if attempt.get('question_order'):
                    order_data = json.loads(attempt['question_order'])
                    if order_data.get('questions'):
                        # Restrict to only the assigned subset
                        ids = {str(q) for q in order_data['questions']}

                for question_id, option in answers.items():
                    if question_id not in ids or (option is not None and
                            (type(option) is not int or option not in range(4))):
                        raise ValueError('Invalid question or answer option.')
                        
                for question_id, option in answers.items():
                    qid_int = int(question_id)
                    ans_id = f"{attempt_id}_{qid_int}"
                    if option is None:
                        db.answers.delete_one({'_id': ans_id})
                    else:
                        db.answers.update_one(
                            {'_id': ans_id},
                            {'$set': {
                                'attempt_id': attempt_id,
                                'quiz_id': attempt['quiz_id'],
                                'question_id': qid_int,
                                'selected_option': option
                            }},
                            upsert=True
                        )
            if submit:
                grade(db, attempt, timestamp)
                
    return to_dict(db.attempts.find_one({'_id': attempt_id}))


def expire_attempts(quiz_id):
    db = get_db()
    timestamp = now()
    attempts = db.attempts.find({'quiz_id': quiz_id, 'submitted_at': None, 'deadline': {'$lte': timestamp}})
    for attempt in attempts:
        grade(db, to_dict(attempt), timestamp)


def ranked_results(quiz_id):
    expire_attempts(quiz_id)
    db = get_db()
    results = to_dict_list(db.attempts.find({'quiz_id': quiz_id, 'submitted_at': {'$ne': None}}).sort([
        ('score', -1),
        ('time_taken', 1),
        ('submitted_at', 1),
        ('_id', 1)
    ]))
    return results
