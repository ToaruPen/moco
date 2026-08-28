# Danger Full Access No Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a locally selected moco Agent profile that runs Codex with no sandbox and no approval prompts, then activate it for the current macOS service.

**Architecture:** Add `danger_full_access_no_approval` as a fourth explicit profile. Realtime and delegated Agent thread creation pin `sandbox=danger-full-access` and `approvalPolicy=never`, while schema discovery proves the exact pair is supported. `inherit_codex` continues rejecting the same pair when it arrives implicitly from global Codex configuration.

**Tech Stack:** Python 3.13, Pydantic settings, Codex app-server JSON-RPC, pytest, uv, just, launchd.

**Specification:** `docs/superpowers/specs/2026-08-28-danger-full-access-no-approval-design.md`

**Official contract:** [OpenAI Docs — Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) describes `danger-full-access` with `never` as network-capable operation without sandbox or approval prompts.

**Commit policy:** Do not commit or merge unless the user explicitly authorizes it after implementation.

---

## File ownership

- `src/moco/config.py`: defines accepted local profile names.
- `src/moco/codex/schema.py`: proves the app-server accepts every policy pair moco may send.
- `src/moco/codex/session.py`: maps realtime thread creation.
- `src/moco/codex/agent.py`: maps delegated Agent thread creation.
- `src/moco/codex/capabilities.py`: retains inherited-policy rejection; production edits are not expected.
- `src/moco/doctor.py`: reports the selected profile and projects admission; production edits are not expected.
- `config/moco.example.yaml` and `README.md`: document the explicit high-risk choice and rollback.
- `tests/test_config.py`, `tests/test_codex_schema.py`, `tests/test_codex_session.py`, `tests/test_codex_agent.py`, `tests/test_doctor.py`, `tests/test_repository_contract.py`: regression coverage.
- `~/Library/Application Support/moco/moco.yaml`: selects the profile on this Mac without environment overrides.

### Task 1: Accept the dedicated high-risk profile

**Files:**
- Modify: `tests/test_config.py:342-391`
- Modify: `src/moco/config.py:220-229`
- Modify: `config/moco.example.yaml:26-33`

- [ ] **Step 1: Change the exact-mode and supported-mode tests first**

```python
def test_agent_profile_modes_are_exactly_four() -> None:
    assert [mode.value for mode in AgentProfileMode] == [
        "read_only",
        "workspace_write",
        "danger_full_access_no_approval",
        "inherit_codex",
    ]


@pytest.mark.parametrize(
    "profile",
    [
        "read_only",
        "workspace_write",
        "danger_full_access_no_approval",
        "inherit_codex",
    ],
)
def test_agent_profile_accepts_supported_modes(tmp_path: Path, profile: str) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text(f"agent:\n  profile: {profile}\n", encoding="utf-8")

    assert load_config(path).agent.profile == profile
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_config.py -k 'agent_profile_modes_are_exactly_four or agent_profile_accepts_supported_modes' -q
```

Expected: FAIL because `danger_full_access_no_approval` is not a valid `AgentProfileMode`.

- [ ] **Step 3: Add the enum member**

```python
class AgentProfileMode(StrEnum):
    """The Agent capability profile moco requests, owned by local configuration."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGER_FULL_ACCESS_NO_APPROVAL = "danger_full_access_no_approval"
    INHERIT_CODEX = "inherit_codex"
```

- [ ] **Step 4: Document the mode without changing the default**

```yaml
  #   danger_full_access_no_approval: disable Codex sandbox and approval prompts.
```

Keep `profile: read_only` in the example.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
uv run pytest tests/test_config.py -k 'agent_profile' -q
```

Expected: all selected tests PASS, including unknown-mode and unknown-key rejection.

### Task 2: Require app-server support for the exact dangerous pair

**Files:**
- Modify: `tests/test_codex_schema.py:1488-1536`
- Modify: `src/moco/codex/schema.py:1726-1733`

- [ ] **Step 1: Add a schema test that admits only inherited and existing explicit pairs**

```python
def test_thread_start_requires_danger_full_access_without_approval_pair(
    tmp_path: Path,
) -> None:
    variant = thread_start_variant()
    params_schema = variant_params_schema(variant)
    params_schema["anyOf"] = [
        {
            "type": "object",
            "properties": {
                "sandbox": {"type": "string", "const": "read-only"},
                "approvalPolicy": {"type": "string", "const": "never"},
            },
        },
        {
            "type": "object",
            "properties": {
                "sandbox": {"type": "string", "const": "workspace-write"},
                "approvalPolicy": {"type": "string", "const": "on-request"},
            },
        },
    ]
    write_schema_bundle(tmp_path, client_variants=[variant], server_variants=[])

    contract = load_generated_contract(tmp_path, version="fake")

    assert contract.method(SemanticMethod.THREAD_START) is None
    assert SemanticMethod.THREAD_START in contract.missing_methods
```

The base witness still covers `inherit_codex` because neither policy field is required.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_codex_schema.py::test_thread_start_requires_danger_full_access_without_approval_pair -q
```

Expected: FAIL because the current witness set incorrectly accepts a schema that cannot carry `danger-full-access + never`.

- [ ] **Step 3: Add the exact policy witness**

```python
SemanticMethod.THREAD_START: _InvocationSpec(
    ParamsKind.OBJECT,
    (
        _object_value(_THREAD_START_BASE),
        _explicit_thread_start("read-only", "never"),
        _explicit_thread_start("workspace-write", "on-request"),
        _explicit_thread_start("danger-full-access", "never"),
    ),
),
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
uv run pytest tests/test_codex_schema.py -k 'thread_start' -q
```

Expected: all selected thread-start schema tests PASS.

### Task 3: Send the pair from both thread owners

**Files:**
- Modify: `tests/test_codex_agent.py:982-1019`
- Modify: `tests/test_codex_session.py:651-694`
- Modify: `src/moco/codex/agent.py:667-680`
- Modify: `src/moco/codex/session.py:590-603`

- [ ] **Step 1: Add the delegated Agent case first**

Add this tuple to `test_thread_start_uses_explicit_profile_policy`:

```python
(
    AgentProfileMode.DANGER_FULL_ACCESS_NO_APPROVAL,
    "danger-full-access",
    "never",
),
```

- [ ] **Step 2: Add the realtime case first**

```python
async def test_starts_realtime_thread_with_danger_full_access_no_approval_profile(
    tmp_path: Path,
) -> None:
    rpc = FakeRpc()
    settings = make_settings(tmp_path).model_copy(
        update={
            "agent": AgentSettings(
                profile=AgentProfileMode.DANGER_FULL_ACCESS_NO_APPROVAL,
            ),
        },
    )
    session = make_session(rpc, settings=settings, capabilities=make_snapshot())

    await session.start("offer-sdp")

    assert rpc.requests[0] == (
        "thread/start",
        {
            "ephemeral": True,
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
            "cwd": str(tmp_path),
        },
    )
    await session.close()
```

- [ ] **Step 3: Verify RED**

Run:

```bash
uv run pytest \
  tests/test_codex_agent.py::test_thread_start_uses_explicit_profile_policy \
  tests/test_codex_session.py::test_starts_realtime_thread_with_danger_full_access_no_approval_profile \
  -q
```

Expected: both new cases FAIL because their payloads omit `sandbox` and `approvalPolicy`.

- [ ] **Step 4: Add the delegated Agent mapping**

Insert before the `INHERIT_CODEX` guard:

```python
elif self._profile is AgentProfileMode.DANGER_FULL_ACCESS_NO_APPROVAL:
    params["sandbox"] = "danger-full-access"
    params["approvalPolicy"] = "never"
```

- [ ] **Step 5: Add the realtime mapping**

```python
elif profile is AgentProfileMode.DANGER_FULL_ACCESS_NO_APPROVAL:
    params["sandbox"] = "danger-full-access"
    params["approvalPolicy"] = "never"
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
uv run pytest tests/test_codex_agent.py tests/test_codex_session.py -q
```

Expected: both modules PASS, including unchanged `inherit_codex` omission and unsafe-policy checks.

### Task 4: Prove explicit opt-in does not weaken inherited-policy rejection

**Files:**
- Modify: `tests/test_doctor.py:377-423`
- Verify: `src/moco/codex/capabilities.py:820-850`
- Verify: `src/moco/doctor.py:312-330`

- [ ] **Step 1: Add the dedicated profile to the Doctor matrix**

```python
(
    AgentProfileMode.DANGER_FULL_ACCESS_NO_APPROVAL,
    DoctorCheck("codex_agent_admission", "ok", "allowed"),
),
```

Keep this existing inherited case:

```python
(
    AgentProfileMode.INHERIT_CODEX,
    DoctorCheck("codex_agent_admission", "error", "unsafe_voice_policy"),
),
```

- [ ] **Step 2: Run the focused test**

Run:

```bash
uv run pytest tests/test_doctor.py::test_doctor_projects_unsafe_global_policy_by_selected_profile -q
```

Expected after Task 1: PASS without production changes, proving `profile_agent_admission` distinguishes explicit opt-in from inherited danger.

- [ ] **Step 3: Run the unknown-policy inheritance test**

Run:

```bash
uv run pytest tests/test_doctor.py::test_doctor_rejects_unknown_policy_only_for_inherit_codex -q
```

Expected: PASS; only `inherit_codex` depends on global policy discovery.

### Task 5: Update authoritative behavior and high-risk warnings

**Files:**
- Modify: `tests/test_repository_contract.py:84-96,135-150,278-285`
- Modify: `README.md:68-77,362`
- Modify: `docs/superpowers/specs/2026-08-07-codex-rich-agent-client-design.md:220-245`
- Modify: `docs/superpowers/specs/2026-08-17-profile-aware-agent-admission-design.md:28-40`

- [ ] **Step 1: Tighten repository documentation assertions first**

Add `danger_full_access_no_approval` to both profile vocabulary lists and assert:

```python
assert "sandbox なし" in stage_b
assert "承認なし" in stage_b
assert "全ファイル" in stage_b
assert "ネットワーク" in stage_b
assert "`inherit_codex` だけが global Codex policy を継承します" in stage_b
assert "`read_only` または `workspace_write` へ戻し" in stage_b
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_repository_contract.py -q
```

Expected: FAIL until the README documents the new mode and rollback.

- [ ] **Step 3: Update README**

Document all four modes and include this exact operational meaning:

```text
`danger_full_access_no_approval` は `danger-full-access` と approval policy `never` を明示します。
これは sandbox なし・承認なしで、moco 実行ユーザーがアクセスできる全ファイルとネットワークを利用できます。
```

State that it can expose credentials, delete data, make external writes, and establish persistence. Explain that profile changes remain local-config-only and rollback is `read_only` or `workspace_write` plus service restart.

- [ ] **Step 4: Update earlier authoritative specs**

Change the profile count and explicit mapping tables to include `danger_full_access_no_approval`. Replace blanket statements that all `danger-full-access + never` voice turns are forbidden with the narrower rule: inherited use remains forbidden, while the dedicated local explicit profile is allowed. Link to the approved 2026-08-28 design.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
uv run pytest tests/test_config.py tests/test_doctor.py tests/test_repository_contract.py -q
```

Expected: all three modules PASS.

- [ ] **Step 6: Search repeated policy vocabulary**

Run:

```bash
rg -n "exactly_three|三つに限定|3種類|danger-full-access|danger_full_access_no_approval" \
  src tests config README.md docs/superpowers/specs
```

Expected: no active assertion or current specification claims exactly three modes or unconditionally rejects every explicit `danger-full-access + never` profile.

### Task 6: Activate the profile without environment overrides

**Files:**
- Modify: `~/Library/Application Support/moco/moco.yaml:20`

- [ ] **Step 1: Change only the owner-private profile**

Use `apply_patch` to replace:

```yaml
  profile: workspace_write
```

with:

```yaml
  profile: danger_full_access_no_approval
```

Do not change environment variables, Irodori endpoints, Cloudflare URLs, operator capabilities, or secrets.

- [ ] **Step 2: Validate configuration**

Run:

```bash
uv run moco config validate --path "$HOME/Library/Application Support/moco/moco.yaml"
```

Expected: exit 0 with no secret values.

- [ ] **Step 3: Restart launchd**

Run:

```bash
launchctl kickstart -k "gui/$(id -u)/dev.toarupen.moco"
```

Expected: exit 0.

- [ ] **Step 4: Verify live capability discovery**

Run:

```bash
just doctor
```

Expected: `codex_profile` reports `danger_full_access_no_approval`, `codex_schema` is compatible, `codex_agent_admission` is allowed, and the public operator checks remain healthy.

### Task 7: Security review and complete verification

**Files:**
- Review: all uncommitted changes relative to `HEAD` and the existing local service boundary.

- [ ] **Step 1: Apply the security-review skill**

Fetch the current official OWASP Top 10 edition and record the check date. Trace authenticated iPhone input → operator WebSocket → Realtime delegation → explicit profile → unrestricted Codex command/file/network execution.

Confirm:

- profile cannot be changed by public UI, WebSocket payload, voice, model output, plugin, app, or MCP response;
- `inherit_codex` still rejects `danger-full-access + never`;
- logs and public UI do not disclose credentials or configuration values;
- owner-private config permissions remain unchanged;
- documentation names data destruction, credential access, exfiltration, and persistence risks;
- app/MCP/provider policy remains a separate boundary and is not bypassed.

- [ ] **Step 2: Run the complete repository gate**

Run:

```bash
just check
```

Expected: formatting, lint, typing, dead-code, dependency, AST, Python coverage, frontend, browser, secret scan, and build gates all exit 0.

- [ ] **Step 3: Verify the actual Codex contract**

Run:

```bash
just contract-codex
```

Expected: the installed Codex app-server contract test exits 0 or reports only an explicitly documented environment prerequisite. Do not treat an unrun contract check as success.

- [ ] **Step 4: Inspect final boundaries**

Run:

```bash
git diff --check
git status --short
rg -n '^\s*profile\s*:' "$HOME/Library/Application Support/moco/moco.yaml"
launchctl print "gui/$(id -u)/dev.toarupen.moco"
```

Expected: no whitespace errors; only intended source, tests, docs, and the owner-private profile are changed; no environment file changes; launchd reports the service.

- [ ] **Step 5: Report without committing**

Report exact test results, contract status, security findings, service state, full-access risk, rollback command, and uncommitted files. Cite OpenAI Docs next to the no-sandbox/no-approval claim.
