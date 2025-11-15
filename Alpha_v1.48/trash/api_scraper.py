"""
api_scraper_fixed.py - API Scraper CORRETTO per il tuo API

QUESTO FILE RISOLVE IL PROBLEMA:
❌ Error fetching sets: 'list' object has no attribute 'get'

IL PROBLEMA:
Il tuo API ritorna una LIST diretta, non un DICT con chiave 'sets'

LA SOLUZIONE:
Questo file gestisce ENTRAMBI i formati (lista e dict)
"""

import aiohttp
import asyncio
import json
import os
import time
from typing import List, Dict, Optional
from threading import Lock

# Import your modules
from database_optimized import DatabaseManager
from thumbnail_generator import ThumbnailGenerator


class TCGPocketAPIScraper:
    """
    High-performance API-based scraper - FIXED VERSION
    
    Handles both API response formats:
    - Direct list: [{"code": "A1", ...}, {"code": "A2", ...}]
    - Dict with key: {"sets": [{"code": "A1", ...}]}
    """
    
    def __init__(
        self, 
        api_base_url="http://www.pkmn-pocket-api.it",
        log_callback=None,
        progress_callback=None
    ):
        """Initialize API scraper"""
        self.api_base_url = api_base_url
        self.log_callback = log_callback or print
        self.progress_callback = progress_callback
        
        # Initialize services
        self.db = DatabaseManager(log_callback=log_callback)
        self.thumb_gen = ThumbnailGenerator(thumbnail_size=(150, 150), quality=85)
        self.db_lock = Lock()
        
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def setup(self):
        """Initialize async resources"""
        if self.db.connect():
            self.db.setup_database()
            self.db.validate_and_repair_database()
        
        if not self.session:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=100, limit_per_host=50),
                timeout=aiohttp.ClientTimeout(total=60)
            )
            self.log_callback("✅ HTTP session created")
    
    async def close(self):
        """Cleanup resources"""
        if self.session:
            await self.session.close()
            self.log_callback("✅ HTTP session closed")
        if self.db:
            self.db.close()
    
    async def scrape_all_sets_parallel(self):
        """
        Main entry point: Scrapes ALL sets in parallel.
        """
        await self.setup()
        
        try:
            start_time = time.time()
            
            # Step 1: Fetch sets
            self.log_callback("📋 Fetching sets from API...")
            sets = await self._fetch_sets()
            
            if not sets:
                self.log_callback("❌ No sets found!")
                return
            
            self.log_callback(f"✅ Found {len(sets)} sets")
            total_cards = 0
            
            # Step 2: Process sets in parallel (3 at a time)
            semaphore = asyncio.Semaphore(3)
            
            async def process_set_wrapper(set_data):
                async with semaphore:
                    return await self._process_set_api(set_data)
            
            tasks = [process_set_wrapper(s) for s in sets]
            
            # Execute with progress tracking
            completed = 0
            for coro in asyncio.as_completed(tasks):
                result = await coro
                completed += 1
                
                if isinstance(result, int):
                    total_cards += result
                elif isinstance(result, Exception):
                    self.log_callback(f"⚠️ Set error: {result}")
                
                # Update progress
                if self.progress_callback:
                    self.progress_callback(completed, len(sets))
            
            elapsed = time.time() - start_time
            self.log_callback(
                f"✅ Scraping complete: {total_cards} cards in {elapsed:.1f}s "
                f"({total_cards/elapsed:.1f} cards/sec)"
            )
            
        finally:
            await self.close()
    
    async def _fetch_sets(self) -> List[Dict]:
        """
        Fetch all sets from API.
        
        ✅ FIXED: Handles both response formats:
        - Direct list: [{"code": "A1"}, {"code": "A2"}]
        - Dict with 'sets' key: {"sets": [{"code": "A1"}]}
        """
        try:
            url = f"{self.api_base_url}/api/sets"
            self.log_callback(f"🔗 API endpoint: {url}")
            
            async with self.session.get(url) as resp:
                self.log_callback(f"📊 Response status: {resp.status}")
                
                if resp.status == 200:
                    data = await resp.json()
                    
                    # ✅ FIX 1: Check if response is a direct list
                    if isinstance(data, list):
                        self.log_callback(f"✅ API returned direct list: {len(data)} sets")
                        return data
                    
                    # ✅ FIX 2: Check if response is dict with 'sets' key
                    elif isinstance(data, dict):
                        if 'sets' in data:
                            sets_list = data.get('sets', [])
                            self.log_callback(f"✅ API returned dict with 'sets' key: {len(sets_list)} sets")
                            return sets_list
                        else:
                            self.log_callback(f"⚠️ Dict received but no 'sets' key. Keys: {data.keys()}")
                            # Try to treat as single set or return empty
                            return []
                    
                    else:
                        self.log_callback(f"❌ Unexpected data type: {type(data)}")
                        return []
                
                else:
                    self.log_callback(f"❌ API error: HTTP {resp.status}")
                    text = await resp.text()
                    self.log_callback(f"Response: {text[:200]}")
                    return []
        
        except Exception as e:
            self.log_callback(f"❌ Error fetching sets: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def _process_set_api(self, set_data: Dict) -> int:
        """
        Process ONE set from API.
        
        Args:
            set_data: Single set object from API
        
        Returns:
            Number of cards processed
        """
        # ✅ Extract set code - handle different formats
        if isinstance(set_data, str):
            # If set_data is just a string code
            set_code = set_data
            set_name = set_data
        else:
            # Try different possible field names
            set_code = (
                set_data.get('code') or 
                set_data.get('set_code') or 
                set_data.get('id') or 
                set_data.get('code')
            )
            set_name = (
                set_data.get('name') or 
                set_data.get('set_name') or 
                set_code
            )
        
        if not set_code:
            self.log_callback(f"⚠️ Cannot extract set code from: {set_data}")
            return 0
        
        try:
            self.log_callback(f"⏳ Processing set {set_code}...")
            
            # Fetch cards for this set
            url = f"{self.api_base_url}/api/cards/{set_code}"
            
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    self.log_callback(f"❌ API error for {set_code}: HTTP {resp.status}")
                    return 0
                
                data = await resp.json()
                
                # ✅ FIX: Handle both list and dict responses
                if isinstance(data, list):
                    # Direct list of cards
                    cards = data
                elif isinstance(data, dict) and 'cards' in data:
                    # Dict with 'cards' key
                    cards = data.get('cards', [])
                else:
                    # Unknown format
                    self.log_callback(f"⚠️ Unexpected card format for {set_code}: {type(data)}")
                    cards = []
            
            if not cards:
                self.log_callback(f"⚠️ No cards found for {set_code}")
                return 0
            
            self.log_callback(f"📥 Set {set_code}: {len(cards)} cards from API")
            
            # Step 2: Download images & generate thumbnails in parallel
            semaphore = asyncio.Semaphore(50)  # 50 concurrent
            
            async def process_card_wrapper(card):
                async with semaphore:
                    return await self._process_card_api(card)
            
            tasks = [process_card_wrapper(c) for c in cards]
            processed_cards = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Filter valid cards
            valid_cards = [c for c in processed_cards if isinstance(c, dict)]
            
            # Step 3: Save to database
            if valid_cards:
                # Save set metadata
                await self._save_set_to_db(set_data, len(valid_cards))
                
                # Batch insert all cards
                self.db.save_cards_batch(valid_cards, batch_size=5000)
            
            self.log_callback(f"✅ Set {set_code}: {len(valid_cards)} cards saved")
            
            return len(valid_cards)
            
        except Exception as e:
            self.log_callback(f"❌ Error processing set {set_code}: {e}")
            import traceback
            traceback.print_exc()
            return 0
    
    async def _process_card_api(self, card_data: Dict) -> Optional[Dict]:
        """
        Process ONE card from API.
        
        Downloads image, generates thumbnail IN-MEMORY (no disk I/O!)
        """
        try:
            image_url = card_data.get('image_url')
            
            # Try to download and generate thumbnail
            if image_url:
                try:
                    async with self.session.get(
                        image_url, 
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            image_bytes = await resp.read()
                            
                            # ✅ Generate thumbnail IN-MEMORY
                            thumbnail_bytes = self.thumb_gen.generate_thumbnail_from_bytes(
                                image_bytes
                            )
                            
                            if thumbnail_bytes:
                                card_data['thumbnail_blob'] = thumbnail_bytes
                            else:
                                card_data['thumbnail_blob'] = None
                        else:
                            card_data['thumbnail_blob'] = None
                except Exception as img_err:
                    # Image download failed, continue without thumbnail
                    card_data['thumbnail_blob'] = None
            else:
                card_data['thumbnail_blob'] = None
            
            # Map API fields to database fields
            return {
                'set_code': card_data.get('set_code'),
                'card_number': card_data.get('card_number'),
                'card_name': card_data.get('card_name', ''),
                'rarity': card_data.get('rarity', ''),
                'image_url': card_data.get('image_url'),
                'local_image_path': None,  # Not saving to disk
                'card_url': card_data.get('card_url'),
                'color_hash': card_data.get('color_hash'),
                'thumbnail_blob': card_data.get('thumbnail_blob'),
            }
            
        except Exception as e:
            self.log_callback(f"⚠️ Card processing error: {e}")
            return card_data
    
    async def _save_set_to_db(self, set_data: Dict, total_cards: int):
        """Save set metadata to database"""
        try:
            with self.db_lock:
                # Extract set code
                set_code = (
                    set_data.get('code') or 
                    set_data.get('set_code') or 
                    set_data.get('id')
                )
                
                set_name = (
                    set_data.get('name') or 
                    set_data.get('set_name') or 
                    set_code
                )
                
                self.db.cursor.execute(
                    """INSERT OR REPLACE INTO sets 
                       (set_code, set_name, url, release_date, total_cards, cover_image_path)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        set_code,
                        set_name,
                        set_data.get('url', ''),
                        set_data.get('release_date', ''),
                        total_cards,
                        set_data.get('cover_image_path'),
                    )
                )
                self.db.conn.commit()
        except Exception as e:
            self.log_callback(f"⚠️ Set save error: {e}")


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

async def main():
    """
    Example usage of API scraper.
    
    Run with: python api_scraper_fixed.py
    """
    scraper = TCGPocketAPIScraper(
        api_base_url="http://www.pkmn-pocket-api.it",
        log_callback=print,
        progress_callback=lambda current, total: print(f"Progress: {current}/{total} sets")
    )
    
    await scraper.scrape_all_sets_parallel()


if __name__ == "__main__":
    asyncio.run(main())
