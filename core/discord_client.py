"""discord_client.py - Client Discord per monitoraggio trade"""

# Import standard library
import discord
from discord.ext import commands
import aiohttp
import asyncio
import json
import os
import sqlite3
import time
import re
from datetime import datetime
from threading import Lock, Semaphore
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
import io
import cv2
import numpy as np
from PIL import Image
# Import configurazione
from config import (
    ACCOUNTS_DIR, 
    LOG_FILENAME, 
    SEARCH_STRING,
    MAX_CONCURRENT_DOWNLOADS, 
    TCG_IMAGES_DIR, 
    SAVE_INTERVAL,
    ACCOUNT_NAME_PATTERN, 
    REQUEST_TIMEOUT, 
    DOWNLOAD_TIMEOUT,
    DB_FILENAME,
    BATCH_SIZE,
    MAX_RETRIES,
    RETRY_DELAY,
    CHUNK_SIZE,
    SELECTED_RARITIES
)

# Import traduzioni
from .translations import t


# =========================================================================
# 🤖 DISCORD BOT CLIENT
# =========================================================================

class TradeMonitorClient(discord.Client):
    """Client Discord per monitorare i trade e scansionare le carte."""
    
    def __init__(self, *, intents: discord.Intents, log_callback, progress_callback, 
                 trade_callback, status_callback, card_found_callback):
        super().__init__(intents=intents)
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.trade_callback = trade_callback
        self.status_callback = status_callback
        self.card_found_callback = card_found_callback
        
        # ❌ RIMOSSO: self.trade_log = []
        # ❌ RIMOSSO: self.processed_message_ids = set()
        
        self.initial_scan_done = False
        self.session = None
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self.pending_trades = []
        
        # ✅ CACHE
        self.last_message_id_cache = 0
        self.last_cache_update = 0
        
        # ✅ THREAD POOL
        self.card_recognition_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="CardRecognizer"
        )
        
        # ✅ CardRecognizer
        from .card_recognizer import CardRecognizer
        self.card_recognizer = CardRecognizer(
            db_path=DB_FILENAME,
            similarity_threshold=75.0
        )
        self.base_url = "http://www.pkmn-pocket-api.it/api"
        # ================================================================
        # ✅ AGGIUNTO: Connessione DB per questo thread
        # ================================================================
        try:
            self.db_conn = sqlite3.connect(DB_FILENAME, check_same_thread=False, timeout=10.0)
            self.db_conn.execute("PRAGMA journal_mode = WAL")
            self.db_conn.row_factory = sqlite3.Row # Per accedere ai dati come dict
        except Exception as e:
            self.log_callback(f"❌ Errore connessione DB nel Client: {e}")
            raise

        self.db_lock = Lock()

    def _create_screenshot_thumbnail(self, image_bytes):
        """Crea un thumbnail 60x60 in-memory dallo screenshot."""
        if not image_bytes:
            return None
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((300, 300), Image.Resampling.LANCZOS)
            
            # Se è WebP/PNG, converti per JPEG (più piccolo)
            if img.mode != 'RGB':
                img = img.convert('RGB')
                
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=80)
            return output.getvalue()
        except Exception as e:
            self.log_callback(f"⚠️ Errore creazione thumbnail screenshot: {e}")
            return None


    async def recover_missing_cards(self, trade_list: List[Dict]):
        """
        Recupera le carte mancanti per messaggi che hanno già gli allegati scaricati.

        Utile per:
        - Messaggi nel log ma senza carte nel DB
        - Scansioni fallite precedentemente
        - Aggiornamenti dell'algoritmo di riconoscimento
        """

        if not trade_list:
            return

        self.log_callback(f"🔍 Recupero carte per {len(trade_list)} messaggi...")

        processed_count = 0
        error_count = 0

        for i, trade_data in enumerate(trade_list, 1):
            try:
                # Mostra progresso
                percent = int((i / len(trade_list)) * 100) if len(trade_list) > 0 else 0
                status = f"Recupero {i}/{len(trade_list)}"
                self.progress_callback({'percent': percent, 'status': status})
                
                image_path = trade_data.get('image_path')
                
                # Verifica che l'immagine esista
                if not image_path or not os.path.exists(image_path):
                    self.log_callback(f"⚠️ [{i}/{len(trade_list)}] Immagine non trovata: {image_path}")
                    error_count += 1
                    continue
                
                # Scansiona l'immagine per riconoscere le carte
                self.log_callback(f"🔍 [{i}/{len(trade_list)}] Scansione: {os.path.basename(image_path)}")
                
                await self.scan_image_for_cards(trade_data)
                
                processed_count += 1
                
                # Piccola pausa per evitare sovraccarico
                #await asyncio.sleep(0.2)
                
            except Exception as e:
                self.log_callback(f"❌ Errore recupero messaggio {i}: {e}")
                error_count += 1
                continue

        self.log_callback(f"✅ Recupero completato: {processed_count} elaborati, {error_count} errori")


    def update_account_credentials(self, account_name, device_account, device_password):
        """Aggiorna le credenziali dell'account nel DB."""
        try:
            with self.db_lock:
                # Assicura che l'account esista
                cursor = self.db_conn.cursor()
                cursor.execute("INSERT OR IGNORE INTO accounts (account_name) VALUES (?)", (account_name,))
                
                # Aggiorna credenziali
                cursor.execute("""
                    UPDATE accounts 
                    SET device_account = ?, device_password = ?, last_updated = CURRENT_TIMESTAMP
                    WHERE account_name = ?
                """, (device_account, device_password, account_name))
                
                self.db_conn.commit()
        except Exception as e:
            self.log_callback(f"⚠️ Errore aggiornamento credenziali {account_name}: {e}")

    async def setup_hook(self):
        """Setup del client."""
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        self.session = aiohttp.ClientSession(connector=connector)
    
    async def close(self):
        """Chiude il client con shutdown SICURO (senza recursion error)."""
        self.log_callback("⏹️ Arresto bot...")
        
        try:
            # ❌ NON fare: asyncio.all_tasks() + cancel (causa RecursionError)
            # ✅ Invece: chiudi direttamente websocket
            
            if self.session:
                await self.session.close()
            
            # Chiudi il thread pool
            if hasattr(self, 'card_recognition_executor'):
                self.card_recognition_executor.shutdown(wait=False)
            if hasattr(self, 'db_conn'):
                self.db_conn.close()            
            # Chiudi la connessione Discord (no tasks.cancel())
            await super().close()
            
            self.log_callback("✅ Bot arrestato")
        
        except asyncio.CancelledError:
            self.log_callback("✅ Bot interrotto")
        except Exception as e:
            self.log_callback(f"⚠️ Errore chiusura: {e}")
        
    async def on_ready(self):
        """
        Esecuzione all'avvio del bot.
        MODIFICATO: Gestisce 4 casi:
        1. DB Vuoto -> Scansione Storica Completa
        2. DB Esistente -> Carica UI
        3. DB Esistente -> Recupera falliti
        4. DB Esistente -> Scansione Incrementale (Nuovi) + Scansione Storica (Vecchi)
        """
        
        self.log_callback("✅ " + t("discord_bot.connected_as", name=self.user.name))
        self.status_callback(t("discord_bot.status_connected"))
        
        cursor = self.db_conn.cursor()
        
        try:
            cursor.execute("SELECT COUNT(message_id) FROM trades")
            total_trades_in_db = cursor.fetchone()[0]

            if total_trades_in_db == 0:
                # ================================================================
                # CASO A: Database Vuoto -> Avvia Scansione Storica Completa
                # ================================================================
                self.log_callback("🚀 Database vuoto. Inizio scansione storica completa...")
                await self.perform_historical_scan_streaming()
                
            else:
                # ================================================================
                # CASO B: Database Esistente
                # ================================================================
                self.log_callback(f"Database esistente con {total_trades_in_db} trade.")
                
                # 1. CARICA LA UI
                try:
                    cursor.execute("SELECT * FROM trades ORDER BY processed_at DESC LIMIT 100")
                    recent_trades = cursor.fetchall()
                    for trade_row in recent_trades:
                        self.trade_callback(dict(trade_row))
                except Exception as e:
                    self.log_callback(f"⚠️ Errore caricamento trade recenti: {e}")

                # 2. RECUPERA TRADE FALLITI (scan_status = 0 o 2)
                try:
                    cursor.execute("SELECT * FROM trades WHERE scan_status = 0 OR scan_status = 2")
                    trades_to_reprocess = cursor.fetchall()
                    if trades_to_reprocess:
                        await self.recover_missing_cards([dict(row) for row in trades_to_reprocess])
                except Exception as e:
                    self.log_callback(f"⚠️ Errore recupero trade: {e}")

                # ================================================================
                # ✅ CORREZIONE: ESEGUI ENTRAMBE LE SCANSIONI
                # ================================================================
                
                # 3. SCANSIONE INCREMENTALE (per messaggi NUOVI, arrivati offline)
                await self.perform_incremental_scan_fast()
                
                # 4. SCANSIONE STORICA (per messaggi VECCHI, se la scansione era incompleta)
                await self.perform_historical_scan_streaming()
                

        except Exception as e:
            self.log_callback(f"❌ Errore critico durante on_ready: {e}")
            import traceback
            traceback.print_exc()
        
        # ================================================================
        # PASSO FINALE: INIZIO MONITORAGGIO IN TEMPO REALE
        # ================================================================
        
        self.initial_scan_done = True
        self.log_callback("👂 Inizio monitoraggio messaggi in tempo reale...")




    def _get_or_create_account(self, account_name, device_account=None, device_password=None):
        """
        Ottiene o crea un account nel database (Thread-safe).
        MODIFICATO: Aggiorna anche le credenziali se fornite.
        """
        if not account_name:
            return None
        try:
            # Usiamo un lock per evitare race condition
            with self.db_lock:
                cursor = self.db_conn.cursor()
                
                # 1. Inserisci o ignora (assicura che l'account esista)
                cursor.execute("INSERT OR IGNORE INTO accounts (account_name) VALUES (?)", (account_name,))
                
                # 2. Aggiorna le credenziali se fornite
                if device_account and device_password:
                    cursor.execute("""
                        UPDATE accounts 
                        SET device_account = ?, device_password = ?, last_updated = CURRENT_TIMESTAMP
                        WHERE account_name = ?
                    """, (device_account, device_password, account_name))
                
                self.db_conn.commit()

                # 3. Recupera l'ID
                cursor.execute("SELECT account_id FROM accounts WHERE account_name = ?", (account_name,))
                result = cursor.fetchone()
                
                if result:
                    return result[0] # Ritorna l'ID
                return None
        except Exception as e:
            self.log_callback(f"⚠️ Errore gestione account '{account_name}': {e}")
            return None

    async def perform_historical_scan_streaming(self):
        """
        Scansiona i messaggi in STREAMING (uno alla volta).
        MODIFICATO: Legge XML e Immagine in memoria.
        """
        
        channel_id = int(os.getenv('CHANNEL_ID', '0'))
        channel = self.get_channel(channel_id)
        
        if not channel:
            self.log_callback("❌ Canale non trovato")
            self.initial_scan_done = True
            return
        
        # Verifica permessi (invariato)
        if hasattr(channel, 'guild') and channel.guild:
            bot_member = channel.guild.get_member(self.user.id)
            if bot_member:
                permissions = channel.permissions_for(bot_member)
                if not permissions.read_message_history:
                    self.log_callback("❌ Permesso negato: lettura cronologia messaggi")
                    self.initial_scan_done = True
                    return
        
        # Carica l'ID più vecchio (invariato)
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("SELECT MIN(CAST(message_id AS INTEGER)) FROM trades")
            result = cursor.fetchone()
            oldest_message_id = int(result[0]) if result and result[0] else None
        except Exception as e:
            self.log_callback(f"⚠️ Errore DB (getting MIN_msg_id): {e}")
            oldest_message_id = None
        
        try:
            self.log_callback(f"🚀 Inizio scansione storica...")
            
            total_messages = 0
            processed_messages = 0
            
            history_iter = channel.history(
                limit=None,
                before=discord.Object(id=oldest_message_id) if oldest_message_id else None,
                oldest_first=False 
            )
            
            async for message in history_iter:
                total_messages += 1
                
                if SEARCH_STRING not in message.content:
                    continue
                
                # ✅ PASSO 1: ESTRAI I DATI DAL MESSAGGIO
                try:
                    trade_data, xml_att, img_att = extract_trade_data_fast(message)
                    trade_data['message_id'] = message.id
                except Exception as e:
                    self.log_callback(f"⚠️ Errore estrazione dati msg {message.id}: {e}")
                    continue
                
                # ================================================================
                # ✅ PASSO 2: LEGGI ALLEGATI IN MEMORIA
                # ================================================================
                account_name = trade_data.get('account_name')
                trade_data['xml_path'] = None
                trade_data['image_url'] = None
                xml_content_bytes = None
                image_content_bytes = None

                try:
                    if xml_att:
                        xml_content_bytes = await xml_att.read()
                    if img_att:
                        image_content_bytes = await img_att.read()
                        trade_data['image_url'] = img_att.url # Salva l'URL
                except Exception as e:
                    self.log_callback(f"⚠️ Errore lettura allegati in RAM (Storico): {e}")
                    continue

                # ================================================================
                # ✅ GESTIONE XML IN-MEMORIA E SYNC INVENTARIO
                # ================================================================
                if xml_content_bytes:
                    try:
                        root = ET.fromstring(xml_content_bytes)
                        data = {child.get('name'): child.text for child in root.findall('string')}
                        d_acc, d_pass = data.get('deviceAccount'), data.get('devicePassword')
                        
                        if d_acc and d_pass:
                            account_id = self._get_or_create_account(account_name, d_acc, d_pass)
                            # Esegui il sync (non aspettarlo, lascialo andare in background)
                            trade_data['xml_path'] = "DB_STORED"
                    except Exception as e:
                        self.log_callback(f"⚠️ Errore parsing XML in-memory (Storico): {e}")

                # ================================================================
                # ✅ GESTIONE IMMAGINE (Thumbnail BLOB)
                # ================================================================
                screenshot_thumb_blob = None
                if image_content_bytes:
                    # Crea la piccola miniatura da 60x60 per il DB
                    screenshot_thumb_blob = self._create_screenshot_thumbnail(image_content_bytes)
                
                # ================================================================
                # ✅ PASSO 3: INSERISCI IL TRADE NEL DATABASE
                # ================================================================
                try:
                    account_id = self._get_or_create_account(account_name)
                    cursor = self.db_conn.cursor()
                    cursor.execute("""
                        INSERT OR IGNORE INTO trades 
                        (message_id, account_id, account_name, xml_path, image_url, 
                         screenshot_thumbnail_blob, message_link, cards_found_text, scan_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        str(trade_data['message_id']),
                        account_id,
                        account_name,
                        trade_data.get('xml_path'),
                        trade_data.get('image_url'),    # <-- L'URL dello screenshot
                        screenshot_thumb_blob,          # <-- La miniatura BLOB
                        trade_data.get('message_link'),
                        trade_data.get('cards_found_text'),
                        0 # scan_status = 0 (In attesa)
                    ))
                    self.db_conn.commit()
                    
                    # Aggiungi alla UI (Tab Bot)
                    trade_data['screenshot_thumbnail_blob'] = screenshot_thumb_blob
                    self.trade_callback(trade_data) 
                    
                except Exception as e:
                    self.log_callback(f"⚠️ Errore INSERT trade msg {message.id}: {e}")
                    continue
                
                # ✅ PASSO 4: SCANSIONA IMMAGINE (Passa i bytes completi)
                if image_content_bytes:
                    try:
                        await self.scan_image_for_cards(trade_data, image_content_bytes) 
                    except Exception as e:
                        self.log_callback(f"⚠️ Errore scansione immagine: {e}")
                
                # Aggiorna progress
                processed_messages += 1
                status = f"Scansione storica: {processed_messages} processati"
                self.progress_callback({'percent': -1, 'status': status}) # Modalità "busy"
            
            self.log_callback(f"✅ Scansione storica completata: {processed_messages} messaggi elaborati")
            self.progress_callback({'percent': 100, 'status': 'Scansione storica completata'})
            
        except Exception as e:
            self.log_callback(f"❌ Errore scansione storica: {e}")
            import traceback
            self.log_callback(traceback.format_exc())
        
        finally:
            # Non impostare initial_scan_done qui, lascia che on_ready lo faccia
            pass

        
    async def perform_incremental_scan_fast(self):
        """Scansione incrementale con cache ottimizzato."""
        start_time = time.time()
        
        channel = self.get_channel(int(os.getenv('CHANNEL_ID', '0')))
        if not channel:
            self.log_callback("❌ Canale non trovato")
            self.initial_scan_done = True
            return
        
        # ❌ RIMOSSO: max_msg_id_from_log
        
        # ✅ CACHE OTTIMIZZATO - Aggiorna ogni 60s
        if self.last_cache_update == 0 or (time.time() - self.last_cache_update > 60):
            try:
                cursor = self.db_conn.cursor()
                # ✅ MODIFICATO: Interroga la nuova tabella 'trades'
                cursor.execute("SELECT MAX(CAST(message_id AS INTEGER)) FROM trades")
                result = cursor.fetchone()
                if result and result[0]:
                    self.last_message_id_cache = int(result[0])
                self.last_cache_update = time.time()
            except Exception as e:
                self.log_callback(f"⚠️ Errore cache: {e}")
        
        max_msg_id = self.last_message_id_cache
        
        if max_msg_id > 0:
            
            new_messages = []
            try:
                async for message in channel.history(limit=None, after=discord.Object(id=max_msg_id), oldest_first=False):
                    new_messages.append(message)
            except Exception as e:
                self.log_callback(f"❌ Errore fetch: {e}")
                return
            
            if new_messages:
                self.log_callback(f"📨 Trovati {len(new_messages)} messaggi")
                batch = []
                
                for i, message in enumerate(new_messages):
                    batch.append(message)
                    percent = int(((i + 1) / len(new_messages)) * 100) if len(new_messages) > 0 else 0
                    status = f"Nuovi messaggi {i+1}/{len(new_messages)}"
                    self.progress_callback({'percent': percent, 'status': status})
                    
                    if len(batch) >= BATCH_SIZE:
                        await self.process_message_batch_fast(batch)
                        batch = []
                
                if batch:
                    await self.process_message_batch_fast(batch)
                
                # ❌ RIMOSSO: save_trade_log_fast(self.trade_log)
                
                elapsed = time.time() - start_time
                self.log_callback(f"✅ Elaborati {len(new_messages)} messaggi in {elapsed:.2f}s")
            else:
                self.log_callback("✅ Nessun nuovo messaggio")
    
    async def download_with_semaphore(self, trade_data, attachment, folder, filename, att_type):
        """Download con semaphore."""
        async with self.semaphore:
            file_path, status = await download_attachment_fast(self.session, attachment, folder, filename)
            
            if file_path:
                if att_type == 'image':
                    trade_data['image_path'] = file_path
                elif att_type == 'xml':
                    trade_data['xml_path'] = file_path
    
    # In discord_client.py

    async def process_message_batch_fast(self, messages: List) -> int:
        """
        Processa batch in PARALLELO.
        MODIFICATO: Legge XML e Immagine in memoria.
        """
        if not messages: return 0
        
        trades_to_insert = [] 
        tasks_to_run = [] # Task di scansione e sync
        
        cursor = self.db_conn.cursor()
        
        for message in messages:
            if SEARCH_STRING not in message.content: continue
            
            try: # Controllo duplicati (invariato)
                cursor.execute("SELECT 1 FROM trades WHERE message_id = ?", (str(message.id),))
                if cursor.fetchone(): continue 
            except Exception as e:
                print(f"Errore controllo duplicato: {e}")
                continue

            trade_data, xml_att, img_att = extract_trade_data_fast(message)
            trade_data['xml_path'] = None
            trade_data['image_url'] = None
            
            # Dati che servono per i task asincroni
            account_name = trade_data.get('account_name')
            
            # Variabili per i dati in-memory
            xml_content_bytes = None
            image_content_bytes = None
            
            # ================================================================
            # ✅ LEGGI ALLEGATI IN MEMORIA
            # ================================================================
            try:
                if xml_att:
                    xml_content_bytes = await xml_att.read()
                if img_att:
                    image_content_bytes = await img_att.read()
                    trade_data['image_url'] = img_att.url # Salva l'URL
            except Exception as e:
                self.log_callback(f"⚠️ Errore lettura allegati in RAM: {e}")
                continue # Salta questo messaggio
            
            # ================================================================
            # ✅ GESTIONE XML IN-MEMORIA E SYNC INVENTARIO
            # ================================================================
            if xml_content_bytes:
                try:
                    root = ET.fromstring(xml_content_bytes)
                    data = {child.get('name'): child.text for child in root.findall('string')}
                    d_acc, d_pass = data.get('deviceAccount'), data.get('devicePassword')
                    
                    if d_acc and d_pass:
                        account_id = self._get_or_create_account(account_name, d_acc, d_pass)
                        # Aggiungi il task di sync inventario
                        trade_data['xml_path'] = "DB_STORED"
                except Exception as e:
                    self.log_callback(f"⚠️ Errore parsing XML in-memory: {e}")

            # ================================================================
            # ✅ GESTIONE IMMAGINE IN-MEMORIA E SCANSIONE
            # ================================================================
            screenshot_thumb_blob = None
            if image_content_bytes:
                # 1. Crea thumbnail per il DB
                screenshot_thumb_blob = self._create_screenshot_thumbnail(image_content_bytes)
                
                # 2. Aggiungi il task di scansione
                tasks_to_run.append(self.scan_image_for_cards(trade_data, image_content_bytes))
            
            # ================================================================
            # ✅ STEP 3: PREPARA E SALVA I TRADE NEL DB
            # ================================================================
            self.trade_callback(trade_data) # Aggiorna la UI (Tab Bot)
            
            account_id = self._get_or_create_account(account_name)
            
            trades_to_insert.append((
                str(trade_data['message_id']),
                account_id,
                account_name,
                trade_data.get('xml_path'),
                trade_data.get('image_url'), # <-- URL salvato
                screenshot_thumb_blob,       # <-- Thumbnail salvato
                trade_data.get('message_link'),
                trade_data.get('cards_found_text'),
                0 # scan_status = 0 (In attesa)
            ))

        # Fine loop messaggi
        
        # Salva tutti i trade nel DB
        if trades_to_insert:
            try:
                cursor.executemany("""
                    INSERT OR IGNORE INTO trades 
                    (message_id, account_id, account_name, xml_path, image_url, 
                     screenshot_thumbnail_blob, message_link, cards_found_text, scan_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, trades_to_insert)
                self.db_conn.commit()
            except Exception as e:
                self.log_callback(f"❌ Errore INSERT batch trades: {e}")

        # ✅ STEP 4: ESEGUI TUTTI I TASK (Sync Inventario + Scansione Immagini)
        if tasks_to_run:
            await asyncio.gather(*tasks_to_run, return_exceptions=True)
        
        return len(trades_to_insert)



    async def scan_image_for_cards(self, trade_data, image_bytes):
        """
        Scansiona immagine (dai bytes) e fa UPDATE sul DB con i risultati.
        MODIFICATO: Passa un oggetto PIL.Image al recognizer.
        """
        if self.card_recognition_executor._shutdown or not image_bytes:
            return
        
        account_name = trade_data.get('account_name')
        message_id = str(trade_data.get('message_id'))
        
        scan_status = 2 # Default = Errore
        results_json = "[]"
        results = []

        try:
            # ✅ Converti i bytes in un oggetto PIL.Image
            source_img = Image.open(io.BytesIO(image_bytes))

            loop = asyncio.get_event_loop()
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    self.card_recognition_executor,
                    # ✅ MODIFICATO: Chiama la nuova funzione in-memory
                    self.card_recognizer.recognize_from_image, 
                    source_img,
                    False, # save_to_db
                    None,  # account_name
                    None   # image_path_for_db
                ),
                timeout=30.0
            )
            scan_status = 1 # Successo
            if results:
                results_json = json.dumps(results) # Salva il JSON completo

        except asyncio.TimeoutError:
            self.log_callback(f"⏱️ Timeout: Scansione fallita per {message_id}")
        except RuntimeError as e:
            if "cannot schedule new futures after shutdown" in str(e): return
            raise
        except Exception as e:
            self.log_callback(f"❌ Errore scansione immagine {message_id}: {e}")

        # ================================================================
        # ✅ PASSO 1: Aggiorna la tabella 'trades' con i risultati
        # ================================================================
        try:
            cursor = self.db_conn.cursor()
            cursor.execute("""
                UPDATE trades 
                SET scan_status = ?, scan_results_json = ?
                WHERE message_id = ?
            """, (scan_status, results_json, message_id))
            self.db_conn.commit()
        except Exception as e:
            self.log_callback(f"❌ Errore UPDATE trade {message_id}: {e}")

        if not results:
            return # Nessuna carta trovata
        
        # ================================================================
        # ✅ PASSO 2: Invia le carte trovate alla UI (Batch Writer)
        # ================================================================
        
        filtered_cards = [c for c in results if c.get('rarity') in SELECTED_RARITIES]
        
        if not filtered_cards:
            return
        
        
        for card_data in filtered_cards:
            try:
                callback_data = {
                    "account_name": account_name,
                    "card_name": card_data.get('card_name', 'Unknown'),
                    "card_number": card_data.get('card_number', '?'),
                    "set_code": card_data.get('set_code', 'Unknown'),
                    "rarity": card_data.get('rarity', 'NA'),
                    "similarity": card_data.get('similarity', 0),
                    "image_path": trade_data.get('image_url', ''), # URL Screenshot
                    "local_image_path": card_data.get('local_image_path', ''), # URL Carta
                    "message_id": message_id,
                }
                self.card_found_callback(callback_data) 
            except Exception as e:
                self.log_callback(f"❌ Errore callback: {e}")



    async def on_message(self, message):
        """
        Chiamato quando viene ricevuto un nuovo messaggio.
        MODIFICATO: Rimosso il controllo 'processed_message_ids' e il salvataggio JSON.
        """
        if not self.initial_scan_done:
            return
        
        # Il controllo duplicati è ora gestito da 'process_message_batch_fast'
        if SEARCH_STRING in message.content:
            self.log_callback(t("misc.new_trade_detected", id=message.id))
            await self.process_message_batch_fast([message])
            
            # ❌ RIMOSSO: save_trade_log_fast(self.trade_log)




def extract_trade_data_fast(message):
    """
    Estrae i dati del trade da un messaggio Discord.
    MODIFICATO: Estrae 'cards_found_text' per il DB.
    """
    content = message.content
    
    # 1️⃣ Estrai l'account_name (Logica invariata)
    account_name = "unknown_account"
    for att in message.attachments:
        if att.filename.endswith(".xml"):
            account_name = att.filename.replace(".xml", "").strip()
            break
    
    if account_name == "unknown_account":
        file_pattern = r'File name: ([\w\-\(\)]+\.xml)'
        match = re.search(file_pattern, content)
        if match:
            xml_filename = match.group(1)
            account_name = xml_filename.replace(".xml", "").strip()
    
    # 2️⃣ Estrai il nome del file XML dal testo (Logica invariata)
    xml_filename_text = "N/A"
    file_line_match = re.search(r'File name: ([\w\-\(\)\.]+)', content)
    if file_line_match:
        xml_filename_text = file_line_match.group(1)
    
    # ================================================================
    # ✅ MODIFICATO: Estrai il testo "Found:"
    # ================================================================
    cards_found_text = ""
    cards_pattern = r'Found: ([\w\s]+(?:\s*\(x\d+\))?(?:,\s*[\w\s]+\s*\(x\d+\))*)'
    cards_match = re.search(cards_pattern, content)
    if cards_match:
        cards_found_text = cards_match.group(1).strip()
    # ================================================================
    
    # 4️⃣ Estrai gli allegati (Logica invariata)
    xml_att = None
    image_att = None
    
    for att in message.attachments:
        if not xml_att and att.filename.endswith('.xml'):
            xml_att = att
        elif not image_att and att.filename.endswith(('.png', '.jpg', '.jpeg', '.webp')):
            image_att = att
        
        if xml_att and image_att:
            break
    
    return {
        "message_id": message.id,
        "account_name": account_name,
        "xml_filename_text": xml_filename_text,
        "cards_found_text": cards_found_text, # <-- ✅ Per il DB (tabella trades)
        "cards_found": cards_found_text,      # <-- Per compatibilità UI (tabella trades_table)
        "message_link": message.jump_url,
        "elaborato": False,  
        "cards": []  
    }, xml_att, image_att

    
async def download_attachment_fast(session: aiohttp.ClientSession, attachment,
                                   sub_folder: str, filename: str) -> Tuple[Optional[str], str]:
    """Downloads an attachment from Discord."""
    os.makedirs(sub_folder, exist_ok=True)
    file_path = os.path.join(sub_folder, filename)
    
    if os.path.exists(file_path):
        return file_path, 'skipped'
    
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(attachment.url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    with open(file_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            f.write(chunk)
                    return file_path, 'downloaded'
                elif attempt == MAX_RETRIES - 1:
                    return None, 'failed'
        except:
            if attempt == MAX_RETRIES - 1:
                return None, 'failed'
        await asyncio.sleep(RETRY_DELAY)
    
    return None, 'failed'

#def load_trade_log_fast():
#    """Loads the trade log from the JSON file."""
#    if os.path.exists(LOG_FILENAME):
#        try:
#            with open(LOG_FILENAME, 'r', encoding='utf-8') as f:
#                return json.load(f)
#        except:
#            return []
#    return []
#
#def save_trade_log_fast(trade_log):
#    """Saves the trade log to the JSON file with pretty formatting."""
#    try:
#        with open(LOG_FILENAME, 'w', encoding='utf-8') as f:
#            json.dump(
#                trade_log, 
#                f, 
#                ensure_ascii=False, 
#                indent=4,  # ✅ PRETTIFY: indentazione di 2 spazi
#                sort_keys=False  # ✅ Mantiene l'ordine delle chiavi
#            )
#    except:
#        pass
