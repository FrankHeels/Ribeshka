---
name: tdd-orchestrator
description: Orchestrate complex Django coding work with repository-configured planning, test-first delivery, implementation, and independent review. Use for Django features, bug fixes, multi-file work, and tasks where tests should define acceptance before implementation. Do not use for trivial edits or simple questions.
---

# TDD-Orchestrator — Django-Orchestration Profile

The user's explicit instructions take precedence over this skill.

## Goal

Use the root agent as the high-quality orchestrator.

Delegate bounded execution work to specialized subagents, then have the root integrate, verify, and present the final result.

## Model and reasoning configuration

Read the actual TOML values in the project root before dispatching agents:

- `.codex/config.toml`: root `model` and `model_reasoning_effort`, plus `[agents]` fallback settings.
- `.codex/agents/<role>.toml`: the named role's `model` and `model_reasoning_effort`; these take precedence over the corresponding fallback settings.

Parsed TOML values are authoritative when comments or this snapshot differ. The current configuration is:

| Role | Model | Reasoning |
|---|---|---|
| root | `gpt-6.1-sol` | `max` |
| explorer | `gpt-6-luna` | `medium` |
| backender | `gpt-6.1-sol` | `xhigh` |
| tester | `gpt-6.1-sol` | `xhigh` |
| reviewer | `gpt-6-astra` | `medium` |
| researcher | `gpt-6-luna` | `medium` |

The generic `[agents]` fallback is `gpt-6-luna` with `xhigh` reasoning. Use it only for settings not supplied by a named role; it does not replace that role's explicit model or effort.

The backender owns implementation; the tester and reviewer keep independent contexts for the test-first and review stages.

---

## Delegation gate

Before doing substantive repository work, classify the task as either:

- root-only
- delegated

Use root-only only when the task is genuinely small, localized, and does not materially benefit from independent exploration, implementation, testing, research, or review.

The task MUST be delegated when at least one of the following is true:

- the task spans multiple files, modules, services, or components
- there are two or more independent workstreams
- repository exploration is needed before implementation
- implementation and verification benefit from separate context
- debugging requires tracing across components
- multiple modules or services need inspection
- external or version-specific facts need verification
- an independent post-change review is materially useful
- the user explicitly asks for delegation, parallelism, agents, or subagents

When a task qualifies for delegation, the root MUST call `spawn_agent` before performing the delegated work itself.

Do not merely describe, simulate, or internally reason about delegation.

Actual subagents must be spawned.

If `spawn_agent` is unavailable or fails, explicitly report that failure.

Do not silently fall back to doing required delegated work in the root thread.

For every delegated task, spawn at least one subagent.

Do not create subagents solely to satisfy this rule when the task is genuinely root-only.

---

## Root-agent responsibilities

The root agent owns:

1. understanding the user's actual goal
2. choosing the architecture and implementation direction
3. decomposing the task
4. deciding which tasks can run in parallel
5. spawning the appropriate subagents
6. giving each subagent a bounded contract
7. resolving conflicting subagent findings
8. integrating changes
9. reviewing the final diff
10. running or coordinating final verification
11. presenting the final result to the user

Subagents provide evidence and bounded execution.

They do not own the overall direction.

The root must not offload architectural ownership to a subagent.

---

## Spawn policy

Use the role configuration resolved above for every spawn. Select the matching named `agent_type` when the tool applies its configured model and reasoning. When explicit overrides are needed, supply both resolved values using the tool's supported independent-context mode.

Re-read the relevant configuration after the user changes it. Preserve the running root session's model and reasoning; `.codex/config.toml` defines the settings for root launches.

For every delegated task:

1. call `spawn_agent`
2. give the agent a descriptive task name using underscores
3. select the configured role and pin its resolved model and reasoning effort
4. give the subagent a bounded delegation contract
5. retain the returned task name or identifier
6. wait for required agents before final synthesis

Do not silently substitute the root agent for a required backender or tester.

Do not spawn Astra workers except for the `reviewer` role unless:

- the user explicitly requests Astra
- a subagent reports a genuinely difficult reasoning blocker
- the root determines that a high-risk architectural or security review needs Astra

Use the configured `explorer` and `researcher` for evidence gathering, `tester` for test authoring and verification, and `backender` for implementation.

---

## Delegation contract

Every delegated task should include:

- Objective: one concrete outcome
- Scope: exact files, module, subsystem, or question when known
- Context: only the information needed to succeed
- Constraints: what must not change
- Deliverable: what the subagent must return or implement
- Acceptance criteria: how success will be checked

Prefer narrow tasks that can finish independently.

Bad:

> Fix the backend.

Good:

> Trace where POST /invoices validates currency. Return the responsible files, validation path, and existing tests. Do not edit files.

For implementation tasks, explicitly state file ownership when possible.

For exploration tasks, tell the agent not to edit files.

For review tasks, tell the agent to report findings rather than silently modify unrelated code.

---

## Role selection

Use `explorer` for:

- repository mapping
- tracing execution or data flow
- locating symbols and tests
- dependency inspection
- configuration inspection
- identifying implementation boundaries

Use `backender` for:

- bounded implementation
- small refactors with explicit scope
- targeted fixes
- adding requested code
- modifying clearly owned files

Use `tester` for:

- writing and owning the initial automated tests from the root's scenarios
- running the tests before implementation and reporting the expected red result
- rerunning tests after implementation or approved review fixes
- reporting exact failures, coverage gaps, and test commands

The tester edits test files only during the initial red phase. The root must explicitly authorize any later test changes. The tester never changes production code to make a test pass.

Use `reviewer` for:

- independent post-change review
- correctness checks
- security review
- regression analysis
- missing-test analysis
- architectural consistency checks

Use `researcher` for:

- current API or framework behavior
- dependency or version questions
- primary documentation verification
- external compatibility questions

---

## Parallelism

Run independent tasks in parallel.

When two or more delegated tasks are independent, spawn all of them before waiting for any one of them.

Good parallel set:

1. spawn backend explorer
2. spawn frontend explorer
3. spawn API researcher
4. wait for all three
5. synthesize findings

Do not do this:

1. spawn backend explorer
2. wait
3. spawn frontend explorer
4. wait
5. spawn researcher
6. wait

unless later tasks genuinely depend on earlier results.

Good parallel examples:

- explorer maps backend path
- explorer maps frontend path
- researcher verifies external API behavior

Serialize dependent work in the TDD order described below. Exploration and external research may run before test authoring when they clarify the contract.

Do not send multiple workers to edit the same files unless the root explicitly coordinates ownership.

Prefer one writer per file or subsystem.

---

## TDD coding workflow

For behavioral features and bug fixes, follow this sequence:

1. Root studies the task and the relevant repository behavior. Use explorers or a researcher first when needed.
2. Root writes concrete test scenarios from the request. Each scenario states setup, action, expected result, and important boundary cases. These scenarios are the acceptance contract.
3. Root delegates those scenarios to the configured tester, with explicit test-file ownership and a bounded contract.
4. Tester implements the tests without changing production code, runs them, and reports that they fail for the expected missing behavior. Fix test setup or expectation errors before handing off; a failure caused by environment or broken test infrastructure is not a valid red result. If the tests pass before implementation, root checks whether the behavior is already present and skips unnecessary implementation.
5. After the red result is confirmed, root delegates the implementation and the test contract to the configured backender. The backender implements the smallest change that makes the specified tests pass and runs focused validation.
6. Root spawns the configured reviewer in an independent read-only context to review implementation and tests. Ask for correctness issues, regressions, missing cases, and worthwhile code improvements or refactoring; findings must identify the reason and a concrete change.
7. Root evaluates each material finding. Delegate accepted code changes to the backender, then have the tester rerun the affected tests. Changes to the test contract remain root-owned and must be explicitly delegated to the tester.
8. Root runs or confirms final verification, checks the final diff, and reports the red/green evidence, review findings resolved, and any remaining limits.

Do not start implementation before the tester reports a valid red result, except for non-behavioral work that has no meaningful automated behavior to specify. For documentation, configuration, or pure refactoring tasks, root defines the applicable checks first and uses existing tests to guard behavior where relevant.

The reviewer proposes improvements; the root decides which to accept, and the backender applies accepted implementation changes. This keeps review independent and preserves one clear owner for each file.

---

## Debugging workflow

For cross-component bugs:

1. spawn explorers for independent suspected areas
2. reproduce the issue when possible
3. collect evidence before selecting a fix
4. root determines the likely root cause
5. root writes a regression scenario for the original failure
6. assign the configured tester to add the regression test and confirm the expected red result
7. assign the configured backender to fix the bug
8. assign the tester to confirm green and run focused regression checks
9. use the configured reviewer for independent review and improvement suggestions

Do not let multiple workers independently attempt competing fixes unless the root intentionally requests alternative approaches.

---

## Research workflow

When current or version-specific external information matters:

1. spawn the configured researcher
2. require primary or authoritative sources when possible
3. return concise findings and compatibility implications
4. let the root decide how those findings affect implementation

Do not mix speculative external claims into implementation decisions without verification.

---

## Cost and context discipline

Choose the appropriate role for each bounded task and preserve its configured model and reasoning effort.

Keep the root context focused on:

- architectural decisions
- summarized evidence
- important diffs
- test results
- reviewer findings
- unresolved risks

Do not paste large raw logs or entire files back into the root when a concise evidence summary is enough.

Subagents should return:

- conclusions
- relevant file paths
- important line or symbol references
- commands run
- test results
- risks or blockers

Avoid returning large amounts of irrelevant raw output.

---

## Escalation behavior

A subagent should report back instead of expanding scope when it encounters:

- an architectural decision
- a breaking API or schema change
- a new dependency
- a security-sensitive design choice
- unclear requirements with materially different outcomes
- unexpected changes outside its assigned scope
- changes that affect another worker's ownership
- a blocker that requires substantially broader reasoning

The root decides what to do next.

A subagent reports a reasoning blocker to the root and preserves its assigned model and effort until the root decides how to proceed.

The root owns model escalation decisions.

---

## Failure handling

If a subagent fails:

1. inspect the failure reason
2. decide whether the task should be retried, narrowed, reassigned, or handled by the root
3. do not silently ignore the failed delegation
4. do not claim the delegated work completed successfully

If `spawn_agent` itself fails, explicitly note the failure.

If a required worker fails repeatedly, the root may continue directly when reasonable, but should record that the fallback occurred.

---

## Delegated-task completion gate

Before producing the final answer for a delegated task, confirm that:

- every required subagent was actually spawned
- every required subagent either completed or explicitly failed
- material findings were integrated
- conflicting findings were resolved
- required verification was performed
- no required agent is still running

Do not finish while required subagents are still running.

Do not claim delegation occurred unless `spawn_agent` was actually called successfully.

---

## Final verification

Before claiming completion, the root should:

1. inspect the final diff
2. confirm the requested behavior is actually implemented
3. check material reviewer findings
4. run or confirm the highest-value tests
5. verify that delegated results were integrated correctly
6. state any validation that could not be performed

For implementation tasks, prefer checking:

- syntax or type checks
- targeted unit tests
- integration tests where relevant
- build success where relevant
- the original reproduction path
- final diff for unintended changes

---

## User-facing behavior

Do not narrate every subagent action unless the user asks for detailed orchestration visibility.

The final answer should focus on:

- what changed
- what was verified
- important findings
- remaining risks or limitations

When useful, briefly mention which agents contributed.

If the user explicitly asks to see delegation, report:

- subagent name
- model
- assigned task
- completion status

Report a model or reasoning effort only when supported by a successful spawn trace and the resolved role configuration.
