"""
Support for prepared statements
"""

# Copyright (C) 2020-2021 The Psycopg Team

from enum import IntEnum, auto
from typing import Optional, Sequence, Tuple, TYPE_CHECKING, Union
from collections import OrderedDict

from .pq import ExecStatus
from ._queries import PostgresQuery

if TYPE_CHECKING:
    from .pq.abc import PGresult


class Prepare(IntEnum):
    NO = auto()
    YES = auto()
    SHOULD = auto()


Key = Tuple[bytes, Tuple[int, ...]]
Value = Union[int, bytes]


class PrepareManager:
    # Number of times a query is executed before it is prepared.
    prepare_threshold: Optional[int] = 5

    # Maximum number of prepared statements on the connection.
    prepared_max: int = 100

    def __init__(self) -> None:
        # Number of times each query was seen in order to prepare it.
        # Map (query, types) -> name or number of times seen
        #
        # Note: with this implementation we keep the tally of up to 100
        # queries, but most likely we will prepare way less than that. We might
        # change that if we think it would be better.
        self._prepared: OrderedDict[Key, Value] = OrderedDict()

        # Counter to generate prepared statements names
        self._prepared_idx = 0

    @staticmethod
    def key(query: PostgresQuery) -> Key:
        return (query.query, query.types)

    def get(
        self, query: PostgresQuery, prepare: Optional[bool] = None
    ) -> Tuple[Prepare, bytes]:
        """
        Check if a query is prepared, tell back whether to prepare it.
        """
        if prepare is False or self.prepare_threshold is None:
            # The user doesn't want this query to be prepared
            return Prepare.NO, b""

        key = self.key(query)
        value: Union[bytes, int] = self._prepared.get(key, 0)
        if isinstance(value, bytes):
            # The query was already prepared in this session
            return Prepare.YES, value

        if value >= self.prepare_threshold or prepare:
            # The query has been executed enough times and needs to be prepared
            name = f"_pg3_{self._prepared_idx}".encode()
            self._prepared_idx += 1
            return Prepare.SHOULD, name
        else:
            # The query is not to be prepared yet
            return Prepare.NO, b""

    def _should_discard(
        self, prep: Prepare, results: Sequence["PGresult"]
    ) -> Optional[bytes]:
        """Check if we need to discard our entire state: it should happen on
        rollback or on dropping objects, because the same object may get
        recreated and postgres would fail internal lookups.
        """
        if self._prepared or prep == Prepare.SHOULD:
            for result in results:
                if result.status != ExecStatus.COMMAND_OK:
                    continue
                cmdstat = result.command_status
                if cmdstat and (
                    cmdstat.startswith(b"DROP ") or cmdstat == b"ROLLBACK"
                ):
                    return self.clear()
        return None

    @staticmethod
    def _check_results(results: Sequence["PGresult"]) -> bool:
        """Return False if 'results' are invalid for prepared statement cache."""
        if len(results) != 1:
            # We cannot prepare a multiple statement
            return False

        status = results[0].status
        if ExecStatus.COMMAND_OK != status != ExecStatus.TUPLES_OK:
            # We don't prepare failed queries or other weird results
            return False

        return True

    def _rotate(self) -> Optional[bytes]:
        """Evict an old value from the cache.

        If it was prepared, deallocate it. Do it only once: if the cache was
        resized, deallocate gradually.
        """
        if len(self._prepared) <= self.prepared_max:
            return None

        old_val = self._prepared.popitem(last=False)[1]
        if isinstance(old_val, bytes):
            return b"DEALLOCATE " + old_val
        else:
            return None

    def maybe_add_to_cache(
        self, query: PostgresQuery, prep: Prepare, name: bytes
    ) -> Optional[Key]:
        """Handle 'query' for possible addition to the cache.

        If a new entry has been added, return its key. Return None otherwise
        (meaning the query is already in cache or cache is not enabled).

        Note: This method is only called in pipeline mode.
        """
        # don't do anything if prepared statements are disabled
        if self.prepare_threshold is None:
            return None

        key = self.key(query)
        if key in self._prepared:
            if isinstance(self._prepared[key], int):
                if prep is Prepare.SHOULD:
                    self._prepared[key] = name
                else:
                    self._prepared[key] += 1  # type: ignore[operator]
            self._prepared.move_to_end(key)
            return None
        else:
            value: Value = name if prep is Prepare.SHOULD else 1
            self._prepared[key] = value
            return key

    def validate(
        self,
        key: Key,
        prep: Prepare,
        name: bytes,
        results: Sequence["PGresult"],
    ) -> Optional[bytes]:
        """Validate cached entry with 'key' by checking query 'results'.

        Possibly return a command to perform maintainance on database side.

        Note: this method is only called in pipeline mode.
        """
        cmd = self._should_discard(prep, results)
        if cmd:
            return cmd
        if not self._check_results(results):
            del self._prepared[key]
            return None
        return self._rotate()

    def clear(self) -> Optional[bytes]:
        if self._prepared_idx:
            self._prepared.clear()
            self._prepared_idx = 0
            return b"DEALLOCATE ALL"
        else:
            return None
