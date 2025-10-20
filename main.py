from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, File, UploadFile, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import mysql.connector
import bcrypt
import os
import secrets
import subprocess
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
import re
import platform

app = FastAPI(root_path="/discours-leaderboard")
# Create directories
Path("templates").mkdir(exist_ok=True)
Path("uploads").mkdir(exist_ok=True)
Path("gold_datasets").mkdir(exist_ok=True)
Path("scorer").mkdir(exist_ok=True)

templates = Jinja2Templates(directory="templates")

# Session storage (in production, use Redis or database)
active_sessions = {}
SECRET_KEY = secrets.token_urlsafe(32)

# Database config
DB_CONFIG = {
    'host': 'localhost',
    'database': 'coref_eval_system',
    'user': 'irlab',
    'password': 'irlab'
}

# Demo data - expanded to include evaluation history
DEMO_USERS = {
    'admin': {'id': 1, 'username': 'admin', 'password_hash': bcrypt.hashpw('admin123'.encode(), bcrypt.gensalt()).decode(), 'email': 'admin@test.com', 'is_active': True},
    'testuser': {'id': 2, 'username': 'testuser', 'password_hash': bcrypt.hashpw('user123'.encode(), bcrypt.gensalt()).decode(), 'email': 'user@test.com', 'is_active': True}
}

DEMO_LANGUAGES = [
    {'id': 1, 'language_code': 'hi', 'language_name': 'Hindi'},
]

# Demo storage for evaluations and gold datasets
DEMO_GOLD_DATASETS = []
DEMO_EVALUATIONS = []

def get_db_connection():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Exception as e:
        print(f"Database connection failed: {e}")
        return None

def get_current_user(session_token: str = Cookie(None)):
    if not session_token or session_token not in active_sessions:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    user_info = active_sessions[session_token]
    return user_info

def authenticate_user(username: str, password: str):
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE username = %s AND is_active = 1", (username,))
            user = cursor.fetchone()
            conn.close()
        except Exception as e:
            print(f"Database authentication error: {e}")
            user = None
            if conn:
                conn.close()
    else:
        user = DEMO_USERS.get(username)
    
    if not user:
        return None
    
    if not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
        return None
    
    return user

def find_gold_dataset(language_id: int):
    """Find the gold dataset for a given language"""
    conn = get_db_connection()
    
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM gold_datasets WHERE language_id = %s ORDER BY created_at DESC LIMIT 1", (language_id,))
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
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%', line)
                
                if recall_match and precision_match and f1_match:
                    scores['muc'] = {
                        'recall': float(recall_match.group(1)) / 100,
                        'precision': float(precision_match.group(1)) / 100,
                        'f1': float(f1_match.group(1)) / 100
                    }
                    print(f"PARSED MUC (Identification): R={scores['muc']['recall']}, P={scores['muc']['precision']}, F1={scores['muc']['f1']}")
            
            # Parse Coreference links (this is like B-CUBED)
            elif "Coreference links:" in line:
                # Format: "Coreference links: Recall: (602 / 602) 100%       Precision: (602 / 602) 100%     F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%', line)
                
                if recall_match and precision_match and f1_match:
                    scores['bcub'] = {
                        'recall': float(recall_match.group(1)) / 100,
                        'precision': float(precision_match.group(1)) / 100,
                        'f1': float(f1_match.group(1)) / 100
                    }
                    print(f"PARSED B-CUBED (Coreference): R={scores['bcub']['recall']}, P={scores['bcub']['precision']}, F1={scores['bcub']['f1']}")
            
            # Parse Non-coreference links (additional metric)
            elif "Non-coreference links:" in line:
                # Format: "Non-coreference links: Recall: (3200 / 3200) 100% Precision: (3200 / 3200) 100%   F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%', line)
                
                if recall_match and precision_match and f1_match:
                    scores['ceafm'] = {
                        'recall': float(recall_match.group(1)) / 100,
                        'precision': float(precision_match.group(1)) / 100,
                        'f1': float(f1_match.group(1)) / 100
                    }
                    print(f"PARSED CEAF-M (Non-coreference): R={scores['ceafm']['recall']}, P={scores['ceafm']['precision']}, F1={scores['ceafm']['f1']}")
            
            # Parse BLANC
            elif "BLANC:" in line:
                # Format: "BLANC: Recall: (1 / 1) 100%       Precision: (1 / 1) 100% F1: 100%"
                recall_match = re.search(r'Recall:\s*\([^)]+\)\s*([\d.]+)%', line)
                precision_match = re.search(r'Precision:\s*\([^)]+\)\s*([\d.]+)%', line)
                f1_match = re.search(r'F1:\s*([\d.]+)%', line)
                
                if recall_match and precision_match and f1_match:
                    scores['blanc'] = {
                        'recall': float(recall_match.group(1)) / 100,
                        'precision': float(precision_match.group(1)) / 100,
                        'f1': float(f1_match.group(1)) / 100
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

def save_evaluation_results(user_id: int, language_id: int, filename: str, file_path: str, scores: dict):
    """Save evaluation results to database or demo storage"""
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
    
    evaluation = {
        'id': len(DEMO_EVALUATIONS) + 1,
        'user_id': user_id,
        'language_id': language_id,
        'language_name': language_name,
        'uploaded_filename': filename,
        'file_path': file_path,
        'formatted_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'muc_f1': scores.get('muc', {}).get('f1'),
        'bcub_f1': scores.get('bcub', {}).get('f1'),
        'ceafm_f1': scores.get('ceafm', {}).get('f1'),
        'ceafe_f1': scores.get('ceafe', {}).get('f1'),
        'blanc_f1': scores.get('blanc', {}).get('f1'),
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
            # Fixed SQL query - removed DATE_FORMAT which might cause parameter issues
            cursor.execute("""
                SELECT ue.*, l.language_name, ue.created_at as formatted_date
                FROM user_evaluations ue
                JOIN languages l ON ue.language_id = l.id
                WHERE ue.user_id = %s
                ORDER BY ue.created_at DESC
                LIMIT 20
            """, (user_id,))
            history = cursor.fetchall()
            
            # Format dates in Python instead of SQL
            for record in history:
                if record['formatted_date']:
                    record['formatted_date'] = record['formatted_date'].strftime('%Y-%m-%d %H:%M:%S')
            
            conn.close()
            print(f"SUCCESS: Retrieved {len(history)} evaluation records from database")
            return history
        except Exception as e:
            print(f"ERROR retrieving history from database: {e}")
            if conn:
                conn.close()
    
    # Fallback to demo data
    history = [eval for eval in DEMO_EVALUATIONS if eval['user_id'] == user_id]
    history.sort(key=lambda x: x['created_at'], reverse=True)
    print(f"SUCCESS: Retrieved {len(history)} evaluation records from demo storage")
    return history[:20]
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
            
            # Get all languages
            cursor.execute("SELECT * FROM languages ORDER BY language_name")
            languages = cursor.fetchall()
            
            for language in languages:
                language_data = {
                    'language_id': language['id'],
                    'language_name': language['language_name'],
                    'language_code': language['language_code'],
                    'top_scores': []
                }
                
                # Get ALL scores for this language (not just top 3)
                cursor.execute("""
                    SELECT ue.*, u.username,
                           ((COALESCE(ue.muc_f1, 0) + COALESCE(ue.bcub_f1, 0) + 
                             COALESCE(ue.ceafm_f1, 0) + COALESCE(ue.blanc_f1, 0)) / 4) as avg_f1
                    FROM user_evaluations ue
                    JOIN users u ON ue.user_id = u.id
                    WHERE ue.language_id = %s
                    AND u.is_active = 1
                    ORDER BY avg_f1 DESC
                """, (language['id'],))
                
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
        language_data = {
            'language_id': language['id'],
            'language_name': language['language_name'],
            'language_code': language['language_code'],
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
                language_evaluations.append(eval_copy)
        
        # Sort by average F1 score (calculate from available metrics)
        for eval in language_evaluations:
            scores = []
            if eval.get('muc_f1'): scores.append(float(eval['muc_f1']))
            if eval.get('bcub_f1'): scores.append(float(eval['bcub_f1']))
            if eval.get('ceafm_f1'): scores.append(float(eval['ceafm_f1']))
            if eval.get('blanc_f1'): scores.append(float(eval['blanc_f1']))
            
            eval['avg_f1'] = sum(scores) / len(scores) if scores else 0
            
            # Ensure all values are float, not Decimal
            for key in eval:
                if key != 'created_at' and isinstance(eval[key], (int, float)):
                    eval[key] = float(eval[key])
        
        # Sort by average F1 and get ALL scores
        language_evaluations.sort(key=lambda x: x.get('avg_f1', 0), reverse=True)
        language_data['top_scores'] = language_evaluations
        
        leaderboards.append(language_data)
    
    print(f"SUCCESS: Retrieved demo leaderboards for {len(DEMO_LANGUAGES)} languages")
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
        # Get homepage statistics
        stats = get_homepage_statistics()
        
        # Get language leaderboards
        leaderboards = get_language_leaderboards()
        
        return templates.TemplateResponse("homepage.html", {
            "request": request,
            "stats": stats,
            "leaderboards": leaderboards
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
            "leaderboards": get_demo_leaderboards()
        })
@app.get("/home", response_class=HTMLResponse) 
async def home_redirect(request: Request):
    """Redirect /home to login for backward compatibility"""
    return RedirectResponse(url="/", status_code=302)
@app.get("/login", response_class=HTMLResponse, name=)
async def login_page(request: Request):
    """Display the login page"""
    return templates.TemplateResponse("login.html", {"request": request})
@app.post("/login",name="login_page")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = authenticate_user(username, password)
    
    if not user:
        return templates.TemplateResponse("login.html", {
            "request": request, 
            "error": "Invalid username or password"
        })
    
    # Create session
    session_token = secrets.token_urlsafe(32)
    active_sessions[session_token] = user
    
    # Redirect based on user type
    redirect_url = "/admin" if user['username'] == 'admin' else "/client"
    response = RedirectResponse(url=redirect_url, status_code=302)
    response.set_cookie(key="session_token", value=session_token, httponly=True, max_age=3600)
    
    print(f"SUCCESS: User {username} logged in successfully")
    return response

@app.get("/logout",name="logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(key="session_token")
    return response

@app.get("/client", response_class=HTMLResponse)
async def client_dashboard(request: Request, user: dict = Depends(get_current_user)):
    if user['username'] == 'admin':
        return RedirectResponse(url="/admin", status_code=302)
    
    # Get languages from database
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM languages ORDER BY language_name")
            languages = cursor.fetchall()
            conn.close()
        except Exception as e:
            print(f"Database error getting languages: {e}")
            languages = DEMO_LANGUAGES
            if conn:
                conn.close()
    else:
        languages = DEMO_LANGUAGES
    
    # Get user's evaluation history
    history = get_user_evaluation_history(user['id'])
    
    return templates.TemplateResponse("client_dashboard.html", {
        "request": request,
        "user": user,
        "languages": languages,
        "history": history
    })

@app.post("/evaluate", name="evaluate")
async def evaluate_file(
    language_id: int = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="Only .txt files allowed")
    
    print(f"STARTING EVALUATION: User {user['username']}, Language ID {language_id}, File {file.filename}")
    
    try:
        # Find gold dataset for the language
        gold_dataset = find_gold_dataset(language_id)
        if not gold_dataset:
            raise HTTPException(status_code=400, detail=f"No gold dataset found for language ID {language_id}. Please upload a gold dataset first.")
        
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
        
        # Run evaluation with actual Perl script
        scores = run_perl_scorer(gold_dataset['file_path'], str(upload_path))
        
        # Save results to database/demo storage
        save_evaluation_results(user['id'], language_id, file.filename, str(upload_path), scores)
        
        print(f"EVALUATION COMPLETE: {file.filename}")
        return {"success": True, "scores": scores, "message": "Evaluation completed successfully"}
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR during evaluation: {e}")
        raise HTTPException(status_code=500, detail=f"Evaluation failed: {str(e)}")
@app.post("/admin/add_language", name="admin_add_language")
async def add_language(
    language_code: str = Form(...),
    language_name: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Validate language code (basic validation)
    language_code = language_code.strip().lower()
    language_name = language_name.strip()
    
    if not language_code or not language_name:
        raise HTTPException(status_code=400, detail="Language code and name are required")
    
    if len(language_code) > 10:
        raise HTTPException(status_code=400, detail="Language code must be 10 characters or less")
    
    # Check if language code already exists
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id FROM languages WHERE language_code = %s", (language_code,))
            existing = cursor.fetchone()
            
            if existing:
                conn.close()
                raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists")
            
            # Insert new language
            cursor.execute(
                "INSERT INTO languages (language_code, language_name) VALUES (%s, %s)",
                (language_code, language_name)
            )
            conn.commit()
            conn.close()
            print(f"SUCCESS: Language {language_name} ({language_code}) added to database")
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR adding language to database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            add_to_demo_languages(language_code, language_name)
    else:
        # Add to demo storage
        add_to_demo_languages(language_code, language_name)
    
    return RedirectResponse(url="/admin", status_code=302)

@app.post("/admin/update_language/{language_id}")
async def update_language(
    language_id: int,
    language_code: str = Form(...),
    language_name: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Validate input
    language_code = language_code.strip().lower()
    language_name = language_name.strip()
    
    if not language_code or not language_name:
        raise HTTPException(status_code=400, detail="Language code and name are required")
    
    if len(language_code) > 10:
        raise HTTPException(status_code=400, detail="Language code must be 10 characters or less")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Check if language code already exists for different language
            cursor.execute("SELECT id FROM languages WHERE language_code = %s AND id != %s", (language_code, language_id))
            existing = cursor.fetchone()
            
            if existing:
                conn.close()
                raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists")
            
            # Update language
            cursor.execute(
                "UPDATE languages SET language_code = %s, language_name = %s WHERE id = %s",
                (language_code, language_name, language_id)
            )
            conn.commit()
            conn.close()
            print(f"SUCCESS: Language updated to {language_name} ({language_code})")
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR updating language in database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            update_demo_language(language_id, language_code, language_name)
    else:
        # Update demo storage
        update_demo_language(language_id, language_code, language_name)
    
    return RedirectResponse(url="/admin", status_code=302)

@app.post("/admin/delete_language/{lang_id}", name="admin_delete_language")
async def delete_language(
    language_id: int,
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            
            # First delete associated gold datasets
            cursor.execute("DELETE FROM gold_datasets WHERE language_id = %s", (language_id,))
            
            # Then delete the language
            cursor.execute("DELETE FROM languages WHERE id = %s", (language_id,))
            
            conn.commit()
            conn.close()
            print(f"SUCCESS: Language and associated datasets deleted from database")
        except Exception as e:
            print(f"ERROR deleting language from database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            delete_from_demo_languages(language_id)
    else:
        # Delete from demo storage
        delete_from_demo_languages(language_id)
    
    return RedirectResponse(url="/admin", status_code=302)

# Helper functions for demo language management
def add_to_demo_languages(language_code: str, language_name: str):
    """Add language to demo storage"""
    global DEMO_LANGUAGES
    
    # Check if code already exists
    for lang in DEMO_LANGUAGES:
        if lang['language_code'] == language_code:
            raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists")
    
    new_id = max([lang['id'] for lang in DEMO_LANGUAGES], default=0) + 1
    new_language = {
        'id': new_id,
        'language_code': language_code,
        'language_name': language_name
    }
    
    DEMO_LANGUAGES.append(new_language)
    print(f"SUCCESS: Language {language_name} ({language_code}) added to demo storage")

def update_demo_language(language_id: int, language_code: str, language_name: str):
    """Update language in demo storage"""
    global DEMO_LANGUAGES
    
    # Check if code already exists for different language
    for lang in DEMO_LANGUAGES:
        if lang['language_code'] == language_code and lang['id'] != language_id:
            raise HTTPException(status_code=400, detail=f"Language code '{language_code}' already exists")
    
    # Find and update the language
    for i, lang in enumerate(DEMO_LANGUAGES):
        if lang['id'] == language_id:
            DEMO_LANGUAGES[i]['language_code'] = language_code
            DEMO_LANGUAGES[i]['language_name'] = language_name
            print(f"SUCCESS: Language updated to {language_name} ({language_code}) in demo storage")
            return
    
    raise HTTPException(status_code=404, detail="Language not found")

def delete_from_demo_languages(language_id: int):
    """Delete language from demo storage"""
    global DEMO_LANGUAGES, DEMO_GOLD_DATASETS
    
    # Remove associated gold datasets
    DEMO_GOLD_DATASETS[:] = [dataset for dataset in DEMO_GOLD_DATASETS if dataset['language_id'] != language_id]
    
    # Remove the language
    original_count = len(DEMO_LANGUAGES)
    DEMO_LANGUAGES[:] = [lang for lang in DEMO_LANGUAGES if lang['id'] != language_id]
    
    if len(DEMO_LANGUAGES) < original_count:
        print(f"SUCCESS: Language and associated datasets deleted from demo storage")
    else:
        raise HTTPException(status_code=404, detail="Language not found")
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: dict = Depends(get_current_user)):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Get languages from database
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM languages ORDER BY language_name")
            languages = cursor.fetchall()
            
            # Get gold datasets
            cursor.execute("""
                SELECT gd.*, l.language_name 
                FROM gold_datasets gd 
                JOIN languages l ON gd.language_id = l.id 
                ORDER BY gd.created_at DESC
            """)
            gold_datasets = cursor.fetchall()
            conn.close()
        except Exception as e:
            print(f"Database error getting admin data: {e}")
            languages = DEMO_LANGUAGES
            gold_datasets = DEMO_GOLD_DATASETS
            if conn:
                conn.close()
    else:
        languages = DEMO_LANGUAGES
        gold_datasets = DEMO_GOLD_DATASETS
    
    return templates.TemplateResponse("admin_dashboard.html", {
        "request": request,
        "users": [],  # You can implement user management if needed
        "languages": languages,
        "gold_datasets": gold_datasets,
        "recent_activities": [],  # You can implement activity logging if needed
        "scorer_exists": False  # Removed scorer functionality
    })

@app.post("/admin/add_user", name="admin_add_user")
async def add_user(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    
    # Add to database or demo users
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, is_active) VALUES (%s, %s, %s, %s)",
                (username, email, password_hash, True)
            )
            conn.commit()
            conn.close()
            print(f"SUCCESS: User {username} added to database")
        except Exception as e:
            print(f"ERROR adding user to database: {e}")
            if conn:
                conn.close()
            # Fallback to demo users
            DEMO_USERS[username] = {
                'id': len(DEMO_USERS) + 1,
                'username': username,
                'password_hash': password_hash,
                'email': email,
                'is_active': True
            }
            print(f"SUCCESS: User {username} added to demo storage")
    else:
        # Add to demo users
        DEMO_USERS[username] = {
            'id': len(DEMO_USERS) + 1,
            'username': username,
            'password_hash': password_hash,
            'email': email,
            'is_active': True
        }
        print(f"SUCCESS: User {username} added to demo storage")
    
    return RedirectResponse(url="/admin", status_code=302)

@app.post("/admin/upload_gold_dataset", name="admin_upload_gold_dataset")
async def upload_gold_dataset(
    language_id: int = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    if not file.filename.endswith('.txt'):
        raise HTTPException(status_code=400, detail="Only .txt files allowed")
    
    try:
        # Create language-specific directory
        lang_dir = Path("gold_datasets") / f"lang_{language_id}"
        lang_dir.mkdir(exist_ok=True)
        
        # Save file with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_path = lang_dir / f"{timestamp}_{file.filename}"
        
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
        
        print(f"GOLD DATASET SAVED: {file_path}")
        
        # Save to database or demo data
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO gold_datasets (language_id, filename, file_path, uploaded_by) VALUES (%s, %s, %s, %s)",
                    (language_id, file.filename, str(file_path), user['username'])
                )
                conn.commit()
                conn.close()
                print(f"SUCCESS: Gold dataset saved to database: {file.filename}")
            except Exception as e:
                print(f"ERROR saving to database: {e}")
                if conn:
                    conn.close()
                # Fallback to demo data
                add_to_demo_datasets(language_id, file.filename, str(file_path), user['username'])
        else:
            # Save to demo data
            add_to_demo_datasets(language_id, file.filename, str(file_path), user['username'])
        
        return RedirectResponse(url="/admin", status_code=302)
        
    except Exception as e:
        print(f"ERROR uploading gold dataset: {e}")
        raise HTTPException(status_code=500, detail=f"Error uploading gold dataset: {str(e)}")
@app.post("/admin/delete_gold_dataset/{dataset_id}", name="admin_delete_gold_dataset")
async def delete_gold_dataset(
    dataset_id: int,
    user: dict = Depends(get_current_user)
):
    if user['username'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Get the dataset file path before deleting
            cursor.execute("SELECT file_path FROM gold_datasets WHERE id = %s", (dataset_id,))
            dataset = cursor.fetchone()
            
            if not dataset:
                conn.close()
                raise HTTPException(status_code=404, detail="Gold dataset not found")
            
            # Delete the physical file if it exists
            file_path = Path(dataset['file_path'])
            if file_path.exists():
                file_path.unlink()
                print(f"SUCCESS: Deleted physical file: {file_path}")
            
            # Delete from database
            cursor.execute("DELETE FROM gold_datasets WHERE id = %s", (dataset_id,))
            conn.commit()
            conn.close()
            print(f"SUCCESS: Gold dataset deleted from database (ID: {dataset_id})")
        except HTTPException:
            raise
        except Exception as e:
            print(f"ERROR deleting gold dataset from database: {e}")
            if conn:
                conn.close()
            # Fallback to demo storage
            delete_from_demo_datasets(dataset_id)
    else:
        # Delete from demo storage
        delete_from_demo_datasets(dataset_id)
    
    return RedirectResponse(url="/admin", status_code=302)

def delete_from_demo_datasets(dataset_id: int):
    """Delete gold dataset from demo storage"""
    global DEMO_GOLD_DATASETS
    
    # Find the dataset
    dataset = next((ds for ds in DEMO_GOLD_DATASETS if ds['id'] == dataset_id), None)
    
    if not dataset:
        raise HTTPException(status_code=404, detail="Gold dataset not found")
    
    # Delete the physical file if it exists
    file_path = Path(dataset['file_path'])
    if file_path.exists():
        file_path.unlink()
        print(f"SUCCESS: Deleted physical file: {file_path}")
    
    # Remove from demo storage
    DEMO_GOLD_DATASETS[:] = [ds for ds in DEMO_GOLD_DATASETS if ds['id'] != dataset_id]
    print(f"SUCCESS: Gold dataset deleted from demo storage (ID: {dataset_id})")
def add_to_demo_datasets(language_id: int, filename: str, file_path: str, uploaded_by: str):
    """Add gold dataset to demo data"""
    language_name = next((lang['language_name'] for lang in DEMO_LANGUAGES if lang['id'] == language_id), 'Unknown')
    
    dataset = {
        'id': len(DEMO_GOLD_DATASETS) + 1,
        'language_id': language_id,
        'language_name': language_name,
        'filename': filename,
        'file_path': file_path,
        'uploaded_by': uploaded_by,
        'created_at': datetime.now()
    }
    
    DEMO_GOLD_DATASETS.append(dataset)
    print(f"SUCCESS: Gold dataset added to demo data: {filename}")

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