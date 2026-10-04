"""ExoML / TwiML voice-markup builder.

Exotel's ExoML and Twilio's TwiML share the same verb vocabulary for
everything the triage flow needs. Attribute names verified against Exotel's
own official library (github.com/exotel/goexoml, verbs.go):

    <Response>
      <Say voice="woman" language="hi-IN" loop="1">text</Say>
      <Gather action="URL" method="POST" timeout="10"
              finishOnKey="#" numDigits="1"> <Say>…</Say> </Gather>
      <Redirect method="POST">URL</Redirect>
      <Hangup/>
    </Response>

Gather semantics (both providers): the caller hears the nested Say/Play and
gets `timeout` seconds to press `numDigits` keys; the digits are submitted to
`action`. With NO input, execution falls through to the next verb -- which is
why every interactive prompt pairs the Gather with a Redirect back to the
same webhook: the redirect arrives without a Digits parameter and the state
machine records it as silence (repeat once, then UNCLEAR -> escalate).

This module only renders. Every decision stays in the call-flow state
machine; the text it hands us is already in the caller's language. Strings
here reach a panicking caller over a live PSTN leg, so escaping is not
optional.
"""
from __future__ import annotations

from ..engine import i18n

XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>'

# Exotel TTS voices are "man"/"woman"; a calm female voice is the right
# default for maternal-health triage in the target regions.
DEFAULT_VOICE = "woman"


def esc(text: str) -> str:
    """Escape for XML text nodes AND attribute values (quotes included)."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def say(text: str, language: str | None = None, voice: str | None = DEFAULT_VOICE) -> str:
    attrs = ""
    if language:
        attrs += f' language="{esc(language)}"'
    if voice:
        attrs += f' voice="{esc(voice)}"'
    return f"<Say{attrs}>{esc(text)}</Say>"


def gather(
    inner: str,
    action: str,
    *,
    method: str = "POST",
    timeout: int = 10,
    num_digits: int = 1,
    finish_on_key: str = "#",
) -> str:
    return (
        f'<Gather action="{esc(action)}" method="{esc(method)}" '
        f'timeout="{int(timeout)}" numDigits="{int(num_digits)}" '
        f'finishOnKey="{esc(finish_on_key)}">{inner}</Gather>'
    )


def redirect(url: str, method: str = "POST") -> str:
    return f'<Redirect method="{esc(method)}">{esc(url)}</Redirect>'


def hangup() -> str:
    return "<Hangup/>"


def response(*verbs: str) -> str:
    return XML_DECL + "<Response>" + "".join(verbs) + "</Response>"


def speech_locale(language: str | None) -> str:
    """Our language code -> the BCP-47 locale providers' TTS expects."""
    return i18n.SPEECH_LOCALE.get(language or "en", "en-IN")


def ivr_gather(
    text: str,
    action_url: str,
    silence_url: str,
    *,
    language: str | None = None,
    timeout: int = 10,
) -> str:
    """One interactive IVR step: speak `text`, collect one digit into
    `action_url`; on no input fall through to `silence_url` (same webhook,
    which records silence and repeats/escalates per the flow rules)."""
    return response(
        gather(say(text, language=speech_locale(language)), action_url,
               timeout=timeout, num_digits=1),
        redirect(silence_url),
    )


def ivr_final(text: str, *, language: str | None = None) -> str:
    """Terminal playback: speak and hang up (no input expected)."""
    return response(say(text, language=speech_locale(language)), hangup())
