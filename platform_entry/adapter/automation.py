"""Composable visible-page automation for 外部平台 paper input."""

from .page_assets import PlatformInputAssetMixin
from .page_cards import PlatformInputCardMixin
from .page_content import PlatformInputContentMixin
from .page_forms import PlatformInputFormMixin
from .page_navigation import PlatformInputNavigationMixin
from .content_imitation import PlatformInputImitationContentMixin
from .content_legacy_exam import PlatformInputLegacyExamContentMixin
from .content_record import PlatformInputRecordContentMixin
from .content_response import PlatformInputResponseContentMixin
from .content_selection import PlatformInputSelectionContentMixin


class PlatformInputPageAutomation(
    PlatformInputNavigationMixin,
    PlatformInputFormMixin,
    PlatformInputCardMixin,
    PlatformInputAssetMixin,
    PlatformInputContentMixin,
    PlatformInputSelectionContentMixin,
    PlatformInputResponseContentMixin,
    PlatformInputImitationContentMixin,
    PlatformInputRecordContentMixin,
    PlatformInputLegacyExamContentMixin,
):
    """Compose reusable navigation, form, card, asset, and content actions.

    A new question type should add its own ``content_<type>.py`` mixin and
    register its parser/count/page-method contract. Existing browser lifecycle
    and upload behavior remains shared by every paper bundle.
    """

    pass


__all__ = ["PlatformInputPageAutomation"]
