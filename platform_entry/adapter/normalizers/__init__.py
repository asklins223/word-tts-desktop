"""Question-family normalizers for the page-semantic input contract."""

from .imitation import normalise_imitation_group
from .legacy_exam import (
    normalise_info_acquisition_group,
    normalise_info_retelling_group,
)
from .record_retelling import normalise_record_retelling_group
from .response import normalise_response_group
from .selection import normalise_selection_group

__all__ = [
    "normalise_imitation_group",
    "normalise_info_acquisition_group",
    "normalise_info_retelling_group",
    "normalise_record_retelling_group",
    "normalise_response_group",
    "normalise_selection_group",
]
