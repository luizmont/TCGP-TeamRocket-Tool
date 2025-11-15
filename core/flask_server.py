"""flask_server.py - Server Flask per web interface"""

# Import standard library
import secrets
import mimetypes
from flask import Flask, render_template, send_file, jsonify, request, send_from_directory, session

import sqlite3
import os
from datetime import datetime
from threading import Thread, Event
from typing import Optional, Dict, List
import json
from functools import wraps
from dotenv import load_dotenv
from PyQt5.QtCore import QThread, pyqtSignal

# Import configurazione
from config import (
    TCG_IMAGES_DIR, 
    ICON_PATH, 
    DB_FILENAME, 
    ACCOUNTS_DIR,
    FLASK_HOST, 
    FLASK_PORT, 
    FLASK_DEBUG, 
    CLOUDFLARE_PASSWORD,
    get_app_data_path,
    SELECTED_RARITIES
)

# Import traduzioni
from .translations import t, set_language, get_language
class FlaskServerThread(QThread):
    """Thread per eseguire il server Flask in background senza bloccare la GUI."""
    
    log_signal = pyqtSignal(str)
    started_signal = pyqtSignal()
    stopped_signal = pyqtSignal()
    error_signal = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.flask_app = None
        self.server = None
        self.should_stop = False
        
    def run(self):
        """Avvia il server Flask in un thread separato."""
        try:
            from flask import Flask, render_template, jsonify, send_from_directory
            from werkzeug.serving import make_server
            
            # Crea Flask app
            app = Flask(__name__, 
                       template_folder='templates',
                       static_folder='static')

            app.static_folder = '.'
            app.static_url_path = '/static'
            # ✅ IMPORTANTE: Configura la chiave segreta per le sessioni
            app.config['SECRET_KEY'] = secrets.token_hex(32)

            # Configurazioni aggiuntive
            app.config['SESSION_COOKIE_SECURE'] = False  # True se usi HTTPS
            app.config['SESSION_COOKIE_HTTPONLY'] = True
            app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
            app.config['PERMANENT_SESSION_LIFETIME'] = 3600  # 1 ora
            
            # ✅ Configura traduzioni per Flask
            # Context processor per passare t() ai template
            @app.context_processor
            def inject_translations():
                # Carica la lingua depuis settings
                try:
                    if os.path.exists('settings.json'):
                        with open('settings.json', 'r', encoding='utf-8') as f:
                            settings = json.load(f)
                            saved_language = settings.get('language', 'fr')
                            set_language(saved_language)
                    else:
                        set_language('fr')
                except:
                    set_language('fr')
                
                # Passe la fonction t() aux templates
                return dict(t=t, current_language=get_language())
            
            self.flask_app = app
            
            # ===== ROUTES  ====
            @app.route('/tcg_images/<path:image_path>')
            def serve_tcg_images(image_path):
                """Serve immagini TCG - funziona con EXE."""
                try:
                    import urllib.parse
                    
                    # Decodifica il path
                    decoded_path = urllib.parse.unquote(image_path)
                    
                    # Costruisci path completo
                    full_path = os.path.join(TCG_IMAGES_DIR, decoded_path)
                    
                    # Normalizza il path (rimuovi .. e simili)
                    full_path = os.path.abspath(full_path)
                    base_dir = os.path.abspath(TCG_IMAGES_DIR)
                    
                    # Verifica che il file sia dentro TCG_IMAGES_DIR (security check)
                    if not full_path.startswith(base_dir):
                        return "Access denied", 403
                    
                    print(f"📁 Requested: {decoded_path}")
                    print(f"📁 Full path: {full_path}")
                    print(f"✅ Exists: {os.path.exists(full_path)}")
                    
                    if not os.path.exists(full_path):
                        return "Not found", 404
                    
                    # Determina il tipo MIME
                    mimetype, _ = mimetypes.guess_type(full_path)
                    if mimetype is None:
                        mimetype = 'image/webp'
                    
                    print(f"✅ Sending: {mimetype}")
                    return send_file(full_path, mimetype=mimetype)
                    
                except Exception as e:
                    print(f"❌ Error: {e}")
                    import traceback
                    traceback.print_exc()
                    return f"Error: {str(e)}", 500
                
            @app.route('/debug/images')
            def debug_images():
                """Debug: mostra il percorso delle immagini."""
                import os
                
                html = f"""
                <h1>Debug Images</h1>
                <p><strong>TCG_IMAGES_DIR:</strong> {TCG_IMAGES_DIR}</p>
                <p><strong>Exists:</strong> {os.path.exists(TCG_IMAGES_DIR)}</p>
                <hr>
                <h2>Available images:</h2>
                <ul>
                """
                
                if os.path.exists(TCG_IMAGES_DIR):
                    for root, dirs, files in os.walk(TCG_IMAGES_DIR):
                        for file in files:
                            if file.endswith(('.webp', '.png', '.jpg')):
                                full_path = os.path.join(root, file)
                                rel_path = os.path.relpath(full_path, TCG_IMAGES_DIR)
                                
                                # Converti backslash a forward slash
                                url_path = rel_path.replace('\\', '/')
                                
                                html += f"""
                                <li>
                                    <strong>{file}</strong><br>
                                    Full: {full_path}<br>
                                    Rel: {rel_path}<br>
                                    URL: <a href="/tcg_images/{url_path}">Test</a>
                                </li>
                                """
                
                html += "</ul>"
                return html




            @app.route('/account/<account_name>', methods=['GET', 'POST'])
            @require_password
            def account_collection(account_name):
                """Visualizza la collezione completa di un account specifico."""
                try:
                    conn = sqlite3.connect(DB_FILENAME)
                    cursor = conn.cursor()
                    
                    # Verifica che l'account esista
                    cursor.execute("SELECT account_id FROM accounts WHERE account_name = ?", (account_name,))
                    account = cursor.fetchone()
                    
                    if not account:
                        conn.close()
                        return f"<h1>Account '{account_name}' not found</h1><a href='/'>Back to home</a>", 404
                    
                    account_id = account[0]
                    
                    # Get inventory dell'account
                    cursor.execute("""
                        SELECT card_id, quantity 
                        FROM account_inventory 
                        WHERE account_id = ? AND quantity > 0
                    """, (account_id,))
                    
                    inventory = {row[0]: row[1] for row in cursor.fetchall()}
                    
                    # Get statistiche account
                    cursor.execute("""
                        SELECT 
                            COUNT(DISTINCT c.id) as unique_cards,
                            SUM(ai.quantity) as total_copies,
                            COUNT(DISTINCT c.set_code) as sets_owned
                        FROM cards c
                        JOIN account_inventory ai ON c.id = ai.card_id
                        WHERE ai.account_id = ? AND ai.quantity > 0
                    """, (account_id,))
                    
                    stats = cursor.fetchone()
                    
                    # Get collezione per set
                    cursor.execute("""
                        SELECT DISTINCT c.set_code, s.set_name
                        FROM cards c
                        JOIN sets s ON c.set_code = s.set_code
                        JOIN account_inventory ai ON c.id = ai.card_id
                        WHERE ai.account_id = ? AND ai.quantity > 0
                        ORDER BY s.set_code
                    """, (account_id,))
                    
                    sets = cursor.fetchall()
                    
                    # Get carte per ogni set
                    collection_by_set = {}
                    for set_code, set_name in sets:
                        cursor.execute("""
                            SELECT c.id, c.card_number, c.card_name, c.rarity, ai.quantity
                            FROM cards c
                            JOIN account_inventory ai ON c.id = ai.card_id
                            WHERE c.set_code = ? AND ai.account_id = ? AND ai.quantity > 0
                            ORDER BY CAST(c.card_number AS INTEGER)
                        """, (set_code, account_id))
                        
                        cards = cursor.fetchall()
                        collection_by_set[set_code] = {
                            'set_name': set_name,
                            'cards': cards
                        }
                    
                    conn.close()
                    
                    return render_template('account_collection.html',
                                        account_name=account_name,
                                        stats={
                                            'unique_cards': stats[0] or 0,
                                            'total_copies': stats[1] or 0,
                                            'sets_owned': stats[2] or 0
                                        },
                                        collection_by_set=collection_by_set)
                
                except Exception as e:
                    import traceback
                    error_msg = traceback.format_exc()
                    return f"<h1>Error loading collection</h1><pre>{error_msg}</pre><a href='/'>Back to home</a>", 500



            @app.route('/tcg_images/<path:filename>', methods=['GET', 'POST'])
            @require_password
            def serve_card_image(filename):
                return send_from_directory('tcg_images', filename)
            
            @app.route('/', methods=['GET', 'POST'])
            @require_password
            def index():
                """Pagina principale con lista di tutti i set."""
                try:
                    conn = sqlite3.connect(DB_FILENAME)
                    cursor = conn.cursor()
                    
                    # Target rarities
                    placeholders = ','.join('?' * len(SELECTED_RARITIES))
                    
                    # Get tutti i set con stats CORRETTI
                    cursor.execute(f"""
                        SELECT 
                            s.set_code,
                            s.set_name,
                            s.release_date,
                            COUNT(DISTINCT CASE WHEN c.rarity IN ({placeholders}) THEN c.id END) as target_total,
                            COUNT(DISTINCT CASE WHEN c.rarity IN ({placeholders}) AND ai.quantity > 0 THEN c.id END) as owned_cards,
                            COALESCE(SUM(CASE WHEN c.rarity IN ({placeholders}) THEN ai.quantity END), 0) as total_copies
                        FROM sets s
                        LEFT JOIN cards c ON s.set_code = c.set_code
                        LEFT JOIN account_inventory ai ON c.id = ai.card_id
                        GROUP BY s.set_code
                        ORDER BY s.set_code DESC
                    """, SELECTED_RARITIES + SELECTED_RARITIES + SELECTED_RARITIES)
                    
                    sets_data = cursor.fetchall()
                    
                    # Formatta i dati per il template
                    sets = []
                    for row in sets_data:
                        set_code, set_name, release_date, target_total, owned_cards, total_copies = row
                        
                        # Calcola completion correttamente
                        if target_total > 0:
                            completion = int((owned_cards / target_total) * 100)
                        else:
                            completion = 0
                        
                        sets.append({
                            'code': set_code,
                            'name': set_name,
                            'release_date': release_date or 'N/A',
                            'total_cards': target_total,
                            'owned': owned_cards,
                            'completion': completion,
                            'copies': total_copies or 0
                        })
                    
                    conn.close()
                    
                    return render_template('index.html', sets=sets)
                
                except Exception as e:
                    import traceback
                    print(f"Error in index: {traceback.format_exc()}")
                    return f"<h1>Error</h1><pre>{traceback.format_exc()}</pre>", 500



            
            @app.route('/set/<set_code>', methods=['GET', 'POST'])
            @require_password
            def set_view(set_code):
                """Visualizza tutte le carte di un set specifico con copie."""
                try:
                    conn = sqlite3.connect(DB_FILENAME)
                    cursor = conn.cursor()
                    
                    # Get filter parameter from query string (default: 'all')
                    filter_type = request.args.get('filter', 'all')  # 'all', 'owned', 'missing'
                    
                    # Get info del set
                    cursor.execute("""
                        SELECT set_name, release_date, total_cards 
                        FROM sets 
                        WHERE set_code = ?
                    """, (set_code,))
                    
                    set_info = cursor.fetchone()
                    
                    if not set_info:
                        conn.close()
                        return f"<h1>Set '{set_code}' not found</h1><a href='/'>Back</a>", 404
                    
                    set_name, release_date, total_cards = set_info
                    
                    # Target rarities
                    
                    # Build query based on filter
                    if filter_type == 'owned':
                        # Only cards with quantity > 0
                        query = """
                            SELECT 
                                c.id, 
                                c.card_number, 
                                c.card_name, 
                                c.rarity,
                                COALESCE(SUM(ai.quantity), 0) as total_copies
                            FROM cards c
                            LEFT JOIN account_inventory ai ON c.id = ai.card_id
                            WHERE c.set_code = ? AND c.rarity IN (?, ?, ?, ?)
                            GROUP BY c.id
                            HAVING COALESCE(SUM(ai.quantity), 0) > 0
                            ORDER BY CAST(c.card_number AS INTEGER)
                        """
                    elif filter_type == 'missing':
                        # Only cards with quantity = 0 or NULL
                        query = """
                            SELECT 
                                c.id, 
                                c.card_number, 
                                c.card_name, 
                                c.rarity,
                                COALESCE(SUM(ai.quantity), 0) as total_copies
                            FROM cards c
                            LEFT JOIN account_inventory ai ON c.id = ai.card_id
                            WHERE c.set_code = ? AND c.rarity IN (?, ?, ?, ?)
                            GROUP BY c.id
                            HAVING COALESCE(SUM(ai.quantity), 0) = 0
                            ORDER BY CAST(c.card_number AS INTEGER)
                        """
                    else:  # 'all'
                        # All cards
                        query = """
                            SELECT 
                                c.id, 
                                c.card_number, 
                                c.card_name, 
                                c.rarity,
                                COALESCE(SUM(ai.quantity), 0) as total_copies
                            FROM cards c
                            LEFT JOIN account_inventory ai ON c.id = ai.card_id
                            WHERE c.set_code = ? AND c.rarity IN (?, ?, ?, ?)
                            GROUP BY c.id
                            ORDER BY CAST(c.card_number AS INTEGER)
                        """
                    
                    cursor.execute(query, (set_code,) + SELECTED_RARITIES)
                    cards = cursor.fetchall()
                    
                    # Format cards data
                    cards_data = []
                    for row in cards:
                        cards_data.append({
                            'id': row[0],
                            'card_number': row[1],
                            'card_name': row[2],
                            'rarity': row[3],
                            'quantity': row[4]
                        })
                    
                    # Get cover image path
                    cursor.execute("SELECT cover_image_path FROM sets WHERE set_code = ?", (set_code,))
                    cover_result = cursor.fetchone()
                    cover_path = cover_result[0] if cover_result else None
                    
                    conn.close()
                    
                    return render_template('set_view.html',
                                        set_code=set_code,
                                        set_name=set_name,
                                        release_date=release_date or 'N/A',
                                        total_cards=len(cards_data),
                                        cards=cards_data,
                                        filter_type=filter_type,
                                        cover_path=cover_path)
                
                except Exception as e:
                    import traceback
                    return f"<h1>Error</h1><pre>{traceback.format_exc()}</pre><a href='/'>Back</a>", 500
            
            @app.route('/card/<int:card_id>', methods=['GET', 'POST'])
            @require_password
            def card_details(card_id):
                conn = sqlite3.connect(DB_FILENAME)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT c.*, s.set_name
                    FROM cards c
                    JOIN sets s ON c.set_code = s.set_code
                    WHERE c.id = ?
                """, (card_id,))
                card = cursor.fetchone()
                if not card:
                    return "Card not found", 404
                cursor.execute("""
                    SELECT a.account_name, ai.quantity
                    FROM account_inventory ai
                    JOIN accounts a ON ai.account_id = a.account_id
                    WHERE ai.card_id = ? AND ai.quantity > 0
                    ORDER BY ai.quantity DESC
                """, (card_id,))
                owners = cursor.fetchall()
                cursor.execute("""
                    SELECT COALESCE(SUM(quantity), 0)
                    FROM account_inventory WHERE card_id = ?
                """, (card_id,))
                total_copies = cursor.fetchone()[0]
                conn.close()
                return render_template('card_details.html', 
                                      card=card, owners=owners, total_copies=total_copies)
            
            @app.route('/stats', methods=['GET', 'POST'])
            @require_password
            def stats():
                conn = sqlite3.connect(DB_FILENAME)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT a.account_name, 
                           COUNT(DISTINCT ai.card_id) as unique_cards,
                           SUM(ai.quantity) as total_copies
                    FROM accounts a
                    JOIN account_inventory ai ON a.account_id = ai.account_id
                    WHERE ai.quantity > 0
                    GROUP BY a.account_id
                    ORDER BY unique_cards DESC LIMIT 5
                """)
                top_accounts = cursor.fetchall()
                cursor.execute("""
                    SELECT c.card_name, c.set_code, c.rarity,
                           SUM(ai.quantity) as total_copies
                    FROM cards c
                    JOIN account_inventory ai ON c.id = ai.card_id
                    WHERE ai.quantity > 0
                    GROUP BY c.id
                    ORDER BY total_copies DESC LIMIT 5
                """)
                top_cards = cursor.fetchall()
                conn.close()
                return render_template('stats.html',
                                      top_accounts=top_accounts,
                                      top_cards=top_cards)
            
            # ⬇️ USA make_server per poterlo fermare correttamente ⬇️
            self.server = make_server('0.0.0.0', 5000, app, threaded=True)
            self.log_signal.emit("🌐 Flask server started on http://localhost:5000")
            self.started_signal.emit()
            
            # Esegui il server (blocca fino a shutdown)
            self.server.serve_forever()
            
        except OSError as e:
            if "Address already in use" in str(e):
                self.error_signal.emit("Port 5000 already in use. Stop the other server first.")
            else:
                self.error_signal.emit(f"Server error: {str(e)}")
        except Exception as e:
            import traceback
            self.error_signal.emit(f"Flask error: {str(e)}\n{traceback.format_exc()}")
        finally:
            self.log_signal.emit("🌐 Flask server stopped")
            self.stopped_signal.emit()
    
    def stop_server(self):
        """Ferma il server Flask in modo sicuro."""
        if self.server:
            self.log_signal.emit("🌐 Stopping Flask server...")
            self.server.shutdown()  # ⬅️ Shutdown thread-safe
            self.server = None
        self.quit()
        self.wait(3000)  # Aspetta max 3 secondi


def require_password(f):
    """Decorator to require password for public access"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # ? ALWAYS reload .env (so the password is up to date)
        load_dotenv(override=True)
        cloudflare_password = os.getenv('CLOUDFLARE_PASSWORD', '').strip()
        
        # Check if it's a real localhost access (not via tunnel)
        is_localhost = (
            request.remote_addr == '127.0.0.1' and
            not request.headers.get('CF-Connecting-IP')
        )
        
        if is_localhost:
            return f(*args, **kwargs)
        
        if session.get('authenticated'):
            return f(*args, **kwargs)
        
        if request.method == 'POST':
            password = request.form.get('password', '').strip()
            
            if not cloudflare_password:
                from .translations import t, set_language, get_language
                # Charge la langue depuis settings
                try:
                    if os.path.exists('settings.json'):
                        with open('settings.json', 'r', encoding='utf-8') as settings_file:
                            settings = json.load(settings_file)
                            saved_language = settings.get('language', 'fr')
                            set_language(saved_language)
                    else:
                        set_language('fr')
                except:
                    set_language('fr')
                return render_template('login.html', error=t('web.password_not_configured'))
            
            if password == cloudflare_password:  # Use the updated local variable
                session['authenticated'] = True
                return f(*args, **kwargs)
            else:
                from .translations import t, set_language, get_language
                # Charge la langue depuis settings
                try:
                    if os.path.exists('settings.json'):
                        with open('settings.json', 'r', encoding='utf-8') as settings_file:
                            settings = json.load(settings_file)
                            saved_language = settings.get('language', 'fr')
                            set_language(saved_language)
                    else:
                        set_language('fr')
                except:
                    set_language('fr')
                return render_template('login.html', error=t('web.incorrect_password'))
        
        return render_template('login.html')
    
    return decorated_function
