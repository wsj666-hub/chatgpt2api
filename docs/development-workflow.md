# 开发工作流（本地 → 仓库 → 服务器）

> 适用范围:`wsj666-hub/chatgpt2api` fork 及其在 VPS `62.146.172.4` 上的部署。
> 严格按本文件顺序操作。**禁止跳过仓库直接 SSH 改服务器代码**——会被下一次 `git pull` 覆盖,且无审计。

---

## 0. 三方角色

| 角色 | 位置 | 作用 |
|---|---|---|
| **本地工作树** | `/Users/wsj/chatgpt2api` | 唯一编辑入口。所有改动从这里开始。 |
| **GitHub Fork** | `wsj666-hub/chatgpt2api` (remote `fork`) | 部署主线。`main` 分支永远代表"线上当前应该跑的代码"。 |
| **GitHub Upstream** | `basketikun/chatgpt2api` (remote `origin`) | 上游;只读拉取,不直接推。 |
| **VPS** | `root@62.146.172.4:/opt/chatgpt2api/src` | clone 自 fork,bind-mount 到容器。 |

---

## 1. 一次完整的修改流程

### 1.1 本地编辑

```bash
cd /Users/wsj/chatgpt2api
git fetch origin
git fetch fork

# 如果上游有新提交,先 rebase
git checkout main
git rebase origin/main          # 如果有冲突,手解再 --continue

# 在 main 上直接改,或开 feature 分支(推荐 feature 分支)
git checkout -b feat/<topic>
# ...编辑文件...
uv run python -m unittest discover -s test -v   # 全绿才能往下走
```

### 1.2 提交到 fork

```bash
git add <files>                                 # 不要 git add -A,避免拽进 .env / data
git commit -m "feat(...): ..."                  # 见 §2 规范
git push fork feat/<topic>                      # 第一次加 -u

# 用 PR 合并到 fork main(即使只有自己,也走 PR 留审计)
gh pr create --base main --head feat/<topic> --repo wsj666-hub/chatgpt2api \
   --title "feat(...): ..." --body "$(cat <<'EOF'
## Summary
- ...

## Test
- [ ] 单测全绿
- [ ] 本地手动验证: ...

## Rollback
git revert <sha>
EOF
)"

# 自审 / merge
gh pr merge --squash --delete-branch <pr-number>

# 同步本地 main
git checkout main
git pull fork main
```

### 1.3 部署到 VPS

**重要**:VPS 用 bind-mount 跑 fork 的 `main` 分支源码。改动到达 `fork/main` 之后才能去服务器拉。

```bash
ssh -i /tmp/dmit.pem root@62.146.172.4 'bash -s' <<'SH'
set -e
cd /opt/chatgpt2api/src
git fetch origin
git log --oneline HEAD..origin/main | head -10   # 看会拉哪些
git pull --ff-only origin main                   # 只允许 fast-forward,避免脏 merge
cd /opt/chatgpt2api
docker compose restart app
sleep 8
curl -s -o /dev/null -w "ready %{http_code}\n" -H "Authorization: Bearer wsj020728" http://127.0.0.1:9080/api/accounts
docker compose logs --tail 20 app | tail -20
SH
```

**说明**:
- VPS 的 `origin` remote 指向 fork(`https://github.com/wsj666-hub/chatgpt2api.git`),不是上游 basketikun。
- `git pull --ff-only` 防止误操作产生 merge commit;如果 fast-forward 失败,需要手动定位 VPS 上的脏改动。
- bind-mount 路径见 `/opt/chatgpt2api/docker-compose.yml` 里的 `volumes`。改了 Python 文件 → `restart` 就生效;改了 `requirements`/`Dockerfile` → 需要换镜像(见 §5)。

### 1.4 VPS 验证

每次部署后必跑:

```bash
ssh -i /tmp/dmit.pem root@62.146.172.4 'python3 /tmp/img_loopback.py'
```

3 张图都在 30s 内出来就 OK。失败立即看 §4。

---

## 2. Commit 与 PR 规范

### 2.1 Commit message 格式

```
<type>(<scope>): <短描述,中文或英文均可,≤72 字符>

<空行>
<body:为什么改、怎么改、副作用,可选>

<空行>
<footer:Refs / Breaking change / Co-Authored-By,可选>
```

`<type>`:`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf` / `ci`

`<scope>`:模块或目录,例如 `tier` / `account` / `image` / `web` / `infra`

例子:
```
fix(image): release slot on poll timeout

ImagePollTimeoutError 路径漏掉 mark_image_result,导致 _image_inflight
永久 +1。3 次后该账号被永久排除直到重启。补一次 mark_image_result(False)
匹配 acquire/release 配对。
```

### 2.2 PR 规范

- 一个 PR 一件事。多 phase / 多 fix 要拆。
- description 至少包括 `## Summary` / `## Test` / `## Rollback` 三段。
- 触动 hot path / 数据结构 / 外部协议 → 必须列回滚步骤。
- 触动配置默认值 → 必须说明老用户升级影响。

---

## 3. 分支策略

| 分支 | 用途 | 谁能 push |
|---|---|---|
| `main` (fork) | 部署主线;VPS 拉这里 | 仅通过 PR merge,不直 push |
| `feat/*` | 功能开发 | 个人随便 push |
| `fix/*` | 缺陷修复 | 个人随便 push |
| `docs/*` | 纯文档 | 个人随便 push |

**禁止**:
- 在 fork `main` 上直接 commit + push。
- `git push --force` 到 `main`。
- 用 `--no-verify` 跳 hook。

---

## 4. 应急回滚

### 4.1 VPS 急停回滚(代码出问题、服务挂了)

```bash
ssh -i /tmp/dmit.pem root@62.146.172.4 'bash -s' <<'SH'
set -e
cd /opt/chatgpt2api/src
git log --oneline -5
git reset --hard HEAD~1               # 或具体到某个已知好 commit
cd /opt/chatgpt2api
docker compose restart app
sleep 8
curl -s -o /dev/null -w "ready %{http_code}\n" -H "Authorization: Bearer wsj020728" http://127.0.0.1:9080/api/accounts
SH
```

之后立刻在本地 `git revert <bad-sha>`,PR 修正,正常流程重新推上线。**不要把 VPS 留在 detached / dirty 状态**。

### 4.2 配置型回滚(不动代码)

很多功能(配额、池映射、并发上限、轮询超时)都做成了 config。出问题先改 `/opt/chatgpt2api/config.json` 并 `docker compose restart app`,代码不动。这是最快的回滚方式,优先于 git revert。

### 4.3 数据回滚

- `data/accounts.json` 改动前会自动备份为 `accounts.json.bak.<timestamp>`(由 `account_service` 处理)。
- `data/user_usage.json` 出错 → 直接删,counters 清零,影响仅是当窗口内计数重置。
- `data/auth_keys.json` 出错 → 用 fork repo 里同时间点附近的备份或 git 历史恢复。**敏感**:这里有 sha256-hash 后的 key,不在 repo 里,**请定期手动备份到本地**。

---

## 5. 何时需要换镜像（不只是 restart）

bind-mount 只覆盖 `api/`、`services/`、`utils/`、`main.py`。下列改动**必须换 Docker 镜像或重建**:

- `pyproject.toml` 添加/删除依赖。
- `Dockerfile` 改动。
- `web/` 改动(`web_dist/` 在镜像构建时编译,bind-mount 没覆盖)。
- 新增顶级 Python 包目录(比如新建 `models/`)→ 要么扩 bind-mount 加 volume,要么换镜像。

换镜像两条路:

**A. 等上游发新 `ghcr.io/basketikun/chatgpt2api:latest`**

```bash
ssh -i /tmp/dmit.pem root@62.146.172.4 'cd /opt/chatgpt2api && docker compose pull && docker compose up -d --force-recreate'
```

**B. 自己在 VPS 上 build**

```bash
ssh -i /tmp/dmit.pem root@62.146.172.4 'bash -s' <<'SH'
cd /opt/chatgpt2api/src
docker build -t chatgpt2api:local .
# 修 docker-compose.yml: image: chatgpt2api:local + pull_policy: never
docker compose up -d --force-recreate
SH
```

涉及前端 / 依赖变更时,A 优先;A 不可行再走 B。

---

## 6. 环境变量与密钥

- **永远不要**把以下内容提交到 git:
  - `config.json` 里的 `auth-key`(管理员密钥)
  - `data/` 目录(账号 token、用量计数、日志)
  - SSH 私钥(`*.pem`)
  - `.env`
- `.gitignore` 里已经覆盖 `data/`、`*.pem`、`.env*`。提交前 `git status` 必看。
- VPS 的 `/opt/chatgpt2api/config.json` 由 host 直接维护,不在 repo 里。修改方式:
  ```bash
  ssh -i /tmp/dmit.pem root@62.146.172.4 'vi /opt/chatgpt2api/config.json && cd /opt/chatgpt2api && docker compose restart app'
  ```

---

## 7. 上游同步

每周或上游发了重要 commit 时:

```bash
cd /Users/wsj/chatgpt2api
git fetch origin
git log --oneline main..origin/main | head -20   # 看上游有什么新东西
git checkout main
git rebase origin/main                           # 把我们的 commits rebase 到上游 HEAD 上
# 解冲突 -> git rebase --continue
uv run python -m unittest discover -s test -v
git push fork main --force-with-lease            # rebase 后历史变了,只能 force-with-lease
```

随后 VPS 拉:
```bash
ssh -i /tmp/dmit.pem root@62.146.172.4 'cd /opt/chatgpt2api/src && git fetch && git reset --hard fork/main && cd .. && docker compose restart app'
```

(force-with-lease 后 VPS 不能 fast-forward,只能 hard reset。这是上游同步的代价,频率应控制在每周以内。)

---

## 8. 流程检查清单（出 PR 前自查）

- [ ] `git status` 干净,没有意外的 `data/` / `config.json` / `*.pem` 入栈
- [ ] `uv run python -m unittest discover -s test -v` 全绿
- [ ] commit message 符合 §2.1 格式
- [ ] PR description 有 Summary / Test / Rollback 三段
- [ ] 改动涉及 hot path → 已在本地用 `/tmp/img_loopback.py` 跑过 3 张图
- [ ] 改动涉及 config / 数据结构 → 已在 PR 里说明回滚步骤
- [ ] 涉及前端 / 依赖 → 已注明需要换镜像(§5)

---

## 9. 部署检查清单（VPS pull 后自查）

- [ ] `curl /api/accounts` 返回 200
- [ ] `docker compose logs --tail 30 app` 无 Traceback / Error
- [ ] `python3 /tmp/img_loopback.py` 3/3 成功
- [ ] 新增字段(如 tier)在 `data/auth_keys.json` 里按预期出现
- [ ] 用 admin key 在 web UI 跑核心场景,无 5xx

---

## 10. 常见错觉

- ❌ "我直接 docker cp 到容器,快多了" → 下次 `docker compose up -d --force-recreate` 直接没了,还没审计。
- ❌ "VPS 拉之前先在 fork main 强推下" → 强推后 VPS `pull --ff-only` 会失败,你得手动 reset,而且回滚困难。先 PR、merge、再 push、再 pull。
- ❌ "small fix 不写测试" → image 路径的 hot path bug 都是这么进来的。配额逻辑、token 选取、并发账本——任何一行改都要补测。
- ❌ "config 改动不算改动" → config.json 不在 repo,但它和代码的行为强耦合。在 PR description 里明确"需要在 VPS 同时改 config.json 哪几个字段",否则一定会忘。
