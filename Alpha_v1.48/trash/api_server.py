from flask import Flask, jsonify, abort, request
import sqlite3
import os

# --- Configurazione Database ---
# Il file del database deve essere nella stessa directory di questo script.
DATABASE_FILE = os.path.join(os.path.dirname(__file__), 'tcg_pocket.db') 

app = Flask(__name__)

def get_db_connection():
    """Stabilisce la connessione al database SQLite tcg_pocket.db."""
    # Controlla se il database esiste prima di tentare la connessione
    if not os.path.exists(DATABASE_FILE):
        print(f"ERRORE: Database file non trovato in {DATABASE_FILE}")
        return None
        
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        # Imposta la row_factory per restituire le righe come dizionari
        conn.row_factory = sqlite3.Row 
        return conn
    except Exception as e:
        print(f"Errore di connessione a SQLite: {e}")
        return None

# --- Endpoint di Test Base ---

@app.route('/', methods=['GET'])
def home():
    """Endpoint di benvenuto."""
    return jsonify({
        "status": "online",
        "message": "Benvenuto alla Pokemon Pocket TCG API",
        "endpoints": {
            "/api/sets": "Elenco di tutti i set.",
            "/api/cards": "Elenco di tutte le carte con filtro opzionale (set_code).",
            "/api/card/<int:card_id>": "Dettagli di una singola carta per ID."
        }
    })

# --- Endpoint 1: Ottenere tutti i Set ---

@app.route('/api/sets', methods=['GET'])
def get_sets():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"errore": "Impossibile connettersi al database interno"}), 500

    try:
        cursor = conn.cursor()
        # Query: Seleziona tutti i campi dalla tabella 'sets'
        cursor.execute("SELECT set_code, set_name, release_date, total_cards, url, cover_image_path FROM sets ORDER BY release_date DESC;") 
        sets_rows = cursor.fetchall()
        
        # Converti le righe in lista di dizionari
        sets_list = [dict(row) for row in sets_rows] 
        
        conn.close()
        return jsonify(sets_list)

    except Exception as e:
        print(f"Errore query 'sets': {e}")
        if conn: conn.close()
        return jsonify({"errore": f"Errore durante l'estrazione dei set: {e}"}), 500

# --- Endpoint 2: Ottenere tutte le Carte con Filtro (Query String) ---

@app.route('/api/cards', methods=['GET'])
def get_cards():
    conn = get_db_connection()
    if conn is None:
        return jsonify({"errore": "Impossibile connettersi al database interno"}), 500
        
    # Ottieni il codice del set dal parametro della query string (?set_code=...)
    set_code_filter = request.args.get('set_code')
    
    query = "SELECT set_code, card_number, card_name, rarity, local_image_path, card_url, color_hash FROM cards"
    params = []
    
    if set_code_filter:
        query += " WHERE set_code = ?"
        params.append(set_code_filter)
    
    query += " ORDER BY set_code, card_number"

    try:
        cursor = conn.cursor()
        # Esegue la query con i parametri, se presenti
        cursor.execute(query, tuple(params))
        cards_rows = cursor.fetchall()
        
        cards_list = [dict(row) for row in cards_rows] 
        conn.close()
        return jsonify(cards_list)

    except Exception as e:
        print(f"Errore query 'cards': {e}")
        if conn: conn.close()
        return jsonify({"errore": f"Errore durante l'estrazione delle carte: {e}"}), 500

# --- Endpoint 3: Ottenere Dettagli di una Singola Carta per ID ---

@app.route('/api/card/<int:card_id>', methods=['GET'])
def get_card_by_id(card_id):
    conn = get_db_connection()
    if conn is None:
        return jsonify({"errore": "Impossibile connettersi al database interno"}), 500

    try:
        cursor = conn.cursor()
        # Query: Seleziona una carta per il suo ID primario
        cursor.execute("""
            SELECT 
                c.set_code, c.card_number, c.card_name, c.rarity, c.local_image_path, c.card_url, c.color_hash,
                s.set_name 
            FROM cards c
            JOIN sets s ON c.set_code = s.set_code
            WHERE c.id = ?
        """, (card_id,)) 
        
        card_row = cursor.fetchone()
        conn.close()
        
        if card_row is None:
            # Ritorna 404 se la carta non viene trovata
            abort(404, description=f"Carta con ID {card_id} non trovata.")
        
        # Converte la singola riga in un dizionario
        return jsonify(dict(card_row))

    except Exception as e:
        print(f"Errore query 'card_by_id': {e}")
        if conn: conn.close()
        return jsonify({"errore": f"Errore durante l'estrazione della carta: {e}"}), 500

# --- Gunicorn non esegue questa sezione, ma è utile per test locali diretti ---
if __name__ == '__main__':
    # Creare il database tcg_pocket.db se non esiste per il testing
    if not os.path.exists(DATABASE_FILE):
        print("ATTENZIONE: Database non trovato. Esegui 1-make_db.py prima di avviare il server.")
    app.run(debug=True, host='0.0.0.0', port=5000)

