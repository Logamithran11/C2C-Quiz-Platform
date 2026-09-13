CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS creators (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
    email CITEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_banks (
    id SERIAL PRIMARY KEY,
    creator_id INTEGER NOT NULL REFERENCES creators(id),
    name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 150),
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS quizzes (
    id SERIAL PRIMARY KEY,
    creator_id INTEGER NOT NULL REFERENCES creators(id),
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 150),
    description TEXT NOT NULL DEFAULT '',
    instructions TEXT DEFAULT '',
    time_limit INTEGER NOT NULL CHECK(time_limit BETWEEN 1 AND 1440),
    pass_fail_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    pass_percentage INTEGER NOT NULL DEFAULT 50 CHECK(pass_percentage BETWEEN 1 AND 100),
    randomize_questions BOOLEAN NOT NULL DEFAULT FALSE,
    randomize_options BOOLEAN NOT NULL DEFAULT FALSE,
    scheduled_start BIGINT,
    scheduled_end BIGINT,
    bank_id INTEGER REFERENCES question_banks(id),
    bank_question_count INTEGER,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS quizzes_creator ON quizzes(creator_id);



CREATE TABLE IF NOT EXISTS bank_questions (
    id SERIAL PRIMARY KEY,
    bank_id INTEGER NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    text TEXT NOT NULL CHECK(length(text) > 0),
    option_0 TEXT NOT NULL CHECK(length(option_0) > 0),
    option_1 TEXT NOT NULL CHECK(length(option_1) > 0),
    option_2 TEXT NOT NULL CHECK(length(option_2) > 0),
    option_3 TEXT NOT NULL CHECK(length(option_3) > 0),
    correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3),
    UNIQUE(bank_id, position)
);

CREATE TABLE IF NOT EXISTS questions (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK(position >= 0),
    text TEXT NOT NULL CHECK(length(text) > 0),
    option_0 TEXT NOT NULL CHECK(length(option_0) > 0),
    option_1 TEXT NOT NULL CHECK(length(option_1) > 0),
    option_2 TEXT NOT NULL CHECK(length(option_2) > 0),
    option_3 TEXT NOT NULL CHECK(length(option_3) > 0),
    correct_option INTEGER NOT NULL CHECK(correct_option BETWEEN 0 AND 3),
    UNIQUE(quiz_id, position),
    UNIQUE(id, quiz_id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id SERIAL PRIMARY KEY,
    quiz_id INTEGER NOT NULL REFERENCES quizzes(id),
    browser_token TEXT NOT NULL,
    name TEXT NOT NULL,
    roll_number TEXT NOT NULL,
    normalized_roll TEXT NOT NULL,
    started_at BIGINT NOT NULL,
    deadline BIGINT NOT NULL CHECK(deadline > started_at),
    submitted_at BIGINT,
    question_order TEXT,
    total INTEGER,
    correct INTEGER,
    wrong INTEGER,
    unanswered INTEGER,
    score INTEGER,
    percentage REAL,
    time_taken INTEGER,
    UNIQUE(quiz_id, normalized_roll),
    UNIQUE(id, quiz_id),
    CHECK(submitted_at IS NULL OR (
        total > 0 AND correct >= 0 AND wrong >= 0 AND unanswered >= 0
        AND total = correct + wrong + unanswered AND score = correct
        AND percentage BETWEEN 0 AND 100 AND time_taken >= 0
    ))
);
CREATE INDEX IF NOT EXISTS attempts_ranking ON attempts(quiz_id, score DESC, time_taken);
CREATE INDEX IF NOT EXISTS attempts_deadline ON attempts(quiz_id, submitted_at, deadline);

CREATE TABLE IF NOT EXISTS answers (
    attempt_id INTEGER NOT NULL,
    quiz_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    selected_option INTEGER NOT NULL CHECK(selected_option BETWEEN 0 AND 3),
    PRIMARY KEY(attempt_id, question_id),
    FOREIGN KEY(attempt_id, quiz_id) REFERENCES attempts(id, quiz_id) ON DELETE CASCADE,
    FOREIGN KEY(question_id, quiz_id) REFERENCES questions(id, quiz_id)
);
