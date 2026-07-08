# Browser Verification

Use this reference for user-visible bugs and any workflow that requires interactive confirmation.

## When Required

Browser verification is required for:

- layout or style bugs
- table scroll/fixed-column behavior
- modals, drawers, forms, tabs, and menus
- upload, preview, rich text, media, and editor behavior
- routing and permission-related page access
- any bug whose evidence is a screenshot or recording

Skip only when the user explicitly says browser verification is unnecessary or no local/browser route can be made available.

## Browser Access

Use the available browser automation mechanism in the current Codex environment. For local apps, prefer the in-app browser or a configured browser automation skill/tool.

Open the configured `browser_verification.app_url`, then navigate to the issue route or reproduction path.

## Dev Server

If the app needs a server:

1. Check whether the configured port is already serving the app.
2. Start the configured dev command when needed.
3. Use another port only if the configured port is occupied by an unrelated process.
4. Keep the server running until verification is complete.

## Login Policy

Follow `login_policy.method_priority` from project config:

1. `existing_browser_session`: reuse the user's logged-in browser session.
2. `manual_user_login`: open the login page and let the user type credentials or scan a QR code.
3. `env_test_account`: read username/password from configured environment variables.
4. `mock_token`: use a project-approved local mock token or auth bypass.

Rules:

- Never ask the user to paste a password into chat.
- Never write credentials into the repository or skill files.
- If manual login is needed, pause with a short instruction and continue after the user completes login.
- If login cannot be completed, mark verification as blocked rather than pretending it passed.

## Evidence

For each browser verification, record:

- URL/route
- key actions performed
- expected result
- observed result
- screenshot path when captured

Use screenshots for visual regressions, layout bugs, and ambiguous UI states.

## Failure Handling

If the fix fails browser verification:

- Do not move the issue to resolved-for-acceptance, completed, or terminated.
- Continue debugging if the cause is clear and scoped.
- Otherwise leave the issue in progress and comment with the failed verification detail when comments are allowed.
