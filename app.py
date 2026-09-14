"""C2C Flask application. Run: flask --app app run"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from pymongo.errors import DuplicateKeyError

from flask import (Flask, abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, send_file, session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, init_app, get_next_sequence_value, to_dict, to_dict_list
from quiz_service import (clean_text, now, process_attempt, ranked_results,
                          save_quiz, start_attempt)
from reports import results_pdf


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY'),
        DATABASE_URL=os.environ.get('DATABASE_URL'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        MAX_FORM_MEMORY_SIZE=8 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if not app.config['SECRET_KEY'] or len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('Set SECRET_KEY to a random value of at least 32 characters. See README.')
    init_app(app)

    def csrf_token():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']

    app.jinja_env.globals['csrf_token'] = csrf_token

    @app.template_filter('ist')
    def format_ist(timestamp):
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Kolkata")).strftime('%d %b %Y, %I:%M %p IST') if timestamp is not None else 'In progress'

    @app.before_request
    def load_user_and_check_csrf():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
            
        g.creator = None
        if session.get('creator_id'):
            g.creator = to_dict(get_db().creators.find_one({'_id': session['creator_id']}))
        if request.method == 'POST':
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            expected = session.get('csrf_token', '')
            # compare_digest accepts ASCII strings only; malformed forms must not cause a 500.
            if not expected or not supplied.isascii() or not secrets.compare_digest(expected, supplied):
                abort(400, description='Your form expired. Refresh the page and try again.')

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(ValueError)
    def validation_error(error):
        if request.is_json:
            return jsonify(error=str(error)), 400
        return render_template('error.html', title='Check your input', message=str(error), status=400), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.is_json:
            return jsonify(error=error.description), error.code
        return render_template('error.html', title=error.name, message=error.description, status=error.code), error.code

    @app.errorhandler(500)
    def internal_error(error):
        return render_template('error.html', title='Something went wrong',
                               message='Please try again later.', status=500), 500

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.creator is None:
                return redirect(url_for('login'))
            return view(*args, **kwargs)
        return wrapped

    def owned_quiz(quiz_id):
        if not 1 <= quiz_id <= 2 ** 63 - 1:
            abort(404, description='Quiz not found.')
        quiz = to_dict(get_db().quizzes.find_one({'_id': quiz_id, 'creator_id': g.creator['id']}))
        if quiz is None:
            abort(404, description='Quiz not found.')
        quiz['question_count'] = get_db().questions.count_documents({'quiz_id': quiz_id})
        return quiz

    def student_attempt(attempt_id):
        if not 1 <= attempt_id <= 2 ** 63 - 1:
            abort(404, description='This quiz is no longer available.')
        attempt = to_dict(get_db().attempts.find_one({'_id': attempt_id, 'browser_token': session.get('student_token', '')}))
        if attempt is None:
            abort(404, description='This quiz is no longer available.')
        return attempt

    def quiz_form_data():
        try:
            return json.loads(request.form.get('quiz_data', ''))
        except (ValueError, TypeError):
            raise ValueError('Enable JavaScript and complete the quiz form.') from None

    @app.get('/')
    def home():
        return render_template('home.html')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            name = clean_text(request.form.get('name', ''), 'Name', 100)
            email = clean_text(request.form.get('email', ''), 'Email', 254).casefold()
            password = request.form.get('password', '')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
                raise ValueError('Enter a valid email address.')
            if not 8 <= len(password) <= 128:
                raise ValueError('Password must contain 8 to 128 characters.')
            if password != request.form.get('confirm_password'):
                raise ValueError('Passwords do not match.')
            db = get_db()
            try:
                creator_id = get_next_sequence_value('creators')
                db.creators.insert_one({
                    '_id': creator_id,
                    'name': name,
                    'email': email,
                    'password_hash': generate_password_hash(password),
                    'created_at': now()
                })
            except DuplicateKeyError:
                raise ValueError('An account with that email already exists.') from None
            flash('Account created. Log in to build your first quiz.', 'success')
            return redirect(url_for('login'))
        return render_template('register.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email', '').strip().casefold()
            password = request.form.get('password', '')
            creator = to_dict(get_db().creators.find_one({'email': email}))
            if len(password) > 128 or not creator or not check_password_hash(creator['password_hash'], password):
                raise ValueError('Incorrect email or password.')
            student_token = session.get('student_token')
            session.clear()
            if student_token:
                session['student_token'] = student_token
            session['creator_id'] = creator['id']
            return redirect(url_for('dashboard'))
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        session.pop('creator_id', None)
        session.pop('csrf_token', None)
        return redirect(url_for('home'))

    @app.get('/dashboard')
    @login_required
    def dashboard():
        db = get_db()
        quizzes = to_dict_list(db.quizzes.find({'creator_id': g.creator['id']}).sort([('created_at', -1), ('_id', -1)]))
        
        quiz_list = []
        current_time = now()
        for q_dict in quizzes:
            q_dict['question_count'] = db.questions.count_documents({'quiz_id': q_dict['id']})
            q_dict['participant_count'] = db.attempts.count_documents({'quiz_id': q_dict['id']})
            
            status = 'LIVE'
            if q_dict.get('scheduled_start') and current_time < q_dict['scheduled_start']:
                status = 'SCHEDULED'
            elif q_dict.get('scheduled_end') and current_time > q_dict['scheduled_end']:
                status = 'ENDED'
            elif q_dict['participant_count'] == 0 and not q_dict.get('scheduled_start') and not q_dict.get('scheduled_end'):
                status = 'DRAFT'
            q_dict['status'] = status
            quiz_list.append(q_dict)
            
        return render_template('dashboard.html', quizzes=quiz_list)

    @app.route('/quizzes/new', methods=['GET', 'POST'])
    @login_required
    def create_quiz():
        if request.method == 'POST':
            save_quiz(g.creator['id'], quiz_form_data())
            flash('Quiz created. Share the link or code with your students.', 'success')
            return redirect(url_for('dashboard'))
        
        db = get_db()
        banks = to_dict_list(db.question_banks.find({'creator_id': g.creator['id']}).sort('name', 1))
        for bank in banks:
            bank['question_count'] = db.bank_questions.count_documents({'bank_id': bank['id']})
            
        return render_template('create_quiz.html', quiz=None, initial_questions=[], banks=banks)

    @app.route('/quizzes/<int:quiz_id>/edit', methods=['GET', 'POST'])
    @login_required
    def edit_quiz(quiz_id):
        quiz = dict(owned_quiz(quiz_id))
        if quiz.get('scheduled_start'):
            quiz['scheduled_start_iso'] = datetime.fromtimestamp(quiz['scheduled_start']).strftime("%Y-%m-%dT%H:%M")
        if quiz.get('scheduled_end'):
            quiz['scheduled_end_iso'] = datetime.fromtimestamp(quiz['scheduled_end']).strftime("%Y-%m-%dT%H:%M")
        
        db = get_db()
        if db.attempts.find_one({'quiz_id': quiz_id}):
            abort(409, description='This quiz is locked because a student has already attempted it.')
        
        if request.method == 'POST':
            save_quiz(g.creator['id'], quiz_form_data(), quiz_id)
            flash('Quiz updated.', 'success')
            return redirect(url_for('dashboard'))
            
        questions = to_dict_list(db.questions.find({'quiz_id': quiz_id}).sort('position', 1))
        initial = [{'text': q['text'], 'options': [q[f'option_{i}'] for i in range(4)],
                    'correct_option': q['correct_option']} for q in questions]
                    
        banks = to_dict_list(db.question_banks.find({'creator_id': g.creator['id']}).sort('name', 1))
        for bank in banks:
            bank['question_count'] = db.bank_questions.count_documents({'bank_id': bank['id']})
            
        return render_template('edit_quiz.html', quiz=quiz, initial_questions=initial, banks=banks)

    def owned_bank(bank_id):
        bank = to_dict(get_db().question_banks.find_one({'_id': bank_id}))
        if not bank:
            abort(404, description='Question bank not found.')
        if bank['creator_id'] != g.creator['id']:
            abort(403, description='You do not have permission to access this question bank.')
        return bank

    @app.get('/banks')
    @login_required
    def banks():
        db = get_db()
        banks = to_dict_list(db.question_banks.find({'creator_id': g.creator['id']}).sort('created_at', -1))
        for bank in banks:
            bank['question_count'] = db.bank_questions.count_documents({'bank_id': bank['id']})
        return render_template('banks.html', banks=banks)

    @app.route('/banks/new', methods=['GET', 'POST'])
    @login_required
    def create_bank():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                raise ValueError('Bank name is required.')
            db = get_db()
            
            bank_id = get_next_sequence_value('question_banks')
            db.question_banks.insert_one({
                '_id': bank_id,
                'creator_id': g.creator['id'],
                'name': name,
                'created_at': now()
            })
            
            questions = json.loads(request.form.get('questions', '[]'))
            if questions:
                docs = []
                for i, q in enumerate(questions):
                    docs.append({
                        '_id': get_next_sequence_value('bank_questions'),
                        'bank_id': bank_id,
                        'position': i,
                        'text': q['text'].strip(),
                        'option_0': q['options'][0].strip(),
                        'option_1': q['options'][1].strip(),
                        'option_2': q['options'][2].strip(),
                        'option_3': q['options'][3].strip(),
                        'correct_option': int(q['correct_option'])
                    })
                db.bank_questions.insert_many(docs)
                
            flash('Question bank created.', 'success')
            return redirect(url_for('banks'))
        return render_template('edit_bank.html', bank=None, initial_questions=[])

    @app.route('/banks/<int:bank_id>/edit', methods=['GET', 'POST'])
    @login_required
    def edit_bank(bank_id):
        bank = owned_bank(bank_id)
        db = get_db()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name:
                raise ValueError('Bank name is required.')
            
            db.question_banks.update_one({'_id': bank_id}, {'$set': {'name': name}})
            db.bank_questions.delete_many({'bank_id': bank_id})
            
            questions = json.loads(request.form.get('questions', '[]'))
            if questions:
                docs = []
                for i, q in enumerate(questions):
                    docs.append({
                        '_id': get_next_sequence_value('bank_questions'),
                        'bank_id': bank_id,
                        'position': i,
                        'text': q['text'].strip(),
                        'option_0': q['options'][0].strip(),
                        'option_1': q['options'][1].strip(),
                        'option_2': q['options'][2].strip(),
                        'option_3': q['options'][3].strip(),
                        'correct_option': int(q['correct_option'])
                    })
                db.bank_questions.insert_many(docs)
                
            flash('Question bank updated.', 'success')
            return redirect(url_for('banks'))
            
        questions = to_dict_list(db.bank_questions.find({'bank_id': bank_id}).sort('position', 1))
        initial = [{'text': q['text'], 'options': [q.get(f'option_{i}') for i in range(4)],
                    'correct_option': q['correct_option']} for q in questions]
        return render_template('edit_bank.html', bank=bank, initial_questions=initial)

    @app.post('/banks/<int:bank_id>/delete')
    @login_required
    def delete_bank(bank_id):
        owned_bank(bank_id)
        db = get_db()
        if db.quizzes.find_one({'bank_id': bank_id}):
            abort(409, description='This question bank is used by an existing quiz and cannot be deleted.')
        
        db.bank_questions.delete_many({'bank_id': bank_id})
        db.question_banks.delete_one({'_id': bank_id})
        flash('Question bank deleted.', 'success')
        return redirect(url_for('banks'))


    @app.route('/join', methods=['GET', 'POST'])
    @app.route('/join/<code>', methods=['GET', 'POST'])
    def join_quiz(code=None):
        code = (code or request.values.get('code', '')).strip().upper()
        quiz = None
        db = get_db()
        if code:
            quiz = to_dict(db.quizzes.find_one({'code': code}))
            if quiz is None:
                raise ValueError('Quiz code not found. Check the code with your creator.')
            quiz['question_count'] = db.questions.count_documents({'quiz_id': quiz['id']})
            
        if request.method == 'POST':
            if quiz is None:
                raise ValueError('Enter a quiz code.')
            if 'student_token' not in session:
                session['student_token'] = secrets.token_urlsafe(32)
            attempt_id = start_attempt(quiz, request.form.get('name', ''), request.form.get('roll_number', ''),
                                       session['student_token'])
            return redirect(url_for('take_quiz', attempt_id=attempt_id))
        return render_template('join_quiz.html', quiz=quiz, code=code)

    @app.get('/attempts/<int:attempt_id>')
    def take_quiz(attempt_id):
        student_attempt(attempt_id)
        attempt = process_attempt(attempt_id)
        if attempt.get('submitted_at') is not None:
            return redirect(url_for('result', attempt_id=attempt_id))
            
        db = get_db()
        quiz = to_dict(db.quizzes.find_one({'_id': attempt['quiz_id']}))
        
        # We need id, position, text, option_0, option_1, option_2, option_3
        q_cursor = db.questions.find({'quiz_id': quiz['id']}, {'correct_option': 0}).sort('position', 1)
        questions = to_dict_list(q_cursor)
                                      
        if attempt.get('question_order'):
            order_data = json.loads(attempt['question_order'])
            q_order = order_data.get('questions')
            o_map = order_data.get('options', {})
            
            if q_order:
                q_dict = {q['id']: dict(q) for q in questions}
                questions = [q_dict[q_id] for q_id in q_order if q_id in q_dict]
            else:
                questions = [dict(q) for q in questions]
                
            if o_map:
                for q in questions:
                    qid_str = str(q['id'])
                    if qid_str in o_map:
                        q['option_order'] = o_map[qid_str]
        else:
            questions = [dict(q) for q in questions]
            
        answers_cursor = db.answers.find({'attempt_id': attempt_id})
        saved = {str(row['question_id']): row['selected_option'] for row in answers_cursor}
        
        return render_template('take_quiz.html', quiz=quiz, attempt=attempt, questions=questions,
                               saved=saved, server_now=now())

    @app.post('/attempts/<int:attempt_id>/save')
    def save_answers(attempt_id):
        student_attempt(attempt_id)
        data = request.get_json()
        if not isinstance(data, dict) or not isinstance(data.get('answers'), dict):
            raise ValueError('Provide an answers object to save.')
        attempt = process_attempt(attempt_id, data['answers'])
        return jsonify(submitted=attempt.get('submitted_at') is not None, deadline=attempt['deadline'],
                       server_now=now(), result_url=url_for('result', attempt_id=attempt_id))

    @app.post('/attempts/<int:attempt_id>/submit')
    def submit_quiz(attempt_id):
        student_attempt(attempt_id)
        data = request.get_json()
        if not isinstance(data, dict) or not isinstance(data.get('answers', {}), dict):
            raise ValueError('Invalid submission. Answers must be an object.')
        process_attempt(attempt_id, data.get('answers', {}), submit=True)
        return jsonify(result_url=url_for('result', attempt_id=attempt_id))

    @app.get('/attempts/<int:attempt_id>/result')
    def result(attempt_id):
        student_attempt(attempt_id)
        attempt = process_attempt(attempt_id)
        if attempt.get('submitted_at') is None:
            return redirect(url_for('take_quiz', attempt_id=attempt_id))
        quiz = to_dict(get_db().quizzes.find_one({'_id': attempt['quiz_id']}))
        return render_template('result.html', result=attempt, quiz=quiz)

    @app.get('/quizzes/<int:quiz_id>/results')
    @login_required
    def quiz_results(quiz_id):
        quiz = owned_quiz(quiz_id)
        results = ranked_results(quiz_id)
        db = get_db()
        active = db.attempts.count_documents({'quiz_id': quiz_id, 'submitted_at': None})
                                  
        analytics = None
        if results:
            avg_score = round(sum(r['score'] for r in results) / len(results), 1)
            high_score = max(r['score'] for r in results)
            low_score = min(r['score'] for r in results)
            avg_percent = round(sum(r['percentage'] for r in results) / len(results), 1)
            pass_count = sum(1 for r in results if r['percentage'] >= quiz.get('pass_percentage', 50)) if quiz.get('pass_fail_enabled') else 0
            fail_count = len(results) - pass_count if quiz.get('pass_fail_enabled') else 0
            
            # Reimplementing the complex SQL JOIN in Python
            q_stats = []
            questions = to_dict_list(db.questions.find({'quiz_id': quiz_id}).sort('position', 1))
            
            # Fetch all answers for submitted attempts
            submitted_attempt_ids = [r['id'] for r in results]
            answers = to_dict_list(db.answers.find({
                'attempt_id': {'$in': submitted_attempt_ids},
                'quiz_id': quiz_id
            }))
            
            for q in questions:
                q_id = q['id']
                q_answers = [a for a in answers if a['question_id'] == q_id]
                answered_count = len(q_answers)
                correct_count = sum(1 for a in q_answers if a.get('selected_option') == q['correct_option'])
                
                q_stats.append({
                    'position': q['position'],
                    'text': q['text'],
                    'answered': answered_count,
                    'correct': correct_count
                })
            
            analytics = {
                'avg_score': avg_score, 'high_score': high_score, 'low_score': low_score,
                'avg_percent': avg_percent, 'pass_count': pass_count, 'fail_count': fail_count,
                'q_stats': q_stats, 'total': len(results)
            }
        return render_template('quiz_results.html', quiz=quiz, results=results, active=active, analytics=analytics)

    @app.get('/quizzes/<int:quiz_id>/live_stats')
    @login_required
    def live_stats(quiz_id):
        quiz = owned_quiz(quiz_id)
        db = get_db()
        total_attempts = db.attempts.count_documents({'quiz_id': quiz_id})
        submitted_count = db.attempts.count_documents({'quiz_id': quiz_id, 'submitted_at': {'$ne': None}})
        in_progress = total_attempts - submitted_count
        avg_score = 0
        if submitted_count > 0:
            pipeline = [
                {'$match': {'quiz_id': quiz_id, 'submitted_at': {'$ne': None}}},
                {'$group': {'_id': None, 'avg_percentage': {'$avg': '$percentage'}}}
            ]
            agg = list(db.attempts.aggregate(pipeline))
            if agg:
                avg_score = round(agg[0]['avg_percentage'] or 0, 1)
                
        active_cursor = db.attempts.find({'quiz_id': quiz_id, 'submitted_at': None}).sort('started_at', -1)
        active_attempts = []
        total_q = db.questions.count_documents({'quiz_id': quiz_id})
        
        for a in active_cursor:
            ans_count = db.answers.count_documents({'attempt_id': a['_id']})
            active_attempts.append({
                'name': a['name'],
                'roll_number': a['roll_number'],
                'answered': ans_count,
                'total_q': total_q
            })
            
        return {
            'total': total_attempts,
            'submitted': submitted_count,
            'in_progress': in_progress,
            'completion': round(submitted_count * 100 / total_attempts, 1) if total_attempts else 0,
            'avg_score': avg_score,
            'active_count': len(active_attempts),
            'participants': active_attempts
        }

    @app.get('/quizzes/<int:quiz_id>/leaderboard')
    @login_required
    def leaderboard(quiz_id):
        quiz = owned_quiz(quiz_id)
        return render_template('leaderboard.html', quiz=quiz, results=ranked_results(quiz_id))

    @app.get('/quizzes/<int:quiz_id>/pdf')
    @login_required
    def download_pdf(quiz_id):
        quiz = owned_quiz(quiz_id)
        return send_file(results_pdf(quiz, ranked_results(quiz_id)),
                         mimetype='application/pdf', as_attachment=True,
                         download_name=f"results_{quiz['code']}.pdf")

    @app.get('/quizzes/<int:quiz_id>/results/csv')
    @login_required
    def download_csv(quiz_id):
        quiz = owned_quiz(quiz_id)
        results = ranked_results(quiz_id)
        
        import csv
        from io import StringIO
        si = StringIO()
        writer = csv.writer(si)
        
        headings = ['Rank', 'Student', 'Roll no.', 'Score', 'Total', 'Percentage']
        if quiz.get('pass_fail_enabled'):
            headings.append('Status')
        headings.extend(['Correct', 'Wrong', 'Unanswered', 'Time (s)', 'Submitted (IST)'])
        
        writer.writerow(headings)
        
        for rank, result in enumerate(results, 1):
            submitted = datetime.fromtimestamp(result['submitted_at'], __import__('zoneinfo').ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %I:%M:%S %p')
            row = [rank, result['name'], result['roll_number'], result['score'], result['total'], result['percentage']]
            if quiz.get('pass_fail_enabled'):
                row.append('PASS' if result['percentage'] >= quiz.get('pass_percentage', 50) else 'FAIL')
            row.extend([result['correct'], result['wrong'], result['unanswered'], result['time_taken'], submitted])
            writer.writerow(row)
            
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename=results_{quiz['code']}.csv"
        output.headers["Content-type"] = "text/csv"
        return output

    @app.get('/quizzes/<int:quiz_id>/qr')
    @login_required
    def download_qr(quiz_id):
        quiz = owned_quiz(quiz_id)
        import qrcode
        from io import BytesIO
        import base64
        url = url_for('join_quiz', code=quiz['code'], _external=True)
        img = qrcode.make(url)
        buf = BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        # If ?inline=1, return JSON with base64 data for the modal
        if request.args.get('inline'):
            b64 = base64.b64encode(buf.getvalue()).decode('ascii')
            return jsonify(image=f'data:image/png;base64,{b64}', url=url)
        return send_file(buf, mimetype='image/png', as_attachment=True, download_name=f"qr_{quiz['code']}.png")

    @app.get('/quizzes/<int:quiz_id>/xlsx')
    @login_required
    def download_xlsx(quiz_id):
        quiz = owned_quiz(quiz_id)
        results = ranked_results(quiz_id)
        from io import BytesIO
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = 'Results'
        headings = ['Rank', 'Student', 'Roll no.', 'Score', 'Total', 'Percentage']
        if quiz.get('pass_fail_enabled'):
            headings.append('Status')
        headings.extend(['Correct', 'Wrong', 'Unanswered', 'Time (s)', 'Submitted (IST)'])
        ws.append(headings)
        for rank, result in enumerate(results, 1):
            submitted = datetime.fromtimestamp(result['submitted_at'], __import__('zoneinfo').ZoneInfo('Asia/Kolkata')).strftime('%Y-%m-%d %I:%M:%S %p')
            row = [rank, result['name'], result['roll_number'], result['score'], result['total'], result['percentage']]
            if quiz.get('pass_fail_enabled'):
                row.append('PASS' if result['percentage'] >= quiz.get('pass_percentage', 50) else 'FAIL')
            row.extend([result['correct'], result['wrong'], result['unanswered'], result['time_taken'], submitted])
            ws.append(row)
        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         as_attachment=True, download_name=f"results_{quiz['code']}.xlsx")

    @app.post('/quizzes/<int:quiz_id>/delete')
    @login_required
    def delete_quiz(quiz_id):
        quiz = owned_quiz(quiz_id)
        db = get_db()
        
        db.answers.delete_many({'quiz_id': quiz_id})
        db.attempts.delete_many({'quiz_id': quiz_id})
        db.questions.delete_many({'quiz_id': quiz_id})
        db.quizzes.delete_one({'_id': quiz_id})
        
        flash('Quiz deleted successfully.', 'success')
        return redirect(url_for('dashboard'))

    return app
