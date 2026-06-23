"""
Kuzu dataset database handler.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional
from uuid import UUID

from m_flow.adapters.dataset_database_handler.dataset_database_handler_interface import (
    DatasetStoreHandlerInterface,
)
from m_flow.adapters.graph.config import get_graph_config
from m_flow.auth.models import DatasetStore, User
from m_flow.base_config import get_base_config
from m_flow.shared.logging_utils import get_logger

_HANDLER_KEY = "kuzu"
_log = get_logger("KuzuDBHandler")


class KuzuDatasetStoreHandler(DatasetStoreHandlerInterface):
    """Provisions and removes local Kuzu databases for individual datasets."""

    @classmethod
    async def create_dataset(
        cls,
        dataset_id: Optional[UUID],
        user: Optional[User],
    ) -> dict:
        """Create a new local directory for a Kuzu DB."""
        graph_cfg = get_graph_config()
        root_cfg = get_base_config()

        if graph_cfg.graph_database_provider.lower() != _HANDLER_KEY:
            raise ValueError(f"Only Kuzu provider is supported by this handler, got {graph_cfg.graph_database_provider}")

        # The databases folder inside the system root directory
        storage_dir = os.path.join(root_cfg.system_root_directory, "databases")

        # In multi-tenant environments, scope by user ID
        user_dir = storage_dir
        if user and user.id:
            user_dir = os.path.join(storage_dir, str(user.id))

        os.makedirs(user_dir, exist_ok=True)

        # e.g., dataset_uuid.kuzu
        db_name = f"{dataset_id}.kuzu"
        full_path = os.path.join(user_dir, db_name)

        return {
            "graph_database_provider": _HANDLER_KEY,
            "graph_database_url": "", # Local files don't use URL
            "graph_database_key": "",
            "graph_database_name": db_name,
            "graph_file_path": user_dir,
            "graph_dataset_database_handler": _HANDLER_KEY,
        }

    @classmethod
    async def delete_dataset(cls, db_record: DatasetStore) -> None:
        """Wipe data by removing the directory."""
        if not db_record.graph_file_path or not db_record.graph_database_name:
            _log.warning("Cannot delete Kuzu dataset: missing path/name.")
            return

        full_path = os.path.join(db_record.graph_file_path, db_record.graph_database_name)
        if os.path.exists(full_path) and os.path.isdir(full_path):
            try:
                shutil.rmtree(full_path)
                _log.info(f"Deleted Kuzu database at {full_path}")
            except Exception as e:
                _log.error(f"Failed to delete Kuzu database at {full_path}: {e}")
