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

Use a browser exemption only when the exact approved plan declares `lightweight` verification and the runner's high-confidence/current-repo/ownership/risk gates pass. Record why a reliable browser route is impractical, inspection evidence, and residual risk. Otherwise, if no local/browser route can be made available, mark the browser check `blocked`; do not silently treat it as passed or not required.

## Browser Access

Follow `browser_verification.surface_priority`. Use this default when the project does not configure one:

1. `existing_chrome_tab`
2. `existing_in_app_browser_tab`
3. `new_in_app_browser_tab`

For a local app, first connect to the user's Chrome when Chrome control is available and inspect the open-tab list read-only. Match the configured app URL or another local target using `localhost`, `127.0.0.1`, or `::1`, including its expected route. Reuse the matching tab so its live application state and existing signed-in session remain available. Do not navigate, reload, close, or otherwise disturb unrelated user tabs.

If no matching Chrome tab exists, reuse a matching in-app browser tab. Only then open a new in-app browser tab at the configured URL. If the user explicitly chose Chrome or the in-app browser, honor that choice instead of applying the default order.

Use the selected matching tab for the issue route or reproduction path. Open `browser_verification.app_url` only when no reusable tab exists; navigate a reused tab only within the matched local app and only as required by the verification flow.

## Dev Server

If the app needs a server:

1. Check whether the configured port is already serving the app.
2. Start the configured dev command when needed.
3. Use another port only if the configured port is occupied by an unrelated process.
4. Keep the server running until verification is complete.

## Login Policy

Follow `login_policy.method_priority` from project config:

1. `existing_chrome_session`: reuse the matching Chrome tab and its existing signed-in session.
2. `existing_in_app_browser_session`: reuse an already authenticated in-app browser tab.
3. `manual_user_login`: open the login page and let the user type credentials or scan a QR code.
4. `env_test_account`: read username/password from configured environment variables.
5. `mock_token`: use a project-approved local mock token or auth bypass.

Treat legacy `existing_browser_session` as the two existing-session options above, with Chrome first for local-project verification.

Rules:

- Never ask the user to paste a password into chat.
- Never write credentials into the repository or skill files.
- Never inspect, export, copy, or inject browser cookies, localStorage, passwords, profiles, or session stores. Do not migrate authentication data between Chrome and the in-app browser.
- Prefer verifying directly in the browser that already owns the authenticated session. If a different browser is required, ask the user to sign in there or use a configured test account/project-approved mock path.
- If manual login is needed, pause with a short instruction and continue after the user completes login.
- If login cannot be completed, mark verification as blocked rather than pretending it passed.

## Evidence

For each browser verification, record:

- URL/route
- key actions performed
- expected result
- observed result
- screenshot path when captured
- structured result: `passed`, `failed`, or `blocked`

Use screenshots for visual regressions, layout bugs, and ambiguous UI states.

Browser `not-required` alone is not enough to mark standard verification done. Plan-approved lightweight verification may record `skipped` only with its exemption reason, inspection evidence, high confidence, and residual risk.

## Failure Handling

If the fix fails browser verification:

- Do not move the issue to resolved-for-acceptance, completed, or terminated.
- Continue debugging if the cause is clear and scoped.
- Otherwise leave the issue in progress and comment with the failed verification detail only when `completion_action_authorized(issue, comment)` is true.
