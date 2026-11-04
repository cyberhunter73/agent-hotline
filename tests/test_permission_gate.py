from agent_hotline.ears import TranscriptResult
from agent_hotline.permission_gate import PermissionAnswer, evaluate_answer


def test_exact_yes_approves():
    assert evaluate_answer(TranscriptResult("yes", 0.05, -0.3)) == PermissionAnswer.APPROVE
    assert evaluate_answer(TranscriptResult("Yes.", 0.05, -0.3)) == PermissionAnswer.APPROVE


def test_deny_keywords_deny():
    for word in ["no", "deny", "nope", "stop"]:
        assert evaluate_answer(TranscriptResult(word, 0.05, -0.3)) == PermissionAnswer.DENY


def test_clarifying_sentence_is_unclear_not_deny():
    assert evaluate_answer(TranscriptResult("what do you mean by that", 0.05, -0.3)) == PermissionAnswer.UNCLEAR


def test_non_speech_transcript_denies():
    assert evaluate_answer(TranscriptResult("Thanks for watching!", 0.95, -0.2)) == PermissionAnswer.DENY


def test_timeout_with_no_transcript_denies():
    assert evaluate_answer(None) == PermissionAnswer.DENY
