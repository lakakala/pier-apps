# Pier Apps

供 Pier controller 同步的应用定义仓库。应用使用 `pier-pkg` schema 2，blueprint 使用 schema 1。

## CLIProxyAPI

`apps/cliproxyapi` 封装 [CLIProxyAPI v8.0.15](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.15) 官方 Linux 普通版二进制，保留其动态库插件能力，默认关闭插件。支持 amd64、arm64；上游 arm64 文件名使用 `aarch64`。下载文件按固定 SHA-256 校验，无需 Go 或 Docker 构建镜像。

在 controller 中同步本仓库，将服务器绑定到 `blueprints/cliproxyapi`，填写变量后手动部署。二进制 app 不传构建镜像，部署 API 的 `images` 可省略。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `API_KEY` | 必填 | 客户端访问 API 的密钥 |
| `MANAGEMENT_KEY` | 必填 | 官方管理面板的密钥 |
| `PORT` | `8317` | 十进制端口，范围 1–65535，不带前导零 |

两个密钥均不能为空或纯空白；可分别使用 `openssl rand -hex 32` 生成。变量以字符串填写，配置渲染阶段校验并进行 YAML 转义。实际密钥通过 Pier 绑定变量提供，不提交到 Git。渲染后的部署包包含配置与密钥，应按服务凭据管理。

服务固定监听 `127.0.0.1`，管理接口只允许本机访问。模型 API 使用 `API_KEY`，管理接口使用 `MANAGEMENT_KEY`。上游请求直连，不提供代理地址或整份 YAML 部署变量。

### 访问和接入账号

在自己的电脑上建立 SSH 隧道（替换 SSH 用户和服务器地址；修改了 `PORT` 时相应调整）：

```sh
ssh -N -L 8317:127.0.0.1:8317 user@server
```

浏览器打开 `http://127.0.0.1:8317/management.html`，使用 `MANAGEMENT_KEY` 登录。面板首次下载及后续更新需要服务器能访问上游面板发布地址；缓存保存在数据目录，下载失败不影响代理 API 自身启动。

在面板中通过 OAuth 授权或导入认证文件接入账号。远程 OAuth 按面板提示粘贴回调 URL；需要浏览器回调端口的提供商可另外建立对应的 SSH 隧道。账号尚未接入时，模型列表可以为空。

兼容客户端的 API base URL 设置为 `http://127.0.0.1:8317/v1`，密钥使用 `API_KEY`。隧道关闭后，本机客户端将无法访问服务器上的应用。

### 配置与数据

完整配置维护在 [app 配置模板](apps/cliproxyapi/configs/config.yaml)，仅上述三个字段由部署变量渲染。供应商 API Key、模型映射、路由等配置可在模板中按 [上游配置示例](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.15/config.example.yaml) 扩展；新增敏感字段时使用专门的 Pier 变量，避免提交真实凭据。OAuth 认证文件可直接由面板管理。

每个实例使用自己的 `PIER_DATA_DIR`：

```text
data/
├── runtime/
│   ├── config.yaml      # 可写运行配置，上游会将管理密钥哈希化
│   └── pier-release     # 当前配置对应的 Pier release 路径
├── auths/               # OAuth 认证文件
├── static/              # 官方管理面板缓存
└── logs/                # 上游请求错误等附加日志（如产生）
```

程序目录只读。首次启动、新部署和回退时，启动脚本将对应包内配置原子复制到运行目录；同一 release 的自动重启或 agent 重启保留面板修改。重新部署会覆盖面板中的配置修改，需长期保留的设置应更新 app 模板或 Pier 变量。认证文件和面板缓存不随配置覆盖，也不参与 Pier 的版本回退。

应用 stdout/stderr 由 Pier 收集并轮转。停止由 Pier 管理；启动脚本通过 `exec` 运行前台进程。

### 升级

在固定的 `apps/cliproxyapi` 目录中更新配方版本、两个架构的下载地址和 SHA-256，同时核对对应版本配置字段。保持 blueprint 路径和实例 ID 不变，以复用已有用户和数据。提交并同步定义仓库后手动部署；Pier 保存历史部署包用于回退。

配方默认直连下载官方发布包。若 controller 下载必须使用代理，可在配方中开启 `proxy.enabled` 并配置 controller 的 `build_proxy`；该设置仅用于 Pier 下载，不配置应用运行时代理。

## 验证

验证工具使用本机 Pier 源码中的打包库，运行时应用本身仅依赖 Linux 系统工具、glibc 和 CA 证书。

```sh
python3 tests/check.py --pier-repo ../pier
```

默认检查配方、blueprint、变量校验、配置转义及启动脚本的数据生命周期，不下载官方程序。检查工具使用 Python 标准库和 Rust；Cargo 以离线方式复用本机 Pier 依赖缓存。缓存尚未准备时，先在 Pier 源码目录执行 `cargo fetch --locked`。

下载校验官方发布包，并使用 `pier-pkg` 生成、解包两个架构及两组测试配置：

```sh
python3 tests/check.py --pier-repo ../pier --release-tests --packages-dir /tmp/cliproxyapi-test-packages
python3 tests/matrix.py --packages /tmp/cliproxyapi-test-packages
```

输出目录须不存在。发布包保存在 `.cache/releases`；测试包使用固定测试密钥，仅用于验证。测试将校验后的官方归档通过临时本机 HTTP 服务交给打包库，以独立验证直连配方的打包流程；测试下载可使用执行环境的网络代理，不修改正式配方。

系统矩阵需要 Docker，跨架构还需要管理员预先配置 QEMU/binfmt。脚本复用已有对应架构的 Pier agent 测试镜像；缺少镜像时，通过 `tests/runtime.Dockerfile` 构建最小测试环境。可用 `--targets ubuntu2404/amd64` 缩小范围。

运行测试使用普通 UID、只读程序挂载和隔离网络，覆盖 API/管理鉴权、管理接口修改配置、重启保留、部署覆盖、回退恢复、凭据保留、错误配置、端口占用及 SIGTERM 停止。面板使用预置缓存夹具，不访问真实账号或模型；首次在线下载面板和真实 OAuth/模型调用需在具备网络及账号的部署环境验收。

2026-10-05 本地验证通过：双架构官方发布包校验与 Pier 打包/解包，以及 AlmaLinux 8.10、AlmaLinux 9.8、Ubuntu 24.04 的 amd64/arm64 六种运行组合；arm64 使用 QEMU。
