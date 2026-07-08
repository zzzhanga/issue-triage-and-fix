# Requirement Repository Mapping

Use this reference after fetching issue details and before ownership triage.

## Goal

Determine whether the current local repository is responsible for an issue by mapping the issue's linked requirement or demand to one or more code repositories.

Do this before deciding whether a bug is frontend-owned, backend-owned, or not-current-repo. A single requirement can legitimately involve multiple repositories.

## Inputs

- Normalized issue JSON, especially `requirements`.
- `requirement_mapping` from the repo-scoped project config.
- Current repository path, name, aliases, package name, and configured repo key.
- Issue title, description, screenshots, routes, and affected product surface.

## Matching Order

1. Exact requirement id or URL match.
2. Exact requirement title match.
3. Requirement title contains configured aliases or demand keywords.
4. Issue title/description contains configured product, module, route, or repo aliases.
5. Code search confirms the affected page/component/API exists in the current repo.
6. User explicitly selected the current repo for this issue.

Do not rely on only one weak text match when the requirement maps to multiple repositories.

## Match Results

Classify exactly one:

- `current-repo`: The current repo is a confident owner or one of the owners with clear issue evidence.
- `multi-repo-unclear`: The requirement maps to multiple repos and the bug evidence does not clearly identify which repo owns it.
- `other-repo`: Another configured repo is a clearer owner.
- `unmatched`: No configured requirement or repo rule matches.
- `low-confidence`: Some weak evidence exists, but not enough to proceed safely.

## Confidence

Use these signals:

- High confidence: exact requirement id/title plus issue evidence naming the current app/module/route/component.
- Medium confidence: requirement title maps to current repo and code search finds a plausible affected component.
- Low confidence: only generic words such as "page", "backend", "image", "display", or a shared requirement name match.

## Confirmation Policy

Ask customer/product/test confirmation when:

- no requirement is linked,
- the requirement maps to multiple repos and evidence is unclear,
- the current repo is not listed but code search found a possible match,
- changing the current repo would not fully fix the user-visible bug,
- the expected behavior is unclear.

Prepare a concise confirmation question with:

- bug id/title,
- linked requirement title/id,
- candidate repositories,
- current best guess,
- the exact missing decision.

Example:

```text
BUG-28814 belongs to requirement "shared publishing workflow".
It may involve both the web repository and the mobile repository. The screenshot/title points to image display in the web console, but the affected client is not explicit.
Please confirm whether this should be fixed in the web repo, the mobile repo, or both.
```

## Output

Attach this result to triage:

```json
{
  "repository_match": "multi-repo-unclear",
  "confidence": "medium",
  "matched_requirement": {
    "id": "REQ-1",
    "title": "Requirement title",
    "url": "https://project.example/..."
  },
  "candidate_repositories": [
    {
      "repo_key": "admin",
      "path": ".",
      "reason": "Requirement maps to admin and issue title mentions backend."
    }
  ],
  "confirmation_required": true,
  "customer_confirmation_question": "..."
}
```
