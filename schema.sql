-- Termly schema for MariaDB (the app also creates these tables itself on startup)
CREATE DATABASE IF NOT EXISTS termly CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE termly;

CREATE TABLE IF NOT EXISTS courses (
  course_code     VARCHAR(20)  PRIMARY KEY,   -- CSE220
  course_name     VARCHAR(255) NOT NULL,      -- Data Structures
  subject         VARCHAR(10)  NOT NULL,      -- CSE
  course_number   VARCHAR(10)  NOT NULL,      -- 220
  description     TEXT,
  credit          INT          NOT NULL DEFAULT 0,
  course_session  VARCHAR(50),                -- First Half / Second Half / Regular Full / Special
  course_career   VARCHAR(50),                -- Undergraduate / Graduate
  has_prereq      BOOLEAN      NOT NULL DEFAULT FALSE,
  INDEX idx_subject (subject),
  INDEX idx_session (course_session),
  INDEX idx_career (course_career)
);

CREATE TABLE IF NOT EXISTS sections (
  crn               VARCHAR(10)  PRIMARY KEY, -- 7587
  course_code       VARCHAR(20)  NOT NULL,
  meeting_days      VARCHAR(20)  DEFAULT '',  -- "M W F", "TR", "" for async
  start_time        VARCHAR(10)  DEFAULT '',  -- 24h "14:20" preferred
  end_time          VARCHAR(10)  DEFAULT '',
  section_location  VARCHAR(100) DEFAULT '',
  section_type      VARCHAR(10)  DEFAULT 'LEC', -- LEC / REC / SEM / LAB
  instructor_name   VARCHAR(100) DEFAULT '',
  instruction_mode  VARCHAR(50),               -- In Person / Online Synchronous / Online Asynchronous
  status            VARCHAR(10)  DEFAULT 'Not Full', -- Full / Not Full
  INDEX idx_course (course_code),
  INDEX idx_mode (instruction_mode),
  FOREIGN KEY (course_code) REFERENCES courses(course_code) ON DELETE CASCADE
);
