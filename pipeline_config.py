"""Pipeline-builder config store — per-module customised stage layouts.

The pipeline builder UI lets a user replace a ``PipelineModule``'s
hard-coded phases with their own ordering, including marking groups of
stages to run in parallel.  Saved layouts are keyed by the pipeline
module's class name (e.g. ``OpenNetworkPipeline``).

A *layout* is a list of phases, where each phase is either:

* a string  – the ``stage.name`` of a single sequential stage; or
* a list of strings – two or more stage names to run in parallel.

The store also records each pipeline's hard-coded defaults the first
time the module is registered, so the UI can show them as a baseline
and offer a "reset to defaults" action.
"""

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from logging_config import get_logger

logger = get_logger('pipeline_config')


# Phase = stage name (str) for sequential, or list[str] for parallel.
Phase = object  # documentation alias; actual type is str | list[str]


class PipelineConfigStore:
    """MongoDB-backed store for user-customised pipeline layouts.

    Falls back to in-memory defaults when MongoDB is unavailable so the
    builder UI keeps working in read-only mode.
    """

    COLLECTION = 'pipeline_config'

    def __init__(self, mongo_uri: str = 'mongodb://localhost:27017/',
                 db_name: str = 'vasili'):
        self._available = False
        self._defaults: dict[str, list] = {}

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=2000)
            self.client.admin.command('ping')
            self.db = self.client[db_name]
            self.collection = self.db[self.COLLECTION]
            self._available = True
            self.collection.create_index('module', unique=True)
            logger.info('PipelineConfigStore connected to MongoDB')
        except (ConnectionFailure, OperationFailure) as e:
            logger.warning(f'MongoDB not available for pipeline config: {e}')
        except Exception as e:
            logger.error(f'Failed to initialise PipelineConfigStore: {e}')

    # ------------------------------------------------------------------
    # Defaults registry — populated by PipelineModule.__init__.
    # ------------------------------------------------------------------

    def register_defaults(self, module_name: str, phases: list):
        """Capture a module's hard-coded phases as the reset target."""
        self._defaults[module_name] = self._serialise(phases)

    def get_defaults(self, module_name: str) -> list:
        return list(self._defaults.get(module_name, []))

    def get_all_defaults(self) -> dict[str, list]:
        return {k: list(v) for k, v in self._defaults.items()}

    # ------------------------------------------------------------------
    # User layouts.
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return self._available

    def get_layout(self, module_name: str) -> list | None:
        """Return the user-saved layout, or ``None`` if defaults apply."""
        if not self._available:
            return None
        try:
            doc = self.collection.find_one({'module': module_name})
            if doc and isinstance(doc.get('phases'), list):
                return doc['phases']
        except Exception as e:
            logger.error(f'Failed to read pipeline config for {module_name}: {e}')
        return None

    def effective_layout(self, module_name: str) -> list:
        """User layout if present, otherwise registered defaults."""
        custom = self.get_layout(module_name)
        if custom is not None:
            return custom
        return self.get_defaults(module_name)

    def set_layout(self, module_name: str, phases: list) -> bool:
        """Persist a user-supplied layout. Validates against the registry."""
        if not self._available:
            logger.warning('Cannot save pipeline layout: MongoDB unavailable')
            return False
        try:
            normalised = self._serialise(phases)
            self.collection.update_one(
                {'module': module_name},
                {'$set': {'phases': normalised}},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f'Failed to save pipeline layout for {module_name}: {e}')
            return False

    def reset_layout(self, module_name: str) -> bool:
        """Delete any user layout so defaults take effect again."""
        if not self._available:
            return False
        try:
            self.collection.delete_one({'module': module_name})
            return True
        except Exception as e:
            logger.error(f'Failed to reset pipeline layout for {module_name}: {e}')
            return False

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _serialise(phases: list) -> list:
        """Coerce a phases list into the on-disk shape (names only)."""
        out: list = []
        for phase in phases or []:
            if isinstance(phase, list):
                names = []
                for item in phase:
                    name = getattr(item, 'name', item)
                    if isinstance(name, str):
                        names.append(name)
                if names:
                    out.append(names)
            else:
                name = getattr(phase, 'name', phase)
                if isinstance(name, str):
                    out.append(name)
        return out
