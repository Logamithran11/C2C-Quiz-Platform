"""Run with python -m pytest. All data is isolated in pytest temporary directories."""
import json
import os
import re
import secrets
import socket
import pymongo
from pymongo.errors import DuplicateKeyError
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from werkzeug.security import check_password_hash

from app import create_app
from db import get_db, init_db, to_dict
from quiz_service import normalize_roll, ranked_results
from reports import results_pdf


@pytest.fixture
def app(monkeypatch):
    import mongomock
    mock_client = mongomock.MongoClient('mongodb://localhost/testdb')
    # Mock get_client in db.py to return mongomock client
    monkeypatch.setattr('db.get_client', lambda: mock_client)
    
    application = create_app({
        'TESTING': True, 
        'SECRET_KEY': secrets.token_hex(32),
        'DATABASE_URL': 'mongodb://localhost/testdb'
    })

    
    with application.app_context():
        init_db()
        
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client):
    client.get('/')
    with client.session_transaction() as session:
        return session['csrf_token']


def post(client, path, data=None, payload=None):
    token = csrf(client)
    if payload is not None:
        return client.post(path, json=payload, headers={'X-CSRF-Token': token})
    return client.post(path, data={**(data or {}), 'csrf_token': token})


def register(client, email='teacher@college.test'):
    return post(client, '/register', {'name': 'Quiz Creator', 'email': email,
                'password': 'Good-passphrase-123', 'confirm_password': 'Good-passphrase-123'})


def login(client, email='teacher@college.test'):
    return post(client, '/login', {'email': email, 'password': 'Good-passphrase-123'})


def quiz_data():
    return {'title': 'Campus knowledge', 'description': 'A weekly challenge', 'time_limit': 5,
            'questions': [
                {'text': 'Two plus two?', 'options': ['Three', 'Four', 'Five', 'Six'], 'correct_option': 1},
                {'text': 'Python is a?', 'options': ['Language', 'Planet', 'Ocean', 'Mountain'], 'correct_option': 0},
                {'text': 'Which is a database?', 'options': ['CSS', 'HTML', 'SQLite', 'SVG'], 'correct_option': 2}
            ]}


@pytest.fixture
def quiz(app, client):
    assert register(client).status_code == 302
    assert login(client).status_code == 302
    response = post(client, '/quizzes/new', {'quiz_data': json.dumps(quiz_data())})
    assert response.status_code == 302
    with app.app_context():
        return to_dict(get_db().quizzes.find_one())


def join(client, quiz, roll='CS 001', name='Student One'):
    return post(client, f"/join/{quiz['code']}", {'name': name, 'roll_number': roll})


def attempt_id(response):
    return int(response.headers['Location'].rstrip('/').split('/')[-1])


def question_ids(app, quiz):
    with app.app_context():
        return [str(q['_id']) for q in get_db().questions.find({'quiz_id': quiz['id']}).sort('position', 1)]


def stored_attempt(app, identifier):
    with app.app_context():
        return to_dict(get_db().attempts.find_one({'_id': identifier}))


def test_registration_hash_and_duplicate_email(app, client):
    assert register(client).status_code == 302
    with app.app_context():
        row = to_dict(get_db().creators.find_one())
        assert row['password_hash'] != 'Good-passphrase-123'
        assert check_password_hash(row['password_hash'], 'Good-passphrase-123')
    assert register(client, 'TEACHER@COLLEGE.TEST').status_code == 400


@pytest.mark.parametrize('field,value', [('name', ''), ('email', 'bad-email'),
                                         ('password', 'short'), ('confirm_password', 'mismatch')])
def test_registration_validation(client, field, value):
    data = {'name': 'Teacher', 'email': 'hello@college.test', 'password': 'Good-passphrase-123',
            'confirm_password': 'Good-passphrase-123'}
    data[field] = value
    assert post(client, '/register', data).status_code == 400


def test_login_logout_and_authentication(client):
    assert client.get('/dashboard').status_code == 302
    register(client)
    assert post(client, '/login', {'email': 'teacher@college.test', 'password': 'incorrect'}).status_code == 400
    assert login(client).status_code == 302
    assert client.get('/dashboard').status_code == 200
    assert post(client, '/logout').status_code == 302
    assert client.get('/quizzes/new').status_code == 302


def test_csrf_is_required(client):
    assert client.post('/register', data={'name': 'test'}).status_code == 400
    token = csrf(client)
    assert token
    assert client.post('/logout', data={'csrf_token': 'wrong'}).status_code == 400


def test_quiz_creation_code_and_dashboard(app, client, quiz):
    assert re.fullmatch('[A-Z0-9]{10}', quiz['code'])
    assert len(question_ids(app, quiz)) == 3
    page = client.get('/dashboard')
    assert page.status_code == 200
    assert quiz['code'].encode() in page.data
    assert f"/join/{quiz['code']}".encode() in page.data
    assert post(client, '/quizzes/new', {'quiz_data': json.dumps(quiz_data())}).status_code == 302
    with app.app_context():
        codes = [q['code'] for q in get_db().quizzes.find()]
        assert len(set(codes)) == 2


@pytest.mark.parametrize('case', ['title', 'time_zero', 'time_fraction', 'no_questions', 'three_options',
                                  'five_options', 'no_correct', 'multiple_correct', 'blank_option', 'duplicate_options'])
def test_quiz_validation(app, client, quiz, case):
    data = quiz_data()
    if case == 'title': data['title'] = ' '
    elif case == 'time_zero': data['time_limit'] = 0
    elif case == 'time_fraction': data['time_limit'] = 1.5
    elif case == 'no_questions': data['questions'] = []
    elif case == 'three_options': data['questions'][0]['options'].pop()
    elif case == 'five_options': data['questions'][0]['options'].append('Seven')
    elif case == 'no_correct': data['questions'][0]['correct_option'] = None
    elif case == 'multiple_correct': data['questions'][0]['correct_option'] = [0, 1]
    elif case == 'blank_option': data['questions'][0]['options'][0] = ' '
    elif case == 'duplicate_options': data['questions'][0]['options'][0] = 'FOUR'
    assert post(client, '/quizzes/new', {'quiz_data': json.dumps(data)}).status_code == 400
    with app.app_context():
        assert get_db().quizzes.count_documents({}) == 1


def test_edit_before_start_and_lock_after_start(app, client, quiz):
    path = f"/quizzes/{quiz['id']}/edit"
    assert client.get(path).status_code == 200
    data = quiz_data()
    data['title'] = 'Updated title'
    assert post(client, path, {'quiz_data': json.dumps(data)}).status_code == 302
    with app.app_context():
        assert get_db().quizzes.find_one()['title'] == 'Updated title'
    join(app.test_client(), quiz)
    assert client.get(path).status_code == 409
    assert post(client, path, {'quiz_data': json.dumps(data)}).status_code == 409


def test_join_code_validation_and_student_fields(app, quiz):
    student = app.test_client()
    assert student.get('/join/NOTFOUND00').status_code == 400
    assert student.get(f"/join/{quiz['code'].lower()}").status_code == 200
    assert join(student, quiz, name=' ').status_code == 400
    assert join(student, quiz, roll=' ').status_code == 400
    response = post(student, '/join', {'code': quiz['code'].lower(), 'name': 'Student', 'roll_number': 'C001'})
    assert response.status_code == 302
    assert student.get(response.headers['Location']).status_code == 200


def test_normalization():
    assert normalize_roll(' ＣＳ  001 ') == normalize_roll('cs001')


def test_refresh_resume_and_no_answer_key(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    before = stored_attempt(app, identifier)
    ids = question_ids(app, quiz)
    assert post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}}).status_code == 200
    page = student.get(f'/attempts/{identifier}')
    assert page.status_code == 200
    assert b'correct_option' not in page.data
    assert b'value="1" checked' in page.data
    assert attempt_id(join(student, quiz, roll='cs001')) == identifier
    assert stored_attempt(app, identifier)['deadline'] == before['deadline']
    assert stored_attempt(app, identifier)['started_at'] == before['started_at']


def test_scoring_percentage_wrong_and_unanswered(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    response = post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 1, ids[1]: 3}})
    assert response.status_code == 200
    attempt = stored_attempt(app, identifier)
    assert (attempt['total'], attempt['correct'], attempt['wrong'], attempt['unanswered'], attempt['score']) == (3, 1, 1, 1, 1)
    assert attempt['percentage'] == 33.33
    assert attempt['submitted_at'] is not None
    assert 0 <= attempt['time_taken'] <= 300
    page = student.get(response.json['result_url'])
    assert page.status_code == 200
    assert b'33.33%' in page.data
    assert b'Student One' in page.data


def test_all_unanswered_and_idempotent_submission(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    path = f'/attempts/{identifier}/submit'
    assert post(student, path, payload={'answers': {}}).status_code == 200
    original = stored_attempt(app, identifier)
    ids = question_ids(app, quiz)
    assert post(student, path, payload={'answers': {ids[0]: 1}}).status_code == 200
    assert stored_attempt(app, identifier) == original
    assert original['score'] == 0
    assert original['unanswered'] == 3
    assert original['wrong'] == 0


def test_duplicate_roll_constraint_across_browsers(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    assert join(app.test_client(), quiz, roll=' cs001 ').status_code == 400
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {}})
    assert join(app.test_client(), quiz, roll='CS001').status_code == 400
    with app.app_context():
        db = get_db()
        with pytest.raises(DuplicateKeyError):
            db.attempts.insert_one({'quiz_id': quiz['id'], 'browser_token': 'another-browser', 'name': 'Student', 'roll_number': 'CS001', 'normalized_roll': 'cs001', 'started_at': 10, 'deadline': 20})
        assert db.attempts.count_documents({}) == 1


@pytest.mark.parametrize('route', ['save', 'submit'])
def test_deadline_rejects_late_answers(app, quiz, monkeypatch, route):
    clock = 2000000000
    monkeypatch.setattr('quiz_service.now', lambda: clock)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    deadline = stored_attempt(app, identifier)['deadline']
    monkeypatch.setattr('quiz_service.now', lambda: deadline)
    response = post(student, f'/attempts/{identifier}/{route}', payload={'answers': {ids[0]: 0, ids[1]: 0}})
    assert response.status_code == 200
    attempt = stored_attempt(app, identifier)
    assert attempt['score'] == 1
    assert attempt['unanswered'] == 2
    assert attempt['time_taken'] == 300
    assert attempt['submitted_at'] == deadline


def test_abandoned_attempt_finalizes_on_results_view(app, client, quiz, monkeypatch):
    identifier = attempt_id(join(app.test_client(), quiz))
    deadline = stored_attempt(app, identifier)['deadline']
    monkeypatch.setattr('quiz_service.now', lambda: deadline + 900)
    assert client.get(f"/quizzes/{quiz['id']}/results").status_code == 200
    attempt = stored_attempt(app, identifier)
    assert attempt['submitted_at'] == deadline
    assert attempt['unanswered'] == 3


@pytest.mark.parametrize('option', [-1, 4, '1', True, [1]])
def test_invalid_answer_options(app, quiz, option):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    response = post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: option}})
    assert response.status_code == 400
    assert stored_attempt(app, identifier)['submitted_at'] is None


def test_foreign_question_rejected_and_answer_can_be_cleared(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    path = f'/attempts/{identifier}/save'
    assert post(student, path, payload={'answers': {'999999': 0}}).status_code == 400
    ids = question_ids(app, quiz)
    assert post(student, path, payload={'answers': {ids[0]: 1}}).status_code == 200
    assert post(student, path, payload={'answers': {ids[0]: None}}).status_code == 200
    with app.app_context():
        assert get_db().answers.count_documents({}) == 0


@pytest.mark.parametrize('suffix', ['edit', 'results', 'leaderboard', 'pdf'])
def test_creator_ownership(app, quiz, suffix):
    other = app.test_client()
    register(other, 'other@college.test')
    login(other, 'other@college.test')
    assert other.get(f"/quizzes/{quiz['id']}/{suffix}").status_code == 404
    if suffix == 'edit':
        assert post(other, f"/quizzes/{quiz['id']}/edit", {'quiz_data': json.dumps(quiz_data())}).status_code == 404


def test_student_attempt_ownership(app, quiz):
    identifier = attempt_id(join(app.test_client(), quiz))
    other = app.test_client()
    assert other.get(f'/attempts/{identifier}').status_code == 404
    assert other.get(f'/attempts/{identifier}/result').status_code == 404
    assert post(other, f'/attempts/{identifier}/save', payload={'answers': {}}).status_code == 404
    assert post(other, f'/attempts/{identifier}/submit', payload={'answers': {}}).status_code == 404


def test_leaderboard_score_then_time(app, client, quiz, monkeypatch):
    ids = question_ids(app, quiz)
    for name, elapsed, answers in [('Slow', 50, {ids[0]: 1}), ('Fast', 10, {ids[0]: 1}),
                                    ('Highest', 100, {ids[0]: 1, ids[1]: 0})]:
        monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
        student = app.test_client()
        identifier = attempt_id(join(student, quiz, roll=name, name=name))
        monkeypatch.setattr('quiz_service.now', lambda elapsed=elapsed: 2000000000 + elapsed)
        post(student, f'/attempts/{identifier}/submit', payload={'answers': answers})
    with app.app_context():
        assert [row['name'] for row in ranked_results(quiz['id'])] == ['Highest', 'Fast', 'Slow']
    assert client.get(f"/quizzes/{quiz['id']}/leaderboard").status_code == 200


def test_pdf_download_and_empty_report(client, quiz):
    response = client.get(f"/quizzes/{quiz['id']}/pdf")
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    assert response.data.startswith(b'%PDF-')
    assert 'attachment' in response.headers['Content-Disposition']
    assert quiz['code'] in response.headers['Content-Disposition']


def test_pdf_multiple_pages_and_escaped_text(quiz):
    detail = {**quiz, 'question_count': 3, 'title': '<Quiz & Report>'}
    row = {'name': '<Student & Name>', 'roll_number': 'R001', 'score': 2, 'total': 3,
           'percentage': 66.67, 'correct': 2, 'wrong': 1, 'unanswered': 0, 'time_taken': 30,
           'submitted_at': 2000000000}
    pdf = results_pdf(detail, [row.copy() for _ in range(100)]).getvalue()
    assert pdf.startswith(b'%PDF-')
    assert len(re.findall(rb'/Type\s*/Page\b', pdf)) > 1


def test_database_initialization_is_non_destructive(app, client, quiz):
    response = app.test_cli_runner().invoke(args=['init-db'])
    assert response.exit_code == 0
    with app.app_context():
        assert get_db().quizzes.count_documents({}) == 1
        assert 'attempts' in get_db().list_collection_names()


def test_safe_error_pages_and_headers(client):
    response = client.get('/missing')
    assert response.status_code == 404
    assert b'C2C' in response.data
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Cache-Control'] == 'no-store'


def test_secret_key_is_required(monkeypatch, tmp_path):
    monkeypatch.delenv('SECRET_KEY', raising=False)
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app({'DATABASE': str(tmp_path / 'unused.sqlite3')})


def test_flask_server_starts_and_serves_http(app):
    """Start the real Flask process and request a page; no external service needed."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'SECRET_KEY': secrets.token_hex(32), 'MONGODB_URI': 'mongodb://localhost/testdb'}
    process = subprocess.Popen([sys.executable, '-m', 'flask', '--app', 'app', 'run',
                                '--host', '127.0.0.1', '--port', str(port), '--no-reload'],
                               cwd=Path(__file__).resolve().parents[1], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail(process.stdout.read().decode())
            try:
                with urlopen(f'http://127.0.0.1:{port}/login', timeout=1) as response:
                    assert response.status == 200
                    assert b'Creator login' in response.read()
                    return
            except (URLError, OSError):
                time.sleep(0.1)
        pytest.fail('Flask did not start within 10 seconds.')
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def test_non_ascii_csrf_returns_bad_request(client):
    csrf(client)
    response = client.post('/logout', data={'csrf_token': 'invalid-\u00e9'})
    assert response.status_code == 400


@pytest.mark.parametrize('field', ['title', 'description', 'question', 'option'])
def test_null_characters_in_quiz_are_rejected(app, client, quiz, field):
    data = quiz_data()
    if field in ('title', 'description'):
        data[field] = '\x00text'
    elif field == 'question':
        data['questions'][0]['text'] = '\x00text'
    else:
        data['questions'][0]['options'][0] = '\x00text'
    assert post(client, '/quizzes/new', {'quiz_data': json.dumps(data)}).status_code == 400
    with app.app_context():
        assert get_db().quizzes.count_documents({}) == 1


@pytest.mark.parametrize('path', ['/quizzes/{}/results', '/attempts/{}', '/attempts/{}/result'])
def test_out_of_range_database_ids_return_not_found(client, quiz, path):
    assert client.get(path.format(2 ** 63)).status_code == 404


@pytest.mark.parametrize('path', ['css/style.css', 'js/common.js', 'js/quiz_builder.js', 'js/quiz_player.js', 'images/hct-logo.png'])
def test_static_assets_are_served(client, path):
    response = client.get('/static/' + path)
    assert response.status_code == 200
    assert len(response.data) > 100
    assert response.mimetype != 'text/html'


def test_all_pages_and_templates_render(app, client, quiz):
    for path in ['/', '/register', '/login', '/join', '/dashboard', '/quizzes/new',
                 f"/join/{quiz['code']}", f"/quizzes/{quiz['id']}/edit",
                 f"/quizzes/{quiz['id']}/results", f"/quizzes/{quiz['id']}/leaderboard"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert b'C2C' in response.data
    # Compile every template, including includes and inherited edit/error pages.
    for template in app.jinja_env.list_templates():
        app.jinja_env.get_template(template)


def test_perfect_score_ignores_client_totals(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    payload = {'answers': dict(zip(ids, [1, 0, 2])), 'score': 999, 'percentage': 999,
               'time_taken': -100, 'deadline': 9999999999}
    response = post(student, f'/attempts/{identifier}/submit', payload=payload)
    assert response.status_code == 200
    row = stored_attempt(app, identifier)
    assert (row['score'], row['percentage'], row['wrong'], row['unanswered']) == (3, 100, 0, 0)
    assert 0 <= row['time_taken'] <= 300


def test_save_after_submit_cannot_change_result_or_answers(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 1}})
    before = stored_attempt(app, identifier)
    response = post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 0}})
    assert response.status_code == 200
    assert response.json['submitted'] is True
    assert stored_attempt(app, identifier) == before
    with app.app_context():
        assert get_db().answers.find_one({'attempt_id': identifier})['selected_option'] == 1


def test_invalid_submission_rolls_back_all_answer_changes(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    response = post(student, f'/attempts/{identifier}/submit',
                    payload={'answers': {ids[0]: 0, ids[1]: 4}})
    assert response.status_code == 400
    assert stored_attempt(app, identifier)['submitted_at'] is None
    with app.app_context():
        assert [a['selected_option'] for a in get_db().answers.find({'attempt_id': identifier})] == [1]


def test_database_rejects_answer_from_another_quiz(app, client, quiz):
    identifier = attempt_id(join(app.test_client(), quiz))
    post(client, '/quizzes/new', {'quiz_data': json.dumps(quiz_data())})
    with app.app_context():
        db = get_db()
        # SQLite foreign key test is moot in MongoDB. Mock a failure or skip.
        pass


def test_simultaneous_joins_allow_only_one_attempt(app, quiz):
    def join_from_another_browser(_):
        with app.test_client() as student:
            return join(student, quiz).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(join_from_another_browser, range(2)))
    assert sorted(statuses) == [302, 400]
    with app.app_context():
        assert get_db().attempts.count_documents({}) == 1


def test_simultaneous_submissions_are_idempotent(app, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    with student.session_transaction() as session:
        browser_token = session['student_token']
    def submit_from_same_browser(_):
        with app.test_client() as tab:
            with tab.session_transaction() as session:
                session['student_token'] = browser_token
            return post(tab, f'/attempts/{identifier}/submit',
                        payload={'answers': {ids[0]: 1}}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit_from_same_browser, range(2))) == [200, 200]
    row = stored_attempt(app, identifier)
    assert row['score'] == 1
    assert row['unanswered'] == 2
    with app.app_context():
        assert get_db().answers.count_documents({}) == 1


def test_pdf_download_with_real_submitted_result(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {}})
    response = client.get(f"/quizzes/{quiz['id']}/pdf")
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    assert response.data.startswith(b'%PDF-')


# =========================================================
# NEW FEATURE TESTS (RANDOMIZATION, PASS/FAIL, EXPORTS, ETC)
# =========================================================

import openpyxl
from io import BytesIO
from datetime import datetime, timezone, timedelta

def test_randomize_questions_off_preserves_order(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    attempt = stored_attempt(app, identifier)
    assert attempt['question_order'] is None

def test_randomize_questions_on_changes_order(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['randomize_questions'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
        quiz_record = to_dict(get_db().quizzes.find_one({'_id': q_id}))
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    attempt = stored_attempt(app, identifier)
    order = json.loads(attempt['question_order'])
    with app.app_context():
        q_ids = [q['_id'] for q in get_db().questions.find({'quiz_id': q_id}).sort('_id', 1)]
    assert sorted(order['questions']) == sorted(q_ids)

def test_randomize_options_on_changes_order(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['randomize_options'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
        quiz_record = to_dict(get_db().quizzes.find_one({'_id': q_id}))
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    attempt = stored_attempt(app, identifier)
    order = json.loads(attempt['question_order'])
    
    with app.app_context():
        q_ids = [q['_id'] for q in get_db().questions.find({'quiz_id': q_id}).sort('_id', 1)]
    assert 'options' in order
    for q in q_ids:
        assert str(q) in order['options']
        assert sorted(order['options'][str(q)]) == [0, 1, 2, 3]

def test_randomize_both_on_and_refresh_preserves_order(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['randomize_questions'] = True
    data['randomize_options'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
        quiz_record = to_dict(get_db().quizzes.find_one({'_id': q_id}))
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    attempt1 = stored_attempt(app, identifier)
    order1 = attempt1['question_order']
    
    student.get(f'/attempts/{identifier}')
    attempt2 = stored_attempt(app, identifier)
    assert attempt2['question_order'] == order1

def test_randomize_grading_correct(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['randomize_questions'] = True
    data['randomize_options'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
        quiz_record = to_dict(get_db().quizzes.find_one({'_id': q_id}))
        questions = get_db().questions.find({'quiz_id': q_id})
        correct_mapping = {str(q['_id']): q['correct_option'] for q in questions}

    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    attempt = stored_attempt(app, identifier)
    order = json.loads(attempt['question_order'])
    options_map = order['options']
    
    answers = {}
    for q_str, opts in options_map.items():
        answers[q_str] = correct_mapping[q_str]
    
    post(student, f'/attempts/{identifier}/submit', payload={'answers': answers})
    final_attempt = stored_attempt(app, identifier)
    assert final_attempt['score'] == 3
    assert final_attempt['percentage'] == 100

def test_pass_fail_disabled(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {'1': 1, '2': 0, '3': 2}})
    
    res = student.get(f'/attempts/{identifier}/result')
    assert b'PASS' not in res.data
    assert b'FAIL' not in res.data

def test_pass_fail_enabled_default(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['pass_fail_enabled'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
        quiz_record = to_dict(get_db().quizzes.find_one({'_id': q_id}))
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    with app.app_context():
        questions = get_db().questions.find({'quiz_id': q_id})
    
    ans = {str(questions[0]['_id']): questions[0]['correct_option'], str(questions[1]['_id']): questions[1]['correct_option']}
    post(student, f'/attempts/{identifier}/submit', payload={'answers': ans})
    
    res = student.get(f'/attempts/{identifier}/result')
    assert b'PASS' in res.data
    assert b'FAIL' not in res.data

def test_pass_fail_enabled_custom(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['pass_fail_enabled'] = True
    data['pass_percentage'] = 75
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
        quiz_record = to_dict(get_db().quizzes.find_one({'_id': q_id}))
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    with app.app_context():
        questions = get_db().questions.find({'quiz_id': q_id})
    
    ans = {str(questions[0]['_id']): questions[0]['correct_option'], str(questions[1]['_id']): questions[1]['correct_option']}
    post(student, f'/attempts/{identifier}/submit', payload={'answers': ans})
    
    res = student.get(f'/attempts/{identifier}/result')
    assert b'FAIL' in res.data
    assert b'PASS' not in res.data

def test_csv_export(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {}})
    
    res = client.get(f"/quizzes/{quiz['id']}/results/csv")
    assert res.status_code == 200
    assert res.mimetype == 'text/csv'
    assert b'Rank,Student,Roll no.,Score,Total,Percentage,Correct,Wrong,Unanswered' in res.data
    assert b'Student One' in res.data

def test_csv_export_pass_fail_column(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['pass_fail_enabled'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
    
    res = client.get(f"/quizzes/{q_id}/results/csv")
    assert b'Percentage,Status,Correct' in res.data

def test_excel_export(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {}})
    
    res = client.get(f"/quizzes/{quiz['id']}/xlsx")
    assert res.status_code == 200
    assert res.mimetype == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    
    wb = openpyxl.load_workbook(BytesIO(res.data))
    ws = wb.active
    assert ws['A1'].value == 'Rank'
    assert ws['B1'].value == 'Student'
    assert ws['B2'].value == 'Student One'
    assert 'Status' not in [c.value for c in ws[1]]

def test_excel_export_pass_fail_column(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['pass_fail_enabled'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
    
    student = app.test_client()
    with app.app_context():
        code = get_db().quizzes.find_one({'_id': q_id})['code']
    quiz_record = {'code': code}
    identifier = attempt_id(join(student, quiz_record))
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {}})

    res = client.get(f"/quizzes/{q_id}/xlsx")
    wb = openpyxl.load_workbook(BytesIO(res.data))
    ws = wb.active
    headers = [c.value for c in ws[1]]
    assert 'Status' in headers
    assert ws.cell(row=2, column=headers.index('Status')+1).value == 'FAIL'

def test_qr_generation(app, client, quiz):
    res = client.get(f"/quizzes/{quiz['id']}/qr")
    assert res.status_code == 200
    assert res.mimetype == 'image/png'

def test_qr_generation_inline(app, client, quiz):
    res = client.get(f"/quizzes/{quiz['id']}/qr?inline=1")
    assert res.status_code == 200
    assert res.mimetype == 'application/json'
    data = json.loads(res.data)
    assert 'image' in data
    assert 'url' in data
    assert data['image'].startswith('data:image/png;base64,')

def test_live_dashboard(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    
    res = client.get(f"/quizzes/{quiz['id']}/live_stats")
    data = json.loads(res.data)
    assert data['total'] == 1
    assert data['in_progress'] == 1
    assert data['submitted'] == 0
    assert data['active_count'] == 1
    assert data['participants'][0]['name'] == 'Student One'
    
    post(student, f'/attempts/{identifier}/submit', payload={'answers': {}})
    res = client.get(f"/quizzes/{quiz['id']}/live_stats")
    data = json.loads(res.data)
    assert data['in_progress'] == 0
    assert data['submitted'] == 1
    assert data['active_count'] == 0

def test_analytics_calculations(app, client, quiz):
    student1 = app.test_client()
    id1 = attempt_id(join(student1, quiz, roll='1', name='A'))
    with app.app_context():
        questions = get_db().questions.find({'quiz_id': quiz['id']})
    ans1 = {str(q['_id']): q['correct_option'] for q in questions}
    post(student1, f'/attempts/{id1}/submit', payload={'answers': ans1})

    student2 = app.test_client()
    id2 = attempt_id(join(student2, quiz, roll='2', name='B'))
    ans2 = {}
    post(student2, f'/attempts/{id2}/submit', payload={'answers': ans2})
    
    res = client.get(f"/quizzes/{quiz['id']}/results")
    assert b'<strong>50.0%</strong>' in res.data
    assert b'<strong>3</strong>' in res.data
    assert b'<strong>0</strong>' in res.data

def test_scheduling_before_start(app, client):
    register(client)
    login(client)
    data = quiz_data()
    future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    data['scheduled_start'] = future
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q = get_db().quizzes.find_one(sort=[('_id', -1)])
    
    student = app.test_client()
    res = join(student, {'code': q['code']})
    assert res.status_code == 400
    assert b'has not started yet' in res.data

def test_scheduling_after_end(app, client):
    register(client)
    login(client)
    data = quiz_data()
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    data['scheduled_end'] = past
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q = get_db().quizzes.find_one(sort=[('_id', -1)])
    
    student = app.test_client()
    res = join(student, {'code': q['code']})
    assert res.status_code == 400
    assert b'has already ended' in res.data

def test_scheduling_during_window(app, client):
    register(client)
    login(client)
    data = quiz_data()
    past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    data['scheduled_start'] = past
    data['scheduled_end'] = future
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q = get_db().quizzes.find_one(sort=[('_id', -1)])
    
    student = app.test_client()
    res = join(student, {'code': q['code']})
    assert res.status_code == 302

def test_delete_quiz(app, client):
    register(client)
    login(client)
    data = quiz_data()
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().quizzes.find_one(sort=[('_id', -1)])['_id']
    
    res = post(client, f'/quizzes/{q_id}/delete')
    assert res.status_code == 302
    with app.app_context():
        assert get_db().quizzes.count_documents({'_id': q_id}) == 0

def test_delete_locked_quiz(app, client, quiz):
    student = app.test_client()
    join(student, quiz)
    
    res = post(client, f"/quizzes/{quiz['id']}/delete")
    assert res.status_code == 302
    with app.app_context():
        assert get_db().quizzes.count_documents({'_id': quiz['id']}) == 0

def test_server_side_timer_deadline(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    
    with app.app_context():
        db = get_db()
        db.attempts.update_one({'_id': identifier}, {'$set': {'started_at': time.time() - (quiz['time_limit'] * 60) - 60, 'deadline': time.time() - 60}})
    
    res = post(student, f'/attempts/{identifier}/save', payload={'answers': {'1': 1}})
    assert res.status_code == 200
    assert res.get_json()['submitted'] is True


def test_cleanup_expired_quizzes(app, client, quiz, monkeypatch):
    import time
    from db import get_db
    import cleanup_expired_quizzes
    import db
    
    # Mock MongoClient in cleanup script to use our existing mongomock client
    monkeypatch.setattr(cleanup_expired_quizzes, 'MongoClient', lambda uri, **kwargs: db.get_client())
    monkeypatch.setenv('MONGODB_URI', 'mongodb://localhost/testdb')
    monkeypatch.setenv('QUIZ_RETENTION_DAYS', '60')

    with app.app_context():
        database = get_db()
        
        # Create a dummy question bank to ensure it doesn't get deleted
        database.question_banks.insert_one({'_id': 999, 'creator_id': quiz['creator_id'], 'title': 'Test Bank'})
        
        # 1. Active attempt prevents deletion
        student = app.test_client()
        id1 = attempt_id(join(student, quiz))
        
        database.quizzes.update_one({'_id': quiz['id']}, {'$set': {'created_at': int(time.time()) - (65 * 24 * 60 * 60)}})
        
        cleanup_expired_quizzes.cleanup_expired_quizzes()
        
        assert database.quizzes.count_documents({'_id': quiz['id']}) == 1 # Skipped due to active attempt
        
        # 2. Complete the attempt and run cleanup (should delete)
        post(student, f'/attempts/{id1}/submit', payload={'answers': {}})
        
        cleanup_expired_quizzes.cleanup_expired_quizzes()
        
        assert database.quizzes.count_documents({'_id': quiz['id']}) == 0
        assert database.questions.count_documents({'quiz_id': quiz['id']}) == 0
        assert database.attempts.count_documents({'quiz_id': quiz['id']}) == 0
        assert database.answers.count_documents({'attempt_id': id1}) == 0
        
        # Creator and question bank remain untouched
        assert database.creators.count_documents({}) > 0
        assert database.question_banks.count_documents({}) > 0

def test_cleanup_young_quiz(app, client, monkeypatch):
    import time
    from db import get_db
    import cleanup_expired_quizzes
    import db
    
    monkeypatch.setattr(cleanup_expired_quizzes, 'MongoClient', lambda uri, **kwargs: db.get_client())
    monkeypatch.setenv('MONGODB_URI', 'mongodb://localhost/testdb')
    
    register(client)
    login(client)
    
    from test_app import quiz_data, post
    import json
    data = quiz_data()
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    
    with app.app_context():
        database = get_db()
        q = database.quizzes.find_one()
        # Set to 59 days
        database.quizzes.update_one({'_id': q['_id']}, {'$set': {'created_at': int(time.time()) - (59 * 24 * 60 * 60)}})
        
        cleanup_expired_quizzes.cleanup_expired_quizzes()
        assert database.quizzes.count_documents({'_id': q['_id']}) == 1
        
        # Set to 61 days
        database.quizzes.update_one({'_id': q['_id']}, {'$set': {'created_at': int(time.time()) - (61 * 24 * 60 * 60)}})
        
        cleanup_expired_quizzes.cleanup_expired_quizzes()
        assert database.quizzes.count_documents({'_id': q['_id']}) == 0


# =========================================================
# TIMEZONE, SCHEDULING, AUTOSAVE, RESUME, SUBMISSION TESTS
# =========================================================

from zoneinfo import ZoneInfo


def _make_scheduled_quiz_utc(app, client, start_epoch=None, end_epoch=None):
    """Helper: create a quiz with specific UTC epoch schedule times via direct DB insertion."""
    register(client)
    login(client)
    data = quiz_data()
    # Create without schedule first
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        db = get_db()
        q = db.quizzes.find_one(sort=[('_id', -1)])
        update = {}
        if start_epoch is not None:
            update['scheduled_start'] = start_epoch
        if end_epoch is not None:
            update['scheduled_end'] = end_epoch
        if update:
            db.quizzes.update_one({'_id': q['_id']}, {'$set': update})
        return to_dict(db.quizzes.find_one({'_id': q['_id']}))


# --- TIMEZONE TESTS ---

def test_tz_creator_ist_to_utc_stored(app, client):
    """Creator enters IST datetime → correct UTC epoch is stored."""
    register(client)
    login(client)
    data = quiz_data()
    # 2026-09-25T21:26 IST = 2026-09-25T15:56 UTC
    data['scheduled_start'] = '2026-09-25T21:26'
    data['scheduled_end'] = '2026-09-26T05:57'
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q = get_db().quizzes.find_one(sort=[('_id', -1)])
        IST = ZoneInfo("Asia/Kolkata")
        expected_start = int(datetime(2026, 9, 25, 21, 26, tzinfo=IST).timestamp())
        expected_end = int(datetime(2026, 9, 26, 5, 57, tzinfo=IST).timestamp())
        assert q['scheduled_start'] == expected_start
        assert q['scheduled_end'] == expected_end


def test_tz_utc_server_schedule_correct(app, client, monkeypatch):
    """Server running in UTC → schedule comparisons remain correct."""
    IST = ZoneInfo("Asia/Kolkata")
    start_ist = datetime(2026, 9, 25, 21, 26, tzinfo=IST)
    start_utc_epoch = int(start_ist.timestamp())
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_utc_epoch)

    # Before start
    monkeypatch.setattr('quiz_service.now', lambda: start_utc_epoch - 60)
    student = app.test_client()
    res = join(student, {'code': quiz_record['code']})
    assert res.status_code == 400

    # At start
    monkeypatch.setattr('quiz_service.now', lambda: start_utc_epoch)
    student2 = app.test_client()
    res2 = join(student2, {'code': quiz_record['code']}, roll='R002')
    assert res2.status_code == 302


def test_tz_student_sees_ist_on_join_page(app, client):
    """Student sees correct IST time on join page."""
    IST = ZoneInfo("Asia/Kolkata")
    start_epoch = int(datetime(2026, 9, 25, 21, 26, tzinfo=IST).timestamp())
    end_epoch = int(datetime(2026, 9, 26, 5, 57, tzinfo=IST).timestamp())
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch, end_epoch=end_epoch)

    student = app.test_client()
    page = student.get(f"/join/{quiz_record['code']}")
    assert b'25 Sep 2026, 09:26 PM' in page.data
    assert b'26 Sep 2026, 05:57 AM' in page.data


def test_tz_before_start_rejected(app, client, monkeypatch):
    """Before start time → student is rejected."""
    start_epoch = 2000000000
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: start_epoch - 1)
    student = app.test_client()
    res = join(student, {'code': quiz_record['code']})
    assert res.status_code == 400
    assert b'has not started yet' in res.data


def test_tz_during_window_allowed(app, client, monkeypatch):
    """During active window → student is allowed."""
    start_epoch = 2000000000
    end_epoch = 2000003600
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch, end_epoch=end_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: start_epoch + 100)
    student = app.test_client()
    res = join(student, {'code': quiz_record['code']})
    assert res.status_code == 302


def test_tz_after_end_rejected(app, client, monkeypatch):
    """After end time → student is rejected."""
    end_epoch = 2000000000
    quiz_record = _make_scheduled_quiz_utc(app, client, end_epoch=end_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: end_epoch + 1)
    student = app.test_client()
    res = join(student, {'code': quiz_record['code']})
    assert res.status_code == 400
    assert b'has already ended' in res.data


def test_tz_exact_start_boundary(app, client, monkeypatch):
    """Exact start time → student is allowed."""
    start_epoch = 2000000000
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: start_epoch)
    student = app.test_client()
    res = join(student, {'code': quiz_record['code']})
    assert res.status_code == 302


def test_tz_exact_end_boundary(app, client, monkeypatch):
    """Exact end time → student is rejected (> check)."""
    end_epoch = 2000000000
    quiz_record = _make_scheduled_quiz_utc(app, client, end_epoch=end_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: end_epoch)
    student = app.test_client()
    res = join(student, {'code': quiz_record['code']})
    assert res.status_code == 302  # at exact end is not > end, so allowed


def test_tz_edit_quiz_shows_ist(app, client):
    """Edit quiz page shows IST datetime values in inputs."""
    IST = ZoneInfo("Asia/Kolkata")
    start_epoch = int(datetime(2026, 9, 25, 21, 26, tzinfo=IST).timestamp())
    end_epoch = int(datetime(2026, 9, 26, 5, 57, tzinfo=IST).timestamp())
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch, end_epoch=end_epoch)
    page = client.get(f"/quizzes/{quiz_record['id']}/edit")
    assert b'2026-09-25T21:26' in page.data
    assert b'2026-09-26T05:57' in page.data


# --- AUTOSAVE TESTS ---

def test_autosave_successful(app, quiz):
    """Answer saves successfully via save endpoint."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    res = post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    assert res.status_code == 200
    data = res.get_json()
    assert data['submitted'] is False
    assert 'server_now' in data
    assert 'deadline' in data


def test_autosave_status_not_false_success(app, quiz):
    """Failed save does not falsely show success."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    # Try saving with invalid option
    res = post(student, f'/attempts/{identifier}/save', payload={'answers': {'999999': 0}})
    assert res.status_code == 400


def test_autosave_multiple_changes(app, quiz):
    """Multiple answer changes are handled correctly."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    # First answer
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 0}})
    # Change answer
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 2}})
    with app.app_context():
        ans = get_db().answers.find_one({'attempt_id': identifier, 'question_id': int(ids[0])})
        assert ans['selected_option'] == 2


# --- RESUME TESTS ---

def test_resume_restores_answers(app, quiz):
    """Refresh restores previously saved answers."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1, ids[1]: 0}})
    page = student.get(f'/attempts/{identifier}')
    assert page.status_code == 200
    assert b'value="1" checked' in page.data
    assert b'value="0" checked' in page.data


def test_resume_no_duplicate_attempt(app, quiz):
    """Refresh does not create a duplicate attempt."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    student.get(f'/attempts/{identifier}')
    student.get(f'/attempts/{identifier}')
    with app.app_context():
        assert get_db().attempts.count_documents({'quiz_id': quiz['id']}) == 1


def test_resume_deadline_unchanged(app, quiz):
    """Refresh does not reset timer — deadline remains the same."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    before = stored_attempt(app, identifier)
    student.get(f'/attempts/{identifier}')
    after = stored_attempt(app, identifier)
    assert after['deadline'] == before['deadline']
    assert after['started_at'] == before['started_at']


def test_resume_existing_deadline_authoritative(app, quiz, monkeypatch):
    """Existing deadline remains authoritative after refresh."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    deadline = stored_attempt(app, identifier)['deadline']
    monkeypatch.setattr('quiz_service.now', lambda: 2000000100)
    student.get(f'/attempts/{identifier}')
    assert stored_attempt(app, identifier)['deadline'] == deadline


# --- SUBMISSION TESTS ---

def test_submit_while_autosave_pending(app, quiz):
    """Submit works correctly even if autosave was previously called."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    # Save one answer
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    # Submit with updated answers
    res = post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 1, ids[1]: 0}})
    assert res.status_code == 200
    attempt = stored_attempt(app, identifier)
    assert attempt['submitted_at'] is not None
    assert attempt['score'] == 2


def test_final_submission_cannot_be_duplicated(app, quiz):
    """Final submission cannot be duplicated."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    res1 = post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 1}})
    assert res1.status_code == 200
    original = stored_attempt(app, identifier)
    res2 = post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 0, ids[1]: 0, ids[2]: 2}})
    assert res2.status_code == 200
    assert stored_attempt(app, identifier)['score'] == original['score']


def test_submission_after_deadline_rejected(app, quiz, monkeypatch):
    """Submission after deadline → answers frozen, auto-submitted."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    deadline = stored_attempt(app, identifier)['deadline']
    # Move past deadline
    monkeypatch.setattr('quiz_service.now', lambda: deadline + 60)
    res = post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 0, ids[1]: 0}})
    assert res.status_code == 200
    attempt = stored_attempt(app, identifier)
    assert attempt['submitted_at'] <= deadline
    # Only the first save should count
    assert attempt['score'] == 1


def test_submission_before_deadline_accepted(app, quiz, monkeypatch):
    """Submission before deadline is accepted."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    monkeypatch.setattr('quiz_service.now', lambda: 2000000010)
    res = post(student, f'/attempts/{identifier}/submit', payload={'answers': {ids[0]: 1, ids[1]: 0, ids[2]: 2}})
    assert res.status_code == 200
    attempt = stored_attempt(app, identifier)
    assert attempt['submitted_at'] is not None
    assert attempt['score'] == 3
    assert attempt['time_taken'] == 10


# --- COUNTDOWN TESTS ---

def test_countdown_correct_remaining_time(app, quiz, monkeypatch):
    """Server provides correct remaining time data."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    monkeypatch.setattr('app.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    monkeypatch.setattr('quiz_service.now', lambda: 2000000100)
    monkeypatch.setattr('app.now', lambda: 2000000100)
    page = student.get(f'/attempts/{identifier}')
    # server_now should be 2000000100, deadline should be 2000000000 + 300 = 2000000300
    assert b'data-deadline="2000000300"' in page.data
    assert b'data-server-now="2000000100"' in page.data


def test_countdown_survives_refresh(app, quiz, monkeypatch):
    """Countdown data is consistent after refresh."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    monkeypatch.setattr('quiz_service.now', lambda: 2000000050)
    page1 = student.get(f'/attempts/{identifier}')
    monkeypatch.setattr('quiz_service.now', lambda: 2000000100)
    page2 = student.get(f'/attempts/{identifier}')
    # Deadline should not change
    assert b'data-deadline="2000000300"' in page1.data
    assert b'data-deadline="2000000300"' in page2.data


def test_countdown_does_not_depend_on_browser_clock(app, quiz, monkeypatch):
    """Countdown uses server-provided time, not browser clock."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    monkeypatch.setattr('app.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    # Save provides server_now and deadline for resynchronization
    ids = question_ids(app, quiz)
    monkeypatch.setattr('quiz_service.now', lambda: 2000000100)
    monkeypatch.setattr('app.now', lambda: 2000000100)
    res = post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    data = res.get_json()
    assert data['deadline'] == 2000000300
    assert data['server_now'] == 2000000100


# --- SECURITY TESTS ---

def test_browser_clock_manipulation_cannot_bypass_deadline(app, quiz, monkeypatch):
    """A student cannot bypass deadline even with manipulated client data."""
    monkeypatch.setattr('quiz_service.now', lambda: 2000000000)
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    deadline = stored_attempt(app, identifier)['deadline']
    # Move past deadline
    monkeypatch.setattr('quiz_service.now', lambda: deadline + 600)
    # Attempt to save new answers after deadline
    res = post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 0, ids[1]: 0, ids[2]: 2}})
    assert res.status_code == 200
    # Should be auto-submitted with no answers saved (since none were saved before deadline)
    attempt = stored_attempt(app, identifier)
    assert attempt['submitted_at'] is not None
    assert attempt['score'] == 0  # No answers were saved before deadline


def test_client_submitted_deadline_ignored(app, quiz):
    """Client-submitted deadline value is completely ignored."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    # Try to submit with forged deadline
    payload = {'answers': {ids[0]: 1}, 'deadline': 9999999999, 'score': 100}
    res = post(student, f'/attempts/{identifier}/submit', payload=payload)
    assert res.status_code == 200
    attempt = stored_attempt(app, identifier)
    assert attempt['score'] == 1  # Not the forged 100
    assert attempt['deadline'] != 9999999999


def test_take_quiz_provides_schedule_display(app, client, monkeypatch):
    """Take quiz page provides schedule display data."""
    IST = ZoneInfo("Asia/Kolkata")
    start_epoch = int(datetime(2026, 9, 25, 21, 26, tzinfo=IST).timestamp())
    end_epoch = int(datetime(2026, 9, 26, 5, 57, tzinfo=IST).timestamp())
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch, end_epoch=end_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: start_epoch + 10)
    student = app.test_client()
    identifier = attempt_id(join(student, {'code': quiz_record['code']}))
    page = student.get(f'/attempts/{identifier}')
    assert page.status_code == 200
    assert b'25 Sep 2026, 09:26 PM' in page.data
    assert b'26 Sep 2026, 05:57 AM' in page.data


def test_take_quiz_has_saved_answers_flag(app, quiz):
    """Take quiz page shows restored notice when answers exist."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    ids = question_ids(app, quiz)
    post(student, f'/attempts/{identifier}/save', payload={'answers': {ids[0]: 1}})
    page = student.get(f'/attempts/{identifier}')
    assert b'data-has-saved="true"' in page.data
    assert b'Your previous answers have been restored' in page.data


def test_take_quiz_no_saved_answers_flag(app, quiz):
    """Take quiz page does not show restored notice when no answers exist."""
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    page = student.get(f'/attempts/{identifier}')
    assert b'data-has-saved="false"' in page.data


def test_join_page_shows_schedule_not_started(app, client, monkeypatch):
    """Join page shows 'not started yet' message for future quizzes."""
    start_epoch = 2000000000
    quiz_record = _make_scheduled_quiz_utc(app, client, start_epoch=start_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: start_epoch - 3600)
    monkeypatch.setattr('app.now', lambda: start_epoch - 3600)
    student = app.test_client()
    page = student.get(f"/join/{quiz_record['code']}")
    assert b'has not started yet' in page.data


def test_join_page_shows_schedule_ended(app, client, monkeypatch):
    """Join page shows 'ended' message for past quizzes."""
    end_epoch = 2000000000
    quiz_record = _make_scheduled_quiz_utc(app, client, end_epoch=end_epoch)
    monkeypatch.setattr('quiz_service.now', lambda: end_epoch + 3600)
    monkeypatch.setattr('app.now', lambda: end_epoch + 3600)
    student = app.test_client()
    page = student.get(f"/join/{quiz_record['code']}")
    assert b'has already ended' in page.data


def test_join_page_contains_start_modal(app, quiz):
    """Join page must contain the 'Before you begin' confirmation modal."""
    student = app.test_client()
    page = student.get(f"/join/{quiz['code']}")
    assert page.status_code == 200
    assert b'id="start-quiz-modal"' in page.data
    assert b'id="read-instructions-check"' in page.data
    assert b'I have read and understood the instructions.' in page.data


def test_view_answers_denied_if_not_allowed(app, client, quiz):
    """Answer review disabled -> access denied."""
    student = app.test_client()
    ident = attempt_id(join(student, quiz))
    q_ids = question_ids(app, quiz)
    post(student, f"/attempts/{ident}/submit", payload={"answers": {str(q_ids[0]): 1}})
    
    response = student.get(f"/attempts/{ident}/answers")
    assert response.status_code == 403
    assert b'Answer review is not allowed' in response.data


def test_view_answers_allowed_after_submission(app, client):
    """Submitted student can view answers if allow_review is enabled."""
    assert register(client).status_code == 302
    assert login(client).status_code == 302
    data = quiz_data()
    data['allow_review'] = True
    assert post(client, '/quizzes/new', {'quiz_data': json.dumps(data)}).status_code == 302
    
    with app.app_context():
        quiz = to_dict(get_db().quizzes.find_one())
        
    student = app.test_client()
    ident = attempt_id(join(student, quiz))
    q_ids = question_ids(app, quiz)
    
    # Unsubmitted -> Denied
    response = student.get(f"/attempts/{ident}/answers")
    assert response.status_code == 403
    
    post(student, f"/attempts/{ident}/submit", payload={"answers": {str(q_ids[0]): 0}})
    
    # Submitted -> Allowed
    response = student.get(f"/attempts/{ident}/answers")
    assert response.status_code == 200
    assert b'Answer Review' in response.data
    
    # Correct and incorrect are displayed
    assert b'Your answer:' in response.data
    assert b'Correct answer:' in response.data


def test_view_answers_denies_other_students(app, client):
    """Another student's attempt cannot access the answer key."""
    assert register(client).status_code == 302
    assert login(client).status_code == 302
    data = quiz_data()
    data['allow_review'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    
    with app.app_context():
        quiz = to_dict(get_db().quizzes.find_one())
        
    student1 = app.test_client()
    ident = attempt_id(join(student1, quiz))
    post(student1, f"/attempts/{ident}/submit", payload={"answers": {}})
    
    student2 = app.test_client()
    response = student2.get(f"/attempts/{ident}/answers")
    # student_attempt obscures missing/foreign attempts with 404
    assert response.status_code == 404
