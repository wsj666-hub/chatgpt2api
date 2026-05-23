# 会员系统开发规范（Free / Member 双池）

> 本文档面向后续 Codex 实现。读完即可直接动手；如有歧义,以本文件为准,代码与本文件冲突时以本文件为准并在 PR 中说明。
> 关联 PR 模板:[`docs/development-workflow.md`](./development-workflow.md)。
> 上一次定稿:2026-05-23(用户:wsj)。

---

## 1. 目标与范围

为 user 角色的 API key 加上 `tier` 维度（`free` / `member`）,实现:

1. **池隔离**:`free` key 只能消费 `account.type == "free"` 的 ChatGPT 账号;`member` key 只能消费 `account.type ∈ {plus, pro, team, ProLite}` 的账号。
2. **限额**:按 tier 设定每小时 / 每日两级配额。
3. **画质差异**:`member` 支持更高分辨率(默认开放 4K,即 `4096×4096` 及常用横竖比);`free` 锁在 `1024×1024` 上限。
4. **后台可视化**:管理员能在 UI 里给 key 设 tier、看实时已用,并一键重置计数。
5. **错误码贴 OpenAI 协议**,客户端能正确触发自带重试逻辑。

`admin` 角色不算 user-tier,默认绕开限额且默认走 member 池。

> 范围之外:文本接口(`/v1/chat/completions`、`/v1/responses`、`/v1/messages`)本期不做配额。代码里要把字段透传,不报错;实际拦截留给下一期。

---

## 2. 限额表(最终值)

| Tier | 每小时上限 | 每日上限 | 单用户并发 | 单张分辨率上限 | 画质策略 |
|---|---:|---:|---:|---|---|
| **free** | 10 | 100 | 1 | 1024×1024 | 走 free 号生成,质量自然较低 |
| **member** | 无 | 100 | 3 | 4096×4096(以及对应横竖比) | 走 plus/pro/team 号,upstream 画质更好 |

**说明**:
- 计数窗口:**自然小时**(`YYYY-MM-DDTHH` 边界,Asia/Shanghai 时区) + **自然日**(`YYYY-MM-DD`,Asia/Shanghai)。不是滑窗。每跨过整点 / 凌晨 0 点,对应计数自动归零。
- 限额数字以 [`config.json`](../config.json) 的 `tier_limits` 段读取,**不要写死在代码里**,以便日后调档不发版。
- `member` 没有小时限额(配置文件里写 `null` 或不存在表示禁用该维度的检查)。
- "单用户并发"等于同一个 `key_id` 同时正在跑的图片请求数;到上限直接 429 `rate_limit_exceeded`,不排队。
- 画质上限以请求体里的 `size` 字段为准。`size` 缺省或为预设宽高比关键字(`1:1`/`16:9` 等)时,按 tier 的默认尺寸(free→1024×1024,member→2048×2048)。

### 2.1 画质放行的具体规则

- `size` 字段允许两种格式:
  1. **像素**:`"WIDTHxHEIGHT"`,例如 `"4096x4096"`,大小写 `x` 都接受。
  2. **比例关键字**:沿用现有 `build_image_prompt` 支持的 `1:1`、`16:9`、`9:16`、`4:3`、`3:4`。
- 校验逻辑:
  - 解析出像素值后,若长边 > tier 上限 → 返回 400 `invalid_request_error`,`code=size_not_allowed_for_tier`,message 写明用户当前 tier 与允许的最大值。
  - 比例关键字:在协议层把它映射到 tier 默认尺寸(free→1024;member→2048),再走正常 prompt 注入。
- 不做"自动降级到 1024"这种隐式行为。要么明确拒绝,要么明确接受,避免 free 用户绕过付费墙。

---

## 3. 错误响应规范(对外契约)

所有返回都保持 OpenAI 风格 `error` envelope,**响应体只包含 `error` 字段**。前端 SDK(openai-python、openai-node 等)能根据 `error.type` + `code` 自动重试或停止重试。

| 触发条件 | HTTP | error.type | error.code | 备注 |
|---|---:|---|---|---|
| 用户当日 / 当时配额耗尽 | 429 | `insufficient_quota` | `insufficient_quota` | 不带 `Retry-After`(OpenAI 也不带) |
| 用户并发上限 | 429 | `rate_limit_exceeded` | `rate_limit_exceeded` | 带 `Retry-After: 30` 头(估算值) |
| 该 tier 池里所有号全限流 / 全异常 | 503 | `service_unavailable` | `tier_pool_unavailable` | 带 `Retry-After: 60` 头 |
| 用户请求 size 超过 tier 上限 | 400 | `invalid_request_error` | `size_not_allowed_for_tier` | 明确告知允许的最大值 |
| 上游 OpenAI 5xx / 网络错误 | 502 | `server_error` | `upstream_error` | 保持现状 |
| 上游内容策略拒绝 | 400 | `invalid_request_error` | `content_policy_violation` | 保持现状 |

message 字段一律用中文,前端可以直接展示。

---

## 4. 数据模型

### 4.1 `auth_keys.json` 加字段(向后兼容)

```jsonc
{
  "users": [
    {
      "id": "abc123",
      "name": "画图用户",
      "key": "<sha256-hash>",
      "role": "user",
      "enabled": true,
      "tier": "free",        // 新增:"free" | "member"。缺省时 _normalize_item 默认填 "free"
      "last_used_at": "2026-05-23 10:00:00",
      "created_at": "2026-05-19 06:00:00"
    }
  ]
}
```

- `_normalize_item` 读到没有 `tier` 字段的旧记录,自动当 `"free"` 处理,**不写回磁盘**(保持读时填默认)。第一次 `update_key` 时再回写。
- `create_key` / `update_key` 接受可选 `tier` 入参。`update_key({"tier": "member"})` 切换 tier。
- `_public_item` 输出包含 `tier`。
- `authenticate` 返回字典里加 `tier`。

### 4.2 用量计数(新文件 `data/user_usage.json`)

```jsonc
{
  "updated_at": "2026-05-23T10:30:00+08:00",
  "users": {
    "abc123": {
      "hourly": { "bucket": "2026-05-23T10", "count": 7 },
      "daily":  { "bucket": "2026-05-23",    "count": 42 }
    }
  }
}
```

- bucket 字符串变了就说明跨了窗口,新窗口的 count 从 0 开始算。
- 内存为主,30s 周期落盘 + 进程优雅关闭时 flush。重启丢失最多 30s 的计数,在配额这种场景可接受。
- 不存历史明细;明细查询走 `data/logs.jsonl`。

### 4.3 配置 `config.json` 加段

```jsonc
{
  "tier_limits": {
    "free":   { "hourly": 10,   "daily": 100, "concurrency": 1, "max_size_px": 1024, "default_size_px": 1024 },
    "member": { "hourly": null, "daily": 100, "concurrency": 3, "max_size_px": 4096, "default_size_px": 2048 }
  },
  "tier_pool_mapping": {
    "free":   ["free"],
    "member": ["plus", "pro", "team", "ProLite"]
  },
  "tier_pool_fallback": false,
  "admin_tier": "member",
  "admin_bypass_quota": true
}
```

- `tier_pool_fallback=true` 时,member 池空了会回落 free 池(不推荐默认开启,会侵蚀免费配额)。
- `admin_tier` 指定 admin 角色默认用哪个 tier 的池。
- `admin_bypass_quota=true` 时 admin 不计配额。

`ConfigStore` 里给每个新字段加 `@property` 和 setter,落入 `data` dict,保留向后兼容(读不到时给上面那套默认值)。

---

## 5. 代码改动清单(给 Codex 执行)

按下列顺序提交,每个 commit 必须独立可回滚、必须带单测。

### 5.1 `services/auth_service.py`

- `AuthKey` 数据形状里加 `tier` 字段(类型 `Literal["free","member"]`,默认 `"free"`)。
- `_normalize_item` 处理 legacy 数据。
- `create_key(*, role, name, tier="free", key=None)`、`update_key(id, fields)` 支持 `tier`。
- `_public_item` 输出 `tier`。
- `authenticate(raw_key)` 返回 `{id, name, role, tier, last_used_at}`。

### 5.2 `services/config.py`

- 加 `tier_limits` / `tier_pool_mapping` / `tier_pool_fallback` / `admin_tier` / `admin_bypass_quota`,带默认值。
- 给每个加 `@property` + `setter`,落 `data` dict。
- 保证 `config.save()` 把新字段写回。

### 5.3 **新文件** `services/usage_service.py`

```
UsageService:
    __init__(self, path: Path):
        - 读 path,内存里维护 dict[key_id, UsageState]
        - 启动 30s 间隔后台线程做 snapshot
        - 注册 atexit flush

    acquire_slot(key_id: str, tier: str, *, is_admin: bool=False) -> SlotDecision:
        - 取 tier_limits[tier]
        - 若 is_admin and admin_bypass_quota → 返回 NoOpDecision
        - 计算当前小时 bucket、当前日 bucket,跨窗自动清零
        - 若 hourly 限额非空且 count >= 上限 → InsufficientQuota
        - 若 daily count >= 上限 → InsufficientQuota
        - 取 inflight[key_id],若 >= concurrency → RateLimitExceeded(retry_after=30)
        - 否则:三个计数器自增,返回 SlotDecision 上下文管理器

    SlotDecision:
        - .quota_snapshot() → {hourly_used, hourly_remaining, daily_used, daily_remaining}
        - .__exit__(exc_type, ...): 总是 inflight -= 1
        - .refund(reason: str): 仅在"还没出图就失败"时由调用方显式调,把 hourly/daily 各 -1
        - .commit(): 默认成功路径调一下(目前 commit 是 no-op,占位)
```

- 锁粒度:全局单锁(`threading.Lock`)。check + 自增加 inflight 整体 <50μs,不需要分桶。
- 落盘策略:每 30s 写 `data/user_usage.json.tmp` 再 rename。启动时读;读不到/损坏就空 dict。
- 提供 `reset(key_id)` 让管理员手动重置。

### 5.4 `services/account_service.py`

- `_list_ready_candidate_tokens(excluded_tokens=None, allowed_types: set[str] | None = None)`:加 `allowed_types` 过滤。
- `_list_available_candidate_tokens` / `_acquire_next_candidate_token` 同步加参数透传。
- `get_available_access_token(tier: str = "")` :
  - tier 为空时保持现行为(任何类型都拿)。
  - tier 非空时:`allowed_types = set(config.tier_pool_mapping.get(tier, []))`;如果 `tier_pool_fallback` 为 true 且首轮拿不到,合并兜底池再试一次。
  - 没拿到任何号 → 抛新类型 `TierPoolUnavailableError(tier)`(继承 `RuntimeError` 以兼容旧捕获)。
- 新增 `list_accounts_by_tier(tier) -> list[dict]` 给后台 UI 用。

### 5.5 `services/protocol/conversation.py`

- `ConversationRequest` dataclass 加字段:
  - `tier: str = ""`
  - `key_id: str = ""`
  - `is_admin: bool = False`
- 新异常子类(在本文件或新建 `services/protocol/errors.py`):
  - `InsufficientQuotaError(ImageGenerationError)` → 429 / `insufficient_quota`
  - `RateLimitExceededError(ImageGenerationError, retry_after: int)` → 429 / `rate_limit_exceeded`
  - `TierPoolUnavailableImageError(ImageGenerationError)` → 503 / `tier_pool_unavailable`
  - `SizeNotAllowedForTierError(ImageGenerationError)` → 400 / `size_not_allowed_for_tier`
- `stream_image_outputs_with_pool`:
  ```
  with usage_service.acquire_slot(req.key_id, req.tier, is_admin=req.is_admin) as decision:
      try:
          token = account_service.get_available_access_token(tier=req.tier)
      except TierPoolUnavailableError as exc:
          decision.refund("tier_pool_unavailable")
          raise TierPoolUnavailableImageError(str(exc)) from exc
      try:
          ...原逻辑...
      except Exception:
          if not emitted:
              decision.refund("upstream_failed_before_emission")
          raise
      else:
          decision.commit()
  ```
- size 校验放在更上游(协议层),见 5.6。

### 5.6 `services/protocol/openai_v1_image_generations.py` 和 `openai_v1_image_edit.py`

- 入参 body 里读 `tier`、`key_id`、`is_admin`、`size`。
- 调用新增工具函数 `resolve_size_for_tier(size: str | None, tier: str) -> tuple[int, int]`:
  - 把比例关键字 / 像素串解析成 `(width, height)`。
  - 比对 `config.tier_limits[tier].max_size_px`,超了就 raise `SizeNotAllowedForTierError`。
- 把解析后的 `(width, height)` 转回字符串塞回 `ConversationRequest`。

### 5.7 `services/image_task_service.py`

- `submit_generation` / `submit_edit` 在**入队前**就调 `usage_service.acquire_slot`,denial 直接同步返回 429 / 503。
- 把 `decision` 对象挂在 task dict 上;`_run_task` 跑完时按"出过图 commit、没出图 refund"分支决定。

### 5.8 `services/log_service.py`

- `_image_error_response` 加分支处理新四个异常子类,返回正确 status_code、headers(`Retry-After`)、body。
- `LoggedCall.log` 加可选参数 `tier`、`quota_snapshot`,写入 detail。

### 5.9 `api/ai.py` / `api/image_tasks.py`

- 从 `identity` 里读 `tier`、`id`、`role`,塞 payload。
- 不变更外部 schema(只加内部字段)。

### 5.10 `api/accounts.py`

- `UserKeyCreate` / `UserKeyUpdate` schema 加 `tier`。
- 新端点:
  - `GET /api/auth/users/{key_id}/usage` → `{tier, hourly:{...}, daily:{...}, inflight}`
  - `POST /api/auth/users/{key_id}/usage/reset` → 管理员重置
- `GET /api/accounts/by-tier?tier=free` 给后台用。

### 5.11 `api/support.py`

- `require_identity` 返回字典里加 `tier`(从 auth_service 拿到)。
- admin 默认 `tier=config.admin_tier`。

### 5.12 前端 `web/src/...`

- "用户密钥"卡片(`web/src/app/settings/...`):
  - 创建 / 编辑表单加 tier 下拉(Free / Member)。
  - 列表多加 "今日: X/Y · 本时: A/B" 列(从 `/api/auth/users/{id}/usage` 拉,可见时 30s 轮询)。
  - 行尾加"重置"按钮(admin)。
- "号池"页:加 tier 维度过滤器,显示每 tier 池子的 capacity / available。

---

## 6. 测试规范

### 6.1 单测(必须随 PR 提交)

| 测试文件 | 覆盖点 |
|---|---|
| `test/test_auth_service_tier.py` | tier 写入 / 读取 / 默认值 / legacy 数据兼容 |
| `test/test_usage_service.py` | 配额耗尽 / 跨小时归零 / 跨日归零 / 并发上限 / refund / commit / admin bypass |
| `test/test_account_service_tier.py` | tier 过滤 / 池空抛新异常 / fallback 开关 / 老调用方(无 tier 参数)行为不变 |
| `test/test_image_endpoints_tier.py` | 端到端(FastAPI TestClient + mock OpenAIBackendAPI):429 配额、429 并发、503 池空、400 尺寸、200 正常 |

执行:
```bash
uv run python -m unittest discover -s test -v
```

覆盖率目标:新增模块 ≥80%(只对 `usage_service.py` 严格要求,其他模块只测改动行)。

### 6.2 VPS 验收清单

部署后在 VPS loopback(`http://127.0.0.1:9080`)逐项手工跑:

1. 创建一个 free key,name="qa-free"。
2. 创建一个 member key,name="qa-member"。
3. `data/auth_keys.json` 里两个 key 都有 `tier` 字段。
4. 用 free key 连发 11 张图,**第 11 张返回 429 `insufficient_quota`**(打到小时上限)。
5. 等 1 小时,再发 → 成功。重复直到当日 100 张,**第 101 张 429**。
6. 用 free key 同时发 2 个请求,**第 2 个 429 `rate_limit_exceeded`**(并发=1)。
7. 用 member key 连发 100 张,**第 101 张 429**;并发 3 路并发应成功。
8. free key 请求 `size="2048x2048"` → 400 `size_not_allowed_for_tier`。
9. member key 请求 `size="4096x4096"` → 200,返回图分辨率约 4096(可能上游协商成略低)。
10. 把所有 plus 号临时禁用,member key 请求 → 503 `tier_pool_unavailable`。
11. `data/logs.jsonl` 末尾几条记录里有 `tier` 和 `quota_snapshot`。
12. 管理员点"重置"后,第 4 步那个 key 立刻能再发。

---

## 7. 部署顺序与回滚

实现按 5.1 → 5.12 单线推进,每 phase 单独 commit。**不要一个 PR 里塞多个 phase**。

每个 phase 的回滚:

| Phase | 回滚操作 |
|---|---|
| 5.1 / 5.2 | `git revert <sha>`,tier 字段在 JSON 里变成静默元数据,无行为影响 |
| 5.3 | 删 `data/user_usage.json`,counters 清零;revert 删除文件 |
| 5.4 | `git revert`,`tier` 参数缺省 → 任意类型都能拿,等于关 partition |
| 5.5 / 5.6 / 5.7 | 不读 payload 里的 tier 字段就行,protocol 层自动退回旧行为 |
| 5.8 / 5.9 / 5.10 | 标准 git revert |
| 5.11 | 砍掉 UI 字段,后端继续工作,管理员手动改 JSON |

任一 phase 上线后出问题,优先**调 config 关闸**(`tier_limits` 调到 100000,`tier_pool_mapping` 改成 `{"free":["free","plus","pro","team","ProLite"],"member":["free","plus","pro","team","ProLite"]}`)而不是 revert 代码。这是把这套数字做成 config 的核心理由。

---

## 8. 出工时估算

| Phase | 工作量 |
|---|---:|
| 5.1 (auth_service) | 0.5d |
| 5.2 (config) | 0.5d |
| 5.3 (usage_service) | 1d |
| 5.4 (account_service tier 过滤) | 0.5d |
| 5.5 (conversation 接入) | 0.5d |
| 5.6 (size 校验) | 0.5d |
| 5.7 (image_task 同步拦截) | 0.5d |
| 5.8-5.11 (log + api) | 0.5d |
| 5.12 (前端) | 1d |
| 测试与 VPS 验收 | 1d |
| **合计** | **~6.5 人天** |

---

## 9. 给 Codex 的接手清单

1. 先读完本文件 + [`docs/development-workflow.md`](./development-workflow.md)。
2. 拉本仓库最新 `main`,确认 commit 是 `f901efb` 或更新(见 `git log`)。
3. 按 §5 顺序逐 phase 实现,每完成一个 phase:
   - `uv run python -m unittest discover -s test -v` 全绿
   - 提 PR,标题前缀 `feat(tier): phase 5.X - ...`
   - 在 PR description 写哪一节、改了哪些文件、如何回滚
4. 全部 phase 合并到 fork main 之后,按 [`docs/development-workflow.md`](./development-workflow.md) §3 部署到 VPS。
5. 跑 §6.2 验收清单,把结果贴回 PR 评论。

---

## 10. 未在本期范围内（备忘）

- 文本接口的 tier 配额。
- 跨副本部署(多容器、Redis 后端的 usage_service)。
- 月配额、积分制、付款集成。
- 异常 / 限流账号的自动补充(目前靠 `auto_remove_*` 配置)。
- 给 member 用户细分 plus / pro / team 三档画质。
