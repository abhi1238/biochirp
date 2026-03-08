


"""File storage utilities for saving and managing data files."""

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional
import pandas as pd

from .uvicorn_logger import setup_logger

# logger = setup_logger("opentargets.file_storage")
import logging
# =========================================================
# Logging
# =========================================================
base_logger = logging.getLogger("uvicorn.error")
logger = base_logger.getChild("opentargets.file_storage")



class FileStorage:
    """Handles file storage operations for data exports."""
    
    def __init__(self, base_path: str = None):
        """Initialize file storage.
        
        Args:
            base_path: Base directory for storing files. Defaults to RESULTS_ROOT env var.
        """
        self.base_path = Path(base_path or os.getenv("RESULTS_ROOT", "/app/results"))
        self._ensure_directory()
    
    def _ensure_directory(self):
        """Ensure the base directory exists."""
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"[FILE STORAGE] Using directory: {self.base_path}")
        except Exception as e:
            logger.error(f"[FILE STORAGE] Failed to create directory {self.base_path}: {e}")
            raise
    
    def _generate_filename(self, prefix: str, extension: str = "csv") -> str:
        """Generate a unique filename.
        
        Args:
            prefix: Filename prefix
            extension: File extension (without dot)
            
        Returns:
            Unique filename string
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:8]
        return f"{prefix}_{timestamp}_{unique_id}.{extension}"
    
    def save_dataframe(
        self, 
        df: pd.DataFrame, 
        prefix: str = "data",
        connection_id: Optional[str] = None,
        index: bool = False
    ) -> Tuple[str, str]:
        """Save DataFrame to CSV file.
        
        Args:
            df: DataFrame to save
            prefix: Filename prefix
            connection_id: Optional connection ID for organizing files
            index: Whether to include DataFrame index
            
        Returns:
            Tuple of (full_path, filename)
        """
        filename = self._generate_filename(prefix)
        
        # Optionally organize by connection ID
        if connection_id:
            save_dir = self.base_path / connection_id
            save_dir.mkdir(parents=True, exist_ok=True)
        else:
            save_dir = self.base_path
        
        full_path = save_dir / filename
        
        try:
            # Save with UTF-8 encoding
            df.to_csv(full_path, index=index, encoding='utf-8-sig')
            
            logger.info(f"[FILE STORAGE] Saved {len(df)} rows to {full_path}")
            
            return str(full_path), filename
            
        except Exception as e:
            logger.error(f"[FILE STORAGE] Failed to save DataFrame: {e}", exc_info=True)
            raise
    
    def get_file_path(self, filename: str) -> Optional[Path]:
        """Get full path for a filename.
        
        Args:
            filename: Name of file to find
            
        Returns:
            Full Path object or None if not found
        """
        # Check direct path
        direct_path = self.base_path / filename
        if direct_path.exists():
            return direct_path
        
        # Search in subdirectories
        for path in self.base_path.rglob(filename):
            return path
        
        return None
    
    def file_exists(self, filename: str) -> bool:
        """Check if a file exists.
        
        Args:
            filename: Name of file to check
            
        Returns:
            True if file exists
        """
        return self.get_file_path(filename) is not None
    
    def list_files(self, pattern: str = "*.csv") -> list:
        """List files matching pattern.
        
        Args:
            pattern: Glob pattern for matching files
            
        Returns:
            List of file info dictionaries
        """
        files = []
        for path in self.base_path.rglob(pattern):
            try:
                stat = path.stat()
                files.append({
                    "name": path.name,
                    "path": str(path),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "download_url": f"/download/{path.name}"
                })
            except Exception as e:
                logger.warning(f"[FILE STORAGE] Failed to stat {path}: {e}")
        
        return sorted(files, key=lambda x: x["modified"], reverse=True)
    
    def delete_file(self, filename: str) -> bool:
        """Delete a file.
        
        Args:
            filename: Name of file to delete
            
        Returns:
            True if file was deleted
        """
        path = self.get_file_path(filename)
        if path:
            try:
                path.unlink()
                logger.info(f"[FILE STORAGE] Deleted {path}")
                return True
            except Exception as e:
                logger.error(f"[FILE STORAGE] Failed to delete {path}: {e}")
        return False
    
    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """Delete files older than specified age.
        
        Args:
            max_age_hours: Maximum age in hours
            
        Returns:
            Number of files deleted
        """
        from datetime import timedelta
        
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        deleted = 0
        
        for path in self.base_path.rglob("*.csv"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
                if mtime < cutoff:
                    path.unlink()
                    deleted += 1
                    logger.info(f"[FILE STORAGE] Cleaned up old file: {path}")
            except Exception as e:
                logger.warning(f"[FILE STORAGE] Failed to cleanup {path}: {e}")
        
        return deleted


# Singleton instance
_file_storage: Optional[FileStorage] = None


def get_file_storage() -> FileStorage:
    """Get the singleton FileStorage instance.
    
    Returns:
        FileStorage instance
    """
    global _file_storage
    if _file_storage is None:
        _file_storage = FileStorage()
    return _file_storage