"""Telegram out. Standard library only.

Nothing here ever raises into the caller: a broken notification must not
be able to stop the loop it is reporting on.
"""

import json
import os
import time
import urllib.request
import urllib.error

# Where the Bot API lives. BRIDGE_TELEGRAM_API points it somewhere else so a
# test can drive the whole path - send, edit, pin, getUpdates - against a
# recording server on localhost instead of the real thing. It is an
# environment variable and not a config key on purpose: nobody running the
# bridge should ever set this, and a config key that silently redirected
# every message to another host would be a worse bug than the one it helps
# to test for.
API_BASE = os.environ.get("BRIDGE_TELEGRAM_API",
                          "https://api.telegram.org").rstrip("/")
API = API_BASE + "/bot%s/%s"


def api_url(token, method):
    """The URL for one Bot API call. One place, so a redirected base
    reaches every caller - daemon's long-poll included."""
    return API % (token, method)


# The last thing Telegram said, so that a channel which has stopped working
# can say so somewhere. Nothing here raises - a broken notification must not
# stop the loop it reports on - and that used to mean a revoked token was
# perfectly silent: no messages, dead buttons, and not one line anywhere
# saying why. HEALTH is read by the daemon and shown on the panel.
HEALTH = {"ok": None, "why": "", "at": 0.0, "code": None}


def _note(ok, why="", code=None):
    HEALTH["ok"] = bool(ok)
    HEALTH["why"] = why or ""
    HEALTH["code"] = code
    HEALTH["at"] = time.time()
    return ok


def health():
    return dict(HEALTH)


def _call_ex(token, method, payload, timeout=10):
    """Returns (ok, body). body carries Telegram's description on refusal,
    or is None when the call never reached Telegram at all."""
    if not token:
        _note(False, "no token set")
        return False, None
    try:
        req = urllib.request.Request(
            API % (token, method),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ok = bool(body.get("ok"))
        _note(ok, "" if ok else (body.get("description") or "refused"))
        return ok, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = None
        _note(False, (body or {}).get("description")
              or "HTTP %s" % exc.code, exc.code)
        return False, body
    except Exception as exc:
        _note(False, "did not reach Telegram: %s" % exc)
        return False, None


def _call(token, method, payload, timeout=10):
    ok, body = _call_ex(token, method, payload, timeout)
    return body if ok else None


def answer_callback(token, callback_id, text=""):
    """Answer a button press with a toast, not with a message.

    Telegram shows this over the chat and it disappears; it adds nothing to
    the history. A button press is a thing you just did, and the answer to
    it is a confirmation, not news - sending it as a message meant every
    tap left a line behind for ever. Capped at 200 characters by the API,
    so anything longer belongs somewhere that can hold it.
    """
    if not token or not callback_id:
        return False
    ok, _ = _call_ex(token, "answerCallbackQuery",
                     {"callback_query_id": callback_id,
                      "text": (text or "")[:200]})
    return ok


def check_token(token):
    """Return the bot's username, or None. Used by the setup wizard."""
    out = _call(token, "getMe", {})
    if out:
        return out.get("result", {}).get("username")
    return None


def first_sender(token):
    """Return the chat id of whoever wrote to the bot first. Pairing step."""
    try:
        req = urllib.request.Request(API % (token, "getUpdates") + "?timeout=0&limit=10")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        for upd in body.get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                return str(chat["id"]), chat.get("first_name") or chat.get("title") or ""
    except Exception:
        pass
    return None, ""


def send(cfg, text, level="silent", buttons=None):
    """level: sound, silent or log. 'log' never reaches Telegram."""
    if level == "log":
        return None
    tg = cfg.get("telegram", {})
    token, chat = tg.get("token"), tg.get("chat_id")
    if not token or not chat:
        return None
    payload = {
        "chat_id": chat,
        "text": text,
        "disable_notification": level != "sound",
    }
    if buttons:
        # A button is either a plain string - the label is also the data -
        # or (label, data), for when the data has to carry something the
        # label should not show, like which pair the button is about.
        # Telegram caps callback_data at 64 bytes and silently rejects the
        # whole keyboard if it is longer, so it is trimmed here rather than
        # costing every button on the message.
        row = []
        for b in buttons:
            label, data = b if isinstance(b, (tuple, list)) else (b, b)
            row.append({"text": str(label),
                        "callback_data": str(data).encode("utf-8")[:64]
                        .decode("utf-8", "ignore")})
        payload["reply_markup"] = {"inline_keyboard": [row]}
    out = _call(token, "sendMessage", payload)
    return out.get("result", {}).get("message_id") if out else None


def _upsert(cfg, key, text, pin=False, force=False):
    """Keep one message per key and edit it in place; pin only if asked.

    force abandons the message being kept and starts a new one: the old is
    unpinned, a fresh message is sent and pinned in its place. Editing in
    place is right for everything automatic - it is silent, and it cannot
    fill the chat - but it leaves nothing to do when the message a human
    needs has been scrolled past, unpinned or deleted. Only a person asking
    for it should ever pass this.
    """
    tg = cfg.get("telegram", {})
    token, chat = tg.get("token"), tg.get("chat_id")
    if not token or not chat:
        return cfg
    id_key, text_key = key + "_message_id", key + "_text"
    mid = tg.get(id_key) or 0
    if mid and tg.get(text_key) == text and not force:
        return cfg          # identical - editing it would only fail
    gone = False
    if mid and force:
        if pin:
            _call(token, "unpinChatMessage", {"chat_id": chat,
                                              "message_id": mid})
        gone, mid = True, 0
    elif mid:
        ok, body = _call_ex(token, "editMessageText",
                            {"chat_id": chat, "message_id": mid,
                             "text": text})
        desc = ((body or {}).get("description") or "").lower()
        if ok or "not modified" in desc:
            cfg.setdefault("telegram", {})[text_key] = text
            return cfg
        gone = ("not found" in desc or "can't be edited" in desc
                or "cannot be edited" in desc or "message to edit" in desc)
        if not gone:
            return cfg      # network hiccup - keep what we have
        if pin:
            _call(token, "unpinChatMessage", {"chat_id": chat,
                                              "message_id": mid})
    out = _call(token, "sendMessage",
                {"chat_id": chat, "text": text,
                 "disable_notification": True,
                 "disable_web_page_preview": True})
    if out:
        mid = out.get("result", {}).get("message_id")
        if pin:
            _call(token, "pinChatMessage",
                  {"chat_id": chat, "message_id": mid,
                   "disable_notification": True})
        tgc = cfg.setdefault("telegram", {})
        tgc[id_key] = mid
        tgc[text_key] = text
    return cfg


def status_message(cfg, text):
    """The live status. Edited in place, never pinned - the pin belongs to
    the remote-control links, which are what you actually reach for."""
    tg = cfg.get("telegram", {})
    stale = tg.get("pinned_message_id")
    if stale:
        # an older bridge pinned the status here; free the pin once
        _call(tg.get("token"), "unpinChatMessage",
              {"chat_id": tg.get("chat_id"), "message_id": stale})
        tg["status_message_id"] = stale
        tg["status_text"] = tg.pop("pinned_text", "")
        tg.pop("pinned_message_id", None)
    return _upsert(cfg, "status", text)


def pin_links(cfg, text, force=False):
    """The pinned message: how to reach the sessions from the phone.

    force sends it again as a new message and pins that, instead of editing
    the one already there. For when the pin has been lost - an edit is
    silent by design, so there is otherwise no way to get the list back in
    front of you.
    """
    return _upsert(cfg, "links", text, pin=True, force=force)


def pin_status(cfg, text):      # kept for older callers
    return status_message(cfg, text)


def bar(pct, width=10):
    pct = max(0, min(100, int(pct or 0)))
    filled = round(pct * width / 100)
    return "#" * filled + "." * (width - filled)
