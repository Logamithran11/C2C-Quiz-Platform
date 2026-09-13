"""Run with python -m pytest. All data is isolated in pytest temporary directories."""
import json
import os
import re
import secrets
import socket
import sqlite3
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
from db import get_db, init_db
from quiz_service import normalize_roll, ranked_results
from reports import results_pdf


@pytest.fixture
def app(tmp_path):
    application = create_app({'TESTING': True, 'SECRET_KEY': secrets.token_hex(32),
                              'DATABASE': str(tmp_path / 'test.sqlite3')})
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
        return dict(get_db().execute('SELECT * FROM quizzes').fetchone())


def join(client, quiz, roll='CS 001', name='Student One'):
    return post(client, f"/join/{quiz['code']}", {'name': name, 'roll_number': roll})


def attempt_id(response):
    return int(response.headers['Location'].rstrip('/').split('/')[-1])


def question_ids(app, quiz):
    with app.app_context():
        return [str(row['id']) for row in get_db().execute(
            'SELECT id FROM questions WHERE quiz_id = ? ORDER BY position', (quiz['id'],))]


def stored_attempt(app, identifier):
    with app.app_context():
        return dict(get_db().execute('SELECT * FROM attempts WHERE id = ?', (identifier,)).fetchone())


def test_registration_hash_and_duplicate_email(app, client):
    assert register(client).status_code == 302
    with app.app_context():
        row = get_db().execute('SELECT * FROM creators').fetchone()
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
        codes = [row[0] for row in get_db().execute('SELECT code FROM quizzes')]
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
        assert get_db().execute('SELECT count(*) FROM quizzes').fetchone()[0] == 1


def test_edit_before_start_and_lock_after_start(app, client, quiz):
    path = f"/quizzes/{quiz['id']}/edit"
    assert client.get(path).status_code == 200
    data = quiz_data()
    data['title'] = 'Updated title'
    assert post(client, path, {'quiz_data': json.dumps(data)}).status_code == 302
    with app.app_context():
        assert get_db().execute('SELECT title FROM quizzes').fetchone()[0] == 'Updated title'
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
        with pytest.raises(sqlite3.IntegrityError), db:
            db.execute('INSERT INTO attempts (quiz_id,browser_token,name,roll_number,normalized_roll,started_at,deadline) '
                       'VALUES (?,?,?,?,?,?,?)', (quiz['id'], 'another-browser', 'Student', 'CS001', 'cs001', 10, 20))
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


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
        assert get_db().execute('SELECT count(*) FROM answers').fetchone()[0] == 0


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
        assert get_db().execute('SELECT count(*) FROM quizzes').fetchone()[0] == 1
        assert get_db().execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert get_db().execute('PRAGMA foreign_key_check').fetchall() == []


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
    env = {**os.environ, 'SECRET_KEY': secrets.token_hex(32), 'DATABASE_PATH': app.config['DATABASE']}
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
        assert get_db().execute('SELECT count(*) FROM quizzes').fetchone()[0] == 1


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
        assert get_db().execute('SELECT selected_option FROM answers WHERE attempt_id = ?',
                                (identifier,)).fetchone()[0] == 1


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
        rows = get_db().execute('SELECT selected_option FROM answers WHERE attempt_id = ?',
                                (identifier,)).fetchall()
        assert [row[0] for row in rows] == [1]


def test_database_rejects_answer_from_another_quiz(app, client, quiz):
    identifier = attempt_id(join(app.test_client(), quiz))
    post(client, '/quizzes/new', {'quiz_data': json.dumps(quiz_data())})
    with app.app_context():
        db = get_db()
        foreign_question = db.execute('SELECT id FROM questions WHERE quiz_id != ?',
                                      (quiz['id'],)).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError), db:
            db.execute('INSERT INTO answers (attempt_id, quiz_id, question_id, selected_option) '
                       'VALUES (?, ?, ?, ?)', (identifier, quiz['id'], foreign_question, 0))
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_simultaneous_joins_allow_only_one_attempt(app, quiz):
    def join_from_another_browser(_):
        with app.test_client() as student:
            return join(student, quiz).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(join_from_another_browser, range(2)))
    assert sorted(statuses) == [302, 400]
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM attempts').fetchone()[0] == 1


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
        assert get_db().execute('SELECT count(*) FROM answers').fetchone()[0] == 1


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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
        quiz_record = dict(get_db().execute('SELECT * FROM quizzes WHERE id = ?', (q_id,)).fetchone())
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    attempt = stored_attempt(app, identifier)
    order = json.loads(attempt['question_order'])
    with app.app_context():
        q_ids = [r[0] for r in get_db().execute('SELECT id FROM questions WHERE quiz_id = ? ORDER BY id', (q_id,)).fetchall()]
    assert sorted(order['questions']) == sorted(q_ids)

def test_randomize_options_on_changes_order(app, client):
    register(client)
    login(client)
    data = quiz_data()
    data['randomize_options'] = True
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
        quiz_record = dict(get_db().execute('SELECT * FROM quizzes WHERE id = ?', (q_id,)).fetchone())
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    attempt = stored_attempt(app, identifier)
    order = json.loads(attempt['question_order'])
    
    with app.app_context():
        q_ids = [r[0] for r in get_db().execute('SELECT id FROM questions WHERE quiz_id = ? ORDER BY id', (q_id,)).fetchall()]
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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
        quiz_record = dict(get_db().execute('SELECT * FROM quizzes WHERE id = ?', (q_id,)).fetchone())
    
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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
        quiz_record = dict(get_db().execute('SELECT * FROM quizzes WHERE id = ?', (q_id,)).fetchone())
        questions = get_db().execute('SELECT id, correct_option FROM questions WHERE quiz_id = ?', (q_id,)).fetchall()
        correct_mapping = {str(q['id']): q['correct_option'] for q in questions}

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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
        quiz_record = dict(get_db().execute('SELECT * FROM quizzes WHERE id = ?', (q_id,)).fetchone())
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    with app.app_context():
        questions = get_db().execute('SELECT id, correct_option FROM questions WHERE quiz_id = ?', (q_id,)).fetchall()
    
    ans = {str(questions[0]['id']): questions[0]['correct_option'], str(questions[1]['id']): questions[1]['correct_option']}
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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
        quiz_record = dict(get_db().execute('SELECT * FROM quizzes WHERE id = ?', (q_id,)).fetchone())
    
    student = app.test_client()
    identifier = attempt_id(join(student, quiz_record))
    with app.app_context():
        questions = get_db().execute('SELECT id, correct_option FROM questions WHERE quiz_id = ?', (q_id,)).fetchall()
    
    ans = {str(questions[0]['id']): questions[0]['correct_option'], str(questions[1]['id']): questions[1]['correct_option']}
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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
    
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
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
    
    student = app.test_client()
    with app.app_context():
        code = get_db().execute('SELECT code FROM quizzes WHERE id = ?', (q_id,)).fetchone()[0]
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
        questions = get_db().execute('SELECT id, correct_option FROM questions WHERE quiz_id = ?', (quiz['id'],)).fetchall()
    ans1 = {str(q['id']): q['correct_option'] for q in questions}
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
        q = get_db().execute('SELECT code FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()
    
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
        q = get_db().execute('SELECT code FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()
    
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
        q = get_db().execute('SELECT code FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()
    
    student = app.test_client()
    res = join(student, {'code': q['code']})
    assert res.status_code == 302

def test_delete_quiz(app, client):
    register(client)
    login(client)
    data = quiz_data()
    post(client, '/quizzes/new', {'quiz_data': json.dumps(data)})
    with app.app_context():
        q_id = get_db().execute('SELECT id FROM quizzes ORDER BY id DESC LIMIT 1').fetchone()[0]
    
    res = post(client, f'/quizzes/{q_id}/delete')
    assert res.status_code == 302
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM quizzes WHERE id = ?', (q_id,)).fetchone()[0] == 0

def test_delete_locked_quiz(app, client, quiz):
    student = app.test_client()
    join(student, quiz)
    
    res = post(client, f"/quizzes/{quiz['id']}/delete")
    assert res.status_code == 302
    with app.app_context():
        assert get_db().execute('SELECT count(*) FROM quizzes WHERE id = ?', (quiz['id'],)).fetchone()[0] == 0

def test_server_side_timer_deadline(app, client, quiz):
    student = app.test_client()
    identifier = attempt_id(join(student, quiz))
    
    with app.app_context():
        db = get_db()
        db.execute('UPDATE attempts SET started_at = ?, deadline = ? WHERE id = ?', 
                   (time.time() - (quiz['time_limit'] * 60) - 60, time.time() - 60, identifier))
        db.commit()
    
    res = post(student, f'/attempts/{identifier}/save', payload={'answers': {'1': 1}})
    assert res.status_code == 200
    assert res.get_json()['submitted'] is True

