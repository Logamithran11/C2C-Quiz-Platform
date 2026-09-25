import pytest
import json
import secrets
from db import get_db, init_db
from app import create_app

@pytest.fixture
def app(monkeypatch):
    import mongomock
    mock_client = mongomock.MongoClient('mongodb://localhost/testdb')
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

def post_with_csrf(client, path, data=None, json=None):
    token = csrf(client)
    if json is not None:
        return client.post(path, json=json, headers={'X-CSRF-Token': token})
    if data is None:
        data = {}
    data['csrf_token'] = token
    return client.post(path, data=data)

def register(client, email='user@college.test', name='User', password='Password123!', role_attempt=None):
    data = {'name': name, 'email': email, 'password': password, 'confirm_password': password}
    if role_attempt:
        data['role'] = role_attempt
    return post_with_csrf(client, '/register', data=data)

def login(client, email='user@college.test', password='Password123!'):
    return post_with_csrf(client, '/login', data={'email': email, 'password': password})

def create_admin(app, email='admin@college.test', password='Password123!'):
    with app.app_context():
        from werkzeug.security import generate_password_hash
        from db import get_next_sequence_value
        from quiz_service import now
        db = get_db()
        db.creators.insert_one({
            '_id': get_next_sequence_value('creators'),
            'name': 'Admin User',
            'email': email,
            'password_hash': generate_password_hash(password),
            'role': 'admin',
            'created_at': now()
        })

def test_unauth_no_admin(client):
    assert client.get('/admin').status_code == 403
    assert client.get('/admin/results').status_code == 403
    assert client.get('/admin/manage').status_code == 403

def test_creator_no_admin(app, client):
    register(client, 'creator@college.test')
    login(client, 'creator@college.test')
    assert client.get('/admin').status_code == 403
    assert client.get('/admin/results').status_code == 403
    assert client.get('/admin/manage').status_code == 403
    assert post_with_csrf(client, '/admin/manage', data={'name': 'A', 'email': 'a@a.com', 'password': 'Password123!', 'confirm_password': 'Password123!'}).status_code == 403

def test_student_no_admin(app, client):
    # Student is inherently unauthenticated in creator context
    assert client.get('/admin').status_code == 403
    
def test_admin_access(app, client):
    create_admin(app, 'admin@college.test')
    login(client, 'admin@college.test')
    assert client.get('/admin').status_code == 200
    assert client.get('/admin/results').status_code == 200
    assert client.get('/admin/manage').status_code == 200

def test_admin_can_create_admin(app, client):
    create_admin(app, 'admin1@college.test')
    login(client, 'admin1@college.test')
    resp = post_with_csrf(client, '/admin/manage', data={
        'name': 'Admin 2',
        'email': 'admin2@college.test',
        'password': 'Password123!',
        'confirm_password': 'Password123!'
    })
    assert resp.status_code == 302
    
    with app.app_context():
        admin2 = get_db().creators.find_one({'email': 'admin2@college.test'})
        assert admin2['role'] == 'admin'
        
    client.post('/logout')
    
    # Newly created admin can login
    login(client, 'admin2@college.test')
    assert client.get('/admin').status_code == 200
    
    # Newly created admin can create another admin
    resp = post_with_csrf(client, '/admin/manage', data={
        'name': 'Admin 3',
        'email': 'admin3@college.test',
        'password': 'Password123!',
        'confirm_password': 'Password123!'
    })
    assert resp.status_code == 302
    with app.app_context():
        assert get_db().creators.find_one({'email': 'admin3@college.test'})['role'] == 'admin'

def test_duplicate_admin_email_rejected(app, client):
    create_admin(app, 'admin@college.test')
    login(client, 'admin@college.test')
    post_with_csrf(client, '/admin/manage', data={
        'name': 'Admin 2',
        'email': 'admin2@college.test',
        'password': 'Password123!',
        'confirm_password': 'Password123!'
    })
    
    # Try duplicate
    resp = post_with_csrf(client, '/admin/manage', data={
        'name': 'Admin 2 Duplicate',
        'email': 'admin2@college.test',
        'password': 'Password123!',
        'confirm_password': 'Password123!'
    })
    assert b'An account with that email already exists.' in resp.data

def test_public_registration_privilege_escalation(app, client):
    # Try to register as admin from public endpoint
    register(client, 'sneaky@college.test', role_attempt='admin')
    
    with app.app_context():
        user = get_db().creators.find_one({'email': 'sneaky@college.test'})
        assert user['role'] == 'creator' # Forced to creator

def test_analytics_correctness(app, client):
    # Create creator, quiz, attempts
    register(client, 'creator1@college.test')
    login(client, 'creator1@college.test')
    
    quiz_data = {
        'title': 'Test Quiz',
        'description': '',
        'time_limit': 10,
        'questions': [
            {'text': 'Q1', 'options': ['A', 'B', 'C', 'D'], 'correct_option': 0},
            {'text': 'Q2', 'options': ['A', 'B', 'C', 'D'], 'correct_option': 1},
        ]
    }
    post_with_csrf(client, '/quizzes/new', data={'quiz_data': json.dumps(quiz_data)})
    
    with app.app_context():
        quiz = get_db().quizzes.find_one()
        q_docs = list(get_db().questions.find().sort('position', 1))
        q1_id = q_docs[0]['_id']
        q2_id = q_docs[1]['_id']
        
    # Attempt 1 (1 correct, 1 incorrect)
    s1 = app.test_client()
    post_with_csrf(s1, '/join', data={'code': quiz['code'], 'name': 'S1', 'roll_number': '1'})
    with app.app_context():
        a1 = get_db().attempts.find_one({'name': 'S1'})
    post_with_csrf(s1, f'/attempts/{a1["_id"]}/submit', json={'answers': {str(q1_id): 0, str(q2_id): 2}})
    
    # Attempt 2 (1 incorrect, 1 unanswered)
    s2 = app.test_client()
    post_with_csrf(s2, '/join', data={'code': quiz['code'], 'name': 'S2', 'roll_number': '2'})
    with app.app_context():
        a2 = get_db().attempts.find_one({'name': 'S2'})
    post_with_csrf(s2, f'/attempts/{a2["_id"]}/submit', json={'answers': {str(q1_id): 1}})
    
    # Admin checks analytics
    create_admin(app, 'admin@college.test')
    login(client, 'admin@college.test')
    
    resp = client.get(f'/quizzes/{quiz["_id"]}/results')
    assert resp.status_code == 200
    
    html = resp.data.decode('utf-8')
    assert '<td>2</td><td style="color:var(--teal);">1</td><td style="color:var(--danger);">1</td><td>0</td><td>50.0%</td>' in html
    assert '<td>2</td><td style="color:var(--teal);">0</td><td style="color:var(--danger);">1</td><td>1</td><td>0.0%</td>' in html

def test_admin_views_all_results(app, client):
    create_admin(app, 'admin@college.test')
    login(client, 'admin@college.test')
    resp = client.get('/admin/results')
    assert resp.status_code == 200
    
def test_admin_views_all_quizzes(app, client):
    create_admin(app, 'admin@college.test')
    login(client, 'admin@college.test')
    resp = client.get('/admin')
    assert resp.status_code == 200
