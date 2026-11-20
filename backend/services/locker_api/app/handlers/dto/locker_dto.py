from __future__ import annotations

from dataclasses import dataclass

from ...locker_data.db.models.locker import LockerView

ALLOWED_BATCH_VIEWS = {v.value for v in LockerView}


@dataclass
class BatchLockerRequestDTO:
    locker_ids: list[str]
    view: str | None = None

    def __post_init__(self) -> None:
        if not self.locker_ids:
            raise ValueError("locker_ids must not be empty")
        if len(self.locker_ids) > 100:
            raise ValueError("locker_ids must not exceed 100 items")
        if self.view is not None and self.view not in ALLOWED_BATCH_VIEWS:
            raise ValueError(
                f"Invalid view value: {self.view}. Allowed: {sorted(ALLOWED_BATCH_VIEWS)}"
            )
