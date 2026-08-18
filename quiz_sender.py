"""
quiz_sender.py
==============
Sends quiz questions from the AchieveCap/ClassX JSON format to Telegram.

Called by bot.py via:
    quiz_sender.send_json_quiz(bot, dest_chat_id, raw_q, n)

The three stubs below (smart_delay, safe_send, safe_send_photo) are patched
at startup in bot.py with the real rate-limited versions:
    quiz_sender.smart_delay     = smart_delay
    quiz_sender.safe_send       = safe_send
    quiz_sender.safe_send_photo = safe_send_photo

Four cases handled
──────────────────
Case 1 – All text
    question, options, explanation are plain text → single quiz poll

Case 2 – Image question  (what your JSON uses)
    question has <img>; options are plain letter text (A/B/C/D)
    Flow:
      1. Send question image   caption: "Q{n}"
      2. Send quiz poll        options: stripped text from option fields
                               correct_option_ids: [correct_idx]
                               Telegram marks the right answer inside the poll
      3. Send explanation img  caption: "💡 Explanation for Q{n}"
         If explanation has BOTH image + text → text becomes the photo caption (Case 3)

Case 3 – Mixed explanation
    explanation has an image AND text.
    Handled inside Case 2 path: text is appended to the explanation photo caption.
    No separate text message sent.

Case 4 – Image options
    One or more option fields contain an image URL.
    Flow:
      1. Question image or text message
      2. Each option image  caption: "Option A / B / C / D"  — NO ✅/❌ markers
         (correct answer shown ONLY inside the poll widget)
      3. Letter poll: options ["A","B","C","D"]
                      correct_option_ids: [correct_idx]
      4. Explanation (image/text/both)
"""

import asyncio
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode


# ── Stubs – replaced by bot.py at import time ─────────────────────────────────

async def smart_delay():
    await asyncio.sleep(0.5)

async def safe_send(coro, retries: int = 6):
    return await coro

async def safe_send_photo(bot, chat_id, img_path,
                          caption=None, reply_to_id=None, retries=6):
    with open(img_path, "rb") as f:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=f,
            caption=caption[:1024] if caption else None,
            reply_to_message_id=reply_to_id,
        )


# ── HTML helpers ──────────────────────────────────────────────────────────────

_STYLE_RE = re.compile(r'<style[^>]*>.*?</style>', re.DOTALL | re.IGNORECASE)
_IMG_RE   = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_TAG_RE   = re.compile(r'<[^>]+>')
_WS_RE    = re.compile(r'\s+')
_CSS_RE   = re.compile(r'\*\s*\{[^}]*\}')  # bare CSS: *{overflow-wrap:…}


def _strip_html(html: str) -> str:
    text = _STYLE_RE.sub('', html)
    text = _TAG_RE.sub(' ', text)
    return _WS_RE.sub(' ', text).strip()


def _extract_img_urls(html: str) -> list[str]:
    return _IMG_RE.findall(html)


def _meaningful_text(html: str) -> str:
    """
    Return plain text after stripping HTML, or '' if nothing meaningful.

    Filters out:
      • Pure CSS noise like "*{overflow-wrap: break-word;}"
      • Single characters — in the AchieveCap format options are literally
        "A", "B", "C", "D" as text.  These are just labels, not content.
        The poll itself will show the letter, so we treat them as empty.
    """
    raw     = _strip_html(html)
    cleaned = _CSS_RE.sub('', raw).strip()
    if len(cleaned) <= 1:
        return ''
    return cleaned


# ── Image download ────────────────────────────────────────────────────────────

def _download_url_to_temp(url: str, suffix: str = ".jpg") -> Optional[str]:
    try:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (QuizBot/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp, \
             open(path, "wb") as f:
            f.write(resp.read())
        return path
    except Exception as e:
        print(f"      ⚠️  Download failed {url[:60]}: {e}")
        return None


async def _send_url_as_photo(bot: Bot, chat_id,
                              url: str,
                              caption: str = "",
                              reply_to_id: Optional[int] = None) -> Optional[int]:
    """Download url and send as photo. Returns message_id or None."""
    ext  = Path(url.split("?")[0]).suffix or ".jpg"
    path = _download_url_to_temp(url, suffix=ext)
    if not path:
        return None
    try:
        sent = await safe_send_photo(
            bot, chat_id, path,
            caption=caption[:1024] if caption else None,
            reply_to_id=reply_to_id,
        )
        return sent.message_id
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


# ── Content classification ────────────────────────────────────────────────────

_LETTERS = ["A", "B", "C", "D", "E", "F"]

def _letter(i: int) -> str:
    return _LETTERS[i] if i < len(_LETTERS) else str(i + 1)


class _Q:
    """Parsed content for one raw question dict."""

    def __init__(self, raw: dict, number: int):
        self.number      = number
        self.raw         = raw
        self.correct_idx = int(raw["correct_answer"]) - 1  # 0-based

        # question
        q_html        = raw.get("question", "")
        self.q_imgs   = _extract_img_urls(q_html)
        self.q_text   = _meaningful_text(q_html)
        self.q_is_img = bool(self.q_imgs)

        # options
        self.options: list[dict] = []
        for opt in raw.get("options", []):
            opt_html  = opt.get("text", "")
            img_field = opt.get("image", "")
            html_imgs = _extract_img_urls(opt_html)
            img_url   = img_field or (html_imgs[0] if html_imgs else "")
            opt_text  = _meaningful_text(opt_html)
            self.options.append({
                "img_url":  img_url,
                "text":     opt_text,
                "is_image": bool(img_url),
            })
        self.opts_are_images = any(o["is_image"] for o in self.options)

        # explanation
        exp_html         = raw.get("explanation", "")
        self.exp_imgs    = _extract_img_urls(exp_html)
        self.exp_text    = _meaningful_text(exp_html)
        self.exp_is_img  = bool(self.exp_imgs)
        self.exp_has_txt = bool(self.exp_text)


# ── Shared explanation sender ─────────────────────────────────────────────────

async def _send_explanation(bot: Bot, chat_id: int, q: _Q) -> None:
    """
    Send explanation after the poll.

    • image only     → photo, caption "💡 Explanation for Q{n}"
    • image + text   → photo, text as caption  (Case 3: one atomic message)
    • text only long → text message
      (short text already put into the poll's explanation field — not repeated)
    """
    if q.exp_is_img:
        for url in q.exp_imgs[:1]:
            if q.exp_has_txt:
                # Case 3: image + text → text becomes the photo caption
                caption = f"💡 Explanation for Q{q.number}:\n{q.exp_text[:900]}"
            else:
                caption = f"💡 Explanation for Q{q.number}"
            print(f"  🖼️  [Expl] Q{q.number} explanation image")
            await _send_url_as_photo(bot, chat_id, url, caption=caption[:1024])
            await smart_delay()

    elif q.exp_has_txt and len(q.exp_text) > 200:
        # Long text that didn't fit in the poll explanation field
        try:
            await safe_send(bot.send_message(
                chat_id=chat_id,
                text=f"💡 Explanation for Q{q.number}:\n{q.exp_text[:3000]}",
            ))
        except Exception as e:
            print(f"  ⚠️  Explanation text send failed Q{q.number}: {e}")
        await smart_delay()


# ── Fallback: plain text when poll API fails ──────────────────────────────────

async def _fallback_text(bot: Bot, chat_id: int, q: _Q) -> None:
    lines = [f"📋 Q{q.number}: {q.q_text or '(see image above)'}"]
    for i, opt in enumerate(q.options):
        letter    = _letter(i)
        marker    = " ✅" if i == q.correct_idx else ""
        opt_label = opt["text"] or f"Option {letter}"
        lines.append(f"  {letter}.{marker} {opt_label}")
    if q.exp_text:
        lines.append(f"\n💡 {q.exp_text[:500]}")
    try:
        await safe_send(bot.send_message(chat_id=chat_id, text="\n".join(lines)))
    except Exception as e:
        print(f"  ❌  Fallback text also failed Q{q.number}: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Main entry-point  (called by bot.py)
# ═════════════════════════════════════════════════════════════════════════════

async def send_json_quiz(bot: Bot, chat_id: int, raw_q: dict, number: int) -> None:
    """
    Send one question from the JSON quiz format to chat_id.

    Dispatch:
      Case 4 first (image options)
      Case 2 next  (image question or image explanation)
      Case 1 last  (all text)
    """
    q = _Q(raw_q, number)

    if q.opts_are_images:
        await _case4_image_options(bot, chat_id, q)
    elif q.q_is_img or q.exp_is_img:
        await _case2_image_question(bot, chat_id, q)
    else:
        await _case1_text(bot, chat_id, q)


# ─────────────────────────────────────────────────────────────────────────────
#  Case 1 – All text
# ─────────────────────────────────────────────────────────────────────────────

async def _case1_text(bot: Bot, chat_id: int, q: _Q) -> None:
    question    = (q.q_text or "❓")[:300]
    option_txts = [(o["text"] or _letter(i))[:100] for i, o in enumerate(q.options)]
    exp_field   = q.exp_text[:200] if q.exp_has_txt else None

    print(f"  📝  [Case 1] Q{q.number}: \"{question[:60]}\"")
    try:
        await safe_send(bot.send_poll(
            chat_id            = chat_id,
            question           = question,
            options            = option_txts,
            type               = "quiz",
            correct_option_ids = [q.correct_idx],
            explanation        = exp_field,
            is_anonymous       = True,
        ))
    except Exception as e:
        print(f"  ⚠️  Case 1 poll failed Q{q.number}: {e}")
        await _fallback_text(bot, chat_id, q)

    await _send_explanation(bot, chat_id, q)


# ─────────────────────────────────────────────────────────────────────────────
#  Case 2 – Image question  (+ Case 3 mixed explanation handled here)
# ─────────────────────────────────────────────────────────────────────────────

async def _case2_image_question(bot: Bot, chat_id: int, q: _Q) -> None:
    """
    This is the pattern your JSON uses for every question:
      question   = <img>  (full question baked into the image)
      options    = "A" / "B" / "C" / "D"  (single-letter text, treated as labels)
      explanation= <img>  (solution image)

    Flow:
      1. Send question image    caption: "Q{n}"
      2. Send quiz poll         options: ["A","B","C","D"]
                                correct_option_ids: [correct_idx]
                                ← Telegram marks the answer inside the poll widget
      3. Send explanation image caption: "💡 Explanation for Q{n}"
         If explanation has text too (Case 3) → text goes into that same caption
    """
    # Step 1 – question image
    q_msg_id: Optional[int] = None
    if q.q_is_img:
        q_caption = f"Q{q.number}"
        if q.q_text:
            q_caption = f"Q{q.number}: {q.q_text}"
        print(f"  🖼️  [Case 2] Q{q.number} question image")
        q_msg_id = await _send_url_as_photo(
            bot, chat_id, q.q_imgs[0], caption=q_caption[:1024]
        )
        if q_msg_id:
            await smart_delay()

    # Step 2 – poll
    # Poll question: use real text if present, otherwise a short placeholder.
    # The actual question content is visible in the image above the poll.
    if q.q_is_img:
        poll_question = f"Q{q.number}"
    else:
        poll_question = (q.q_text or "❓")[:300]

    # Option text: use meaningful text if present, else fall back to letter label.
    # For your JSON this always gives ["A","B","C","D"] because single chars
    # are filtered out by _meaningful_text() and the fallback _letter() kicks in.
    option_txts = []
    for i, o in enumerate(q.options):
        txt = o["text"] or _letter(i)
        option_txts.append(txt[:100])

    # Put explanation text in the poll field only when there is NO explanation image.
    # When there IS an image, the text goes on the image caption (Step 3 / Case 3).
    exp_field: Optional[str] = None
    if q.exp_has_txt and not q.exp_is_img:
        exp_field = q.exp_text[:200]

    print(f"  🗳️  [Case 2] Q{q.number} poll — correct: {_letter(q.correct_idx)}")
    try:
        await safe_send(bot.send_poll(
            chat_id             = chat_id,
            question            = poll_question,
            options             = option_txts,
            type                = "quiz",
            correct_option_ids  = [q.correct_idx],
            explanation         = exp_field,
            is_anonymous        = True,
            reply_to_message_id = q_msg_id,
        ))
    except Exception as e:
        print(f"  ⚠️  Case 2 poll failed Q{q.number}: {e}")
        await _fallback_text(bot, chat_id, q)

    await smart_delay()

    # Step 3 – explanation (handles image-only, image+text, long text)
    await _send_explanation(bot, chat_id, q)


# ─────────────────────────────────────────────────────────────────────────────
#  Case 4 – Options are images  (NO correct-answer markers in captions)
# ─────────────────────────────────────────────────────────────────────────────

async def _case4_image_options(bot: Bot, chat_id: int, q: _Q) -> None:
    """
    Flow:
      1. Question image (if present) or plain text message
      2. Each option image  caption: "Option A" / "Option B" …
         — NO ✅/❌ markers anywhere in captions
         — correct answer lives ONLY inside the Telegram poll widget
      3. Letter poll: options ["A","B","C","D"]
                      correct_option_ids: [correct_idx]
      4. Explanation (image / text / both)
    """
    # Step 1 – question
    q_msg_id: Optional[int] = None
    if q.q_is_img:
        q_caption = f"Q{q.number}" + (f": {q.q_text}" if q.q_text else "")
        print(f"  🖼️  [Case 4] Q{q.number} question image")
        q_msg_id = await _send_url_as_photo(
            bot, chat_id, q.q_imgs[0], caption=q_caption[:1024]
        )
        if q_msg_id:
            await smart_delay()
    elif q.q_text:
        try:
            await safe_send(bot.send_message(
                chat_id=chat_id,
                text=f"*Q{q.number}:* {q.q_text}",
                parse_mode=ParseMode.MARKDOWN,
            ))
        except Exception:
            await safe_send(bot.send_message(
                chat_id=chat_id, text=f"Q{q.number}: {q.q_text}"
            ))
        await smart_delay()

    # Step 2 – option images (neutral captions, no markers)
    for i, opt in enumerate(q.options):
        letter = _letter(i)
        if opt["is_image"]:
            caption = f"Option {letter}"   # ← no ✅/❌
            print(f"  🖼️  [Case 4] Q{q.number} option {letter} image")
            await _send_url_as_photo(bot, chat_id, opt["img_url"], caption=caption)
            await smart_delay()
        elif opt["text"]:
            try:
                await safe_send(bot.send_message(
                    chat_id=chat_id,
                    text=f"Option {letter}: {opt['text']}",
                ))
            except Exception:
                pass
            await smart_delay()

    # Step 3 – letter poll (correct marked inside Telegram's poll widget)
    poll_question = (q.q_text or f"Q{q.number} — choose the correct option above")[:300]
    if not poll_question.strip():
        poll_question = f"Q{q.number} — choose the correct option above"

    option_txts = [_letter(i) for i in range(len(q.options))]

    exp_field: Optional[str] = None
    if q.exp_has_txt and not q.exp_is_img:
        exp_field = q.exp_text[:200]

    print(f"  🗳️  [Case 4] Q{q.number} letter poll — correct: {_letter(q.correct_idx)}")
    try:
        await safe_send(bot.send_poll(
            chat_id             = chat_id,
            question            = poll_question,
            options             = option_txts,
            type                = "quiz",
            correct_option_ids  = [q.correct_idx],
            explanation         = exp_field,
            is_anonymous        = True,
            reply_to_message_id = q_msg_id,
        ))
    except Exception as e:
        print(f"  ⚠️  Case 4 poll failed Q{q.number}: {e}")
        await _fallback_text(bot, chat_id, q)

    await smart_delay()

    # Step 4 – explanation
    await _send_explanation(bot, chat_id, q)
