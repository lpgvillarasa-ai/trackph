# AGENTS.md - How AI agents work in this repo

Shared rules for every AI agent here. Codex reads this file directly; Claude Code reads it
through CLAUDE.md. Project facts live in README.md; read it first.

## Owner

Lee (lpgvillarasa-ai) runs this repo. Lee is non-technical: explain every change and finding
in plain, everyday language. Lee makes every final call.

## Non-negotiable rules

1. **Work through pull requests.** Never commit straight to the default branch. Build on a
   branch named for the task, then open a PR with a plain-language description: what changed,
   why, how to check it, and anything risky.
2. **No secrets in code.** Never commit API keys, service-role keys, tokens, or passwords.
   Public/publishable keys that are designed to ship to the browser are fine.
3. **Protect people's data.** Never log, print, or send personal data (names, emails, phone
   numbers, addresses, health, payroll, or payment details) to consoles, analytics, or third
   parties. Never commit real customer data; seed and test data stays fake.
4. **Access control lives on the server or database,** never only in the UI. If the project
   uses Supabase, every table keeps row-level security and schema changes go in a new
   migration file (never edit an old one).
5. **Small, focused changes.** Stay inside the task's scope. No drive-by refactors or
   dependency upgrades unless asked.
6. **Prove it works.** Run the project's build (and tests, if any) before saying a task is done.

## Review guidelines

When reviewing a pull request, flag as **P0/P1** only things that matter:

- A secret or real personal data in code, config, or seed files
- Data readable or editable by people who should not have access
- Anything that breaks the build, deploy, or a live page
- Personal data written to logs, errors, or third-party calls
- Logic that silently loses or corrupts data

Ignore pure style preferences. If nothing serious is found, say so in one line.

## The two agents

- **Claude Code** (`@claude`) and **Codex** (`@codex`) both work here. Either can build,
  review, plan, or manage. Lee decides who leads each task, usually based on who has capacity
  that day. Codex auto-reviews every new PR.

**Builder mode** (a comment gives you a task): implement it on the same PR branch, follow
every rule above, keep to the stated scope, and reply with a short plain-language summary of
what changed, anything left undone, and any open question.

**Manager mode** (Lee asks you to plan and hand the building to the other agent): post the
plan as a PR comment, then delegate each piece in its own comment starting with the other
agent's handle, stating the goal, files in scope, and what "done" looks like. When the builder
reports back, review its work against this file and either approve or reply with specific
fixes. You stay accountable for the final result.

**Loop mode** (Lee says "loop until it's right" or similar): builder pushes → reviewer
reviews → builder fixes the P0/P1 findings and asks for a re-review (`@codex review` or
`@claude review`) → repeat until the reviewer reports no P0/P1 findings. Then tell Lee it is
ready with a plain-language summary of what was built and what was fixed along the way.

**Asking the other agent:** if you are stuck, unsure about a design decision, or want a second
opinion, ask the other agent in a PR comment with one clear question instead of guessing.

**Avoid ping-pong:** mention the other agent only when you need something from it. Never
mention it just to say thanks or "done". After **3 rounds** back and forth on the same issue,
or **5 loop rounds** on one PR, stop and ask Lee to decide. If you disagree with the other
agent, say so with your reason instead of silently reverting its work.
