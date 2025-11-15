# core/cards_found_tab.py
"""
Questo modulo contiene il QWidget per la scheda "Cards Found".
"""

# Import PyQt5
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QLabel, QPushButton, QHeaderView, QFileDialog, QMessageBox, QWidget,
    QHBoxLayout, QLabel
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QRunnable, pyqtSlot
from PyQt5.QtGui import QPixmap

# Import standard
import os
import sqlite3
import json
import csv
import base64
import urllib.request
from typing import TYPE_CHECKING

# Import moduli app
from config import DB_FILENAME, RARITY_DATA, get_app_data_path, get_resource_path
from .translations import t

# Import classi helper per il download (copiate da collection_tab)
class ImageLoaderSignals(QObject):
    finished = pyqtSignal(bytes, str, QLabel)
    error = pyqtSignal(str, str, QLabel)

class ImageDownloaderWorker(QRunnable):
    def __init__(self, image_url: str, target_label: QLabel):
        super().__init__()
        self.image_url = image_url
        self.target_label = target_label
        self.signals = ImageLoaderSignals()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

    @pyqtSlot()
    def run(self):
        if not self.image_url or not self.image_url.startswith('http'):
            self.signals.error.emit("URL non valido", self.image_url, self.target_label)
            return
        try:
            req = urllib.request.Request(self.image_url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=10) as response:
                image_data = response.read()
            if image_data:
                self.signals.finished.emit(image_data, self.image_url, self.target_label)
            else:
                self.signals.error.emit("Dati immagine vuoti", self.image_url, self.target_label)
        except Exception as e:
            self.signals.error.emit(str(e), self.image_url, self.target_label)

# Type checking
if TYPE_CHECKING:
    from .ui_main_window import MainWindow

class CardsFoundTab(QWidget):
    
    def __init__(self, main_window: 'MainWindow', parent=None):
        super().__init__(parent)
        
        # Riferimenti
        self.main_window = main_window
        
        # Risorse condivise
        self.image_cache = main_window.image_cache
        self.image_loader_pool = main_window.image_loader_pool
        self.placeholder_pixmap = main_window.placeholder_pixmap
        
        # Stato interno
        self.cards_offset = 0
        self.found_cards_list = [] # Sostituisce 'self.found_cards' di MainWindow
        
        # Avvia UI
        self.setup_ui()
        
        # Carica dati iniziali
        self.load_found_cards_from_database()

    def setup_ui(self):
        """Configura l'interfaccia utente di questa scheda."""
        cards_layout = QVBoxLayout(self)
        
        # Controls
        controls_layout = QHBoxLayout()
        
        self.clear_cards_btn = QPushButton(t("ui.clear_list"))
        self.clear_cards_btn.clicked.connect(self.clear_cards_list)
        controls_layout.addWidget(self.clear_cards_btn)
        
        self.export_cards_btn = QPushButton(t("ui.export_csv"))
        self.export_cards_btn.clicked.connect(self.export_cards_to_csv)
        controls_layout.addWidget(self.export_cards_btn)
        
        controls_layout.addStretch()
        
        self.cards_count_label = QLabel(t("ui.total_cards_found", count=0))
        self.cards_count_label.setStyleSheet("QLabel { font-size: 12px; font-weight: bold; }")
        controls_layout.addWidget(self.cards_count_label)
        
        cards_layout.addLayout(controls_layout)
        
        # Cards Table
        self.cards_table = QTableWidget()
        self.cards_table.setColumnCount(8)
        self.cards_table.setHorizontalHeaderLabels([
            t("ui.table.card"), t("ui.table.pack"), t("ui.table.account"),
            t("ui.table.set"), t("ui.table.card_number"), t("ui.table.card_name"),
            t("ui.table.rarity"), "Similarity"
        ])
        self.cards_table.horizontalHeader().setStretchLastSection(True)
        self.cards_table.setAlternatingRowColors(True)
        self.cards_table.setSortingEnabled(True)

        self.cards_table.setColumnWidth(0, 70)
        self.cards_table.setColumnWidth(1, 70)
        self.cards_table.setColumnWidth(2, 100)
        self.cards_table.setColumnWidth(3, 60)
        self.cards_table.setColumnWidth(4, 80)
        self.cards_table.setColumnWidth(5, 150)
        self.cards_table.setColumnWidth(6, 80)
        self.cards_table.setColumnWidth(7, 80)
        self.cards_table.verticalHeader().setDefaultSectionSize(70)
        
        self.cards_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.cards_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.cards_table.setStyleSheet("""
            QTableWidget::item:selected {
                background-color: #f39c12; color: #000000;
            }
        """)
        cards_layout.addWidget(self.cards_table)
        
        # Load More Button
        self.load_more_btn = QPushButton("📥 " + t("ui.load_more", count=20)) # t()
        self.load_more_btn.clicked.connect(self.on_load_more_cards)
        load_more_layout = QHBoxLayout()
        load_more_layout.addStretch()   
        self.load_more_btn.setMaximumWidth(200) # Aumentato
        load_more_layout.addWidget(self.load_more_btn)
        
        cards_layout.addLayout(load_more_layout)
    
    # --- Metodo Pubblico per MainWindow ---
    
    def add_new_card(self, card_data: dict):
        """
        Metodo pubblico chiamato da MainWindow (on_card_found) 
        per aggiungere una carta in cima alla lista.
        """
        self.add_card_to_table(card_data, insert_at_top=True)
        
        # Aggiorna il conteggio
        self.cards_count_label.setText(t("ui.total_cards_found", count=self.cards_table.rowCount()))
        
        # Rimuovi la riga più vecchia se superiamo il limite (es. 20)
        # Questo mantiene la tabella reattiva
        while self.cards_table.rowCount() > 20:
            self.cards_table.removeRow(self.cards_table.rowCount() - 1)
            
    # --- Logica Interna ---

    def on_load_more_cards(self):
        """Callback per caricamento altre cards."""
        if self.load_more_found_cards():
            self.load_more_btn.setText("📥 " + t("ui.load_more", count=20))
        else:
            self.load_more_btn.setEnabled(False)
            self.load_more_btn.setText("✓ " + t("ui.all_cards_loaded")) # t()
            
    def _get_rarity_filter(self) -> list:
        """Carica le rarità selezionate da settings.json."""
        try:
            settings_path = get_app_data_path("settings.json")
        except:
            settings_path = "settings.json"   
        
        saved_rarities = list(RARITY_DATA.keys()) # Default
        if os.path.exists(settings_path):
            try:
                with open(settings_path, 'r', encoding="utf-8") as f:
                    settings = json.load(f)       
                saved_rarities = settings.get('selected_rarities', list(RARITY_DATA.keys()))
            except Exception:
                pass # Usa il default
        return saved_rarities

    def load_found_cards_from_database(self):
        """Carica le prime 20 carte trovate dal database."""
        try:
            self.cards_table.setRowCount(0)
            self.cards_offset = 20 # Imposta l'offset per il prossimo caricamento
            
            saved_rarities = self._get_rarity_filter()
            rarity_placeholders = ', '.join('?' for _ in saved_rarities)

            with sqlite3.connect(DB_FILENAME) as conn:
                cursor = conn.cursor()
                
                sql_query = f"""
                    SELECT 
                        c.card_name, c.rarity,
                        COALESCE(a.account_name, 'Unknown') as account_name,
                        c.set_code, c.card_number,
                        c.thumbnail_blob,     -- [5] (BLOB Carta)
                        t.image_url,          -- [6] (URL Screenshot)
                        c.local_image_path,   -- [7] (URL Carta)
                        t.screenshot_thumbnail_blob, -- [8] (BLOB Screenshot)
                        fc.confidence_score   -- [9]
                    FROM found_cards fc
                    JOIN cards c ON fc.card_id = c.id
                    LEFT JOIN accounts a ON fc.account_id = a.account_id
                    LEFT JOIN trades t ON fc.message_id = t.message_id
                    WHERE c.rarity IN ({rarity_placeholders})
                    ORDER BY fc.found_at DESC
                    LIMIT 20
                """
                
                cursor.execute(sql_query, saved_rarities)
                cards = cursor.fetchall()

                # Conta totale
                cursor.execute(f"""
                    SELECT COUNT(fc.id) FROM found_cards fc
                    JOIN cards c ON fc.card_id = c.id
                    WHERE c.rarity IN ({rarity_placeholders})
                """, saved_rarities)
                total_count = cursor.fetchone()[0]
            
            self.cards_count_label.setText(f"{t('ui.cards_found')}: {total_count} ({t('ui.showing')} {len(cards)})") # t()
            
            for card_data in cards:
                card_dict = {
                    'card_name': card_data[0],
                    'rarity': card_data[1],
                    'account_name': card_data[2],
                    'set_code': card_data[3],
                    'card_number': str(card_data[4]),
                    'thumbnail_blob': card_data[5],
                    'image_url_screenshot': card_data[6],
                    'image_url': card_data[7],
                    'screenshot_thumbnail_blob': card_data[8],
                    'similarity': (card_data[9] * 100) if card_data[9] else 0
                }
                self.add_card_to_table(card_dict, insert_at_top=False)
                    
        except Exception as e:
            print(f"❌ Errore load_found_cards: {e}")
            import traceback
            traceback.print_exc()

    def load_more_found_cards(self):
        """Carica più carte trovate (paginazione)."""
        try:
            saved_rarities = self._get_rarity_filter()
            rarity_placeholders = ', '.join('?' for _ in saved_rarities)

            with sqlite3.connect(DB_FILENAME) as conn:
                cursor = conn.cursor()
                
                sql_query = f"""
                    SELECT 
                        c.card_name, c.rarity,
                        COALESCE(a.account_name, 'Unknown') as account_name,
                        c.set_code, c.card_number,
                        c.thumbnail_blob,     -- [5] (BLOB Carta)
                        t.image_url,          -- [6] (URL Screenshot)
                        c.local_image_path,   -- [7] (URL Carta)
                        t.screenshot_thumbnail_blob, -- [8] (BLOB Screenshot)
                        fc.confidence_score   -- [9]
                    FROM found_cards fc
                    JOIN cards c ON fc.card_id = c.id
                    LEFT JOIN accounts a ON fc.account_id = a.account_id
                    LEFT JOIN trades t ON fc.message_id = t.message_id
                    WHERE c.rarity IN ({rarity_placeholders})
                    ORDER BY fc.found_at DESC
                    LIMIT 20 OFFSET ?
                """
                
                params = saved_rarities + [self.cards_offset]
                cursor.execute(sql_query, params)
                cards = cursor.fetchall()
                
                if not cards:
                    self.main_window.append_bot_log("✅ " + t("ui.no_more_cards_to_load")) #t()
                    return False # Disabilita pulsante
            
            self.main_window.append_bot_log(f"📦 {t('ui.loaded_cards', count=len(cards))} (offset: {self.cards_offset})") #t()
            
            for card_data in cards:
                card_dict = {
                    'card_name': card_data[0],
                    'rarity': card_data[1],
                    'account_name': card_data[2],
                    'set_code': card_data[3],
                    'card_number': str(card_data[4]),
                    'thumbnail_blob': card_data[5],
                    'image_url_screenshot': card_data[6],
                    'image_url': card_data[7],
                    'screenshot_thumbnail_blob': card_data[8],
                    'similarity': (card_data[9] * 100) if card_data[9] else 0
                }
                self.add_card_to_table(card_dict, insert_at_top=False)
            
            self.cards_offset += 20
            return True # Continua
        
        except Exception as e:
            self.main_window.append_bot_log(f"❌ {t('ui.error_loading_cards')}: {e}") #t()
            import traceback
            traceback.print_exc()
            return False

    def add_card_to_table(self, card_data: dict, insert_at_top: bool = False):
        """Aggiunge una riga alla tabella (in cima o in fondo)."""
        
        row = 0 if insert_at_top else self.cards_table.rowCount()
        self.cards_table.insertRow(row)
        
        # COL 0: MINIATURA CARTA
        card_preview_label = QLabel()
        card_preview_label.setAlignment(Qt.AlignCenter)
        card_preview_label.setFixedSize(60, 60)
        image_blob = card_data.get('thumbnail_blob') 
        
        # ================================================================
        # ✅ FIX: Controlla entrambe le possibili chiavi per l'URL
        # ================================================================
        card_image_url = card_data.get('image_url') or card_data.get('local_image_path')
        # ================================================================

        pixmap = self.image_cache.get(card_image_url)
        
        if pixmap:
            card_preview_label.setPixmap(pixmap.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        elif image_blob:
            pixmap = QPixmap()
            pixmap.loadFromData(image_blob) 
            if not pixmap.isNull():
                pixmap = pixmap.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                # Ora card_image_url non è None, quindi la cache funziona
                if card_image_url:
                    self.image_cache.put(card_image_url, pixmap)
                card_preview_label.setPixmap(pixmap)
            else:
                card_preview_label.setText("❌")
        elif card_image_url:
            # Passa le dimensioni corrette per il fallback
            self.load_card_image_async(card_image_url, card_preview_label, 60, 60)
        else:
            card_preview_label.setText("🎴")
        
        card_preview_label.setToolTip(self.create_image_tooltip(
            image_blob, card_data.get('card_name', 'Carta')
        ))
        self.cards_table.setCellWidget(row, 0, card_preview_label)

        # COL 1: MINIATURA PACCHETTO
        pack_preview_label = QLabel()
        pack_preview_label.setAlignment(Qt.AlignCenter)
        pack_preview_label.setFixedSize(60, 60)
        pack_blob = card_data.get('screenshot_thumbnail_blob')
        
        if pack_blob:
            pixmap = QPixmap()
            pixmap.loadFromData(pack_blob)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(60, 60, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                pack_preview_label.setPixmap(pixmap)
            else:
                pack_preview_label.setText("❌")
        else:
            pack_preview_label.setText("📦") 
        
        pack_preview_label.setToolTip(self.create_image_tooltip(
            pack_blob, "Screenshot"
        ))
        self.cards_table.setCellWidget(row, 1, pack_preview_label)
        
        # ALTRE COLONNE
        self.cards_table.setItem(row, 2, QTableWidgetItem(card_data.get('account_name', '')))
        self.cards_table.setItem(row, 3, QTableWidgetItem(card_data.get('set_code', '')))
        self.cards_table.setItem(row, 4, QTableWidgetItem(str(card_data.get('card_number', ''))))
        self.cards_table.setItem(row, 5, QTableWidgetItem(card_data.get('card_name', '')))
        
        # Colonna Rarità con Icona
        rarity_name = card_data.get('rarity', 'NA')
        rarity_widget = QWidget()
        rarity_layout = QHBoxLayout(rarity_widget)
        rarity_layout.setContentsMargins(0, 0, 0, 0)
        rarity_layout.setAlignment(Qt.AlignCenter)
        
        if rarity_name in RARITY_DATA:
            icon_full_path = get_resource_path(RARITY_DATA[rarity_name])
            if os.path.exists(icon_full_path):
                rarity_icon_label = QLabel()
                rarity_icon_label.setAlignment(Qt.AlignCenter)
                pixmap = QPixmap(icon_full_path)
                pixmap = pixmap.scaledToHeight(25, Qt.SmoothTransformation)
                rarity_icon_label.setPixmap(pixmap)
                rarity_icon_label.setToolTip(rarity_name)
                rarity_layout.addWidget(rarity_icon_label)
            else:
                rarity_layout.addWidget(QLabel(rarity_name)) # Fallback testo
        else:
            rarity_layout.addWidget(QLabel(rarity_name)) # Fallback testo
            
        self.cards_table.setCellWidget(row, 6, rarity_widget)
        
        self.cards_table.setItem(row, 7, QTableWidgetItem(f"{card_data.get('similarity', 0):.1f}%"))
        
        if insert_at_top:
            self.found_cards_list.insert(0, card_data)
        else:
            self.found_cards_list.append(card_data)

    def clear_cards_list(self):
        """Pulisce la lista delle carte trovate."""
        reply = QMessageBox.question(self, 'Confirm', 
                                     'Are you sure you want to clear the cards list?',
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            self.cards_table.setRowCount(0)
            self.found_cards_list.clear()
            self.cards_count_label.setText(t("ui.total_cards_found", count=0))
    
    def export_cards_to_csv(self):
        """Esporta le carte trovate in CSV."""
        if self.cards_table.rowCount() == 0:
            QMessageBox.information(self, "Info", "No cards to export")
            return
        
        filename, _ = QFileDialog.getSaveFileName(self, "Save CSV", "", "CSV Files (*.csv)")
        
        if filename:
            try:
                import csv
                with open(filename, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    headers = [self.cards_table.horizontalHeaderItem(c).text() for c in range(self.cards_table.columnCount())]
                    writer.writerow(headers)
                    
                    for row in range(self.cards_table.rowCount()):
                        row_data = []
                        for col in range(self.cards_table.columnCount()):
                            if col == 0 or col == 1 or col == 6: # Colonne Widget
                                item = self.cards_table.cellWidget(row, col)
                                if isinstance(item, QWidget) and item.layout() and item.layout().itemAt(0):
                                    label = item.layout().itemAt(0).widget()
                                    row_data.append(label.toolTip()) # Esporta il tooltip (nome rarità)
                                else:
                                    row_data.append("N/A")
                            else: # Colonne Testo
                                item = self.cards_table.item(row, col)
                                row_data.append(item.text() if item else '')
                        writer.writerow(row_data)
                
                QMessageBox.information(self, "Success", f"Exported {self.cards_table.rowCount()} cards to {filename}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export: {str(e)}")

    # --- Funzioni Helper (Copiate) ---

    def create_image_tooltip(self, blob_data, text_fallback=""):
        if not blob_data: return text_fallback
        try:
            b64_data = base64.b64encode(blob_data).decode('utf-8')
            return f'<html><img src="data:image/jpeg;base64,{b64_data}"></html>'
        except Exception as e:
            return text_fallback

    def load_card_image_async(self, image_url: str, target_label: QLabel, scale_w: int, scale_h: int):
        if not image_url:
            target_label.setPixmap(self.placeholder_pixmap.scaled(scale_w, scale_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return
        pixmap = self.image_cache.get(image_url)
        if pixmap:
            target_label.setPixmap(pixmap.scaled(scale_w, scale_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            return
        
        target_label.setPixmap(self.placeholder_pixmap.scaled(scale_w, scale_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        worker = ImageDownloaderWorker(image_url, target_label)
        worker.signals.finished.connect(self.on_image_loaded)
        worker.signals.error.connect(self.on_image_load_error)
        self.image_loader_pool.start(worker)

    @pyqtSlot(bytes, str, QLabel)
    def on_image_loaded(self, image_data: bytes, image_url: str, target_label: QLabel):
        try:
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            if pixmap.isNull(): raise Exception("Impossibile caricare QPixmap")
            scaled_pixmap = pixmap.scaled(target_label.width(), target_label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_cache.put(image_url, scaled_pixmap)
            if target_label and target_label.isVisible():
                target_label.setPixmap(scaled_pixmap)
        except Exception as e:
            print(f"❌ Errore on_image_loaded: {e}")

    @pyqtSlot(str, str, QLabel)
    def on_image_load_error(self, error_msg: str, image_url: str, target_label: QLabel):
        pass # Lascia il segnaposto