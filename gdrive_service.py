"""
Google Drive API Service Layer.
Handles cloud authentication, folder discovery/creation, chunked downloads,
and resumable video uploads.

Updated for the 4-segment riddle pipeline:
- Only manages 'AI Generated Videos' + 'Final Videos' folders
- Exposes credentials so SheetReader can share them (avoids double auth)
"""
import io
import logging
from pathlib import Path
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger("TikTokDaemon")


class GDriveService:
    def __init__(self):
        self.creds = None
        self.service = None
        self.authenticate()

    # -----------------------------------------------------------------------
    # AUTHENTICATION
    # -----------------------------------------------------------------------

    def authenticate(self):
        """
        Authenticates with Google Drive + Sheets using either:
        1. Service Account (service_account.json)
        2. OAuth 2.0 User Login (credentials.json + token.json)

        Credentials are stored on self.creds so SheetReader can reuse them.
        """
        # 1. Service Account
        if config.SERVICE_ACCOUNT_FILE.exists():
            logger.info(f"Authenticating via Service Account: {config.SERVICE_ACCOUNT_FILE}")
            self.creds = service_account.Credentials.from_service_account_file(
                str(config.SERVICE_ACCOUNT_FILE),
                scopes=config.SCOPES
            )
            self.service = build("drive", "v3", credentials=self.creds)
            return

        # 2. OAuth 2.0 — load existing token
        if config.TOKEN_FILE.exists():
            try:
                self.creds = Credentials.from_authorized_user_file(
                    str(config.TOKEN_FILE), config.SCOPES
                )
            except Exception as e:
                logger.warning(f"Failed to read existing token.json: {e}")
                self.creds = None

        # Refresh or initiate new flow
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                logger.info("Refreshing expired Google Drive OAuth token...")
                self.creds.refresh(Request())
            else:
                if not config.CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        f"No Google Drive credentials found!\n"
                        f"Place '{config.CREDENTIALS_FILE.name}' (OAuth Client ID) or "
                        f"'{config.SERVICE_ACCOUNT_FILE.name}' (Service Account) "
                        f"in the project directory.\n"
                        f"Refer to README.md for step-by-step instructions."
                    )
                logger.info("Launching Google OAuth2 authorization in browser...")
                logger.info(
                    "NOTE: The consent screen will request Drive + Sheets access "
                    "(needed to read your riddle spreadsheet)."
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(config.CREDENTIALS_FILE),
                    config.SCOPES
                )
                self.creds = flow.run_local_server(port=0)

            # Persist token for 24/7 background reuse
            with open(config.TOKEN_FILE, "w") as token_file:
                token_file.write(self.creds.to_json())
            logger.info("Saved Google Drive/Sheets authorization token to token.json")

        self.service = build("drive", "v3", credentials=self.creds)

    # -----------------------------------------------------------------------
    # FOLDER MANAGEMENT
    # -----------------------------------------------------------------------

    def find_folder(self, folder_name: str, parent_id: Optional[str] = None) -> Optional[str]:
        """Searches for an untrashed folder by name (case-insensitive)."""
        query_parts = [
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false"
        ]
        if parent_id:
            query_parts.append(f"'{parent_id}' in parents")

        results = self.service.files().list(
            q=" and ".join(query_parts),
            fields="files(id, name)",
            spaces="drive"
        ).execute()

        for f in results.get("files", []):
            if f["name"].strip().lower() == folder_name.strip().lower():
                return f["id"]
        return None

    def create_folder(self, folder_name: str, parent_id: Optional[str] = None) -> str:
        """Creates a folder in Google Drive and returns its ID."""
        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder"
        }
        if parent_id:
            metadata["parents"] = [parent_id]

        folder = self.service.files().create(
            body=metadata, fields="id"
        ).execute()
        logger.info(f"Created Drive folder '{folder_name}' (ID: {folder.get('id')})")
        return folder.get("id")

    def get_or_create_folder(self, folder_name: str, parent_id: Optional[str] = None) -> str:
        """Finds an existing folder or creates it if absent."""
        folder_id = self.find_folder(folder_name, parent_id)
        if not folder_id:
            folder_id = self.create_folder(folder_name, parent_id)
        return folder_id

    def setup_drive_structure(self) -> Dict[str, str]:
        """
        Locates or creates the root folder and the two subfolders:
          - AI Generated Videos  (input — user uploads here)
          - Final Videos         (output — daemon uploads here)

        Returns: { "root": id, "ai": id, "final": id }
        """
        # Root folder
        root_id = config.GDRIVE_ROOT_FOLDER_ID
        if not root_id:
            root_id = self.find_folder(config.GDRIVE_ROOT_FOLDER_NAME)
            if not root_id:
                # Typo-tolerant fallback
                root_id = self.find_folder("AI Automation TIktok")
            if not root_id:
                root_id = self.create_folder(config.GDRIVE_ROOT_FOLDER_NAME)

        # AI Generated Videos
        ai_folder_id = (
            self.find_folder(config.AI_GEN_DIR_NAME, parent_id=root_id)
            or self.find_folder("AI Genreated Videos", parent_id=root_id)
            or self.create_folder(config.AI_GEN_DIR_NAME, parent_id=root_id)
        )

        # Final Videos
        final_folder_id = (
            self.find_folder(config.FINAL_DIR_NAME, parent_id=root_id)
            or self.create_folder(config.FINAL_DIR_NAME, parent_id=root_id)
        )

        return {
            "root":  root_id,
            "ai":    ai_folder_id,
            "final": final_folder_id,
        }

    # -----------------------------------------------------------------------
    # FILE LISTING
    # -----------------------------------------------------------------------

    def list_videos_in_folder(self, folder_id: str) -> List[Dict]:
        """
        Lists all video files (non-folder, non-trashed) inside a Drive folder.
        Returns list of dicts: { id, name, size, md5Checksum }.
        """
        query = (
            f"'{folder_id}' in parents "
            f"and trashed = false "
            f"and mimeType != 'application/vnd.google-apps.folder'"
        )
        video_files = []
        page_token = None

        while True:
            response = self.service.files().list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, size, md5Checksum)",
                pageToken=page_token
            ).execute()

            for f in response.get("files", []):
                if any(f["name"].lower().endswith(ext) for ext in config.VIDEO_EXTENSIONS):
                    video_files.append(f)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return video_files

    # -----------------------------------------------------------------------
    # DOWNLOAD
    # -----------------------------------------------------------------------

    def download_file(self, file_id: str, destination_path: Path) -> Path:
        """Downloads a Drive file in 10 MB chunks to destination_path."""
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dest = destination_path.with_name(f".{destination_path.name}.part")

        request = self.service.files().get_media(fileId=file_id)
        with open(temp_dest, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=10 * 1024 * 1024)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status:
                    logger.debug(
                        f"Downloading {destination_path.name}: "
                        f"{int(status.progress() * 100)}%"
                    )

        temp_dest.replace(destination_path)
        logger.info(f"Downloaded '{destination_path.name}' from Drive.")
        return destination_path

    # -----------------------------------------------------------------------
    # UPLOAD
    # -----------------------------------------------------------------------

    def upload_file(
        self,
        local_path: Path,
        parent_folder_id: str,
        file_name: Optional[str] = None,
        mime_type: str = "video/mp4",
    ) -> Dict:
        """Uploads a local file to Drive using a chunked resumable upload."""
        local_path = Path(local_path)
        target_name = file_name or local_path.name

        metadata = {"name": target_name, "parents": [parent_folder_id]}
        media = MediaFileUpload(
            str(local_path),
            mimetype=mime_type,
            resumable=True,
            chunksize=5 * 1024 * 1024,
        )

        request = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, size"
        )

        logger.info(f"Uploading '{target_name}' to Drive 'Final Videos'...")
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(
                    f"Upload '{target_name}': {int(status.progress() * 100)}%"
                )

        logger.info(
            f"Upload complete: '{target_name}' (Drive ID: {response.get('id')})"
        )
        return response
