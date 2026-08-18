# Irodori Dynamic Caption Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows Irodori の delivery caption capability を moco が受理し、確定回答の speech plan を検証して全音声文節へ送信できるようにする。

**Architecture:** moco 所有の capability/request 型で未公開の Windows 契約を再現可能に表現し、bounded HTTP client で capability を取得する。確定回答は Web 境界で一度だけ `body + delivery_caption` に分離し、SpeechQueue がターン内の各文節へ caption を保持し、IrodoriSynthesizer が capability 上限を再検証して送信する。

**Tech Stack:** Python 3.13、Pydantic v2、httpx、FastAPI/WebSocket、pytest/pytest-asyncio、vanilla JavaScript tests

---

## File map

- Create `src/moco/speech/contracts.py`: Windows Irodori と一致する capability と synthesis request の moco 所有型。
- Create `src/moco/speech/plan.py`: 一行 speech plan の構造解析と caption 正規化。
- Create `tests/test_speech_plan.py`: parser の focused unit tests。
- Modify `src/moco/speech/irodori.py`: raw capability client、型変換、caption 付き synthesis。
- Modify `src/moco/speech/queue.py`: caption をターン内の全 `_SpeechItem` へ伝搬。
- Modify `src/moco/config.py`: `caption_mode=auto` を受理。
- Modify `src/moco/doctor.py`: moco 所有 capability 型で live response を検証。
- Modify `src/moco/web/app.py`: capability state、確定回答解析、表示非漏出、error/telemetry。
- Modify `src/moco/web/static/app.js`: 新しい安定 error code の表示。
- Modify `src/moco/runtime/telemetry.py`: content-free caption metadata の allowlist。
- Modify `tests/test_irodori.py`, `tests/test_speech_queue.py`, `tests/test_config.py`, `tests/test_doctor.py`, `tests/test_web.py`, `tests/test_telemetry.py`, `tests/js/app.test.js`: Red/Green regression coverage。
- Modify `README.md`: `caption_mode=auto` と speech plan の利用契約。

### Task 1: Own the Windows Irodori capability boundary

**Files:**
- Create: `src/moco/speech/contracts.py`
- Modify: `src/moco/speech/irodori.py`
- Modify: `src/moco/doctor.py`
- Test: `tests/test_irodori.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write failing capability tests**

Add tests which model the live Windows response and require strict supported/max consistency:

```python
from moco.speech.contracts import (
    DeliveryCaptionCapability,
    IrodoriCapabilities,
)


def test_dynamic_delivery_caption_capability_is_accepted() -> None:
    capabilities = IrodoriCapabilities.model_validate(
        {
            "contract_version": 1,
            "generation": "fixture-generation",
            "ready": True,
            "readiness": "ready",
            "voices": [
                {
                    "id": "narrator",
                    "label": "Narrator",
                    "aliases": [],
                    "default": True,
                }
            ],
            "conditioning": {
                "delivery_caption": {"supported": True, "max_chars": 300},
                "emoji": {"supported": True},
            },
        },
        strict=True,
    )

    assert capabilities.conditioning.delivery_caption == DeliveryCaptionCapability(
        supported=True,
        max_chars=300,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"supported": True, "max_chars": None},
        {"supported": False, "max_chars": 300},
    ],
)
def test_delivery_caption_capability_requires_matching_limit(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DeliveryCaptionCapability.model_validate(payload, strict=True)
```

Add an async transport test whose mock `/capabilities` response advertises `true/300`, and assert `IrodoriSynthesizer.from_settings(...).capabilities()` returns it instead of `invalid_response`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_irodori.py tests/test_doctor.py -q
```

Expected: collection/import failure for `moco.speech.contracts` or capability validation failure on `supported=true`.

- [ ] **Step 3: Add the minimal moco-owned contracts**

Create `src/moco/speech/contracts.py`:

```python
from __future__ import annotations

from typing import Self

from irodori_tts_infra.contracts import (
    CapabilitiesResponse as InfraCapabilitiesResponse,
    ConditioningCapabilities as InfraConditioningCapabilities,
    SynthesisRequest as InfraSynthesisRequest,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeliveryCaptionCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    supported: bool = False
    max_chars: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _require_matching_limit(self) -> Self:
        if self.supported != (self.max_chars is not None):
            raise ValueError("max_chars must be present exactly when captions are supported")
        return self


class ConditioningCapabilities(InfraConditioningCapabilities):
    delivery_caption: DeliveryCaptionCapability = Field(
        default_factory=DeliveryCaptionCapability
    )


class IrodoriCapabilities(InfraCapabilitiesResponse):
    conditioning: ConditioningCapabilities = Field(
        default_factory=ConditioningCapabilities
    )


class IrodoriSynthesisRequest(InfraSynthesisRequest):
    delivery_caption: str | None = None
```

- [ ] **Step 4: Fetch raw capabilities through the bounded transport**

In `src/moco/speech/irodori.py`, add a focused capability client and inject it only from `from_settings`:

```python
class CapabilityClient(Protocol):
    async def capabilities(self) -> IrodoriCapabilities: ...
    async def aclose(self) -> None: ...


class _HttpCapabilityClient:
    def __init__(self, *, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
        )

    async def capabilities(self) -> IrodoriCapabilities:
        response = await self._client.get("/capabilities")
        response.raise_for_status()
        return IrodoriCapabilities.model_validate_json(response.content, strict=True)

    async def aclose(self) -> None:
        await self._client.aclose()
```

`IrodoriSynthesizer` receives `capability_client: CapabilityClient | None`, defaults to the existing fake/client in tests, and closes each distinct client once. `from_settings` constructs `_HttpCapabilityClient` with `_build_transport(..., max_bytes=_MAX_CAPABILITY_RESPONSE_BYTES)` so address override, SNI, and response bounds remain unchanged.

Change doctor/web type validation to `IrodoriCapabilities.model_validate(...)`; keep voice selection behavior unchanged.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_irodori.py tests/test_doctor.py -q
```

Expected: all selected tests pass, including `supported=true / max_chars=300`.

- [ ] **Step 6: Commit the capability boundary**

```bash
git add src/moco/speech/contracts.py src/moco/speech/irodori.py src/moco/doctor.py tests/test_irodori.py tests/test_doctor.py
git commit -m "feat: accept dynamic Irodori capabilities"
```

### Task 2: Parse the authoritative speech plan

**Files:**
- Create: `src/moco/speech/plan.py`
- Create: `tests/test_speech_plan.py`

- [ ] **Step 1: Write parser tests first**

Cover a valid caption, explicit `null`, plain body, malformed JSON, duplicate keys, unknown fields, wrong version, over-limit caption, control characters, angle brackets, and empty body:

```python
def test_parses_plan_and_removes_control_line() -> None:
    result = parse_speech_plan(
        '{"type":"moco.speech_plan","version":1,'
        '"delivery_caption":" calm "}\n本文です。',
        max_chars=300,
    )

    assert result == SpeechPlanResult(
        body="本文です。",
        delivery_caption="calm",
        error_code=None,
        plan_chars=76,
        plan_present=True,
    )


def test_invalid_plan_drops_only_control_line() -> None:
    result = parse_speech_plan(
        '{"type":"moco.speech_plan","version":2,'
        '"delivery_caption":"calm"}\n本文です。',
        max_chars=300,
    )

    assert result.body == "本文です。"
    assert result.delivery_caption is None
    assert result.error_code == "speech_caption_invalid"
```

Compute expected `plan_chars` with `len(control_line)` in the test rather than hard-coding a fragile count.

- [ ] **Step 2: Run parser tests and verify RED**

Run:

```bash
uv run pytest tests/test_speech_plan.py -q
```

Expected: import failure for `moco.speech.plan`.

- [ ] **Step 3: Implement the deterministic parser**

Create `src/moco/speech/plan.py` with:

```python
@dataclass(frozen=True, slots=True)
class SpeechPlanResult:
    body: str
    delivery_caption: str | None
    error_code: Literal["speech_caption_invalid"] | None
    plan_chars: int
    plan_present: bool

    @classmethod
    def plain(cls, body: str) -> SpeechPlanResult:
        return cls(
            body=body,
            delivery_caption=None,
            error_code=None,
            plan_chars=0,
            plan_present=False,
        )


class _SpeechPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["moco.speech_plan"]
    version: Literal[1]
    delivery_caption: str | None


def normalize_delivery_caption(value: str, *, max_chars: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_chars:
        raise ValueError("delivery caption length is invalid")
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise ValueError("delivery caption contains a control character")
    if "<" in normalized or ">" in normalized:
        raise ValueError("delivery caption contains a forbidden delimiter")
    return normalized
```

`parse_speech_plan` must preserve a plain response unchanged, parse only the first non-empty line beginning with `{`, reject duplicate keys through `json.loads(..., object_pairs_hook=_unique_object)`, remove a candidate control line on failure, and require a non-blank body for a valid plan. It must never log input content.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_speech_plan.py -q
```

Expected: all parser tests pass.

- [ ] **Step 5: Commit the parser**

```bash
git add src/moco/speech/plan.py tests/test_speech_plan.py
git commit -m "feat: parse Irodori speech plans"
```

### Task 3: Send validated captions to Irodori

**Files:**
- Modify: `src/moco/speech/irodori.py`
- Modify: `tests/test_irodori.py`

- [ ] **Step 1: Write failing synthesis tests**

Add tests that first load `supported=true / max_chars=300` capabilities, select a voice, synthesize with a caption, and inspect the fake request:

```python
async def test_sends_validated_delivery_caption() -> None:
    client = FakeIrodoriClient()
    client.capabilities_response = make_dynamic_capabilities(max_chars=300)
    synthesizer = IrodoriSynthesizer(client, settings=MocoSettings())
    await synthesizer.capabilities()
    synthesizer.select_voice("fixture-id-0")

    await synthesizer.synthesize("本文です。", delivery_caption=" calm ")

    assert client.requests[-1].delivery_caption == "calm"
```

Also require `caption_unsupported` when the runtime advertises false, and `speech_caption_invalid` when the normalized caption exceeds the advertised maximum. Confirm caption-less synthesis still works and omits no existing fields.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
uv run pytest tests/test_irodori.py -q
```

Expected: `synthesize()` rejects the new keyword or the fake request lacks `delivery_caption`.

- [ ] **Step 3: Extend synthesis without changing sampling defaults**

Change the public method to:

```python
async def synthesize(
    self,
    text: str,
    *,
    delivery_caption: str | None = None,
) -> bytes:
```

When caption is present, require `capabilities.conditioning.delivery_caption.supported`, require a non-null `max_chars`, call `normalize_delivery_caption`, and build `IrodoriSynthesisRequest(..., delivery_caption=normalized)`. Preserve voice ID, generation, `num_steps`, sway, duration, and cfg scales exactly. Map local failures to stable `IrodoriError` codes `caption_unsupported` and `speech_caption_invalid`.

- [ ] **Step 4: Run focused tests and verify GREEN**

```bash
uv run pytest tests/test_irodori.py -q
```

Expected: caption and existing synthesis tests pass.

- [ ] **Step 5: Commit synthesis support**

```bash
git add src/moco/speech/irodori.py tests/test_irodori.py
git commit -m "feat: send delivery captions to Irodori"
```

### Task 4: Carry one caption through every speech segment

**Files:**
- Modify: `src/moco/speech/queue.py`
- Modify: `tests/test_speech_queue.py`

- [ ] **Step 1: Write queue propagation and reset tests**

Use a caption-aware fake synthesizer and a transcript that produces two segments:

```python
async def test_caption_is_sent_to_every_segment_and_not_reused() -> None:
    synthesizer = CaptionAwareSynthesizer()
    queue = SpeechQueue(synthesizer, deliver=ignore_audio, max_chars=80)
    queue.start()

    await queue.on_transcript(
        role="assistant",
        delta="一つ。二つ。",
        done=True,
        delivery_caption="calm",
    )
    await queue.join()
    await queue.on_transcript(role="user", delta="", done=True)
    await queue.on_transcript(role="assistant", delta="次。", done=True)
    await queue.join()
    await queue.close()

    assert synthesizer.calls == [
        ("一つ。", "calm"),
        ("二つ。", "calm"),
        ("次。", None),
    ]
```

Add an invalidation test which queues captioned work, invalidates it, then confirms the next turn uses `None`.

- [ ] **Step 2: Run queue tests and verify RED**

```bash
uv run pytest tests/test_speech_queue.py -q
```

Expected: `on_transcript` rejects `delivery_caption`.

- [ ] **Step 3: Store caption on immutable queue items**

Add `delivery_caption: str | None` to `_SpeechItem`. `SpeechQueue.on_transcript` receives a keyword-only caption, fixes it when the assistant turn begins, copies it to every segment, and clears turn state on done, user reset, invalidation, and close. `_process_item` calls the existing one-argument synthesizer when caption is `None`, and calls `synthesize(text, delivery_caption=...)` only when present so existing caption-less fakes remain compatible.

- [ ] **Step 4: Run queue tests and verify GREEN**

```bash
uv run pytest tests/test_speech_queue.py -q
```

Expected: all queue tests pass.

- [ ] **Step 5: Commit queue propagation**

```bash
git add src/moco/speech/queue.py tests/test_speech_queue.py
git commit -m "feat: propagate captions through speech queue"
```

### Task 5: Enable auto mode at the Web boundary

**Files:**
- Modify: `src/moco/config.py`
- Modify: `src/moco/web/app.py`
- Modify: `src/moco/web/static/app.js`
- Modify: `tests/test_config.py`
- Modify: `tests/test_web.py`
- Modify: `tests/js/app.test.js`

- [ ] **Step 1: Write failing config and Web integration tests**

Replace the old rejection test with acceptance plus unknown-value rejection:

```python
def test_irodori_caption_mode_accepts_auto(tmp_path: Path) -> None:
    path = tmp_path / "moco.yaml"
    path.write_text("irodori:\n  caption_mode: auto\n", encoding="utf-8")
    assert load_config(path).irodori.caption_mode == "auto"
```

Add Web tests for:

- state reports `captionMode=auto` and `deliveryCaptionSupported=true`;
- `caption_unsupported` stops conversation start when auto is configured against false capability;
- a valid plan displays only body and passes caption to the speech queue/synthesizer;
- malformed plan displays/reads body without caption and emits exactly one `speech_caption_invalid`;
- control line never appears in WebSocket messages or synthesizer text.

Add JS assertions that both new error codes render Japanese copy and `caption_unsupported` is a conversation-start error.

- [ ] **Step 2: Run config/Web/JS tests and verify RED**

```bash
uv run pytest tests/test_config.py tests/test_web.py -q
node --test tests/js/app.test.js
```

Expected: auto config is rejected and Web state/parser assertions fail.

- [ ] **Step 3: Implement config, capability state, and final-answer parsing**

Change `caption_mode` to `Literal["off", "auto"]` while retaining `off` as default.

In `_BrowserConnection`, cache:

```python
self._delivery_caption_supported = False
self._delivery_caption_max_chars: int | None = None
```

Populate them in `_cache_capabilities`. When `caption_mode == "auto"`, `_prepare_start_synthesizer` returns `caption_unsupported` unless both support and a positive limit are present.

In `on_turn_finished`, after control emoji stripping and before display/speech branching:

```python
result = (
    parse_speech_plan(authoritative_text, max_chars=self._delivery_caption_max_chars)
    if self._settings.irodori.caption_mode == "auto"
    and self._delivery_caption_max_chars is not None
    else SpeechPlanResult.plain(authoritative_text)
)
```

Send `result.body` to `_send_turn_result_transcript` and `_queue_terminal_speech_text`; pass `result.delivery_caption` only to speech. On invalid result, schedule one `_send_error("speech_caption_invalid")`. Update state conditioning from settings/capability rather than constants.

Add `caption_unsupported` and `speech_caption_invalid` to the Python public error boundary and JS copy without exposing caption content.

- [ ] **Step 4: Run config/Web/JS tests and verify GREEN**

```bash
uv run pytest tests/test_config.py tests/test_web.py -q
node --test tests/js/app.test.js
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the Web integration**

```bash
git add src/moco/config.py src/moco/web/app.py src/moco/web/static/app.js tests/test_config.py tests/test_web.py tests/js/app.test.js
git commit -m "feat: enable automatic delivery captions"
```

### Task 6: Add content-free telemetry and user documentation

**Files:**
- Modify: `src/moco/runtime/telemetry.py`
- Modify: `tests/test_telemetry.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing telemetry privacy tests**

Require only `caption_present`, `plan_chars`, `caption_mode`, and existing `contract_version` metadata to survive sanitization. Require caption/body strings and unknown attributes to be rejected:

```python
def test_caption_telemetry_accepts_metadata_without_content() -> None:
    assert sanitize_attributes(
        {
            "caption_present": True,
            "plan_chars": 120,
            "caption_mode": "auto",
            "delivery_caption": "private caption",
        },
        strict=False,
    ) == {
        "caption_present": True,
        "plan_chars": 120,
        "caption_mode": "auto",
    }
```

- [ ] **Step 2: Run telemetry tests and verify RED**

```bash
uv run pytest tests/test_telemetry.py -q
```

Expected: new safe metadata is dropped.

- [ ] **Step 3: Add narrow telemetry allowlist rules**

Allow only strict bool `caption_present`, non-negative strict int `plan_chars`, and enum string `caption_mode in {"off", "auto"}`. Keep `delivery_caption` and arbitrary content absent from `_ALLOWED_ATTRIBUTES`.

Document in `README.md`:

```yaml
irodori:
  caption_mode: auto
```

Document the one-line `moco.speech_plan` envelope, dynamic capability limit, caption-less fallback, and the fact that AGENTS.md may later instruct Codex when to emit the envelope.

- [ ] **Step 4: Run telemetry tests and verify GREEN**

```bash
uv run pytest tests/test_telemetry.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit telemetry and docs**

```bash
git add src/moco/runtime/telemetry.py tests/test_telemetry.py README.md
git commit -m "docs: describe dynamic delivery captions"
```

### Task 7: Repository verification, review, PR, merge, cleanup, and live deployment

**Files:**
- Verify all modified files
- Modify only review findings that are within this design scope

- [ ] **Step 1: Run focused regression suites**

```bash
uv run pytest tests/test_speech_plan.py tests/test_irodori.py tests/test_speech_queue.py tests/test_config.py tests/test_doctor.py tests/test_web.py tests/test_telemetry.py -q
node --test tests/js/app.test.js
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full repository gates**

```bash
uv run pytest
npm ci
just check
```

Expected: Python and frontend formatting, lint, typecheck, dead-code, dependency, ast-grep, coverage, browser, secret scan, and build gates pass. Record pre-existing tool warnings separately from failures.

- [ ] **Step 3: Run polishment then ai-slop-cleaner subagent reviews**

Dispatch the user-requested reviews sequentially. Apply only concrete findings that preserve this design, rerun focused tests after each review, and commit fixes separately.

- [ ] **Step 4: Publish and converge the PR**

Push `codex/irodori-dynamic-caption`, open a PR describing the live Windows contract and Red/Green evidence, watch CI and review feedback, fix actionable findings, and stop only when required checks are green and review threads are resolved.

- [ ] **Step 5: Merge and clean Git state**

Merge the PR, update `/Users/monsoon/Dev/moco` main, confirm it matches `origin/main`, remove the clean feature worktree and local/remote feature branch, and verify no unrelated user changes were removed.

- [ ] **Step 6: Configure and restart the real service**

Update `/Users/monsoon/Library/Application Support/moco/moco.yaml` with:

```yaml
irodori:
  base_url: https://win-toarupen.tailffb07d.ts.net/
  connect_ip: 100.112.161.83
  caption_mode: auto
```

Validate config, record the current stderr size/timestamp, restart `moco service`, and confirm the launchd PID runs the merged main checkout.

- [ ] **Step 7: Run live startup and synthesis verification**

```bash
uv run moco service status
curl --fail http://127.0.0.1:8765/
uv run moco doctor --synthesize "接続確認です。"
```

Run a focused Python live probe through `IrodoriSynthesizer` with a valid caption, verify the returned bytes are a complete RIFF/WAVE file, and compare the server-reported or wall-clock caption-less/captioned durations without asserting an unstable performance threshold. Inspect only new service stderr and require no startup failure or Input Monitoring trust warning.

- [ ] **Step 8: Complete only after real-process evidence exists**

Record merged commit, service PID/status, operator HTTP result, doctor result, live caption WAV byte count, Tailscale capability result, and macOS permission state. Do not claim completion while System Settings authentication or a required live check remains pending.
