<!-- BEGIN:ai-collab-kit -->
<!-- Managed by lpgvillarasa-ai/ai-collab-hub. Edit it there, not here. -->

# CLAUDE.md

@AGENTS.md

AGENTS.md holds the rules both AI agents follow and how they work together. This file adds
the Claude-specific mechanics.

## Pull requests

Create a branch named for the task (e.g. `feat/contact-form`, `fix/mobile-menu`), commit in
small steps, and open the PR with `gh pr create`. Opening the PR is what brings Codex in: it
reviews every new PR automatically.

## Reading Codex's review

```
gh pr view <number> --comments
gh api repos/<owner>/<repo>/pulls/<number>/comments
```

Fix every P0/P1 finding on the same branch, push, reply to the comment with what you changed,
then comment `@codex review` to ask for a re-review. If you disagree, reply with your
reasoning. If Codex has not reviewed yet, say so in your summary instead of waiting forever.

## Delegating to Codex

```
gh pr comment <number> --body "@codex <goal>. Files in scope: <files>. Done means: <criteria>. Follow AGENTS.md."
```

Delegate when Lee asks, or for a well-bounded task that benefits from a second model. Review
what Codex pushes exactly as you would a teammate's work before telling Lee it is ready.

## When Codex is the manager

If you were started by an `@claude` comment from Codex (`chatgpt-codex-connector[bot]`),
treat it as a request from a teammate lead: do exactly the task described, stay inside the
stated scope and the rules in AGENTS.md, push to the same PR branch, and reply with a short
plain-language summary. If the request is unclear or would break a rule, ask one clear
question with `@codex` instead of guessing. Never start work nobody asked for.

## Before saying a task is done

- The build passes (and tests, if the project has them)
- The PR description explains the change in plain language
- Codex's review has been checked and P0/P1 items resolved (or noted as still pending)

<!-- END:ai-collab-kit -->
