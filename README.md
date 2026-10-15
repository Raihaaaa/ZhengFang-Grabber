# 正方教务抢课助手

针对**新正方教务系统（jwglxt，2019 及以后版本）**的桌面抢课工具。
Python + Tkinter 编写，可打包成单文件 exe，双击即用。

> ⚠️ **免责声明**：本项目仅供个人学习与选课便利使用。请遵守所在学校的教务系统使用规定，
> 不要设置过高的轮询频率（建议 ≥ 1.5 秒），避免对服务器造成压力。因使用本工具产生的
> 一切后果由使用者自行承担。

---

## 功能

| 功能 | 说明 |
|---|---|
| 自动登录 | 复刻官方 `login.js` 的 RSA/ECB/PKCS1 密码加密 + CSRF Token 提交（纯 Python 实现） |
| 课程搜索 | 调用教务搜索接口，按关键字列出可选教学班及实时余量 |
| 教师 / 时间 / 余量补全 | 搜索结果本身**不含**教师与容量，程序会自动（或点按钮）逐个查教学班，把**教师、余量、上课时间**补进结果表 |
| 教学班级展开 | 同一门课有多位老师 / 多个时间段时**分行列出**，可只把想要的那个老师加入志愿 |
| 只看还有余量 | 一个复选框即可隐藏已满教学班（状态栏说明隐藏了几个），与教务网站看到的数量对齐；**默认不勾选**，满员教学班也照常列出 |
| 志愿列表 | 多课程 / 多教学班，可勾选启用、上下调整优先级 |
| 余量监控 | 按设定间隔轮询，检测到有人退课（余量 > 0）立即提交 |
| 定时开抢 | 指定 `YYYY-MM-DD HH:MM:SS`，临近时用 5ms 精度对齐抢占先机 |
| 并发提交 | 命中余量时可多线程同时提交同一教学班，提高成功率 |
| 会话复用 | 登录一次后，搜索 / 补全 / 刷新余量 / 检查阶段全部复用**同一个会话**（同一套 cookie），不再每次操作都重新登录；会话被服务端断开时自动重登一次 |
| 断线重连 | 会话过期自动重新登录，网络错误自动退避重试 |
| 防重复退课 | 已抢到的课程会被记入持久标记，重启后不会再次提交（避免误退课） |
| 结果通知 | 桌面弹窗 / 邮件（SMTP）/ Webhook（Server酱·PushPlus·企业微信·钉钉） |
| 运行日志 | 带颜色分级，支持导出，便于排查 |
| 零原生依赖 | RSA 加密用纯 Python 实现，无 `.pyd`、无 VC 运行库要求 |

> ⚠️ **重要安全提醒**：新正方在「计划内选课」中对**已选上的课程重复提交，有一定概率把该课程退掉**。
> 本程序因此在课程抢到后写入持久标记，之后即使重启程序、重新点「开始抢课」，也**不会**再提交这些课程。
> 只有在「课程选择」页显式点击 **「清除已抢到标记」** 并确认后，才会重新提交。
> 请勿在抢到课后反复点开始，除非你确实想改抢别的教学班。

---

## 界面预览

三个标签页：**① 系统配置**（卡片式表单，内容超出窗口时右侧可滚动）、
**② 课程选择**（结果表与志愿表上下分栏，中间分隔条可拖动，默认 55% 给搜索结果）、
**③ 抢课监控**（实时监控表 + 深色运行日志）。

界面按 1320×900 设计（最小 1120×760），字号、行高、按钮和列宽都经过放大，
高分屏或窗口较小时也能看清；同一门课的多个教学班在结果表里**上下相邻并同底色成组**，
一眼就能分辨「示例课程A · 张明（0/90 已满）」和「示例课程A · 李华（12/90）」。

## 快速开始

### 方式一：直接跑源码

```bash
pip install -r requirements.txt
python run.py
```
首次运行会自动生成 `config.json`（字段说明见下文，也可以先复制仓库里的
`config.example.json` 作为模板）。**它保存你的教务地址、学号与密码，属于私有
文件** —— 仓库的 `.gitignore` 已经把 `config.json` 与 `wishlist.json` 排除，
不会被提交，请勿手动 `git add -f` 上去。

> 依赖只有一个 `requests`。RSA 加密由项目内的 `pkcs1.py` 用**纯 Python** 实现，
> 不需要 `pycryptodome`、不需要 VC 运行库、不需要任何原生扩展 —— 打包成 exe 后
> 在别人的电脑上不会因为缺 `.pyd` 或系统安全策略而启动失败。

### 方式二：打包成 exe

```bash
pip install pyinstaller
python build.py
# 产物 1：dist/正方抢课助手.exe                 单文件版（运行时解压到 %TEMP%）
# 产物 2：dist/正方抢课助手-文件夹版/正方抢课助手.exe  文件夹版（不解压，最稳）
```

把 exe 放到一个空目录里运行，首次启动会自动生成 `config.json`。

> 单文件版方便转发，但**每次启动都要把运行时解压到系统临时目录**，
> 少数安全软件（实测火绒）会拦这一步并报
> `Could not create the temporary directory`。遇到就换成**文件夹版**
> （整个文件夹一起拷贝，直接双击里面的 exe），或把 exe 加入信任区。
> `build.py` 会固定 `PYTHONHASHSEED=0` 并在失败时自动重试 3 次。PyInstaller 的
> 分析阶段对模块遍历顺序敏感，放任哈希随机时同一个命令会**时好时坏**
> （实测报过 `TypeError`、`SystemError: Unmatched paren in format`、
> `0xC0000005` 访问冲突三种不同错误），固定种子后即可稳定成功。
>
> 若打包在受限的沙箱/应用控制策略环境下失败（报 `PermissionError: [WinError 5]
> 拒绝访问`，堆栈落在 PyInstaller 的隔离子进程 `create_pipe`），那是沙箱禁止创建
> 命名管道所致，与本项目无关 —— 在普通 Windows 上 `pip install pyinstaller` 后
> 重新执行即可。

### 运行测试

```bash
python tests/run_all.py
```

不联网、不接触真实教务系统：本地起一个模拟正方服务器跑通全链路，真正构造
GUI 验证控件与配置读写，并覆盖引擎的定时开抢与 start/stop 生命周期。

七个测试文件共 160+ 项断言，正常情况下应全部 PASS：

```
tests/mock_server_test.py     登录 → 搜索 → 取教学班 → 提交 全链路（含纯 Python RSA 往返）
tests/search_fallback_test.py 复刻「必须先点查询才出课程」的学校，验证搜索兜底与诊断记录
tests/paging_test.py          复刻「只能搜到少量课程」「提交恒回 flag=0」「提交被权限网关回『无操作权限！』」，守住搜索翻页窗口
                              与提交请求体必须恰好是浏览器那 19 个字段、且必须带 gnmkdm 查询串
tests/prefix_test.py          复刻「教务系统挂在根目录（没有 /jwglxt）」的学校，验证应用前缀自动探测、
                              地址归一化、公钥接口回网页时的报错、302 弹回登录页不算成功，
                              **老配置里锁死的错误前缀能自愈**，以及**登录被拒时不再误报
                              「路径前缀不对」**（前缀没问题就必须如实说是账号/密码/验证码问题）
tests/xkkz_parse_test.py      xkkz_id 的各种页面形态、取值优先级、剪贴板文本提取
tests/gui_smoke_test.py       真实 Tkinter 界面、配置往返、志愿列表、事件队列、诊断反馈、
                              教学班分行与「只看还有余量」过滤
tests/engine_test.py          定时开抢、start/stop 生命周期、已抢到不重复提交
tests/live_check.py           可选：对着真实教务系统只读体检（需自行配置 config.json）
```

另有 `tests/live_check.py`：**连真实教务系统**的体检脚本（登录 → 搜索 → 取教学班
与余量，不会提交选课），用来在自己的学校环境里验证协议是否一致：

```bash
python tests/live_check.py
```

> `mock_server_test.py` 里有一项与 `pycryptodome` 的交叉验证：如果本机装了
> `pycryptodome` 但被安全策略拦截（会打印 `[SKIP]`），不影响其余断言 ——
> 核心逻辑本来就不依赖它。

> **已在真实教务系统上验证**（2026 年实测某高校新版
> `jwglxt`，出于隐私学校名与域名已隐去）：登录、搜索课程（自动翻页）、取教学班与余量全部跑通 —— 教学班显示
> **教师、上课时间与余量**（如「余量 12/90」「0/90（满）」）。真实环境体检脚本：
> `python tests/live_check.py`（只查不提交，不会动课表）。
>
> 但不同学校的接口细节仍有差异，**请先用「测试登录」「搜索」「刷新余量」确认
> 你的学校与本程序一致，再正式使用**。其它学校若仍搜不到课程，多半是上面
> 「6. 服务端会因为页面渲染字段直接报错」之外的新差异，把 `测试登录` /
> `诊断` 输出的日志反馈即可。

---

## 使用步骤
**先装依赖！！！**
`pip install -r requirements.txt`
1. **系统配置** 页填写：
   - `教务系统地址`：填域名即可，如 `https://jwglxt.yourschool.edu.cn`（**末尾不要带斜杠**）。
     **也可以直接把浏览器地址栏里的登录页/选课页链接整条粘进来**，程序会自动取域名，
     例如 `https://jwxt.yourschool.edu.cn/xtgl/login_slogin.html` → `https://jwxt.yourschool.edu.cn`。
   - `应用前缀 app_prefix`：**保持默认 `auto` 就行**。多数新正方挂在 `/jwglxt` 下，
     也有学校直接挂在根目录；程序登录前会探一次并把结果写回这一格（探测结果与域名绑定，
     换学校后会自动重探）。只有当自动探测不成功时，才需要手工填 `/jwglxt` 或留空（=根目录）。
   - `学号`、`密码`
2. 点 **「测试登录」** 确认账号可用。
3. 点 **「一键探测选课参数」**，自动填充 `njdm_id` / `zyh_id` / `bh_id` / `xkkz_id` 等。
4. **课程选择** 页输入课程关键字 → **搜索** → 选中结果 → **加入志愿列表**。
   （也可以「手动添加」，已知课程号时最快）
   - 搜索结果默认会**自动补全**教师 / 余量 / 上课时间（每门课一次请求，50 门课约 10 秒）。
     补全后同一门课若有多位老师会**分行列出**，可以只挑想要的那位老师加入志愿。
   - 默认**不过滤**，满员的教学班也会列出来（这样同一门课的每位老师都看得到）。
     需要只看还能抢的课时，勾选 **「只看还有余量」**：已满教学班会被隐藏，
     状态栏会注明「已隐藏 N 个已满教学班」。这就是「网站显示 50 门、软件列出 57 条」
     的由来 —— 见下方 FAQ。
   - 若你的学校**打开选课页时课程列表是空的、必须点一下「查询」才出课程**，
     本程序已经处理：搜索请求会自动带上 `xkkz_id`，并在关键字没有匹配结果时
     自动改用「空关键字查询」把课程全部列出来，状态栏会注明
     「关键字无匹配，已列出全部课程」。
   - 仍然搜不到时，点搜索框旁边的 **「诊断」** 按钮：它会把登录、页面解析、
     解析到的 `xkkz_id`、搜索响应字段名和每一次尝试写进日志，便于定位原因。
5. 调整志愿优先级，勾选要抢的课。
6. **抢课监控** 页点 **「🚀 开始抢课」**。命中余量会自动提交，抢到后弹窗/邮件通知。
7. 抢到后引擎会**自动停止**，该课程在列表里标记为「已抢到 ✅」。此时即使重启程序，
   也不会再提交它 —— 这是为了避免「重复提交导致已选课程被退掉」。
   若确实要重抢，在「课程选择」页点 **「清除已抢到标记」** 并确认。

---

## 配置文件说明（config.json）

| 字段 | 含义 | 备注 |
|---|---|---|
| `base_url` | 教务系统根地址 | 必填，无末尾斜杠；填成整条页面链接也行（会自动取域名） |
| `app_prefix` | 应用路径前缀 | `auto`（默认，自动探测）/ `""`（挂在根目录）/ `/jwglxt` 等 |
| `app_prefix_for` | 前缀是给哪个域名探的 | 程序自动写入；`base_url` 一变即作废重探 |
| `username` / `password` | 学号 / 密码 | 必填 |
| `xkxnm` / `xkxqm` | 选课学年 / 学期 | 学期 `12`=第一学期，`3`=第二学期（5~8 月） |
| `njdm_id` | 年级代码 | 如 `2023`，留空则从学号前四位推断 |
| `zyh_id` | 专业代号 | 可自动探测 |
| `bh_id` | 班级号 | 如 `23195101`，可从学号截取 |
| `xkkz_id_<kklxdm>` | 选课轮次标识 | 每种课程类型一个；自动探测后写入，也可手工填 `xkkz_id_10` |
| `xkkz_id_auto` | 选课轮次标识（通用） | 界面提取功能写入，各课程类型共用时的兜底值 |
| `poll_interval` | 全局轮询间隔（秒） | 默认 `1.2`，**不建议低于 1.0** |
| `monitor_interval` | 单课刷新间隔（秒） | `0` = 每轮都刷 |
| `concurrent_submit` | 并发提交线程数 | 默认 `1`，调高会增加服务器压力 |
| `start_time` | 定时开抢时间 | `YYYY-MM-DD HH:MM:SS`，留空立即开始 |
| `submit_even_if_full` | 余量为 0 也提交 | 部分学校支持排队/候补时才开 |
| `bad_keys` | 自动学到的「有害请求字段」 | 程序自己维护（默认已含 `localeKey`/`cdTsxx`/`pageNumber`），清空后会重新探测 |
| `notify_*` | 通知渠道开关与参数 | 见下 |

**2. 选课三步**

```
① 搜索课程
   POST /jwglxt/xsxk/zzxkyzb_cxZzxkYzbPartDisplay.html?gnmkdm=N253512&su=<学号>
        filter_list[0]=<关键字> & kklxdm=<类型> & xkkz_id=<轮次标识>
        & njdm_id & zyh_id & bh_id ...
   ← {"tmpList": [ {kch, jxb, kcmc, xf, ...} ]}

② 取教学班（拿 do_jxb_id、余量，以及教师 / 容量 / 上课时间）
   POST /jwglxt/xsxk/zzxkyzbjk_cxJxbWithKchZzxkYzb.html?gnmkdm=N253512&su=<学号>
        kch_id=<服务端课程id，见下> & xkkz_id=<轮次标识> ...
   ← [ {do_jxb_id, jxb, jsxx(教师), jxbrl(教学班容量), yxzrs(已选), sksj(上课时间), ...} ]

   `jsxx` 的原始格式是 `工号/姓名/职称`（如 `200001/张明/无`、`000000/网络教师/无`），
   程序用 `JwglxtClient.clean_teacher()` 只取姓名显示。**搜索结果本身不含教师**，
   所以界面上的教师列来自这一步（见第 14 节）。

③ 提交选课
   POST /jwglxt/xsxk/zzxkyzbjk_xkBcZyZzxkYzb.html?gnmkdm=N253512&su=<学号>
        jxb_ids=<do_jxb_id> & kch_id & kcmc="(课程号)课程名 - X.X 学分"
        & rwlx & rlkz & cdrlkz & rlzlkz & sxbj & xxkbj & qz & cxbj
        & xkkz_id & njdm_id & zyh_id & kklxdm & xklc & xkxnm & xkxqm & jcxx_id
   ← {"flag":"1"} / {"flag":"6"} / {"flag":"3"}   抢课成功
                                                   （3 = 抢到，但免费学分已达上限）
     {"flag":"2"}                                 上课时间冲突
     {"flag":"-1","msg":"a,b,c,d"}                人数已满（msg 逗号分隔，
                                                  第 3 项 = 已选人数，第 4 项 = 本轮已选）
     {"flag":"0","msg":"无操作权限！"}              请求没进业务逻辑，见第 13 节
```

**端点名易错（实测踩过，已修）**：自主选课的写接口是 `zzxkyzbjk_xkBcZyZzxkYzb.html`
（`zzxkyzbjk_` 前缀）；带 `jk_` 的才是「教学班 + 写库」这一族。曾经写的
`zzxkyzb_xkBcZzxkYzb.html` 是**别的选课模式**的处理器，任何参数都只回
「出现未知异常，请与管理员联系！」，所以在界面上表现为「点抢课没反应 / flag=0」。
`JwglxtClient.SUBMIT` 现在指向正确端点，旧值保留为 `SUBMIT_LEGACY` 仅作兜底重试。

**提交体只有 19 个字段**，且必须与浏览器**完全一致**——既不能少，也不能多塞
搜索用的 `filter_list[0]`/`pageNumber` 之类字段（服务端会回「出现未知异常」）。
字段清单见 `JwglxtClient.SUBMIT_FIELDS`：

```
jxb_ids  kch_id  kcmc  rwlx  rlkz  cdrlkz  rlzlkz  sxbj  xxkbj  qz
cxbj  xkkz_id  njdm_id  zyh_id  kklxdm  xklc  xkxnm  xkxqm  jcxx_id
```

其中 `rwlx`/`rlkz`/`cdrlkz`/`rlzlkz`/`xklc`/`xkxnm`/`xkxqm`/`njdm_id`/`zyh_id`
取自选课页隐藏域；`sxbj = "1" if ("1" in (rlkz, cdrlkz, rlzlkz)) else "0"` 在
浏览器里是**现算的**；`cxbj`/`xxkbj` 是**每门课一个**的隐藏域（对应搜索结果的
`cxbj`/`xxkbj` 字段，缺省 `"0"`）；`qz` 非权重选课恒为 `"0"`；`jcxx_id` 无教材时
是空串。拼装逻辑集中在 `JwglxtClient.build_submit_body()`。

**关键实现细节**：`do_jxb_id`（教学班 id）具有时效性，每轮提交前都必须重新获取，
不能缓存 —— 这正是 `engine.py` 中 `_try_course()` 每轮先调 `get_jxb_ids()` 的原因。

**两个字段名坑（实测踩过，已修）**：

- **`kch_id` ≠ `kch`**。搜索结果的 `kch` 是给人看的课程号（`12070001A`），
  取教学班接口要的是服务端 id `kch_id`（`4DAD111B9D0A624DE0635D01020AB8DF`）。
  实测对照：传 `kch_id` → 1 个教学班；传 `kch` → 0 个；不传 → 0 个。
  所以 `Course` 里专门存了 `kch_id` 字段，搜索归一化时就带上。
- **余量字段是 `jxbrl`（教学班容量），不是 `jxbrs`**。原来是按 `jxbrs` 取的，
  该字段不存在 → 余量恒显示「未知」。已改为 `jxbrl or jxbrs or zrs`，并且
  搜索结果里**没有容量字段**（只有 `yxzrs` 已选人数），所以容量必须来自教学班接口。

**3. xkkz_id 的获取**：每种课程类型对应一个 `xkkz_id`。解析逻辑见
`JwglxtClient.parse_xkkz_ids()` / `_extract_xkkz_candidates()`，按置信度覆盖
多种页面形态：

1. `onclick="queryCourse(this,'10','<xkkz_id>','2020','2021')"`（最可靠）
2. 隐藏域 / 任意 `<input name="...xkkzId..." value="...">`（从 name 里取课程类型数字）
3. URL 查询串：`...&kklxdm=10&xkkz_id=<值>`（两者必须相邻，且中间最多跨一个 `&`，
   以免把 `kklxdm` 自己的值误当成轮次标识）
4. JS 变量 / JSON：`var xkkz_id='...'`、`{'kklxdm':'10','xkkz_id':'...'}`
   ——取不到课程类型时记成通用键 `*`，任意课程类型都可取用

**实测存在的真实差异**：某高校（域名隐去）的选课首页
`zzxkyzb_cxZzxkYzbIndex.html` **不下发** `xkkz_id`，用户搜索时得到
「未取到 xkkz_id（选课轮次标识）」。因此 `fetch_xsxk_page()` 在首页解析不到时
会继续按顺序尝试其它候选页面（`_collect_xkkz_ids()`）：

```
XSXK_PAGE?gnmkdm=...            （无 layout/default，部分学校此形态才有值）
CHOOSED_DISPLAY?gnmkdm=&su=     zzxkyzb_cxZzxkYzbChoosedDisplay.html
CHOOSED_PAGE?gnmkdm=&su=        zzxkyzb_cxZzxkYzbChoosed.html
MENU_PAGE?gnmkdm=N253512&su=    xtgl/index_initMenu.html
```

命中即并入 `xkkz_map` 并停止；全部落空时才抛 `JwglxtError`，且错误文案直接给出
「F12 → Network → cxZzxkYzbPartDisplay → Payload 里 xkkz_id」这一具体位置。

**取值的优先级**（`get_xkkz_id()`）：配置 `xkkz_id_<课程类型>` > 页面解析出的该类型
> 通用键 `*` > 解析出的任意一个值（很多学校各课程类型共用一个轮次 id）。
另有 `ensure_xkkz()`：搜索前若 `xkkz_map` 为空，会先跑一遍上面的多页探测；
若配置里已有手工填写的值则直接采用，不发任何请求。

**4. 「必须先点查询才出课程」是怎么处理的**：部分学校要求先点一次查询按钮，
课程列表才会出现。服务端的真实行为是：请求里缺少 `xkkz_id` 时只回一个空 JSON
对象 `{}`，前端看起来就是「没有课程」。因此 `search_courses()` 做了三件事：

1. 搜索请求**始终带上 `xkkz_id`**（`_search_body(xkkz_id=...)`，回归测试
   `tests/search_fallback_test.py` 专门守这条，避免以后被误删）；
2. 分两轮尝试：先「入口路径 × 课程类型 × 关键字」，全都拿不到结果时再退到
   「同一组入口 × 空关键字」（等价于什么都不填直接点查询）；命中兜底结果会置
   `keyword_fallback_used = True`，界面据此提示「关键字无匹配，已列出全部课程」；
3. 每次尝试写入 `search_trace`，界面搜不到时直接打印这些记录并提示「点诊断」。

另外 `search_courses()` 只在「**所有**尝试都失败、且服务端每次都回空对象 `{}`」
时才把「缺 xkkz_id」当成失败原因抛出。如果服务端回过任何有内容的 JSON
（例如 `{"tmpList": []}`），就说明参数没问题、只是确实没有课程，此时返回空列表
而不是报错 —— 避免把「没课」误报成「参数缺失」。

**5. 缺 xkkz_id 时的补救入口（界面）**：错误一旦抛出，课程选择页会弹出对话框
（`_prompt_xkkz_then_retry()`），把剪贴板内容预填进去，用户可以把
① 选课页地址栏链接、② F12 里复制的表单文本、③ 值本身，任意一种粘进去，
`_parse_xkkz_from_text()` 会自动识别并保存为 `xkkz_id_10` + `xkkz_id_auto`，
然后自动重新搜索。系统配置页另有
「从浏览器复制的链接/表单里提取 xkkz_id」按钮（`on_extract_xkkz()`）做同样的事。
识别顺序：HTML input 片段（name/value 顺序皆可）→ `xkkz_id=值` → 整行一个值 →
整段一个值。

**6. 服务端会因为页面渲染字段直接报错（自动学习 `bad_keys`）**

选课页里有一百多个隐藏域，其中**分页、语言、窗口宽度这类渲染状态不是业务参数**，
原样发给接口会让服务端抛异常，返回一张 HTML 错误页：

```html
<!doctype html><head><title>错误提示</title>...
```

实测某高校：整份表单（156 字段）→ 报错；**只把 `pageNumber` 去掉** → 正常返回
11 条课程。于是 `search_courses()` 在遇到这种 HTML 错误页时会走一次自愈探测
（`_probe_extra_keys()`）：

1. 先确认基线可用 —— 基线是**只发计算字段**的 29 个字段，实测这份一定通；
2. 把「选课页下发字段 + 配置字段」整体做**二分**：哪一半会让服务端报错就往里切，
   切到单个字段就记进 `bad_keys` 并打印
   `请求体里的字段 xxx 会让服务端报错，已忽略。`；
3. 用剔除后的请求体重试，成功则在 `search_trace` 里写
   `→ 已剔除导致服务端报错的页面字段后重试成功`；
4. 学到的字段写回 `config.json` 的 `bad_keys`，**下次启动不再重探**。

只会在服务端已经拒绝过一次时才跑，正常学校一次都不会执行；全程十几条请求，
一次性开销。

> 踩过的两次坑（都已修正，写在这里免得重复踩）：① 候选只挑了**配置字段**，
> 而真正致命的 `pageNumber` 来自**选课页字段** —— 候选必须两边都收；
> ② 一开始用「整份请求体 + 一个候选字段」做对照，而整份请求体本身就是坏的，
> 于是 51 个无辜字段全被判成坏字段 —— 判据必须建立在**已知可用的基线**上。
> ③ `localeKey` / `cdTsxx` / `pageNumber` 现在直接进默认黑名单
> （`PAGE_LEVEL_KEYS`），第一次搜索就不必再试探一遍。

**7. 搜索要翻页，否则「只能搜到少量课程」**

`cxZzxkYzbPartDisplay` **一次只回一个窗口**，而且响应里根本没有总页数
（某高校只回 `tmpList` / `sfxsjc` 两个键）。窗口边界是 `kspage` 与 `jspage`
**这一对**：真实 JS 里是

```js
var step = ($("#xkmcjzxskcs").val() != "") ? $("#xkmcjzxskcs").val() : 10;
"kspage": parseInt(jspage) + 1,  "jspage": parseInt(jspage) + parseInt(step)
```

所以第 2 页应该是 `kspage=11&jspage=20`，而**不是** `kspage=2&jspage=10`。
旧实现把「第几页」直接当 `kspage` 发、`jspage` 写死成 10，窗口永远停在最前面，
实测就是「只剩 11 条」。

现在 `search_courses(pages=0)`（默认）会一直往后翻，直到某页为空、或整页课程
都已见过为止（`MAX_PAGES = 60` 兜底）。每页写一行 trace：

```
→ 第 2 页 11 条，新增 11 条
→ 第 3 页 10 条，新增 10 条
→ 第 7 页起没有更多课程，翻页结束
→ ✅ 命中 57 条
```

实测某高校每页多回一行（`kspage=1&jspage=10` 回 11 条），相邻窗口因此重叠，
结果按 `(kch_id, jxb)` 去重。回归测试：`tests/paging_test.py`。

**8. 提交选课的请求体以「搜索请求体」为基座**

旧 `submit()` 自己拼了十来个字段，其中 `njdm_id` / `zyh_id` / `xkxqm` 直接取
配置 —— 而配置里这几项很可能是空的（它们由选课页下发）。实测这份请求体发出去，
服务端恒定回 `{"flag":"0"}`，**连 msg 都没有**，界面表现就是「点了抢课没反应」。

现在 `submit()` 先调 `_search_body()`（页面参数 + 配置参数 + 计算参数，并自动
剔除 `bad_keys`），再叠加提交专用字段：

| 字段 | 说明 |
| --- | --- |
| `jxb_ids` | **教学班标识 `do_jxb_id`**（教学班接口返回，时效性强，不能缓存） |
| `kch_id` | 服务端课程 id（**不是**课程号 `kch`，见上面第 6 节的两个字段名坑） |
| `kcmc` | `(课程号)课程名 - X.X 学分`，由 `build_kcmc()` 拼 |
| `jxbzls` | 教学班总学时（页面 `onclick` 的第四个参数） |
| `sxbj` / `qz` / `xxkbj` | 固定 `1` / `0` / `0` |
| `xkkz_id` / `xkkz_id_` | 轮次标识，两种拼写都带上 |

于是提交体里 `njdm_id=2026`、`zyh_id=55FC…F870`、`bh_id=01202601`、`xkxqm=3`、
`xslbdm=wlb`、`xsbj=0` 全部与浏览器一致（共 160+ 字段）。回归测试同样是
`tests/paging_test.py`。

**9. 页面上下文必须单独补齐**

配置里存过 `xkkz_id_auto` / `xkkz_id_10` 之后，`ensure_xkkz()` 会直接返回、
**再也不会打开选课页**，于是 `_page_params` 一直是空的 —— 搜索缺少页面下发的
`njdm_id` / `zyh_id` / `bh_id` / `xkxqm` 时，服务端只会回
`{"tmpList": [], "sfxsjc": "1"}`，看起来又变成「一门课都搜不到」。

所以「有没有 xkkz_id」与「有没有页面上下文」是两件事：`ensure_page_context()`
专门保证后者，`ensure_xkkz()` 与 `submit()` 都会先调它。同一个 `JwglxtClient`
实例只抓一次页面。

> 附带一个真实教训：内置默认里的 `xslbdm=421`、`xsbj=4294967296` 在某高校
> **是错的**（真值 `wlb` / `0`）。它们之所以能被纠正，就是因为页面下发的值
> 优先级高于配置与内置默认（`effective_param()`：页面 > 配置 > 默认）。

**10. HTTP 910 与「没有 flag 的通用空信封」—— 抢不上最常见的原因**

这条是最容易误判的一条。非选课时间段里，往提交接口发任何东西（哪怕参数完全
正确）都会得到：

```
HTTP 910
{"jgpxzd":"1","listnav":"false","localeKey":"zh_CN","pageable":true,
 "queryModel":{"currentPage":1,"currentResult":0,...},"rangeable":true,
 "totalResult":"0","userModel":{"monitor":false,...,"usable":false}}
```

注意**这个回包里根本没有 `flag` 字段** —— 它是一份「分页查询空结果」的通用
信封，与选课业务无关。旧代码用 `data.get("flag", "0")` 取值，据此报出
`flag=0` 且 `msg` 为空，界面看起来就像「服务端明确拒绝了，但不说原因」。

实测排除了所有参数层面的猜测：把 `jxb_ids` 换成短 id、`sxbj=0`、`qz=1`、
去掉全部非协议字段（167 → 147 字段）、不打 `X-Requested-With`、`Referer` 换成
Display 页 —— **回包一字不差**。换端点探测也证明这不是「路径不存在」：
故意访问一个不存在的路径会拿到 404 + HTML 维护页，而提交路径是 910 + JSON。

所以 `submit()` 现在会在缺少 `flag` 时明确说话，而不是伪装成业务失败：

```
服务端没有进入选课流程（HTTP 910，回包是通用空信封，无 flag 字段）。
最常见的原因是当前不在选课时间；请先在教务网站确认此刻能正常选课。
```

## 项目结构

```
zhengfang-grabber/
├── run.py             启动入口（依赖检查 → 拉起 GUI）
├── gui.py             Tkinter 图形界面（三个标签页）
├── engine.py          抢课引擎：监控循环、提交、重连、通知触发
├── jwglxt.py          教务系统客户端：登录、搜索、取教学班、提交
├── pkcs1.py           纯 Python RSA / PKCS#1 v1.5 加密与 DER 编解码
├── notifier.py        通知模块：桌面 / 邮件 / Webhook
├── config.py          配置读写（config.json）
├── build.py           PyInstaller 打包脚本
├── 启动.bat           Windows 一键启动
├── requirements.txt   依赖清单（只有 requests）
├── tests/
│   ├── run_all.py               一键跑全部测试
│   ├── mock_server_test.py      模拟正方服务器 + 全链路端到端测试
│   ├── search_fallback_test.py  搜索兜底：复刻「必须先点查询」的学校
│   ├── paging_test.py           搜索翻页窗口 + 提交体参数（复刻「搜不全 / 抢不上」）
│   ├── prefix_test.py           应用前缀：复刻「挂在根目录 / 挂在 /jwglxt」两种部署
│   ├── xkkz_parse_test.py       轮次标识 xkkz_id 的页面形态解析、取值优先级、剪贴板提取
│   ├── gui_smoke_test.py        GUI 控件、配置读写与诊断反馈测试
│   ├── engine_test.py           定时开抢与引擎生命周期测试
│   └── live_check.py            真实教务系统体检（登录 → 搜索 → 余量，只查不提交）
├── config.json        运行后自动生成
└── wishlist.json      志愿列表，运行后自动生成
```

---

## 本软件仅供交流学习，有bug请提交issues或者通过邮箱peo67i@163.com反馈
## 如果你觉得好用，请给我点个star,这对我很有帮助，谢谢！
