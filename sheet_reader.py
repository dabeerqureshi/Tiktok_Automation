"""
Google Sheets Reader — Row-indexed riddle metadata lookup.

Reads the Google Sheet that contains riddle prompts, answers, and explanations.
Row N (1-based, skipping header) corresponds to vid_N.

The reader caches the full sheet in memory for SHEET_CACHE_TTL seconds
to avoid rate-limiting the Sheets API during high-frequency polls.

Sheet columns (0-based index):
    Col A (0): Full AI generation prompt   ← informational
    Col B (1): Riddle Answer               ← SHEET_COL_ANSWER
    Col C (2): Riddle Explanation          ← SHEET_COL_EXPLANATION
"""
import logging
import time
from typing import Dict, List, Optional

import config

logger = logging.getLogger("TikTokDaemon")


class SheetReader:
    """
    Lazy-initialised Google Sheets reader with in-memory TTL caching.
    Requires a `google.oauth2` Credentials object (passed from GDriveService).
    """

    def __init__(self, credentials):
        self._creds = credentials
        self._service = None
        self._resolved_tab_title: Optional[str] = None
        self._cache: List[List[str]] = []
        self._cache_ts: float = 0.0

    # -----------------------------------------------------------------------
    # PUBLIC API
    # -----------------------------------------------------------------------

    def get_row(self, row_index: int) -> Optional[Dict[str, str]]:
        """
        Returns metadata for the given 1-based row index (vid_N → row N).
        The sheet is expected to have a header row at row 1, so vid_1 data
        is at sheet row 2 (index 0 inside `_cache`).

        Returns dict:
            { "answer": "...", "explanation": "..." }
        or None if the row doesn't exist.
        """
        if not config.SHEET_ID:
            logger.error(
                "SHEET_ID is not set in config / .env! "
                "Add SHEET_ID=<your-sheet-id> to your .env file."
            )
            return None

        rows = self._get_cached_rows()
        if not rows:
            return None

        # vid_N → data row N (0-indexed inside _cache, which skips header)
        data_idx = row_index - 1        # row_index 1 → _cache[0]
        if data_idx < 0 or data_idx >= len(rows):
            logger.warning(
                f"Sheet row {row_index} not found "
                f"(sheet has {len(rows)} data rows)."
            )
            return None

        row = rows[data_idx]

        def _cell(col: int) -> str:
            return row[col].strip() if col < len(row) else ""

        answer = _cell(config.SHEET_COL_ANSWER)
        explanation = _cell(config.SHEET_COL_EXPLANATION)

        return {
            "answer": answer,
            "explanation": explanation,
        }

    def invalidate_cache(self):
        """Force a fresh fetch on the next `get_row` call."""
        self._cache_ts = 0.0

    # -----------------------------------------------------------------------
    # CACHING
    # -----------------------------------------------------------------------

    def _get_cached_rows(self) -> List[List[str]]:
        """Returns cached rows, refreshing if the TTL has expired."""
        now = time.monotonic()
        if self._cache and (now - self._cache_ts) < config.SHEET_CACHE_TTL:
            return self._cache

        rows = self._fetch_sheet()
        if rows is not None:
            self._cache = rows
            self._cache_ts = now
        return self._cache

    # -----------------------------------------------------------------------
    # SHEETS API & TAB RESOLUTION
    # -----------------------------------------------------------------------

    def _get_service(self):
        """Lazily builds the Sheets v4 service."""
        if self._service is None:
            from googleapiclient.discovery import build
            self._service = build("sheets", "v4", credentials=self._creds)
        return self._service

    def _resolve_tab_name(self) -> str:
        """
        Determines the target tab title from metadata:
        1. If SHEET_NAME is configured and found, use it.
        2. If SHEET_GID is configured, find the sheet with that sheetId.
        3. Default to the first sheet in the spreadsheet.
        """
        if self._resolved_tab_title:
            return self._resolved_tab_title

        service = self._get_service()
        try:
            meta = service.spreadsheets().get(spreadsheetId=config.SHEET_ID).execute()
            sheets = meta.get("sheets", [])

            if not sheets:
                self._resolved_tab_title = config.SHEET_NAME or "Sheet1"
                return self._resolved_tab_title

            # Match by GID if specified
            if config.SHEET_GID:
                target_gid = str(config.SHEET_GID)
                for s in sheets:
                    props = s.get("properties", {})
                    if str(props.get("sheetId")) == target_gid:
                        self._resolved_tab_title = props.get("title", "Sheet1")
                        logger.info(f"Resolved Google Sheet tab by gid={target_gid}: '{self._resolved_tab_title}'")
                        return self._resolved_tab_title

            # Match by Name if specified
            if config.SHEET_NAME:
                for s in sheets:
                    props = s.get("properties", {})
                    if props.get("title", "").strip().lower() == config.SHEET_NAME.strip().lower():
                        self._resolved_tab_title = props.get("title")
                        return self._resolved_tab_title

            # Default to first sheet tab
            first_title = sheets[0].get("properties", {}).get("title", "Sheet1")
            self._resolved_tab_title = first_title
            logger.info(f"Defaulting to first Google Sheet tab: '{self._resolved_tab_title}'")
            return self._resolved_tab_title

        except Exception as e:
            logger.warning(f"Could not fetch spreadsheet metadata to resolve tab: {e}")
            self._resolved_tab_title = config.SHEET_NAME or "Sheet1"
            return self._resolved_tab_title

    def _fetch_sheet(self) -> Optional[List[List[str]]]:
        """
        Fetches all rows from the resolved sheet tab.
        Returns a list of rows (each row is a list of cell strings).
        Row 0 = header (skipped when indexing by vid_N).
        """
        try:
            service = self._get_service()
            tab_name = self._resolve_tab_name()
            # Wrap in single quotes to handle spaces/symbols in tab title
            safe_tab = tab_name.replace("'", "\\'")
            range_name = f"'{safe_tab}'!A:Z"

            result = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=config.SHEET_ID, range=range_name)
                .execute()
            )
            values: List[List[str]] = result.get("values", [])

            if not values:
                logger.warning(f"Google Sheet '{config.SHEET_ID}' tab '{tab_name}' is empty.")
                return []

            # Row 0 = header; data starts at row 1
            data_rows = values[1:]
            logger.info(
                f"Sheet cache refreshed: {len(data_rows)} data rows loaded from '{tab_name}'."
            )
            return data_rows

        except Exception as e:
            logger.error(f"Failed to fetch Google Sheet: {e}", exc_info=True)
            return None
