# 外部平台套卷页面录入脚本

脚本位置：

`/Users/asklins/Documents/xiaozhu_workspace/edge-tts-webui-main/platform_entry/paper_input.py`

这份说明针对本次演示的“人教版 听说测试题模板”页面流程。用户演示时只
填了一部分“听后选择”和“听后应答”，因此脚本不会把演示中的半成品当成
完整数据；执行前必须把输入文件中的每一道小题、每个选项、正确答案和必填
分数补齐。

桌面 App 和命令行脚本会自动使用内置的部署配置，不需要用户输入后台页或接口
地址。地址在运行时恢复，源码中不保留明文；这只是为了隐藏源码文本，不提供
真正的安全保护。

## 模块结构

试卷录入的兼容入口与命令行位于 `platform_entry/paper_input.py`；
可复用实现位于 `platform_entry/adapter/`：

- `normalization.py`：页面语义输入校验和公共数据规范化
- `normalizers/`：按题型拆分的听后选择、听后应答、模仿朗读、听后记录解析器
- `handlers.py`、`group_counts.py`：代码内题型 handler 注册表和卡片计数
- `rules.py`：受审查的套卷模板规则注册表
- `page_navigation.py`：登录、列表、新建/编辑和左侧题型导航
- `page_forms.py`：基础属性、下拉框和模板选择
- `page_cards.py`：题卡、题干、选项、答案和分数控件
- `page_assets.py`：音频/图片控件及无文件选择窗口上传
- `page_content.py`：公共编排、数量闸门、保存和只读回读
- `content_selection.py`、`content_response.py`、`content_imitation.py`、
  `content_record.py`：四类题型各自的页面录入 handler
- `observer.py`、`flow.py`、`runtime.py`、`cli.py`：反馈观察、流程、浏览器运行时和 CLI

扩展新题型时，新增一个 `normalizers/<type>.py` 规范化模块、一个
`content_<type>.py` 页面 handler 和一个计数函数，再用 `QuestionTypeHandler`
在 `normalization.py` 注册；公共
浏览器生命周期、上传、卡片基础操作和数量闸门不需要复制。扩展新套卷时在
`rules.py` 增加代码内规则和测试，不把 CSS 选择器、接口载荷或可执行代码放进
外部 JSON。旧的命令行用法改为 `python3 platform_entry/paper_input.py …`（等价于 `python3 -m platform_entry.paper_input`）。

## 页面流程

脚本只操作页面控件，顺序是：

1. 点击“新增试卷”并填写基础属性
2. 搜索并选择页面显示的试卷模板
3. 点击“下一步：录入试题内容”
4. 按题型组填写第二步页面
5. 通过页面控件上传本地音频/图片
6. 点击“保存试卷”
7. 可选地关闭编辑页，点击“查询”刷新列表并读取保存后的状态

脚本不会点击“预览”，不会用 `fetch`、`page.request` 或其他客户端直接
调用平台写接口。页面因用户输入、上传和保存而产生的 POST/PUT 响应只做
方法、路径和状态码记录；脚本主动读取的反馈只来自白名单 GET 响应。

排查模板懒加载或题卡字段串位时，可临时设置
`PLATFORM_INPUT_DOM_DEBUG=1`。脚本会把真实可见导航、题卡编辑器、输入框和
关键字段的 `outerHTML` 输出到标准错误；默认不开启，不影响正常录入流程。

## 输入格式

套卷优先使用 `question_groups`。`groups` 是同义字段。旧版只有一个编辑器
条目的 `items` 输入仍然兼容，但不能表达复合套卷中的共享录音稿。

最重要的层级是：

```text
题型组
└─ materials（听后选择的录音材料，可有多段）
   └─ questions（同一录音稿下的一道或多道小题）
```

例如，听后选择中“两道题共用一个录音稿”必须这样写；不能把同一个音频路径
复制到两道题并伪装成两个独立材料：

```json
{
  "type": "听后选择",
  "materials": [
    {
      "audio_path": "audio/listening-choice-01.mp3",
      "listening_text": "M: Where did you go? W: I went to Yunnan.",
      "times": 2,
      "gap": 5,
      "questions": [
        {
          "prompt": "Where did the boy go?",
          "options": [
            {"option_id": "A", "text": "To Yunnan."},
            {"option_id": "B", "text": "To Beijing."},
            {"option_id": "C", "text": "To Wuhan."}
          ],
          "answer": "A",
          "score": 1,
          "answer_time": 5
        },
        {
          "prompt": "Who went with him?",
          "options": [
            {"option_id": "A", "text": "His family."},
            {"option_id": "B", "text": "His teacher."},
            {"option_id": "C", "text": "His friend."}
          ],
          "answer": "A",
          "score": 1,
          "answer_time": 5
        }
      ]
    }
  ]
}
```

一个完整听说套卷的主体结构如下。示例中的数量对应本次演示的模板：听后
选择 8 题、听后应答 7 题、模仿朗读 1 题、听后记录 3 题、信息转述 1 题。

```json
{
  "paper_category": "听说考试",
  "paper": {
    "title": "人教七上-S9",
    "province": {"name": "湖北省"},
    "city": {"name": "武汉市"},
    "districts": [],
    "stage": {"name": "初中"},
    "grade": {"name": "七年级"},
    "paper_type": {"name": "单元测试题"},
    "year": 2026,
    "duration": 60
  },
  "template_name": "人教版 听说测试题模板",
  "question_groups": [
    {
      "type": "听后选择",
      "materials": [
        {
          "audio_path": "audio/choice-01.mp3",
          "listening_text": "第一段听力原文",
          "questions": [
            {
              "prompt": "第一道小题题干",
              "options": [
                {"option_id": "A", "text": "选项 A"},
                {"option_id": "B", "text": "选项 B"},
                {"option_id": "C", "text": "选项 C"}
              ],
              "answer": "B",
              "score": 1
            },
            {
              "prompt": "同一录音稿对应的第二道小题题干",
              "options": [
                {"option_id": "A", "text": "选项 A"},
                {"option_id": "B", "text": "选项 B"},
                {"option_id": "C", "text": "选项 C"}
              ],
              "answer": "C",
              "score": 1
            }
          ]
        }
      ]
    },
    {
      "type": "听后应答",
      "questions": [
        {
          "listening_text": "Where did Lisa spend her vacation?",
          "audio_path": "audio/response-01.mp3",
          "options": [
            {"option_id": "A", "text": "In Beijing."},
            {"option_id": "B", "text": "In Wuhan."}
          ],
          "answer": "A",
          "score": 1
        }
      ]
    },
    {
      "type": "模仿朗读",
      "questions": [
        {
          "listening_text": "需要朗读的完整英文原文",
          "audio_path": "audio/imitation-01.mp3",
          "score": 7,
          "reference_answers": ["参考答案全文"]
        }
      ]
    },
    {
      "type": "听后记录并转述信息",
      "recording": {
        "audio_path": "audio/record-01.mp3",
        "image_path": "images/record-table.png",
        "listening_text": "听后记录使用的完整听力原文",
        "questions": [
          {"score": 1, "answers": ["第一空答案"]},
          {"score": 1, "answers": ["第二空答案"]},
          {"score": 1, "answers": ["第三空答案"]}
        ]
      },
      "retelling": {
        "prompt": "This is Cindy's room.",
        "score": 5,
        "answer_time": 90,
        "reference_answers": ["信息转述参考答案全文"]
      }
    }
  ]
}
```

上面的完整结构示例为了说明字段层级，只展开了一段听后选择材料和一道听后应答
小题，并不是本次模板的可执行完整数据。实际执行“人教版 听说测试题模板”时，
必须把 `materials[].questions` 合计补到 8 道，把听后应答 `questions` 补到 7
道；每道选择题/应答题都要有完整选项、正确答案和分数。

### 听后应答

每一道应答小题单独提供 `listening_text`、`audio_path`、`options`、`answer`
和 `score`。如果平台页面需要填写答题时长，可在该小题增加 `answer_time`。
平台模板把听后应答渲染为录音题卡；脚本会把两个选项文本按原顺序拼入“题干”，
中间只保留一个空格，并将 `answer` 对应的选项写入“参考答案”。如果选项前有
Word 使用的 `★` 标记，参考答案会去掉该标记。`audio_path` 要上传到每道题
“听力原文”所在材料块的“原文音频”字段；录音题卡内的“题干音频”保持为空，
“听力原文”文本仍写入对应材料编辑器。

### 模仿朗读

每一道题提供 `listening_text`、`audio_path`、`score` 和至少一个
`reference_answers`。参考答案数组中的每一项会成为页面中的一条“答案”；
多条答案会通过“添加答案”逐条补齐。`audio_path` 上传到“听力原文”旁的
“原文音频”；录音题卡的“题干音频”保持为空。模仿朗读专项不需要参考答案行。

### 听后记录并转述信息

`recording.questions` 对应第一节的填空题，每题的 `answers` 可以有多个
可接受答案；`retelling` 对应第二节的信息转述题。第二节在页面上使用上方
相同的听力原文和音频；由于平台第二节有独立的材料控件，脚本会把第一节的
`recording.listening_text` 再写入第二节，并把同一个 `recording.audio_path`
再次上传到第二节的“原文音频”，不会改用转述题干生成另一份音频。

图片使用页面的 JPG/PNG 上传控件，`image_path` 必须是本地文件路径。

从 Word 套卷或专项卷解析进入系统时，听后记录表不要求用户另外打开文件选择窗口：
解析器会保存源表格的 `table_index`，服务端用隔离的文档渲染依赖把该表格裁成
PNG，生成 `system-input-image` Artifact，再由页面脚本通过真实图片上传控件
提交。裁切会保留四条外框线，且不会把表格下方紧邻的正文带入；表格跨页时会
按源文档顺序拼接。找不到完整表格或中文字体时会在录入前失败，不会上传残缺图片。

Word 解析规则还会读取显式的红色字体作为正确答案。听后选择/听后应答只在
恰好一个选项为红色时写入正确选项；没有红色或出现多个红色选项会保留为不完整，
不会根据语义猜答案。听后应答的每个选项文本会保留页面要求的 `★` 前缀；模仿
朗读的听力原文同时作为参考答案；信息转述的参考答案会完整保留多条答案。

## 完整性保护

对题型组输入，脚本会在开始填写前读取页面题目卡片数量并做闸门校验：

- 本次演示模板要求 8 个选择题卡片（听后选择）
- 听后应答 7 个、模仿朗读 1 个、信息转述 1 个，共 9 个录音题卡片
- 需要 3 个填空题卡片
- 输入数量与页面卡片数量不一致时直接失败，不点击“保存试卷”
- 选择题少于两个选项、缺少正确答案或分数时在本地校验阶段失败
- 共享材料的音频只按 `materials` 上传一次，再将多个小题填入同一材料下

这意味着演示时只填第一题的半成品数据不能直接执行；必须把其余小题也
写入 JSON。

## 使用

默认只做本地校验并输出页面动作计划，不打开浏览器：

```bash
python3 platform_entry/paper_input.py path/to/spec.json
```

确认输入完整后，显式执行可见 Chrome 页面流程：

```bash
python3 platform_entry/paper_input.py path/to/spec.json --execute
```

保存后如需停留在第二步页面调试：

```bash
python3 platform_entry/paper_input.py path/to/spec.json --execute --stay-on-page
```

脚本不自动重试创建、上传或保存动作。执行中断时，先在页面和列表确认当前
状态，再人工决定是否重新录入，避免重复录入。

## 接口与监控边界

脚本只监听页面实际产生的响应：

- `GET /admin-api/system/paper/content/get`：读取页面加载内容时可见的
  `paperId`
- `GET /admin-api/system/paper/page`：回列表后读取保存状态和 `paperId`
- 其他 GET：只做页面加载审计
- POST/PUT/上传响应：只记录方法、路径和状态码，不读取响应正文

本次演示的页面操作和元素变化记录由页面记录器写入：

`/Users/asklins/Documents/xiaozhu_workspace/edge-tts-webui-main/.runtime/platform_input_ui_operations_persistent.jsonl`

记录现在包含：

- 初始页面 DOM 元素快照（分块保存），包括元素路径、属性、尺寸、可见性和
  表单状态；
- 元素新增、删除、属性变化和文本变化；
- 页面脚本通过表单属性（值、勾选、选择项等）产生的程序化变化；
- 点击、键盘输入、粘贴、复制/剪切、文件选择、拖放、焦点、滚动、鼠标、指针、
  触摸、输入法组合等页面操作；
- 同源 iframe 和脚本安装后创建的 Shadow DOM 根节点；跨域 iframe 受浏览器
  同源策略限制，无法读取内部元素。

预览/试听目标按策略跳过。密码、token 和剪贴板原文不会写入记录。记录器的
失败上报会保留在本地发送队列中并自动重试；这只保证监控事件上报，不会重试
创建、上传或保存试卷等业务动作。
