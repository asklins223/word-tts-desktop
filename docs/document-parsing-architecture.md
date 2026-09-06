# 文档解析分层架构与规则治理方案

> 状态：设计基线；模仿朗读解析画像与录入门禁最小闭环已落地，其余画像和规范结构仍按阶段实施；更新日期：2026-09-04；适用范围：词汇 Excel、教材课文跟读、听说试卷 Word 的识别、结构化解析、音频切分和后续系统录入前的数据准备
> 关联文档：[系统录入整合方案](system-input-integration-plan.md)、[Word 解析原子小题模型改造方案](atomic-question-model-plan.md)

## 1. 背景与结论

当前 `question_types` 的 8 个 Parser（信息获取、听后选择、听后应答、课文跟读、信息转述及询问、听后记录并转述信息、模仿朗读、词汇）是现有的**解析能力入口**，不是产品领域中的并列顶层文档类型。

产品实际面对的是三类源文档：

1. `词汇`：纯 Excel，核心是词汇单元、单词和例句。
2. `课文`：教材跟读文档，可能包含多个 Unit；出版社、年级、册别和排版版本会影响结构识别规则。
3. `试卷`：Word 试卷，可按`题型专项`或`套卷`组织；其中包含一个或多个考试大题型，每个大题型又可能包含多种小题、材料、答案和音频片段。

本方案冻结以下原则：

- 顶层先识别`词汇 / 课文 / 试卷`，再进入各自的结构和题型解析；不再把“听后选择”等大题型当作整份源文档的顶层类型。
- `专项 / 套卷`是试卷的**组织形态**，不是题型。一个专项可以包含多种小题；一个源文件也可以包含多个专项或多套试卷。
- `人教版 / 外研版 / 年级 / 册别 / 排版格式`是课文的**解析画像（profile）**，不是新的业务类型。规则差异由 profile 管理，不能散落为大量版本判断。
- 一个源文档可产生零个、一个或多个`录入单元`；当前“一个文档一套卷”只是已知输入习惯，不能写死为模型约束。
- 结构识别、题型抽取、音频切分和外部系统录入使用同一份结构事实。旧的 `category` 输出只作为兼容和展示字段，不再承担全部业务语义。
- “模仿朗读”等考试大题型可以对应多个试卷版式画像；`major_type_code=imitation_reading`只说明题型语义，绝不等同于“已支持外部录入”。音频能力和录入能力必须分别、按目标范围声明，外部录入默认关闭。

本文件只定义解析边界、领域层级、规则归属和改造顺序；不替代现有工作流、数据库、外部录入和 UI 方案。

## 2. 领域层级

### 2.1 结构树与关系图

```text
SourceDocument
└─ DocumentRevision
   ├─ VocabularyDocument                     文档大类：词汇
   │  └─ InputUnit*                          词汇单元/课次/词汇组
   │     └─ VocabularyEntry*                 单词、例句、释义等
   │
   ├─ TextbookDocument                       文档大类：课文
   │  └─ InputUnit*                          Unit / Starter Unit / 默认单元
   │     └─ StructureNode*                   Section / Chapter / Reading Plus
   │        └─ ContentTypeNode*              句子跟读 / 段落跟读 / 语篇跟读
   │           └─ ContentGroup*              Conversation / 语篇 / 文章小标题
   │              └─ ContentItem*            逻辑朗读内容
   │                 └─ AudioSegment*        说话轮次、句子、自然段等
   │
   └─ ExamDocument                           文档大类：试卷
      └─ InputUnit*                          unit_kind=ASSESSMENT；一套试卷或一个专项
         └─ MajorSection*                    信息获取、听后选择等考试大题型
            ├─ Stimulus*                     录音稿、对话、文章、图片说明等共享材料
            ├─ QuestionGroup*                题组、子节、任务组
            └─ QuestionItem*                 最小业务小题
               └─ AudioSegment*              与音频产物的映射
```

`*`表示该层可出现多个节点。上图只表达**包含关系**：一个节点在文档结构中属于哪个父级。源文档派生的节点必须保存原文范围、顺序、置信度和证据；人工新增或纯派生节点必须保存来源类型、父级/变换链和创建修订，不能伪造不存在的 Word 范围。无法归类的内容也必须保留在最近父节点下的`unclassified`节点中，不能静默丢弃。

材料、题目和音频产物并不总是树形关系，必须额外保存关系边：

```text
QuestionItem ──primary_material / supporting_material──> Stimulus
ContentItem  ──contains────────────────────────────────> AudioSegment
AudioSegment ──rendered_as─────────────────────────────> AudioArtifact
```

例如，一段录音稿可同时服务三道选择题；这三道题在结构树中各自归属题组或大题，但通过三条 `QuestionItem → Stimulus` 关系边引用同一份材料。不得为了迁就树结构而复制录音稿正文。

上图是**主文档的逻辑解析树**，不是要求立即新建 `VocabularyDocument`、`TextbookDocument`、`ExamDocument` 三套独立物理表。一次导入即使只有一个文件，也由第 3.4 节定义的 `SourcePackageRevision` 包裹；其中恰有一个主文档进入上图。一份 `DocumentRevision` 在最终裁决后只选择一个 `document_kind`；存在跨领域冲突时保留候选与诊断，等待用户拆分或确认。答案、听力稿和独立音频等附件不构成第四种 `document_kind`，也不能改变主文档的顶层分类。新表、现有原子模型和旧投影的落地方式仍以[Word 解析原子小题模型改造方案](atomic-question-model-plan.md)为准。

### 2.2 不同层级不能混用

| 概念 | 代表值 | 回答的问题 | 不应被误用为 |
| --- | --- | --- | --- |
| 文档大类 `document_kind` | `vocabulary`、`textbook`、`exam` | 这份文件整体属于什么业务领域？ | 大题型或音频类别 |
| 试卷组织形态 `exam_form` | `special`、`paper`、`unknown` | 试卷如何组织和录入？ | 大题型名称 |
| 试卷大题解析画像 `major_section_profile` | `imitation_legacy_unit_source`、`imitation_unit_source_special`、`imitation_boxed_special` | 这一个连续大题块采用哪套版式规则？ | 整份文档类型或“可录入”结论 |
| 外部录入画像 `entry_profile` | `imitation_reading_v1` | 该目标对应哪个已注册的平台页面契约？ | 源文档版式、音频能力或自动放行依据 |
| 教材解析画像 `textbook_profile` | `renjiao_section_ab`、`foreign_language_legacy` | 用哪套结构规则解析课文？ | 面向用户的业务类型 |
| 考试大题型 `major_type_code` | `listening_choice`、`imitation_reading` | 这一连续试卷分区是什么题型？ | 整份文档类型 |
| 业务小题型 `sub_type_code` | `listening_info`、`answer_question` | 该题型内部的内容或任务是什么？ | 版式规则 |
| 结构节点 `node_kind` | `unit`、`section`、`conversation`、`article` | 这段内容在树中的组织角色是什么？ | 音频文件类别 |
| 音频片段 `segment_kind` | `sentence`、`paragraph`、`speaker_turn` | 以什么粒度朗读或生成音频？ | 业务题型 |

例如，`Section A`、`Conversation 1`、`语篇跟读`、`U1`不能都塞入一个 `category` 字段：它们分别可能是章节、子组、内容类型和录入单元名称。

### 2.3 当前 Parser 到目标模型的映射

| 当前 Parser / Family | 目标文档大类 | 在目标树中的角色 | 已知业务子类型 |
| --- | --- | --- | --- |
| `vocabulary` | `vocabulary` | `InputUnit` 下的 `VocabularyEntry` 抽取器 | 单词、例句 |
| `text_reading` | `textbook` | `InputUnit` 下的课文内容抽取器 | 句子跟读、段落跟读、语篇跟读 |
| `info_acquisition` | `exam` | `MajorSection` 抽取器 | 听选信息、回答问题 |
| `listening_choice` | `exam` | `MajorSection` 抽取器 | 听后选择 |
| `listening_response` | `exam` | `MajorSection` 抽取器 | 听后应答 |
| `info_retelling` | `exam` | `MajorSection` 抽取器 | 信息转述、询问信息 |
| `listening_record_retelling` | `exam` | `MajorSection` 抽取器 | 听后记录并转述信息 |
| `imitation_reading` | `exam` | `MajorSection` 抽取器 | 模仿朗读 |

现有 8 个 Family 和 12 个 SubType 可以继续使用，但它们必须位于本表所示的第二、三层。新增出版社或教材排版时，原则上新增/调整 profile，而不是新增一个 Family。

### 2.4 身份、定位与修订边界

解析结构有三种不同的“身份”，不得混用：

| 身份 | 作用 | 稳定范围 |
| --- | --- | --- |
| `block_id` | 原始段落、表格单元格、文本框或媒体锚点的物理位置 | 仅在一个 `DocumentRevision` 内稳定 |
| `source_locator` | 面向用户和诊断的可读路径，例如 `U1 / Section A / Conversation 2` | 可随结构命名或人工调整变化 |
| `node_id` / `question_id` / `stimulus_id` | 逻辑内容身份，用于版本匹配、历史映射和下游引用 | 可跨修订延续，但必须经过 revision match 裁决 |

每次解析必须绑定不可变的 `source_package_revision_id`、主 `document_revision_id`、输入清单哈希、解析 schema 版本和规则集版本。重新解析同一来源包时，不能把新的结构直接覆盖旧结构；应由现有原子模型中的 revision match 规则决定哪些逻辑节点沿用身份、哪些节点产生新修订、哪些节点进入待确认。

`block_id`和 `source_locator`都不能单独充当跨修订业务主键：前者会因 Word 排版调整而变化，后者会因标题改名或用户编辑而变化。

一个逻辑节点可以引用多个不连续原文范围，例如“题干在段落、选项在表格、材料在后文”的选择题；也可以在已确认的来源包内同时引用主文档和答案附件。规范字段应使用 `source_ranges[]`，且每项都要指明所属 `document_revision_id`；如新 API 仍保留单值 `source_range`，它只能是首个/主要范围的兼容视图，不能丢掉其余来源，也不能与旧存储摘要 `source_range_json` 混用。

每个节点还必须声明 `origin`：

| `origin` | 适用对象 | 必需追溯信息 |
| --- | --- | --- |
| `source` | 直接从段落、表格、图片等原文读取 | 一个或多个 `source_ranges` |
| `inferred` | 根据多个原文块推断出的 Unit、题组或结构节点 | 证据规则、参与的 `source_ranges`、置信度 |
| `user` | 用户新增、合并、拆分、改写的内容或节点 | `structure_revision`、编辑者动作、可选锚点范围 |
| `derived` | 音频片段、清洗文本、导出投影等派生对象 | 父节点/输入修订、变换或策略版本 |

人工内容的 `source_locator` 可以使用稳定的 `manual:<node_id>` 形式，但不得冒充源文档段落定位。

### 2.5 注册表的职责边界

“单一事实源”不等于把所有层级塞进一个注册表。目标状态应有多个边界明确、单向依赖的注册表：

| 注册表 | 唯一负责的事实 | 不负责的事实 |
| --- | --- | --- |
| `DocumentKindRegistry` | `vocabulary`、`textbook`、`exam` 的展示、能力和入口策略 | 题型正则、教材版式 |
| `Family/SubType Registry` | 考试大题/小题语义、题目角色、命名/音色兼容能力 | 顶层文档类型、出版社规则 |
| `TextbookProfileRegistry` | 教材布局、适用条件、回退链、版本和样例 | 考试题目关联规则 |
| `RuleCatalog` | 可复用标记、结构谓词、优先级、规则版本和正反样例 | UI 文案、外部平台字段 |
| `AudioPolicyRegistry` | 音频切分和角色/音色策略 | 文档分类、逻辑节点身份 |
| `ExternalInputSchemaRegistry` | 各外部页面要求的字段和完成条件 | 从原始文本猜测题型 |

当前 `question_model/model.py` 的 Family/SubType Registry 可继续担任第二行职责。新增顶层注册表或 profile 注册表时，必须由上层向下层单向引用；检测器、Parser、命名模块和 UI 都消费同一份派生结果，而不是各自维护相同的字符串集合。

### 2.6 `InputUnit` 与现有 `ContentUnit` 的术语约束

`录入单元`的规范领域名固定为 `InputUnit`：它是面向核对、配置、交付和外部提交的顶层业务容器，统一覆盖课文 Unit、词汇课次/组和试卷专项/套卷。

现有 `question_model.model.ContentUnit` 的实际含义是学习内容**叶子**，例如一句跟读文本、一个段落片段、一个单词或一个例句。它不能同时表示“U1”这类父级录入单元。迁移期采用以下约束：

| 对象 | 角色 | 关系 |
| --- | --- | --- |
| `InputUnit` | 统一业务容器 | 一个源文档可有多个；拥有 `unit_id`、顺序、来源范围、类型和修订 |
| `AssessmentUnit` | 试卷场景的显示/兼容别名 | 等价于 `InputUnit(unit_kind=ASSESSMENT)`，不是第二套实体 |
| 现有 `ContentUnit` | 学习内容叶子 | 通过 `input_unit_id` / 结构节点关系归属于一个 `InputUnit` |
| `QuestionItem` / `Stimulus` | 考试内容事实 | 通过 `major_section_id` 和 `input_unit_id` 归属于一个试卷 `InputUnit` |

因此，新解析结果的 `input_units[]`始终返回 `InputUnit`，不再用“词汇/课文返回 `ContentUnit`、试卷返回 `AssessmentUnit`”这种按文档类型变化的外壳。现有 `ContentUnit` 表或对象在迁移期间继续保留，待建立父级单元与结构节点映射后再逐步收口。

`InputUnit.unit_kind`是领域语义，不能和现有工作流的兼容字段 `input_type` 混为一谈。迁移期间映射固定为：`TEXTBOOK → textbook`、`VOCABULARY → vocabulary`、`ASSESSMENT → paper`；试卷究竟为专项还是套卷仍由独立的 `exam_form=special|paper|unknown` 表达。这样不会再把 `input_type=paper` 误解成“这一定是一整套套卷”。

## 3. 顶层分类与解析入口

### 3.1 一次读取，分层判定

在同一个 `SourcePackageRevision` 中，主文档和每个已确认、可读的文本附件都必须各自只做一次原文结构读取：Word 获取段落、表格、文本框、样式、颜色、自动编号、嵌入媒体锚点和定位信息；Excel 获取工作簿、工作表、表头和单元格定位信息。每个 `DocumentRevision` 产生自己的 `DocumentCorpus`，包级 `PackageCorpus`只保存以修订 ID 为键的语料映射、成员角色和安全读取诊断，绝不能把多个文件的段落串接成一条伪阅读顺序。

后续所有检测器和 Parser 共享对应成员的 `DocumentCorpus`，跨文件关联只通过显式范围和关系边发生，不能各自重新扫描文件或凭数组下标跨文件取内容。

```text
上传文件 / ImportBatch
  → 不可变源快照与 SourcePackage 分组
  → 冻结 SourcePackageRevision / parse_input_manifest
  → 每个参与成员结构读取一次（PackageCorpus）
  → 主文档顶层分类候选（词汇 / 课文 / 试卷）
  → 课文 profile 候选（仅课文；可先给出保守布局建议）
  → 录入单元候选分组与章节/大题分区
  → 试卷组织形态候选（仅试卷；基于已得到的分区与组合）
  → 类型候选抽取和唯一 owner 裁决
  → 已确认附件的答案/材料/媒体候选绑定
  → 规范化结构树
  → 音频切分映射、QuestionItem / Stimulus / ContentUnit 投影
  → 覆盖率、冲突和待确认诊断
```

`exam_form`不能作为选择题型 Parser 的前置开关：先完整识别试卷中的连续大题区块，再判断它是一个专项、一个套卷还是多个专项/多套候选。课文 `layout_profile`可以在结构切分前给出候选，但若后续结构证据不支持，必须降级或改为 `unknown_textbook`，不能强行驱动错误切分。

文件扩展名可以作为强辅助证据：`.xlsx`通常优先进入词汇通道，`.docx`通常进入课文/试卷通道；但文件名只能作为低优先级命名或补充证据，不能单独决定业务类型、试卷分类或录入单元数量。

### 3.2 顶层分类规则

| 候选类型 | 强证据 | 中等证据 | 不足以单独确认的证据 |
| --- | --- | --- | --- |
| `vocabulary` | Excel 表中存在可映射的词汇字段和有效数据行 | 工作表名含“词汇”“单词” | 文件名中含“单词” |
| `textbook` | 存在课文 Unit/章节结构，且正文呈现跟读内容类型或教材内容结构 | Unit、Section、Conversation、Reading Plus 等组合 | 单独出现 `U1` 或 `Section A` |
| `exam` | 存在明确的大题标题、答题说明、题号/选项/参考答案/录音稿等考试结构 | 分值、答题时间、考试控制词 | 文件名含“试卷”“专项” |

同一文档可能同时有课文和试卷样式线索。此时必须保留候选和证据，并令规范字段 `document_kind_decision.resolution_state=AMBIGUOUS`；旧链路如仍需要 `document_kind_status=conflict`，只能由该规范决定投影得到。只有顶层分类裁决完成后，才允许派生旧工作流的 `input_type_status`。不能为了让流程继续而静默选其中一个。

建议的输出字段如下：

```json
{
  "document_kind": "exam",
  "document_kind_decision": {
    "value": "exam",
    "resolution_state": "CANDIDATE",
    "decision_source": "parser",
    "confidence_band": "HIGH",
    "evidence": [
      {"kind": "major_heading", "value": "第一节 听后选择", "block_id": "p:12"},
      {"kind": "question_options", "count": 8, "block_ids": ["p:15", "p:16"]}
    ],
    "candidates": [
      {"code": "exam", "score": 0.95},
      {"code": "textbook", "score": 0.08}
    ]
  },
  "document_kind_status": "suggested"
}
```

`HIGH`且无冲突时可以作为默认候选，但不等同于用户确认。`document_kind`、课文 profile、单元边界、答案绑定和外部录入字段默认不得因置信度而自动变成 `CONFIRMED`；只有明确列出的低风险字段，才可由有版本记录的策略自动确认，并写入 `decision_source=policy` 和策略版本。`MEDIUM`、`LOW`或候选冲突时应在核对页展示来源与依据，并允许用户覆盖。

### 3.3 非文本结构与媒体资产

表格、图片、文本框和嵌入对象是源文档事实，不是“清洗失败的文本”。结构读取层应把它们作为一等 `SourceBlock`：

| SourceBlock 类型 | 必须保留的信息 | 典型用途 |
| --- | --- | --- |
| `paragraph` | 原文、样式、编号、段落位置 | 标题、题干、录音稿、课文正文 |
| `table` / `table_cell` | 表格索引、行列位置、文本和样式 | 听后记录表、选择题选项、框内英文 |
| `textbox` | 锚点、文本、样式和相对顺序 | Word 中的浮动说明、题号、答案 |
| `image` / `drawing` | 关系 ID、内容哈希、尺寸、锚点和可访问文本 | 图片材料、外部录入所需的表格截图 |
| `audio` / `video` / 嵌入媒体 | 关系 ID、媒体哈希、MIME、大小、锚点、可读文件名和提取状态 | 原始录音、视频材料、人工提供的音频 |

图片不能被自动 OCR 后替换原始内容；如未来启用 OCR，OCR 文本必须作为可追溯的派生候选，包含工具版本、置信度和原始图片关联。需要上传 Word 表格图片的外部录入场景，应从 `table_index` / `block_id` 派生图像产物，而不是用最终文本重新猜测表格位置。

嵌入媒体必须先作为独立 `MediaAsset` 保存（记录 `media_origin=SOURCE_EMBEDDED`），再由明确的关系边绑定到 `Stimulus`、`QuestionItem` 或 `ContentItem`。媒体文件名、出现在题目附近或与标题同名，都不足以证明它服务哪个内容；未能可靠关联时保留媒体和 `media_relation_ambiguous` 诊断，但默认不替代 TTS，也不得自动上传到外部平台。

### 3.4 多文件来源包与附件

`SourcePackage` 表示一组逻辑上属于同一份业务资料的来源容器，不是第四种业务文档类型，也不等同于一次上传动作。即使用户只上传一份 Word 或 Excel，也应形成一个仅含主文件的来源包；有答案、听力稿、原始音频或补充材料时，它们是同一包内的附件，而不是被拼接成一份虚假的源文档。

#### 3.4.1 上传批次不等于来源包

`ImportBatch`只表示一次拖拽、选择或 API 上传动作；它是传输/UI 边界，不是业务关联证据。一个批次可以产生多个彼此独立的 `SourcePackage`，也可以留下尚未归属任何包的文件：

```text
ImportBatch
├─ SourcePackage A（主试卷 + 已确认答案）
├─ SourcePackage B（独立课文）
└─ UnassignedSource*（待用户关联或单独建立来源包）
```

同一上传批次、同名目录、相邻时间或相似文件名都不能把两个文件自动拼成一个来源包。每个来源包恰有一个 `PRIMARY_DOCUMENT`；若一次上传中有两份可独立解析的课文/试卷/词汇表，应创建两个包，而不是让其中一份降级成附件。确实由多个文件共同构成一份业务内容时，用户或可解释的强结构证据必须选定规范主文档，其余文件才可作为附件进入该包。这样既允许“一次上传多份资料”，也不会让一个附件意外改变另一份文件的分类、答案或音频。

每个 `SourcePackageRevision` 都是不可变的成员与关联快照。它至少保存一个且仅一个已确认的 `PRIMARY_DOCUMENT`，以及可选附件：

| 成员角色 | 可承载的来源 | 对解析结构的权限 |
| --- | --- | --- |
| `PRIMARY_DOCUMENT` | 一个 `DocumentRevision` | 唯一决定 `document_kind`，并可产生顶层 `InputUnit` |
| `ANSWER_KEY` | Word、Excel、文本等 `DocumentRevision` | 仅补充答案、解析、评分说明等参考字段；不能自行产生录入单元或朗读正文 |
| `LISTENING_SCRIPT` | 独立录音稿、文本稿 `DocumentRevision` | 经显式内容关联后可补充 `Stimulus`；是否进入 TTS 仍由题型/音频策略决定 |
| `SUPPORTING_DOCUMENT` | 图片说明、补充材料、教师备注等 `DocumentRevision` | 只能通过明确关系补充已有实体，不得改变主文档顶层分类 |
| `MEDIA` | 独立上传的 `MediaAsset` | 只能作为候选媒体或已确认绑定；复用规则仍遵循 7.2.1 |

成员关联必须带 `association_status=SUGGESTED|CONFIRMED|REJECTED`、提议角色、关联证据和操作者/时间。相同文件名、同一上传批次、相邻上传时间、目录位置或“答案/录音”字样最多形成 `SUGGESTED`，绝不能自动确认。只有 `PRIMARY_DOCUMENT` 与已确认、且角色允许参与解析的附件进入 `parse_input_manifest`；被拒绝或尚待确认的附件保留给核对页和诊断，但不会悄悄填充答案、替换 TTS 或影响缓存结果。

建议的不可变清单形态如下：

```json
{
  "source_package_revision_id": "package-revision:...",
  "primary_document_revision_id": "revision:primary-doc",
  "members": [{
    "member_id": "member:primary",
    "member_kind": "DOCUMENT",
    "document_revision_id": "revision:primary-doc",
    "media_asset_id": null,
    "role": "PRIMARY_DOCUMENT",
    "association_status": "CONFIRMED",
    "content_hash": "sha256:...",
    "association_evidence": ["user.selected_primary"]
  }],
  "parse_input_manifest_hash": "sha256:..."
}
```

`parse_input_manifest_hash`由所有实际参与解析的成员内容哈希、角色、确认状态、成员排序和关联决定计算；仅新增一个未确认附件时可以复用既有解析，主文件或已确认附件的字节、角色或关联发生变化时必须产生新的输入清单和解析结果。跨文件来源只能引用当前输入清单中的成员：例如主试卷的 `QuestionItem` 可以以 `role=answer_key` 指向答案文档中的范围，但必须保存该关联证据；答案附件不能因为自身存在就自动变成新的试卷或新的题组。

#### 3.4.2 附件的实体级绑定与冲突

来源包中的成员角色只说明“该文件能否参与本次解析”，不说明“它的哪一条答案对应主文档的哪一道题”。这层关系必须单独以 `ReferenceBinding` 表达，避免把“答案文件已确认”错误理解成“所有题号都已正确匹配”。

```json
{
  "reference_binding_id": "reference-binding:...",
  "parse_revision_id": "parse-revision:...",
  "target": {
    "entity_kind": "QUESTION",
    "entity_id": "question:...:3",
    "field_code": "answer"
  },
  "reference_kind": "ANSWER|EXPLANATION|SCORING_RUBRIC",
  "source_ranges": [{
    "range_id": "range:answer-3",
    "document_revision_id": "revision:answer-key",
    "role": "answer_key",
    "start": {"block_id": "p:18", "char_offset_utf16": 0},
    "end": {"block_id": "p:18"}
  }],
  "value": {"kind": "single_choice", "value": "B"},
  "resolution_state": "CANDIDATE|CONFIRMED|REJECTED|AMBIGUOUS|UNRESOLVED",
  "decision_source": "parser|user|policy|migration",
  "match_method": "explicit_key|structural_key|user",
  "evidence": ["same-major-section", "question-stem-fingerprint"],
  "decision_structure_revision_id": null
}
```

解析器只能在 `ParseRevision` 中生成候选绑定；用户确认、拒绝或手工重新配对时，创建新的 `StructureRevision` 并写入 `decision_structure_revision_id`。题号、同一行序或相同文本只能是辅助证据，尤其不能跨题组、跨 Unit 或编号重启的区域自动配对。只有一个明确选中的 `CONFIRMED`绑定才可把附件值投影为正式 `answer` / `reference` 字段；候选值和冲突值保留用于核对，但不得进入 `tts_text`，也不得作为外部录入的必填字段来源。

主文档内已有的答案、已确认答案附件或人工答案若给出不同值，必须保留各自来源并输出 `reference_conflict`，不能按文件顺序、颜色、文件名或最后写入时间静默覆盖。冲突字段使受影响目标的 `external_input=false`；若内容朗读不依赖该字段，音频仍可按其自身策略继续。独立 `LISTENING_SCRIPT` 与主文档 `Stimulus` 的配对也应采用相同的显式关系和冲突规则，只是其结果是材料关联而非答案字段。

文件级成员角色/确认状态属于 `SourcePackageRevision`，会改变 `parse_input_manifest_hash`；实体级 `ReferenceBinding` 的候选与用户决定分别属于 `ParseRevision` 和 `StructureRevision`，不应为了用户确认一条答案而重新读取全部文件。只有用户改变了附件本身、附件角色或是否让它参与解析，才创建新的来源包修订和新的解析结果。

## 4. 三类文档的详细策略

### 4.1 词汇：Excel 是独立通道

词汇文档不需要经过试卷题型检测，也不应套用课文的 Unit/Section 规则。

#### 4.1.1 解析目标

```text
Workbook
└─ Sheet
   └─ InputUnit（`unit_kind=VOCABULARY`；展示上可称 VocabularyUnit/课次/词汇组）
      └─ VocabularyEntry
         ├─ word
         ├─ pronunciation / phonetic（如有）
         ├─ meaning / note（如有）
         ├─ example_sentence（如有）
         └─ source_locator（sheet + row + column）
```

#### 4.1.2 规则

- 通过表头别名建立列映射，例如“单词”“单词名称”“英文”“例句”等；不能依赖固定列号。
- 每个工作表独立判断是否为有效词汇表，允许一个工作簿包含多个有效工作表。
- 空行、说明行、合并标题行和明显不完整行必须保留诊断，不得误生成空内容音频。
- 面向用户的 `source_locator`应展示到 `sheet_name + row + column`；规范 `SourceRange`仍以 `sheet_ordinal + row + column` 的 `block_id`为准，工作表改名不能改变稳定锚点。
- 默认只把可见工作表作为词汇候选；隐藏/非常隐藏工作表、隐藏行列必须保留 `visibility_state`和诊断，可由 profile 或用户明确纳入，但不能因“有单词”自动混入朗读或外部录入。纳入后仍使用同一稳定单元格锚点，不因显示状态变化重建 ID。
- 单词和例句是内容字段或内容条目，不应被误升格为新的顶层文档类型。
- 当前首期支持的工作簿格式以 `.xlsx` 为准；`.xls`、`.csv` 或损坏工作簿在未接入对应读取器前必须显式标记为 `unsupported_source_format`，不能因扩展名相近而伪装成空词汇文档。

### 4.2 课文：版本差异通过 profile 管理

课文是当前最复杂的领域。复杂度来自真实业务：出版社、年级、册别、版式、单元组织、章节标题、对话和文章结构都可能不同。复杂并不意味着要把每一个版本写成独立 Parser。

#### 4.2.1 课文画像

`TextbookProfile`需要将“教材身份”和“可执行的格式规则”分开保存：

```json
{
  "identity": {
    "publisher": {
      "value": "renjiao",
      "resolution_state": "CANDIDATE",
      "decision_source": "parser",
      "confidence_band": "MEDIUM",
      "evidence": [{"kind": "heading", "value": "Section A"}]
    },
    "edition": {
      "value": null,
      "resolution_state": "UNRESOLVED",
      "decision_source": "parser",
      "confidence_band": "LOW",
      "candidates": [{"value": "2024", "score": 0.52}],
      "evidence": []
    },
    "school_stage": {
      "value": "junior",
      "resolution_state": "CANDIDATE",
      "decision_source": "parser",
      "evidence": []
    },
    "grade": {
      "value": "7",
      "resolution_state": "CANDIDATE",
      "decision_source": "parser",
      "evidence": []
    },
    "term": {
      "value": "first",
      "resolution_state": "CANDIDATE",
      "decision_source": "parser",
      "evidence": []
    }
  },
  "layout_profile": {
    "value": "renjiao_section_ab",
    "profile_version": "1",
    "resolution_state": "CANDIDATE",
    "decision_source": "parser",
    "confidence_band": "MEDIUM",
    "evidence": [
      {"kind": "heading", "value": "Section A"},
      {"kind": "heading", "value": "Reading Plus"}
    ]
  }
}
```

教材身份的每一个字段与 `layout_profile` 都是独立的 `FieldDecision`，不能用一个 profile 总状态掩盖“布局较可信、年级/册别未知”之类的真实情况。布局候选可以在结构切分前保守使用，但不得反向把它本身当作出版社、年级或册别已确认的证据；外部录入只消费自己实际需要且已经确认的身份字段。

字段含义：

| 字段 | 用途 | 规则 |
| --- | --- | --- |
| `identity.publisher.value` | 人教版、外研版等教材来源 | 面向业务和核对展示，单独保存候选/证据/确认状态 |
| `identity.grade`、`identity.term`、`identity.edition` | 年级、册别、版本信息 | 可分别来自正文、文件名或用户配置；一个字段未知不应连带确认其他字段 |
| `layout_profile.value` | 实际调用的结构策略 | 只描述排版/结构特征，不等同于出版社 |
| `layout_profile.profile_version` | 规则版本 | 便于基线回归与问题定位；与身份字段的确认状态分离 |

同一出版社不同年级可能使用不同 `layout_profile`；不同出版社也可能共享同一种布局。因此不能写成“人教版 = 一套固定正则”。

profile 的选择需要有明确回退链：`最具体的出版社/年级布局 → 同布局的教材基础策略 → unknown_textbook 保守策略`。回退只能降低自动结论的置信度，不能把与正文证据冲突的文档强行套入另一出版社 profile。每个 profile 还必须声明适用条件、排除条件、标题别名和测试样例，避免“未知格式默认落到某个最像的旧版本”。

字段决定负责说明“我们认为它像什么”，而 `InterpretationRevision` 必须额外冻结“本次实际按什么规则读”：`effective_layout_profile_code`、`effective_profile_version`、profile 注册表快照 hash、回退路径/原因、与读取相关的可见性策略。它们必须进入 `InterpretationRevision` 的稳定内容或 hash，从而间接进入 `parse_key`；解析运行时不得再从可变的“当前 profile 注册表”读取同名 profile。

#### 4.2.2 课文的固定层级

```text
InputUnit（U1 / Starter Unit 1 / 默认单元）
├─ Section / Chapter
│  ├─ ContentType（句子跟读 / 段落跟读 / 语篇跟读）
│  │  └─ ContentGroup（Conversation、语篇编号、文章小标题）
│  │     └─ ContentItem（完整逻辑内容）
│  │        └─ AudioSegment（句子、自然段、说话轮次）
│  └─ ContentItem（缺少内容类型标题时的兼容挂载点）
└─ unclassified（解析不确定但必须保留的原文）
```

固定规则：

1. `Unit/U/第X单元`用于命名和定位已确定的课文单元，不因出现一次 Unit 字样就直接拆单元。
2. `Section A/B`、`Understanding Idea`、`Reading for writing`、`Reading Plus`是章节，不是录入单元也不是题型。
3. `句子跟读`、`段落跟读`、`语篇跟读`是内容类型节点。
4. `Conversation 1`、`语篇 1`、文章标题是内容子组。它们可以承载多轮对话或多个自然段，但不能替代父级 Unit。
5. 说话人标签、自然段、句子属于音频切分或内容片段依据；是否一条对应一个音频由音频策略决定，不能反向破坏逻辑内容结构。

#### 4.2.3 多 Unit 识别

一个课文文件可以有多个 Unit。识别步骤必须是：

1. 先从完整文档形成章节—内容类型—正文的连续结构签名。
2. 比较是否存在两个或以上独立、重复或明显切换的完整结构块。
3. 再用 Unit 标题、分页、编号重启、标题层级和文件名补强边界与名称。
4. 无法可靠拆分时，输出一个默认 Unit 或 `multiple_candidate`，由用户在核对页拆分/合并。

以下内容绝不能单独触发新 Unit：多个 `Conversation`、多个语篇、多个文章标题、同一章节下的多个句子/段落组，或同一 Unit 内重复出现的内容类型标题。

#### 4.2.4 profile 的职责边界

| 规则类型 | 应归属的位置 | 示例 |
| --- | --- | --- |
| 文档共用结构规则 | 公共结构工具 | 段落/表格读取、编号、原文定位、空白归一化 |
| 课文共用语义规则 | `textbook` 基础策略 | Unit、章节、内容类型、说话人、正文的层级关系 |
| 版式差异规则 | `TextbookProfile` | `Section A/B`、外研版旧章节标题、特定文章分隔方式 |
| 年级或册别例外 | profile override | 某年级特有的标题别名或缺失字段兜底 |
| 音频偏好 | 独立音频策略 | 对话按角色、语篇按段落、句子逐句 |

新增一种教材格式时，应优先新增 profile、样例和回归测试。只有确实所有教材都适用的行为，才进入通用解析器。

#### 4.2.5 角色、原文与音频策略分离

课文和试卷录音稿都可能含有 `W:`、`M:`、人物名或教材角色名。解析结果必须同时保留：

- `speaker_raw`：原文标签，例如 `M`、`Ms. Li`、`Tom`；
- `speaker_role`：经过 profile 映射后的可选规范角色，例如 `male`、`female`、`narrator`、`character`；
- `voice_policy`：音频层的选择策略，例如按角色、强制女声、默认音色。

profile 只负责角色标签的识别和归一化，不能直接决定最终 TTS 声音。这样同一份课文既可以按角色生成音频，也可以在用户配置中改为统一音色，而不改变课文结构或原文内容。

### 4.3 试卷：专项和套卷是组织形态

#### 4.3.1 试卷模型

```text
ExamDocument
└─ InputUnit*                          unit_kind=ASSESSMENT；可对应一套试卷或一个专项录入单元
   ├─ exam_form: special | paper | unknown
   ├─ major_sections[]
   │  ├─ major_type: 听后选择、模仿朗读等
   │  ├─ stimuli[]
   │  ├─ question_groups[]
   │  └─ question_items[]
   └─ source_ranges[] / evidence / confidence
```

试卷场景常将 `InputUnit(unit_kind=ASSESSMENT)`简称为 `AssessmentUnit`。它是未来兼容“一个文件多套卷”的容器：当前多数套卷可产生一个，但模型必须允许多个；不能使用“源文件 ID = 试卷 ID”的一对一假设。

##### 4.3.1.1 与现有试卷配置字段的映射

本方案使用 `exam_form` 作为解析层唯一的组织形态字段；产品和外部录入层不得再单独维护另一套同义分类：

| 解析层字段 | 用户/现有方案文案 | 当前配置行为 |
| --- | --- | --- |
| `exam_form=special` | 题型专项 | 不显示或提交仅属于听说考试的 `paperType` |
| `exam_form=paper` | 听说考试 / 套卷 | 可按平台能力要求填写 `paperType`，如阶段测试、期末模拟、单元测试 |
| `exam_form=unknown` | 待确认 | 不自动提交外部平台；允许继续解析、核对和生成音频 |

已有的 `paper_category` 是兼容/界面投影，应由 `exam_form` 派生，不作为第二个可独立写入的事实字段。`paperType`描述的是完整试卷的业务用途，不是专项/套卷判断依据。

#### 4.3.2 专项与套卷的定义

| 组织形态 | 含义 | 常见结构 | 不能据此推断的内容 |
| --- | --- | --- | --- |
| `special` | 围绕一种主要考试大题型组织的录入单元 | 模仿朗读专项、听后选择专项；内部可有多个材料、多个小题型 | 只出现一个标题就一定是专项 |
| `paper` | 完整听说测试或组合试卷 | 多个考试大题型按试卷顺序组成 | 只要大题型数量大于一就一定是套卷 |
| `unknown` | 结构或命名冲突，无法安全确认 | 专项合集、残缺试卷、混合文档 | 自动随意选择其中一个 |

专项文档可以包含多个连续专项块，或者一个专项下的多种小题型；套卷也可能因缺题、格式变化而只识别出部分大题。因此，`exam_form`必须基于完整结构、总标题、答题说明、题型组合、编号重启、边界独立性等综合证据判断，不能只按大题型数量判断。

`exam_form`是完成大题分区后的组织结论，影响录入配置和单元提交方式，但不决定哪些题型 Parser 可以运行。这样残缺套卷、专项合集和未来新格式都仍能先保留完整解析事实。

当一个文档中出现多个彼此独立的专项块时，默认生成多个 `AssessmentUnit` 候选，每个候选的 `exam_form=special`；不能因为“包含多种大题型”就自动把它们合并成一套 `paper`。反之，同一套完整试卷中出现的多个 `MajorSection` 应保留在同一个 `AssessmentUnit`。两种解释无法区分时使用 `multiple_candidate`，交由用户确认，而不是静默选择。

#### 4.3.2.1 模仿朗读的版式画像与录入资格

`major_type_code=imitation_reading`只表达“这是模仿朗读材料”，不能据此生成外部录入页面。每个连续模仿朗读区块必须同时产出`major_section_profile`和候选能力；录入资格按`AssessmentUnit`或其实际外部目标判断，不能在文件级把同一份文档内的所有模仿朗读素材一并放行。

首期将已知格式冻结为以下画像。画像是解析规则和能力策略的共同键，不是 UI 可任意修改的标签：

| `major_section_profile` | 强结构证据 | 基线样例 | 音频能力 | 外部录入能力 |
| --- | --- | --- | --- | --- |
| `imitation_legacy_unit_source` | `U/Unit`单元标记，且正文按`外网`/`教材`来源标签组织，但没有可确认的题目分值段落 | 无分值的旧版 U/来源文档 | 可提取每篇材料为 `Stimulus` 并生成音频 | **固定为 `false`**；不生成录入 `page_input`，不显示文稿录入视图 |
| `imitation_unit_source_special` | `U/Unit`单元、`外网`/`教材`来源标签和带分值的`模仿朗读`大题标题；每个来源段落是一份录音稿 | `模仿朗读-7上-U5-U6.docx` | 每篇录音稿独立生成音频 | 每篇录音稿生成一个独立录入单元/试卷，题目分值取对应大题标题分值 |
| `imitation_boxed_special` | 编号朗读题干后紧邻一个可定位的框内/表格英文正文 | `七上Starter Unit1 Hello模仿朗读专项.docx` | 可生成音频 | 仅在题号、朗读正文、分值、参考文本及目标平台所需字段均已确认后为 `true` |
| `imitation_numbered_exam_special` | 编号朗读题干、操作提示和直接排版的纯英文正文构成完整题块，且不使用旧来源标签布局 | `九上Unit1模仿朗读(1).docx` | 可生成音频 | 仅在题号、朗读正文、分值、参考文本及目标平台所需字段均已确认后为 `true` |
| `imitation_unknown` | 新版式、混合版式或证据不足，无法匹配上表 | 新增或残缺文档 | 可在正文范围可靠时保守生成音频 | 默认 `false`；保留诊断和人工核对入口，但不开放外部录入 |

`major_section_profile`描述源文档版式，`entry_profile`描述外部平台页面契约，两者不可混用。首期中，三个已支持专项画像在字段确认后可映射到`entry_profile=imitation_reading_v1`；`imitation_legacy_unit_source`和`imitation_unknown`必须保持`entry_profile=null`。即使未来三个专项画像使用不同平台页面，也应注册不同的`entry_profile`，而不是让 UI 从题型名称或版式名称猜测。

这里的关键是“默认拒绝”：没有带分值大题标题的旧版 `U + 外网/教材`素材不是解析失败，而是**音频可用、录入未开放**。带明确“模仿朗读（共 N 分）”标题的同类版式由独立的`imitation_unit_source_special`画像处理，每个录音稿单独形成一个录入单元；它不会把旧画像同步升级。反过来，一份文件中即使同时包含一个已支持专项和一段旧素材，旧素材也不能继承前者的`external_input=true`。

#### 4.3.3 试卷解析顺序

1. 识别明确的大题标题、答题说明、题号、分值、录音稿和答案区域，建立连续结构块。
2. 为每个连续块生成大题型候选及证据，不直接把整份文档判为单一题型。
3. 按标题层级、内容范围、题号重启、说明重复和分页等信号，分出一个或多个 `AssessmentUnit` 候选。
4. 在每个单元内，将连续块裁决为唯一 `MajorSection` owner。
5. 由对应大题 Parser 抽取 `QuestionItem`、`Stimulus`、`QuestionGroup`、答案和音频片段。
6. 计算覆盖率；未被任何 owner 认领的块进入 `unclassified`，而不是被下一个 Parser 悄悄吞掉。

当前考试大题型和常见抽取目标如下：

| 考试大题型 | 常见结构 | 最终业务对象 |
| --- | --- | --- |
| 信息获取 | 听选信息、回答问题、录音稿 | `QuestionItem` + `Stimulus` |
| 听后选择 | 题干、选项、答案、录音原文 | `QuestionItem` + `Stimulus` |
| 听后应答 | 应答提示、应答语、答案标记 | `QuestionItem`，可带上下文材料 |
| 信息转述及询问 | 转述材料、转述任务、询问任务 | `QuestionItem` + `Stimulus` |
| 听后记录并转述信息 | 记录题、转述任务、表格或录音稿 | `QuestionGroup` + `QuestionItem` + `Stimulus` |
| 模仿朗读 | 外网/教材材料、朗读任务、分值 | 当前兼容投影为材料级 `Stimulus`；是否形成可外部提交的 `QuestionItem` 取决于朗读任务契约，不能凭文章正文自动假定 |

#### 4.3.4 共享材料与题目的关联

`Stimulus`不是一段孤立文本。一个录音稿、对话、文章或图片材料可关联零个、一个或多个 `QuestionItem`；一个复合题也可引用多个材料或材料片段。因此必须保存显式关联，而不是靠相邻顺序或 `category` 推断：

```json
{
  "question_id": "question:...",
  "stimulus_id": "stimulus:...",
  "relation_type": "primary_material",
  "ordinal": 1,
  "stimulus_ranges": [{
    "range_id": "range:stimulus-excerpt-1",
    "document_revision_id": "revision:primary-doc",
    "role": "referenced_excerpt",
    "start": {"block_id": "p:18", "char_offset_utf16": 0},
    "end": {"block_id": "p:21"}
  }],
  "evidence": ["exam.choice.script-before-questions"]
}
```

关联规则：

- 一段材料回答多道题时，建立多条关联，不能复制材料正文。
- 题干在录音稿前、录音稿后或表格中都允许；关联必须由题型规则和来源范围决定，不能只按出现顺序猜测。
- 无法可靠关联时，题目或材料保持 `AMBIGUOUS` / `UNRESOLVED`，可进入音频核对，但不得伪装成完整的外部录入题目。
- 对应关系变更会影响外部录入目标或音频范围时，必须产生新的结构/内容修订。

## 5. 检测、解析与裁决的职责边界

### 5.1 必须共享的中间协议

`detection.py`、分段器和各题型 Parser 不能再各自维护一套互不相认的“这是某题型”的判断。三者必须围绕同一份候选协议协作：

```json
{
  "candidate_id": "candidate:doc-1:block-12:listening-choice",
  "kind": "major_section",
  "type_code": "listening_choice",
  "major_section_profile": null,
  "entry_profile": null,
  "claimed_block_ids": ["p:12", "p:13", "p:14", "p:15", "p:16", "p:17", "p:18", "p:19", "p:20", "p:21", "p:22", "p:23", "p:24", "p:25"],
  "claimed_ranges": [{
    "range_id": "range:candidate-choice-1",
    "document_revision_id": "revision:primary-doc",
    "role": "candidate_extent",
    "start": {"block_id": "p:12", "char_offset_utf16": 0},
    "end": {"block_id": "p:25"}
  }],
  "evidence": [
    {"rule_id": "exam.choice.heading", "block_id": "p:12"},
    {"rule_id": "exam.choice.option_layout", "block_id": "p:16"}
  ],
  "confidence": 0.96,
  "diagnostics": [],
  "capabilities": {
    "parse": true,
    "audio": true,
    "normalize": true,
    "external_input": false
  }
}
```

规则要求：

- 检测器负责候选、范围、证据和置信度，不直接生成最终业务对象。
- Parser 只在已分配或已声明的范围内解析；它必须返回实际认领的原文块和未处理原因。
- 裁决器负责同一原文块的唯一 owner。多个候选同时命中且无法唯一决定时，标记 `AMBIGUOUS`，不得发布两份重复结果。
- 检测器与 Parser 必须调用同一套 `RuleSpec` 或共享谓词；`RuleSpec`必须位于中立规则模块，检测器不得通过 import Parser 实现复用，以免产生循环依赖。禁止“检测器要求 A+B+C、Parser 只要求 A+B”这类不一致。
- Parser 的内容抽取失败不应改写顶层分类，但必须补充诊断，例如`heading_found_but_no_question_body`。
- `major_section_profile`和`entry_profile`是 Parser 依据实际结构输出的事实；`external_input`只能由已注册的`entry_profile`和字段校验共同授予。不得根据`major_type_code`、旧`category`、`page_input.type`，或“已经生成了一份页面数据”反向推断录入资格。

这条约束直接解决“解析器本可解析某段内容，但检测器因额外条件漏检”的问题。

#### 5.1.1 Canonical block claim 与兼容过渡

目标协议中的 `claimed_block_ids`必须引用 `DocumentRevision` 内的规范 `block_id`，并允许用 `char_offset_utf16`表示同一段落中的局部范围。当前兼容链路仍有以 `source_locator` 声明 claim 的情况，这只能作为阶段性投影：

- 新结构层先生成不可变的 `SourceBlock` 序列；表格单元格、文本框和媒体锚点也必须有 block ID。
- Parser 返回 `claimed_ranges[]`，而不是只返回最终字符串或 `items` 序号。
- 裁决器以范围重叠而非纯字符串相等检查冲突；父级范围与明确子级范围可以共存，但两个同层 owner 不可重叠。
- 输出到旧链路时再生成可读的 `source_locator`，保证兼容 UI 和日志。

这一步是“各 Parser 只解析自己的范围”能够可靠落地的前提。

### 5.2 规则优先级

同一位置存在多个解释时，按以下顺序裁决：

1. 用户已确认的边界、类型或 profile。
2. 明确的正文标题、表格字段、样式标记和规范题号结构。
3. 与当前 document/profile 相匹配的结构组合。
4. 通用关键词和弱版式信号。
5. 文件名、目录名和历史默认值。

较低优先级证据只能补强，不能推翻高优先级明确结构。任何低置信度自动判断都必须允许用户在核对页修正，并保留修正前的证据。

### 5.3 覆盖率是解析完成条件

每次解析必须输出：

```json
{
  "primary": {
    "document_revision_id": "revision:primary-doc",
    "member_read_status": "READABLE",
    "source_block_count": 120,
    "claimed_block_count": 112,
    "ignored_auxiliary_count": 5,
    "unclassified_count": 3,
    "coverage_status": "partial"
  },
  "by_member": [{
    "member_id": "member:answer-key",
    "document_revision_id": "revision:answer-key",
    "role": "ANSWER_KEY",
    "included_in_parse_input": true,
    "member_read_status": "READABLE",
    "source_block_count": 24,
    "claimed_block_count": 18,
    "unbound_block_count": 6,
    "coverage_status": "partial"
  }],
  "parse_coverage_status": "partial"
}
```

主文档的 `coverage_status`固定区分：`complete`（全部业务内容已认领或有记录地忽略）、`partial`（存在未完成的可处理范围）、`unclassified`（存在明确无法归属的范围）和 `empty`（读取成功但没有可处理正文）。顶层 `parse_coverage_status`默认反映主文档，不能因答案附件尚未全部配对而把一个已完整识别的试卷降级为 `unclassified`；附件状态应从 `by_member[]`读取。`empty`只适用于已成功读取的主文档结果，不创建空 `InputUnit`；主文档读取失败则使用 `parse_status=FAILED` 和读取诊断，不伪造任何覆盖率结论。

确认参与解析的附件也必须单独计数，但它们的“已认领”含义按角色解释：`ANSWER_KEY`要求值已形成明确绑定或被记录为无主/冲突，`LISTENING_SCRIPT`要求已绑定 `Stimulus` 或保留未绑定诊断，`MEDIA`要求已有内容关系或 `media_relation_ambiguous`。附件的未绑定内容不能偷偷并入主文档 `unclassified`。如果主文档可读取但已确认附件无法读取，允许发布主文档的结构结果并将整体 `parse_status=PARTIAL`；受该附件影响的答案、材料或外部字段必须保持缺失/待确认，不能使用旧版本或猜测值补齐。

只有目标范围内的全部内容已被认领，或被明确标记为辅助内容并记录原因时，才可输出 `complete`。`partial` 和 `unclassified` 不应阻止纯音频流程，但外部录入前必须提示用户核对；一旦未完成范围恰好是平台必需的答案、材料或图片字段，则该目标的 `external_input`仍必须为 `false`。

覆盖率统计必须以不重叠的规范范围计算：一个表格既作为父块又被多个单元格认领时，不能重复计数。`ignored_auxiliary`必须携带原因代码，例如 `answer_key`、`page_number`、`instruction_only`，不能成为隐藏丢弃内容的出口。

### 5.4 解析能力、内容完整度与外部录入能力

“已解析”不等于“可以录入外部系统”。每个 Family/SubType 和每个实际候选都应独立声明以下能力：

| 能力 | 含义 | 典型阻断条件 |
| --- | --- | --- |
| `parse` | 可以识别结构和保留原文 | 结构块完全无法识别 |
| `audio` | 可以生成可审阅的朗读内容 | 缺失 `tts_text` 或音频范围不确定 |
| `normalize` | 可投影为 `QuestionItem`、`Stimulus` 或 `ContentUnit` | 关键字段无法映射 |
| `external_input` | 可构造当前外部平台所需的完整页面输入 | 缺题干、选项、答案、图片或人工确认 |

建议每个候选输出：

```json
{
  "resolution_state": "CANDIDATE",
  "capabilities": {
    "parse": true,
    "audio": true,
    "normalize": true,
    "external_input": false
  },
  "missing_required_fields": ["answer"],
  "manual_review_required": true
}
```

外部录入模块只能消费 `DEFAULT_EXECUTION` 中已选定、结果仍为 `ACTIVE`、`parse_status`为 `SUCCEEDED` / `PARTIAL`、受影响范围校验通过，且目标自身 `resolution_state=CONFIRMED`、`external_input=true` 的目标；任何一项都不能由另一项替代。纯音频或内容核对流程可以消费 `DRAFT`、`CANDIDATE`、`AMBIGUOUS` 的结构化结果，但必须展示状态和风险范围。这样不会因为已有 Parser 输出了音频文本，就误认为所有题型已经具备完整的系统录入字段。

#### 5.4.1 录入能力的单向派生与界面门禁

录入能力必须沿着以下单向链路产生，后一步不得反向成为前一步的证据：

```text
SourceBlock + 已裁决的结构范围
  → major_section_profile / entry_profile
  → Candidate.capabilities.external_input
  → 已确认的外部录入目标与 page_input
  → 录入视图、开始录入动作、外部提交
```

具体约束如下：

- `page_input`是`external_input=true`目标的受控投影，不是放行录入的证据。`external_input=false`时，适配器不得自动构造可执行的`page_input`；若核对页需要展示原文，应使用只读的 review payload，不能复用录入页面模型。
- 后端预检、文稿核对视图、系统录入配置/进度入口和实际提交必须读取同一份目标级能力投影。界面不能因`模仿朗读`名称、旧`category`、已有音频或局部页面字段而自行开放录入；后端开始动作也必须重复校验，避免陈旧投影绕过门禁。
- 前端收到缓存的`document_entry_support`时，只接受当前`schema_version`、完整的`structured_count`，以及与当前条目数量和`format`一致的`detected_types`/`expected_types`，且`invalid_count`、`major_section_profile_invalid_count`、`entry_profile_invalid_count`、`entry_capability_invalid_count`均为`0`的`supported=true`结果，并再次核对条目级版式画像/录入画像/能力；旧缓存一律回到音频核对并提示重新解析。
- 必须按`AssessmentUnit`和外部目标逐一判断。一个已支持题块不能使同一文档内的旧素材、未知题块或字段冲突题块获得录入资格。
- `external_input`的初始值一律为`false`。只有规则注册表命中明确`entry_profile`、平台必填字段完整、相关人工决定已确认、适配器可用且目标未被冲突/覆盖率诊断阻断时，才允许置为`true`。

因此，“音频可生成但不支持录入”是正常且可展示的状态，不是失败或半成品状态；例如`imitation_legacy_unit_source`应在音频核对页可见，但文稿录入区域必须保持关闭。

当前代码已先落地这条链路的最小闭环：模仿朗读 Parser 为旧版和三个已接入专项分别写入
major_section_profile、entry_profile 和 capabilities；workflow/parser.py 及页面事实构造器只为
entry_profile=imitation_reading_v1 且 capabilities.external_input=true 的目标生成 page_input；
后端预检、开始录入动作、录入运行快照、实际执行边界、核对页和系统录入入口都读取同一份门禁结果。旧版 U + 外网/教材因此仍保留音频条目，
但不会生成可执行页面事实，也不会显示文稿录入视图；恢复旧运行时若快照门禁缺失或与当前重算结果不一致，也会在外部操作前停止。完整套卷、课文、词汇及未知模仿朗读画像仍按
本文后续阶段逐步接入，不应因为顶层题型名称而自动放行。

### 5.5 标准诊断契约

`diagnostics`不能只是零散字符串。每一条可见或可统计的问题应至少包含：

```json
{
  "code": "block_owner_conflict",
  "severity": "warning",
  "stage": "adjudication",
  "rule_id": "exam.choice.option-layout",
  "candidate_id": "candidate:...",
  "affected_ranges": [{
    "range_id": "range:diagnostic-1",
    "document_revision_id": "revision:primary-doc",
    "role": "conflict",
    "start": {"block_id": "p:12", "char_offset_utf16": 0},
    "end": {"block_id": "p:16"}
  }],
  "message": "同一范围被两个大题候选认领",
  "suggested_action": "review_type_owner"
}
```

`severity`固定为 `info`、`warning`、`error`；`stage`至少区分 `load`、`classify`、`segment`、`extract`、`adjudication`、`normalize`、`external_gate`。诊断面向用户展示时使用安全摘要，质量统计时不上传或记录不必要的全文内容。

### 5.6 规范化后的结构校验

Parser 返回候选不代表结果可以发布。候选裁决后必须执行独立的结构校验器；校验器只能报告、阻断或降级状态，不能为了“让结果通过”而猜测内容。

| 校验层 | 必须成立的条件 | 失败处理 |
| --- | --- | --- |
| 范围与顺序 | 每个 `source`/`inferred`范围的端点属于同一 `DocumentRevision`，且该修订在当前 `parse_input_manifest` 内；同层兄弟顺序稳定；同级 `InputUnit` 不重叠 | `invalid_range` 或 `multiple_candidate` |
| 结构树 | 每个节点只有一个包含父级；无环；`role=primary`的源派生子节点范围不越出父级主范围集合。答案/听力稿等跨文件辅助范围必须使用受控 role、已确认附件和关联证据，不能伪装成包含关系 | `invalid_tree`，不进入下游任务 |
| 关系图 | `QuestionItem → Stimulus`、片段—产物关联均有存在的端点；默认不跨 `InputUnit`，也不得引用输入清单外的附件 | `orphan_relation` / `cross_unit_relation` / `source_package_member_invalid`，进入待确认 |
| 参考字段绑定 | `ReferenceBinding`的目标存在于当前解析/结构修订，来源范围属于允许角色的已确认附件或主文档；同一目标字段只能有一个活动的 `CONFIRMED`值，候选/冲突值不得覆盖正式字段 | `reference_binding_ambiguous` / `reference_conflict`；受影响目标禁止外部录入 |
| 小题语义 | 同一大题内题号/选项标识不重复；选择题选项顺序稳定；必填字段与 SubType 能力一致 | `question_incomplete`，仅允许音频或核对能力 |
| 文本与答案 | `raw_text`、`tts_text`、清洗轨迹完整；参考答案、评分说明和解析与朗读正文分离 | `answer_leaked_to_tts`，阻止该片段生成 |
| 下游范围 | `InputUnit`、内容、音频片段和外部目标的成员关系可回溯到同一结构修订 | `scope_membership_invalid`，不创建可执行 scope |

参考答案、红色正确项、评分说明和教师备注属于结构化 `answer` / `reference` / `auxiliary` 字段，默认不得进入 `tts_text` 或音频任务。只有题型规则明确声明“参考应答本身需要朗读”时，才可生成独立内容条目并留下对应规则证据。

校验结果应输出 `validation_status=valid|needs_review|invalid`，但该顶层字段只是最严重结果的摘要；规范事实是 `validation.by_scope[]`，其中每个 `InputUnit`、题组、题目或音频片段范围都有自己的状态和诊断 ID。`valid`只说明该范围通过结构/字段约束；要生成完整下游范围，仍须同时满足当前选择、解析终态、字段裁决和目标能力等发布资格。`needs_review`可保留音频核对或人工修订入口；`invalid`保留原文和诊断，但不为**受影响范围**创建音频或外部副作用任务。多单元文档中，其余边界独立且校验通过的 `InputUnit` 可以继续处理；整体覆盖状态相应标为 `partial`，并要求用户确认是否移出无效范围。下游任务只能读取自身 `by_scope` 状态，不能因顶层摘要为 `invalid` 就误阻断无关范围。

### 5.7 阅读顺序、性能与资源边界

Word 的 XML 顺序、表格内部顺序和浮动对象的视觉位置并不总是一致。结构读取应采用确定性阅读顺序：

1. 正文段落和内联表格按文档 body 顺序建立 `SourceBlock`。
2. 表格内部按行、列、单元格段落顺序建立子范围，且保留父表格关系。
3. 文本框、图片和浮动对象按锚点段落和关系 ID附着；锚点无法确定或视觉顺序冲突时不强行插入正文顺序，而是标记 `floating_object_order_ambiguous`。
4. 页眉、页脚、页码、水印和批注默认作为辅助内容排除，除非 profile 明确声明它们是业务正文。

读取层还必须保存可见性与修订状态，而不是只输出一条“看起来像正文”的纯文本流：

- Word 默认以最终可见视图读取：保留已接受/插入后的正文，排除删除、移动出、隐藏文本、批注及修订气泡中的内容；它们保留为可审计的辅助块和原因码，默认不得参与 `tts_text`、答案抽取或外部字段。
- 若读取库无法可靠区分最终正文与修订/隐藏内容，输出 `revision_visibility_ambiguous`，受影响范围只允许核对，不允许自动生成音频或外部目标。profile 想读取页眉、脚注、隐藏文本或批注时必须显式声明用途，并经过同样的答案泄漏校验。
- Excel 的工作表、行列可见状态同样进入 `SourceBlock` 元数据。隐藏不等于删除，也不等于可自动朗读；是否纳入词汇候选由 4.1.2 的可见性策略和用户确认决定。

性能规则：

- 每个源修订只读取一次，建立可按标题、块类型、编号、样式和语言查询的索引；Parser 只能扫描已分配范围。
- 规则评估必须有输入长度、文档块数、表格单元格数和递归层数上限；超过上限时输出 `resource_limit` 诊断，而不是长时间卡住或部分静默丢弃。
- Rule Catalog 中的正则和自定义谓词必须使用受控输入、基线性能样例和超时/预算策略；不接受用户提供的可执行规则。
- 可缓存的解析结果以 5.8 定义的 `parse_key` 为键；规则集、读取器或用户解释修订变化后创建新结果，不覆盖旧缓存。

#### 5.7.1 不可信源文件的读取安全边界

DOCX/XLSX 都应视为不可信压缩容器，而非可以直接解压和执行的“普通文档”。在交给 Word/Excel 读取库前，源文件接入层必须做到：

- 校验文件签名、压缩目录、文件数、单项和总解压大小、压缩比及嵌套深度；超过预算输出 `archive_budget_exceeded`，不进入解析器。
- 只读取允许的 OOXML 内容与媒体条目，拒绝绝对路径、路径穿越和异常关系目标；嵌入文件先进入受限 Artifact 存储，再按 MIME、哈希和大小处理。
- 不执行宏、VBA、OLE、公式、脚本或嵌入对象；Excel 公式只作为原始公式和已缓存显示值保存，不能在服务器端求值。
- 不主动访问文档中的外部关系、远程图片、超链接或模板地址。外部 URL 只能作为不可信元数据保留；需要下载时必须经过独立、显式授权的连接器流程。
- 所有拒绝、忽略或降级行为都写入诊断（例如 `macro_ignored`、`external_relationship_blocked`），不能仅返回一个没有内容的成功结果。

这些限制不会改变业务解析结论：安全层只决定“能否安全读取哪一部分源”，不会擅自把外部资源内容补进课文、词汇或试卷结构。

#### 5.7.2 不可变源快照、保留与可复现性

文件名、临时上传路径、用户本地文件的最后修改时间都不能作为 `DocumentRevision` 的唯一事实。接入层在读取前必须把每个主文档、已确认附件和独立媒体保存为不可变的 `source_artifact_id` / `media_asset_id`，并记录字节哈希、大小、MIME、原始展示名、接入时间和安全读取状态。`DocumentRevision`引用该不可变源快照；每次读取前校验实际字节哈希，校验不一致时输出 `source_integrity_mismatch`，不得沿用旧的 `SourceBlock`、解析缓存或音频绑定。

底层可以按内容哈希做物理去重，但逻辑来源身份、包成员关系和访问控制不得随之合并：同一份字节被两个用户/项目上传时，仍是两个独立的来源引用，不能因为去重而让一方看到另一方的文件名、诊断、答案或媒体关系。`SourcePackageRevision`、`ParseRevision`、`StructureRevision`、音频验收和已经创建的 `OperationScopeRevision` / `input_run` 都应对所需源快照建立保留引用；正常清理只能清理未被任何活动或历史事实引用的副本。

用户从当前工作区移除文件时，默认只解除当前包/页面的可见关联，不得物理删除已被历史解析、音频或外部回执引用的源快照。若合规保留策略最终使历史源字节不可再访问，系统必须保留其哈希、修订/范围摘要和删除原因，并标记 `source_snapshot_unavailable`：旧结果可作为历史记录查看，但不得声称可完整复跑、重新定位全文或重新生成依赖该源的音频。

### 5.8 解析执行、缓存与发布边界

`DocumentRevision`只表示某一个源文档的字节和读取事实，不能直接等同于一次解析结果；`SourcePackageRevision`则表示主文档与附件的成员、角色和确认关系。同一份有效解析输入可能因为规则升级、用户强制选择课文 profile 或修复读取器而产生多个合法解析结果；这些结果也不能直接覆盖用户结构编辑或已执行任务。

目标版本链固定为：

```text
SourcePackage（稳定的逻辑导入身份）
  └─ SourcePackageRevision（不可变成员/角色/关联快照）
       ├─ PRIMARY_DOCUMENT → SourceDocument → DocumentRevision（不可变源字节 / SourceBlock）
       ├─ CONFIRMED 附件 → SourceDocument → DocumentRevision*
       ├─ CONFIRMED 媒体 → MediaAsset*
       ├─ parse_input_manifest_hash
       └─ InterpretationRevision（分类、profile、读取策略及用户强制选择）
            ├─ ParseRun（一次可复用的执行；只承载运行态）
            │    └─ ParseRevision（某版本读取器 + schema + 规则集的终态结果）
            │         └─ 基线 StructureRevision → 用户 StructureRevision*
            └─ ParseSelection / CurrentParseSelection（按选择范围选定不可变解析结果及其可选结构；保留追加式选择历史）
                 └─ selected ParseRevision + StructureRevision?（失败/取消时为 null）
```

| 对象 | 何时新建 | 不应因何事新建 |
| --- | --- | --- |
| `SourcePackageRevision` | 成员增删、角色/确认状态/关联证据变化，或任一成员改为新的文档/媒体版本 | 纯 UI 展开、改音色、改外部平台字段 |
| `DocumentRevision` | 某一个 Word、Excel 或文本来源的字节发生变化 | 改附件角色、改音色、改外部平台字段、仅更改规则版本 |
| `MediaAsset` | 独立上传或提取的媒体字节发生变化（新内容使用新的 asset ID） | 改文件展示名、改音色或仅调整外部模板 |
| `InterpretationRevision` | 用户强制 `document_kind` / profile、改变读取策略或显式解析入口 | 改单元名称、顺序、音频配置 |
| `ParseRun` | 有新的解析请求且没有可复用的同 key 活动/终态结果时 | 纯 UI 展开、音频生成、外部重试或用户只改显示名称 |
| `ParseRevision` | `ParseRun`结束后固化一次终态结果；输入、解释、读取器/schema 或规则集变化会产生新的候选结果 | 纯 UI 展开、音频生成或外部重试 |
| `ParseSelection` | 在一个 `selection_scope` 中选择已完成 `ParseRevision` 及其 `StructureRevision`（成功/部分成功时必有；失败/取消时为空）为默认，或当前输入/解释/结构选择变化导致选择上下文切换 | 解析内容本身、用户结构编辑、音频或外部重试 |
| `StructureRevision` | 每个 `SUCCEEDED` / `PARTIAL` 的 `ParseRevision` 先生成一个基线结构修订；其后用户拆分、合并、移动、确认边界/owner 或改写内容时追加下一版 | 仅重新运行同一解析、仅改导出文件名 |

`InterpretationRevision`可首期实现为不可变的解释配置快照，而非独立物理表；但它必须有稳定 ID 或 hash，不能只读“当前 UI 选择”。每个可审阅的 `SUCCEEDED` / `PARTIAL` 解析结果都必须同时创建一个从 `ParseRevision` 派生的基线 `StructureRevision`（可记为 revision 0）：它不表示用户确认，只把规范解析结构作为后续用户决定和 `OperationScopeRevision` 的统一冻结起点。`FAILED` / `CANCELLED` 不创建基线结构修订。每个 `StructureRevision` 至少记录 `base_parse_revision_id`、`parent_structure_revision_id`（基线为 null）、`caused_by_review_decision_id`（基线为 null）和规范 `structure_result_hash`；它不得跨 `ParseRevision` 继承父级或复用另一份输入清单的范围。`ParseRevision`至少应保存：

```json
{
  "parse_run_id": "parse-run:...",
  "parse_revision_id": "parse-revision:...",
  "parse_input_manifest_hash": "sha256:...",
  "primary_document_revision_id": "revision:primary-doc",
  "interpretation_revision_id": "interpretation:...",
  "reader_version": "docx-reader/v2",
  "parser_schema_version": "document-parse/v1",
  "rule_set_version": "2026.09",
  "parse_key": "sha256:...",
  "parse_status": "SUCCEEDED|PARTIAL|FAILED|CANCELLED",
  "validation_status": "valid|needs_review|invalid",
  "result_hash": "sha256:...|null",
  "diagnostics_hash": "sha256:..."
}
```

其中 `parse_key` 必须由 `parse_input_manifest_hash + interpretation_revision_id + reader_version + parser_schema_version + rule_set_version` 计算。`InterpretationRevision` 的稳定内容必须包含实际路由所用的 `document_kind`、有效 profile code/version/注册表快照 hash、回退路径和读取策略，不能只保存用户界面上的显示名称。`parse_input_manifest_hash`包含实际参与解析的主文档/已确认附件的内容哈希、角色和关联；答案附件或独立录音稿一旦被确认、替换或解绑，就会自然产生新的 key。仅增加、重命名或拒绝一个不在输入清单内的候选附件，不必无意义地重新解析 Word/Excel。音色、导出命名、外部平台字段等下游配置同样不属于该 key。`result_hash`只对 `SUCCEEDED` / `PARTIAL` 的规范结构结果有值；`FAILED` / `CANCELLED` 必须保存 `diagnostics_hash` 和安全元数据，不能伪造一份空结构的结果哈希。`freshness_status` 不属于不可变 `ParseRevision` 行；它由冻结结果与 `ParseSelection` 事件/当前指针计算后投影给 UI 和下游。

#### 5.8.1 同 key 幂等、并发与过期发布

解析本身可以耗时，但发布结果必须像外部录入一样具有明确的幂等和并发规则：

1. 对同一个 `parse_key`，同时到达的请求只允许一个活动 `ParseRun`；其余请求复用进行中的运行。已校验的 `SUCCEEDED` / `PARTIAL` 不可变 `ParseRevision` 可以直接复用；`FAILED` / `CANCELLED` 只复用诊断展示，用户或调度器显式重试时创建带 `retry_of_parse_run_id` 的新 `ParseRun`，但仍不得与同 key 活动运行并发。
2. 每次运行在开始时冻结解释、读取器/schema 和规则集版本；运行中不得读取“当前最新 profile”或当前 UI 表单。
3. 解析、候选裁决和结构校验在草稿结果上完成；`SUCCEEDED` / `PARTIAL` 结果先生成基线 `StructureRevision`，随后才以 compare-and-set 追加 `ParseSelection` 事件并更新 `CurrentParseSelection` 指针，使其指向该不可变 parse + structure 对。`FAILED` / `CANCELLED` 如属于当前选择上下文，也可用同一机制选定其诊断性 `ParseRevision`，但 `selected_structure_revision_id=null`。结果外壳据此投影 `freshness_status=ACTIVE`，而不会修改 `ParseRevision` 本身。
4. 若运行期间用户创建了更新的 `InterpretationRevision`，或来源包产生了内容/角色/关联均已变化的新 `parse_input_manifest_hash`，当前选择上下文随之切换；旧运行仍可保留诊断与结果，其结果视图必须投影 `freshness_status=STALE`，不得覆盖新的默认结构，也不得触发音频或外部任务。只改变未参与输入清单的候选附件时，可继续复用原结果。
5. 运行失败、取消或达到资源上限时，必须保留失败诊断和已读取的安全元数据；不能发布“空的 complete 解析结果”，更不能删除之前已发布的解析/结构修订。
6. 若在基线复跑、缓存修复或灾备重算中，同一个 `parse_key` 的两个 `SUCCEEDED` / `PARTIAL` 结果得到不同 `result_hash`，必须报告 `parser_non_deterministic` 并阻止自动替换；不得按完成时间任选一份作为权威结果。一次失败后重试成功本身不是非确定性，但其失败诊断和重试链必须可追溯。

解析层的条件发布需要与现有 workflow 一样使用版本围栏或 compare-and-set；“最后完成的任务覆盖先完成的任务”不是正确的发布策略。`sync_projection`、音频计划和系统录入只能消费 `DEFAULT_EXECUTION` 选择范围中明确选定、结果视图为 `freshness_status=ACTIVE` 且 `parse_status`为 `SUCCEEDED` 或 `PARTIAL` 的 `ParseRevision + StructureRevision` 组合；不得再用“`parse_status`不是 `STALE`”这种混合条件。

#### 5.8.2 源文件读取失败的受控结果

读取失败也是解析结果的一部分，必须有明确诊断代码和可恢复策略：

| 情况 | 必须输出 | 是否允许进入下游 |
| --- | --- | --- |
| 不支持的扩展名或读取器未接入 | `unsupported_source_format` | 否；可提示转换或接入读取器 |
| 主文档损坏、截断或无法打开的 DOCX/XLSX | `source_corrupt` | 否；保留源文件身份和错误摘要 |
| 主文档加密/受密码保护 | `source_password_protected` | 否；不得把密码错误伪装为零内容 |
| 源快照字节与登记哈希不一致 | `source_integrity_mismatch` | 否；不能复用旧 `SourceBlock`、解析或音频结果 |
| 历史源快照已按保留策略不可访问 | `source_snapshot_unavailable` | 旧结果仅可作为历史查看；不可完整复跑、重新定位全文或生成依赖该源的新音频 |
| 超过解析预算 | `resource_limit` | 否；可允许用户降低范围或使用人工拆分 |
| 合法文件但没有可处理正文 | `no_usable_content` + 覆盖率 `empty` | 仅允许查看诊断，不创建空 `InputUnit` |
| 附件角色或关联尚未确认 | `companion_association_ambiguous` | 可解析主文档，但不得读取该附件填充答案、材料或音频 |
| 已确认附件损坏、受保护、超预算或读取器未接入 | `companion_source_unavailable` + 对应读取诊断 | 主文档可发布 `PARTIAL` 结构结果；该附件不得提供答案、材料、图片或音频字段 |

这些状态和 `unknown` / `ambiguous`不同：后两者说明源已读取但业务含义待确认，仍可保留可审阅的结构；读取失败则没有足够的结构事实，不得被“默认单元”掩盖。

#### 5.8.3 重解析身份匹配与拆并规则

`atomic-question-model-plan.md` 6.2.1 已定义 `QuestionItem` / `Stimulus` 的两阶段 revision match。本方案将同一原则扩展到所有会影响核对、音频或外部录入的实体；不能只因“文本一样”或“都叫 U1”就延续历史身份。

| 实体 | 自动匹配的强锚点 | 仅能作为辅助证据，不能单独匹配 |
| --- | --- | --- |
| `InputUnit` | 同一 `unit_kind`、已匹配的上级来源、独立结构边界签名、用户确认的业务标识 | `U1` / `Unit 1` 标题、同级 ordinal、文件名 |
| `StructureNode` | 已匹配父级、同一 `node_kind`、标题/题型规范键、来源范围与相邻锚点 | 中文显示名称、单独的 Section 编号 |
| `ContentItem` / `ContentUnit` | 已匹配 `InputUnit`、内容类型、局部键/来源范围、规范化正文指纹 | 句子正文、行号或音频文件名 |
| `QuestionItem` / `Stimulus` | 按原子方案的题型、结构范围、题干/选项指纹、材料关系和稳定业务键 | 题号、`source_locator`、文本 hash 的任一单项 |

匹配仍分为两阶段：先做可证明的一对一确定性匹配，再对剩余实体按“实体类型 + 已匹配祖先 + 局部结构锚点 + 内容/关系指纹”产生候选。候选只有一对一、超过固定阈值且没有类型/范围冲突时才可自动沿用逻辑 ID；否则标记 `AMBIGUOUS`，等待人工裁决。匹配结果统一至少包含 `MATCHED`、`NEW`、`REMOVED`、`CHANGED`、`AMBIGUOUS`，并保存算法版本、候选分数、证据、裁决人和时间。

现有 `revision_match_decisions` 以 `question_id` 为中心，不能把 `InputUnit`、`StructureNode` 或课文 `ContentUnit` 伪装为题目写进去。迁移时应保留该表的题目兼容记录，并新增通用的 `entity_match_decisions`（或等价的按实体类型关联表），至少包含 `entity_kind`、旧/新逻辑 ID、旧/新 `ParseRevision`、候选集、算法版本和人工裁决。这样历史查询仍能读旧表，新结构也有完整审计链。

拆分和合并是高风险边界，固定规则如下：

- 一个旧实体匹配到多个新实体，或多个旧实体匹配到一个新实体时，默认不自动沿用任一逻辑 ID，也不复用音频验收、外部记录或操作 scope。
- 仅对 `InputUnit`，用户明确指定“主单元”且未产生外部副作用时，才可按[系统录入整合方案](system-input-integration-plan.md)延续主单元 ID；其余新成员仍必须新建身份和结构修订。
- 已有外部回执的旧实体始终保留为历史事实。即使人工确认新旧内容业务上相关，也只能创建新的 scope/run 或由外部适配器明确执行更新，不能把旧回执迁移到新实体。
- 相同文本出现在不同 Unit、不同题组或不同父级时默认是不同实体；相同 `source_locator` 在不同 `DocumentRevision` 中也默认不是同一实体。

#### 5.8.4 状态轴、合法组合与发布资格

一个状态字段只能回答一个问题。不能把“解析成功、内容完整、用户确认、当前版本、可录入”揉成一个 `status`，否则重解析、人工核对和任务调度一定会互相覆盖。规范状态轴如下：

| 状态轴（归属） | 枚举/形态 | 回答的问题 |
| --- | --- | --- |
| 附件关联（`SourcePackageRevision.member`） | `SUGGESTED`、`CONFIRMED`、`REJECTED` | 这个来源是否以某角色加入来源包；不代表已读取、已绑定或可用 |
| 成员读取（`ParseRevision.coverage.primary/by_member`） | `NOT_REQUESTED`、`READABLE`、`FAILED`、`UNAVAILABLE` | 这一轮、这一读取器能否安全读取该成员；只有 `READABLE` 才能给出覆盖率 |
| 运行生命周期（`ParseRun`） | `QUEUED`、`RUNNING`、`CANCEL_REQUESTED`、`SUCCEEDED`、`PARTIAL`、`FAILED`、`CANCELLED` | 后台执行是否仍在运行；它不是解析结构事实 |
| 解析终态（`ParseRevision.parse_status`） | `SUCCEEDED`、`PARTIAL`、`FAILED`、`CANCELLED` | 该不可变结果是否产生了可审阅的结构；`PARTIAL` 可保留未受影响范围 |
| 新鲜度/选定状态（`ParseSelection` / 输出视图） | `ACTIVE`、`STALE`、`SUPERSEDED` | 在指定 `selection_scope` 中，该解析结果及其可选结构是否仍是当前输入/解释下被选定的默认结果 |
| 结构校验（`validation.by_scope[]`；`validation_status` 为摘要） | `valid`、`needs_review`、`invalid` | 结构和字段约束是否通过；按受影响范围阻断，而不是覆盖解析结果 |
| 覆盖率（`coverage_status`） | `complete`、`partial`、`unclassified`、`empty` | 已读正文/附件范围是否被认领；不是运行成功与否 |
| 字段或实体裁决（`resolution_state`） | `DRAFT`、`CANDIDATE`、`AMBIGUOUS`、`UNRESOLVED`、`CONFIRMED`、`REJECTED` | 某个值、边界、绑定或实体是否被确认；不是整份文档的执行状态 |
| 下游能力（`capabilities.*`） | 独立布尔能力和缺失字段 | 当前目标能否生成音频、规范化或外部录入；不能由单个全局状态推断 |
| 音频/外部执行 | 继续归 `OperationTask` / Attempt / `input_run` | 任务有没有真实副作用；不能写回解析或结构状态 |

`freshness_status` 必须带 `selection_scope` 才有意义。`DEFAULT_EXECUTION` 是唯一允许创建真实音频/外部任务的范围；`PREVIEW:<user-or-session>` 等预览范围可以并存，却不能抢占默认执行选择。每个 `source_package_id + selection_scope` 至多有一个 `CurrentParseSelection` 指针，且它必须保存 `selected_parse_revision_id`；当该结果为 `SUCCEEDED` / `PARTIAL` 时还必须保存非空 `selected_structure_revision_id`，失败/取消诊断则显式保存 null。`ACTIVE`表示该指针选中该结果/结构组合；`STALE`表示其输入清单或解释已不再是当前选择上下文；`SUPERSEDED`表示输入仍可比对，但较新的读取器/schema/规则或结构修订对已被明确选为默认。后两者都可供历史查看和审计，却不得据此新建音频或外部任务。选择变化本身通过追加事件审计，不能改写 `ParseRevision` 或 `StructureRevision`。一个最新尝试可合法是 `FAILED + ACTIVE`，此时它说明当前输入未能解析，但没有可选定的 `StructureRevision`；此前成功结果仍是历史记录，不能被伪装成当前结构。

下列组合必须被明确支持，而非由一个笼统的“完成”状态猜测：

- `SUCCEEDED + ACTIVE + complete + valid` 只说明该范围结构完整；每个目标仍需检查自己的 `resolution_state`、必填字段和 `capabilities.external_input`。
- `PARTIAL + ACTIVE + 主文档 complete` 可以出现在已确认答案附件无法读取或部分未绑定时；主文档的音频可继续，依赖附件的字段和外部目标必须保持不可用。
- `SUCCEEDED + STALE` 是可审计的历史结果，不能因其内容看起来完整就重新触发下游。
- `needs_review` 不会把无关、已校验范围降为无效；但受影响范围不得自动外部录入，音频是否可用由其单独能力和风险策略决定。

旧字段如 `document_kind_status`、旧 `document_profile.status` 只能是显示/兼容投影：`suggested → CANDIDATE`、`confirmed → CONFIRMED`、`conflict → AMBIGUOUS`、`unknown → UNRESOLVED`、`rejected → REJECTED`。新写入统一持久化为 `FieldDecision(field_path, value, resolution_state, decision_source=parser|user|policy|migration, evidence, confidence, policy_version?)`；API 可以同时投影兼容值字段和对应 `*_decision` 元数据，但两者必须同源且数值一致。不得再以旧字符串状态作为事实源。

## 6. 规则治理

### 6.1 Rule Catalog

所有会影响边界、类型或字段归属的规则都必须具备稳定 ID、所属层级、版本、优先级和测试样例。建议以代码注册表或受版本控制的数据文件表达，逻辑上等价于：

```json
{
  "rule_id": "textbook.renjiao.section-a-heading",
  "scope": "textbook_profile",
  "profile_code": "renjiao_section_ab",
  "purpose": "section_boundary",
  "pattern": "...",
  "priority": 80,
  "rule_set_version": "2026.09",
  "introduced_in": "profile-v1",
  "positive_examples": ["课文跟读人教九上U1.docx"],
  "negative_examples": ["七上Starter Unit 1 听说测试题（2026新题型）.docx"]
}
```

不要求把所有规则改为纯 JSON；复杂关联逻辑仍可保留在 Python 中。但无论规则以何种形式存在，都必须有稳定的 `rule_id` 和明确归属。

### 6.2 规则应该放在哪里

| 规则类别 | 唯一归属 | 示例 |
| --- | --- | --- |
| 原文结构与定位 | 公共结构读取层 | 段落、表格、文本框、样式、自动编号 |
| 通用文本标记 | 公共规则库 | 录音稿、参考答案、分值、说话人、控制词、编号 |
| 顶层类型证据 | 文档分类器 | Excel 词汇表、考试标题组合、教材结构组合 |
| 教材版式 | `TextbookProfile` | Section A/B、旧版章节标题、文章分隔 |
| 考试大题语义 | 大题 Parser | 题干—选项—答案—材料的关联和归属 |
| 音频拆分 | 音频策略 | 逐句、按段落、按角色、整篇 |
| 展示文案/兼容类别 | 适配层 | 旧 `category`、旧文件命名 |

以下内容不得重复维护：录音稿标记、参考答案标记、题号格式、说话人格式、控制词、章节标题的通用部分。新增规则前必须先检查公共规则库和现有 profile，避免复制一条近似正则。

### 6.3 规则变更的发布纪律

一条规则的新增、删除或优先级变化都可能改变单元边界、题型 owner 或音频命名，因此必须按一次小型契约变更处理：

1. 更新规则 ID、规则集版本、适用 profile/Families 和正反样例。
2. 运行受影响样例和全量解析基线，记录结构树、覆盖率、owner 和 legacy 投影差异。
3. 若差异是预期修复，写明迁移原因和受影响的历史解析结果；若非预期，阻止发布。
4. 保留旧规则集版本，使历史 `DocumentRevision` 的诊断可复现；重新解析时明确使用新的规则集版本创建新修订。

规则分值或置信度不是业务优先级。`priority`只用于可解释的裁决顺序；`confidence`只表示当前证据的充分程度。不得因文件名命中或单个关键词而产生高置信度结论。

### 6.4 新格式接入流程

新增一个文档样式时，按以下顺序处理：

1. 先判断它属于词汇、课文还是试卷；不得直接新增顶层 Parser。
2. 若为课文，判断是已有 `layout_profile` 的变体、profile override，还是全新布局。
3. 若为试卷，判断是已有大题 Parser 的新排版，还是新的考试大题语义。
4. 为样例建立结构断言：单元边界、大题范围、题号、材料关联和预期覆盖率。
5. 在规则目录中增加带 `rule_id` 的规则和版本说明。
6. 运行全部已有样例；确认新规则不会改变无关 profile/题型的边界。
7. 只有确实引入新的业务语义时，才新增 Family 或 SubType，并同步更新领域模型、抽取器和契约测试。

### 6.5 质量指标与回归观测

规则治理不仅依赖“某个样例能跑通”。每次基线或本地发布应至少聚合以下不含正文的指标，并按 `rule_set_version`、document kind、profile 和 Family 观察变化：

- 文档分类、profile 和 `exam_form` 的 `CANDIDATE` / `CONFIRMED` / `AMBIGUOUS` / `UNRESOLVED` / `REJECTED` 比例，以及策略自动确认的字段和策略版本；
- `complete`、`partial`、`unclassified` 覆盖率分布，以及主文档和附件的 `member_read_status`、未绑定范围比例；
- 来源包候选关联、确认、拒绝、附件读取失败和 `source_snapshot_unavailable` 的数量；
- owner 冲突、材料关联不确定、`ReferenceBinding`冲突、外部字段缺失和不支持格式的数量；
- `ReviewDecision` 的接受/拒绝/改写比例、并发 `review_decision_conflict`、迁移成功/待复核和策略自动确认的数量；
- 同一源文件、同一规则集重复解析的确定性差异数；
- `ParseRun` 的运行结果、重试链、`parse_key` 缓存命中/未命中、并发复用，以及 `ACTIVE` / `STALE` / `SUPERSEDED` 分布；
- 新规则导致的结构节点、音频片段、`SynthesisChunk`清单和 legacy 投影变化数。

指标的作用是发现规则漂移和误判集中区，不是用高置信度数量掩盖未归类内容。任何异常上升都应能通过 `rule_id`、诊断和样例定位回具体规则。

## 7. 统一输出契约

### 7.1 顶层解析结果

无论输入类型如何，解析器应返回统一外壳：

```json
{
  "schema_version": "document-parse/v1",
  "rule_set_version": "2026.09",
  "source_package_id": "package:...",
  "source_package_revision_id": "package-revision:...",
  "parse_input_manifest_hash": "sha256:...",
  "document_id": "source:...",
  "document_revision_id": "revision:primary-doc",
  "interpretation_revision_id": "interpretation:...",
  "parse_run_id": "parse-run:...",
  "parse_revision_id": "parse-revision:...",
  "parse_selection_id": "parse-selection:...",
  "selection_scope": "DEFAULT_EXECUTION",
  "structure_revision_id": "structure-revision:0",
  "parse_status": "SUCCEEDED",
  "freshness_status": "ACTIVE",
  "validation_status": "valid",
  "validation": {"summary_status": "valid", "by_scope": []},
  "source_content_hash": "sha256:...",
  "source_file": "课文跟读-7上U1.docx",
  "source_package_members": [],
  "document_kind": "textbook",
  "document_kind_decision": {
    "value": "textbook",
    "resolution_state": "CANDIDATE",
    "decision_source": "parser",
    "confidence_band": "HIGH",
    "evidence": []
  },
  "document_profile": {
    "identity": {
      "publisher": {
        "value": "renjiao",
        "resolution_state": "CANDIDATE",
        "decision_source": "parser",
        "evidence": []
      }
    },
    "layout_profile": {
      "value": "renjiao_section_ab",
      "profile_version": "1",
      "resolution_state": "CANDIDATE",
      "decision_source": "parser",
      "confidence_band": "HIGH",
      "evidence": []
    }
  },
  "input_units": [],
  "reference_bindings": [],
  "unclassified_nodes": [],
  "diagnostics": [],
  "coverage": {"primary": {}, "by_member": []},
  "legacy_projection": {}
}
```

字段约束：

- `document_id`、`document_revision_id`、`source_content_hash`和 `source_file`仍指主文档，供旧消费者兼容；多文件解析的规范输入身份是 `source_package_revision_id + parse_input_manifest_hash`，不能只凭主文件哈希缓存或追溯。
- `source_package_members`返回成员角色、关联状态、读取状态、内容哈希、可展示名称和关联证据的安全摘要；只有已确认、已读取且进入输入清单的成员可以为结构字段提供 `source_ranges`。待确认附件必须对核对页可见，但其全文和媒体不会被自动混入主结果。
- `coverage.primary`描述主文档业务结构的覆盖，`coverage.by_member`描述已参与解析附件的读取/绑定情况；前者不能被附件的未绑定块污染，后者也不能被省略而掩盖答案或材料未能关联的事实。
- `validation_status`等于 `validation.summary_status`，只供列表/摘要展示；`validation.by_scope[]` 才是单元、题目、片段和外部目标的实际闸门。一个无关范围为 `invalid` 时，不得阻断独立且为 `valid` 的范围。
- `input_units`始终是统一的 `InputUnit`；其 `unit_kind`区分课文、词汇和试卷评测，面向产品统一称为“录入单元”。
- 每个 `StructureNode`必须有 `node_id`、`node_kind`、`node_label`、`ordinal`、`origin`、`confidence` 和 `evidence`；叶子业务对象使用各自的稳定 ID（如 `content_item_id`、`question_id`、`stimulus_id`），但同样必须保存来源、置信度和证据。`source`/`inferred`对象还必须有 `source_ranges[]`，人工/派生对象必须有可追溯的父级或修订来源。
- 正文必须区分 `raw_text` 与 `tts_text`；任何清洗均不得覆盖 `raw_text`。
- `legacy_projection`仅用于现有 `items`、`questions`、`tasks` 等下游兼容，不能反过来作为结构事实源。
- `category`只能用于展示或旧链路适配。新代码判断业务语义时必须使用明确的 `document_kind`、`major_type_code`、`sub_type_code`、`node_kind` 和 `segment_kind`。
- `schema_version`和 `rule_set_version`是结果的一部分；不得用当前运行环境的规则去解释历史解析结果。
- `parse_run_id`标识可观察的执行过程，`parse_revision_id`标识其不可变终态产物；`parse_selection_id + selection_scope + structure_revision_id + freshness_status` 是从选择事件/当前指针得到的动态视图，不能反写到解析结果。音频、录入和 UI 只能消费 `DEFAULT_EXECUTION` 中明确选定、`freshness_status=ACTIVE` 且 `parse_status`为 `SUCCEEDED` / `PARTIAL` 的 parse/structure 修订对，不能从“当前工作流 items”反推来源版本。
- 每个自动填入的业务字段应保存 `value + resolution_state + decision_source + evidence`；例如年级、出版社、`exam_form`和题型 owner 都要能说明来自正文、文件名、模板、策略还是用户确认。用户值不是覆盖后丢弃原证据，而是新增一条优先级最高的修订事实；旧 `status` 仅可作为兼容投影。

#### 7.1.1 `InputUnit` 与结构节点的最小契约

`InputUnit`是所有文档类型一致的顶层处理容器；`StructureNode`承载 Unit 内的章节、题型、对话、题组和内容类型。建议至少使用如下契约：

```json
{
  "unit_id": "unit:source-1:U1",
  "unit_kind": "TEXTBOOK|VOCABULARY|ASSESSMENT",
  "unit_label": "U1",
  "ordinal": 1,
  "root_node_id": "node:unit-u1",
  "origin": "inferred",
  "source_ranges": [{
    "range_id": "range:unit-u1",
    "document_revision_id": "revision:primary-doc",
    "role": "primary",
    "start": {"block_id": "p:1", "char_offset_utf16": 0},
    "end": {"block_id": "p:48"}
  }],
  "resolution_state": "CANDIDATE",
  "evidence": []
}
```

```json
{
  "node_id": "node:section-a",
  "input_unit_id": "unit:source-1:U1",
  "parent_node_id": "node:unit-u1",
  "node_kind": "section",
  "node_label": "Section A",
  "ordinal": 1,
  "origin": "source",
  "source_ranges": [{
    "range_id": "range:section-a",
    "document_revision_id": "revision:primary-doc",
    "role": "heading_and_content",
    "start": {"block_id": "p:2", "char_offset_utf16": 0},
    "end": {"block_id": "p:20"}
  }],
  "evidence": []
}
```

`unit_kind`、`node_kind`和 `relation_type`使用受控代码表；中文标题始终保留在 `unit_label` / `node_label`，不能作为程序分支键。一个节点的来源范围允许非连续；`ordinal`而不是范围起点决定同级显示顺序。

#### 7.1.2 参考字段的输出与确认

`reference_bindings[]`是答案、解析和评分说明的来源事实；`QuestionItem.answer`、`reference_answer`等便于下游消费的字段只是当前 `StructureRevision` 选择后的投影。每条绑定至少携带目标实体/字段、`reference_kind`、来源范围、值、状态、匹配方法、规则/人工证据和产生决定的结构修订。

- `CANDIDATE`、`AMBIGUOUS`和 `REJECTED`绑定必须保留在核对输出中，但不能覆盖正式字段，也不能被外部输入构造器当作可靠答案。
- `CONFIRMED`绑定只能来自当前 `parse_input_manifest`中允许角色的来源；若用户改选答案、解除答案附件或确认新的配对，创建新的 `StructureRevision`，旧投影仍可追溯。
- 一个多空题、题组公共答案或评分说明可有多个绑定，但每个目标字段/题目分项都必须有明确 scope；禁止用一个答案范围隐式填充整个大题。
- 参考字段与朗读内容始终分离。即使该绑定已确认，除非题型规则明确声明“参考应答需朗读”，否则它只服务核对和外部字段，不进入 `tts_text` 或音频产物。

### 7.2 内容清洗与音频片段契约

`ContentItem` 和 `AudioSegment`必须分开保存。建议最小字段如下：

| 对象 | 最小字段 | 约束 |
| --- | --- | --- |
| `ContentItem` | `content_item_id`、`raw_text`、`tts_text`、`normalization_trace`、`source_ranges` | `normalization_trace`记录去掉了哪些序号、提示语或来源标签；不得只留下清洗后的文本 |
| `AudioSegment` | `segment_id`、`parent_content_item_id`、`segment_order`、`source_ranges`、`content_spans`、`tts_text`、`language_tag`、`speaker_raw`、`speaker_role`、`voice_policy` | `source_ranges`定位源文档，`content_spans`定位父内容内的切片；`language_tag`与音色策略共同决定可用的合成能力；片段可按句子、段落或说话轮次形成，但不能脱离父级逻辑内容 |
| 片段—产物关联 | `segment_revision_id`、`artifact_id`、`artifact_origin`、`audio_config_hash`、`media_asset_id`、`time_boundary` | 一个合成音频可覆盖多个片段；同一片段也可因不同配置产生多个音频产物 |

音频文件名、ZIP 路径和展示标题都是可再生成的导出投影，不是 `segment_id` 或 `artifact_id` 的来源。命名策略只能消费已确定的结构路径、题号、音频策略和导出排序；重跑、重命名或冲突后追加后缀都不能改变逻辑片段身份。

`content_spans[]`至少包含 `parent_content_item_id`、`raw_start_utf16`、`raw_end_utf16`和对应的 `normalization_step_ids`；若规范化后仍可精确映射，再记录可选的 `tts_start_utf16` / `tts_end_utf16`。不能在“去除了编号、答案或提示语”后伪造一组看似连续的 TTS 偏移，`normalization_trace`才是该类映射的权威依据。

`language_tag`应使用明确代码（例如 `en-US`、`zh-CN`、`und`），并保存其来源/置信度。不能只因教材年级、文件名或当前 UI 默认音色而猜定语言；不支持或混合语言的片段进入 `MANUAL_REQUIRED` / `needs_review`，而不是交给任意默认音色朗读。用户对语言、读音词典或音色的覆盖属于音频配置修订，不修改原文或解析结构。

#### 7.2.1 语义片段与合成执行切片

`AudioSegment`是核对页、来源定位和业务范围中的**语义朗读片段**，不能因为供应商一次请求的字数限制而被拆成新的逻辑内容。实际调用 TTS 时可从一个 `AudioSegmentRevision` 派生多个不可变 `SynthesisChunk`：

```text
AudioSegmentRevision（对话第 2 轮 / 一段课文）
  ├─ SynthesisChunk #1（可安全合成的前半段）
  ├─ SynthesisChunk #2（可安全合成的后半段）
  └─ AudioArtifact（按 chunk_order 拼接或对应的多产物清单）
```

每个 `SynthesisChunk`至少记录 `chunk_id`、父 `segment_revision_id`、`chunk_order`、在 `AudioSegment.tts_text`中的半开区间、`chunk_text_hash`、`language_tag`、已解析的音色/参数、供应商能力/长度策略版本和请求载荷哈希。执行层按句末、段落或已确认说话轮次优先切分；不得切开 Unicode 代理对、组合字符、结构标签、说话人边界或被规则标记为不可分的发音单元。找不到安全边界时输出 `tts_chunk_boundary_ambiguous`，转为人工处理或使用明确的保守策略，不能静默截断文本。

`SynthesisChunk`是音频执行细节，不进入 `InputUnit`、题目或内容树身份。相同 `segment_revision_id + audio_config_hash + provider_capability_version`必须得到确定的 chunk 顺序和 `chunk_text_hash`集合；音色、语言、读音词典、速度/音调、供应商版本或长度策略改变时创建新的音频修订/清单，而不是新的 `ParseRevision`。单个 chunk 可以按其冻结输入安全重试，但只有所有必需 chunk 都通过格式、时长和内容完整性核验并写入产物清单后，父 `AudioSegment`才有可验收的音频；失败或缺失的 chunk 绝不能以一段“看似完整”的拼接音频发布。

#### 7.2.2 源音频、生成音频与人工上传的边界

音频文件的来源不是展示属性，而是决定生成、验收和外部上传资格的事实。`artifact_origin`固定为 `GENERATED_TTS`、`SOURCE_EMBEDDED`、`USER_UPLOADED` 或 `DERIVED_MEDIA`：

| 来源 | 必需事实 | 默认行为 |
| --- | --- | --- |
| `GENERATED_TTS` | `segment_revision_id`、音色/速度等 `audio_config_hash`、生成器版本 | 可按音频策略重新生成；验收的是该配置下的具体 artifact |
| `SOURCE_EMBEDDED` | `media_asset_id`、源文档范围、媒体哈希、MIME 和显式内容关联证据 | 不自动替代 TTS；用户确认且平台允许时才可复用 |
| `USER_UPLOADED` | `media_asset_id`、上传者、上传时间、目标范围、媒体哈希和用户选择记录 | 必须重新做格式/时长/完整性校验，不能冒充源文档附件 |
| `DERIVED_MEDIA` | 父 `media_asset_id`、提取/转码工具与版本、派生产物哈希 | 只继承已确认的源关联，不能因转码丢失来源链 |

`AudioSegment`另有 `audio_mode=GENERATE_TTS|REUSE_SOURCE|MANUAL_REQUIRED|NONE`，它描述这次操作应如何取得音频，不能由 `artifact_origin`反推。没有明确 `MediaAsset → 内容`关系时，`audio_mode`默认 `GENERATE_TTS` 或 `MANUAL_REQUIRED`；绝不能因发现一个 MP3 就默认为 `REUSE_SOURCE`。外部平台若限制来源、版权或音频格式，适配器必须在创建 scope 前声明可接受的 `artifact_origin`，并把最终选择冻结进目标快照。

### 7.3 必须稳定的原文定位与人工来源

#### 7.3.1 `SourceBlock` 是范围的唯一锚点

在构建任何 `InputUnit`、节点或业务实体前，读取层必须先为当前 `DocumentRevision` 生成不可变的 `SourceBlock` 序列。范围不能直接指向“第 N 个 item”或清洗后的文本字符串。每个块至少需要：

```json
{
  "block_id": "table:3/row:2/cell:1/p:0",
  "document_revision_id": "revision:...",
  "block_kind": "paragraph|table_cell|textbox|image_anchor|drawing_anchor|audio_anchor|video_anchor|worksheet_cell",
  "parent_block_id": "table:3",
  "reading_ordinal": 42,
  "raw_text": "原始可读文本",
  "raw_content_hash": "sha256:...",
  "media_reference": null
}
```

表格单元格、文本框和媒体锚点可作为正文块的子级，但仍必须有独立 `block_id` 和确定的 `reading_ordinal`。图片 OCR 是派生文本，保留 `origin=derived`、OCR 引擎/版本和原始媒体锚点；它不能覆盖或替代图片本身的来源事实。

Excel 也必须落到同一协议：建议单元格 ID 使用 `sheet:{sheet_ordinal}/cell:r{row}c{column}`，工作表名称仅作为展示元数据；同时保存显示值、公式（如有）、单元格样式和合并单元格主锚点。筛选、隐藏行或工作表改名不能改变当前 `DocumentRevision` 内的物理单元格 ID；合并区域中实际内容所在的左上角单元格是主块，其余格只保留到主块的引用，避免一条词汇被重复计入覆盖率。

#### 7.3.2 `SourceRange` 的精确定义

规范定位使用 `source_ranges[]`；每一项至少支持以下信息：

```json
{
  "range_id": "range:3",
  "document_revision_id": "revision:primary-doc",
  "role": "content",
  "start": {"block_id": "table:3/cell:2,1/p:0", "char_offset_utf16": 0},
  "end": {"block_id": "p:48", "char_offset_utf16": 37},
  "source_locator": "U1 / Section A / Conversation 2 / 第 3 轮"
}
```

范围端点属于该项 `document_revision_id`所指的同一个 `DocumentRevision`，且该修订必须位于当前 `parse_input_manifest`；`char_offset_utf16`以 `SourceBlock.raw_text` 的 UTF-16 code unit 计数，采用半开区间 `[start, end)`。整块引用可以省略偏移量，分别表示块首和块尾。单个 `SourceRange` 只能表示同一文档规范阅读顺序中的连续跨度；不连续来源或跨主文档/已确认附件的来源必须拆成多项，不能用一个大范围吞掉中间无关内容。

`content_spans[]`不是第二种源定位：它只描述 `AudioSegment` 在父级 `ContentItem.raw_text` / `tts_text` 内的切分偏移，并必须同时引用 `parent_content_item_id`。这样“源文档在哪里”和“应朗读父内容的哪一段”不会被混为一个字段。

不得仅依赖“第 N 个 `items`”或最终文本内容定位，因为文本相同、重新排序、表格内容和人工编辑都会使这类定位失效。若新 API 继续输出单值 `source_range`，它的值等于 `source_ranges` 中 `role=primary` 的首项；旧存储层的 `source_range_json` 仍只是首尾 locator/sequence 摘要。新逻辑必须读取完整集合，不能把两者当作同一精度的字段。

人工新增内容使用 `origin=user`、`manual:<node_id>` 以及创建它的 `structure_revision` / 用户动作记录；它可以有可选的锚点范围，但不要求存在虚假的源范围。人工拆分、合并或改写原文派生节点时，原始 `source_ranges` 保持只读，编辑后的文本和结构作为新的修订叠加保存。

#### 7.3.3 当前定位字段的兼容迁移

现有 `input_units.source_range_json` 主要保存主文档的首/尾 `sequence` 和 `source_locator`，适合作为旧页面的展示摘要，但不足以表达非连续范围、表格子块、字符切片、跨已确认附件的来源或冲突裁决。目标迁移必须遵守以下规则：

- 新解析结果新增完整 `source_ranges[]`（每项含 `document_revision_id`，可先存为 JSON，查询和裁决量上升后再拆为范围关联表）；`source_range_json`继续只输出主范围兼容摘要，不能反向作为规范范围事实。
- 旧记录若只有 locator/sequence，只能标记为 `legacy_locator_only`；在重新读取源文件建立 `SourceBlock` 前，不得把它当作可做严格重叠校验的精确范围。
- `SourceRange`必须带 `document_revision_id`；结构节点和业务对象必须带 `parse_revision_id`及其 `parse_input_manifest_hash`。同一逻辑 ID 在新修订中匹配成功时可延续身份，但它的来源范围仍是新输入清单自己的事实，不能直接复制旧范围。
- 任何已被 `input_run`、音频验收或历史回执引用的旧摘要和范围都保留原值；新解析只写入新的解析/结构修订。

### 7.4 人工核对、重解析与覆盖优先级

自动解析、用户修改和后续重解析必须分层保存：

```text
自动解析结果（某个 parse_input_manifest + InterpretationRevision + ParseRevision）
  → 基线 StructureRevision（无用户决定的 revision 0）
  → 用户决定（按 PACKAGE / INTERPRETATION / STRUCTURE 路由）
     ├─ 来源包或解释改变 → 新 SourcePackageRevision / InterpretationRevision → 新 ParseRun
     └─ 边界、owner、绑定或展示结构改变 → 新 StructureRevision
  → 音频或外部录入目标快照
  → 下一次自动重解析（新的源版本、解释、读取器或规则集）
  → 将可安全迁移的旧结构决定作为候选，而不是静默覆盖
```

固定规则：

- 用户确认的 `document_kind`、`exam_form`、profile、单元边界和题型 owner 优先于后续自动建议；自动结果只能以“发现新的差异”形式出现。
- 用户强制选择 `document_kind` 或课文 profile 时，必须创建新的解释/解析修订并按所选入口重建候选；不能只改一个标签后继续沿用不匹配的 Parser 结果。用户修改 `exam_form`通常不应重跑题型抽取，但必须重新校验录入单元分组和外部配置。
- 用户只改显示名称、顺序或父子关系且未改变 `tts_text` / 音频范围时，可沿用音频映射；改变正文、材料关联、片段边界或音频策略时必须创建新的音频修订。
- 重新解析后，系统通过逻辑身份和来源范围尝试迁移用户修订；无法安全匹配时进入 `needs_review`，绝不能把旧修订套到相似但不同的内容上。
- 已经产生外部副作用的目标范围、配置快照和历史映射不可被普通结构编辑改写；新版本必须创建新的可执行范围或新的运行记录。

这与系统录入方案中的 `structure_revision`、`audio_revision` 和 `input_run_id` 分层保持一致。

#### 7.4.1 可重建投影与冻结历史的写入边界

解析结果、核对结果和外部副作用不能共用一张“随时覆盖”的表。目标实现应把它们明确分为三个窗口：

| 状态窗口 | 允许的写入 | 不允许的写入 |
| --- | --- | --- |
| 草稿解析投影 | 根据当前 `parse_input_manifest + InterpretationRevision + parse_key` 重新生成候选、范围、覆盖率和未分类内容 | 将自动建议伪装成用户确认或历史事实 |
| 用户决定 / 音频验收前 | 按 `PACKAGE` / `INTERPRETATION` / `STRUCTURE` 路由保存来源包、解释或结构修订；必要时创建新的音频修订 | 用新解析静默覆盖已确认的用户边界，或把 profile/附件决定错误写入旧结构 |
| `OperationScopeRevision` / `input_run` 已创建后 | 仅追加 Attempt、回执、对账结果或新版本的 scope/run | 删除、重标、重分组或重绑定该历史运行的单元、片段、配置和音频 |

具体约束如下：

- 用户确认的单元边界应引用稳定的 `item_id` / `SourceRange` 集合，而不是一次解析临时生成的 `unit_id`；新解析无法覆盖完整成员时，把旧决定标记为 `stale` 并要求重新确认。
- 用户确认、拒绝或手工改配 `ReferenceBinding` 时，只创建新的 `StructureRevision`；它不得修改原始答案附件、旧解析候选或已冻结的外部载荷。若新结构修订仍存在 `reference_conflict` / `AMBIGUOUS`答案绑定，受影响题目继续禁止外部录入。
- 已验收音频之后，如果重解析改变了成员集合、`tts_text` 哈希、片段边界或音频策略，必须使旧验收仅归属于旧音频修订，并创建下一次音频验收候选；不能继续复用为新结构的“已验收音频”。
- 对已经被用户配置、音频验收、OperationScope 或外部回执引用的 `InputUnit`，新解析发现其消失时应标记 `superseded` / `removed_from_latest_parse` 并保留引用链；只有从未被引用的草稿投影才可以物理替换。
- 一旦存在 `input_run`，页面展示“最新解析”与展示历史运行必须按不同 revision 查询；同步解析不得删除、改名或重新绑定该运行的目标快照。

#### 7.4.2 通用人工决定、并发写入与安全迁移

人工核对不能直接更新“当前节点”或把候选的状态字段原地改成确认值。每一次接受候选、改字段、拆分、合并、移动或拒绝候选，都应追加一条不可变 `ReviewDecision`；但决定影响哪一种修订，必须由 `decision_scope` 明确路由，不能一律错误地写成 `StructureRevision`。建议最小契约如下：

```json
{
  "review_decision_id": "review-decision:...",
  "decision_scope": "STRUCTURE",
  "base_source_package_revision_id": null,
  "base_interpretation_revision_id": null,
  "base_parse_revision_id": "parse-revision:...",
  "base_structure_revision_id": "structure-revision:...",
  "target_refs": [{
    "entity_kind": "INPUT_UNIT",
    "entity_id": "unit:...",
    "field_path": "boundary.source_ranges",
    "expected_target_signature": "sha256:..."
  }],
  "action": "ACCEPT_CANDIDATE|SET_VALUE|CLEAR_VALUE|REJECT_CANDIDATE|SPLIT|MERGE|MOVE",
  "candidate_id": "candidate:...",
  "value": {},
  "reason_code": "user_verified_source",
  "decision_source": "user",
  "actor_id": "user:...",
  "actor_role": "REVIEWER|PUBLISHER|ADMIN",
  "created_at": "2026-09-04T09:00:00Z",
  "resulting_source_package_revision_id": null,
  "resulting_interpretation_revision_id": null,
  "resulting_parse_run_id": null,
  "resulting_structure_revision_id": "structure-revision:..."
}
```

| `decision_scope` | 典型动作 | 规范结果与禁止项 |
| --- | --- | --- |
| `PACKAGE` | 确认/拒绝附件关联、修改成员角色、选择主文档 | 新建 `SourcePackageRevision`，并按新输入清单创建/复用 `ParseRun`；不得只改当前解析结果中的附件标签 |
| `INTERPRETATION` | 强制 `document_kind`、选择有效 `layout_profile`、读取/可见性策略 | 新建 `InterpretationRevision`，再按冻结解释创建/复用 `ParseRun`；不得给旧 `ParseRevision` 直接改类型或 profile |
| `STRUCTURE` | 单元边界、题型/owner、`exam_form` 分组、答案/材料绑定、名称/顺序 | 新建 `StructureRevision`；用户明确应用到某个选择范围时，再以 CAS 让 `CurrentParseSelection` 指向新的 parse + structure 对。不重新读取源文件；若影响 `tts_text` / 音频边界，再由后续规则创建新的音频修订 |

实际记录只要求填入其 `decision_scope` 所需的 `base_*_revision_id`，其余基准字段必须为 null，不能为了满足表结构从“当前最新”猜一个值。`target_refs` 可包含多个成员，因此 `SPLIT` / `MERGE` 必须把全部输入实体、目标父级和各自的预期签名一并冻结。`candidate_id`、`value` 按 action 选填，但原始候选、来源范围、置信度和诊断永远不能被改写或删除。`ReferenceBinding` 的确认、拒绝或人工改配属于 `STRUCTURE` 决定，其 `decision_structure_revision_id` 必须指向这次决定生成的结构修订。每条记录只允许填入其作用域对应的 `resulting_*_revision_id`；`PACKAGE` / `INTERPRETATION` 还必须记录新输入或解释所启动/复用的 `resulting_parse_run_id`。结构决定若不被应用到当前选择范围，可以保留为可预览的分支；任务绝不能因为“最新结构修订”而自动跳到该分支。

权限校验必须先于并发校验：`actor_id`、`actor_role` 和 `decision_source` 由服务端认证上下文写入，不能相信浏览器提交的值。仅有查看/预览权限的用户只能写入自己的 `PREVIEW:<user-or-session>` 选择范围；只有具备发布权限的角色才能改变 `DEFAULT_EXECUTION` 指针、确认高风险字段或创建真实音频/外部任务。策略和迁移程序也必须使用受限的服务身份与版本化策略，不能伪装成普通用户决定。

服务端必须按作用域做乐观并发校验，而不是按页面最后保存时间覆盖：`PACKAGE` 校验 `base_source_package_revision_id + expected_target_signature`，`INTERPRETATION` 校验 `base_source_package_revision_id + base_interpretation_revision_id + expected_target_signature`，`STRUCTURE` 校验 `base_parse_revision_id + base_structure_revision_id + expected_target_signature`。

- 基准修订仍是当前分支、且全部目标签名匹配时，才创建该作用域对应的新修订或运行。
- 任一目标在用户打开页面后被另一位用户、迁移程序或新解析改变时，返回 `review_decision_conflict`，同时给出当前结构、原候选和差异摘要；前端应要求用户重新选择或显式 rebase。
- 只有目标集合互不重叠、父级/语义校验都仍成立时，服务端才可自动重放两个并发 `STRUCTURE` 决定；不能因为字段路径不同就跳过拆并、owner、范围和外部引用校验。`PACKAGE` 与 `INTERPRETATION` 决定默认不自动合并。
- 撤销也是一条基于后续修订的反向 `ReviewDecision`，不是删除原决定或修改历史 revision。

决定来源和状态迁移也必须受限：Parser 只能产生 `DRAFT`、`CANDIDATE`、`AMBIGUOUS`、`UNRESOLVED`；用户操作可产生 `CONFIRMED` / `REJECTED`；策略自动确认只限事先登记的低风险字段，并记录策略版本和全部证据。`REJECTED` 仅表示候选不被采用，不表示源文、诊断或候选历史可被删除。`INTERPRETATION` 决定即使确认了类型/profile，也必须先形成新解析结果，不能把旧输出原地升级为已确认。迁移程序不得把新候选悄悄升级为 `CONFIRMED`。

重解析时，旧 `STRUCTURE` 决定只能作为“可迁移候选”。对于边界、题型/owner、答案/材料绑定和正文改写，只有同时满足“实体一对一匹配、已匹配祖先一致、参与成员/关系和规范锚点签名仍一致、没有冻结 scope/外部回执冲突”时，才可自动重放；其余情况保留旧决定为历史并让新结构进入 `needs_review`。仅显示名称、显示顺序等不改变业务语义的决定，可以在稳定逻辑 ID 与父级一致时较宽松迁移，但同样必须记录迁移来源和校验结果。来源包关联和解释选择在源包/解释变化后最多复制为新的建议，不能静默迁移成已确认事实。

### 7.5 音频生成与系统录入的共同操作契约

解析结构是两条能力的共同事实源，但音频生成和系统录入必须创建不同的不可变操作范围。沿用现有原子模型的 `OperationPlan → OperationScope → OperationTask`，而不是另建一套状态机：

```text
parse_input_manifest + StructureRevision
  └─ OperationPlan
     ├─ OperationScopeRevision（音频目标）
     │  └─ OperationTask(AUDIO_GENERATE)
     │     └─ AudioArtifact / artifact_manifest
     └─ OperationScopeRevision（外部录入目标）
        └─ OperationTask(EXTERNAL_UPSERT)
           └─ input_run → entry / attempt / external receipt
```

两类任务的边界如下：

| 操作 | 典型 Scope | 消费的结构事实 | 产物与成功闸门 |
| --- | --- | --- | --- |
| `AUDIO_GENERATE` | `CONTENT_UNIT`、`STIMULUS`、`QUESTION`、`GROUP` 或内容片段集合 | `tts_text`、语言、角色、音色策略、片段边界、音频配置和冻结的 `SynthesisChunk` 清单 | 已验证的 `AudioArtifact`、chunk—片段关系和当前音频清单 |
| `EXTERNAL_UPSERT` | 通常为 `QUESTION` 或 `AssessmentUnit` | 已确认的小题、材料关系、平台字段、图片/表格资源、当前配置 | 外部回执或可对账状态；若平台需要音频，还必须绑定已验收的音频清单 |

`OperationScopeRevision`是唯一的任务目标事实：同一音频可服务多个小题，同一个 `InputUnit` 也可包含多个小题，但任务只能引用冻结的成员集合和内容修订，不能在执行中读取“当前最新 items”。每个真实任务 scope revision 至少冻结 `source_package_revision_id`、`parse_input_manifest_hash`、`parse_selection_id`、`selection_scope=DEFAULT_EXECUTION`、`parse_revision_id`、`structure_revision_id`、目标成员及其内容修订；音频 scope 另冻结音频配置/语言/供应商能力版本，外部 scope 另冻结配置和 payload。`OperationTask`继续映射到现有 workflow step；`input_run_id`是一次批量外部录入运行，不替代音频任务或内容版本。

#### 7.5.1 外部目标的音频绑定快照

当平台页面需要上传、选择或关联音频时，外部录入目标必须显式携带音频绑定，而不是在执行时寻找“最新音频”：

```json
{
  "scope_id": "scope:assessment-unit:U1",
  "scope_revision": 3,
  "source_package_revision_id": "package-revision:...",
  "parse_input_manifest_hash": "sha256:...",
  "parse_selection_id": "parse-selection:...",
  "selection_scope": "DEFAULT_EXECUTION",
  "parse_revision_id": "parse-revision:...",
  "structure_revision": 8,
  "configuration_revision": 5,
  "audio_requirement": {
    "required": true,
    "audio_revision": 12,
    "artifact_manifest_hash": "sha256:...",
    "accepted_at": "2026-09-04T00:00:00Z"
  },
  "audio_bindings": [
    {
      "target_id": "question:...:1",
      "binding_role": "listening_script",
      "segment_ids": ["segment:dialogue-1"],
      "artifacts": [{
        "artifact_id": "artifact:audio-1",
        "artifact_origin": "GENERATED_TTS",
        "media_asset_id": null
      }]
    }
  ]
}
```

规则：

- 一个 `Stimulus` 被多道题共享时，可以只生成一份音频产物，但每道外部题目都通过自己的 `audio_binding` 引用该产物；不得复制音频或通过文件名猜测归属。
- 只有当前结构范围、音频修订、配置修订和 `artifact_manifest_hash`同时匹配时，才可开始需要音频的外部提交。
- `SOURCE_EMBEDDED`、`USER_UPLOADED`和 `DERIVED_MEDIA`绑定必须带非空 `media_asset_id`及其来源/关联证据；`GENERATED_TTS`必须带生成配置和内容修订。适配器只接受自己显式声明支持的来源类型，来源、媒体哈希或转码版本变化都要求新的音频验收与目标快照。
- 平台不需要音频的外部任务，由适配器能力声明 `needs_audio_artifact=false`，可以不等待音频任务；不得用“通常有音频”作为隐式依赖。
- 一次外部提交启动后，其音频绑定、payload 和配置快照冻结。后来生成的新音频只能被新的外部运行使用，不能悄悄替换历史运行已提交的音频。

#### 7.5.2 两条路径的失败、重试与变更矩阵

| 情况 | 音频处理 | 外部录入处理 |
| --- | --- | --- |
| 音频任务失败，且外部任务需要音频 | 仅重试或重建音频任务 | 阻止启动，不创建外部副作用 |
| 音频已验收，外部提交失败且确认未产生副作用 | 不重生成音频 | 使用同一目标快照安全重试，新增 Attempt |
| 外部提交超时或结果不明 | 不重生成音频 | 进入 `AMBIGUOUS` / 对账；不得自动重复提交 |
| 修改外部平台字段或模板，音频内容未变 | 复用已验收音频 | 创建新的配置/目标快照，按平台幂等规则新建或重试外部运行 |
| 修改 `tts_text`、片段边界、角色/音色策略 | 创建新的音频修订并重新验收 | 旧外部运行保持历史；需要提交新音频时创建新的外部目标快照 |
| 修改题干、选项、答案或材料关联 | 仅在影响朗读内容时重生成音频 | 总是重新校验完整度，并为外部提交创建新的目标快照 |

课文和词汇当前仍可创建 `AUDIO_GENERATE` 任务和结构化核对结果；在外部平台字段和适配器能力尚未确认前，它们不得创建真实 `EXTERNAL_UPSERT` 运行。这是能力开关，不是数据模型缺失。

## 8. 当前代码的演进边界

本方案不要求一次性重写现有 Parser。建议按职责逐步收口：

| 当前位置 | 当前主要职责 | 目标职责 |
| --- | --- | --- |
| `question_types/detection.py` | 类型检测和证据评分 | 输出顶层类型、profile、结构块候选与证据；不复制 Parser 私有规则 |
| `question_types/segmenter.py` | 一次读取和类型路由 | 持有主 `DocumentCorpus`及包级 `PackageCorpus`视图，建立 Unit/Section 候选范围并调度 Parser；不得拼接多个成员的正文顺序 |
| `workflow/docx_table_image.py` 与媒体读取层 | Word 表格图片派生 | 统一提取 `MediaAsset` 和媒体锚点；不把图片/嵌入音频误当纯文本或生成音频 |
| `question_types/text_reading.py` | 多格式课文解析和音频拆分 | 拆为课文基础结构 + 若干 profile 策略 + 音频策略 |
| 各试卷 Parser | 全文扫描、提取最终 items | 在已分配 `MajorSection` 内抽取标准候选和实体 |
| `question_types/text_utils.py` | 公共文本工具 | 仅保留真正共享的标记、清洗、定位和结构辅助能力 |
| `question_model/model.py` | Family/SubType 与现有原子实体 | 保持题型语义、能力和兼容映射；现有叶子 `ContentUnit` 增加到 `InputUnit` 的成员映射，不能兼任父级单元 |
| `question_model/extractors.py` | 原始结果到原子模型映射 | 消费统一候选协议，减少对中文 `category` 的字符串分支 |
| `question_model/adjudication.py` | Candidate owner 裁决 | 基于规范 `claimed_ranges` 和显式优先级裁决同层 owner，不以字符串 locator 代替范围 |
| `question_model/operations.py`、`question_model/runner.py` | 操作范围与统一执行 | 以结构/内容修订创建不可变 scope 和 AUDIO/EXTERNAL 任务；执行状态继续由 workflow step/attempt 管理 |
| `workflow/parser.py` 与来源导入层 | 单文件哈希、解析适配 | 先写入并校验不可变源快照，再冻结 `SourcePackageRevision` / `parse_input_manifest`；生成基线结构修订后，以条件更新 `CurrentParseSelection` 将选定 `ParseRevision + StructureRevision` 对投影到工作流，不重新解释题型规则，也不凭上传批次自动关联附件 |
| 核对页与 `StructureRevision` 服务（新增） | 分散的页面字段保存 | 以 `ReviewDecision` 追加人工决定，校验 base revision / 目标签名并创建新结构修订；不直接更新当前节点或历史范围 |
| `workflow/system_input_content.py` | 外部平台页面输入构造 | 只消费已确认且 `external_input=true` 的规范目标；不得从旧 `category`、顶层题型或已有 `page_input` 反推资格，也不得为未开放画像伪造可执行页面数据 |
| `workflow/system_input.py`、`workflow/system_input_executor.py` | 录入快照与平台执行 | 冻结结构、配置、音频清单和绑定后创建/执行 `input_run`；执行时不查找“最新音频” |
| `audio_naming.py`、`wordtts/speakers.py` 与合成计划层 | 命名、说话人和实际 TTS 请求切分 | 消费结构、语言和音频策略，派生可重试的 `SynthesisChunk`；不再维护独立的文档分类规则或以供应商长度限制改变逻辑片段 |

`text_reading.py`的拆分优先级最高，但不能以“减少文件行数”为目标。首先要确保 Unit、章节、内容类型、子组、内容条目和音频片段的职责可单独测试。

## 9. 分阶段改造计划

### 阶段 A：冻结分类契约与样例

- 新增 `document_kind`、`document_profile`、`input_units`、`coverage` 的只读输出，保留旧 `items` 输出不变。
- 建立 `InputUnit` 与现有 `ContentUnit` / `QuestionItem` / `Stimulus` 的成员映射，先解决父级录入单元和内容叶子的语义混淆。
- 建立 `ImportBatch → SourcePackage → SourcePackageRevision` 的显式边界：单文件也生成来源包，多个独立文件不自动互相成为附件；先写入哈希可校验、可保留的源快照，再为每个 `DocumentRevision` 生成带可见性/修订状态的规范 `SourceBlock` / `claimed_range` 兼容映射，并在结果中写入 `schema_version`、`rule_set_version` 和源内容哈希。
- 建立 `InterpretationRevision` / `ParseRevision` 的 `parse_key`、同 key 幂等复用和条件发布；`parse_key`必须使用冻结的 `parse_input_manifest_hash`。读取失败也必须产出受控诊断而非空解析结果。
- 分离 `ParseRun` 运行态、`ParseRevision.parse_status`、`freshness_status`、覆盖率、校验和字段裁决状态；旧 `status` 字段只保留兼容投影，不能作为新流程事实源。
- 为答案、解析和评分说明落地候选 `ReferenceBinding`、通用 `ReviewDecision` 与 `StructureRevision` 决定；候选/冲突值不得直接进入 TTS 或外部录入字段。
- 为现有样例标注预期文档大类、课文 profile、试卷大题顺序、`major_section_profile`、录入单元数量和目标级能力；至少冻结旧版 `U + 外网/教材` 模仿朗读为“音频可用、录入关闭”，并冻结框内英文专项和编号试卷正文专项为各自的候选录入画像。
- 增加“检测候选与 Parser 实际认领范围一致”、同 key 并发解析/失败后重试、用户解释变更导致旧运行过期、并发人工核对冲突与安全迁移、同批独立文件不串包、附件读取失败、答案绑定冲突，以及 Word 修订/隐藏内容不泄漏的测试。

完成标准：新增字段不改变旧音频输出；每份基线样例都有可读的诊断和覆盖率。

### 阶段 B：课文 profile 化

- 从 `text_reading.py` 中提取统一课文结构树构建器。
- 首先落地已有的课文布局 profile，例如 `renjiao_section_ab`、现有旧版布局以及 `unknown_textbook` 保守兜底。
- 将版本/年级差异移入 profile override，逐步移除 Parser 内分散的版式判断。
- 对每种 profile 建立至少一个单 Unit、一个多 Unit 或边界候选样例。

完成标准：同一份课文可在不改变结构模型的情况下更换 profile；未知格式不丢内容，只进入待确认。

### 阶段 C：试卷先分区再解析

- 先为试卷生成 `AssessmentUnit` 和 `MajorSection` 候选范围。
- 各考试 Parser 改为接收范围，不再默认扫描全文。
- 引入唯一 owner 裁决、未认领块和冲突诊断。
- 增加专项、专项合集、完整套卷、题型混合、题号重启和残缺试卷样例。

完成标准：一个混合试卷中每个原文块只有一个 owner；检测结果与解析结果不再出现“能解析但未检测”的分歧。

### 阶段 D：规则收口与旧输出降级

- 将公共标记规则迁移到共享规则库，删除重复正则和无引用规则。
- 将旧 `category` 映射收敛为兼容投影，业务逻辑改读显式 code 和节点角色。
- 让音频命名和说话人解析消费结构化字段，而不是重新扫描整篇文本。

完成标准：新增格式只在一个 profile 或一个题型规则模块内增加主要规则；同一语义不再在检测器、Parser 和命名模块重复实现。

### 阶段 E：以规范结构驱动下游

- `QuestionItem`、`Stimulus`、`ContentUnit` 和 `AudioSegment`成为音频与外部录入的共同事实来源。
- 旧 `items`、`questions`、`tasks` 保留为受控投影，直到所有消费者完成迁移。
- 为长文本建立确定性 `SynthesisChunk` 清单；chunk 可独立重试，但语义片段、音频验收和外部目标仍以 `AudioSegment` / `audio_revision`为准。
- 音频和外部目标快照显式绑定 `source_package_revision_id + parse_input_manifest_hash + parse_revision_id + structure_revision`，以及已验收的 `audio_revision + artifact_manifest_hash + audio_bindings`；音频和外部失败可独立重试。

完成标准：工作流、音频、核对页和外部录入不再各自解释 `category + text`，外部提交也不会在运行时误取另一版或“最新”的音频。

## 10. 验收矩阵

| 场景 | 必须验证的结果 |
| --- | --- |
| 单工作表词汇 Excel | 表头映射正确、空行不产出、单词/例句定位可回溯 |
| 多工作表词汇 Excel | 每张有效表独立识别，非词汇表不误入 |
| 含隐藏工作表、行或列的词汇 Excel | 可见性状态可追溯；隐藏内容默认不自动生成词汇/音频，用户或 profile 显式纳入后仍使用稳定单元格锚点 |
| 人教版单 Unit 课文 | Section、内容类型、对话/文章、音频片段层级正确 |
| 外研版或旧版课文 | 通过对应 profile 解析，不污染人教版规则 |
| 多 Unit 课文 | 正确拆分多个 `InputUnit`；多个 Conversation 不误拆 Unit；叶子 `ContentUnit` 正确归属父级 |
| 未知课文版式 | 保留原文和候选诊断，不生成空结果或错误高置信度结论 |
| 单一专项试卷 | 识别为 `special`，大题、小题、材料和答案关系正确 |
| 带分值的 Unit + 外网/教材模仿朗读（`模仿朗读-7上-U5-U6.docx`） | 识别为 `imitation_unit_source_special`；6 篇材料各自生成一套录入单元/试卷；每题分值为对应大题标题的 6 分；生成 `page_input` 并在文稿视图按录音稿分组展示 |
| 框内英文模仿朗读专项（`七上Starter Unit1 Hello模仿朗读专项.docx`） | 识别为 `imitation_boxed_special`；题号、框内英文、分值和参考文本关联正确；字段确认后才可令对应目标 `external_input=true` |
| 编号试卷正文模仿朗读专项（`九上Unit1模仿朗读(1).docx`） | 识别为 `imitation_numbered_exam_special`；不得把操作提示或答案区混入朗读正文；字段确认后才可令对应目标 `external_input=true` |
| 模仿朗读未知/混合版式 | 保留可识别材料及诊断；即使顶层题型为 `imitation_reading` 或可生成音频，也必须保持 `external_input=false`，直到注册对应画像和录入契约 |
| 一个文件多个专项 | 形成多个 `AssessmentUnit` 候选或明确的专项块，不因材料标签误拆 |
| 完整套卷 | 多个 `MajorSection` 按顺序归属同一 `AssessmentUnit` |
| 同一材料对应多道题 | 多个 `QuestionItem` 通过关系边引用同一个 `Stimulus`，材料正文不复制 |
| 套卷与专项合集边界相近 | 先完成大题分区，再输出 `paper`、多个 `special` 或 `multiple_candidate`；不以题型数直接判定 |
| 混合题型且部分残缺 | 正确输出 `partial`/`ambiguous`，不漏掉已可识别的区块 |
| 多 Unit / 多题组中仅一处结构校验失败 | `validation.summary_status`可为 `invalid`，但 `validation.by_scope[]` 中独立 `valid` 的单元仍可按自身资格生成任务；失败范围不产生副作用 |
| 听后记录表或图片材料 | 表格/图片保留可回溯的 `block_id` 与媒体关系；需要图片输入时能从源表派生产物 |
| 文件名与正文冲突 | 正文结构优先，冲突可见且允许用户覆盖 |
| 高置信度的文档类型或课文 profile | 可作为 `CANDIDATE` 驱动保守解析，但不因置信度自动成为 `CONFIRMED`；旧 `status` 与规范字段决定投影一致 |
| 同次上传两份独立课文/试卷 | 生成两个 `SourcePackage` 或待分组候选；不得因上传批次、目录或相似文件名把它们互相当作附件 |
| 主试卷 + 已确认答案附件 | 只有主试卷产生 `InputUnit`；答案范围以 `ReferenceBinding`关联到具体题目字段，且可完整追溯到答案文件 |
| 答案附件中题号重启或同号题跨题组 | 不得仅凭题号自动填答案；无唯一结构/题干证据时生成 `AMBIGUOUS`绑定并阻断该题外部录入 |
| 主文档答案与附件答案冲突 | 保留双方来源并输出 `reference_conflict`；不按上传/文件顺序覆盖，答案不进入 TTS，受影响外部目标不可提交 |
| 已确认附件无法读取 | 主文档仍可产生 `PARTIAL`结构结果，`coverage.by_member.member_read_status=FAILED` / `UNAVAILABLE`；附件提供的答案、材料、图片或音频字段全部保持未确认，不能回退使用旧值 |
| 已确认答案附件部分未绑定 | `coverage.by_member`显示未绑定块和原因；主文档覆盖率不被误报为未分类，平台必需字段仍不可自动补齐 |
| 上传后源文件被覆盖、移动或底层字节被篡改 | 解析只读取不可变源快照；哈希不一致时明确阻断，不能复用旧范围、缓存或音频 |
| 已有外部回执的历史源达到保留期限 | 保留哈希、范围摘要和审计链并标记快照不可用；历史页面可读，但不能假装仍可完整复跑 |
| 重复题号或编号重启 | 通过父级范围定位，不发生跨大题错误合并 |
| 用户拆分后重新解析 | 自动建议不能覆盖用户边界；无法安全迁移的修订进入 `needs_review` |
| 两位用户同时确认同一边界、owner 或答案绑定 | 后一决定必须因 base revision / 目标签名不匹配得到 `review_decision_conflict`；不能按最后保存时间覆盖 |
| 重解析后存在历史人工决定 | 只有一对一匹配、祖先/锚点/关系一致且无冻结引用冲突时自动重放；其余保留历史并进入 `needs_review` |
| 不同 Unit 中出现相同句子/单词 | 因父级和来源范围不同而保留为不同实体，不共享错误的 ID、音频或外部映射 |
| 一个 Unit 或题组被拆成多个 / 多个被合并 | 默认产生新的逻辑身份和 scope；只有用户明确主单元且无外部副作用时才允许按规则延续主单元 ID |
| 同题号但题型、材料或结构范围变化 | 不能仅凭题号复用历史 `QuestionItem`；输出 `CHANGED` / `AMBIGUOUS` 并阻断外部复用 |
| 音频可生成但字段不完整 | 可进入音频核对，`external_input=false`，不可提交外部平台 |
| 长课文或材料超过单次 TTS 长度限制 | 保持一个逻辑 `AudioSegment`，按确定性 `SynthesisChunk`合成并核验完整拼接；不得截断、重复或改写内容树 |
| 片段语言未知或所选音色不支持 | 输出语言/音色诊断并进入 `MANUAL_REQUIRED` / `needs_review`；不得把文本交给任意默认音色生成 |
| 仅修改音色、语言、读音词典、供应商或长度策略 | 新建音频修订和 artifact 清单，不重新解析文档；旧验收与外部运行仍引用旧音频版本 |
| 同源文件、同规则集重复解析 | 节点排序、范围、逻辑身份和 legacy 投影保持确定性；规则集变化产生可追溯的新修订 |
| 同一 `parse_key` 并发解析 | 只允许一个活动 `ParseRun`；其余请求复用该运行，终态结果通过条件发布选定，不产生并发覆盖 |
| 同一 `parse_key` 失败后显式重试 | 保留原失败诊断；新 `ParseRun` 带 `retry_of_parse_run_id` 且不与同 key 活动运行并发，成功后才可能选为默认结果 |
| 同一 `parse_key` 的成功/部分成功复跑结果哈希不同 | 产生 `parser_non_deterministic` 诊断并阻止自动替换，保留两份结果供定位 |
| 解析中修改 profile 或强制文档类型 | 旧结果在 `DEFAULT_EXECUTION` 视图中投影为 `STALE` 且不触发下游；新 `InterpretationRevision` 的结果才可成为默认结果 |
| 当前输入解析失败或被取消 | 保留 `FAILED` / `CANCELLED` 诊断；即使它是 `ACTIVE` 的最新尝试，也不选定空 `StructureRevision` 或拿旧结构冒充当前结果 |
| 同一输入以较新读取器/schema/规则结果替代 | 旧成功结果标记 `SUPERSEDED`，保留审计但不得再创建音频或外部任务 |
| 损坏、受密码保护或不支持的源文件 | 输出准确的读取诊断；不创建空 `InputUnit`、假 `complete` 覆盖率或下游任务 |
| 异常压缩包、外部关系或带宏的 Office 文件 | 在安全预算/沙箱前置层阻断或忽略危险内容，并输出对应诊断；不执行宏、不访问外网、不伪装解析成功 |
| 文档含嵌入音频但关联不明确 | 保留 `MediaAsset` 与 `media_relation_ambiguous`；不自动替代 TTS 或上传平台 |
| 用户确认复用源音频 | 外部/音频快照包含来源、媒体哈希和关联证据；来源或转码变化后必须重新验收 |
| 一段录音稿关联多题并需平台音频 | 只生成一份共享音频；每个外部目标的冻结快照都明确引用同一 Artifact |
| 音频重生成后再次录入 | 旧 `input_run` 保留旧清单；新运行必须绑定新验收的 `audio_revision + artifact_manifest_hash` |
| 外部提交失败或结果不明 | 失败可仅重试外部任务；不明状态先对账，绝不因重试重复生成或重复提交音频 |
| 参考答案、红色答案或评分说明 | 保留为结构化参考信息，不泄漏进 `tts_text`；若规则要求朗读必须有显式证据 |
| 浮动文本框、表格与大文档 | 阅读顺序可解释；超出资源预算时产生诊断，不出现超时或无声丢失 |
| 含修订痕迹、删除文本、隐藏文字或批注的 Word | 只使用最终可见正文；辅助内容保留诊断但不泄漏进答案、`tts_text`或外部字段；无法判定可见性时阻断受影响自动任务 |

所有场景都必须验证以下通用不变量：

- 原文块不被两个 Parser 同时发布为正式结果。
- `raw_text`从不因清洗被覆盖。
- 每个输出节点均可定位回源文档。
- 每个跨文件字段或媒体关系均可定位到当前来源包成员、具体范围和关联/确认决定；上传批次本身不能充当关联证据。
- 新规则不会改变无关样例的结构边界、题型 owner 或音频命名。
- 解析器、检测器和 UI 显示的文档类型使用同一份最终裁决结果。
- 用户确认和已产生外部副作用的历史范围不会被后续自动解析静默改写。
- 运行生命周期、解析终态、新鲜度、覆盖率、校验结果和字段裁决状态彼此独立；任何一个状态不得被另一个状态字段复用或覆盖。
- 每条人工决定均可追溯到基准 revision、目标签名、操作者和生成的结构修订；历史决定、候选和外部回执均不可被原地删除。

## 11. 明确的非目标与待确认项

### 11.1 非目标

- 不要求一次性支持所有出版社、所有年级和所有历史版式。
- 不根据文件名自动做高风险分类或拆分。
- 不因同一次上传、同一目录或相似文件名自动把多份独立文件合并为一个来源包，或自动把答案附件填入题目。
- 不把每个自然段、每个换行都强制拆成独立音频。
- 不因当前“一个文档通常一套卷”而限制未来一个文档包含多个套卷。
- 不为课文、词汇、试卷分别建立三套平行的执行状态机。

### 11.2 后续需要业务确认的规则

1. 已支持教材的正式 profile 清单，以及每个 profile 覆盖的年级、册别和样例文件。
2. `专项`与`套卷`在产品和外部平台上的最终判定依据；尤其是“多个专项连续出现在一个文档”时，是拆成多个录入单元还是作为一个专项合集提交。
3. 模仿朗读的最小业务单元：一篇材料、一个朗读任务还是按段落/句子提交。
4. 各课文 profile 的默认音频策略是否由教材、内容类型还是用户配置决定。
5. `unknown`、`ambiguous`、`partial`解析结果在内容核对页中的默认交互和外部录入阻断规则。

这些待确认项不阻碍先落地统一结构和诊断能力；它们只决定自动结论的置信度、默认值和后续外部提交策略。

## 12. 最终原则

解析系统的目标不是“用更多正则覆盖更多文档”，而是把真实的业务结构稳定地表达出来：

```text
先确认哪些上传文件构成同一个来源包，并冻结主文档和已确认附件
  → 再判主文档属于什么领域
  → 再识别它的组织方式和解析画像
  → 再划分可独立处理的录入单元
  → 再解析单元中的章节、大题、材料和小题
  → 将答案、材料和媒体以显式关系绑定到实体
  → 最后按同一份结构决定音频与外部录入范围
```

只要遵守这个顺序，课文的版本复杂度会被隔离在 profile 内，试卷的题型复杂度会被隔离在大题 Parser 内，词汇 Excel 也能保持独立且简单；下游则消费统一的结构树和显式关系图，而不需要猜测某个 `category` 字符串到底代表文档类型、章节、题型还是音频片段。
