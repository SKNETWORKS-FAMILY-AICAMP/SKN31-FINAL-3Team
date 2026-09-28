"""Known ERP entry-template guidance is not an item specification."""
import re

_ITEM_TEMPLATE_NOTICE = re.compile(
    r"\[?\s*자동생성\s*[-–—]\s*품목분류\s+필수\s+규격\s*,\s*값을\s*채워주세요\s*\]?"
)


def remove_item_template_notice(text: str) -> str:
    """Remove only the known notice, never a whole line or required labels.

    Keep ERP source documents unchanged: their item-entry form still uses its
    marker. Call this on text used for display/parsing/model input instead.
    """
    return _ITEM_TEMPLATE_NOTICE.sub("", text).strip()
