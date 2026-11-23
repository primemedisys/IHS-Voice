"""Offline checks for the situation-aware filler lines. Run: python tests/test_filler_offline.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.shared.loader import load_replies, set_reply_language  # noqa: E402
from app.voice.filler import filler_kind, pick_filler  # noqa: E402


def test_kinds():
    assert filler_kind("my knee pain is bad", "intake") == "empathy"
    assert filler_kind("ମୋ ଗୋଡ଼ରେ ଯନ୍ତ୍ରଣା ଅଛି", "intake") == "empathy"
    assert filler_kind("what is the fee", "anything_else") == "question"
    assert filler_kind("my name is Ravi", "intake") == "details"
    assert filler_kind("tomorrow at 5", "slot") == "details"
    assert filler_kind(None, None) == "details"


def test_none_on_emergency():
    assert filler_kind("heavy bleeding please help", "intake") is None
    assert filler_kind("the pain is getting worse", "intake") is None


def test_no_repeat_and_languages():
    set_reply_language("en")
    for lang in ("en", "hi", "or"):
        used: set[str] = set()
        lines = [pick_filler("empathy", lang, used) for _ in range(3)]
        assert len(set(lines)) == 3 and all(lines)
        assert pick_filler("empathy", lang, used) is None


def test_all_kinds_have_all_languages():
    replies = load_replies()
    for kind in ("empathy", "details", "question"):
        for lang in ("en", "hi", "or"):
            assert len(replies[f"filler_{kind}"][lang]) >= 3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
