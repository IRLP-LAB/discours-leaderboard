-- Migration Script for Coreference Evaluation System
-- Updates existing database to version 2.0 with new features

USE coref_eval_system;

-- Step 1: Add team_name column to users table if it doesn't exist
ALTER TABLE users 
ADD COLUMN IF NOT EXISTS team_name VARCHAR(255) NULL AFTER password_hash;

-- Step 1a: Ensure team_name column allows NULL values
ALTER TABLE users MODIFY COLUMN team_name VARCHAR(255) NULL;

-- Step 2: Add is_admin column to users table if it doesn't exist
ALTER TABLE users 
ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE AFTER team_name;

-- Step 3: Create activity_logs table if it doesn't exist
CREATE TABLE IF NOT EXISTS activity_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    activity_type VARCHAR(50) NOT NULL,
    details TEXT,
    language_id INT,
    filename VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (language_id) REFERENCES languages(id) ON DELETE SET NULL,
    INDEX idx_user_id (user_id),
    INDEX idx_activity_type (activity_type),
    INDEX idx_created_at (created_at)
);

-- Step 3a: Add soft delete flags to languages table
ALTER TABLE languages 
ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE AFTER language_name;

ALTER TABLE languages 
ADD INDEX IF NOT EXISTS idx_is_deleted (is_deleted);

-- Step 3b: Add soft delete and version flags to gold_datasets table
ALTER TABLE gold_datasets 
ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE AFTER uploaded_by;

ALTER TABLE gold_datasets 
ADD COLUMN IF NOT EXISTS version INT DEFAULT 1 AFTER is_deleted;

ALTER TABLE gold_datasets 
ADD INDEX IF NOT EXISTS idx_is_deleted (is_deleted);

ALTER TABLE gold_datasets 
ADD INDEX IF NOT EXISTS idx_version (version);

-- Step 4: Update existing admin user to have is_admin flag and NULL team_name
UPDATE users SET is_admin = TRUE, team_name = NULL WHERE username = 'admin';

-- Step 5: Update existing admin2 user to have is_admin flag and NULL team_name
UPDATE users SET is_admin = TRUE, team_name = NULL WHERE username = 'admin2';

-- Step 6: Insert or update default admin user (password: admin123)
INSERT INTO users (username, email, password_hash, team_name, is_admin, is_active) 
VALUES (
    'admin', 
    'admin@test.com', 
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYIE.cXG8mu',
    NULL,
    TRUE,
    TRUE
) ON DUPLICATE KEY UPDATE 
    password_hash = VALUES(password_hash),
    team_name = VALUES(team_name),
    is_admin = VALUES(is_admin),
    is_active = VALUES(is_active);


-- Step 7: Insert or update second admin user (password: admin123)
INSERT INTO users (username, email, password_hash, team_name, is_admin, is_active) 
VALUES (
    'admin2', 
    'admin2@test.com', 
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYIE.cXG8mu',
    NULL,
    TRUE,
    TRUE
) ON DUPLICATE KEY UPDATE 
    password_hash = VALUES(password_hash),
    team_name = VALUES(team_name),
    is_admin = VALUES(is_admin);

-- Step 8: Insert or update test user (password: user123)
INSERT INTO users (username, email, password_hash, team_name, is_admin, is_active) 
VALUES (
    'testuser', 
    'user@test.com', 
    '$2b$12$92IXUNpkjO0rOQ5byMi.Ye4oKoEa3Ro9llC/.og/at2.uheWG/igi',
    'Test Team',
    FALSE,
    TRUE
) ON DUPLICATE KEY UPDATE 
    password_hash = VALUES(password_hash),
    team_name = VALUES(team_name),
    is_admin = VALUES(is_admin);

-- Step 9: Add indexes if they don't exist (MySQL will ignore if they exist)
ALTER TABLE users ADD INDEX IF NOT EXISTS idx_is_admin (is_admin);
ALTER TABLE users ADD INDEX IF NOT EXISTS idx_is_active (is_active);
ALTER TABLE users ADD INDEX IF NOT EXISTS idx_team_name (team_name);

-- Step 10: Create view for user statistics
CREATE OR REPLACE VIEW user_stats AS
SELECT 
    u.id,
    u.username,
    u.email,
    u.team_name,
    u.is_admin,
    u.is_active,
    COUNT(DISTINCT ue.language_id) as languages_evaluated,
    COUNT(ue.id) as total_evaluations,
    MAX(ue.created_at) as last_evaluation
FROM users u
LEFT JOIN user_evaluations ue ON u.id = ue.user_id
GROUP BY u.id, u.username, u.email, u.team_name, u.is_admin, u.is_active;

-- Step 11: Create view for leaderboard
CREATE OR REPLACE VIEW leaderboard AS
SELECT 
    ue.language_id,
    l.language_name,
    l.language_code,
    ue.user_id,
    u.username,
    u.team_name,
    gd.version,
    MAX(ue.muc_f1) as best_muc_f1,
    MAX(ue.bcub_f1) as best_bcub_f1,
    MAX(ue.ceafm_f1) as best_ceafm_f1,
    MAX(ue.blanc_f1) as best_blanc_f1,
    AVG((COALESCE(ue.muc_f1, 0) + COALESCE(ue.bcub_f1, 0) + COALESCE(ue.ceafm_f1, 0) + COALESCE(ue.blanc_f1, 0)) / 4) as avg_f1,
    MAX(ue.created_at) as latest_submission
FROM user_evaluations ue
JOIN users u ON ue.user_id = u.id
JOIN languages l ON ue.language_id = l.id
JOIN gold_datasets gd ON ue.language_id = gd.language_id
WHERE u.is_active = TRUE AND l.is_deleted = FALSE AND gd.is_deleted = FALSE AND gd.version = (
    SELECT MAX(version) FROM gold_datasets WHERE language_id = ue.language_id AND is_deleted = FALSE
)
GROUP BY ue.language_id, l.language_name, l.language_code, ue.user_id, u.username, u.team_name, gd.version
ORDER BY l.language_name, avg_f1 DESC;

-- Verify the migration
SELECT 'Migration completed successfully!' as Status;
SELECT 'Checking users table structure...' as Info;
DESCRIBE users;

SELECT 'Checking activity_logs table...' as Info;
DESCRIBE activity_logs;

SELECT 'Current users with teams and admin status:' as Info;
SELECT id, username, email, team_name, is_admin, is_active FROM users;

COMMIT;
