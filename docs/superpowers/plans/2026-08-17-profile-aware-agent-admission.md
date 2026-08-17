# Profile-aware Agent Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository instructions prohibit commits unless the user explicitly requests one, so this plan creates no commits.

**Goal:** Make explicit `read_only` and `workspace_write` profiles independent of the global Codex policy while preserving fail-closed global-policy checks for `inherit_codex`.

**Architecture:** Capability discovery will report profile-independent Agent readiness and continue carrying the observed global policy as separate evidence. Agent session and doctor will apply that evidence only when the selected profile is `inherit_codex`; explicit profiles continue sending their own sandbox and approval policy at `thread/start`.

**Tech Stack:** Python 3.13, Pydantic settings, asyncio Codex App Server client, pytest, `uv`, `just`.

---

## Task 1: Separate capability readiness from global policy

**Files:**
- Modify: `tests/test_codex_capabilities.py`
- Modify: `src/moco/codex/capabilities.py`

- [ ] **Step 1: Change the unsafe-global-policy test to require profile-independent admission**

Replace `test_only_danger_full_access_never_is_blocked` with:

```python
async def test_global_unsafe_policy_does_not_change_profile_independent_admission(
    tmp_path: Path,
) -> None:
    snapshot, _ = await discover(
        tmp_path,
        actions=happy_actions(sandbox="danger-full-access", approval="never"),
    )

    assert snapshot.effective_policy == EffectivePolicy(
        SandboxMode.DANGER_FULL_ACCESS,
        ApprovalMode.NEVER,
    )
    assert snapshot.policy_state == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
    assert snapshot.realtime.status is CapabilityStatus.AVAILABLE
```

In `test_malformed_granular_policy_is_version_mismatch`, replace the final assertion with:

```python
    assert snapshot.agent_admission == CapabilityState(CapabilityStatus.AVAILABLE, "ready")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_codex_capabilities.py \
  -k 'global_unsafe_policy or malformed_granular_policy' -q
```

Expected: the new assertions fail because `_agent_admission` still returns `unsafe_voice_policy` or propagates the invalid global policy.

- [ ] **Step 3: Remove global policy from profile-independent admission**

Change the call in `CapabilityDiscovery.discover` to:

```python
        admission = (
            _PROBE_FAILED
            if terminal
            else _agent_admission(
                contract,
                validation,
                account,
            )
        )
```

Replace `_agent_admission` with:

```python
def _agent_admission(
    contract: CodexProtocolContract,
    validation: _ContractValidation,
    account: CapabilityState,
) -> CapabilityState:
    readiness = _agent_execution_readiness(contract, validation)
    if readiness.status is not CapabilityStatus.AVAILABLE:
        return readiness
    if account.status is not CapabilityStatus.AVAILABLE:
        return account
    approvals = _approval_readiness(validation, validation.adaptable_categories)
    if approvals is not None:
        return approvals
    return _AVAILABLE
```

Keep `is_unsafe_voice_policy` in this module because Agent session and doctor use the canonical predicate.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_codex_capabilities.py \
  -k 'global_unsafe_policy or malformed_granular_policy' -q
```

Expected: all selected tests pass.

## Task 2: Project admission according to the selected profile

**Files:**
- Modify: `tests/test_doctor.py`
- Modify: `src/moco/doctor.py`

- [ ] **Step 1: Make doctor tests distinguish explicit and inherited profiles**

Replace `test_doctor_projects_unsafe_policy_without_hiding_realtime` with:

```python
@pytest.mark.parametrize(
    ("profile", "expected_admission"),
    [
        (
            AgentProfileMode.READ_ONLY,
            DoctorCheck("codex_agent_admission", "ok", "allowed"),
        ),
        (
            AgentProfileMode.WORKSPACE_WRITE,
            DoctorCheck("codex_agent_admission", "ok", "allowed"),
        ),
        (
            AgentProfileMode.INHERIT_CODEX,
            DoctorCheck("codex_agent_admission", "error", "unsafe_voice_policy"),
        ),
    ],
)
async def test_doctor_projects_unsafe_global_policy_by_selected_profile(
    tmp_path: Path,
    profile: AgentProfileMode,
    expected_admission: DoctorCheck,
) -> None:
    snapshot = make_snapshot(
        effective_policy=EffectivePolicy(
            SandboxMode.DANGER_FULL_ACCESS,
            ApprovalMode.NEVER,
        ),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
    )

    checks = await run_doctor(
        MocoSettings(
            agent=AgentSettings(profile=profile),
            codex=CodexSettings(working_directory=tmp_path),
        ),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )
    by_code = {check.code: check for check in checks}

    assert by_code["codex_policy"] == DoctorCheck(
        "codex_policy", "ok", "danger_full_access_never"
    )
    assert by_code["codex_agent_admission"] == expected_admission
    assert by_code["codex_realtime"] == DoctorCheck("codex_realtime", "ok", "available")
```

Add an inherited unknown-policy test:

```python
async def test_doctor_rejects_unknown_policy_only_for_inherit_codex(tmp_path: Path) -> None:
    snapshot = make_snapshot(
        effective_policy=None,
        policy_state=CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response"),
        agent_admission=CapabilityState(CapabilityStatus.AVAILABLE, "ready"),
    )

    checks = await run_doctor(
        MocoSettings(
            agent=AgentSettings(profile=AgentProfileMode.INHERIT_CODEX),
            codex=CodexSettings(working_directory=tmp_path),
        ),
        command_resolver=lambda _value: CodexCommand(("fixture-codex",)),
        discovery_factory=lambda _command, _rpc, _cwd: FakeCapabilityDiscovery(snapshot),
        connection_factory=lambda _command: FakeConnection(),
        synthesizer_factory=lambda _settings: FakeSynthesizer(),
        hotkey_probe=lambda: True,
    )

    assert DoctorCheck("codex_agent_admission", "error", "invalid_response") in checks
```

- [ ] **Step 2: Run doctor tests and verify RED**

Run:

```bash
uv run pytest tests/test_doctor.py \
  -k 'unsafe_global_policy_by_selected_profile or unknown_policy_only_for_inherit_codex' -q
```

Expected: `inherit_codex` cases fail because doctor currently projects only `snapshot.agent_admission` and does not receive the selected profile.

- [ ] **Step 3: Add profile-aware doctor projection**

Import the selected profile and canonical predicate:

```python
from moco.codex.capabilities import (
    CapabilityDiscovery,
    CapabilitySnapshot,
    CapabilityState,
    CapabilityStatus,
    EffectivePolicy,
    is_unsafe_voice_policy,
)
from moco.config import AgentProfileMode, MocoSettings
```

Pass `settings.agent.profile` from `run_doctor` into `_probe_codex`, and pass it from `_probe_codex` into `_project_codex_snapshot`. Change the projection signature and admission entry to:

```python
def _project_codex_snapshot(
    snapshot: CapabilitySnapshot,
    profile: AgentProfileMode,
) -> list[DoctorCheck]:
    return [
        _project_schema(snapshot),
        _project_capability("codex_account", snapshot.account, available_detail="authenticated"),
        _project_policy(snapshot.effective_policy, snapshot.policy_state),
        _project_capability(
            "codex_agent_admission",
            _profile_agent_admission(snapshot, profile),
            available_detail="allowed",
        ),
        _project_capability(
            "codex_local_review",
            snapshot.server_requests,
            available_detail="available",
        ),
        _project_capability("codex_realtime", snapshot.realtime, available_detail="available"),
        _project_capability("codex_interrupt", snapshot.interrupt, available_detail="available"),
        _project_capability(
            "codex_server_requests",
            snapshot.server_requests,
            available_detail="discovered",
        ),
    ]
```

Add the profile boundary helper immediately before `_project_policy`:

```python
def _profile_agent_admission(
    snapshot: CapabilitySnapshot,
    profile: AgentProfileMode,
) -> CapabilityState:
    admission = snapshot.agent_admission
    if profile is not AgentProfileMode.INHERIT_CODEX:
        return admission
    if admission.status is not CapabilityStatus.AVAILABLE:
        return admission
    if snapshot.policy_state.status is not CapabilityStatus.AVAILABLE:
        return snapshot.policy_state
    if snapshot.effective_policy is None:
        return CapabilityState(CapabilityStatus.VERSION_MISMATCH, "invalid_response")
    if is_unsafe_voice_policy(snapshot.effective_policy):
        return CapabilityState(CapabilityStatus.DISABLED, "unsafe_voice_policy")
    return admission
```

- [ ] **Step 4: Run doctor tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_doctor.py \
  -k 'unsafe_global_policy_by_selected_profile or unknown_policy_only_for_inherit_codex' -q
```

Expected: all selected tests pass.

## Task 3: Lock the Agent wire boundary and align authoritative docs

**Files:**
- Modify: `tests/test_codex_agent.py`
- Modify: `tests/test_repository_contract.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-07-codex-rich-agent-client-design.md`

- [ ] **Step 1: Make the explicit-profile Agent test use an unsafe global snapshot**

In `test_thread_start_uses_explicit_profile_policy`, construct the session with observed unsafe global policy while leaving profile-independent admission available:

```python
    session = make_session(
        connection,
        profile=profile,
        snapshot=capabilities(
            effective_policy=EffectivePolicy(
                SandboxMode.DANGER_FULL_ACCESS,
                ApprovalMode.NEVER,
            ),
        ),
    )
```

Keep the existing exact `thread/start` assertions for `read-only + never` and `workspace-write + on-request`. The existing `test_inherit_rechecks_profile_policy_at_wire_boundary` remains the fail-closed counterpart.

- [ ] **Step 2: Add a README contract test and verify RED**

In `test_readme_documents_current_codex_requirements_and_doctor_codes`, add:

```python
    stage_b = readme.split("### macOS / Windows Stage B", maxsplit=1)[1].split(
        "## 最短の起動手順", maxsplit=1
    )[0]
    assert "`read_only` と `workspace_write` は global Codex policy を admission 条件にしません" in stage_b
    assert "`inherit_codex` だけが global Codex policy を継承します" in stage_b
```

Run:

```bash
uv run pytest tests/test_repository_contract.py \
  -k current_codex_requirements_and_doctor_codes -q
```

Expected: FAIL because README still states that the unsafe global combination blocks every profile.

- [ ] **Step 3: Update README and the earlier accepted design**

Replace the Stage B policy paragraph in `README.md` with text containing these exact sentences:

```markdown
Agent profile は設定ファイルの `agent.profile` で選びます。既定の `read_only`、明示的な
`workspace_write`、Codex の有効設定を上書きしない `inherit_codex` の3種類です。音声や
公開画面から profile は変更できません。`read_only` と `workspace_write` は global Codex policy を admission 条件にしません。
各 profile が sandbox と approval policy を thread 作成時に明示します。`inherit_codex` だけが global Codex policy を継承します。
この profile で有効 policy を確認できない場合、または `danger-full-access` と approval policy
`never` の組み合わせになる場合は、音声から Agent turn を開始しません。
```

Update `docs/superpowers/specs/2026-08-07-codex-rich-agent-client-design.md` in both the admission-policy section and acceptance criteria so it states:

```markdown
Capability discovery は global effective policy を観測結果として保持するが、明示 profile の
admission 条件には使わない。`read_only` と `workspace_write` は thread 作成時に各 profile の
policy を明示し、`inherit_codex` だけが global effective policy を継承する。`inherit_codex` で
effective policy を正規化できない場合、または `danger-full-access` かつ `approvalPolicy=never`
の場合は admission safety ceiling で拒否する。
```

- [ ] **Step 4: Run documentation and Agent boundary tests**

Run:

```bash
uv run pytest tests/test_repository_contract.py \
  -k current_codex_requirements_and_doctor_codes -q
uv run pytest tests/test_codex_agent.py \
  -k 'thread_start_uses_explicit_profile_policy or inherit_rechecks_profile_policy' -q
```

Expected: all selected tests pass.

## Task 4: Verify the complete change and deployment compatibility fix

**Files:**
- Verify: `src/moco/codex/capabilities.py`
- Verify: `src/moco/doctor.py`
- Verify: `src/moco/codex/agent.py`
- Verify: `src/moco/codex/connection.py`
- Verify: `tests/test_codex_capabilities.py`
- Verify: `tests/test_doctor.py`
- Verify: `tests/test_codex_agent.py`
- Verify: `tests/test_codex_connection.py`
- Verify: `README.md`
- Verify: `docs/superpowers/specs/2026-08-07-codex-rich-agent-client-design.md`
- Verify: `docs/superpowers/specs/2026-08-17-profile-aware-agent-admission-design.md`

- [ ] **Step 1: Run focused Python suites including the already-added startup-timeout regression**

Run:

```bash
just test-python \
  tests/test_codex_capabilities.py \
  tests/test_doctor.py \
  tests/test_codex_agent.py \
  tests/test_codex_connection.py \
  tests/test_repository_contract.py -q
```

Expected: all selected tests pass, including `test_startup_timeout_is_independent_of_operation_timeout`.

- [ ] **Step 2: Search for stale policy statements and unintended identifiers**

Run:

```bash
rg -n "unsafe_voice_policy|danger-full-access|global Codex policy|_profile_agent_admission" \
  src tests README.md docs/superpowers/specs
```

Expected: runtime checks mention unsafe policy only at the inherited-profile boundary; docs consistently distinguish explicit profiles from `inherit_codex`; `_profile_agent_admission` appears only in doctor.

- [ ] **Step 3: Run the canonical repository gate**

Run:

```bash
just check
```

Expected: formatter check, lint, mypy, dead-code, dependency checks, ast-grep, Python/JS tests, coverage, browser tests, secret scan, and build all pass.

- [ ] **Step 4: Re-run live local readiness without pinning a Codex binary**

Run:

```bash
uv run moco config validate
uv run moco doctor
```

Expected: config is valid; `codex.command` resolves automatically; Codex schema, account, selected-profile Agent admission, Realtime, interrupt, and server-request checks pass. Irodori may remain a separate deployment blocker until its Windows host endpoint is configured and reachable.

- [ ] **Step 5: Review the final diff without committing**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; only the startup-timeout compatibility fix, profile-aware admission change, tests, README, and approved specs/plans are modified. Do not commit unless the user explicitly asks.
