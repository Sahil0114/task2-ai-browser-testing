"""Turn a plain-English test case into a real, verified browser run.

The engine loops:
    inspect page -> ask model for ONE action -> run it -> inspect again

and, at the end, asks the model to describe the page conditions that would
prove the test passed, then checks those conditions against the LIVE page with
Playwright. The model never decides the final PASS/FAIL on its own.
"""

import json
import os
import uuid

from dotenv import load_dotenv
from openai import OpenAI

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"

MAX_STEPS_DEFAULT = 10

# Elements the agent is allowed to interact with. No site-specific selectors.
INTERACTIVE_SELECTOR = (
    'a[href], button, input, select, textarea, '
    '[role="button"], [role="link"], [role="menuitem"], summary'
)

# JavaScript run inside the page to collect a compact snapshot. Every visible
# interactive element gets a temporary `data-ai-ref` attribute so Playwright can
# find it again by number. %s is a JSON-encoded selector list, %d a text cap.
_SNAPSHOT_JS = """
() => {
  const isVisible = (el) => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const nameOf = (el) => {
    const t = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
    return (el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.getAttribute('alt')
      || t).slice(0, 120);
  };
  document.querySelectorAll('[data-ai-ref]').forEach(e => e.removeAttribute('data-ai-ref'));
  const els = Array.from(document.querySelectorAll(%s));
  const elements = [];
  els.forEach((el) => {
    if (!isVisible(el)) return;
    const ref = elements.length;
    const tag = el.tagName.toLowerCase();
    const info = { ref: ref, tag: tag, name: nameOf(el), type: el.getAttribute('type') || '' };
    if (tag === 'select') {
      info.options = Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean).slice(0, 30);
    }
    el.setAttribute('data-ai-ref', String(ref));
    elements.push(info);
  });
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, %d),
    elements: elements,
  };
}
"""

ACTOR_SYSTEM = (
    "You are a web-browser test executor. You are given a TEST CASE in plain "
    "English and a SNAPSHOT of the current page (URL, title, visible text, and a "
    "numbered list of interactive elements like @0, @1, ...).\n\n"
    "Choose ONE action to make progress on the test case. Reply with ONLY a JSON "
    "object, exactly one of:\n"
    '{"action": "click", "ref": 3}\n'
    '{"action": "type", "ref": 2, "text": "text to enter"}\n'
    '{"action": "press", "key": "Enter"}\n'
    '{"action": "select", "ref": 4, "value": "English"}\n'
    '{"action": "goto", "url": "https://example.com"}\n'
    '{"action": "done"}\n\n'
    "Rules:\n"
    "- Only use refs that appear in the CURRENT snapshot. Never invent one.\n"
    "- Use \"type\" to fill username/password/search fields with the value the "
    "test case (or the page text itself) tells you to use.\n"
    "- Click the element whose name/text clearly matches what you need to do next.\n"
    "- Return \"done\" as soon as you have carried out the test case as far as the "
    "page allows (for example, you submitted the form), or if you cannot proceed.\n"
    "- Return ONLY the JSON object and nothing else."
)

JUDGE_SYSTEM = (
    "You are a careful test verifier. You are given a TEST CASE and the FINAL "
    "state of the page after the test ran.\n\n"
    "Describe the page conditions that would PROVE the test case passed, as a "
    "machine-checkable list. You do NOT decide pass/fail yourself; another "
    "program checks your conditions against the real page.\n\n"
    "Reply with ONLY a JSON object of this shape:\n"
    '{"conditions": [{"type": "...", "value": "..."}], "reason": "..."}\n\n'
    "Condition types:\n"
    '{"type": "text_present", "value": "..."}   -> visible page text CONTAINS this (case-insensitive)\n'
    '{"type": "text_absent", "value": "..."}    -> visible page text does NOT contain this\n'
    '{"type": "url_contains", "value": "..."}   -> the page URL contains this\n'
    '{"type": "url_equals", "value": "..."}     -> the page URL equals this exactly\n'
    '{"type": "title_contains", "value": "..."} -> the page title contains this\n'
    '{"type": "title_equals", "value": "..."}   -> the page title equals this (case-insensitive)\n\n'
    "Rules:\n"
    "1. Every condition must be DIRECTLY about the outcome the TEST CASE asks "
    "for, and must be text or a URL you can actually see in the final page "
    "state. Quote the exact wording that appears on the page.\n"
    "2. Do NOT add conditions that are merely true but unrelated to the test "
    "case (for example, do not check for the login page's title when the test "
    "case is about whether the login SUCCEEDED).\n"
    "3. Always describe the conditions for a PASS. If the expected outcome is "
    "absent, still list the conditions that would have proved it; the verifier "
    "will detect they are absent.\n"
    "4. If the outcome genuinely cannot be expressed as checkable conditions, "
    'return {"conditions": [], "reason": "cannot express outcome as checkable conditions"}.'
)

RELEVANCE_SYSTEM = (
    "You decide whether a set of page conditions, if all true, actually "
    "demonstrates that a test case's expected outcome happened.\n\n"
    'Reply with ONLY a JSON object: {"relevant": true/false, "note": "..."}\n\n'
    '"relevant" is true if, assuming every condition is true, the outcome the '
    "TEST CASE asked for must have happened. Extra supporting details of the "
    "same outcome are fine and must NOT make it false (for example, a success "
    "flash message after a login is good supporting evidence).\n"
    '"relevant" is false ONLY if the conditions could all be true while the '
    "test case outcome did NOT happen (for example, the conditions describe the "
    "starting login page when the test case is about a SUCCESSFUL login).\n"
    'Explain briefly in "note".'
)


def load_config():
    """Read config from the environment (loaded from .env). Never prints values."""
    load_dotenv()
    api_key = os.getenv("OPENCODE_API_KEY")
    base_url = os.getenv("OPENCODE_BASE_URL")
    model = os.getenv("OPENCODE_MODEL", "glm-5.3-flash")
    if not api_key or not base_url:
        raise RuntimeError("OPENCODE_API_KEY and OPENCODE_BASE_URL must be set in .env")
    return {"api_key": api_key, "base_url": base_url, "model": model}


def build_client(config):
    """Create an OpenAI-compatible client pointed at the OpenCode Go endpoint.

    A custom User-Agent and a stable x-opencode-session header are sent because
    the OpenCode Go endpoint asks non-OpenCode clients to identify themselves.
    """
    session_id = uuid.uuid4().hex
    return OpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"],
        default_headers={
            "User-Agent": "cloudqa-browser-testing/1.0",
            "x-opencode-session": session_id,
        },
    )


def _parse_json(text):
    """Parse JSON out of a model reply, tolerating markdown fences/prose."""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def call_model(client, model, system, user, retries=3):
    """Call the model and return parsed JSON, retrying on bad output."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err = "no response"
    for _ in range(retries):
        for json_mode in (True, False):
            kwargs = {"model": model, "messages": messages, "temperature": 0}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content
            except Exception as exc:  # noqa: BLE001 - surface any provider error
                last_err = str(exc)
                continue
            data = _parse_json(content)
            if data is not None:
                return data
            last_err = "response was not valid JSON"
    raise RuntimeError(f"model call failed: {last_err}")


def snapshot(page, text_limit=6000):
    """Return {url, title, text, elements} for the current page."""
    js = _SNAPSHOT_JS % (json.dumps(INTERACTIVE_SELECTOR), text_limit)
    return page.evaluate(js)


def _current_state(page):
    """Authoritative page state (full text) used for verification."""
    return {
        "url": page.url,
        "title": page.title(),
        "text": page.evaluate("document.body ? document.body.innerText : ''"),
    }


def _elements_text(elements):
    lines = []
    for el in elements:
        extra = ""
        if el.get("type"):
            extra += f' type="{el["type"]}"'
        if el.get("options"):
            extra += " options=[" + ", ".join(el["options"]) + "]"
        lines.append(f'@{el["ref"]} <{el["tag"]}{extra}> "{el.get("name") or ""}"')
    return "\n".join(lines) if lines else "(no interactive elements)"


def _actor_user(test, step, max_steps, snap, history, note=""):
    parts = [f"TEST CASE: {test}", "", f"STEP {step} / {max_steps}"]
    if history:
        parts += ["", "ACTIONS SO FAR:"]
        parts += [f"  {h}" for h in history[-6:]]
    if note:
        parts += ["", f"NOTE: {note}"]
    parts += [
        "",
        "CURRENT PAGE:",
        f"URL: {snap['url']}",
        f"TITLE: {snap['title']}",
        "",
        "VISIBLE TEXT (may be truncated):",
        snap.get("text") or "(empty)",
        "",
        "INTERACTIVE ELEMENTS:",
        _elements_text(snap.get("elements") or []),
        "",
        "Reply with exactly one JSON action.",
    ]
    return "\n".join(parts)


def _judge_user(test, state, text_limit=15000):
    text = (state.get("text") or "").strip()
    if len(text) > text_limit:
        text = text[:text_limit] + "\n...[truncated]"
    return (
        f"TEST CASE: {test}\n\n"
        "FINAL PAGE STATE:\n"
        f"URL: {state['url']}\n"
        f"TITLE: {state['title']}\n\n"
        "VISIBLE TEXT:\n" + (text or "(empty)")
    )


def _relevance_user(test, conditions):
    return f"TEST CASE: {test}\n\nCONDITIONS: {json.dumps(conditions, ensure_ascii=False)}"


def _resolve_ref(snap, action):
    ref = action.get("ref")
    try:
        ref = int(ref)
    except (TypeError, ValueError):
        return None
    if not any(el["ref"] == ref for el in snap["elements"]):
        return None
    return ref


def run_action(page, action, snap):
    """Run one validated action. Returns (ok: bool, message: str)."""
    kind = action.get("action")
    try:
        if kind == "click":
            ref = _resolve_ref(snap, action)
            if ref is None:
                return False, "invalid or missing ref"
            page.locator(f'[data-ai-ref="{ref}"]').click(timeout=5000)
            return True, f"clicked @{ref}"

        if kind == "type":
            ref = _resolve_ref(snap, action)
            text = action.get("text")
            if ref is None:
                return False, "invalid or missing ref"
            if not isinstance(text, str) or not text:
                return False, "missing text"
            page.locator(f'[data-ai-ref="{ref}"]').fill(text, timeout=5000)
            return True, f"typed into @{ref}"

        if kind == "press":
            key = action.get("key")
            if not isinstance(key, str):
                return False, "missing key"
            page.keyboard.press(key)
            return True, f"pressed {key}"

        if kind == "select":
            ref = _resolve_ref(snap, action)
            value = action.get("value")
            if ref is None or not isinstance(value, str):
                return False, "invalid ref or missing value"
            loc = page.locator(f'[data-ai-ref="{ref}"]')
            try:
                loc.select_option(value=value, timeout=5000)
            except Exception:
                loc.select_option(label=value, timeout=5000)
            return True, f"selected {value} in @{ref}"

        if kind == "goto":
            url = action.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                return False, "missing or invalid url"
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return True, f"navigated to {url}"

        return False, f"unknown action {kind!r}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{kind} failed: {exc}"


def _describe(action):
    """Human summary of an action, with typed text hidden (no password leaks)."""
    kind = action.get("action")
    if kind == "type":
        return f"type @{action.get('ref')} (text hidden)"
    if kind in ("click", "select"):
        return f"{kind} @{action.get('ref')}"
    if kind == "goto":
        return f"goto {action.get('url')}"
    if kind == "press":
        return f"press {action.get('key')}"
    return str(action)


def _check_condition(state, cond):
    """Evaluate one verification condition against the live page state."""
    ctype = cond.get("type")
    value = cond.get("value", "")
    if not isinstance(value, str):
        value = str(value)
    text = (state.get("text") or "").lower()
    val = value.lower()
    if ctype == "text_present":
        return val in text
    if ctype == "text_absent":
        return val not in text
    if ctype == "url_contains":
        return val in state.get("url", "")
    if ctype == "url_equals":
        return state.get("url", "") == value
    if ctype == "title_contains":
        return val in (state.get("title") or "").lower()
    if ctype == "title_equals":
        return (state.get("title") or "").lower() == val
    return False


def _settle(page):
    """Wait for the page to finish loading using Playwright's built-in waits."""
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except Exception:
        pass


def run_test(test, url, client, model, headless=True, max_steps=MAX_STEPS_DEFAULT):
    """Run one test case and return {status, steps, reason, conditions, verified}."""
    from playwright.sync_api import sync_playwright

    result = {"status": ERROR, "steps": 0, "reason": "", "conditions": [], "verified": []}
    history = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_context().new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _settle(page)

            consecutive_errors = 0
            note = ""
            for step in range(1, max_steps + 1):
                snap = snapshot(page)
                action = call_model(
                    client, model, ACTOR_SYSTEM,
                    _actor_user(test, step, max_steps, snap, history, note),
                )
                result["steps"] = step

                if not isinstance(action, dict) or "action" not in action:
                    consecutive_errors += 1
                    note = "Reply with exactly one JSON action and nothing else."
                    history.append(f"step {step}: (invalid model response)")
                    if consecutive_errors >= 2:
                        result["reason"] = "model repeatedly returned invalid responses"
                        return result
                    continue

                kind = action["action"]
                if kind == "done":
                    history.append(f"step {step}: done")
                    break

                ok, msg = run_action(page, action, snap)
                history.append(f"step {step}: {_describe(action)} -> {msg}")
                if ok:
                    consecutive_errors = 0
                    note = ""
                    _settle(page)
                else:
                    consecutive_errors += 1
                    note = f"Your last action failed: {msg}. Re-inspect the page and try again."
                    _settle(page)
                    if consecutive_errors >= 2:
                        result["reason"] = f"action failed twice: {msg}"
                        return result

            # Verify against the real, current browser state (not the model).
            _settle(page)
            state = _current_state(page)
            judge = call_model(client, model, JUDGE_SYSTEM, _judge_user(test, state))
            conditions = judge.get("conditions", []) if isinstance(judge, dict) else []
            conditions = [c for c in conditions if isinstance(c, dict)]
            result["conditions"] = conditions

            if not conditions:
                result["status"] = FAIL
                result["reason"] = (judge.get("reason") if isinstance(judge, dict)
                                    else "") or "no checkable outcome could be derived"
                return result

            checks = [(c, _check_condition(state, c)) for c in conditions]
            result["verified"] = [
                {"type": c.get("type"), "value": c.get("value"), "found": ok} for c, ok in checks
            ]
            all_true = all(ok for _, ok in checks)

            rel = call_model(client, model, RELEVANCE_SYSTEM,
                             _relevance_user(test, conditions))
            relevant = bool(rel.get("relevant")) if isinstance(rel, dict) else False
            rel_note = rel.get("note", "") if isinstance(rel, dict) else ""

            if all_true and relevant:
                result["status"] = PASS
                result["reason"] = (judge.get("reason") if isinstance(judge, dict)
                                    else "") or "all conditions verified on the page"
            elif not relevant:
                result["status"] = FAIL
                result["reason"] = "proposed evidence did not match the test case: " + rel_note
            else:
                failed = [c for c, ok in checks if not ok]
                result["status"] = FAIL
                result["reason"] = "expected conditions not met: " + json.dumps(failed, ensure_ascii=False)
            return result
        finally:
            browser.close()
