# Prompt 集中管理

项目发给模型的系统提示词、各步骤任务、共用规则、重试指令和工具描述统一放在这里。
运行时代码通过 `core/prompts.py` 读取这些文件，修改提示词无需修改 Python 中的长字符串。

## 按用途查找

| 目录 | 用途 |
| --- | --- |
| `system/` | 默认 Agent、Peach Agent、DataModel 诊断和修复 Agent 的系统提示词 |
| `language/` | 简体中文和其他语言的输出要求 |
| `discovery/` | Step 1 报文类型提取、Step 2 自定义标量运行时生成 |
| `datamodel/` | Step 2 规划、共享模块和报文族生成、组装修复，以及 Step 3 诊断和修复 |
| `mutators/` | Step 4 语义价值评估、方法契约和实现，以及 Step 5 验证修复 |
| `diagnosis/` | 独立 DataModel 诊断 CLI 的系统提示词和任务，输出结构直接写在任务中 |
| `shared/` | 随运行状态变化的自定义标量上下文和用户补充指令 |
| `tools/` | 模型可见的工具说明，如 `Read_File`、`Apply_Patch`、`RFC_Search` |

## 修改方式

直接修改对应的 UTF-8 `.txt` 文件。正文已清理多余缩进和空行，保留段落间隔及代码示例的相对缩进；文件内容会原样发送给模型。
追加到任务末尾的指令片段保留一个必要的起始换行，避免与前文粘连。

动态参数使用 `{{参数名}}`，例如 `{{protocol_name}}`、`{{packet_packet_type}}`。
固定建模规则、源码风格、C#/.NET 版本要求、文档路径和输出格式直接写在对应提示词中，
只把协议、运行路径、日志、评估结果等实际变化的内容作为参数。
参数由调用代码提供，不在模板里执行 Python 表达式。编辑正文时保留所需占位符；
增加或重命名占位符时，需要同步调整调用处的 `render_prompt(...)` 参数。
缺少参数会明确报错，并指出文件名和参数名。

JSON 和 C# 的普通 `{`、`}` 不需要转义。参数值只替换一次，插入的日志、代码或用户输入
即使包含 `{{...}}`，也不会再次作为模板处理。

独立诊断任务的 JSON 输出示例直接放在 `diagnosis/task.txt` 中。
生成流程要求阅读的 RFC、`docs/peach-dsl.md` 和 SDK 文档仍由工具按需读取。
运行数据、工具返回值、历史日志和备份中的旧提示词不属于这里的模板。

模板路径相对于仓库中的 `prompts/` 解析，不受启动目录影响。文件读取不做缓存；
但系统配置、工具说明和部分元数据会在导入或创建 Agent 时读取，修改后重启流水线可使所有提示词生效。

## 验证

```bash
uv run python -m unittest tests.test_prompts tests.test_agent_language tests.test_mutator_assessment tests.test_mutator_pipeline
```

`tests/test_prompts.py` 会检查实际代码中的模板引用和动态参数，避免遗漏文件或引入无法渲染的占位符。
