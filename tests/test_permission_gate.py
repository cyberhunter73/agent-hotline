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


def test_rising_intonation_yes_question_is_unclear_not_approve():
    """M3: Whisper's rising-intonation transcription of "yes...?" (a caller
    thinking out loud, not committing) is not consent — must reprompt, not
    approve."""
    assert evaluate_answer(TranscriptResult("yes?", 0.05, -0.3)) == PermissionAnswer.UNCLEAR
    assert evaluate_answer(TranscriptResult("Yes...?", 0.05, -0.3)) == PermissionAnswer.UNCLEAR


def test_empty_transcript_with_high_speech_likelihood_is_unclear_not_approve():
    """Task 3's already-deferred missing test: an empty transcript that
    `ears` nonetheless scored as likely speech (low no_speech_prob, high
    avg_logprob) must never resolve to APPROVE — there is no "yes" in it."""
    result = evaluate_answer(TranscriptResult("", 0.05, -0.3))
    assert result == PermissionAnswer.UNCLEAR
