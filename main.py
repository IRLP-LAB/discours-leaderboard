from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, File, UploadFile, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import mysql.connector
import bcrypt
import os
import secrets
import subprocess
import tempfile
import shutil
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re
import platform
import time
from urllib.parse import urlencode


app = FastAPI(root_path="/discours-leaderboard")

DEFAULT_TASK_SUGGESTIONS = ["Coreference", "POS Tag", "Chunk"]

@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    """Redirect unauthenticated users to login; otherwise return JSON detail."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        login_url = request.url_for("login_page")
        # Preserve the originally requested path so we can redirect back after login later if desired
        if request.url.path and request.url.path != "/login":
            login_url = f"{login_url}?next={request.url.path}"
        return RedirectResponse(url=login_url, status_code=302)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

def redirect_to_admin(
    tab: str | None = None,
    lang_error: str | None = None,
    dataset_error: str | None = None,
    user_error: str | None = None,
):
    """Return a relative redirect to the admin dashboard so host stays consistent."""
    base = app.root_path.rstrip("/") if app.root_path else ""
    url = f"{base}/admin"
    params = {}
    if tab:
        params["tab"] = tab
    if lang_error:
        params["lang_error"] = lang_error
    if dataset_error:
        params["dataset_error"] = dataset_error
    if user_error:
        params["user_error"] = user_error
    if params:
        url = f"{url}?{urlencode(params)}"
    return RedirectResponse(url=url, status_code=303)

# Create directories
Path("templates").mkdir(exist_ok=True)
Path("uploads").mkdir(exist_ok=True)
Path("gold_datasets").mkdir(exist_ok=True)
Path("scorer").mkdir(exist_ok=True)

templates = Jinja2Templates(directory="templates")

# Middleware to add cache control headers to authenticated routes
@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    # List of paths that require authentication and should not be cached
    protected_paths = ["/client", "/admin", "/evaluate", "/logout"]
    
    response = await call_next(request)
    
    # Check if the request path requires authentication
    if any(request.url.path.startswith(path) for path in protected_paths):
        # Add cache control headers for protected pages
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    
    return response

# Session storage (in production, use Redis or database)
active_sessions = {}
SECRET_KEY = secrets.token_urlsafe(32)
SESSION_TIMEOUT = 3600  # 1 hour in seconds

# Database config - Use environment variables for Docker
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'database': os.getenv('DB_NAME', 'coref_eval_system'),
    'user': os.getenv('DB_USER', 'harsh'),
    'password': os.getenv('DB_PASSWORD', 'harsh')
} 

# Demo data - expanded to include evaluation history
DEMO_USERS = {
    'admin': {'id': 1, 'username': 'admin', 'password_hash': bcrypt.hashpw('admin123'.encode(), bcrypt.gensalt()).decode(), 'email': 'admin@test.com', 'is_active': True, 'team_name': None, 'is_admin': True},
    'testuser': {'id': 2, 'username': 'testuser', 'password_hash': bcrypt.hashpw('user123'.encode(), bcrypt.gensalt()).decode(), 'email': 'user@test.com', 'is_active': True, 'team_name': 'Test Team', 'is_admin': False}
}

DEMO_LANGUAGES = [
    {'id': 1, 'language_code': 'hi', 'language_name': 'Hindi', 'task': 'Coreference'},
    {'id': 2, 'language_code': 'en', 'language_name': 'English', 'task': 'Coreference'}
]

# Demo storage for evaluations and gold datasets
DEMO_GOLD_DATASETS = []
DEMO_EVALUATIONS = []
DEMO_ACTIVITY_LOGS = []

def normalize_task(task: str) -> str:
    """Normalize and validate incoming task names (freeform, but required)."""
    task_clean = (task or "").strip()
    if not task_clean:
        raise HTTPException(status_code=400, detail="Task selection is required")
    if len(task_clean) > 50:
        raise HTTPException(status_code=400, detail="Task name must be 50 characters or less")
    return task_clean

def get_db_connection():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Exception as e:
        print(f"Database connection failed: {e}")
        return None

def get_session_user(session_token: str | None):
    """Return session user if token is valid, otherwise None."""
    if not session_token:
        return None
    
    session_data = active_sessions.get(session_token)
    if not session_data:
        return None
    
    expires_at = session_data.get('expires_at')
    if expires_at and time.time() > expires_at:
        # Session expired - remove and deny
        del active_sessions[session_token]
        return None
    
    # Refresh sliding expiration on activity
    session_data['expires_at'] = time.time() + SESSION_TIMEOUT
    return session_data.get('user')

def get_current_user(session_token: str = Cookie(None)):
    user = get_session_user(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def log_activity(user_id: int, activity_type: str, language_id: int = None, filename: str = None, details: str = None):
    """Log user activities to database for audit trail"""
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO activity_logs (user_id, activity_type, language_id, filename, details)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, activity_type, language_id, filename, details))
            conn.commit()
            conn.close()
            print(f"ACTIVITY LOGGED: User {user_id} - {activity_type}")
        except Exception as e:
            print(f"ERROR logging activity: {e}")
            if conn:
                conn.close()
            # Fall back to demo log on error
            add_demo_activity(user_id, activity_type, language_id, filename, details)
    else:
        print(f"DATABASE UNAVAILABLE: Could not log activity - User {user_id} - {activity_type}")
        add_demo_activity(user_id, activity_type, language_id, filename, details)

def add_demo_activity(user_id: int, activity_type: str, language_id: int = None, filename: str = None, details: str = None):
    """Store activity in demo log when DB is unavailable"""
    global DEMO_ACTIVITY_LOGS
    user_record = next((u for u in DEMO_USERS.values() if u['id'] == user_id), None)
    username = user_record['username'] if user_record else f"user_{user_id}"
    team_name = user_record.get('team_name') if user_record else None
    is_admin = user_record.get('is_admin') if user_record else False
    language_used = None
    language_task = None
    if language_id:
        lang_obj = next((lang for lang in DEMO_LANGUAGES if lang['id'] == language_id), None)
        if lang_obj:
            language_used = lang_obj.get('language_name')
            language_task = lang_obj.get('task')
    
    DEMO_ACTIVITY_LOGS.append({
        'id': len(DEMO_ACTIVITY_LOGS) + 1,
        'user_id': user_id,
        'username': username,
        'team_name': team_name,
        'is_admin': is_admin,
        'activity_type': activity_type,
        'language_id': language_id,
        'language_used': language_used,
        'language_task': language_task,
        'filename': filename,
        'file_uploaded': filename,
        'details': details,
        'created_at': datetime.now()
    })

def authenticate_user(username: str, password: str):
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE username = %s AND is_active = 1", (username,))
            user = cursor.fetchone()
            conn.close()
            print(f"DEBUG: Found user in database: {username}")
        except Exception as e:
            print(f"Database authentication error: {e}")
            user = None
            if conn:
                conn.close()
    else:
        user = DEMO_USERS.get(username)
        if user and not user.get('is_active', True):
            print(f"DEBUG: Demo user inactive: {username}")
            user = None
        print(f"DEBUG: Database unavailable, using demo users for: {username}")
    
    if not user:
        print(f"DEBUG: User not found: {username}")
        return None
    
    # Handle both bytes and string password hashes
    password_hash = user['password_hash']
    print(f"DEBUG: Password hash type: {type(password_hash)}")
    print(f"DEBUG: Password hash (first 20 chars): {str(password_hash)[:20]}")
    
    if isinstance(password_hash, str):
        password_hash = password_hash.encode()
    
    # Test password check
    try:
        result = bcrypt.checkpw(password.encode(), password_hash)
        print(f"DEBUG: Password check result: {result}")
        if not result:
            print(f"DEBUG: Password mismatch for user: {username}")
            return None
    except Exception as e:
        print(f"DEBUG: bcrypt error: {e}")
        return None
    
    print(f"DEBUG: Authentication successful for: {username}")
    return user

def find_gold_dataset(language_id: int, task: str | None = None):
    """Find the gold dataset for a given language (and task if provided)"""
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            if task:
                cursor.execute("""
                    SELECT * FROM gold_datasets 
                    WHERE language_id = %s AND task = %s AND is_deleted = FALSE
                    ORDER BY created_at DESC LIMIT 1
                """, (language_id, task))
            else:
                cursor.execute("""
                    SELECT * FROM gold_datasets 
                    WHERE language_id = %s AND is_deleted = FALSE
                    ORDER BY created_at DESC LIMIT 1
                """, (language_id,))
            dataset = cursor.fetchone()
            conn.close()
            if dataset:
                return dataset
        except Exception as e:
            print(f"Database error finding gold dataset: {e}")
            if conn:
                conn.close()
    
    # Fallback to demo data
    for dataset in DEMO_GOLD_DATASETS:
        if dataset['language_id'] == language_id:
            if task:
                if dataset.get('task') == task:
                    return dataset
            else:
                return dataset
    
    return None

def check_perl_availability():
    """Check if Perl is available on the system"""
    try:
        result = subprocess.run(['perl', '-v'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False

def check_perl_dependencies():
    """Check if required Perl modules are available"""
    required_modules = [
        'Math::Combinatorics',
        'Algorithm::Munkres'
    ]
    
    missing_modules = []
    
    for module in required_modules:
        try:
            result = subprocess.run([
                'perl', '-e', f'use {module}; print "OK";'
            ], capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                missing_modules.append(module)
        except:
            missing_modules.append(module)
    
    return missing_modules

def run_perl_scorer(gold_file_path: str, system_file_path: str) -> dict:
    """Execute the Perl scorer script and parse results - NO DEMO FALLBACK"""
    scorer_script = Path("scorer") / "scorer.pl"
    
    if not scorer_script.exists():
        raise HTTPException(status_code=400, detail="Scorer script not found. Please upload scorer.pl through admin panel.")
    
    # Check if Perl is available - FAIL if not found
    if not check_perl_availability():
        raise HTTPException(status_code=400, detail="Perl not installed. Please install Perl from https://strawberryperl.com/ and restart the server.")
    
    # Check for required Perl modules
    corscore_pm = Path("scorer") / "CorScorer.pm"
    if not corscore_pm.exists():
        raise HTTPException(status_code=400, detail="CorScorer.pm module not found in scorer directory. Please upload the complete CorScorer package.")
    
    # Check Perl dependencies
    missing_modules = check_perl_dependencies()
    if missing_modules:
        module_list = ", ".join(missing_modules)
        install_commands = "\n".join([f"cpan install {module}" for module in missing_modules])
        raise HTTPException(
            status_code=400, 
            detail=f"Missing required Perl modules: {module_list}. Install them using:\n{install_commands}"
        )
    
    try:
        # Convert paths to absolute paths to avoid issues
        gold_path = os.path.abspath(gold_file_path)
        system_path = os.path.abspath(system_file_path)
        scorer_path = os.path.abspath(scorer_script)
        scorer_dir = os.path.dirname(scorer_path)
        
        # Verify files exist
        if not os.path.exists(gold_path):
            raise HTTPException(status_code=400, detail=f"Gold dataset file not found: {gold_path}")
        if not os.path.exists(system_path):
            raise HTTPException(status_code=400, detail=f"System file not found: {system_path}")
        
        print(f"EXECUTING: perl \"{scorer_path}\" all \"{gold_path}\" \"{system_path}\"")
        print(f"Working directory: {scorer_dir}")
        
        # Run the perl script with proper library path
        env = os.environ.copy()
        # Add scorer directory to Perl's library path
        if 'PERL5LIB' in env:
            env['PERL5LIB'] = f"{scorer_dir}{os.pathsep}{env['PERL5LIB']}"
        else:
            env['PERL5LIB'] = scorer_dir
            
        result = subprocess.run([
            'perl', '-I', scorer_dir, scorer_path, 'all', gold_path, system_path
        ], capture_output=True, text=True, timeout=120, cwd=scorer_dir, env=env)
        
        print(f"PERL SCRIPT STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"PERL SCRIPT STDERR:\n{result.stderr}")
        print(f"PERL SCRIPT RETURN CODE: {result.returncode}")
        
        if result.returncode != 0:
            error_msg = f"Perl script failed with exit code {result.returncode}."
            
            if "Can't locate Math/Combinatorics.pm" in result.stderr:
                error_msg = "Missing Math::Combinatorics module. Install with: cpan install Math::Combinatorics"
            elif "Can't locate Algorithm/Munkres.pm" in result.stderr:
                error_msg = "Missing Algorithm::Munkres module. Install with: cpan install Algorithm::Munkres"
            elif "Can't locate" in result.stderr:
                error_msg += " Missing Perl modules. Please install required dependencies."
            elif result.stderr:
                error_msg += f" Error: {result.stderr}"
                
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Parse the output to extract scores
        scores = parse_scorer_output(result.stdout)
        
        if not scores:
            raise HTTPException(status_code=400, detail=f"Could not parse scorer output. Raw output: {result.stdout}")
        
        print(f"PARSED SCORES: {scores}")
        return scores
    
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="Perl script execution timeout (>120 seconds)")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=400, detail=f"Perl script execution failed: {e}")
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail="Perl command not found. Please install Perl and restart the server.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error running scorer: {str(e)}")

def parse_micro_output(output: str) -> dict:
    """Parse Micro Precision/Recall/F1/Accuracy lines from python evaluators."""
    metrics = {}
    for line in output.splitlines():
        line = line.strip()
        if "Micro Precision" in line:
            match = re.search(r"Micro Precision\s*=\s*([0-9.]+)", line)
            if match:
                metrics['precision'] = float(match.group(1))
        elif "Micro Recall" in line:
            match = re.search(r"Micro Recall\s*=\s*([0-9.]+)", line)
            if match:
                metrics['recall'] = float(match.group(1))
        elif "Micro F1" in line:
            match = re.search(r"Micro F1\s*=\s*([0-9.]+)", line)
            if match:
                metrics['f1'] = float(match.group(1))
        elif "Micro Accuracy" in line:
            match = re.search(r"Micro Accuracy\s*=\s*([0-9.]+)", line)
            if match:
                metrics['accuracy'] = float(match.group(1))
    if metrics:
        return {'micro': metrics}
    return {}

def run_python_scorer(script_path: Path, gold_file_path: str, system_file_path: str) -> dict:
    """Run a Python-based scorer (POS/Chunk) and parse micro metrics."""
    if not script_path.exists():
        raise HTTPException(status_code=400, detail=f"Scorer script not found: {script_path}")
    try:
        result = subprocess.run(
            ["python", str(script_path), "--ref", gold_file_path, "--pred", system_file_path],
            capture_output=True,
            text=True,
            timeout=120
        )
        print(f"PY SCORER STDOUT:\n{result.stdout}")
        if result.stderr:
            print(f"PY SCORER STDERR:\n{result.stderr}")
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Scorer failed with exit code {result.returncode}")
        scores = parse_micro_output(result.stdout)
        if not scores:
            raise HTTPException(status_code=400, detail="Could not parse scorer output for micro metrics.")
        return scores
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="Scorer execution timeout (>120 seconds)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error running scorer: {str(e)}")

def parse_scorer_output(output: str) -> dict:
    """Parse the Perl scorer output to extract metrics"""
    scores = {}
    
    if not output or output.strip() == "":
        return {}
    
    try:
        # Print raw output for debugging
        print(f"RAW SCORER OUTPUT:\n{repr(output)}")
        
        # Parse the specific format from your scorer
        lines = output.split('\n')
        
        for line in lines:
            line = line.strip()
            
            # Parse Identification of Mentions (this is like MUC)
            if "Identification of Mentions:" in line:
                # Format: "Identification of Mentions: Recall: (291 / 291) 100%      Precision: (291 / 291) 100%     F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%?', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%?', line)
                
                if recall_match and precision_match and f1_match:
                    def conv(val):
                        v = float(val)
                        return v / 100 if "%" in line or v > 1 else v
                    scores['muc'] = {
                        'recall': conv(recall_match.group(1)),
                        'precision': conv(precision_match.group(1)),
                        'f1': conv(f1_match.group(1))
                    }
                    print(f"PARSED MUC (Identification): R={scores['muc']['recall']}, P={scores['muc']['precision']}, F1={scores['muc']['f1']}")
            
            # Parse Coreference links (this is like B-CUBED)
            elif "Coreference links:" in line:
                # Format: "Coreference links: Recall: (602 / 602) 100%       Precision: (602 / 602) 100%     F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%?', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%?', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%?', line)
                
                if recall_match and precision_match and f1_match:
                    def conv(val):
                        v = float(val)
                        return v / 100 if "%" in line or v > 1 else v
                    scores['bcub'] = {
                        'recall': conv(recall_match.group(1)),
                        'precision': conv(precision_match.group(1)),
                        'f1': conv(f1_match.group(1))
                    }
                    print(f"PARSED B-CUBED (Coreference): R={scores['bcub']['recall']}, P={scores['bcub']['precision']}, F1={scores['bcub']['f1']}")
            
            # Parse Non-coreference links (additional metric)
            elif "Non-coreference links:" in line:
                # Format: "Non-coreference links: Recall: (3200 / 3200) 100% Precision: (3200 / 3200) 100%   F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%?', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%?', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%?', line)
                
                if recall_match and precision_match and f1_match:
                    def conv(val):
                        v = float(val)
                        return v / 100 if "%" in line or v > 1 else v
                    scores['ceafm'] = {
                        'recall': conv(recall_match.group(1)),
                        'precision': conv(precision_match.group(1)),
                        'f1': conv(f1_match.group(1))
                    }
                    print(f"PARSED CEAF-M (Non-coreference): R={scores['ceafm']['recall']}, P={scores['ceafm']['precision']}, F1={scores['ceafm']['f1']}")
            
            # Parse BLANC
            elif "BLANC:" in line:
                # Format: "BLANC: Recall: (1 / 1) 100%       Precision: (1 / 1) 100% F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%?', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%?', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%?', line)
                
                if recall_match and precision_match and f1_match:
                    def conv(val):
                        v = float(val)
                        return v / 100 if "%" in line or v > 1 else v
                    scores['blanc'] = {
                        'recall': conv(recall_match.group(1)),
                        'precision': conv(precision_match.group(1)),
                        'f1': conv(f1_match.group(1))
                    }
                    print(f"PARSED BLANC: R={scores['blanc']['recall']}, P={scores['blanc']['precision']}, F1={scores['blanc']['f1']}")
            
            # Also try to parse any standard CoNLL format that might be in the output
            elif re.search(r'MUC.*?Recall:.*?Precision:.*?F1:', line, re.IGNORECASE):
                recall_match = re.search(r'Recall:\s*([\d.]+)', line)
                precision_match = re.search(r'Precision:\s*([\d.]+)', line)
                f1_match = re.search(r'F1:\s*([\d.]+)', line)
                
                if recall_match and precision_match and f1_match:
                    if 'muc' not in scores:  # Don't overwrite if already parsed
                        scores['muc'] = {
                            'recall': float(recall_match.group(1)),
                            'precision': float(precision_match.group(1)),
                            'f1': float(f1_match.group(1))
                        }
                        print(f"PARSED MUC (standard): R={scores['muc']['recall']}, P={scores['muc']['precision']}, F1={scores['muc']['f1']}")
        
        # If we didn't find any metrics, try alternative parsing
        if not scores:
            print("No standard metrics found, trying alternative patterns...")
            
            # Look for percentage patterns
            percentage_lines = [line for line in lines if '%' in line and ('Recall' in line or 'Precision' in line)]
            for line in percentage_lines:
                print(f"PERCENTAGE LINE: {line}")
                
                # Try to extract any three consecutive percentages
                percentages = re.findall(r'([\d.]+)%', line)
                if len(percentages) >= 3:
                    try:
                        recall = float(percentages[0]) / 100
                        precision = float(percentages[1]) / 100
                        f1 = float(percentages[2]) / 100
                        
                        # Assign to a generic metric if we don't have any
                        if not scores:
                            scores['overall'] = {
                                'recall': recall,
                                'precision': precision,
                                'f1': f1
                            }
                            print(f"PARSED OVERALL: R={recall}, P={precision}, F1={f1}")
                            break
                    except ValueError:
                        continue
        
        return scores
    
    except Exception as e:
        print(f"ERROR parsing scorer output: {e}")
        return {}

def generate_demo_scores() -> dict:
    """Generate realistic demo scores"""
    import random
    
    print("GENERATING DEMO SCORES (Perl script not executed)")
    
    scores = {}
    metrics = ['muc', 'bcub', 'ceafm', 'ceafe', 'blanc']
    
    for metric in metrics:
        # Generate realistic scores between 0.7-0.9
        recall = round(random.uniform(0.70, 0.90), 4)
        precision = round(random.uniform(0.70, 0.90), 4)
        f1 = round(2 * recall * precision / (recall + precision), 4)
        
        scores[metric] = {
            'recall': recall,
            'precision': precision,
            'f1': f1
        }
    
    return scores

def normalize_scores_for_storage(scores: dict) -> dict:
    """Map generic scores to storage-friendly keys (reuses coref columns)."""
    mapped = {**scores}
    micro = scores.get('micro')
    if micro:
        mapped.setdefault('muc', {})
        mapped['muc']['precision'] = micro.get('precision')
        mapped['muc']['recall'] = micro.get('recall')
        mapped['muc']['f1'] = micro.get('f1')
        mapped.setdefault('blanc', {})
        mapped['blanc']['f1'] = micro.get('accuracy')
        # Also carry micro for downstream consumers
        mapped['micro'] = {
            'precision': micro.get('precision'),
            'recall': micro.get('recall'),
            'f1': micro.get('f1'),
            'accuracy': micro.get('accuracy'),
        }
    return mapped

def save_evaluation_results(user_id: int, language_id: int, filename: str, file_path: str, scores: dict):
    """Save evaluation results to database or demo storage"""
    scores = normalize_scores_for_storage(scores)
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_evaluations (
                    user_id, language_id, uploaded_filename, file_path,
                    muc_recall, muc_precision, muc_f1,
                    bcub_recall, bcub_precision, bcub_f1,
                    ceafm_recall, ceafm_precision, ceafm_f1,
                    ceafe_recall, ceafe_precision, ceafe_f1,
                    blanc_recall, blanc_precision, blanc_f1
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id, language_id, filename, file_path,
                scores.get('muc', {}).get('recall'), scores.get('muc', {}).get('precision'), scores.get('muc', {}).get('f1'),
                scores.get('bcub', {}).get('recall'), scores.get('bcub', {}).get('precision'), scores.get('bcub', {}).get('f1'),
                scores.get('ceafm', {}).get('recall'), scores.get('ceafm', {}).get('precision'), scores.get('ceafm', {}).get('f1'),
                scores.get('ceafe', {}).get('recall'), scores.get('ceafe', {}).get('precision'), scores.get('ceafe', {}).get('f1'),
                scores.get('blanc', {}).get('recall'), scores.get('blanc', {}).get('precision'), scores.get('blanc', {}).get('f1')
            ))
            conn.commit()
            conn.close()
            print("SUCCESS: Evaluation results saved to database")
        except Exception as e:
            print(f"ERROR saving to database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            save_to_demo_evaluations(user_id, language_id, filename, file_path, scores)
    else:
        # Save to demo storage
        save_to_demo_evaluations(user_id, language_id, filename, file_path, scores)

def save_to_demo_evaluations(user_id: int, language_id: int, filename: str, file_path: str, scores: dict):
    """Save evaluation to demo storage"""
    language_name = next((lang['language_name'] for lang in DEMO_LANGUAGES if lang['id'] == language_id), 'Unknown')
    language_task = next((lang.get('task') for lang in DEMO_LANGUAGES if lang['id'] == language_id), None)
    user_team = next((user['team_name'] for user in DEMO_USERS.values() if user['id'] == user_id), None)
    
    evaluation = {
        'id': len(DEMO_EVALUATIONS) + 1,
        'user_id': user_id,
        'language_id': language_id,
        'language_name': language_name,
        'task': language_task,
        'team_name': user_team,
        'uploaded_filename': filename,
        'file_path': file_path,
        'formatted_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'muc_f1': scores.get('muc', {}).get('f1'),
        'muc_precision': scores.get('muc', {}).get('precision'),
        'muc_recall': scores.get('muc', {}).get('recall'),
        'bcub_f1': scores.get('bcub', {}).get('f1'),
        'bcub_precision': scores.get('bcub', {}).get('precision'),
        'bcub_recall': scores.get('bcub', {}).get('recall'),
        'ceafm_f1': scores.get('ceafm', {}).get('f1'),
        'ceafe_f1': scores.get('ceafe', {}).get('f1'),
        'blanc_f1': scores.get('blanc', {}).get('f1'),
        'micro_precision': scores.get('micro', {}).get('precision'),
        'micro_recall': scores.get('micro', {}).get('recall'),
        'micro_f1': scores.get('micro', {}).get('f1'),
        'micro_accuracy': scores.get('micro', {}).get('accuracy'),
        'created_at': datetime.now()
    }
    
    DEMO_EVALUATIONS.append(evaluation)
    print(f"SUCCESS: Evaluation results saved to demo storage (ID: {evaluation['id']})")

def get_user_evaluation_history(user_id: int):
    """Get evaluation history for a user"""
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT ue.*, l.language_name, l.task as task, u.team_name, ue.created_at as formatted_date
                FROM user_evaluations ue
                JOIN languages l ON ue.language_id = l.id
                JOIN users u ON ue.user_id = u.id
                WHERE ue.user_id = %s
                ORDER BY ue.created_at DESC
                LIMIT 20
            """, (user_id,))
            history = cursor.fetchall()
            
            # Format dates in Python instead of SQL
            for record in history:
                if record['formatted_date']:
                    record['formatted_date'] = record['formatted_date'].strftime('%Y-%m-%d %H:%M:%S')
                task_name = (record.get('task') or "").lower()
                if 'pos' in task_name or 'chunk' in task_name:
                    record['micro_precision'] = record.get('muc_precision')
                    record['micro_recall'] = record.get('muc_recall')
                    record['micro_f1'] = record.get('muc_f1')
                    record['micro_accuracy'] = record.get('blanc_f1')
            
            conn.close()
            print(f"SUCCESS: Retrieved {len(history)} evaluation records from database")
            return history
        except Exception as e:
            print(f"ERROR retrieving history from database: {e}")
            if conn:
                conn.close()
    
    # Fallback to demo data
    history = [eval for eval in DEMO_EVALUATIONS if eval['user_id'] == user_id]
    enriched_history = []
    for eval_record in history:
        record = eval_record.copy()
        if not record.get('team_name'):
            user_data = next((u for u in DEMO_USERS.values() if u['id'] == user_id), {})
            record['team_name'] = user_data.get('team_name')
        if not record.get('task'):
            language = next((lang for lang in DEMO_LANGUAGES if lang['id'] == record.get('language_id')), {})
            record['task'] = language.get('task')
        task_name = (record.get('task') or "").lower()
        if 'pos' in task_name or 'chunk' in task_name:
            record['micro_precision'] = record.get('micro_precision') or record.get('muc_precision')
            record['micro_recall'] = record.get('micro_recall') or record.get('muc_recall')
            record['micro_f1'] = record.get('micro_f1') or record.get('muc_f1')
            record['micro_accuracy'] = record.get('micro_accuracy') or record.get('blanc_f1')
        enriched_history.append(record)
    
    enriched_history.sort(key=lambda x: x['created_at'], reverse=True)
    print(f"SUCCESS: Retrieved {len(enriched_history)} evaluation records from demo storage")
    return enriched_history[:20]

def get_homepage_statistics():
    """Get statistics for the homepage hero section"""
    stats = {
        'total_languages': 0,
        'total_participants': 0,
        'total_evaluations': 0
    }
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get total languages
            cursor.execute("SELECT COUNT(*) as count FROM languages")
            result = cursor.fetchone()
            stats['total_languages'] = result['count'] if result else 0
            
            # Get total unique participants (users who have made evaluations)
            cursor.execute("SELECT COUNT(DISTINCT user_id) as count FROM user_evaluations")
            result = cursor.fetchone()
            stats['total_participants'] = result['count'] if result else 0
            
            # Get total evaluations
            cursor.execute("SELECT COUNT(*) as count FROM user_evaluations")
            result = cursor.fetchone()
            stats['total_evaluations'] = result['count'] if result else 0
            
            conn.close()
            print(f"SUCCESS: Retrieved homepage statistics - Languages: {stats['total_languages']}, Participants: {stats['total_participants']}, Evaluations: {stats['total_evaluations']}")
        except Exception as e:
            print(f"ERROR retrieving homepage statistics from database: {e}")
            if conn:
                conn.close()
            # Fallback to demo data
            stats = get_demo_statistics()
    else:
        # Use demo data
        stats = get_demo_statistics()
    
    return stats

def get_demo_statistics():
    """Get statistics from demo data"""
    stats = {
        'total_languages': len(DEMO_LANGUAGES),
        'total_participants': len([user for user in DEMO_USERS.values() if user['username'] != 'admin']),
        'total_evaluations': len(DEMO_EVALUATIONS)
    }
    print(f"SUCCESS: Using demo statistics - Languages: {stats['total_languages']}, Participants: {stats['total_participants']}, Evaluations: {stats['total_evaluations']}")
    return stats

def get_language_leaderboards():
    """Get top 3 scores for each language"""
    leaderboards = []
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get all non-deleted languages
            cursor.execute("SELECT * FROM languages WHERE is_deleted = FALSE ORDER BY language_name")
            languages = cursor.fetchall()
            
            for language in languages:
                language_data = {
                    'language_id': language['id'],
                    'language_name': language['language_name'],
                    'language_code': language['language_code'],
                    'task': language.get('task', 'Coreference'),
                    'top_scores': []
                }
                
                # Get ALL scores for this language, using only non-deleted datasets
                # Get the latest version of the dataset for this language
                cursor.execute("""
                    SELECT ue.*, u.username, u.team_name,
                           ((COALESCE(ue.muc_f1, 0) + COALESCE(ue.bcub_f1, 0) + 
                             COALESCE(ue.ceafm_f1, 0) + COALESCE(ue.blanc_f1, 0)) / 4) as avg_f1
                    FROM user_evaluations ue
                    JOIN users u ON ue.user_id = u.id
                    JOIN gold_datasets gd ON ue.language_id = gd.language_id AND (gd.task = %s OR gd.task IS NULL)
                    WHERE ue.language_id = %s
                    AND u.is_active = 1
                    AND gd.is_deleted = 0
                    AND gd.version = (
                        SELECT MAX(version) FROM gold_datasets 
                        WHERE language_id = %s AND is_deleted = 0 AND (task = %s OR task IS NULL)
                    )
                    ORDER BY avg_f1 DESC
                """, (language.get('task', 'Coreference'), language['id'], language['id'], language.get('task', 'Coreference')))
                
                top_scores = cursor.fetchall()
                
                # Convert Decimal and datetime to JSON-serializable types
                for score in top_scores:
                    # Convert Decimal to float
                    score['muc_f1'] = float(score['muc_f1']) if score['muc_f1'] is not None else None
                    score['bcub_f1'] = float(score['bcub_f1']) if score['bcub_f1'] is not None else None
                    score['ceafm_f1'] = float(score['ceafm_f1']) if score['ceafm_f1'] is not None else None
                    score['ceafe_f1'] = float(score['ceafe_f1']) if score['ceafe_f1'] is not None else None
                    score['blanc_f1'] = float(score['blanc_f1']) if score['blanc_f1'] is not None else None
                    score['avg_f1'] = float(score['avg_f1']) if score['avg_f1'] is not None else None
                    
                    # Convert other Decimal fields if they exist
                    for key in ['muc_recall', 'muc_precision', 'bcub_recall', 'bcub_precision',
                               'ceafm_recall', 'ceafm_precision', 'ceafe_recall', 'ceafe_precision',
                               'blanc_recall', 'blanc_precision']:
                        if key in score and score[key] is not None:
                            score[key] = float(score[key])
                    
                    # Convert datetime to string
                    if 'created_at' in score and score['created_at'] is not None:
                        score['created_at'] = score['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                    if 'updated_at' in score and score['updated_at'] is not None:
                        score['updated_at'] = score['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
                
                language_data['top_scores'] = top_scores
                leaderboards.append(language_data)
            
            conn.close()
            print(f"SUCCESS: Retrieved leaderboards for {len(languages)} languages from database")
            
        except Exception as e:
            print(f"ERROR retrieving leaderboards from database: {e}")
            import traceback
            traceback.print_exc()
            if conn:
                conn.close()
            # Fallback to demo data
            leaderboards = get_demo_leaderboards()
    else:
        # Use demo data
        leaderboards = get_demo_leaderboards()
    
    return leaderboards

def get_demo_leaderboards():
    """Get leaderboards from demo data"""
    leaderboards = []
    
    for language in DEMO_LANGUAGES:
        # Skip deleted languages
        if language.get('is_deleted', False):
            continue
            
        language_data = {
            'language_id': language['id'],
            'language_name': language['language_name'],
            'language_code': language['language_code'],
            'task': language.get('task', 'Coreference'),
            'top_scores': []
        }
        
        # Get evaluations for this language from demo data
        language_evaluations = []
        for eval in DEMO_EVALUATIONS:
            if eval['language_id'] == language['id']:
                # Create a copy and convert datetime
                eval_copy = eval.copy()
                if 'created_at' in eval_copy and eval_copy['created_at'] is not None:
                    if hasattr(eval_copy['created_at'], 'strftime'):
                        eval_copy['created_at'] = eval_copy['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                
                # Get team_name from DEMO_USERS
                user_data = next((u for u in DEMO_USERS.values() if u['id'] == eval['user_id']), {})
                eval_copy['team_name'] = user_data.get('team_name')
                
                language_evaluations.append(eval_copy)
        
        # Sort by average F1 score (calculate from available metrics)
        for eval in language_evaluations:
            scores = []
            if eval.get('muc_f1') is not None: scores.append(float(eval['muc_f1']))
            if eval.get('bcub_f1') is not None: scores.append(float(eval['bcub_f1']))
            if eval.get('ceafm_f1') is not None: scores.append(float(eval['ceafm_f1']))
            if eval.get('blanc_f1') is not None: scores.append(float(eval['blanc_f1']))
            if eval.get('micro_f1') is not None: scores.append(float(eval['micro_f1']))
            
            eval['avg_f1'] = sum(scores) / len(scores) if scores else 0
            
            # Ensure all values are float, not Decimal
            for key in eval:
                if key != 'created_at' and isinstance(eval[key], (int, float)):
                    eval[key] = float(eval[key])
        
        # Sort by average F1 and get ALL scores
        language_evaluations.sort(key=lambda x: x.get('avg_f1', 0), reverse=True)
        language_data['top_scores'] = language_evaluations
        
        leaderboards.append(language_data)
    
    print(f"SUCCESS: Retrieved demo leaderboards for {len(leaderboards)} non-deleted languages")
    return leaderboards

def get_best_user_score_per_language():
    """Get each user's best score per language for leaderboard ranking"""
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Simplified query to get best scores per user per language
            cursor.execute("""
                SELECT 
                    ue.user_id,
                    ue.language_id,
                    u.username,
                    l.language_name,
                    l.language_code,
                    MAX(COALESCE(ue.muc_f1, 0)) as best_muc_f1,
                    MAX(COALESCE(ue.bcub_f1, 0)) as best_bcub_f1,
                    MAX(COALESCE(ue.ceafm_f1, 0)) as best_ceafm_f1,
                    MAX(COALESCE(ue.ceafe_f1, 0)) as best_ceafe_f1,
                    MAX(COALESCE(ue.blanc_f1, 0)) as best_blanc_f1,
                    MAX(ue.created_at) as latest_submission,
                    MAX((COALESCE(ue.muc_f1, 0) + COALESCE(ue.bcub_f1, 0) + 
                         COALESCE(ue.ceafm_f1, 0) + COALESCE(ue.blanc_f1, 0)) / 4) as best_avg_f1
                FROM user_evaluations ue
                JOIN users u ON ue.user_id = u.id
                JOIN languages l ON ue.language_id = l.id
                WHERE u.is_active = 1
                GROUP BY ue.user_id, ue.language_id, u.username, l.language_name, l.language_code
                ORDER BY l.language_name, best_avg_f1 DESC
            """)
            
            results = cursor.fetchall()
            conn.close()
            return results
            
        except Exception as e:
            print(f"ERROR retrieving best scores from database: {e}")
            if conn:
                conn.close()
            return []
    else:
        return []
    
@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    """Homepage with dynamic leaderboards and statistics"""
    try:
        # Check if a session exists to personalize the page
        session_user = get_session_user(request.cookies.get("session_token"))
        dashboard_url = None
        if session_user:
            is_admin = session_user.get('is_admin', False) or session_user.get('username') == 'admin'
            dashboard_url = request.url_for("admin_dashboard") if is_admin else request.url_for("client_dashboard")
        
        # Get homepage statistics
        stats = get_homepage_statistics()
        
        # Get language leaderboards
        leaderboards = get_language_leaderboards()
        
        return templates.TemplateResponse("homepage.html", {
            "request": request,
            "stats": stats,
            "leaderboards": leaderboards,
            "user": session_user,
            "dashboard_url": dashboard_url
        })
        
    except Exception as e:
        print(f"ERROR loading homepage: {e}")
        # Fallback with minimal data
        return templates.TemplateResponse("homepage.html", {
            "request": request,
            "stats": {
                'total_languages': len(DEMO_LANGUAGES),
                'total_participants': len(DEMO_USERS),
                'total_evaluations': len(DEMO_EVALUATIONS)
            },
            "leaderboards": get_demo_leaderboards(),
            "user": None,
            "dashboard_url": None
        })
    
@app.get("/home", response_class=HTMLResponse) 
async def home_redirect(request: Request):
    """Redirect /home to login for backward compatibility"""
    return RedirectResponse(url=request.url_for("homepage"), status_code=302)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, session_token: str = Cookie(None)):
    """Display the login page, or redirect if already authenticated"""
    session_user = get_session_user(session_token)
    if session_user:
        is_admin = session_user.get('is_admin', False) or session_user.get('username') == 'admin'
        redirect_url = request.url_for("admin_dashboard") if is_admin else request.url_for("client_dashboard")
        response = RedirectResponse(url=redirect_url, status_code=302)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", name="login")
async def login(request: Request, username: str = Form(default=""), password: str = Form(default="")):
    # Check for missing credentials
    if not username or not password:
        return templates.TemplateResponse("login.html", {
            "request": request, 
            "error": "Both username and password are required"
        })
    
    user = authenticate_user(username, password)
    
    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request, 
            "error": "Invalid username or password"
        })
    
    # Create session
    session_token = secrets.token_urlsafe(32)
    active_sessions[session_token] = {
        'user': user,
        'created_at': time.time(),
        'expires_at': time.time() + SESSION_TIMEOUT
    }
    
    # Log login activity
    log_activity(user['id'], 'login')
    
    # Redirect based on user is_admin flag (supports multiple admins)
    is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
    redirect_url = request.url_for("admin_dashboard") if is_admin else request.url_for("client_dashboard")
    response = RedirectResponse(url=redirect_url, status_code=302)
    
    # Set session cookie with httponly flag for security
    response.set_cookie(
        key="session_token", 
        value=session_token, 
        httponly=True,  # Prevent JavaScript access
        secure=False,  # Set to True in production with HTTPS
        samesite="strict",  # CSRF protection
        max_age=SESSION_TIMEOUT
    )
    
    # Add cache control headers to prevent caching of protected pages
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    print(f"SUCCESS: User {username} logged in successfully (Admin: {is_admin})")
    return response

@app.get("/logout", name="logout")
async def logout(request: Request, session_token: str = Cookie(None)):
    session_user = get_session_user(session_token)
    # Clear the session from server-side storage
    if session_token and session_token in active_sessions:
        del active_sessions[session_token]
        print(f"SUCCESS: Session cleared for token: {session_token[:20]}...")
    if session_user:
        log_activity(session_user['id'], 'logout')
    
    response = RedirectResponse(url=request.url_for("homepage"), status_code=302)
    
    # Delete the cookie
    response.delete_cookie(key="session_token", path="/")
    
    # Add cache control headers to prevent caching after logout
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    
    return response

@app.get("/client", response_class=HTMLResponse)
async def client_dashboard(request: Request, user: dict = Depends(get_current_user)):
    if user['username'] == 'admin':
        return RedirectResponse(url=request.url_for("admin_dashboard"), status_code=302)
    
    # Get languages from database
    task_suggestions = []
    client_languages = []
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT DISTINCT 
                    l.id,
                    l.language_code,
                    l.language_name,
                    l.task as lang_task,
                    gd.task as dataset_task
                FROM languages l
                JOIN gold_datasets gd 
                    ON gd.language_id = l.id 
                    AND gd.is_deleted = FALSE
                WHERE l.is_deleted = FALSE
                ORDER BY l.language_name
            """)
            rows = cursor.fetchall()
            languages = []
            seen = set()
            for row in rows:
                task_val = row.get('dataset_task') or row.get('lang_task') or 'Coreference'
                key = (row['id'], task_val)
                if key in seen:
                    continue
                seen.add(key)
                lang_entry = {
                    'id': row['id'],
                    'language_code': row['language_code'],
                    'language_name': row['language_name'],
                    'task': task_val
                }
                client_languages.append(lang_entry)
                languages.append(lang_entry)  # reuse for tojson if needed elsewhere
                if task_val and task_val not in task_suggestions:
                    task_suggestions.append(task_val)
            conn.close()
        except Exception as e:
            print(f"Database error getting languages: {e}")
            languages = DEMO_LANGUAGES
            client_languages = []
            seen = set()
            for lang in DEMO_LANGUAGES:
                if lang.get('is_deleted'):
                    continue
                # Only include languages that have a dataset for this task
                for ds in DEMO_GOLD_DATASETS:
                    if ds.get('is_deleted'):
                        continue
                    if ds.get('language_id') == lang['id']:
                        task_val = ds.get('task') or lang.get('task') or 'Coreference'
                        key = (lang['id'], task_val)
                        if key in seen:
                            continue
                        seen.add(key)
                        entry = {
                            'id': lang['id'],
                            'language_code': lang['language_code'],
                            'language_name': lang['language_name'],
                            'task': task_val
                        }
                        client_languages.append(entry)
                        if task_val and task_val not in task_suggestions:
                            task_suggestions.append(task_val)
            if conn:
                conn.close()
    else:
        languages = DEMO_LANGUAGES
        client_languages = []
        seen = set()
        for lang in DEMO_LANGUAGES:
            if lang.get('is_deleted'):
                continue
            for ds in DEMO_GOLD_DATASETS:
                if ds.get('is_deleted'):
                    continue
                if ds.get('language_id') == lang['id']:
                    task_val = ds.get('task') or lang.get('task') or 'Coreference'
                    key = (lang['id'], task_val)
                    if key in seen:
                        continue
                    seen.add(key)
                    entry = {
                        'id': lang['id'],
                        'language_code': lang['language_code'],
                        'language_name': lang['language_name'],
                        'task': task_val
                    }
                    client_languages.append(entry)
                    if task_val and task_val not in task_suggestions:
                        task_suggestions.append(task_val)
    
    # Get user's evaluation history
    history = get_user_evaluation_history(user['id'])
    history_by_task = {}
    for record in history:
        task_name = record.get('task') or 'Coreference'
        history_by_task.setdefault(task_name, []).append(record)
    
    return templates.TemplateResponse("client_dashboard.html", {
        "request": request,
        "user": user,
        "languages": client_languages,
        "task_suggestions": sorted(set(task_suggestions)),
        "history": history,
        "history_by_task": history_by_task
    })

@app.post("/evaluate")
async def evaluate_file(
    request: Request,
    task: str = Form(None),
    language_id: int = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="Only .txt files allowed")
    
    print(f"STARTING EVALUATION: User {user['username']}, Language ID {language_id}, File {file.filename}")
    
    try:
        task_normalized = normalize_task(task) if task else "Coreference"
        # Find gold dataset for the language
        gold_dataset = find_gold_dataset(language_id, task_normalized)
        if not gold_dataset:
            raise HTTPException(status_code=400, detail=f"No gold dataset found for language ID {language_id} and task '{task}'. Please upload a gold dataset first.")
        
        print(f"FOUND GOLD DATASET: {gold_dataset['filename']} at {gold_dataset['file_path']}")
        
        # Save uploaded file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        upload_path = Path("uploads") / f"{user['id']}_{timestamp}_{file.filename}"
        
        with open(upload_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
        
        print(f"SAVED USER FILE: {upload_path}")
        
        # Check if both files exist
        if not os.path.exists(gold_dataset['file_path']):
            print(f"ERROR: Gold dataset file not found: {gold_dataset['file_path']}")
            raise HTTPException(status_code=400, detail="Gold dataset file not found")
        
        if not os.path.exists(upload_path):
            print(f"ERROR: User file not found: {upload_path}")
            raise HTTPException(status_code=400, detail="User file not found")
        
        # Choose scorer based on task
        task_lower = task_normalized.lower()
        if 'pos' in task_lower:
            scores = run_python_scorer(Path("scorer") / "eval.py", gold_dataset['file_path'], str(upload_path))
        elif 'chunk' in task_lower:
            scores = run_python_scorer(Path("scorer") / "eval-chunker.py", gold_dataset['file_path'], str(upload_path))
        else:
            scores = run_perl_scorer(gold_dataset['file_path'], str(upload_path))
        
        # Save results to database/demo storage
        save_evaluation_results(user['id'], language_id, file.filename, str(upload_path), scores)
        
        # Log activity
        log_activity(user['id'], 'file_uploaded', language_id, file.filename)
        
        print(f"EVALUATION COMPLETE: {file.filename}")
        return {"success": True, "scores": scores, "message": "Evaluation completed successfully"}
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR during evaluation: {e}")
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {str(e)}")

@app.post("/admin/add_language",name="add_language")
async def add_language(
    request: Request,
    language_code: str = Form(...),
    language_name: str = Form(...),
    task: str = Form(...),
    user: dict = Depends(get_current_user)
):
    try:
        # Check if user is admin
        is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
        if not is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        # Validate language code (basic validation)
        language_code = language_code.strip().lower()
        language_name = language_name.strip()
        task = normalize_task(task)
        
        if not language_code or not language_name:
            raise HTTPException(status_code=400, detail="Language code and name are required")
        
        if len(language_code) > 10:
            raise HTTPException(status_code=400, detail="Language code must be 10 characters or less")
        
        # Check if language code already exists
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    "SELECT id FROM languages WHERE language_code = %s AND task = %s AND is_deleted = FALSE",
                    (language_code, task)
                )
                existing = cursor.fetchone()
                
                if existing:
                    conn.close()
                    raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists for task '{task}'")
                
                # Insert new language
                cursor.execute(
                    "INSERT INTO languages (language_code, language_name, task) VALUES (%s, %s, %s)",
                    (language_code, language_name, task)
                )
                conn.commit()
                conn.close()
                print(f"SUCCESS: Language {language_name} ({language_code}) added to database")
                
                # Log activity
                log_activity(user['id'], 'language_added')
            except HTTPException as e:
                # Send back to languages tab with error message
                return redirect_to_admin(tab="languages", lang_error=str(e.detail))
            except Exception as e:
                print(f"ERROR adding language to database: {e}")
                if conn:
                    conn.close()
                # Fallback to demo storage
                add_to_demo_languages(language_code, language_name, task)
                log_activity(user['id'], 'language_added')
        else:
            # Add to demo storage
            add_to_demo_languages(language_code, language_name, task)
            log_activity(user['id'], 'language_added')

        return redirect_to_admin(tab="languages")
    except HTTPException as e:
        # Catch any validation/duplicate errors and surface on the languages tab
        return redirect_to_admin(tab="languages", lang_error=str(e.detail))

@app.post("/admin/update_language/{language_id}")
async def update_language(
    request: Request,
    language_id: int,
    language_code: str = Form(...),
    language_name: str = Form(...),
    task: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Validate input
    language_code = language_code.strip().lower()
    language_name = language_name.strip()
    task = normalize_task(task)
    
    if not language_code or not language_name:
        raise HTTPException(status_code=400, detail="Language code and name are required")
    
    if len(language_code) > 10:
        raise HTTPException(status_code=400, detail="Language code must be 10 characters or less")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Check if language code already exists for different language
            cursor.execute(
                "SELECT id FROM languages WHERE language_code = %s AND task = %s AND id != %s AND is_deleted = FALSE",
                (language_code, task, language_id)
            )
            existing = cursor.fetchone()
            
            if existing:
                conn.close()
                raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists for task '{task}'")
            
            # Update language
            cursor.execute(
                "UPDATE languages SET language_code = %s, language_name = %s, task = %s WHERE id = %s",
                (language_code, language_name, task, language_id)
            )
            conn.commit()
            conn.close()
            print(f"SUCCESS: Language updated to {language_name} ({language_code})")
            log_activity(user['id'], 'language_updated', language_id)
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR updating language in database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            update_demo_language(language_id, language_code, language_name, task)
            log_activity(user['id'], 'language_updated', language_id)
    else:
        # Update demo storage
        update_demo_language(language_id, language_code, language_name, task)
        log_activity(user['id'], 'language_updated', language_id)
    return redirect_to_admin(tab="languages")

@app.post("/admin/delete_language/{language_id}",name="delete_language")
async def delete_language(
    request: Request,
    language_id: int,
    user: dict = Depends(get_current_user)
):
    # Check if user is admin
    is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get language name for confirmation message
            cursor.execute("SELECT language_name FROM languages WHERE id = %s", (language_id,))
            language = cursor.fetchone()
            
            if not language:
                conn.close()
                raise HTTPException(status_code=404, detail="Language not found")
            
            language_name = language['language_name']
            
            # Soft delete: Mark language as deleted
            cursor.execute(
                "UPDATE languages SET is_deleted = TRUE WHERE id = %s",
                (language_id,)
            )
            
            # Soft delete: Mark associated gold datasets as deleted
            cursor.execute(
                "UPDATE gold_datasets SET is_deleted = TRUE WHERE language_id = %s",
                (language_id,)
            )
            
            conn.commit()
            conn.close()
            print(f"SUCCESS: Language '{language_name}' marked as deleted from database (ID: {language_id})")
            
            # Log activity
            log_activity(user['id'], 'language_deleted')
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR deleting language from database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            delete_from_demo_languages(language_id)
            log_activity(user['id'], 'language_deleted', language_id)
    else:
        # Delete from demo storage
        delete_from_demo_languages(language_id)
        log_activity(user['id'], 'language_deleted', language_id)

    return redirect_to_admin(tab="languages")

# Helper functions for demo language management
def add_to_demo_languages(language_code: str, language_name: str, task: str):
    """Add language to demo storage"""
    global DEMO_LANGUAGES
    
    # Check if code already exists
    for lang in DEMO_LANGUAGES:
        if lang['language_code'] == language_code and lang.get('task', 'Coreference') == task and not lang.get('is_deleted'):
            raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists for task '{task}'")
    
    new_id = max([lang['id'] for lang in DEMO_LANGUAGES], default=0) + 1
    new_language = {
        'id': new_id,
        'language_code': language_code,
        'language_name': language_name,
        'task': task
    }
    
    DEMO_LANGUAGES.append(new_language)
    print(f"SUCCESS: Language {language_name} ({language_code}) added to demo storage")

def update_demo_language(language_id: int, language_code: str, language_name: str, task: str):
    """Update language in demo storage"""
    global DEMO_LANGUAGES
    
    # Check if code already exists for different language
    for lang in DEMO_LANGUAGES:
        if (
            lang['language_code'] == language_code
            and lang.get('task', 'Coreference') == task
            and lang['id'] != language_id
            and not lang.get('is_deleted')
        ):
            raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists for task '{task}'")
    
    # Find and update the language
    for i, lang in enumerate(DEMO_LANGUAGES):
        if lang['id'] == language_id:
            DEMO_LANGUAGES[i]['language_code'] = language_code
            DEMO_LANGUAGES[i]['language_name'] = language_name
            DEMO_LANGUAGES[i]['task'] = task
            print(f"SUCCESS: Language updated to {language_name} ({language_code}) in demo storage")
            return
    
    raise HTTPException(status_code=404, detail="Language not found")

def delete_from_demo_languages(language_id: int):
    """Soft delete language from demo storage"""
    global DEMO_LANGUAGES, DEMO_GOLD_DATASETS
    
    # Find the language
    language = next((lang for lang in DEMO_LANGUAGES if lang['id'] == language_id), None)
    if not language:
        raise HTTPException(status_code=404, detail="Language not found")
    
    # Soft delete: Mark language as deleted
    for lang in DEMO_LANGUAGES:
        if lang['id'] == language_id:
            lang['is_deleted'] = True
            break
    
    # Soft delete: Mark associated datasets as deleted
    for dataset in DEMO_GOLD_DATASETS:
        if dataset['language_id'] == language_id:
            dataset['is_deleted'] = True
    
    print(f"SUCCESS: Language marked as deleted from demo storage")

def build_task_suggestions(languages: list, fallback_languages: list) -> list:
    """Return a sorted list of task suggestions from active languages plus defaults."""
    tasks = set()
    source = languages if languages else fallback_languages
    for lang in source:
        if lang.get('is_deleted'):
            continue
        task = lang.get('task')
        if task:
            tasks.add(task)
    for default_task in DEFAULT_TASK_SUGGESTIONS:
        tasks.add(default_task)
    return sorted(tasks)

def make_json_safe(records: list) -> list:
    """Return a version of the records list that is safe to JSON encode (datetime -> string)."""
    safe_records = []
    for record in records or []:
        if isinstance(record, dict):
            converted = {}
            for key, value in record.items():
                if isinstance(value, (datetime, timedelta)):
                    converted[key] = value.isoformat()
                elif isinstance(value, Decimal):
                    converted[key] = float(value)
                else:
                    converted[key] = value
            safe_records.append(converted)
        else:
            safe_records.append(record)
    return safe_records

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: dict = Depends(get_current_user)):
    # Check if user is admin using is_admin flag or username
    is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
    
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Demo fallbacks for offline/empty database use
    demo_users = [
        {
            'id': info['id'],
            'username': info['username'],
            'email': info['email'],
            'is_active': info.get('is_active', True),
            'team_name': info.get('team_name'),
            'is_admin': info.get('is_admin', False),
            'created_at': 'Demo'
        }
        for info in DEMO_USERS.values()
    ]
    demo_languages = [lang for lang in DEMO_LANGUAGES if not lang.get('is_deleted')]
    demo_gold_datasets = [ds for ds in DEMO_GOLD_DATASETS if not ds.get('is_deleted')]
    demo_recent_activities = []
    for activity in DEMO_ACTIVITY_LOGS:
        if not activity.get('language_task') and activity.get('language_id'):
            lang_obj = next((lang for lang in demo_languages if lang['id'] == activity.get('language_id')), None)
            if lang_obj:
                activity['language_task'] = lang_obj.get('task')
        display_name = activity.get('username', 'Unknown')
        team_name = activity.get('team_name')
        if team_name:
            display_name = f"{display_name} ({team_name})"
        if activity.get('created_at') and hasattr(activity['created_at'], 'strftime'):
            formatted_date = activity['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        else:
            formatted_date = str(activity.get('created_at', 'N/A'))
        demo_recent_activities.append({**activity, 'formatted_date': formatted_date, 'display_name': display_name})
    
    # Get languages from database
    conn = get_db_connection()
    users = []
    languages = []
    gold_datasets = []
    recent_activities = []
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get all users (active and inactive)
            cursor.execute("""
                SELECT id, username, email, is_active, team_name, is_admin, created_at
                FROM users 
                ORDER BY created_at DESC
            """)
            users = cursor.fetchall()
            
            # Exclude deleted languages
            cursor.execute("SELECT * FROM languages WHERE is_deleted = FALSE ORDER BY language_name")
            languages = cursor.fetchall()
            
            # Get gold datasets (only non-deleted)
            cursor.execute("""
                SELECT gd.*, l.language_name, l.task as language_task
                FROM gold_datasets gd 
                JOIN languages l ON gd.language_id = l.id 
                WHERE gd.is_deleted = FALSE AND l.is_deleted = FALSE
                ORDER BY gd.created_at DESC
            """)
            gold_datasets = cursor.fetchall()
            
            # Get recent activity logs (last 20)
            cursor.execute("""
                SELECT al.*, u.username, u.team_name, u.is_admin, l.task as language_task, l.language_name
                FROM activity_logs al
                JOIN users u ON al.user_id = u.id
                LEFT JOIN languages l ON al.language_id = l.id
                ORDER BY al.created_at DESC
                LIMIT 20
            """)
            recent_activities = cursor.fetchall()
            
            # Format timestamps for display
            for activity in recent_activities:
                if activity.get('created_at') and hasattr(activity['created_at'], 'strftime'):
                    activity['formatted_date'] = activity['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                else:
                    activity['formatted_date'] = str(activity.get('created_at', 'N/A'))
                if not activity.get('language_used') and activity.get('language_name'):
                    activity['language_used'] = activity.get('language_name')
                display_name = activity.get('username', 'Unknown')
                if activity.get('team_name'):
                    display_name = f"{display_name} ({activity['team_name']})"
                activity['display_name'] = display_name
            
            if not recent_activities:
                recent_activities = demo_recent_activities
            
            conn.close()
        except Exception as e:
            print(f"Database error getting admin data: {e}")
            users = demo_users
            languages = demo_languages
            gold_datasets = demo_gold_datasets
            recent_activities = demo_recent_activities
            if conn:
                conn.close()
    else:
        users = demo_users
        languages = demo_languages
        gold_datasets = demo_gold_datasets
        recent_activities = demo_recent_activities

    # Enrich demo gold_datasets with task when using demo or when DB missing
    if gold_datasets:
        lang_task_map = {lang['id']: lang.get('task') for lang in languages}
        for ds in gold_datasets:
            if not ds.get('language_task'):
                ds['language_task'] = lang_task_map.get(ds.get('language_id'))

    task_suggestions = build_task_suggestions(languages, demo_languages)
    # Stats: distinct tasks and distinct language codes (ignoring task)
    source_langs = languages if languages else demo_languages
    unique_tasks = set()
    unique_codes = set()
    for lang in source_langs:
        if lang.get('is_deleted'):
            continue
        code = lang.get('language_code')
        task = lang.get('task')
        if code:
            unique_codes.add(code.lower())
        if task:
            unique_tasks.add(task)
    tasks_count = len(unique_tasks) if unique_tasks else len(task_suggestions)
    languages_unique_count = len(unique_codes)
    languages_json = make_json_safe(languages)
    gold_datasets_json = make_json_safe(gold_datasets)

    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "users": users,
        "languages": languages,
        "languages_json": languages_json,
        "gold_datasets": gold_datasets,
        "gold_datasets_json": gold_datasets_json,
        "recent_activities": recent_activities,
        "scorer_exists": False,  # Removed scorer functionality
        "task_suggestions": task_suggestions,
        "tasks_count": tasks_count,
        "languages_unique_count": languages_unique_count
    })

@app.post("/admin/add_user", name="add_user")
async def add_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    team_name: str = Form(None),
    is_admin: bool = Form(False),
    user: dict = Depends(get_current_user)
):
    if not (user.get('is_admin', False) or user.get('username') == 'admin'):
        raise HTTPException(status_code=403, detail="Admin access required")

    username_clean = (username or "").strip()
    email_clean = (email or "").strip()
    if not username_clean or not email_clean:
        return redirect_to_admin(tab="users", user_error="Username and email are required.")

    # Duplicate checks for demo mode
    for demo_user in DEMO_USERS.values():
        if not demo_user.get('is_active', True):
            continue
        if demo_user.get('username') == username_clean:
            return redirect_to_admin(tab="users", user_error="Username already exists.")
        if demo_user.get('email') == email_clean:
            return redirect_to_admin(tab="users", user_error="Email already exists.")
    
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    
    # Don't allow team_name for admin users
    if is_admin:
        team_name = None
    
    # Add to database or demo users
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            # Duplicate checks in database
            cursor.execute("SELECT id FROM users WHERE username = %s AND is_active = 1", (username_clean,))
            if cursor.fetchone():
                conn.close()
                return redirect_to_admin(tab="users", user_error="Username already exists.")
            cursor.execute("SELECT id FROM users WHERE email = %s AND is_active = 1", (email_clean,))
            if cursor.fetchone():
                conn.close()
                return redirect_to_admin(tab="users", user_error="Email already exists.")

            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, is_active, team_name, is_admin) VALUES (%s, %s, %s, %s, %s, %s)",
                (username_clean, email_clean, password_hash, True, team_name, is_admin)
            )
            conn.commit()
            conn.close()
            print(f"SUCCESS: User {username_clean} added to database (Admin: {is_admin})")
            log_activity(user['id'], 'user_added')
        except Exception as e:
            print(f"ERROR adding user to database: {e}")
            if conn:
                conn.close()
            # Fallback to demo users
            DEMO_USERS[username] = {
                'id': len(DEMO_USERS) + 1,
                'username': username_clean,
                'password_hash': password_hash,
                'email': email_clean,
                'is_active': True,
                'team_name': team_name,
                'is_admin': is_admin
            }
            print(f"SUCCESS: User {username_clean} added to demo storage (Admin: {is_admin})")
            log_activity(user['id'], 'user_added')
    else:
        # Add to demo users
        DEMO_USERS[username] = {
            'id': len(DEMO_USERS) + 1,
            'username': username_clean,
            'password_hash': password_hash,
            'email': email_clean,
            'is_active': True,
            'team_name': team_name,
            'is_admin': is_admin
        }
        print(f"SUCCESS: User {username_clean} added to demo storage (Admin: {is_admin})")
        log_activity(user['id'], 'user_added')
    
    return redirect_to_admin()

@app.post("/admin/update_user/{user_id}", name="update_user")
async def update_user(
    request: Request,
    user_id: int,
    username: str = Form(...),
    email: str = Form(...),
    is_active: str = Form("false"),
    user: dict = Depends(get_current_user)
):
    if not (user.get('is_admin', False) or user.get('username') == 'admin'):
        raise HTTPException(status_code=403, detail="Admin access required")

    username_clean = (username or "").strip()
    email_clean = (email or "").strip()
    active_flag = str(is_active).lower() in ("true", "1", "yes", "on")

    if not username_clean or not email_clean:
        return redirect_to_admin(tab="users", user_error="Username and email are required.")

    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            target_user = cursor.fetchone()
            if not target_user:
                conn.close()
                return redirect_to_admin(tab="users", user_error="User not found.")

            if target_user.get('username') == 'admin' and not active_flag:
                conn.close()
                return redirect_to_admin(tab="users", user_error="Primary admin cannot be deactivated.")

            cursor.execute(
                "SELECT id FROM users WHERE username = %s AND id != %s AND is_active = 1",
                (username_clean, user_id)
            )
            if cursor.fetchone():
                conn.close()
                return redirect_to_admin(tab="users", user_error="Username already exists.")

            cursor.execute(
                "SELECT id FROM users WHERE email = %s AND id != %s AND is_active = 1",
                (email_clean, user_id)
            )
            if cursor.fetchone():
                conn.close()
                return redirect_to_admin(tab="users", user_error="Email already exists.")

            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET username = %s, email = %s, is_active = %s WHERE id = %s",
                (username_clean, email_clean, active_flag, user_id)
            )
            conn.commit()
            conn.close()
            print(f"SUCCESS: User {user_id} updated (username: {username_clean})")
            log_activity(user['id'], 'user_updated', details=f"target_id={user_id}")
            return redirect_to_admin(tab="users")
        except Exception as e:
            print(f"ERROR updating user in database: {e}")
            if conn:
                conn.close()
            # Fall through to demo update
    # Demo duplicate checks
    for info in DEMO_USERS.values():
        if info.get('id') == user_id:
            continue
        if not info.get('is_active', True):
            continue
        if info.get('username') == username_clean:
            return redirect_to_admin(tab="users", user_error="Username already exists.")
        if info.get('email') == email_clean:
            return redirect_to_admin(tab="users", user_error="Email already exists.")

    # Demo update fallback
    updated = False
    for existing_username, info in list(DEMO_USERS.items()):
        if info.get('id') == user_id:
            if existing_username == 'admin' and not active_flag:
                return redirect_to_admin(tab="users", user_error="Primary admin cannot be deactivated.")
            info['username'] = username_clean
            info['email'] = email_clean
            info['is_active'] = active_flag
            if existing_username != username_clean:
                # maintain dict keyed by username
                DEMO_USERS.pop(existing_username, None)
                DEMO_USERS[username_clean] = info
            updated = True
            print(f"DEMO: User {user_id} updated (username: {username_clean})")
            log_activity(user['id'], 'user_updated', details=f"target_id={user_id}")
            break

    if not updated:
        return redirect_to_admin(tab="users", user_error="User not found.")

    return redirect_to_admin(tab="users")

@app.post("/admin/delete_user/{user_id}", name="delete_user")
async def delete_user(
    request: Request,
    user_id: int,
    user: dict = Depends(get_current_user)
):
    # Check if user is admin
    is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Prevent deleting yourself
    if user['id'] == user_id:
        return redirect_to_admin(tab="users", user_error="Cannot delete your own account.")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get user info before hard delete
            cursor.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            user_to_delete = cursor.fetchone()
            
            if not user_to_delete:
                conn.close()
                return redirect_to_admin(tab="users", user_error="User not found.")

            if user_to_delete.get('username') == 'admin':
                conn.close()
                return redirect_to_admin(tab="users", user_error="Cannot delete primary admin account.")
            
            # Hard delete user
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            
            conn.commit()
            conn.close()
            print(f"SUCCESS: User '{user_to_delete['username']}' deleted (ID: {user_id})")
            log_activity(user['id'], 'user_deleted', details=f"target_id={user_id}")
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR deleting user from database: {e}")
            if conn:
                conn.close()
            # Fall through to demo delete
    # Demo delete fallback
    demo_deleted = False
    for existing_username in list(DEMO_USERS.keys()):
        info = DEMO_USERS[existing_username]
        if info.get('id') == user_id:
            if info.get('username') == 'admin':
                return redirect_to_admin(tab="users", user_error="Cannot delete primary admin account.")
            DEMO_USERS.pop(existing_username, None)
            print(f"DEMO: User '{existing_username}' deleted (ID: {user_id})")
            log_activity(user['id'], 'user_deleted', details=f"target_id={user_id}")
            demo_deleted = True
            break

    if not demo_deleted and not conn:
        return redirect_to_admin(tab="users", user_error="User not found.")
    
    return redirect_to_admin(tab="users")

@app.post("/admin/upload_gold_dataset",name="upload_gold_dataset")
async def upload_gold_dataset(
    request: Request,
    language_id: int = Form(...),
    task: str = Form(None),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    # Check if user is admin
    is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="Only .txt files allowed")
    
    try:
        task_normalized = normalize_task(task) if task else "Coreference"
        
        # Create language-specific directory
        lang_dir = Path("gold_datasets") / f"lang_{language_id}"
        lang_dir.mkdir(exist_ok=True)
        
        # Get the next version number for this language
        conn = get_db_connection()
        next_version = 1
        existing_dataset = False
        
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                if task_normalized:
                    cursor.execute(
                        "SELECT MAX(version) as max_version FROM gold_datasets WHERE language_id = %s AND task = %s AND is_deleted = FALSE",
                        (language_id, task_normalized)
                    )
                else:
                    cursor.execute(
                        "SELECT MAX(version) as max_version FROM gold_datasets WHERE language_id = %s AND is_deleted = FALSE",
                        (language_id,)
                    )
                result = cursor.fetchone()
                if result and result['max_version'] is not None:
                    existing_dataset = True
                    next_version = int(result['max_version']) + 1
                conn.close()
            except Exception as e:
                print(f"Error getting version: {e}")
                if conn:
                    conn.close()
            existing_dataset = demo_dataset_exists(language_id, task_normalized)
        else:
            existing_dataset = demo_dataset_exists(language_id, task_normalized)
        
        if existing_dataset:
            return redirect_to_admin(tab="datasets", dataset_error="Dataset already exists for this language and task.")
        
        # Save file with timestamp and version
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_path = lang_dir / f"v{next_version}_{timestamp}_{file.filename}"
        
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
        
        print(f"GOLD DATASET SAVED: {file_path} (Version {next_version})")
        
        # Save to database or demo data
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO gold_datasets (language_id, filename, file_path, uploaded_by, is_deleted, version, task) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (language_id, file.filename, str(file_path), user['username'], False, next_version, task_normalized or "Coreference")
                )
                conn.commit()
                conn.close()
                print(f"SUCCESS: Gold dataset saved to database (Version {next_version}): {file.filename}")
            except Exception as e:
                print(f"ERROR saving to database: {e}")
                if conn:
                    conn.close()
                # Fallback to demo data
                add_to_demo_datasets(language_id, file.filename, str(file_path), user['username'], next_version, task_normalized)
        else:
            # Save to demo data
            add_to_demo_datasets(language_id, file.filename, str(file_path), user['username'], next_version, task_normalized)

        # Log activity
        log_activity(user['id'], 'gold_dataset_uploaded', language_id, file.filename)
        
        return redirect_to_admin(tab="datasets")

    except Exception as e:
        print(f"ERROR uploading gold dataset: {e}")
        return redirect_to_admin(tab="datasets", dataset_error=f"Upload failed: {str(e)}")

@app.post("/admin/delete_gold_dataset/{dataset_id}")
async def delete_gold_dataset(
    request: Request,
    dataset_id: int,
    user: dict = Depends(get_current_user)
):
    # Check if user is admin
    is_admin = user.get('is_admin', False) or user.get('username') == 'admin'
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get the dataset info before marking as deleted
            cursor.execute("SELECT filename, language_id, task FROM gold_datasets WHERE id = %s", (dataset_id,))
            dataset = cursor.fetchone()
            
            if not dataset:
                conn.close()
                raise HTTPException(status_code=404, detail="Gold dataset not found")
            
            filename = dataset['filename']
            
            # Soft delete: Mark dataset as deleted instead of hard delete
            cursor.execute(
                "UPDATE gold_datasets SET is_deleted = TRUE WHERE id = %s",
                (dataset_id,)
            )
            
            conn.commit()
            conn.close()
            print(f"SUCCESS: Gold dataset '{filename}' marked as deleted (ID: {dataset_id})")
            log_activity(user['id'], 'gold_dataset_deleted', dataset.get('language_id'), filename)
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR deleting gold dataset from database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            target_lang = dataset.get('language_id') if dataset else None
            delete_from_demo_datasets(dataset_id)
            log_activity(user['id'], 'gold_dataset_deleted', target_lang, filename if 'filename' in locals() else None)
    else:
        # Delete from demo storage
        demo_dataset = next((ds for ds in DEMO_GOLD_DATASETS if ds.get('id') == dataset_id), None)
        target_lang = demo_dataset.get('language_id') if demo_dataset else None
        filename = demo_dataset.get('filename') if demo_dataset else None
        delete_from_demo_datasets(dataset_id)
        log_activity(user['id'], 'gold_dataset_deleted', target_lang, filename)

    return redirect_to_admin(tab="datasets")

def delete_from_demo_datasets(dataset_id: int):
    """Soft delete gold dataset from demo storage"""
    global DEMO_GOLD_DATASETS
    
    # Find the dataset
    dataset = next((ds for ds in DEMO_GOLD_DATASETS if ds['id'] == dataset_id), None)
    
    if not dataset:
        raise HTTPException(status_code=404, detail="Gold dataset not found")
    
    # Soft delete: Mark dataset as deleted (don't remove from list)
    for ds in DEMO_GOLD_DATASETS:
        if ds['id'] == dataset_id:
            ds['is_deleted'] = True
            break
    
    print(f"SUCCESS: Gold dataset marked as deleted from demo storage (ID: {dataset_id})")

def add_to_demo_datasets(language_id: int, filename: str, file_path: str, uploaded_by: str, version: int = 1, task: str = None):
    """Add gold dataset to demo data"""
    lang_obj = next((lang for lang in DEMO_LANGUAGES if lang['id'] == language_id), {})
    language_name = lang_obj.get('language_name', 'Unknown')
    language_task = task or lang_obj.get('task', 'Coreference')
    
    dataset = {
        'id': len(DEMO_GOLD_DATASETS) + 1,
        'language_id': language_id,
        'language_name': language_name,
        'language_task': language_task,
        'task': language_task,
        'filename': filename,
        'file_path': file_path,
        'uploaded_by': uploaded_by,
        'version': version,
        'is_deleted': False,
        'created_at': datetime.now()
    }
    
    DEMO_GOLD_DATASETS.append(dataset)
    print(f"SUCCESS: Gold dataset added to demo data (Version {version}): {filename}")

def demo_dataset_exists(language_id: int, task: str | None) -> bool:
    """Return True if a non-deleted demo dataset exists for the language/task."""
    target_task = task or "Coreference"
    for ds in DEMO_GOLD_DATASETS:
        if ds.get('is_deleted'):
            continue
        if ds.get('language_id') == language_id and (ds.get('task') or ds.get('language_task') or "Coreference") == target_task:
            return True
    return False

if __name__ == "__main__":
    import uvicorn
    print("Starting Coreference Evaluation System...")
    print("Demo credentials:")
    print("  Admin: admin/admin123")
    print("  User: testuser/user123")
    print("Access at: http://localhost:8000")
    print()
    print("System checks:")
    print(f"  Perl available: {check_perl_availability()}")
    if not check_perl_availability():
        print("  Install Perl from: https://strawberryperl.com/ (recommended for Windows)")
    print(f"  Database connection: {'OK' if get_db_connection() else 'Failed (using demo mode)'}")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000)
