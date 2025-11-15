# core/collection_tab.py
"""
Questo modulo contiene l'intero QWidget per la scheda "Collezione",
inclusa la logica di caricamento, i filtri e la visualizzazione della griglia.
"""

# Import PyQt5
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QScrollArea, QFrame,
    QComboBox, QLineEdit, QToolButton, QSizePolicy, 
    QGraphicsOpacityEffect, QApplication
)
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QSize, QThreadPool, QRunnable, QObject, pyqtSlot, QUrl
from PyQt5.QtGui import QPixmap, QFont

# Import standard
import os
import sqlite3
import urllib.request
from typing import TYPE_CHECKING

# Import moduli app
from config import DB_FILENAME, RARITY_DATA, get_app_data_path, get_resource_path
from .translations import t
from .database import DatabaseManager
from .wishlist_manager import WishlistManager

# Questo trucco di typing evita un'importazione circolare,
# permettendo a PyCharm/VSCode di sapere che 'main_window' è di tipo 'MainWindow'
if TYPE_CHECKING:
    from .ui_main_window import MainWindow

from .ui_widgets import CollectionCardDialog

# ================================================================
# 1. CLASSI HELPER PER IL DOWNLOAD ASINCRONO
# (Spostate da ui_main_window.py)
# ================================================================

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
            print(f"❌ Errore download miniatura UI: {e} | URL: {self.image_url}")
            self.signals.error.emit(str(e), self.image_url, self.target_label)


# ================================================================
# 2. CLASSE PRINCIPALE DELLA SCHEDA COLLEZIONE
# ================================================================

class CollectionTab(QWidget):
    
    def __init__(self, main_window: 'MainWindow', parent=None):
        super().__init__(parent)
        
        # 1. Riferimenti alla finestra principale
        self.main_window = main_window
        
        # 2. Dipendenze (passate dalla finestra principale)
        self.db_manager = main_window.db
        self.wishlist_manager = main_window.wishlist_manager
        
        # 3. Risorse condivise (dalla finestra principale)
        self.image_loader_pool = main_window.image_loader_pool
        self.placeholder_pixmap = main_window.placeholder_pixmap
        self.image_cache = main_window.image_cache
        
        # 4. Stato interno della scheda
        self.collection_card_widgets = {} # Cache per i filtri
        self.inventory_map = {}           # Cache dell'inventario corrente
        self.wishlist_map = {}            # DEPRECATO (ora in wishlist_manager)
        self.collection_loaded = False    # Flag per il primo caricamento
        
        # 5. Avvia la costruzione dell'interfaccia
        self.setup_ui()

    # ----------------------------------------------------------------
    # COSTRUZIONE INTERFACCIA (Spostato da setup_collection_tab)
    # ----------------------------------------------------------------
    
    def setup_ui(self):
        """Configura l'interfaccia utente di questa scheda."""
        collection_layout = QVBoxLayout(self) # Imposta il layout principale del QWidget
        
        header_layout = QHBoxLayout()
        
        header_layout.addWidget(QLabel(t("collection_ui.account")))
        self.collection_account_combo = QComboBox()
        self.collection_account_combo.addItem(t("ui.all_accounts"))
        self.collection_account_combo.currentTextChanged.connect(self.refresh_collection_display)
        self.load_accounts_into_combo_box()      
        header_layout.addWidget(self.collection_account_combo)
        
        header_layout.addStretch()
        
        # Questi pulsanti chiamano funzioni sulla main_window
        cloudflare_btn = QPushButton(t("collection_ui.configure_cloudflare"))
        cloudflare_btn.clicked.connect(self.main_window.open_cloudflare_dialog)
        header_layout.addWidget(cloudflare_btn)

        self.tunnel_btn = QPushButton(t("collection_ui.expose_publicly"))
        self.tunnel_btn.clicked.connect(self.main_window.toggle_cloudflare_tunnel)
        header_layout.addWidget(self.tunnel_btn)
        
        self.web_viewer_btn = QPushButton(t("ui.open_web_viewer"))
        self.web_viewer_btn.clicked.connect(self.main_window.toggle_web_server)
        header_layout.addWidget(self.web_viewer_btn)
                
        refresh_collection_btn = QPushButton(t("collection_ui.refresh_collection"))
        refresh_collection_btn.clicked.connect(self.refresh_collection)
        header_layout.addWidget(refresh_collection_btn)
        
        collection_layout.addLayout(header_layout)
        
        # Filtri
        filters_layout = QHBoxLayout()
        filters_layout.addWidget(QLabel(t("collection_ui.search")))
        self.collection_search_input = QLineEdit()
        self.collection_search_input.setPlaceholderText(t("collection_ui.search_placeholder"))
        self.collection_search_input.textChanged.connect(self.apply_collection_filters)
        filters_layout.addWidget(self.collection_search_input)
        
        filters_layout.addWidget(QLabel(t("collection_ui.ownership_filter")))
        self.collection_ownership_filter = QComboBox()
        self.collection_ownership_filter.addItem(t("collection_ui.filter_all"), "all")
        self.collection_ownership_filter.addItem(t("collection_ui.filter_owned"), "owned")
        self.collection_ownership_filter.addItem(t("collection_ui.filter_missing"), "missing")
        self.collection_ownership_filter.currentIndexChanged.connect(self.apply_collection_filters)
        filters_layout.addWidget(self.collection_ownership_filter)
        
        self.collection_rarity_filter = QComboBox()
        self.collection_rarity_filter.addItem(t("collection_ui.filter_all"), "all")
        for rarity_name in RARITY_DATA.keys():
            self.collection_rarity_filter.addItem(rarity_name, rarity_name)
        self.collection_rarity_filter.currentIndexChanged.connect(self.apply_collection_filters)
        filters_layout.addWidget(self.collection_rarity_filter)
        
        filters_layout.addStretch()
        collection_layout.addLayout(filters_layout)
        
        # Stats bar
        self.collection_stats_label = QLabel(t("collection_ui.loading_collection"))
        self.collection_stats_label.setStyleSheet("QLabel { font-size: 11px; color: #888; padding: 5px; }")
        collection_layout.addWidget(self.collection_stats_label)
        
        # Scroll area
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        self.collection_container = QWidget()
        self.collection_container_layout = QVBoxLayout(self.collection_container)
        self.collection_container_layout.setSpacing(5)
        self.collection_container_layout.addStretch()
        
        scroll_area.setWidget(self.collection_container)
        collection_layout.addWidget(scroll_area)
        
        # Non c'è 'self.tabs.addTab' perché questo QWidget
        # verrà aggiunto ai tab da ui_main_window.py

    # ----------------------------------------------------------------
    # LOGICA DI CARICAMENTO COLLEZIONE (Spostata da MainWindow)
    # ----------------------------------------------------------------

    def _handle_card_click(self, event, clicked_widget, card_id: int):
        """
        Gestisce il click sulla carta. Ignora se il click è
        sul bottone wishlist (già gestito).
        """
        # Trova il bottone wishlist all'interno del widget
        wishlist_btn = clicked_widget.findChild(QPushButton) 
        
        if wishlist_btn:
            # Controlla se il click è avvenuto DENTRO i bordi del bottone
            if wishlist_btn.geometry().contains(event.pos()):
                # Se sì, è un click sulla wishlist, ignora l'apertura del dialog
                event.accept() # Segna l'evento come gestito
                return

        # ✅ CORREZIONE: Passa 'clicked_widget' alla funzione successiva
        self.open_card_details(event, card_id, clicked_widget)


    def open_card_details(self, event, card_id: int, clicked_widget):
        """
        Apre il dialog dei dettagli, ma ignora se il click
        è sul bottone wishlist.
        """
        # Ignora se non è il tasto sinistro
        if event.button() != Qt.LeftButton:
            return

        # Cerca il bottone wishlist
        wishlist_btn = clicked_widget.findChild(QPushButton)
        
        if wishlist_btn:
            # ================================================================
            # ✅ FIX: Mappa le coordinate del click
            # ================================================================
            
            # Mappa il punto del click (relativo a card_widget)
            # alle coordinate relative al bottone
            pos_in_button = wishlist_btn.mapFrom(clicked_widget, event.pos())
            
            # Controlla se il click è dentro il rettangolo del bottone
            if wishlist_btn.rect().contains(pos_in_button):
                # Se sì, il click era sul bottone. Ignora l'apertura del dialog
                # (il segnale clicked.connect del bottone farà il resto)
                return
            # ================================================================

        # Se non era sul bottone, apri il dialog
        try:
            dialog = CollectionCardDialog(card_id, self.db_manager, self)
            dialog.exec_()
        except Exception as e:
            # Aggiungiamo un log in caso di crash del dialog
            print(f"❌ Errore durante l'apertura del CollectionCardDialog: {e}")
            import traceback
            traceback.print_exc()

    def refresh_collection(self):
        """
        Avvia il caricamento/ricaricamento della collezione.
        """
        self.image_cache.clear()
        print(f"🗑️ Cache pulito")
        
        if not os.path.exists(DB_FILENAME):
            self.collection_stats_label.setText("⚠️ Database not found")
            return
        
        self.collection_account_combo.setEnabled(False)
        self.collection_stats_label.setText("⏳ Loading collection data...")
        QApplication.processEvents()

        try:
            # 1. Ricarica la lista degli account
            self.load_accounts_into_combo_box()
            
            # 2. Ricarica la cache della wishlist (DAL NUOVO MANAGER)
            self.wishlist_manager.load_wishlist()
            
            # 3. Avvia il refresh del display
            self.refresh_collection_display()
            
        except Exception as e:
            print(f"❌ Errore caricamento collezione: {e}")

    def refresh_collection_display(self):
        """Pulisce e ricarica i set in base all'account selezionato."""
        try:
            # Pulisci il container precedente
            while self.collection_container_layout.count() > 1:
                item = self.collection_container_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            
            # Pulisci la cache dei widget per i filtri
            self.collection_card_widgets = {}
            
            selected_account = self.collection_account_combo.currentText()
            is_all_accounts = selected_account == t("ui.all_accounts")
            
            print(f"\n📊 [COLLEZIONE] === Inizio caricamento ===")
            print(f"📊 [COLLEZIONE] Account: {selected_account}")

            # Eseguiamo tutto in un'unica connessione
            with sqlite3.connect(DB_FILENAME) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # ================================================================
                # PASSO 1: QUERY INVENTARIO
                # ================================================================
                print("Query 1: Caricamento inventario...")
                self.inventory_map = {} # Resetta la mappa dell'inventario
                
                if is_all_accounts:
                    cursor.execute("""
                        SELECT card_id, SUM(quantity) as total
                        FROM account_inventory
                        GROUP BY card_id
                    """)
                else:
                    cursor.execute("""
                        SELECT ai.card_id, SUM(ai.quantity) as total
                        FROM account_inventory ai
                        JOIN accounts a ON ai.account_id = a.account_id
                        WHERE a.account_name = ?
                        GROUP BY ai.card_id
                    """, (selected_account,))
                
                for row in cursor.fetchall():
                    self.inventory_map[row['card_id']] = row['total']
                print(f"Inventario caricato: {len(self.inventory_map)} carte uniche possedute.")

                # ================================================================
                # PASSO 2: QUERY STATISTICHE SET
                # ================================================================
                print("Query 2: Caricamento statistiche set...")
                stats_map = {}
                
                query_stats = """
                    SELECT c.set_code, COUNT(DISTINCT c.id) as owned, SUM(ai.quantity) as copies
                    FROM cards c
                    JOIN account_inventory ai ON c.id = ai.card_id
                """
                params_stats = []
                
                if not is_all_accounts:
                    query_stats += " JOIN accounts a ON ai.account_id = a.account_id WHERE a.account_name = ?"
                    params_stats.append(selected_account)
                
                query_stats += " GROUP BY c.set_code"
                
                cursor.execute(query_stats, params_stats)
                
                for row in cursor.fetchall():
                    stats_map[row['set_code']] = (row['owned'], int(row['copies']))
                print(f"Statistiche caricate per {len(stats_map)} set.")

                # ================================================================
                # PASSO 3: QUERY TUTTI I SET
                # ================================================================
                print("Query 3: Caricamento tutti i set...")
                cursor.execute("""
                    SELECT set_code, set_name, total_cards, cover_image_path 
                    FROM sets 
                    ORDER BY release_date DESC
                """)
                sets = cursor.fetchall()
                total_sets = len(sets)
                print(f"Trovati {total_sets} set.")
                
                # ================================================================
                # PASSO 4: COSTRUZIONE UI (SENZA QUERY)
                # ================================================================
                
                for i, set_row in enumerate(sets):
                    set_code = set_row['set_code']
                    set_name = set_row['set_name']
                    
                    self.collection_stats_label.setText(
                        f"⏳ Building UI {set_name}... ({i+1}/{total_sets})"
                    )
                    self.collection_stats_label.repaint()
                    QApplication.processEvents()
                    
                    total_cards = set_row['total_cards'] if set_row['total_cards'] else 0
                    cover_path = set_row['cover_image_path']
                    set_stats = stats_map.get(set_code, (0, 0)) # (owned, copies)
                    
                    set_section = self.create_set_section_fast(
                        set_code, set_name, total_cards, cover_path,
                        set_stats[0], # owned_count
                        set_stats[1], # total_copies
                        cursor # Passa il cursore per il lazy loading
                    )
                    
                    if set_section:
                        self.collection_container_layout.insertWidget(
                            self.collection_container_layout.count() - 1,
                            set_section
                        )
            
            # Finito!
            self.collection_stats_label.setText(
                f"✅ Collection loaded! {total_sets} sets"
            )
            self.collection_account_combo.setEnabled(True)
            print("✅ Completato!")
            
        except Exception as e:
            self.collection_stats_label.setText(f"❌ Error: {str(e)}")
            print(f"❌ Errore refresh_collection_display: {e}")
            import traceback
            traceback.print_exc()

    def create_set_section_fast(self, set_code, set_name, total_cards, cover_path, 
                                owned_count, total_copies, cursor):
        """
        Crea una sezione collapsible per un set.
        MODIFICATO (Pillar 3: N+1 Query):
        - Rimuove le query per le statistiche (calcolate in 'refresh_collection').
        - Accetta 'owned_count' e 'total_copies' come parametri.
        - Accetta 'global_inventory_map' e la passa a 'toggle_content'
          per il lazy loading.
        """
        try:
            # Frame principale
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            frame.setStyleSheet("QFrame { border: 1px solid #555; border-radius: 5px; margin: 2px; }")
            
            main_layout = QVBoxLayout(frame)
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.setSpacing(0)
            
            # =========================================================================
            # HEADER CLICCABILE CON COVER
            # =========================================================================
            
            header_widget = QWidget()
            header_layout = QHBoxLayout(header_widget)
            header_layout.setContentsMargins(5, 5, 5, 5)
            header_layout.setSpacing(10)
            
            # Arrow button (per expand/collapse)
            arrow_btn = QToolButton()
            arrow_btn.setCheckable(True)
            arrow_btn.setChecked(False)
            arrow_btn.setArrowType(Qt.RightArrow)
            arrow_btn.setFixedSize(20, 20)
            arrow_btn.setStyleSheet("""
                QToolButton {
                    border: none;
                    background: transparent;
                }
            """)
            header_layout.addWidget(arrow_btn)
            
            # Cover image (se esiste)
            # MODIFICATO (Pillar 2): Usa il loader asincrono
            cover_label = QLabel()
            cover_label.setFixedSize(60, 40)
            cover_label.setStyleSheet("""
                QLabel {
                    border: 1px solid #555;
                    border-radius: 3px;
                    background-color: #2a2a2a;
                }
            """)
            # 'cover_path' ora è un URL dalla tua API
            self.load_card_image_async(cover_path, cover_label, 60, 40)
            header_layout.addWidget(cover_label)

            # =========================================================================
            # ❌ RIMOSSO (Pillar 3): Blocco "Calcola statistiche del set"
            # (I dati 'owned_count' e 'total_copies' ora sono passati come argomenti)
            # =========================================================================
            
            completion = (owned_count / total_cards * 100) if total_cards and total_cards > 0 else 0
            
            # Text label con nome e stats (usa i dati passati)
            text_label = QLabel(
                f"<b>{set_name}</b> ({set_code}) - {owned_count}/{total_cards} "
                f"({completion:.0f}%) • {total_copies} copies"
            )
            text_label.setStyleSheet("""
                QLabel {
                    color: white;
                    font-size: 14px;
                    padding: 5px;
                }
            """)
            header_layout.addWidget(text_label)
            header_layout.addStretch()
            
            # Rendi tutto il widget cliccabile
            header_widget.setStyleSheet("""
                QWidget {
                    background-color: #353535;
                    border: 1px solid #555;
                    border-radius: 5px;
                }
                QWidget:hover {
                    background-color: #454545;
                }
            """)
            
            main_layout.addWidget(header_widget)
            
            # =========================================================================
            # CONTENT WIDGET (nascosto inizialmente)
            # =========================================================================
            
            content_widget = QWidget()
            content_widget.setVisible(False)
            content_widget.cards_loaded = False  # Flag per lazy loading
            content_layout = QVBoxLayout(content_widget)
            content_layout.setContentsMargins(10, 10, 10, 10)
            content_layout.setSpacing(0)
            
            main_layout.addWidget(content_widget)
            main_layout.addStretch()
            
            # =========================================================================
            # TOGGLE FUNCTION CON LAZY LOADING
            # =========================================================================
            
            def toggle_content():
                """Toggle espansione/collasso con lazy loading."""
                try:
                    is_expanded = arrow_btn.isChecked()
                    content_widget.setVisible(is_expanded)
                    arrow_btn.setArrowType(Qt.DownArrow if is_expanded else Qt.RightArrow)
                    
                    # ✅ Fa espandere il widget dinamicamente
                    if is_expanded:
                        content_widget.setSizePolicy(
                            QSizePolicy.Expanding,
                            QSizePolicy.Expanding
                        )
                        content_widget.setMinimumHeight(400)
                    else:
                        content_widget.setMinimumHeight(0)
                        content_widget.setSizePolicy(
                            QSizePolicy.Expanding,
                            QSizePolicy.Minimum
                        )
                    
                    # Lazy load delle carte solo la prima volta
                    if is_expanded and not content_widget.cards_loaded:
                        try:
                            with sqlite3.connect(DB_FILENAME) as conn:
                                conn.row_factory = sqlite3.Row
                                cursor_refresh = conn.cursor()
                                
                                # ✅ MODIFICATO (Pillar 3):
                                # Passa 'global_inventory_map' invece di 'inventory'
                                self.load_set_cards_lazy(
                                    set_code, set_name, content_widget, 
                                    cursor_refresh
                                )
                            content_widget.cards_loaded = True
                        except Exception as e:
                            print(f"❌ Error loading cards for {set_code}: {e}")
                            import traceback
                            traceback.print_exc()
                            
                            error_label = QLabel(f"❌ Error loading cards: {str(e)}")
                            error_label.setStyleSheet("QLabel { color: #e74c3c; padding: 10px; }")
                            content_widget.layout().addWidget(error_label)
                            
                except Exception as e:
                    print(f"❌ Error in toggle_content: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Collega il signal toggled
            arrow_btn.toggled.connect(toggle_content)
            
            # Gestisci il click sul header widget
            def header_clicked(event):
                """Gestisce il click sul header per espandere/collassare."""
                try:
                    if event.button() == Qt.LeftButton:
                        arrow_btn.setChecked(not arrow_btn.isChecked())
                except Exception as e:
                    print(f"❌ Error in header_clicked: {e}")
            
            header_widget.mousePressEvent = header_clicked
            
            return frame
            
        except Exception as e:
            print(f"❌ Error creating set section for {set_code}: {e}")
            import traceback
            traceback.print_exc()
            
            # Ritorna frame di errore
            error_frame = QFrame()
            error_layout = QVBoxLayout(error_frame)
            error_label = QLabel(f"❌ Error loading set {set_code}: {str(e)}")
            error_label.setStyleSheet("QLabel { color: #e74c3c; padding: 10px; font-size: 12px; }")
            error_layout.addWidget(error_label)
            return error_frame
        
    def load_set_cards_lazy(self, set_code, set_name, cards_container, cursor):
        """Carica le carte per un set specifico (chiamato on-demand)."""
        try:
            print(f"📂 Iniziando lazy load per {set_code}...")
            
            cursor.execute("""
                SELECT id, card_name, rarity, thumbnail_blob, card_number 
                FROM cards 
                WHERE set_code = ?
                ORDER BY CAST(card_number AS INTEGER)
            """, (set_code,))
            cards = cursor.fetchall()
            
            total_cards = len(cards)
            print(f"📊 Trovate {total_cards} carte in {set_code}")

            # Layout e ScrollArea
            existing_layout = cards_container.layout()
            if existing_layout is None: # Fallback di sicurezza
                existing_layout = QVBoxLayout(cards_container)
                cards_container.setLayout(existing_layout)
            
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { border: none; }")
            
            grid_widget = QWidget()
            grid_layout = QGridLayout(grid_widget)
            grid_layout.setSpacing(10)
            
            # Salva la griglia per i filtri
            self.collection_card_widgets[set_code] = {
                'widgets': [],
                'layout': grid_layout
            }
            
            # Carica in batch
            batch_size = 50
            card_index = 0
            
            for batch_num in range(0, total_cards, batch_size):
                batch_cards = cards[batch_num:batch_num + batch_size]
                
                for idx, card_row in enumerate(batch_cards):
                    card_id = card_row[0]
                    card_name = card_row[1]
                    rarity = card_row[2]
                    image_blob = card_row[3]
                    card_number = card_row[4] 
                    
                    # Usa la mappa dell'inventario (già caricata)
                    quantity = self.inventory_map.get(card_id, 0)
                    
                    card_widget = self.create_card_widget(
                        card_id, card_name, rarity, card_number, image_blob, quantity
                    )
                    
                    # Salva il widget e i metadati per il filtro
                    self.collection_card_widgets[set_code]['widgets'].append((
                        card_widget, card_name, rarity, quantity, card_number
                    ))
                    
                    row = card_index // 5
                    col = card_index % 5
                    card_index += 1
                    grid_layout.addWidget(card_widget, row, col)
                
                QApplication.processEvents()
                progress = int((batch_num + len(batch_cards)) / total_cards * 100)
                self.collection_stats_label.setText(
                    f"⏳ Loading {set_name}... {progress}%"
                )
                self.collection_stats_label.repaint()
            
            scroll.setWidget(grid_widget)
            existing_layout.addWidget(scroll)
            
            print(f"✅ {set_code} completato")
            self.collection_stats_label.setText(f"✅ Ready!")
            
        except Exception as e:
            print(f"❌ Errore lazy load {set_code}: {e}")
            import traceback
            traceback.print_exc()

    def create_card_widget(self, card_id: int, card_name, rarity, card_number, image_blob, total_quantity: int) -> QWidget:
        """Crea il widget per una singola carta."""
        
        is_wished = self.wishlist_manager.is_wished(card_id)
        
        card_widget = QFrame()
        card_widget.setFixedSize(120, 210) 
        # ✅ FIX 1: Rimuovi qualsiasi stile predefinito di bordo/sfondo dal QFrame principale.
        # Se c'era uno stylesheet qui prima, dovrebbe essere rimosso.
        # Ad esempio: card_widget.setStyleSheet("border: 1px solid gray; border-radius: 5px; background-color: #333;")
        # Lasciare vuoto o impostare uno stile minimalista per il background se necessario.
        card_widget.setStyleSheet("background-color: #333;") # Un colore di sfondo base se non ne vuoi uno trasparente

        main_v_layout = QVBoxLayout(card_widget)
        main_v_layout.setContentsMargins(0, 0, 0, 0)
        main_v_layout.setSpacing(2) 

        image_container = QWidget()
        image_container.setFixedSize(120, 160) 
        card_layout = QGridLayout(image_container)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        
        image_label = QLabel()
        image_label.setScaledContents(True)
        pixmap = QPixmap()
        if image_blob:
            pixmap.loadFromData(image_blob)
            if not pixmap.isNull(): 
                pixmap = pixmap.scaled(120, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.image_cache.put(card_id, pixmap)
                image_label.setPixmap(pixmap)
            else:
                image_label.setPixmap(self.placeholder_pixmap.scaled(120, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            image_label.setPixmap(self.placeholder_pixmap.scaled(120, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        card_layout.addWidget(image_label, 0, 0) 
        
        quantity_label = QLabel(f"x{total_quantity}")
        quantity_label.setAlignment(Qt.AlignCenter)
        quantity_label.setStyleSheet("""
            background-color: #000000; color: white; padding: 2px 5px;
            margin: 5px; border-radius: 5px; font-weight: bold;
        """)
        card_layout.addWidget(quantity_label, 0, 0, Qt.AlignRight | Qt.AlignBottom)

        wishlist_btn = QPushButton()
        wishlist_btn.setFixedSize(28, 28) 
        self.update_wishlist_button_style(wishlist_btn, is_wished)
        wishlist_btn.clicked.connect(lambda: self._handle_wishlist_toggle(card_id, wishlist_btn))
        card_layout.addWidget(wishlist_btn, 0, 0, Qt.AlignLeft | Qt.AlignTop)
        
        if total_quantity == 0:
            opacity_effect = QGraphicsOpacityEffect(image_container)
            opacity_effect.setOpacity(0.4) 
            image_container.setGraphicsEffect(opacity_effect)
            quantity_label.hide()
        
        main_v_layout.addWidget(image_container)

        # ✅ FIX 2: Rimuovi stili di bordo/sfondo dai QLabel di nome e rarità
        
        # Nome
        name_label = QLabel(f"#{card_number} {card_name}")
        name_label.setStyleSheet("font-size: 9px; font-weight: bold; color: white; background-color: transparent;") # Rimuovi background-color
        name_label.setWordWrap(True)
        name_label.setAlignment(Qt.AlignCenter)
        main_v_layout.addWidget(name_label)
        
        # Linea Separatore
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine) 
        separator.setFrameShadow(QFrame.Sunken)
        separator.setStyleSheet("background-color: #555; max-height: 1px; margin-top: 2px; margin-bottom: 2px;")
        main_v_layout.addWidget(separator)
        
        # Icona Rarità
        rarity_icon_label = QLabel()
        rarity_icon_label.setStyleSheet("color: white; background-color: transparent;") # Rimuovi background-color
        
        if rarity in RARITY_DATA:
            icon_full_path = get_resource_path(RARITY_DATA[rarity])
            if os.path.exists(icon_full_path):
                rarity_pixmap = QPixmap(icon_full_path)
                scaled_pixmap = rarity_pixmap.scaledToHeight(20, Qt.SmoothTransformation)
                rarity_icon_label.setFixedHeight(20)                    
                rarity_icon_label.setPixmap(scaled_pixmap)
                rarity_icon_label.setAlignment(Qt.AlignCenter)
                rarity_icon_label.setToolTip(rarity)
            else:
                rarity_icon_label.setText(rarity) 
        else:
            rarity_icon_label.setText(rarity) 

        main_v_layout.addWidget(rarity_icon_label) 
        
        main_v_layout.addStretch(1) 
        
        card_widget.mousePressEvent = lambda event, c_id=card_id, widget=card_widget: self._handle_card_click(event, widget, c_id)
        card_widget.setCursor(Qt.PointingHandCursor)
        
        return card_widget
    # ----------------------------------------------------------------
    # LOGICA WISHLIST (Spostata da MainWindow)
    # ----------------------------------------------------------------

    def _handle_wishlist_toggle(self, card_id: int, wishlist_btn: QPushButton):
        """
        Handler UI per il click sulla wishlist.
        Chiama il manager e poi aggiorna lo stile.
        """
        # 1. Chiama il manager per fare la modifica
        new_state = self.wishlist_manager.toggle_wishlist(card_id)
        
        # 2. Aggiorna lo stile del bottone
        self.update_wishlist_button_style(wishlist_btn, new_state)

    def update_wishlist_button_style(self, wishlist_btn: QPushButton, is_wishlisted: bool):
        """Aggiorna lo stile del pulsante wishlist."""
        if is_wishlisted:
            wishlist_btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(231, 76, 60, 200);
                    border: 2px solid #c0392b;
                    border-radius: 14px;
                    font-size: 16px;
                }
                QPushButton:hover {
                    background-color: rgba(231, 76, 60, 255);
                }
            """)
            wishlist_btn.setText("❤️")
        else:
            wishlist_btn.setStyleSheet("""
                QPushButton {
                    background-color: rgba(0, 0, 0, 150);
                    border: 2px solid #555;
                    border-radius: 14px;
                    font-size: 16px;
                }
                QPushButton:hover {
                    background-color: rgba(231, 76, 60, 150);
                }
            """)
            wishlist_btn.setText("🤍")

    # ----------------------------------------------------------------
    # ALTRE FUNZIONI HELPER (Spostate da MainWindow)
    # ----------------------------------------------------------------

    def load_accounts_into_combo_box(self):
        """Carica gli account nel combobox."""
        try:
            self.collection_account_combo.blockSignals(True)
            current_text = self.collection_account_combo.currentText() # Salva selezione
            
            self.collection_account_combo.clear()
            self.collection_account_combo.addItem(t("ui.all_accounts"))
            
            db_path = get_app_data_path(DB_FILENAME)
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT account_id, account_name FROM accounts ORDER BY account_name")
                accounts = cursor.fetchall()
                
                for account in accounts:
                    self.collection_account_combo.addItem(account["account_name"], account["account_id"])
            
            # Ripristina la selezione precedente, se esiste ancora
            index = self.collection_account_combo.findText(current_text)
            if index != -1:
                self.collection_account_combo.setCurrentIndex(index)
            
            print(f"✅ Caricati {len(accounts)} account nel combo")
            self.collection_account_combo.blockSignals(False)
            
        except Exception as e:
            print(f"❌ Errore caricamento account: {e}")

    def apply_collection_filters(self):
        """Applica i filtri di ricerca/possesso/rarità."""
        search_text = self.collection_search_input.text().lower()
        ownership_filter = self.collection_ownership_filter.currentData()
        rarity_filter = self.collection_rarity_filter.currentData()
        
        for set_code, set_data in self.collection_card_widgets.items():
            widgets = set_data.get('widgets', [])
            
            for widget, card_name, rarity, quantity, card_number in widgets:
                show = True
                
                # Filtro ricerca
                if search_text and search_text not in card_name.lower():
                    show = False
                
                # Filtro possesso
                if show and ownership_filter != "all":
                    if ownership_filter == "owned" and quantity == 0:
                        show = False
                    elif ownership_filter == "missing" and quantity > 0:
                        show = False
                
                # Filtro rarità
                if show and rarity_filter != "all":
                    if rarity != rarity_filter:
                        show = False
                
                widget.setVisible(show)

    def load_card_image_async(self, image_url: str, target_label: QLabel, scale_w: int, scale_h: int):
        """Carica un'immagine asincrona."""
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
        """Slot per immagine caricata."""
        try:
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            if pixmap.isNull():
                raise Exception("Impossibile caricare QPixmap")

            scaled_pixmap = pixmap.scaled(target_label.width(), target_label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_cache.put(image_url, scaled_pixmap)
            
            if target_label and target_label.isVisible():
                target_label.setPixmap(scaled_pixmap)
                
        except Exception as e:
            print(f"❌ Errore on_image_loaded: {e}")

    @pyqtSlot(str, str, QLabel)
    def on_image_load_error(self, error_msg: str, image_url: str, target_label: QLabel):
        """Slot per errore caricamento."""
        pass # Lascia il segnaposto