# tsn-auto-run · 校园跑 / 阳光跑 记录工作台

一个**本地运行**的跑步记录工作台（体适能「阳光跑 / 校园跑」类 App 适用）。
在你的电脑上起一个本地服务 + 一个页面：**载入跑道 → 生成 GPS 轨迹 → 自动人脸打卡 → 上传**，
并提供「今天能不能跑」判定与历史记录查询。

> ⚠️ **仅供学习与个人研究。** 请遵守你所在学校的规定与相关服务条款，使用风险自负。
> 本仓库不含任何个人凭据、真机设备码或真实记录号。

## 两个版本（lite / full）
**同一份源码**，靠可执行文件名区分 —— 名字里带 `full` 就是完整版：
- **lite（精简版）**：一键全流程，只留必需控件（我只想跑一条合格的）；
- **full（完整版）**：工作台全量（画线、随机起点、区域选择、历史等全露出来）。

打包时用环境变量 `TSN_EXE_NAME` 指定名字（默认 `tsn-auto-lite`）。

## 特性
- 载入 / 绘制跑道（内部坐标口径 **WGS-84**；对外交换 GCJ-02）
- 一键生成合格轨迹（起终点落在围栏内；默认落在有效区间：单次 0.8–3 km、约 6′00″/km）
- 自动人脸打卡（起 / 中 / 末，按服务器下发的设置）
- 「今天能不能跑」判定（以服务器设置为准）+ 历史跑步记录查询（按月汇总）
- 机型下拉（**带搜索**）+「设为默认」+ 找不到机型时手填 / 粘贴 AI 返回的 JSON
- 内置本地 mock（只监听 `127.0.0.1`），默认不连任何真实服务器

## 运行
```bash
pip install -r requirements.txt
python app.py              # 起 mock + 工作台 + 窗口
python app.py --no-window  # 只起服务（自测 / 无桌面环境）
python app.py --self-test  # 起服务后自己跑一遍全流程再退出
python app.py --gdfe       # 打开「广财阳光跑」精简页（需 static/gdfe.html）
```

## 打包（Windows 单文件）
```bash
pip install pyinstaller
set TSN_EXE_NAME=tsn-auto-lite
pyinstaller --noconfirm --clean route_studio.spec
# 产物：dist/tsn-auto-lite.exe（onefile，无控制台）
```

## 依赖
- Python 3.10+
- `pywebview`（桌面窗口；可选，缺失则回退系统浏览器）
- `pycryptodome`（请求信封加解密）
- `Pillow`（人脸图）

## 目录
```
app.py             一键启动：本地 mock + 工作台 + 窗口
server.py          本地 HTTP 服务（页面 + 各 API）
tracklib.py        轨迹生成（几何 / 配速 / 噪声 / 点字段）
flow.py            上传流程编排（配置 / 打卡 / 提交 / 读回）
v2client.py        新链路客户端（信封 / 签名 / 表单）
v2_chain.py        签名与加解密工具
w8b_sign_probe.py  签名探针
replay.py          轨迹回放 / 编号换算
mock_boxkj.py      本地 mock 服务器（默认只监听本机）
device_profile.py  设备 / app 身份（默认档 + 机型目录）
device_catalog.py  机型目录（自动生成）
envlib.py          环境 / 路径
static/index.html  路线工作台页面
static/gdfe.html   广财定制精简页（示例）
static/geo.js      跑道几何
static/schools.json 学校快照
```

## 安全与隐私
- **账号密码不在代码里**：只在运行时于本机读取或页面手输。
- **默认离线**：内置 mock 只监听 `127.0.0.1`，默认不连任何真实服务器。
- **数据留本机**：日志与「记住账号密码」都落在本机（明文、仅本机，可随时清除）。
- **一账号一机**：机型固定、iOS 设备码**每台机首启随机生成并持久化**。
- 仓库已脱敏。

## License
MIT
