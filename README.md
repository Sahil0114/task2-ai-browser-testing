# Browser Test Runner

A small tool that takes a test case written in plain English, runs it in a real
browser (Playwright), and reports an honest **PASS**, **FAIL**, or **ERROR**.

It is general-purpose: nothing is hard-coded to any website. The model reads the
page, picks one action at a time, and Playwright carries it out — repeatedly,
until the outcome can be checked.

## How it works (in one paragraph)

The tool opens the URL, takes a **snapshot** of the page (URL, title, visible
text, and every interactive element numbered like `@0`, `@1`), and asks the
model to choose **one** action (click, type, press, select, goto). It runs
that action through Playwright, takes a fresh snapshot, and repeats. When the
model says it is done, a separate **verifier** step asks the model to describe
the page conditions that would prove the test passed, and then **Playwright —
not the model — checks those conditions against the live page**.

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install the browser Playwright drives
playwright install chromium

# 3. Configure your endpoint
cp .env.example .env
#    then edit .env and fill in OPENCODE_API_KEY (and model/base URL if needed)
```

`.env` holds your API key and is never opened, printed, or committed (it is in
`.gitignore`).

## Usage

```bash
python main.py "the test case" "https://starting-url"
```

Examples:

```bash
# Real login (credentials are published on the page itself)
python main.py \
  "Log in with username tomsmith and password SuperSecretPassword! and confirm you reach the Secure Area page." \
  "https://the-internet.herokuapp.com/login"

# Visible-page assertion
python main.py \
  "Confirm that the page shows a 'Get your demo access' section." \
  "https://phptravels.com/demo"
```

Options: `--headed` shows the browser window, `--max-steps N` changes the action
budget (default 10).

Exit codes: `0` = PASS, `1` = FAIL, `2` = ERROR.

## Important notes

- **Credentials:** the text of the test case is sent to the model so it can act
  on it. Only put *demo* credentials in a test case, never real secrets. Your
  `.env` API key is never sent to the model or printed anywhere.
- **CAPTCHA / anti-bot:** the tool does not attempt to bypass these. If a page
  is behind a CAPTCHA, the test will honestly fail or error rather than cheat.
- **No hard-coded selectors:** elements are found by their on-page name/label,
  not by site-specific CSS.

## Known limitations

- Elements inside `<iframe>`s are not visible to the snapshot.
- A CAPTCHA cannot be passed (by design).
- The final relevance check uses the model, so there is a small chance of a
  misjudgment; the truth of every piece of evidence is still checked by
  Playwright, not the model.
- Uses the OpenCode Go endpoint, which is intended for coding-agent traffic.
